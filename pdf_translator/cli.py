# -*- coding: utf-8 -*-
"""pdftranslator CLI：agent 驱动的逐页翻译接口。

约定：stdout 只输出结果（--json 时为一个 JSON 对象），日志全部走 stderr。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__
from .api_translator import ApiTranslator, TranslationError
from .bridge import AgentBridge, AgentError, resolve_command
from .config import (
    agent_settings,
    apply_book_overrides,
    backend_settings,
    book_settings,
    load_config,
    resolve_key,
)
from .prompts import (
    BEGIN_MARK,
    END_MARK,
    INSTRUCTIONS,
    cjk_ratio,
    clean_translation,
    source_text,
    split_paragraphs,
    text_hash,
    validate_translation,
)
from .store import Store, _atomic_write, initialize_book

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NOT_INIT = 3
EXIT_PAGE = 4
EXIT_REJECT = 5
EXIT_BUSY = 6
EXIT_AGENT = 7


class CliError(Exception):
    def __init__(self, code: str, message: str, hint: str = "",
                 exit_code: int = EXIT_FAIL, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.details = details


def log(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


def _write_stdout(text: str) -> None:
    """JSON 固定 UTF-8 字节输出，不受 Windows 控制台/重定向代码页影响。"""
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(text.encode("utf-8"))
        buf.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def _emit(args, command: str, data: dict, human: str | None = None) -> None:
    if getattr(args, "json", False):
        _write_stdout(json.dumps(
            {"ok": True, "command": command, "data": data}, ensure_ascii=False,
        ) + "\n")
    else:
        print(human if human is not None else json.dumps(data, ensure_ascii=False, indent=2))


def _emit_error(args, command: str, err: dict) -> None:
    if getattr(args, "json", False):
        _write_stdout(json.dumps(
            {"ok": False, "command": command, "error": err}, ensure_ascii=False,
        ) + "\n")
    else:
        print(f"错误 [{err.get('code')}]: {err.get('message')}", file=sys.stderr)
        if err.get("hint"):
            print(f"提示: {err['hint']}", file=sys.stderr)


# ---------- 参数解析 ----------
def _common(suppress: bool = False) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    d = argparse.SUPPRESS if suppress else None
    p.add_argument("--json", action="store_true", default=d if suppress else False)
    p.add_argument("--cache-dir", default=d if suppress else ".cache")
    p.add_argument("--config", default=d)
    p.add_argument("--pdf", default=d)
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdftranslator",
        description="Agent-driven bilingual PDF translator (host agent translates, API fallback).",
        parents=[_common()],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_, parents=[_common(True)])

    p = add("init", "抽取全文并初始化缓存")
    p.add_argument("pdf_pos", nargs="?", metavar="pdf")
    p.add_argument("--force", action="store_true", help="强制重新抽取")

    add("status", "查看翻译进度")

    p = add("next", "取下一批未翻译页（建议翻译入口）")
    p.add_argument("--count", type=int, default=1, help="最多取几页（1-10）")
    p.add_argument("--include-empty", action="store_true", help="包含无文本页")

    p = add("page", "查看指定页原文与已有译文")
    p.add_argument("page", type=int)

    p = add("save", "保存译文（支持单页或 JSON 批量）")
    p.add_argument("page", type=int, nargs="?", help="页码（1-based）；--from-json 时可省略")
    p.add_argument("--from", dest="from_path", help="译文文件路径，或 - 表示 stdin")
    p.add_argument("--text", help="直接给译文字符串")
    p.add_argument("--from-json", dest="from_json", help="批量 JSON 文件路径，或 -")
    p.add_argument("--force", action="store_true", help="覆盖已有译文")
    p.add_argument("--allow-empty", action="store_true", help="允许空译文（记为跳过）")
    p.add_argument("--note", default="", help="备注")

    p = add("skip", "标记某页跳过")
    p.add_argument("page", type=int)
    p.add_argument("--reason", default="")

    p = add("unskip", "取消跳过/删除该页记录")
    p.add_argument("page", type=int)

    p = add("verify", "校验译文质量与完整性")
    p.add_argument("--strict", action="store_true", help="把段落数不一致也算作问题")

    p = add("export", "导出 Markdown 对照稿")
    p.add_argument("--out", required=True, help="输出 .md 路径")
    p.add_argument("--mode", choices=["bilingual", "zh", "en"], default="bilingual")
    p.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")

    p = add("doctor", "检查安装、缓存、agent/API 配置")
    p.add_argument("--agent-probe", action="store_true", help="真实调用一次 agent 做探针（会花 token）")

    p = add("batch", "API 兜底批量翻译")
    p.add_argument("--api", action="store_true", help="使用 API 批量翻译（目前唯一模式）")
    p.add_argument("--from", dest="from_page", type=int, default=1)
    p.add_argument("--to", type=int, default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=None)

    p = add("serve", "启动网页阅读器（T 按需拉起一次性 agent）")
    p.add_argument("pdf_pos", nargs="?", metavar="pdf")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--backend", choices=["auto", "agent", "api"], default=None)
    return parser


# ---------- 通用 ----------
def _load_cfg(args):
    try:
        return load_config(args.config)
    except ValueError as exc:
        raise CliError("bad_config", str(exc), exit_code=EXIT_USAGE)


def _roots(args) -> Path:
    return Path(args.cache_dir).expanduser().resolve()


def _resolve(args, require_en: bool = True):
    cfg, _ = _load_cfg(args)
    root = _roots(args)
    if args.pdf:
        pdf = Path(args.pdf).expanduser().resolve()
        if not pdf.is_file():
            raise CliError("book_not_found", f"找不到 PDF: {pdf}", exit_code=EXIT_NOT_INIT)
        store = Store.for_pdf(pdf, root)
    else:
        store = Store.load_active(root)
        if store is None:
            raise CliError(
                "not_initialized", "还没有初始化任何书",
                "先运行: pdftranslator init <book.pdf> --json", EXIT_NOT_INIT,
            )
    meta = store.load_book()
    if not meta:
        raise CliError(
            "not_initialized", f"缓存缺少 book.json: {store.root}",
            "先运行: pdftranslator init <book.pdf>", EXIT_NOT_INIT,
        )
    if require_en and store.load_en() is None:
        raise CliError(
            "not_initialized", f"缓存缺少 en.json: {store.root}",
            "重新运行 init --force", EXIT_NOT_INIT,
        )
    name = meta.get("pdf_name") or meta.get("pdf") or ""
    cfg = apply_book_overrides(cfg, name)
    context, glossary = book_settings(cfg, name)
    return store, meta, cfg, context, glossary


def _backend_info(cfg: dict, store: Store) -> tuple[dict, dict, AgentBridge, bool]:
    try:
        agent_cfg = agent_settings(cfg)
    except ValueError as exc:
        raise CliError("bad_config", str(exc), exit_code=EXIT_USAGE)
    key = resolve_key(cfg)
    api_ok = (cfg.get("provider") or "openai").lower() == "mock" or bool(key)
    bridge = AgentBridge(agent_cfg, store, log=log)
    agent_ok = bool(agent_cfg["command"]) and bridge.available()
    info = backend_settings(cfg, agent_ok, api_ok)
    info.update({
        "agent_command": bridge.label(),
        "agent_available": agent_ok,
        "api_model": f"{cfg.get('provider')}/{cfg.get('model')}",
        "api_key_present": bool(key),
    })
    return info, agent_cfg, bridge, api_ok


def _check_page(page: int | None, meta: dict) -> int:
    if page is None:
        raise CliError("usage", "缺少页码", "例如: pdftranslator save 12 --from page.txt", EXIT_USAGE)
    n = int(meta.get("page_count") or 0)
    if page < 1 or page > n:
        raise CliError(
            "page_out_of_range", f"页码 {page} 超出范围 1..{n}",
            "页码从 1 开始（PDF 阅读器显示的页号）", EXIT_PAGE,
        )
    return page


def _page_payload(store: Store, meta: dict, en: list[list[str]], page: int) -> dict:
    paras = en[page - 1] if 0 <= page - 1 < len(en) else []
    text = source_text(paras)
    rec = store.load_zh(page)
    existing = None
    if rec and not rec.get("skipped") and str(rec.get("text") or "").strip():
        existing = {
            "text": rec.get("text"),
            "backend": rec.get("backend"),
            "ts": rec.get("ts"),
        }
    return {
        "page": page,
        "text": text,
        "paragraphs": paras,
        "paragraph_count": len(paras),
        "char_count": len(text),
        "empty": not text.strip(),
        "source_hash": text_hash(text),
        "prev_translation_tail": store.prev_tail(page),
        "draft_path": str(store.draft_path(page)),
        "existing_translation": existing,
    }


def _progress(store: Store) -> dict:
    s = store.stats()
    return {k: s[k] for k in ("page_count", "translated", "skipped", "remaining", "next_page", "done", "percent")}


def _read_text(source: str) -> str:
    if source == "-":
        return sys.stdin.buffer.read().decode("utf-8-sig")
    p = Path(source).expanduser()
    if not p.is_file():
        raise CliError("file_not_found", f"找不到文件: {p}", exit_code=EXIT_USAGE)
    return p.read_bytes().decode("utf-8-sig")


def _read_json(source: str):
    raw = _read_text(source)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError("bad_json", f"不是合法 JSON: {exc}", exit_code=EXIT_USAGE)


def _entries_from_json(data) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("pages") or data.get("items") or []
    if not isinstance(data, list) or not data:
        raise CliError("bad_json", "批量 JSON 需要非空数组，或 {\"pages\": [...]}", exit_code=EXIT_USAGE)
    entries = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or "page" not in item or "text" not in item:
            raise CliError("bad_json", f"第 {i + 1} 项需要 page 和 text", exit_code=EXIT_USAGE)
        try:
            page = int(item["page"])
        except (TypeError, ValueError):
            raise CliError("bad_json", f"第 {i + 1} 项 page 不是整数", exit_code=EXIT_USAGE)
        entries.append({"page": page, "text": str(item["text"]), "note": str(item.get("note") or "")})
    return entries


# ---------- 命令 ----------
def cmd_init(args) -> int:
    pdf_arg = args.pdf or args.pdf_pos
    if not pdf_arg:
        cands = sorted(Path.cwd().glob("*.pdf"))
        if not cands:
            raise CliError(
                "book_not_found", "未指定 PDF，且当前目录没有 .pdf 文件",
                "pdftranslator init <book.pdf> --json", EXIT_NOT_INIT,
            )
        if len(cands) > 1:
            log(f"当前目录有多个 PDF，使用第一个: {cands[0].name}")
        pdf_arg = str(cands[0])

    cfg, cfg_path = _load_cfg(args)
    try:
        store, meta, reused = initialize_book(pdf_arg, _roots(args), force=args.force, log=log)
    except FileNotFoundError as exc:
        raise CliError("book_not_found", str(exc), exit_code=EXIT_NOT_INIT)
    except Exception as exc:  # noqa: BLE001
        raise CliError("init_failed", f"初始化失败: {exc}", exit_code=EXIT_FAIL)

    name = meta.get("pdf_name") or meta.get("pdf") or ""
    cfg = apply_book_overrides(cfg, name)
    context, glossary = book_settings(cfg, name)
    info, _, _, _ = _backend_info(cfg, store)
    stats = store.stats()
    data = {
        "book_id": meta.get("book_id"),
        "title": meta.get("title"),
        "pdf": meta.get("pdf"),
        "page_count": meta.get("page_count"),
        "source_hash": meta.get("source_hash"),
        "cache_dir": str(store.root),
        "config": str(cfg_path) if cfg_path else None,
        "reused": reused,
        "empty_pages": meta.get("empty_pages") or [],
        "context": context,
        "glossary": glossary,
        "stats": stats,
        "backend": info,
    }
    human = (
        f"已初始化《{meta.get('title')}》{meta.get('page_count')} 页"
        f"（空页 {len(meta.get('empty_pages') or [])}）\n"
        f"缓存: {store.root}\n下一页: {stats['next_page']}  后端: {info['chosen']}"
    )
    _emit(args, "init", data, human)
    return EXIT_OK


def cmd_status(args) -> int:
    store, meta, cfg, _, _ = _resolve(args)
    info, _, _, _ = _backend_info(cfg, store)
    stats = store.stats()
    data = {
        "book_id": meta.get("book_id"),
        "title": meta.get("title"),
        "pdf": meta.get("pdf"),
        "cache_dir": str(store.root),
        "page_count": stats["page_count"],
        "translated": stats["translated"],
        "skipped": stats["skipped"],
        "empty_pages": stats["empty_pages"],
        "translated_pages": stats["translated_pages"],
        "skipped_pages": stats["skipped_pages"],
        "missing_pages": stats["missing_pages"],
        "remaining": stats["remaining"],
        "next_page": stats["next_page"],
        "done": stats["done"],
        "percent": stats["percent"],
        "backend": info,
    }
    human = (
        f"《{data['title']}》 已译 {data['translated']}/{data['page_count']}，"
        f"跳过 {data['skipped']}，空页 {len(data['empty_pages'])}，"
        f"剩余 {data['remaining']}，下一 {data['next_page']}  后端 {info['chosen']}"
    )
    _emit(args, "status", data, human)
    return EXIT_OK


def cmd_next(args) -> int:
    store, meta, cfg, context, glossary = _resolve(args)
    en = store.load_en() or []
    stats = store.stats()
    candidates = list(stats["missing_pages"])
    if args.include_empty:
        recs = store.scan()
        extra = [p for p in stats["empty_pages"] if p not in recs]
        candidates = sorted(set(candidates) | set(extra))
    count = max(1, min(int(args.count or 1), 10))
    pages = candidates[:count]
    if pages:
        store.drafts_dir.mkdir(parents=True, exist_ok=True)
    works = [_page_payload(store, meta, en, p) for p in pages]
    data = {
        "done": not works,
        "next_page": pages[0] if pages else None,
        "remaining": len(candidates),
        "truncated": len(pages) < len(candidates),
        "page_count": stats["page_count"],
        "context": context,
        "glossary": glossary,
        "instructions": INSTRUCTIONS,
        "pages": works,
    }
    if not works:
        human = "全书已完成。"
    else:
        first = works[0]
        human = f"下一页 {first['page']}（剩余 {data['remaining']}）\n\n{first['text']}"
    _emit(args, "next", data, human)
    return EXIT_OK


def cmd_page(args) -> int:
    store, meta, cfg, context, glossary = _resolve(args)
    en = store.load_en() or []
    page = _check_page(args.page, meta)
    stats = store.stats()
    data = {
        "book_id": meta.get("book_id"),
        "context": context,
        "glossary": glossary,
        "next_page": stats["next_page"],
        "page": _page_payload(store, meta, en, page),
    }
    _emit(args, "page", data, f"第 {page} 页\n\n{data['page']['text']}")
    return EXIT_OK


def _save_one(store: Store, meta: dict, en: list[list[str]], page: int, text: str,
              force: bool, allow_empty: bool, note: str) -> dict:
    _check_page(page, meta)
    paras = en[page - 1] if 0 <= page - 1 < len(en) else []
    src = source_text(paras)
    cleaned = clean_translation(text)
    existing = store.load_zh(page)

    if not cleaned:
        if not allow_empty:
            raise CliError(
                "empty_translation", f"第 {page} 页译文为空",
                "确认原文后再保存；确实要跳过请用: pdftranslator skip <page> --reason ...",
                EXIT_REJECT,
            )
        store.save_zh(page, {
            "text": "",
            "skipped": True,
            "backend": "agent",
            "provider": "",
            "model": "",
            "source_hash": text_hash(src),
            "paragraph_count": 0,
            "source_paragraph_count": len(paras),
            "note": note or "empty translation",
        })
        return {
            "page": page, "saved": True, "skipped": True, "unchanged": False,
            "paragraph_count": 0, "source_paragraph_count": len(paras),
            "warnings": ["译文为空，已记为跳过"],
        }

    if existing and not existing.get("skipped") and str(existing.get("text") or "").strip():
        if existing.get("text") == cleaned:
            return {
                "page": page, "saved": True, "unchanged": True, "skipped": False,
                "paragraph_count": len(split_paragraphs(cleaned)),
                "source_paragraph_count": len(paras), "warnings": [],
            }
        if not force:
            raise CliError(
                "translation_exists", f"第 {page} 页已有译文",
                "加 --force 覆盖，或先检查是否重复翻译", EXIT_REJECT,
            )

    checked = validate_translation(paras, cleaned)
    if checked["error"]:
        raise CliError(
            "invalid_translation", f"第 {page} 页译文未通过校验: {checked['error']}",
            "确认输出的是纯译文；不要包含提示词、说明或代码块", EXIT_REJECT,
        )

    store.save_zh(page, {
        "text": cleaned,
        "skipped": False,
        "backend": "agent",
        "provider": "",
        "model": "",
        "source_hash": text_hash(src),
        "paragraph_count": checked["paragraph_count"],
        "source_paragraph_count": len(paras),
        "note": note,
    })
    return {
        "page": page, "saved": True, "unchanged": False, "skipped": False,
        "paragraph_count": checked["paragraph_count"],
        "source_paragraph_count": len(paras),
        "warnings": checked["warnings"],
    }


def cmd_save(args) -> int:
    inputs = [x for x in (args.from_path, args.text, args.from_json) if x is not None]
    if len(inputs) != 1:
        raise CliError(
            "usage", "需要且只能提供 --from / --text / --from-json 之一",
            "推荐: pdftranslator save 12 --from page12.txt --json", EXIT_USAGE,
        )

    store, meta, cfg, context, glossary = _resolve(args)
    en = store.load_en() or []

    if args.from_json is not None:
        entries = _entries_from_json(_read_json(args.from_json))
        results = []
        for i, item in enumerate(entries):
            try:
                results.append(_save_one(
                    store, meta, en, item["page"], item["text"],
                    args.force, args.allow_empty, item.get("note") or args.note,
                ))
            except CliError as exc:
                exc.details = {"saved": results, "failed_index": i, "failed_page": item["page"]}
                raise
        data = {"saved": results, "progress": _progress(store)}
        _emit(args, "save", data, f"已保存 {len(results)} 页")
        return EXIT_OK

    page = _check_page(args.page, meta)
    text = args.text if args.text is not None else _read_text(args.from_path)
    result = _save_one(store, meta, en, page, text, args.force, args.allow_empty, args.note)
    data = {"saved": [result], "progress": _progress(store)}
    msg = f"第 {page} 页" + ("（无变化）" if result.get("unchanged") else " 已保存")
    if result.get("warnings"):
        msg += "\n警告: " + "；".join(result["warnings"])
    _emit(args, "save", data, msg)
    return EXIT_OK


def cmd_skip(args) -> int:
    store, meta, _, _, _ = _resolve(args)
    en = store.load_en() or []
    page = _check_page(args.page, meta)
    paras = en[page - 1] if 0 <= page - 1 < len(en) else []
    store.save_zh(page, {
        "text": "",
        "skipped": True,
        "backend": "manual",
        "provider": "",
        "model": "",
        "source_hash": text_hash(source_text(paras)),
        "paragraph_count": 0,
        "source_paragraph_count": len(paras),
        "note": args.reason,
    })
    data = {"page": page, "skipped": True, "reason": args.reason, "progress": _progress(store)}
    _emit(args, "skip", data, f"第 {page} 页已跳过")
    return EXIT_OK


def cmd_unskip(args) -> int:
    store, meta, _, _, _ = _resolve(args)
    page = _check_page(args.page, meta)
    store.delete_zh(page)
    data = {"page": page, "skipped": False, "progress": _progress(store)}
    _emit(args, "unskip", data, f"第 {page} 页记录已删除")
    return EXIT_OK


def cmd_verify(args) -> int:
    store, meta, _, _, _ = _resolve(args)
    en = store.load_en() or []
    stats = store.stats()
    n = int(meta.get("page_count") or 0)
    recs = store.scan()
    invalid = store.invalid_files()

    empty_translations, no_chinese, prompt_echo, stale = [], [], [], []
    mismatches = []
    for page, rec in sorted(recs.items()):
        if page < 1 or page > n or rec.get("skipped"):
            continue
        text = str(rec.get("text") or "")
        src_paras = en[page - 1] if 0 <= page - 1 < len(en) else []
        src = source_text(src_paras)
        if not text.strip():
            empty_translations.append(page)
            continue
        if len(src) >= 100 and cjk_ratio(text) < 0.05:
            no_chinese.append(page)
        if any(m in text for m in (BEGIN_MARK, END_MARK, "[待翻译正文]", "翻译要求：")):
            prompt_echo.append(page)
        if rec.get("source_hash") and rec["source_hash"] != text_hash(src):
            stale.append({"page": page, "expected": text_hash(src), "actual": rec["source_hash"]})
        tp = len(split_paragraphs(text))
        if tp != len(src_paras):
            mismatches.append({
                "page": page,
                "source_paragraphs": len(src_paras),
                "translation_paragraphs": tp,
            })

    issues = (
        len(empty_translations) + len(no_chinese) + len(prompt_echo)
        + len(stale) + len(invalid) + (len(mismatches) if args.strict else 0)
    )
    data = {
        "book_id": meta.get("book_id"),
        "pass": issues == 0,
        "issues": issues,
        "complete": stats["remaining"] == 0,
        "page_count": n,
        "translated": stats["translated"],
        "skipped_pages": stats["skipped_pages"],
        "empty_pages": stats["empty_pages"],
        "missing_pages": stats["missing_pages"],
        "empty_translations": empty_translations,
        "no_chinese": no_chinese,
        "prompt_echo": prompt_echo,
        "stale_pages": stale,
        "paragraph_mismatches": mismatches,
        "invalid_files": invalid,
        "strict": bool(args.strict),
    }
    human = (
        "校验通过" if data["pass"] else f"发现 {issues} 个问题"
    ) + f"；进度 {stats['translated']}/{n}，剩余 {stats['remaining']}"
    _emit(args, "verify", data, human)
    return EXIT_OK if data["pass"] else EXIT_FAIL


def cmd_export(args) -> int:
    store, meta, _, _, _ = _resolve(args)
    en = store.load_en() or []
    stats = store.stats()
    n = int(meta.get("page_count") or 0)
    recs = store.scan()
    out = Path(args.out).expanduser().resolve()
    if out.exists() and not args.force:
        raise CliError(
            "output_exists", f"输出文件已存在: {out}",
            "加 --force 覆盖", EXIT_REJECT,
        )

    missing = []
    lines = [f"# {meta.get('title') or 'PDF'}", ""]
    for page in range(1, n + 1):
        paras = en[page - 1] if 0 <= page - 1 < len(en) else []
        src = source_text(paras)
        rec = recs.get(page)
        skipped = bool(rec and rec.get("skipped"))
        zh = str(rec.get("text") or "") if rec and not skipped else ""
        if not zh and not skipped and page not in stats["empty_pages"]:
            missing.append(page)
        lines.append(f"## Page {page}")
        lines.append("")
        if args.mode in ("bilingual", "en"):
            lines.append(src if src else "_（无可提取文本）_")
            lines.append("")
        if args.mode in ("bilingual", "zh"):
            lines.append("**中文**")
            lines.append("")
            if zh:
                lines.append(zh)
            elif skipped:
                lines.append("_（本页已跳过）_")
            elif page in stats["empty_pages"]:
                lines.append("_（本页无可提取文本）_")
            else:
                lines.append("_（本页尚未翻译）_")
            lines.append("")
        lines.append("---")
        lines.append("")
    body = "\n".join(lines).rstrip() + "\n"
    try:
        _atomic_write(out, body.encode("utf-8"))
    except OSError as exc:
        raise CliError("io_error", f"写入失败: {exc}", exit_code=EXIT_FAIL)
    data = {
        "out": str(out),
        "mode": args.mode,
        "pages": n,
        "translated": stats["translated"],
        "missing_pages": missing,
        "complete": not missing,
        "bytes": len(body.encode("utf-8")),
    }
    _emit(args, "export", data, f"已导出 {out}（{n} 页，缺译 {len(missing)} 页）")
    return EXIT_OK


def cmd_doctor(args) -> int:
    import importlib
    import tempfile
    from importlib.resources import files

    checks = []

    def add(name: str, ok: bool, detail: str = "", **extra):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, **extra})

    add("python", sys.version_info >= (3, 10), sys.version.split()[0])
    missing_deps = []
    for mod in ("pdfplumber", "pypdfium2", "PIL", "httpx"):
        try:
            importlib.import_module(mod)
        except Exception:
            missing_deps.append(mod)
    add("dependencies", not missing_deps, "ok" if not missing_deps else "缺少: " + ", ".join(missing_deps))

    root = _roots(args)
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, prefix=".doctor-", delete=True) as fh:
            fh.write(b"ok")
        add("cache_writable", True, str(root))
    except Exception as exc:  # noqa: BLE001
        add("cache_writable", False, str(exc))

    try:
        assets = files("pdf_translator") / "web"
        ok_assets = all((assets / n).is_file() for n in ("index.html", "app.js", "style.css"))
        add("web_assets", ok_assets, str(assets))
    except Exception as exc:  # noqa: BLE001
        add("web_assets", False, str(exc))

    cfg, cfg_path = _load_cfg(args)
    store = None
    meta = None
    try:
        store, meta, cfg, context, glossary = _resolve(args)
        add("book", True, f"{meta.get('title')} · {meta.get('page_count')} 页 · {store.root}")
    except CliError as exc:
        add("book", False, exc.message)

    try:
        agent_cfg = agent_settings(cfg)
        bridge = AgentBridge(agent_cfg, store or Store(root, "_doctor"), log=log)
        resolved = ""
        try:
            resolved = " ".join(resolve_command(agent_cfg["command"]))
        except AgentError as exc:
            resolved = exc.message
        add(
            "agent_command",
            bool(agent_cfg["command"]) and bridge.available(),
            bridge.label() or "未配置",
            resolved=resolved,
            prompt_mode=agent_cfg["prompt_mode"],
        )
        if args.agent_probe:
            if not agent_cfg["command"]:
                add("agent_probe", False, "未配置 agent.command")
            elif store is None:
                add("agent_probe", False, "需要先初始化书")
            else:
                try:
                    result = bridge.probe(timeout=min(60, agent_cfg["timeout"]))
                    add("agent_probe", True, result["text"][:120], elapsed=result["elapsed"])
                except AgentError as exc:
                    add("agent_probe", False, f"{exc.code}: {exc.message}")
    except ValueError as exc:
        add("agent_command", False, str(exc))
        if args.agent_probe:
            add("agent_probe", False, str(exc))

    key = resolve_key(cfg)
    api_ok = (cfg.get("provider") or "openai").lower() == "mock" or bool(key)
    add("api", api_ok, f"{cfg.get('provider')}/{cfg.get('model')}" if api_ok else "未配置 API Key")
    add("config", True, str(cfg_path) if cfg_path else "使用默认值")

    essential = {"python", "dependencies", "cache_writable", "web_assets"}
    ok = all(c["ok"] for c in checks if c["name"] in essential)
    usable = any(
        c["ok"] for c in checks if c["name"] in ("agent_command", "api")
    )
    data = {"ok": ok, "usable": usable, "checks": checks}
    human = "\n".join(
        f"[{'OK' if c['ok'] else '!!'}] {c['name']}: {c['detail']}" for c in checks
    )
    _emit(args, "doctor", data, human)
    return EXIT_OK if ok else EXIT_FAIL


def cmd_batch(args) -> int:
    if not args.api:
        raise CliError(
            "usage", "batch 目前只支持 --api 兜底模式",
            "整本高质量翻译请让宿主 agent 使用 next/save 循环", EXIT_USAGE,
        )
    store, meta, cfg, context, glossary = _resolve(args)
    api = ApiTranslator(cfg, context, glossary, log=log)
    if not api.available():
        raise CliError(
            "api_unavailable", "未配置 API Key，无法使用 --api 批量翻译",
            "设置 TRANSLATE_API_KEY，或改用 agent 的 next/save 循环", EXIT_AGENT,
        )
    en = store.load_en() or []
    stats = store.stats()
    pages = [
        p for p in stats["missing_pages"]
        if p >= args.from_page and (args.to is None or p <= args.to)
    ]
    if args.limit:
        pages = pages[:args.limit]
    concurrency = max(1, int(args.concurrency or cfg.get("concurrency") or 4))

    def work(page: int) -> int:
        paras = en[page - 1] if 0 <= page - 1 < len(en) else []
        text = api.translate_paras(paras, store.prev_tail(page))
        checked = validate_translation(paras, text)
        if checked["error"]:
            raise TranslationError(checked["error"])
        store.save_zh(page, {
            "text": text,
            "skipped": False,
            "backend": "api",
            "provider": cfg.get("provider"),
            "model": cfg.get("model"),
            "source_hash": text_hash(source_text(paras)),
            "paragraph_count": checked["paragraph_count"],
            "source_paragraph_count": len(paras),
            "note": "",
        })
        return page

    done, failed = [], []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(work, p): p for p in pages}
        for fut in as_completed(futs):
            page = futs[fut]
            try:
                done.append(fut.result())
                log(f"  第 {page} 页完成")
            except Exception as exc:  # noqa: BLE001
                api.stats["errors"] += 1
                failed.append({"page": page, "error": str(exc)[:300]})
                log(f"  第 {page} 页失败: {exc}")

    data = {
        "mode": "api",
        "queued": len(pages),
        "translated": sorted(done),
        "failed": failed,
        "stats": api.stats,
        "progress": _progress(store),
    }
    _emit(args, "batch", data, f"API 批翻完成: 成功 {len(done)}，失败 {len(failed)}")
    return EXIT_OK if not failed else EXIT_FAIL


def cmd_serve(args) -> int:
    cfg, _ = _load_cfg(args)
    root = _roots(args)
    pdf_arg = args.pdf or args.pdf_pos
    if pdf_arg:
        pdf = Path(pdf_arg).expanduser().resolve()
        if not pdf.is_file():
            raise CliError("book_not_found", f"找不到 PDF: {pdf}", exit_code=EXIT_NOT_INIT)
        store = Store.for_pdf(pdf, root)
        meta = store.load_book()
        if not meta or store.load_en() is None:
            store, meta, _ = initialize_book(pdf, root, force=False, log=log)
    else:
        store = Store.load_active(root)
        if store is None:
            raise CliError(
                "not_initialized", "还没有初始化任何书",
                "先运行: pdftranslator init <book.pdf>", EXIT_NOT_INIT,
            )
        meta = store.load_book()
        if not meta:
            raise CliError("not_initialized", "缓存缺少 book.json", "重新运行 init", EXIT_NOT_INIT)
        if store.load_en() is None:
            pdf = Path(meta.get("pdf") or "")
            if pdf.is_file():
                store, meta, _ = initialize_book(pdf, root, force=False, log=log)
            else:
                raise CliError("not_initialized", "缓存缺少 en.json 且原 PDF 不存在", "重新 init", EXIT_NOT_INIT)

    if args.backend:
        cfg["backend"] = args.backend
    name = meta.get("pdf_name") or meta.get("pdf") or ""
    cfg = apply_book_overrides(cfg, name)
    context, glossary = book_settings(cfg, name)

    info, _, bridge, _ = _backend_info(cfg, store)
    api = ApiTranslator(cfg, context, glossary, log=log)
    if info["chosen"] == "none":
        log("警告: 没有可用的 agent 或 API，网页将以只读模式运行。")
    elif info["chosen"] == "api" and info["preferred"] == "agent":
        log("警告: 配置要求 agent，但命令不可用；已回退到 API。")

    from .server import run_server

    run_server(
        store=store,
        meta=meta,
        cfg=cfg,
        backend=info["chosen"],
        api=api,
        bridge=bridge,
        context=context,
        glossary=glossary,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return EXIT_OK


_COMMANDS = {
    "init": cmd_init,
    "status": cmd_status,
    "next": cmd_next,
    "page": cmd_page,
    "save": cmd_save,
    "skip": cmd_skip,
    "unskip": cmd_unskip,
    "verify": cmd_verify,
    "export": cmd_export,
    "doctor": cmd_doctor,
    "batch": cmd_batch,
    "serve": cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "command", "")
    try:
        return _COMMANDS[command](args) or EXIT_OK
    except CliError as exc:
        _emit_error(args, command, {
            "code": exc.code,
            "message": exc.message,
            "hint": exc.hint,
            "details": exc.details,
        })
        return exc.exit_code
    except AgentError as exc:
        _emit_error(args, command, exc.as_dict())
        return EXIT_BUSY if exc.code == "agent_timeout" else EXIT_AGENT
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        _emit_error(args, command, {
            "code": "internal_error",
            "message": str(exc),
            "hint": "用 doctor 检查环境；必要时加 --json 查看错误对象",
        })
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
