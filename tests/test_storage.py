from pathlib import Path

import pytest

from app import storage as S


def test_sanitize_component():
    assert S.sanitize_component('契約:書/<名>?*') == "契約_書__名___"
    assert S.sanitize_component("CON") == "_CON"
    assert S.sanitize_component("lpt1.txt") == "_lpt1.txt"
    assert S.sanitize_component("名称. ") == "名称"
    assert S.sanitize_component("..") == "名称未設定"
    assert len(S.sanitize_component("あ" * 200)) == S.MAX_NAME_PART


def test_build_filename():
    assert S.build_pdf_filename("C000001", "業務委託契約書", "株式会社A") == "C000001_業務委託契約書_株式会社A.pdf"
    assert S.build_pdf_filename("C000002", None, None) == "C000002_契約書.pdf"


def test_safe_join_blocks_traversal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert S.safe_join(root, "a", "b.pdf") == (root / "a" / "b.pdf").resolve()
    with pytest.raises(S.StorageError):
        S.safe_join(root, "..", "evil.pdf")
    with pytest.raises(S.StorageError):
        S.safe_join(root, "C:\\Windows\\evil.pdf")


def test_fit_path_shortens_but_keeps_mgmt_no(tmp_path):
    root = tmp_path / ("x" * 120)
    root.mkdir()
    rel = S.fit_path(root, "カテゴリー", "C000001_" + "長" * 60 + "_" + "相" * 60 + ".pdf")
    assert rel.startswith("カテゴリー/C000001")
    assert len(str(root.resolve())) + 1 + len(rel) <= S.MAX_PATH


def test_unique_relpath_and_no_overwrite(tmp_path):
    root = tmp_path
    (root / "f").mkdir()
    (root / "f" / "a.pdf").write_bytes(b"1")
    assert S.unique_relpath(root, "f/a.pdf") == "f/a_2.pdf"
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    with pytest.raises(S.StorageError):
        S.copy_verified(src, root / "f" / "a.pdf", None)
    assert (root / "f" / "a.pdf").read_bytes() == b"1"  # 既存ファイルは変更されない


def test_copy_verified_checks_hash(tmp_path):
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    with pytest.raises(S.StorageError):
        S.copy_verified(src, tmp_path / "out" / "x.pdf", "0" * 64)
    assert not (tmp_path / "out" / "x.pdf").exists()
    assert not (tmp_path / "out" / "x.pdf.part").exists()
    h = S.copy_verified(src, tmp_path / "out" / "y.pdf", S.sha256_file(src))
    assert h == S.sha256_file(src)
