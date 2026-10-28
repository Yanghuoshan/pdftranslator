# pdftranslator — 给宿主 agent 的操作手册

这是一个**由宿主 agent 驱动翻译**的 PDF 双语阅读器。Python 负责 PDF 取文、分段、渲染、缓存与状态管理；翻译由**你自己的模型/上下文**完成，逐页循环，不需要 API Key。另保留 OpenAI 兼容 / Anthropic API 作为兜底。

整个文件夹可以直接复制到任意工作区。你不必是 Claude Code，任何能读文档、执行命令、写文件的 agent 都可以驱动。

> 需要**扩展功能或网页 UI** 时，先读 [OVERVIEW.md](OVERVIEW.md)：代码分层、数据契约、扩展清单与不能破坏的不变量都在那里。

## 0. 硬性约定

- **页码一律 1-based**：CLI 参数、JSON 字段、HTTP 参数、缓存文件名都是 1-based，和 PDF 阅读器显示一致。第 1 页 = `1`。
- **stdout 只输出结果**：加 `--json` 时 stdout 恰好是一个 JSON 对象；所有日志和进度在 stderr。
- **写文件，不要拼 shell 字符串**：译文中文多、引号多，Windows shell 容易坏。用 `next` 返回的 `draft_path` 写 UTF-8 文件，再 `save <page> --from <draft_path>`。
- **不要修改 `.cache/<slug>/` 以外的内容**；不要提交 `.cache/`、真实 API Key 或 PDF 版权文件。

## 1. 安装

