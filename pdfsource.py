# -*- coding: utf-8 -*-
"""PDF 取文与渲染：按段落切分、去掉页眉页脚、懒渲染原页图片。"""
from __future__ import annotations

import io
import re
import statistics
import threading
from collections import Counter
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium

# 出现在页边距里的页码（阿拉伯数字/罗马数字）
_NUM_ONLY = re.compile(r"^[\s\divxIVX\.\-–—]{1,7}$")
_SENT_END = (".", "!", "?", ":", ";", '"', "”", "’", ")", "」", "』")


class Book:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.plumber = pdfplumber.open(str(self.path))
        self.fium = pdfium.PdfDocument(str(self.path))
        self.page_count = len(self.plumber.pages)
        self._lock = threading.RLock()
        self._paras: dict[int, list[str]] = {}

    def close(self) -> None:
        try:
            self.plumber.close()
        except Exception:
            pass

    def page_size(self, n: int) -> tuple[float, float]:
        p = self.plumber.pages[n]
        return float(p.width), float(p.height)

    # ---------- 文本 ----------
    def paragraphs(self, n: int) -> list[str]:
        with self._lock:
            if n in self._paras:
                return self._paras[n]
        paras = self._extract(n)
        with self._lock:
            self._paras[n] = paras
        return paras

    def text(self, n: int) -> str:
        return "\n\n".join(self.paragraphs(n))

    def set_paras(self, pages: list[list[str]]) -> None:
        with self._lock:
            self._paras = {i: list(p) for i, p in enumerate(pages)}

    def extract_all(self, on_progress=None) -> list[list[str]]:
        """全本抽取（后台跑），顺带剔除跨页重复的页眉/页脚。"""
        pages = [self._extract(n) for n in range(self.page_count)]
        counter: Counter[str] = Counter()
        for paras in pages:
            for p in set(paras):
                if len(p) <= 80:
                    counter[p] += 1
        threshold = max(8, int(self.page_count * 0.25))
        noise = {p for p, c in counter.items() if c >= threshold}
        cleaned = [[p for p in paras if p not in noise] for paras in pages]
        with self._lock:
            for i, paras in enumerate(cleaned):
                self._paras[i] = paras
        if on_progress:
            on_progress(self.page_count)
        return cleaned

    def _extract(self, n: int) -> list[str]:
        page = self.plumber.pages[n]
        try:
            raw = page.extract_text_lines(x_tolerance=1, y_tolerance=3) or []
        except Exception:
            raw = []
        items = []
        height = float(page.height)
        for line in raw:
            t = (line.get("text") or "").strip()
            if not t:
                continue
            top, bottom = float(line["top"]), float(line["bottom"])
            if _NUM_ONLY.match(t) and (top < height * 0.12 or bottom > height * 0.88):
                continue  # 页码
            items.append({"text": t, "x0": float(line["x0"]), "top": top, "bottom": bottom})
        return self._group(items)

    @staticmethod
    def _group(items: list[dict]) -> list[str]:
        if not items:
            return []
        heights = [i["bottom"] - i["top"] for i in items]
        med_h = statistics.median(heights) or 10.0
        body_left = statistics.median([i["x0"] for i in items])
        paras: list[str] = []
        cur: list[str] = []
        prev: dict | None = None
        for it in items:
            if prev is None:
                cur = [it["text"]]
                prev = it
                continue
            gap = it["top"] - prev["bottom"]
            indent = it["x0"] - body_left
            prev_indent = prev["x0"] - body_left
            new_para = gap > 0.7 * med_h
            if not new_para and indent > 1.2 and prev_indent <= 1.2 and gap > 0.15 * med_h:
                new_para = True  # 首行缩进
            if not new_para and prev["text"].endswith(_SENT_END) and gap > 0.45 * med_h and it["text"][:1].isupper():
                new_para = True  # 段末留白较大
            if new_para:
                paras.append(Book._join(cur))
                cur = [it["text"]]
            else:
                cur.append(it["text"])
            prev = it
        if cur:
            paras.append(Book._join(cur))
        return [p for p in paras if p]

    @staticmethod
    def _join(lines: list[str]) -> str:
        out = lines[0]
        for nxt in lines[1:]:
            if out.endswith("-") and nxt[:1].islower():
                out = out[:-1] + nxt          # 断词连字符
            else:
                out = out + " " + nxt
        return re.sub(r"\s+", " ", out).strip()

    # ---------- 图片 ----------
    def render_png(self, n: int, scale: int = 2) -> bytes:
        bitmap = self.fium[n].render(scale=scale)
        img = bitmap.to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
