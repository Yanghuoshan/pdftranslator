# -*- coding: utf-8 -*-
"""缓存与状态管理。

目录结构（默认 .cache/）：
    active_book.json              当前书指针
    <slug>/book.json              书元数据
    <slug>/en.json                全文段落（0-based 数组）
    <slug>/zh/00006.json          第 6 页译文记录（1-based 文件名）
    <slug>/img/00006@2x.png       原页渲染
    <slug>/drafts/00006.txt       next 建议的译文草稿路径
    <slug>/agent-run/             一次性 agent 的空工作目录
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA = 1
EXTRACTOR_VERSION = "1"


def content_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _slug_for(pdf: Path, digest: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", pdf.stem).strip("._-")[:40] or "book"
    return f"{stem}-{digest[:8]}"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                tmp = ""
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _atomic_json(path: Path, data: Any) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


class Store:
    def __init__(self, cache_root: str | Path, slug: str, book_id: str = "",
                 source_hash: str = "", pdf: str | Path | None = None):
        self.cache_root = Path(cache_root)
        self.slug = slug
        self.book_id = book_id
        self.source_hash = source_hash
        self.pdf = Path(pdf) if pdf else None
        self.root = self.cache_root / slug

    # ---------- 定位 ----------
    @classmethod
    def for_pdf(cls, pdf: str | Path, cache_root: str | Path = ".cache") -> "Store":
        pdf = Path(pdf).expanduser().resolve()
        digest = content_sha256(pdf)
        return cls(
            cache_root,
            _slug_for(pdf, digest),
            book_id=digest[:16],
            source_hash="sha256:" + digest,
            pdf=pdf,
        )

    @classmethod
    def load_active(cls, cache_root: str | Path = ".cache") -> "Store | None":
        root = Path(cache_root)
        ptr = root / "active_book.json"
        if ptr.is_file():
            try:
                data = json.loads(ptr.read_text("utf-8"))
                slug = data.get("slug")
            except Exception:
                slug = None
            if slug and (root / slug / "book.json").is_file():
                return cls(root, str(slug), str(data.get("book_id") or ""))
        if not root.is_dir():
            return None
        cands = [
            (d.stat().st_mtime, d.name)
            for d in root.iterdir()
            if d.is_dir() and (d / "book.json").is_file()
        ]
        if not cands:
            return None
        cands.sort(reverse=True)
        return cls(root, cands[0][1])

    def set_active(self) -> None:
        meta = self.load_book() or {}
        _atomic_json(self.cache_root / "active_book.json", {
            "schema": SCHEMA,
            "slug": self.slug,
            "book_id": self.book_id or meta.get("book_id") or "",
            "pdf": meta.get("pdf") or (str(self.pdf) if self.pdf else ""),
        })

    # ---------- 路径 ----------
    @property
    def book_path(self) -> Path:
        return self.root / "book.json"

    @property
    def en_path(self) -> Path:
        return self.root / "en.json"

    @property
    def zh_dir(self) -> Path:
        return self.root / "zh"

    @property
    def img_dir(self) -> Path:
        return self.root / "img"

    @property
    def drafts_dir(self) -> Path:
        return self.root / "drafts"

    @property
    def agent_run_dir(self) -> Path:
        return self.root / "agent-run"

    def zh_path(self, page: int) -> Path:
        return self.zh_dir / f"{page:05d}.json"

    def draft_path(self, page: int) -> Path:
        return self.drafts_dir / f"{page:05d}.txt"

    def img_path(self, page: int, scale: int) -> Path:
        return self.img_dir / f"{page:05d}@{scale}x.png"

    # ---------- 书元数据 ----------
    def load_book(self) -> dict | None:
        if not self.book_path.is_file():
            return None
        try:
            data = json.loads(self.book_path.read_text("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def save_book(self, meta: dict) -> None:
        _atomic_json(self.book_path, meta)

    # ---------- 英文文本 ----------
    def load_en(self) -> list[list[str]] | None:
        if not self.en_path.is_file():
            return None
        try:
            data = json.loads(self.en_path.read_text("utf-8"))
            pages = data.get("pages")
            return pages if isinstance(pages, list) else None
        except Exception:
            return None

    def save_en(self, pages: list[list[str]]) -> None:
        _atomic_json(self.en_path, {
            "schema": SCHEMA,
            "book_id": self.book_id,
            "pages": pages,
        })

    # ---------- 译文 ----------
    def load_zh(self, page: int) -> dict | None:
        p = self.zh_path(page)
        if not p.is_file():
            return None
        try:
            rec = json.loads(p.read_text("utf-8"))
            return rec if isinstance(rec, dict) else None
        except Exception:
            return None

    def save_zh(self, page: int, rec: dict) -> None:
        data = dict(rec)
        data["schema"] = SCHEMA
        data["page"] = int(page)
        data.setdefault("ts", time.time())
        _atomic_json(self.zh_path(page), data)

    def delete_zh(self, page: int) -> None:
        try:
            self.zh_path(page).unlink()
        except FileNotFoundError:
            pass

    def scan(self) -> dict[int, dict]:
        out: dict[int, dict] = {}
        if not self.zh_dir.is_dir():
            return out
        for p in sorted(self.zh_dir.glob("*.json")):
            try:
                rec = json.loads(p.read_text("utf-8"))
                page = int(rec.get("page") or p.stem.split(".")[0])
            except Exception:
                continue
            if page > 0:
                rec["page"] = page
                out[page] = rec
        return out

    def invalid_files(self) -> list[dict]:
        out = []
        if not self.zh_dir.is_dir():
            return out
        for p in sorted(self.zh_dir.glob("*.json")):
            try:
                json.loads(p.read_text("utf-8"))
            except Exception as exc:
                out.append({"file": str(p), "error": str(exc)[:200]})
        return out

    def prev_tail(self, page: int, limit: int = 300) -> str:
        for p in range(page - 1, 0, -1):
            rec = self.load_zh(p)
            if rec and not rec.get("skipped") and str(rec.get("text") or "").strip():
                return str(rec["text"])[-limit:]
        return ""

    # ---------- 图片 ----------
    def load_img(self, page: int, scale: int) -> bytes | None:
        p = self.img_path(page, scale)
        return p.read_bytes() if p.is_file() else None

    def save_img(self, page: int, scale: int, data: bytes) -> None:
        _atomic_write(self.img_path(page, scale), data)

    # ---------- 统计 ----------
    def stats(self) -> dict:
        meta = self.load_book() or {}
        n = int(meta.get("page_count") or 0)
        empty = sorted({
            int(x) for x in (meta.get("empty_pages") or [])
            if 1 <= int(x) <= n
        }) if n else []
        records = self.scan()
        translated = sorted(
            p for p, r in records.items()
            if 1 <= p <= n and not r.get("skipped") and str(r.get("text") or "").strip()
        )
        skipped = sorted(
            p for p, r in records.items()
            if 1 <= p <= n and r.get("skipped")
        )
        done = set(translated) | set(skipped) | set(empty)
        missing = [p for p in range(1, n + 1) if p not in done]
        return {
            "page_count": n,
            "translated": len(translated),
            "skipped": len(skipped),
            "empty_pages": empty,
            "translated_pages": translated,
            "skipped_pages": skipped,
            "missing_pages": missing,
            "remaining": len(missing),
            "next_page": missing[0] if missing else None,
            "done": bool(n) and not missing,
            "percent": round(len(done) / n, 4) if n else 0.0,
        }


def initialize_book(pdf: str | Path, cache_root: str | Path = ".cache",
                    force: bool = False, log=print) -> tuple[Store, dict, bool]:
    """抽取全文并落盘。返回 (store, meta, reused)。"""
    pdf = Path(pdf).expanduser().resolve()
    if not pdf.is_file():
        raise FileNotFoundError(f"找不到 PDF: {pdf}")
    store = Store.for_pdf(pdf, cache_root)
    meta = store.load_book()
    en = store.load_en() if not force else None
    if (meta and en is not None
            and len(en) == int(meta.get("page_count") or -1)
            and meta.get("extractor_version") == EXTRACTOR_VERSION):
        store.set_active()
        return store, meta, True

    from .book import Book

    log(f"抽取正文: {pdf.name}")
    started = time.time()
    book = Book(pdf)
    try:
        page_count = book.page_count

        def progress(done: int, total: int) -> None:
            if done == total or done % 10 == 0:
                log(f"  抽取 {done}/{total}")

        pages = book.extract_all(on_progress=progress)
    finally:
        book.close()

    empty_pages = [
        i + 1 for i, paras in enumerate(pages)
        if not any(str(p).strip() for p in paras)
    ]
    meta = {
        "schema": SCHEMA,
        "book_id": store.book_id,
        "pdf": str(pdf),
        "pdf_name": pdf.name,
        "title": pdf.stem,
        "page_count": page_count,
        "source_hash": store.source_hash,
        "extractor_version": EXTRACTOR_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "empty_pages": empty_pages,
    }
    store.save_en(pages)
    store.save_book(meta)
    store.set_active()
    log(f"完成: {page_count} 页，{len(empty_pages)} 页无文本，用时 {time.time() - started:.1f}s")
    return store, meta, False
