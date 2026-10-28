# -*- coding: utf-8 -*-
"""大模型翻译：OpenAI 兼容 / Anthropic / mock，分段请求、并发、逐页落盘缓存。"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

import httpx
from store import model_slug, resolve_key

SYSTEM_PROMPT = """你是一位资深英译中译者，擅长历史、宗教、哲学类学术著作的中文翻译。

翻译规则：
1. 用简体中文，忠实原文、语句通顺优雅；不要漏译，也不要添加原文没有的内容。
2. 严格保持段落结构：输入用空行分隔段落，输出必须用同样的空行分隔，段落数量与顺序完全一致。
3. 术语表给出的词按术语表翻译；术语表未覆盖的专有名词（人名、地名、书名）保留英文原文。
4. 不要输出任何说明、注释、标题或"译文："之类的前缀，只输出译文正文。
5. 忽略页码、页眉、装饰性符号等无关内容。"""


class TranslationError(RuntimeError):
    pass


class Translator:
    def __init__(self, cfg: dict, book, store, context: str = "", glossary: dict | None = None, log=print):
        self.cfg = cfg
        self.book = book
        self.store = store
        self.context = context or ""
        self.glossary = glossary or {}
        self.log = log

        self.provider = (cfg.get("provider") or "openai").lower()
        self.model = cfg.get("model") or ""
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = resolve_key(cfg)
        self.temperature = float(cfg.get("temperature", 0.2))
        self.timeout = float(cfg.get("timeout", 180))
        self.max_chars = int(cfg.get("max_chars_per_chunk", 1600))
        self.concurrency = max(1, int(cfg.get("concurrency", 4)))
        self.mslug = model_slug(self.provider, self.model)

        self.client = httpx.Client(timeout=self.timeout, trust_env=True)
        self.pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="batch-tr")
        self.fast = ThreadPoolExecutor(max_workers=2, thread_name_prefix="live-tr")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._futures: dict[int, Future] = {}
        self._zh: dict[int, str] = {}
        self._meta: dict[int, dict] = {}
        self.stats = {"tokens": 0, "pages": 0, "errors": 0, "requests": 0}
        self.batch: dict | None = None

        for n, rec in store.translated_pages(self.mslug).items():
            self._zh[n] = rec.get("text") or ""
            self._meta[n] = rec
        if self._zh:
            log(f"已载入 {len(self._zh)} 页缓存译文（{self.provider}/{self.model}）")

    # ---------- 查询 ----------
    def has(self, n: int) -> bool:
        return n in self._zh

    def get(self, n: int) -> str | None:
        return self._zh.get(n)

    def translated(self) -> list[int]:
        return sorted(self._zh)

    def in_flight(self) -> list[int]:
        with self._lock:
            return [n for n, f in self._futures.items() if not f.done()]

    # ---------- 触发 ----------
    def submit(self, pages, force: bool = False) -> dict[int, Future]:
        out = {}
        for n in pages:
            if not force and n in self._zh:
                continue
            with self._lock:
                f = self._futures.get(n)
                if f is None or f.done():
                    f = self.pool.submit(self._run, n, force)
                    self._futures[n] = f
            out[n] = f
        return out

    def ensure(self, pages, force: bool = False, timeout: float | None = None) -> dict[int, dict]:
        """当前页翻译：走优先队列并等待结果，返回 {页: {ok, text, error}}。"""
        result: dict[int, dict] = {}
        futures: dict[int, Future] = {}
        for n in pages:
            if not force and n in self._zh:
                result[n] = {"ok": True, "text": self._zh[n]}
                continue
            with self._lock:
                f = self._futures.get(n)
            if f is None or f.done():
                f = self.fast.submit(self._run, n, force)
                with self._lock:
                    self._futures[n] = f
            futures[n] = f
        for n, f in futures.items():
            try:
                result[n] = {"ok": True, "text": f.result(timeout=timeout)}
            except Exception as exc:  # noqa: BLE001
                result[n] = {"ok": False, "error": str(exc)}
        return result

    def start_batch(self, pages) -> bool:
        if self.batch and not self.batch.get("finished"):
            return False
        pages = [n for n in pages]
        if not pages:
            return False
        self._stop.clear()
        self.batch = {"total": len(pages), "done": 0, "finished": False}
        threading.Thread(target=self._run_batch, args=(pages,), daemon=True).start()
        return True

    def stop_batch(self) -> None:
        self._stop.set()
        if self.batch:
            self.batch["finished"] = True

    def _run_batch(self, pages) -> None:
        sem = threading.Semaphore(self.concurrency)
        futures = []

        def run(n):
            with sem:
                if self._stop.is_set():
                    return
                self._run(n, False)

        for n in pages:
            if self._stop.is_set():
                break
            futures.append(self.pool.submit(run, n))
        for i, f in enumerate(futures):
            if self._stop.is_set():
                f.cancel()
                continue
            try:
                f.result()
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                self.log(f"批翻失败: {exc}")
            if self.batch is not None:
                self.batch["done"] = i + 1
        if self.batch is not None:
            self.batch["finished"] = True
        self.log(f"批翻结束，已翻译 {len(self._zh)}/{self.book.page_count} 页")

    # ---------- 核心 ----------
    def _run(self, n: int, force: bool = False) -> str:
        if not force and n in self._zh:
            return self._zh[n]
        paras = self.book.paragraphs(n)
        body = "\n\n".join(paras)
        if not body.strip():
            return self._save(n, "", {"note": "empty"})
        chunks = self._chunk(paras)
        pieces: list[str] = []
        tail = ""
        for idx, chunk in enumerate(chunks):
            user = self._build_user(n, "\n\n".join(chunk), tail, idx, len(chunks))
            raw = self._call(SYSTEM_PROMPT, user)
            text = self._clean(raw)
            pieces.append(text)
            tail = text[-200:]
        zh = "\n\n".join(p for p in pieces if p.strip())
        return self._save(n, zh, {"chunks": len(chunks)})

    def _save(self, n: int, zh: str, extra: dict) -> str:
        rec = {
            "page": n,
            "provider": self.provider,
            "model": self.model,
            "text": zh,
            "ts": time.time(),
        }
        rec.update(extra)
        self.store.save_zh(n, self.mslug, rec)
        with self._lock:
            self._zh[n] = zh
            self._meta[n] = rec
            self.stats["pages"] += 1
        return zh

    def _build_user(self, n: int, body: str, prev_tail: str, idx: int, total: int) -> str:
        parts = []
        if self.context:
            parts.append(f"[书籍背景] {self.context}")
        if self.glossary:
            table = "\n".join(f"{k} = {v}" for k, v in self.glossary.items())
            parts.append(f"[术语表]\n{table}")
        if prev_tail:
            parts.append(f"[上一页译文结尾，用于保持语气与译名一致]\n{prev_tail}")
        if total > 1:
            parts.append(f"[本页第 {idx + 1}/{total} 段，只需翻译本段]")
        parts.append(f"[待翻译正文]\n{body}")
        return "\n\n".join(parts)

    def _chunk(self, paras: list[str]) -> list[list[str]]:
        chunks: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for p in paras:
            if cur and size + len(p) > self.max_chars and size > self.max_chars * 0.5:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(p)
            size += len(p) + 2
        if cur:
            chunks.append(cur)
        if len(chunks) > 1 and sum(len(p) for p in chunks[-1]) < self.max_chars * 0.2:
            chunks[-2].extend(chunks[-1])
            chunks.pop()
        return chunks or [[]]

    def _call(self, system: str, user: str) -> str:
        last: Exception | None = None
        for attempt in range(3):
            if self._stop.is_set():
                raise TranslationError("翻译已取消")
            try:
                if self.provider == "mock":
                    return "[离线自检译文] " + user.split("[待翻译正文]")[-1][:80] + " …"
                if self.provider in ("anthropic", "claude"):
                    return self._anthropic(system, user)
                return self._openai(system, user)
            except TranslationError as exc:
                last = exc
                if attempt == 2 or not re.search(r"(429|5\d\d|timeout|timed out|超时|连接)", str(exc), re.I):
                    raise
                time.sleep(2 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt == 2:
                    break
                time.sleep(2 * (attempt + 1))
        raise TranslationError(str(last) if last else "翻译失败")

    def _openai(self, system: str, user: str) -> str:
        if not self.base_url:
            raise TranslationError("未配置 base_url")
        if not self.api_key:
            raise TranslationError("未配置 API Key：请在 config.json 填写 api_key 或设置环境变量 TRANSLATE_API_KEY")
        resp = self.client.post(
            self.base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "temperature": self.temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        if resp.status_code >= 400:
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        if not content:
            raise TranslationError("模型返回空内容")
        usage = data.get("usage") or {}
        self.stats["tokens"] += int(usage.get("total_tokens") or 0)
        self.stats["requests"] += 1
        return content

    def _anthropic(self, system: str, user: str) -> str:
        if not self.api_key:
            raise TranslationError("未配置 API Key：请设置环境变量 ANTHROPIC_API_KEY")
        base = self.base_url or "https://api.anthropic.com/v1"
        resp = self.client.post(
            base + "/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "temperature": self.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        if resp.status_code >= 400:
            raise TranslationError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        blocks = data.get("content") or []
        content = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if not content:
            raise TranslationError("模型返回空内容")
        usage = data.get("usage") or {}
        self.stats["tokens"] += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        self.stats["requests"] += 1
        return content

    @staticmethod
    def _clean(text: str) -> str:
        t = text.strip()
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
        t = re.sub(r"^(译文|翻译|Translation)\s*[:：]\s*", "", t)
        return re.sub(r"\n{3,}", "\n\n", t).strip()
