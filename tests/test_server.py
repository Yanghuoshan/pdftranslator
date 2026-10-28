# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from pdf_translator.api_translator import ApiTranslator
from pdf_translator.bridge import AgentBridge
from pdf_translator.config import agent_settings, load_config
from pdf_translator.server import App, Handler
from pdf_translator.store import initialize_book


def noop(*_args):
    pass


@pytest.fixture
def book(project):
    store, meta, _ = initialize_book(project.pdf, project.cache, log=noop)
    cfg, _ = load_config(str(project.cfg))
    return store, meta, cfg


def make_app(book, backend="api", agent_mode=None, fallback=True):
    store, meta, cfg = book
    if agent_mode:
        cfg["agent"]["command"] = [sys.executable, str(agent_mode[0]), agent_mode[1]]
        cfg["agent"]["fallback_to_api"] = fallback
    api = ApiTranslator(cfg, "", {}, log=noop)
    bridge = AgentBridge(agent_settings(cfg), store, log=noop)
    app = App(store, meta, cfg, backend, api, bridge, "", {}, log=noop)
    return app, store


@contextmanager
def serve(app):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.app = app
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        app.close()


def request(base, path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers.get_content_type()
    except HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get_content_type()


def get_json(base, path):
    status, raw, _ = request(base, path)
    return status, json.loads(raw.decode("utf-8"))


def post_json(base, path, body):
    status, raw, _ = request(base, path, "POST", body)
    return status, json.loads(raw.decode("utf-8"))


def test_static_assets_use_1_based_api(book):
    app, _ = make_app(book)
    with serve(app) as base:
        status, raw, ctype = request(base, "/")
        assert status == 200 and ctype == "text/html"
        html = raw.decode("utf-8")
        assert "PDF 双语阅读器" in html
        assert 'id="zhHint"' in html

        status, raw, ctype = request(base, "/app.js")
        assert status == 200 and "javascript" in ctype
        js = raw.decode("utf-8")
        assert "/api/page?page=" in js
        assert "/api/img?page=" in js
        assert "auto_translate" in js
        assert "?n=" not in js

        status, raw, ctype = request(base, "/style.css")
        assert status == 200 and ctype == "text/css"


def test_meta_page_status_and_1_based_bounds(book):
    app, store = make_app(book)
    with serve(app) as base:
        status, meta = get_json(base, "/api/meta")
        assert status == 200
        assert meta["pages"] == 4
        assert meta["backend"] == "api"
        assert meta["auto_translate"] is True

        status, page = get_json(base, "/api/page?page=1")
        assert status == 200
        assert page["page"] == 1
        assert page["has_zh"] is False
        assert "formation of Christendom" in page["en"]

        status, body = get_json(base, "/api/page?page=0")
        assert status == 400 and body["code"] == "page_out_of_range"
        status, body = get_json(base, "/api/page?page=5")
        assert status == 400 and body["code"] == "page_out_of_range"
        status, body = get_json(base, "/api/page")
        assert status == 400 and body["code"] == "missing_page"

        status, st = get_json(base, "/api/status")
        assert status == 200
        assert st["translated"] == 0
        assert st["empty_pages"] == [3]
        assert st["remaining"] == 3
        assert st["auto_translate"] is True

        status, png, ctype = request(base, "/api/img?page=1")
        assert status == 200
        assert ctype == "image/png"
        assert png.startswith(b"\x89PNG")


def test_api_mode_prewarm(book):
    import time

    app, store = make_app(book)
    try:
        assert app.prewarm(count=2) == 2
        for _ in range(100):
            if store.load_zh(1) and store.load_zh(2):
                break
            time.sleep(0.05)
        assert store.load_zh(1) and store.load_zh(2)
    finally:
        app.close()


def test_api_translate_and_empty_page(book):
    app, store = make_app(book)
    with serve(app) as base:
        status, data = post_json(base, "/api/translate", {"page": 1, "wait": True})
        assert status == 200
        result = data["results"]["1"]
        assert result["ok"] is True
        assert result["backend"] == "api"
        assert "离线自检译文" in result["text"]
        rec = store.load_zh(1)
        assert rec and rec["backend"] == "api" and rec["text"]

        status, data = post_json(base, "/api/translate", {"page": 3, "wait": True})
        assert status == 200
        assert data["results"]["3"]["ok"] is False
        assert data["results"]["3"]["code"] == "empty_page"

        status, body = post_json(base, "/api/translate", {"page": 5, "wait": True})
        assert status == 400 and body["code"] == "page_out_of_range"


def test_agent_mode_no_auto_translate_and_batch_409(book, fake_agent):
    app, store = make_app(book, backend="agent", agent_mode=(fake_agent, "ok"))
    with serve(app) as base:
        status, meta = get_json(base, "/api/meta")
        assert meta["auto_translate"] is False
        assert meta["prefetch"] == 0
        assert meta["backend"] == "agent"

        status, st = get_json(base, "/api/status")
        assert status == 200
        assert st["auto_translate"] is False

        status, body = post_json(base, "/api/batch", {"from": 1})
        assert status == 409
        assert body["code"] == "batch_requires_api"

        # 只有显式 POST /api/translate（网页按 T）才拉起一次性 agent
        status, data = post_json(base, "/api/translate", {"page": 2, "wait": True})
        assert status == 200
        result = data["results"]["2"]
        assert result["ok"] is True
        assert result["backend"] == "agent"
        rec = store.load_zh(2)
        assert rec and rec["backend"] == "agent"
        assert "第 1 段测试译文" in rec["text"]

        status, page = get_json(base, "/api/page?page=2")
        assert page["has_zh"] is True
        assert "第 1 段测试译文" in page["zh"]


def test_agent_failure_falls_back_to_api(book, fake_agent):
    app, store = make_app(book, backend="agent", agent_mode=(fake_agent, "fail"))
    with serve(app) as base:
        status, data = post_json(base, "/api/translate", {"page": 1, "wait": True})
        assert status == 200
        result = data["results"]["1"]
        assert result["ok"] is True
        assert result["backend"] == "api"
        assert "回退" in result["warning"]
        rec = store.load_zh(1)
        assert rec and rec["backend"] == "api"
