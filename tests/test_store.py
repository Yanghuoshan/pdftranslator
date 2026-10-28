# -*- coding: utf-8 -*-
from __future__ import annotations

import shutil

from pdf_translator.store import Store, _atomic_write, initialize_book


def test_cache_filenames_are_one_based(tmp_path):
    store = Store(tmp_path, "book-1234abcd")
    assert store.zh_path(6).name == "00006.json"
    assert store.draft_path(6).name == "00006.txt"
    assert store.img_path(6, 2).name == "00006@2x.png"


def test_atomic_write_overwrites_without_temp_files(tmp_path):
    target = tmp_path / "nested" / "value.bin"
    _atomic_write(target, b"one")
    _atomic_write(target, b"two")
    assert target.read_bytes() == b"two"
    assert list((tmp_path / "nested").glob("*.tmp")) == []


def test_stats_exclude_empty_and_skipped(tmp_path):
    store = Store(tmp_path, "slug")
    store.save_book({"page_count": 6, "empty_pages": [3]})
    store.save_en([["a"]] * 6)
    store.save_zh(1, {"text": "第一页中文", "skipped": False})
    store.save_zh(2, {"text": "", "skipped": True})
    store.save_zh(9, {"text": "越界页", "skipped": False})

    stats = store.stats()
    assert stats["translated_pages"] == [1]
    assert stats["skipped_pages"] == [2]
    assert stats["empty_pages"] == [3]
    assert stats["missing_pages"] == [4, 5, 6]
    assert stats["remaining"] == 3
    assert stats["next_page"] == 4
    assert stats["done"] is False
    assert stats["percent"] == 0.5


def test_prev_tail_and_invalid_files(tmp_path):
    store = Store(tmp_path, "slug")
    store.save_book({"page_count": 5, "empty_pages": []})
    store.save_zh(1, {"text": "第一页译文结尾", "skipped": False})
    store.save_zh(3, {"text": "跳过页", "skipped": True})
    assert store.prev_tail(4) == "第一页译文结尾"
    assert store.prev_tail(1) == ""

    bad = store.zh_dir / "00002.json"
    bad.write_text("{not json", encoding="utf-8")
    assert store.load_zh(2) is None
    assert [x["file"] for x in store.invalid_files()] == [str(bad)]
    assert 2 not in store.scan()
    assert store.scan()[1]["text"] == "第一页译文结尾"


def test_initialize_reuse_and_content_identity(project):
    store, meta, reused = initialize_book(project.pdf, project.cache, log=lambda *a: None)
    assert reused is False
    assert meta["page_count"] == 4
    assert meta["empty_pages"] == [3]
    assert store.load_en() is not None

    again, meta2, reused2 = initialize_book(project.pdf, project.cache, log=lambda *a: None)
    assert reused2 is True
    assert meta2["book_id"] == meta["book_id"]

    copy = project.tmp / "renamed.pdf"
    shutil.copyfile(project.pdf, copy)
    other = Store.for_pdf(copy, project.cache)
    assert other.book_id == store.book_id          # 内容一致 -> 同一本书
    assert other.slug != store.slug                # 文件名不同 -> 目录不同
