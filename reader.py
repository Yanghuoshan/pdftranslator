# -*- coding: utf-8 -*-
"""PDF 双语阅读器：本地服务 + 懒加载翻译。

用法:
    python reader.py [book.pdf] [--port 8765] [--prewarm 3] [--provider mock]
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdfsource import Book  # noqa: E402
from store import Store, apply_book_overrides, book_settings, load_config  # noqa: E402
from translator import TranslationError, Translator  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent / "web"


class App:
    def __init__(self, pdf: Path, cfg: dict, cache_root: str):
        self.pdf = pdf
        self.cfg = cfg
        self.book = Book(pdf)
        self.store = Store(pdf, cache_root)
        self.context, self.glossary = book_settings(cfg, pdf)
        self.tr = Translator(cfg, self.book, self.store, self.context, self.glossary, log=self.log)
        self.scale = int(cfg.get("render_scale", 2))
        self.prefetch = int(cfg.get("prefetch_next_pages", 0))
        self.extract = {"done": 0, "total": self.book.page_count, "ready": False}
        self._img_lock = threading.Lock()
        self._img_busy: set[int] = set()

    def log(self, *args):
        print(f"[{time.strftime('%H:%M:%S')}]", *args, flush=True)

    # ---------- 启动 ----------
    def bootstrap(self, prewarm: int | None):
        cached = self.store.load_en()
        if cached and len(cached) == self.book.page_count:
            self.book.set_paras(cached)
            self.extract = {"done": self.book.page_count, "total": self.book.page_count, "ready": True}
            self.log(f"英文文本缓存命中（{self.book.page_count} 页）")
        else:
            threading.Thread(target=self._extract_all, daemon=True).start()

        n = self.book.page_count if prewarm is None else max(0, min(int(prewarm), self.book.page_count))
        if n:
            todo = [i for i in range(n) if not self.tr.has(i)]
            if todo:
                self.log(f"预热翻译前 {len(todo)} 页…")
                self.tr.submit(todo)

    def _extract_all(self):
        self.log(f"正在抽取全文 {self.book.page_count} 页…")
        pages = self.book.extract_all(on_progress=lambda i: self.extract.update(done=i))
        self.store.save_en(pages)
        self.extract["ready"] = True
        self.log("全文抽取完成，已落盘")

    # ---------- 业务 ----------
    def page_payload(self, n: int) -> dict:
        paras = self.book.paragraphs(n)
        rec = self.tr.get(n)
        return {
            "n": n,
            "paras": paras,
            "en": "\n\n".join(paras),
            "zh": rec,
            "has_zh": rec is not None,
            "pending": n in self.tr.in_flight(),
        }

    def img_bytes(self, n: int) -> bytes:
        cached = self.store.load_img(n, self.scale)
        if cached:
            return cached
        with self._img_lock:
            if n in self._img_busy:
                self._img_busy.discard(n)
            data = self.book.render_png(n, self.scale)
        self.store.save_img(n, self.scale, data)
        return data

    def status(self) -> dict:
        tr = self.tr
        total = self.book.page_count
        done = len(tr.translated())
        batch = tr.batch or {}
        return {
            "pages": total,
            "translated": done,
            "translated_pages": tr.translated(),
            "in_flight": tr.in_flight(),
            "batch": {
                "running": bool(batch) and not batch.get("finished"),
                "done": batch.get("done", 0),
                "total": batch.get("total", 0),
            },
            "extract": dict(self.extract),
            "stats": dict(tr.stats),
            "model": f"{tr.provider}/{tr.model}",
            "has_key": bool(tr.api_key) or tr.provider == "mock",
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "pdfreader/1.0"

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
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, msg: str, code: int = 400):
        self._json({"error": msg}, code)

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path in ("/", "/index.html"):
            return self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path in ("/app.js", "/style.css"):
            return self._file(WEB_DIR / path.lstrip("/"), "text/javascript; charset=utf-8"
                              if path.endswith(".js") else "text/css; charset=utf-8")

        if path == "/api/meta":
            app = self.app
            return self._json({
                "file": app.pdf.name,
                "title": app.pdf.stem,
                "pages": app.book.page_count,
                "page_size": app.book.page_size(0),
                "model": f"{app.tr.provider}/{app.tr.model}",
                "has_key": bool(app.tr.api_key) or app.tr.provider == "mock",
                "context": app.context,
                "prewarm": app.cfg.get("prewarm_pages"),
                "prefetch": app.prefetch,
                "cache": str(app.store.root),
            })
        if path == "/api/status":
            return self._json(self.app.status())
        if path == "/api/page":
            n = self._page_arg(qs)
            if n is None:
                return self._error("缺少页码")
            try:
                return self._json(self.app.page_payload(n))
            except Exception as exc:  # noqa: BLE001
                return self._error(str(exc), 500)
        if path == "/api/img":
            n = self._page_arg(qs)
            if n is None:
                return self._error("缺少页码")
            try:
                return self._send(200, self.app.img_bytes(n), "image/png", cache=True)
            except Exception as exc:  # noqa: BLE001
                return self._error(str(exc), 500)
        return self._error("not found", 404)

    def _file(self, path: Path, ctype: str):
        if not path.is_file():
            return self._error("not found", 404)
        self._send(200, path.read_bytes(), ctype)

    @staticmethod
    def _page_arg(qs: dict) -> int | None:
        try:
            return int(qs.get("n", ["-1"])[0])
        except ValueError:
            return None

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        body = self._read_body()
        try:
            data = json.loads(body or b"{}")
        except Exception:  # noqa: BLE001
            data = {}
        app = self.app

        if u.path == "/api/translate":
            pages = self._pages(data)
            if not pages:
                return self._error("缺少 pages")
            force = bool(data.get("force"))
            if data.get("wait"):
                res = app.tr.ensure(pages, force=force, timeout=float(app.cfg.get("timeout", 180)))
                return self._json({"results": {str(k): v for k, v in res.items()}})
            app.tr.submit(pages, force=force)
            return self._json({"accepted": pages})

        if u.path == "/api/batch":
            if data.get("stop"):
                app.tr.stop_batch()
                return self._json({"stopped": True})
            start = int(data.get("from", 0))
            pages = [n for n in range(start, app.book.page_count) if not app.tr.has(n)]
            ok = app.tr.start_batch(pages)
            return self._json({"started": ok, "queued": len(pages) if ok else 0})

        return self._error("not found", 404)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    @staticmethod
    def _pages(data: dict) -> list[int]:
        raw = data.get("pages") or ([data["page"]] if "page" in data else [])
        try:
            return [int(x) for x in raw]
        except (TypeError, ValueError):
            return []


def pick_pdf(cli: str | None) -> Path:
    if cli:
        p = Path(cli).expanduser()
        if not p.is_file():
            sys.exit(f"找不到 PDF: {p}")
        return p
    here = sorted(Path(__file__).resolve().parent.glob("*.pdf")) + sorted(Path.cwd().glob("*.pdf"))
    if not here:
        sys.exit("未指定 PDF，且当前目录没有 .pdf 文件")
    return here[0]


def main():
    ap = argparse.ArgumentParser(description="PDF 双语阅读器")
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--config")
    ap.add_argument("--cache-dir", default=".cache")
    ap.add_argument("--prewarm", type=int, default=None, help="预热页数，默认取配置 prewarm_pages")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--model"), ap.add_argument("--base-url"), ap.add_argument("--provider")
    ap.add_argument("--concurrency", type=int)
    args = ap.parse_args()

    cfg, cfg_path = load_config(args.config)
    if args.model:
        cfg["model"] = args.model
    if args.base_url:
        cfg["base_url"] = args.base_url
    if args.provider:
        cfg["provider"] = args.provider
    if args.concurrency:
        cfg["concurrency"] = args.concurrency

    pdf = pick_pdf(args.pdf)
    cfg = apply_book_overrides(cfg, pdf)

    app = App(pdf, cfg, args.cache_dir)
    app.log(f"打开《{pdf.name}》共 {app.book.page_count} 页，配置来自 {cfg_path or '默认值'}")
    app.log(f"翻译模型: {app.tr.provider}/{app.tr.model}  缓存目录: {app.store.root}")
    if not app.tr.api_key and app.tr.provider != "mock":
        app.log("⚠️ 未检测到 API Key：请在 config.json 填 api_key，或 export TRANSLATE_API_KEY=...")

    prewarm = args.prewarm if args.prewarm is not None else int(cfg.get("prewarm_pages", 3))
    app.bootstrap(prewarm)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.app = app  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/"
    app.log(f"阅读器已启动: {url}")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        app.log("退出中…")
    finally:
        app.tr.stop_batch()
        srv.shutdown()
        app.book.close()


if __name__ == "__main__":
    main()
