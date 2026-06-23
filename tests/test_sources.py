"""Filesystem source listing + staging."""

from __future__ import annotations

import pytest

from evilflowers_books_digitalizer.sources import FilesystemBookSource, build_source


@pytest.fixture
def mount(tmp_path):
    base = tmp_path / "raw-scans" / "fad" / "FA-skeny"
    book = base / "CVI_OPACID_FA_9788022750462" / "stream_pages_tif"
    book.mkdir(parents=True)
    for i in range(1, 4):
        (book / f"p{i:04d}.tif").write_bytes(b"x" * 10)  # 10 bytes each
    (base / "@Recycle").mkdir()
    (base / "lost+found").mkdir()
    return tmp_path / "raw-scans"


def test_build_source_filesystem(mount):
    src = build_source(
        {"backend": "filesystem", "root": str(mount), "paths": {"fad": "fad/FA-skeny"}}, "fad"
    )
    assert isinstance(src, FilesystemBookSource)
    assert src.list_books() == ["CVI_OPACID_FA_9788022750462"]  # system dirs excluded


def test_get_book_lists_pages_in_order(mount):
    src = FilesystemBookSource("fad", mount / "fad" / "FA-skeny")
    book = src.get_book("CVI_OPACID_FA_9788022750462")
    assert book.n_pages == 3
    assert book.total_bytes == 30


def test_stage_symlink_is_zero_copy_and_resumable(mount, tmp_path):
    src = FilesystemBookSource("fad", mount / "fad" / "FA-skeny", stage_mode="symlink")
    dest = tmp_path / "cache" / "book"
    staged = src.stage_book("CVI_OPACID_FA_9788022750462", dest, progress=False)
    assert all(p.is_symlink() for p in staged)
    assert staged == src.stage_book("CVI_OPACID_FA_9788022750462", dest, progress=False)


def test_stage_copy_materializes_real_files(mount, tmp_path):
    src = FilesystemBookSource("fad", mount / "fad" / "FA-skeny", stage_mode="copy")
    dest = tmp_path / "cache" / "book"
    staged = src.stage_book("CVI_OPACID_FA_9788022750462", dest, progress=False)
    assert not any(p.is_symlink() for p in staged)
    assert all(p.stat().st_size == 10 for p in staged)


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        build_source({"backend": "nope"}, "fad")


def test_invalid_stage_mode_raises(tmp_path):
    with pytest.raises(ValueError):
        FilesystemBookSource("fad", tmp_path, stage_mode="teleport")
