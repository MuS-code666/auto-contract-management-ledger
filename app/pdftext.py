"""PDFの検査・テキスト取得・ページ画像化（pypdfium2 / Apache-2.0, BSD-3-Clause）。

ページ画像は1ページずつ生成して即座に解放し、複数ページ分をメモリに保持しない。
"""
from __future__ import annotations

import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium


class PdfError(Exception):
    def __init__(self, message: str, hint: str = "", code: str = "pdf_error"):
        super().__init__(message)
        self.hint = hint
        self.code = code


@dataclass
class PageText:
    page_no: int
    text: str
    chars: int
    usable: bool
    note: str = ""


@contextmanager
def open_pdf(path: Path):
    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as e:
        msg = str(e).lower()
        if "password" in msg:
            raise PdfError(
                "パスワードで保護されたPDFのため読み込めません。",
                "パスワードを解除したPDFを用意して取り込んでください（元ファイルは変更しません）。",
                "encrypted",
            )
        raise PdfError(
            "PDFとして読み込めません（破損または未対応の形式）。",
            "スキャンし直す、またはPDFを別のソフトで保存し直してから取り込んでください。",
            "corrupt",
        )
    try:
        yield pdf
    finally:
        pdf.close()


def inspect(path: Path, max_mb: int, max_pages: int) -> int:
    """サイズ・形式・ページ数を検査し、ページ数を返す。"""
    size = path.stat().st_size
    if size == 0:
        raise PdfError("ファイルが空です。", "正しいPDFファイルを選択してください。", "empty")
    if size > max_mb * 1024 * 1024:
        raise PdfError(
            f"PDFのサイズが上限（{max_mb} MB）を超えています。",
            "メモリ・処理時間の超過を防ぐための制限です。PDFを分割するか、設定画面で上限を見直してください。",
            "too_large",
        )
    with open(path, "rb") as f:
        head = f.read(1024)
    if b"%PDF-" not in head:
        raise PdfError("PDFファイルではありません。", "拡張子が .pdf のファイルを選択してください。", "not_pdf")
    with open_pdf(path) as pdf:
        n = len(pdf)
    if n == 0:
        raise PdfError("ページがないPDFです。", "", "no_pages")
    if n > max_pages:
        raise PdfError(
            f"ページ数（{n}）が上限（{max_pages}）を超えています。",
            "処理時間とメモリの超過を防ぐための制限です。PDFを分割するか、設定画面で上限を見直してください。",
            "too_many_pages",
        )
    return n


_GOOD = re.compile(r"[぀-ヿ㐀-鿿０-ｚA-Za-z0-9　-〿！-／ー、。・（）「」：；！？\-\.,:;()\[\]/%＆&]")


def clean_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    s = s.replace("￾", "").replace("­", "")
    return s


def judge_usable(text: str, min_chars: int) -> tuple[bool, str, int]:
    body = re.sub(r"\s", "", text)
    n = len(body)
    if n == 0:
        return False, "文字情報なし", 0
    if n < min_chars:
        return False, f"文字情報が少ない（{n}文字）", n
    good = len(_GOOD.findall(body))
    bad = sum(1 for ch in body if ch == "�" or unicodedata.category(ch) in ("Co", "Cn"))
    if bad / n > 0.05 or good / n < 0.6:
        return False, "文字情報が文字化けしている可能性", n
    return True, "", n


def extract_page_text(pdf, index: int, min_chars: int) -> PageText:
    page = pdf[index]
    try:
        tp = page.get_textpage()
        try:
            raw = tp.get_text_bounded()
        finally:
            tp.close()
    finally:
        page.close()
    text = clean_text(raw)
    usable, note, n = judge_usable(text, min_chars)
    return PageText(index + 1, text, n, usable, note)


def render_page_png(pdf, index: int, dpi: int, out_path: Path) -> None:
    """1ページを PNG 画像としてファイルへ書き出す（メモリ上に保持しない）。"""
    page = pdf[index]
    try:
        bitmap = page.render(scale=dpi / 72, grayscale=True)
        try:
            img = bitmap.to_pil()
            img.save(out_path, format="PNG")
            img.close()
        finally:
            bitmap.close()
    finally:
        page.close()


def is_blank_page(png_path: Path) -> bool:
    """画像がほぼ白紙かどうか（空ページ判定）。

    スキャンの点状ノイズをメディアンフィルタで除いてから、暗い画素の割合で判定する。
    署名欄だけの短いページ（暗画素 約0.3%）を空ページと誤判定しないよう、閾値は 0.05% とする。
    """
    from PIL import Image, ImageFilter

    with Image.open(png_path) as im:
        g = im.convert("L")
        w, h = g.size
        small = g.resize((max(1, w // 3), max(1, h // 3)), Image.BOX).filter(ImageFilter.MedianFilter(3))
        hist = small.histogram()
    return sum(hist[:170]) / sum(hist) < 0.0005
