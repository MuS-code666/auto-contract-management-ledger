"""OCRアダプター。

新しいOCRエンジンを追加する場合は OcrEngine を継承し、get_engine() に登録する。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path


class OcrUnavailable(Exception):
    """OCRエンジンが未導入・設定不備。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class OcrFailed(Exception):
    pass


class OcrEngine:
    name = "base"

    def check(self) -> dict:
        """動作確認。{'ok': bool, 'message': str, 'hint': str, 'detail': {...}}"""
        raise NotImplementedError

    def recognize(self, image_path: Path, timeout: float, cancel=None) -> str:
        """cancel: 中止要求を返す関数。True になったら処理を打ち切る。"""
        raise NotImplementedError


class NoOcr(OcrEngine):
    name = "none"

    def check(self) -> dict:
        return {"ok": False, "message": "OCRは無効に設定されています。",
                "hint": "画像だけのPDFを読み取るには、設定画面でOCRエンジンを「Tesseract」にしてください。"}

    def recognize(self, image_path: Path, timeout: float, cancel=None) -> str:
        raise OcrUnavailable("OCRは無効に設定されています。")


_CJK = r"[\u3000-\u30ff\u3400-\u9fff\uff00-\uffef]"


def tidy_ocr_text(s: str) -> str:
    """Tesseract が日本語の文字間に挿入する空白を除去する。"""
    s = s.replace("\r\n", "\n")
    s = re.sub(rf"(?<={_CJK})[ \t]+(?={_CJK})", "", s)
    s = re.sub(rf"(?<={_CJK})[ \t]+(?=[0-9０-９])", "", s)
    s = re.sub(rf"(?<=[0-9０-９])[ \t]+(?={_CJK})", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


class TesseractOcr(OcrEngine):
    """Tesseract OCR（Apache-2.0）を外部プロセスとして実行する。ネットワークは使用しない。"""

    name = "tesseract"
    DEFAULT_LOCATIONS = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
    ]

    def __init__(self, path: str = "", languages: str = "jpn+jpn_vert", app_root: Path | None = None):
        self.configured = path
        self.languages = languages or "jpn"
        self.app_root = app_root

    def exe(self) -> str | None:
        cands = []
        if self.configured:
            cands.append(self.configured)
        if self.app_root:
            cands.append(str(self.app_root / "tools" / "tesseract" / "tesseract.exe"))
        cands += self.DEFAULT_LOCATIONS
        for c in cands:
            if c and Path(c).is_file():
                return c
        return shutil.which("tesseract")

    def _run(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        exe = self.exe()
        if not exe:
            raise OcrUnavailable(
                "Tesseract OCR が見つかりません。",
                "README の「OCRの導入」に従って Tesseract と日本語データ(jpn)を導入し、"
                "設定画面で tesseract.exe の場所を指定してください。",
            )
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = dict(os.environ)
        env.setdefault("OMP_THREAD_LIMIT", "4")
        return subprocess.run([exe, *args], capture_output=True, timeout=timeout, creationflags=flags, env=env)

    def check(self) -> dict:
        try:
            ver = self._run(["--version"], 20)
            langs = self._run(["--list-langs"], 20)
        except OcrUnavailable as e:
            return {"ok": False, "message": str(e), "hint": e.hint}
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "message": f"Tesseract を起動できません（{type(e).__name__}）。",
                    "hint": "tesseract.exe のパスを確認してください。"}
        out = (langs.stdout or b"").decode("utf-8", "replace") + (langs.stderr or b"").decode("utf-8", "replace")
        available = {l.strip() for l in out.splitlines()[1:] if l.strip()}
        need = [l for l in self.languages.split("+") if l]
        missing = [l for l in need if l not in available]
        version = ((ver.stdout or b"") + (ver.stderr or b"")).decode("utf-8", "replace").splitlines()[:1]
        if missing:
            return {"ok": False, "message": f"言語データが不足しています: {', '.join(missing)}",
                    "hint": "tessdata フォルダに jpn.traineddata（縦書きは jpn_vert.traineddata）を配置してください。",
                    "detail": {"exe": self.exe(), "version": version, "languages": sorted(available)}}
        return {"ok": True, "message": "Tesseract OCR は利用可能です。",
                "detail": {"exe": self.exe(), "version": version, "languages": sorted(available)}}

    def recognize(self, image_path: Path, timeout: float, cancel=None) -> str:
        exe = self.exe()
        if not exe:
            raise OcrUnavailable("Tesseract OCR が見つかりません。", "設定画面で tesseract.exe の場所を指定してください。")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = dict(os.environ)
        env.setdefault("OMP_THREAD_LIMIT", "4")
        try:
            proc = subprocess.Popen([exe, str(image_path), "stdout", "-l", self.languages, "--psm", "3"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags, env=env)
        except OSError as e:
            raise OcrFailed(f"OCRを実行できません（{type(e).__name__}）")
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel and cancel():
                    proc.kill()
                    proc.communicate()
                    raise OcrFailed("中止されました")
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.communicate()
                    raise OcrFailed(f"OCRがタイムアウトしました（{int(timeout)}秒）")
        if proc.returncode != 0:
            e = (err or b"").decode("utf-8", "replace")
            if "Failed loading language" in e or "Error opening data file" in e:
                raise OcrUnavailable(
                    "Tesseract の日本語データが見つかりません。",
                    "tessdata フォルダに jpn.traineddata を配置してください。",
                )
            raise OcrFailed("OCRの実行に失敗しました")
        return tidy_ocr_text((out or b"").decode("utf-8", "replace"))


class RapidOcr(OcrEngine):
    """RapidOCR（Apache-2.0）＋ PP-OCR の ONNX モデル（Apache-2.0）を ONNX Runtime（MIT）で実行する。

    pip で導入でき、日本語を含む認識モデルがパッケージに同梱されている。
    実行時の自動ダウンロードを防ぐため、モデルファイルのパスを明示し、存在しなければ処理しない。
    """

    name = "rapidocr"
    _cache: dict = {}
    _lock = threading.Lock()

    def __init__(self, model_dir: str = ""):
        self.model_dir = model_dir

    def _models(self) -> dict[str, Path] | None:
        try:
            import rapidocr  # noqa: F401
        except ImportError:
            return None
        base = Path(self.model_dir) if self.model_dir else Path(rapidocr.__file__).parent / "models"
        found = {}
        for key, pattern in (("det", "*det*.onnx"), ("rec", "*rec*.onnx"), ("cls", "*cls*.onnx")):
            files = sorted(base.glob(pattern))
            if not files:
                return None
            found[key] = files[-1]
        return found

    def check(self) -> dict:
        try:
            import onnxruntime  # noqa: F401
            import rapidocr  # noqa: F401
        except ImportError:
            return {"ok": False, "message": "RapidOCR が導入されていません。",
                    "hint": "setup.bat を再実行するか、README の「OCRの導入」を参照してください。"}
        m = self._models()
        if not m:
            return {"ok": False, "message": "RapidOCR のモデルファイルが見つかりません（自動ダウンロードはしません）。",
                    "hint": "README の「OCRの導入」に従ってモデルを配置してください。"}
        return {"ok": True, "message": "RapidOCR は利用可能です。",
                "detail": {"models": [p.name for p in m.values()], "version": getattr(rapidocr, "__version__", "")}}

    def _engine(self):
        m = self._models()
        if not m:
            raise OcrUnavailable("RapidOCR のモデルファイルが見つかりません。", "README の「OCRの導入」を参照してください。")
        key = tuple(str(p) for p in m.values())
        with self._lock:
            if key not in self._cache:
                from rapidocr import RapidOCR

                self._cache.clear()
                self._cache[key] = RapidOCR(params={
                    "Global.log_level": "warning",
                    "Det.model_path": str(m["det"]), "Rec.model_path": str(m["rec"]), "Cls.model_path": str(m["cls"]),
                })
            return self._cache[key]

    def recognize(self, image_path: Path, timeout: float, cancel=None) -> str:
        try:
            eng = self._engine()
        except ImportError:
            raise OcrUnavailable("RapidOCR が導入されていません。", "setup.bat を再実行してください。")
        start = time.monotonic()
        try:
            with self._lock:  # ONNXセッションの同時実行を避ける
                res = eng(str(image_path))
        except Exception as e:
            raise OcrFailed(f"OCRの実行に失敗しました（{type(e).__name__}）")
        if time.monotonic() - start > timeout:
            raise OcrFailed(f"OCRがタイムアウトしました（{int(timeout)}秒）")
        txts = list(getattr(res, "txts", None) or [])
        return tidy_ocr_text("\n".join(txts))


def get_engine(ocr_settings: dict, app_root: Path | None = None) -> OcrEngine:
    eng = (ocr_settings or {}).get("engine", "none")
    if eng == "tesseract":
        return TesseractOcr(ocr_settings.get("tesseract_path", ""), ocr_settings.get("languages", "jpn"), app_root)
    if eng == "rapidocr":
        return RapidOcr(ocr_settings.get("rapidocr_model_dir", ""))
    return NoOcr()
