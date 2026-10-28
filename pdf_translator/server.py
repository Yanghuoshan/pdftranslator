# -*- coding: utf-8 -*-
"""本地 Web 阅读器。

HTTP API 全部使用 1-based 页码。agent 模式下按 T 只拉起一次短命子进程，
页面切换/切中文不会自动翻译；“一键翻译全书”按页顺序逐页拉起一次性 agent，
显式触发、可随时停止；API 模式保留原有预热/预取/批翻。
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .api_translator import ApiTranslator
from .book import Book
from .bridge import AgentBridge, AgentError
from .prompts import source_text, text_hash, validate_translation
from .store import Store

WEB = files("pdf_translator") / "web"
_MAX_BODY = 2 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint

    def payload(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint}


class App:
    def __init__(self, store: Store, meta: dict, cfg: dict, backend: str,
                 api: ApiTranslator, bridge: AgentBridge, context: str, glossary: dict,
                 log=print):
        self.store = store
        self.meta = meta
        self.cfg = cfg
        self.backend = backend
        self.api = api
        self.bridge = bridge
        self.context = context
        self.glossary = glossary
        self.log = log
        self.scale = int(cfg.get("render_scale", 2))
        self.pdf = Path(meta.get("pdf") or "")
        if not self.pdf.is_file():
            raise RuntimeError(f"找不到原 PDF: {self.pdf}（阅读器需要它渲染页面）")
        self.book = Book(self.pdf)
        en = store.load_en()
        if en is not None:
            self.book.set_paras(en)
        self.page_count = int(meta.get("page_count") or self.book.page_count)

        self._state_lock = threading.Lock()
        self._inflight: set[int] = set()
        self._page_locks: dict[int, threading.Lock] = {}
        self._agent_sem = threading.Semaphore(int(self.bridge.cfg.get("max_concurrent") or 1))
        self._img_lock = threading.Lock()
        self._batch: dict | None = None
        self._stop = threading.Event()

    # ---------- 基础 ----------
    def log_line(self, *args) -> None:
        self.log(*args)

    def validate(self, page: int) -> int:
        if page < 1 or page > self.page_count:
            raise ApiError(400, "page_out_of_range", f"页码 {page} 超出范围 1..{self.page_count}")
        return page

    def paragraphs(self, page: int) -> list[str]:
        return self.book.paragraphs(page - 1)

    def backend_label(self) -> str:
        if self.backend == "agent":
            return "agent: " + (self.bridge.label() or "未配置")
        if self.backend == "api":
            return self.api.label
        return "只读"

    def _lock_for(self, page: int) -> threading.Lock:
        with self._state_lock:
            lock = self._page_locks.get(page)
            if lock is None:
                lock = threading.Lock()
                self._page_locks[page] = lock
            return lock

    def _mark(self, page: int, active: bool) -> None:
        with self._state_lock:
            if active:
                self._inflight.add(page)
            else:
                self._inflight.discard(page)

    def in_flight(self) -> list[int]:
        with self._state_lock:
            return sorted(self._inflight)

    # ---------- 负载 ----------
    def meta_payload(self) -> dict:
        try:
            page_size = self.book.page_size(0)
        except Exception:
            page_size = [595.0, 842.0]
        return {
            "file": self.meta.get("pdf_name"),
            "title": self.meta.get("title"),
            "pages": self.page_count,
            "page_size": page_size,
            "model": self.backend_label(),
            "backend": self.backend,
            "backend_label": self.backend_label(),
            "auto_translate": self.backend == "api",
            "prefetch": int(self.cfg.get("prefetch_next_pages", 0)) if self.backend == "api" else 0,
            "has_key": self.api.available(),
            "context": self.context,
            "cache": str(self.store.root),
        }

    def page_payload(self, page: int) -> dict:
        paras = self.paragraphs(page)
        rec = self.store.load_zh(page)
        text = str(rec.get("text") or "") if rec and not rec.get("skipped") else None
        return {
            "page": page,
            "paras": paras,
            "en": source_text(paras),
            "zh": text,
            "has_zh": bool(text and text.strip()),
            "skipped": bool(rec and rec.get("skipped")),
            "backend": rec.get("backend") if rec else None,
            "pending": page in self.in_flight(),
        }

    def status_payload(self) -> dict:
        s = self.store.stats()
        with self._state_lock:
            inflight = sorted(self._inflight)
        return {
            "pages": s["page_count"],
            "translated": s["translated"],
            "translated_pages": s["translated_pages"],
            "skipped": s["skipped"],
            "skipped_pages": s["skipped_pages"],
            "empty_pages": s["empty_pages"],
            "remaining": s["remaining"],
            "next_page": s["next_page"],
            "done": s["done"],
            "in_flight": inflight,
            "batch": self.batch_status(),
            "backend": self.backend,
            "backend_label": self.backend_label(),
            "auto_translate": self.backend == "api",
            "extract": {"done": self.page_count, "total": self.page_count, "ready": True},
            "stats": dict(self.api.stats),
            "model": self.backend_label(),
            "has_key": self.api.available(),
        }

    # ---------- 翻译 ----------
    def translate_page(self, page: int, force: bool = False, wait: bool = True) -> dict:
        self.validate(page)
        if not wait:
            threading.Thread(target=self._translate_sync, args=(page, force), daemon=True).start()
            return {"ok": True, "page": page, "accepted": True}
        return self._translate_sync(page, force)

    def _save(self, page: int, text: str, backend: str, provider: str, model: str,
              src: str, source_paragraphs: int, warnings: list[str]) -> dict:
        checked = validate_translation(self.paragraphs(page), text)
        if checked["error"]:
            raise ApiError(502, "invalid_translation", f"译文未通过校验: {checked['error']}")
        self.store.save_zh(page, {
            "text": text,
            "skipped": False,
            "backend": backend,
            "provider": provider,
            "model": model,
            "source_hash": text_hash(src),
            "paragraph_count": checked["paragraph_count"],
            "source_paragraph_count": source_paragraphs,
            "note": "",
        })
        return {
            "ok": True,
            "page": page,
            "text": text,
            "backend": backend,
            "cached": False,
            "warnings": warnings + checked["warnings"],
        }

    def _agent_translate(self, page: int, paras: list[str], prev_tail: str) -> dict:
        with self._agent_sem:
            return self.bridge.translate(
                page=page,
                paragraphs=paras,
                context=self.context,
                glossary=self.glossary,
                prev_tail=prev_tail,
            )

    def _api_translate(self, paras: list[str], prev_tail: str) -> str:
        if not self.api.available():
            raise ApiError(503, "api_unavailable", "未配置 API Key，API 兜底不可用")
        return self.api.translate_paras(paras, prev_tail)

    def _translate_sync(self, page: int, force: bool) -> dict:
        lock = self._lock_for(page)
        with lock:
            existing = self.store.load_zh(page)
            if (not force and existing and not existing.get("skipped")
                    and str(existing.get("text") or "").strip()):
                return {
                    "ok": True, "page": page, "text": existing.get("text"),
                    "backend": existing.get("backend"), "cached": True, "warnings": [],
                }

            paras = self.paragraphs(page)
            src = source_text(paras)
            if not src.strip():
                raise ApiError(400, "empty_page", f"第 {page} 页没有可提取文本",
                               "确实不需要翻译请用: pdftranslator skip <page>")
            prev_tail = self.store.prev_tail(page)
            self._mark(page, True)
            started = time.time()
            try:
                if self.backend == "agent":
                    try:
                        result = self._agent_translate(page, paras, prev_tail)
                        out = self._save(page, result["text"], "agent", "", "",
                                         src, len(paras), result.get("warnings", []))
                        out["elapsed"] = result.get("elapsed")
                        self.log_line(f"[agent] 第 {page} 页完成 ({out['elapsed']}s)")
                        return out
                    except AgentError as exc:
                        fallback = self.bridge.cfg.get("fallback_to_api", True)
                        if not (fallback and self.api.available()):
                            raise ApiError(
                                502, exc.code, exc.message, exc.hint,
                            ) from exc
                        self.log_line(f"[agent] 第 {page} 页失败({exc.code})，回退 API")
                        text = self._api_translate(paras, prev_tail)
                        out = self._save(page, text, "api", str(self.cfg.get("provider")),
                                         str(self.cfg.get("model")), src, len(paras), [])
                        out["warning"] = f"agent 失败，已回退 API: {exc.code}"
                        return out

                if self.backend == "api":
                    text = self._api_translate(paras, prev_tail)
                    out = self._save(page, text, "api", str(self.cfg.get("provider")),
                                     str(self.cfg.get("model")), src, len(paras), [])
                    self.log_line(f"[api] 第 {page} 页完成 ({time.time() - started:.1f}s)")
                    return out

                raise ApiError(
                    503, "no_backend", "没有可用的翻译后端",
                    "在 config.json 配置 agent.command，或设置 TRANSLATE_API_KEY",
                )
            finally:
                self._mark(page, False)

    # ---------- 图片 ----------
    def img_bytes(self, page: int) -> bytes:
        self.validate(page)
        cached = self.store.load_img(page, self.scale)
        if cached:
            return cached
        with self._img_lock:
            cached = self.store.load_img(page, self.scale)
            if cached:
                return cached
            data = self.book.render_png(page - 1, self.scale)
            self.store.save_img(page, self.scale, data)
            return data

    # ---------- 批翻（API / agent） ----------
    def batch_status(self) -> dict:
        if not self._batch:
            return {"running": False, "done": 0, "total": 0, "failed": 0,
                    "current": None, "mode": self.backend}
        return dict(self._batch)

    def batch_start(self, from_page: int = 1) -> dict:
        if self.backend not in ("api", "agent"):
            raise ApiError(
                409, "batch_unavailable",
                "没有可用的翻译后端",
                "在 config.json 配置 agent.command，或设置 TRANSLATE_API_KEY",
            )
        if self._batch and self._batch.get("running"):
            return {"started": False, "reason": "already_running"}
        stats = self.store.stats()
        pages = [p for p in stats["missing_pages"] if p >= from_page]
        if not pages:
            return {"started": False, "reason": "nothing_to_do", "queued": 0}
        self._stop.clear()
        self._batch = {"running": True, "done": 0, "total": len(pages),
                       "failed": 0, "current": None, "mode": self.backend}
        threading.Thread(target=self._run_batch, args=(pages,), daemon=True).start()
        return {"started": True, "queued": len(pages), "mode": self.backend}

    def batch_stop(self) -> dict:
        self._stop.set()
        if self._batch:
            self._batch["running"] = False
        return {"stopped": True}

    def _run_batch(self, pages: list[int]) -> None:
        try:
            if self.backend == "agent":
                self._run_batch_agent(pages)
            else:
                self._run_batch_api(pages)
        finally:
            if self._batch:
                self._batch["running"] = False
                self._batch["current"] = None
            self.log_line(f"[batch] 翻译结束（{self.backend}）")

    def _run_batch_agent(self, pages: list[int]) -> None:
        # 串行、每页一次短命子进程；停止后不再启动新进程，当前页会跑完
        for page in pages:
            if self._stop.is_set():
                break
            if self._batch:
                self._batch["current"] = page
            try:
                self._translate_sync(page, False)
            except Exception as exc:  # noqa: BLE001
                self.log_line(f"[batch] 第 {page} 页失败: {exc}")
                if self._batch:
                    self._batch["failed"] += 1
            finally:
                if self._batch:
                    self._batch["done"] += 1

    def _run_batch_api(self, pages: list[int]) -> None:
        concurrency = max(1, int(self.cfg.get("concurrency") or 4))
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(self._translate_sync, p, False): p for p in pages}
            for fut in as_completed(futures):
                if self._stop.is_set():
                    for f in futures:
                        f.cancel()
                    break
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    self.log_line(f"[batch] 第 {futures[fut]} 页失败: {exc}")
                    if self._batch:
                        self._batch["failed"] += 1
                finally:
                    if self._batch:
                        self._batch["done"] += 1

    # ---------- 预热（仅 API 模式） ----------
    def prewarm(self, count: int | None = None) -> int:
        if self.backend != "api":
            return 0
        n = int(self.cfg.get("prewarm_pages") or 0) if count is None else int(count)
        if n <= 0:
            return 0
        recs = self.store.scan()
        empty = set(self.store.stats()["empty_pages"])
        todo = [
            p for p in range(1, min(self.page_count, n) + 1)
            if p not in recs and p not in empty
        ]
        if not todo:
            return 0

        def worker() -> None:
            for p in todo:
                try:
                    self._translate_sync(p, False)
                except Exception as exc:  # noqa: BLE001
                    self.log_line(f"[prewarm] 第 {p} 页失败: {exc}")

        threading.Thread(target=worker, daemon=True).start()
        return len(todo)

    # ---------- 关闭 ----------
    def close(self) -> None:
        self.batch_stop()
        try:
            self.book.close()
        except Exception:
            pass
        self.api.close()
        self.bridge.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "pdftranslator/0.2"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        if "/api/status" not in (self.path or ""):
            super().log_message(fmt, *args)

    # ---------- 输出 ----------
    def _send(self, code: int, body: bytes, ctype: str, cache: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, msg: str, code: int = 400, err: str = ""):
        self._json({"error": msg, "code": err or "error"}, code)

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        app = self.app

        if path in ("/", "/index.html", "/app.js", "/style.css"):
            name = "index.html" if path == "/" else path.lstrip("/")
            ctype = {
                "index.html": "text/html; charset=utf-8",
                "app.js": "text/javascript; charset=utf-8",
                "style.css": "text/css; charset=utf-8",
            }[name]
            try:
                return self._send(200, (WEB / name).read_bytes(), ctype)
            except Exception:
                return self._error("not found", 404)

        if path == "/api/meta":
            return self._json(app.meta_payload())
        if path == "/api/status":
            return self._json(app.status_payload())
        if path == "/api/page":
            page = self._page_arg(qs)
            if page is None:
                return self._error("缺少 page 参数", 400, "missing_page")
            try:
                app.validate(page)
                return self._json(app.page_payload(page))
            except ApiError as exc:
                return self._error(exc.message, exc.status, exc.code)
            except Exception as exc:  # noqa: BLE001
                return self._error(str(exc), 500)
        if path == "/api/img":
            page = self._page_arg(qs)
            if page is None:
                return self._error("缺少 page 参数", 400, "missing_page")
            try:
                return self._send(200, app.img_bytes(page), "image/png", cache=True)
            except ApiError as exc:
                return self._error(exc.message, exc.status, exc.code)
            except Exception as exc:  # noqa: BLE001
                return self._error(str(exc), 500)
        return self._error("not found", 404)

    @staticmethod
    def _page_arg(qs: dict) -> int | None:
        raw = (qs.get("page") or [None])[0]
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        data = self._read_json()
        app = self.app

        if u.path == "/api/translate":
            pages = self._pages(data)
            if not pages:
                return self._error("缺少 page/pages", 400, "missing_page")
            try:
                for p in pages:
                    app.validate(p)
            except ApiError as exc:
                return self._error(exc.message, exc.status, exc.code)
            force = bool(data.get("force"))
            if not data.get("wait", True):
                for p in pages:
                    app.translate_page(p, force=force, wait=False)
                return self._json({"accepted": pages})
            results = {}
            for p in pages:
                try:
                    results[str(p)] = app.translate_page(p, force=force, wait=True)
                except ApiError as exc:
                    results[str(p)] = {
                        "ok": False, "page": p, "error": exc.message,
                        "code": exc.code, "hint": exc.hint,
                    }
                except Exception as exc:  # noqa: BLE001
                    results[str(p)] = {"ok": False, "page": p, "error": str(exc)}
            return self._json({"results": results})

        if u.path == "/api/batch":
            try:
                if data.get("stop"):
                    return self._json(app.batch_stop())
                return self._json(app.batch_start(int(data.get("from") or 1)))
            except ApiError as exc:
                return self._error(exc.message, exc.status, exc.code)
        return self._error("not found", 404)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > _MAX_BODY:
            return {}
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _pages(data: dict) -> list[int]:
        raw = data.get("pages") or ([data["page"]] if "page" in data else [])
        try:
            return [int(x) for x in raw][:50]
        except (TypeError, ValueError):
            return []


def run_server(store: Store, meta: dict, cfg: dict, backend: str,
               api: ApiTranslator, bridge: AgentBridge, context: str, glossary: dict,
               host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
               log=print) -> None:
    app = App(store, meta, cfg, backend, api, bridge, context, glossary, log=log)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.app = app  # type: ignore[attr-defined]
    url = f"http://{host}:{port}/"
    log(f"阅读器已启动: {url}")
    log(f"后端: {app.backend_label()}  缓存: {store.root}")
    warm = app.prewarm()
    if warm:
        log(f"预热翻译前 {warm} 页…")
    if host not in ("127.0.0.1", "localhost", "::1"):
        log("警告: 正在监听非本机地址；agent 命令只接受本地配置，请勿暴露到不可信网络。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("退出中…")
    finally:
        app.close()
        srv.shutdown()
        srv.server_close()