需要 Python >= 3.10。推荐 [uv](https://docs.astral.sh/uv/)（可自动下载 Python）：

```bash
cd pdftranslator
uv sync
uv run pdftranslator doctor --json
```

或者：

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
pdftranslator doctor --json
```

检查结果看 `data.usable`：为 `true` 表示至少有一个翻译后端可用。`agent_command` 检查的是 `config.json` 的 `agent.command`；若你打算自己逐页翻译，它**不必可用**。

## 2. 最短闭环：初始化 → 取页 → 翻译 → 保存

```bash
# 抽取全文并建立缓存（首次较慢；之后 reuse）
uv run pdftranslator init book.pdf --json

# 看进度
uv run pdftranslator status --json

# 取下一批未翻译页（默认 1 页，最多 10 页）
uv run pdftranslator next --count 3 --json
```

`next` 的 `data.pages[]` 每项形如：

```json
{
  "page": 6,
  "text": "全文段落，段落间空行……",
  "paragraphs": ["第一段……", "第二段……"],
  "paragraph_count": 5,
  "char_count": 2100,
  "empty": false,
  "source_hash": "sha256:…",
  "prev_translation_tail": "上一页译文结尾，用于保持译名/语气一致",
  "draft_path": "D:/…/.cache/<slug>/drafts/00006.txt",
  "existing_translation": null
}
```

顶层还带 `context`（书籍背景）、`glossary`（术语表）和 `instructions`（翻译规则）。**每页都要翻译** `pages[].text`，保持：

- 简体中文，忠实原文，不漏译，不加说明/标题/前缀。
- 段落数量、顺序与原文一致；段落之间用空行分隔（`text` 里就是这么排的）。
- 术语表优先；未覆盖的人名、地名、书名保留英文。
- 忽略页码、页眉页脚、装饰符号。
- 只输出译文正文。

把译文写到该页的 `draft_path`（UTF-8，无 BOM 或有 BOM 都行），然后保存：

```bash
uv run pdftranslator save 6 --from "<draft_path>" --json
```

一次保存多页时，写一个 JSON 文件再批量提交：

```json
[
  {"page": 6, "text": "第 6 页译文……"},
  {"page": 7, "text": "第 7 页译文……"}
]
```

```bash
uv run pdftranslator save --from-json pages.json --json
```

循环直到 `status` 的 `data.done` 为 `true`：

```text
while not done:
    r = pdftranslator next --json
    if r.data.done: break
    for p in r.data.pages:
        translation = translate(p.text, p.paragraphs, context, glossary, p.prev_translation_tail)
        write_utf8(p.draft_path, translation)
        pdftranslator save p.page --from p.draft_path --json
```

## 3. 命令参考

| 命令 | 作用 | 重要输出 |
| --- | --- | --- |
| `init [pdf] [--force]` | 抽取全文、记录空页、设为当前书 | `book_id, page_count, empty_pages, stats, backend` |
| `status` | 进度 | `translated, skipped, empty_pages, remaining, next_page, done, missing_pages` |
| `next [--count N] [--include-empty]` | 取未译页（默认跳过空页） | `done, pages[], context, glossary, instructions` |
| `page N` | 查看指定页原文与已有译文 | 单页对象 + `existing_translation` |
| `save N --from FILE\|-` | 保存单页译文 | `saved[], progress` |
| `save --from-json FILE\|-` | 批量保存 | 同上 |
| `skip N --reason "…"` | 明确跳过（不是翻译为空） | `progress` |
| `unskip N` | 删除该页记录，重新变回未译 | `progress` |
| `verify [--strict]` | 校验空译文/无中文/提示词回显/源已变/段落数 | `pass, issues, …`；有问题退出码 1 |
| `export --out book.md [--mode bilingual\|zh\|en]` | 导出 Markdown 对照稿 | `out, translated, missing_pages` |
| `doctor [--agent-probe]` | 检查依赖/缓存/agent/API | `ok, usable, checks[]` |
| `batch --api` | API 兜底批量翻译（宿主 agent 不要用） | `translated, failed, stats` |
| `serve [pdf]` | 打开网页阅读器 | 长驻 |

所有命令都支持 `--json`，也可放在子命令前：`pdftranslator --json status`。全局参数：`--config <path>`、`--cache-dir <dir>`、`--pdf <path>`。

## 4. JSON 信封与退出码

成功：

```json
{"ok": true, "command": "next", "data": {…}}
```

失败：

```json
{"ok": false, "command": "save",
 "error": {"code": "translation_exists", "message": "第 6 页已有译文", "hint": "加 --force 覆盖，或先检查是否重复翻译"}}
```

退出码：

| 码 | 含义 | 处理建议 |
| --- | --- | --- |
| 0 | 成功 | — |
| 1 | 校验/操作失败（`verify` 有问题、export 等） | 读 `error` / `data.issues` |
| 2 | 用法错误 | 修命令参数 |
| 3 | 未初始化 / 找不到书 | 先 `init` |
| 4 | 页码越界 | 用 1..`page_count` |
| 5 | 译文被拒（已存在/为空/校验失败） | 见 `hint`；覆盖加 `--force` |
| 6 | agent 超时/忙 | 稍后重试或调大 `agent.timeout` |
| 7 | agent 不可用 | 检查 `agent.command` 或改用 API |

常见错误码：`not_initialized`、`page_out_of_range`、`translation_exists`、`empty_translation`、`invalid_translation`（提示词回显/无中文/过长）、`book_not_found`、`api_unavailable`。

## 5. 空页、跳过与进度口径

- `empty_pages`：`init` 时源文本为空的页，视为已完成。
- `skipped`：你用 `skip` 或 `save --allow-empty` 显式跳过的页。
- `translated`：有非空译文的页。
- `remaining = page_count - translated - skipped - empty_pages`；`done = remaining == 0`。
- `next` 默认不返回空页；确实要处理（例如 OCR）才加 `--include-empty`，这类页请 `skip` 或在译文为空时用 `save --allow-empty`。
- 每页只有**一份当前译文**（记录里保留 `backend/provider/model`）。要重译已译页必须 `save --force`。

## 6. 翻译完成后

```bash
uv run pdftranslator verify --strict --json   # 退出 0 才算干净
uv run pdftranslator export --out book.zh.md --mode bilingual --json
```

`verify` 会检查：空译文、源文本较长却几乎没有中文、提示词回显、源文本已变化（`source_hash` 不符）、损坏的 JSON；`--strict` 还把段落数不一致算问题。发现问题就重译对应页（`save --force`）。

## 7. 网页阅读器（可选）

```bash
uv run pdftranslator serve --no-browser      # 默认 http://127.0.0.1:8765/
```

- **agent 模式**（配置了 `agent.command`）：切页、切中文**不会**自动翻译；只有按 `T` 才按需拉起一次 agent 子进程翻译当前页，完成即退出，不常驻、空闲不花 token。后台批翻按钮禁用。
- **API 模式**（有 Key 但没配 agent，或 `--backend api`）：保留自动翻译/预取/后台批翻。
- **只读模式**（都没有）：阅读正常，按 `T` 返回明确错误。

网页 HTTP API 同样 1-based：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/meta` | 页数、后端、`auto_translate` |
| GET | `/api/status` | 进度、`in_flight`、`batch` |
| GET | `/api/page?page=6` | 原文与已有译文 |
| GET | `/api/img?page=6` | 页面 PNG |
| POST | `/api/translate` | `{"page": 6, "wait": true, "force": false}` 或 `{"pages":[…], "wait": false}` |
| POST | `/api/batch` | `{"from": 1}` 或 `{"stop": true}`；agent 模式返回 409 |

## 8. 配置

`config.json`（首次使用可复制 `config.example.json`）。与本次集成相关的字段：

```json
{
  "backend": "auto",
  "agent": {
    "command": ["claude", "-p", "--output-format", "text"],
    "prompt_mode": "stdin",
    "timeout": 180,
    "max_concurrent": 1,
    "fallback_to_api": true,
    "cwd": "",
    "env": {}
  },
  "provider": "openai",
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "api_key": "",
  "api_key_env": "TRANSLATE_API_KEY"
}
```

- `backend`：`auto`（默认：agent 优先，其次 API，最后只读）| `agent` | `api`。
- `agent.command`：**argv 数组**，绝不拼 shell。Windows 上 `.cmd/.bat` 会自动用 `%COMSPEC%` 包一层。
- `agent.prompt_mode`：`stdin`（提示词写 stdin，读 stdout）| `arg`（替换参数里的 `{prompt}`）| `file`（写提示词文件，替换 `{prompt_file}`）。
- `agent.cwd`：留空时子进程在 `.cache/<slug>/agent-run/` 空目录里运行，避免它读到本项目的 AGENTS.md 后自行调用工具。
- `agent.fallback_to_api`：agent 失败时是否回退 API（需要 Key）。
- `books.<文件名子串>`：按书覆盖 `context` / `glossary`。

**API 兜底**：`api_key` 留空时依次读 `TRANSLATE_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`；`provider` 可选 `openai`（任何兼容 `/chat/completions` 的服务）、`anthropic`、`mock`（离线自检）。

### 其他 agent / CLI 的 adapter 写法

bridge 只要求：**stdin 给提示词，stdout 给译文**。可以用任意脚本包装：

```json
{"agent": {"command": ["python", "examples/adapters/echo_agent.py"], "prompt_mode": "stdin"}}
```

- 若某个 CLI 不读 stdin：用 `prompt_mode=file`，命令里写 `{prompt_file}`。
- 若它只接受参数：用 `prompt_mode=arg`，命令里写 `{prompt}`。
- 验证配置：`pdftranslator doctor --agent-probe --json`。这会真实调用一次，花 token；`echo_agent.py` 不花。
- 示例：Codex 可写 `["codex", "exec", "--skip-git-repo-check", "-"]`；自定义 HTTP 客户端可写自己的脚本。

## 9. 缓存布局（只读了解即可）

```text
.cache/
  active_book.json              当前书指针
  <slug>/
    book.json                   book_id / pdf / page_count / empty_pages
    en.json                     全文段落（内部 0-based 数组，页面边界才转 1-based）
    zh/00006.json               第 6 页译文记录
    img/00006@2x.png            渲染缓存
    drafts/00006.txt            next 建议的译文草稿路径
    agent-run/                  一次性 agent 的空工作目录
```

`book_id = sha256(PDF 内容)[:16]`，文件名 slug 带哈希前缀，所以复制文件夹不会串缓存，改名也不失效。

## 10. 排障

- `not_initialized` → 先 `init`；`--pdf` 可显式指定书。
- `translation_exists` → 已译过；确认后加 `--force`。
- `invalid_translation` → 输出含提示词/没有中文/长度异常；只保存纯译文。
- `page_out_of_range` → 页码从 1 开始，最大 `page_count`。
- `agent_unavailable` → `agent.command` 找不到可执行文件；`doctor --json` 看解析结果。
- `agent_timeout` → 调大 `agent.timeout`，或减小单页请求。
- 中文乱码 → 确保写文件用 UTF-8；`save --from` 接受 `utf-8-sig`。
- 端口占用 → `serve --port 8766`。
