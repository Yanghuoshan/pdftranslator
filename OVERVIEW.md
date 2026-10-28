# pdftranslator 项目概览（扩展指南）

这份文档面向**部署后要改代码/改界面的宿主 agent 或维护者**。操作翻译流程请先读 [AGENTS.md](AGENTS.md)；这里回答的是：代码怎么分层、数据从哪来到哪去、想加功能/改 UI 应该动哪些文件、哪些不变量不能破坏。

## 1. 设计目标

- **agent 宿主优先**：翻译由宿主 agent 完成，Python 只做 PDF 取文、分段、渲染、缓存、状态与展示。没有 API Key 也能用。
- **CLI 是主接口**：所有能力都能通过 `pdftranslator --json` 驱动，`next/save` 是逐页循环的核心。
- **磁盘是唯一事实来源**：译文、原文、图片、进度都落盘；进程重启不丢状态，网页与 CLI 看到的是同一份数据。
- **网页按需调用**：agent 模式下按 `T` 才拉起一次性子进程，不常驻、空闲不烧 token。
- **API 是兜底**：OpenAI 兼容 / Anthropic / mock 只在需要时使用。
- **单一译文集**：每页只有一份当前译文，记录里保留 backend/provider/model 便于追溯。

## 2. 运行拓扑

```text
                  ┌──────────────────────┐
  宿主 agent ───▶ │ CLI (cli.py)          │ ──▶ Store (.cache/ 原子读写)
  (next/save 循环) │  next/page/save/...   │
                  └──────────┬───────────┘
                             │ 读写同一份缓存
                  ┌──────────▼───────────┐
  浏览器 ───────▶ │ HTTP 服务 (server.py) │
  (web/app.js)    │  /api/meta,page,img  │
                  └───┬──────────────┬───┘
                      │              │
           ┌──────────▼───┐   ┌──────▼─────────┐
           │ AgentBridge  │   │ ApiTranslator  │
           │ 一次性子进程 │   │ HTTP 兜底      │
           └──────────────┘   └────────────────┘
```

三条翻译路径：

1. **宿主 agent（主路径）**：`next` 取页 → agent 自己翻译 → 写 `draft_path` → `save`。不经过 HTTP，不消耗额外进程。
2. **网页按 T（agent 模式）**：`POST /api/translate` → `AgentBridge` 在 `agent-run/` 空目录里跑一次性 CLI → 清洗/校验 stdout → 服务端写缓存。
3. **API 兜底（api 模式）**：`ApiTranslator.translate_paras` → 分块 HTTP 调用 → 清洗/校验 → 写缓存。CLI `batch --api` 与网页批翻都走这条。

## 3. 代码地图

| 文件 | 职责 | 关键入口 |
| --- | --- | --- |
| `pdf_translator/cli.py` | 命令行、参数解析、JSON 信封、退出码、逐页翻译流程 | `build_parser`、`_COMMANDS`、`cmd_*`、`_resolve`、`_page_payload`、`_save_one` |
| `pdf_translator/book.py` | PDF 取文、段落合并、页眉页脚去重、PNG 渲染 | `Book.paragraphs`（**0-based**）、`render_png`（**0-based**）、`extract_all` |
| `pdf_translator/store.py` | 缓存路径、原子写、译文记录、进度统计、active book | `Store`、`initialize_book`、`Store.stats`、`Store.save_zh` |
| `pdf_translator/prompts.py` | 翻译规则、提示词、清洗、校验（CLI/网页/API 共用） | `TRANSLATION_RULES`、`INSTRUCTIONS`、`build_live_prompt`、`clean_translation`、`validate_translation` |
| `pdf_translator/config.py` | 默认配置、环境变量覆盖、按书覆盖、后端选择 | `DEFAULTS`、`load_config`、`agent_settings`、`backend_settings` |
| `pdf_translator/bridge.py` | 一次性 agent 子进程、stdin/arg/file 三种提示词模式、超时与进程树清理 | `AgentBridge.translate`、`resolve_command`、`AgentError` |
| `pdf_translator/api_translator.py` | OpenAI 兼容 / Anthropic / mock HTTP 兜底 | `ApiTranslator.translate_paras`、`_call`、`_openai`、`_anthropic` |
| `pdf_translator/server.py` | 本地 HTTP 服务、1-based API、网页翻译/图片/批翻/预热 | `App`、`Handler`、`run_server` |
| `pdf_translator/web/` | 原生前端（无构建步骤） | `index.html`、`app.js`、`style.css` |
| `scripts/release.py` | 构建、归档审计、安装冒烟、可选发布 | 维护者发布用 |

## 4. 数据流与契约

### 4.1 页码边界

**公开面一律 1-based**：CLI 参数、JSON 字段、HTTP 参数、缓存文件名、译文记录里的 `page`。只有 `Book` 内部使用 0-based。

```text
HTTP/CLI/缓存文件名 (1-based)
  └─ CLI: en[page - 1] / Book 调用前 -1
  └─ Server: self.book.paragraphs(page - 1) / render_png(page - 1)
  └─ Web: state.page 0-based，只在 fetch 时 apiPage(n) = n + 1
```

新增任何页面相关接口时，在**边界做一次**转换，不要污染 `Book` 或前端内部状态。

### 4.2 缓存布局

```text
.cache/
  active_book.json              当前书指针（slug/book_id/pdf）
  <slug>/book.json              book_id / pdf / page_count / empty_pages / source_hash
  <slug>/en.json                {"pages": [[段落...], ...]}，数组 0-based
  <slug>/zh/00006.json          第 6 页译文记录，1-based 文件名
  <slug>/img/00006@2x.png       页面渲染缓存
  <slug>/drafts/00006.txt       next 返回的建议草稿路径
  <slug>/agent-run/             一次性 agent 的空工作目录
```

`book_id = sha256(PDF 内容)[:16]`；slug 由文件名清洗后加哈希前缀，复制文件夹不会串缓存。

### 4.3 单页译文记录

```json
{
  "schema": 1,
  "page": 6,
  "text": "译文正文，段落间空行",
  "backend": "agent | api | manual",
  "provider": "",
  "model": "",
  "source_hash": "sha256:...",
  "paragraph_count": 5,
  "source_paragraph_count": 5,
  "skipped": false,
  "note": "",
  "ts": 1760000000.0
}
```

写入统一走 `Store.save_zh`（原子替换、自动补 schema/page/ts）。不要绕过它直接写 JSON，否则并发与半写文件会出问题。

### 4.4 进度口径

`Store.stats()` 是唯一统计源：

- `translated`：有非空译文的页；`skipped`：显式跳过的页；`empty_pages`：`init` 时源文本为空的页（视为已完成）。
- `remaining = page_count - translated - skipped - empty_pages`；`done = remaining == 0`；`next_page` 是最小未完成页。
- CLI `status` 输出 `page_count`；HTTP `/api/status` 输出 `pages`；网页按 `s.pages` 使用。新增统计字段时，`Store.stats` → `server.status_payload` → `app.js` 三处都要接上。

### 4.5 CLI 与 HTTP 的 JSON 契约

- CLI `--json`：stdout **恰好一个** JSON 对象，日志/进度全在 stderr；UTF-8 由 `_write_stdout` 保证。
- 信封：`{"ok": true, "command": "...", "data": {...}}` 或 `{"ok": false, "command": "...", "error": {"code","message","hint"}}`。
- 退出码：0 成功、1 校验/操作失败、2 用法、3 未初始化、4 页码越界、5 译文被拒、6 agent 超时/忙、7 agent 不可用。完整表见 AGENTS.md。
- HTTP：成功直接返回数据对象；错误返回 `{"code","message","hint"}` 与对应状态码（400/404/409/502/503）。

## 5. 扩展手册

下面按“我想加什么”列出最小改动面。改完都要跑测试，必要时更新文档与发布审计清单。

### 5.1 新增一个 CLI 子命令

1. `cli.py::build_parser`：`add("name", "说明")`，按需加参数。
2. 写 `cmd_name(args) -> int`：用 `_resolve(args)` 拿 store/meta/cfg/context/glossary；成功用 `_emit(args, "name", data, human)`；失败抛 `CliError(code, message, hint, exit_code)`。
3. 注册到 `_COMMANDS`。
4. 若涉及译文写入，复用 `_save_one` 或 `Store.save_zh`，不要另写文件格式。
5. `tests/test_cli.py` 加一个 `cli("name", ...)` 用例；注意测试夹具会断言 stdout 只有一个 JSON 且非空。
6. 更新 `AGENTS.md` 的命令表；面向人的命令再更新 `README.md`。

### 5.2 新增配置项

1. 在 `config.py::DEFAULTS` 加默认值。
2. 在消费点通过 `cfg.get(...)` 读取；需要环境变量覆盖就在 `load_config` 里加（参考 `PDFTRANSLATOR_BACKEND`）。
3. 如果是 agent 相关，加入 `agent_settings` 的归一化与校验，非法值抛 `ValueError`。
4. 如果要按书覆盖，扩展 `apply_book_overrides` 或 `book_settings`。
5. `config.example.json` 加字段和 `_说明`；`AGENTS.md` 配置节说明。
6. 测试覆盖默认值/覆盖值/非法值（参考 `tests/test_cli.py`、`tests/test_bridge.py`）。

### 5.3 新增或修改 HTTP 接口

1. 业务逻辑放 `server.py::App`，返回普通 `dict`；可预期的失败抛 `ApiError(status, code, message, hint)`。
2. 在 `Handler.do_GET` / `do_POST` 里加路由；JSON 用 `self._json`，二进制用 `self._send`；POST 体用 `_read_json`（上限 2 MB）。
3. 页码参数用 `_page_arg` + `app.validate(page)`，不要自己解析边界；新接口继续 1-based。
4. 涉及翻译写入时走 `App._translate_sync` / `App._save`，保证校验、缓存和 backend 记录一致。
5. `tests/test_server.py` 加请求用例（成功、越界、错误码）；静态资源测试会检查 1-based 路径。
6. 前端如果需要，按 5.4 接入。

### 5.4 扩展网页 UI

文件分工：

- `index.html`：结构与按钮/容器 id。
- `style.css`：布局、`#zhHint` 覆盖层、响应式。
- `app.js`：状态、请求、渲染、键盘、轮询。

关键约定：

- `state.page` 是 **0-based**；所有 fetch 用 `apiPage(n)`。不要在前端全局改成 1-based。
- `state.data` 以 0-based 页缓存 `{paras, en, zh, has_zh, backend, ...}`。
- `render()` 负责中英切换与空译提示；`loadPage()` 负责取页/图片/预加载；`bindUI()` 绑定事件；`refreshStatus()` 每 2 秒轮询进度并发现外部 agent 新落盘的译文。
- **agent 模式纪律**：`meta.auto_translate` 为 false 时，切页/切中文绝不能自动发翻译请求；只有显式按 `T` 的 `ensureZh()` 才能调用 `/api/translate`。`meta.prefetch` 在 agent 模式为 0，不要绕过它。
- 新按钮/快捷键加到 `bindUI()`；需要持久化的偏好用 `localStorage`（现有：`mode`、`fs`、`page:<title>`）。
- 当前页可能在外部被翻译：保持 `refreshStatus` 里“发现 `translated_pages` 包含当前页则重新拉取”的逻辑。

**新增静态资源**（例如 `favicon.ico`、`reader.css`）需要同步四处：

1. `server.py::Handler.do_GET` 的静态白名单与 Content-Type 映射。
2. `cli.py::cmd_doctor` 的 `web_assets` 检查（如果它是启动必需资源）。
3. `scripts/release.py` 的 `WHEEL_REQUIRED` / `SDIST_REQUIRED`（如果是发布必需文件）。
4. `tests/test_server.py::test_static_assets_use_1_based_api`。

打包说明：wheel 以 `packages = ["pdf_translator"]` 收录整个包目录，`web/` 会自动进 wheel；服务端用 `importlib.resources.files("pdf_translator") / "web"` 定位，不要用相对当前工作目录的路径。

### 5.5 接入新的“一次性 agent”CLI

通常**不需要改 Python**，只改配置：

```json
{"agent": {"command": ["你的CLI", "..."], "prompt_mode": "stdin"}}
```

契约：stdin（或 `{prompt_file}` / `{prompt}`）给提示词，stdout 给译文。bridge 会：

- 在 `agent-run/` 空目录里运行（避免子进程读到项目文档后自行调用工具）；
- 自动包裹 Windows `.cmd/.bat`，不拼 shell 字符串；
- 超时杀进程树，清洗 ANSI/BOM/代码围栏，再走 `validate_translation`。

若 CLI 不读 stdin：用 `prompt_mode=file`（命令里写 `{prompt_file}`）或 `prompt_mode=arg`（写 `{prompt}`）。零成本联调模板见 `examples/adapters/echo_agent.py`，测试见 `tests/test_bridge.py` 的 fake agent 模式（ok/fenced/bom/fail/empty/slow/echo）。

### 5.6 新增 API 提供方

`ApiTranslator` 已经抽象出 `translate_paras(paragraphs, prev_tail) -> str`。接入新协议时：

1. 在 `_call` 里按 `self.provider` 增加分支，实现 `_your_provider(system, user) -> str`。
2. 复用 `clean_translation` 和现有重试/统计逻辑；不要在 provider 里写缓存。
3. 更新 `available()` / `label`（若凭据来源不同）；在 `config.example.json` 与 AGENTS.md 说明 `provider` 可选值。
4. 用 `provider: "mock"` 做端到端测试；真实 HTTP 客户端可用 `httpx.MockTransport` 或本地假服务。

如果协议与 OpenAI/Anthropic 差异很大，**优先写一个 adapter 脚本走 agent bridge**，而不是把核心代码变成协议适配泥潭。

### 5.7 新增本地模型或其他翻译后端

保持与 `ApiTranslator` 相同的窄接口（`available()` + `translate_paras(paragraphs, prev_tail) -> str`）最省事：

1. 新建 `pdf_translator/<backend>.py`。
2. 在 `server.py::App._translate_sync` 的后端分支和 `cli.py::cmd_batch` 里按需调用。
3. 更新 `config.py::backend_settings` 与 `cmd_doctor` 的可用性检查。
4. 保存仍然统一走 `App._save` / `Store.save_zh`，记录正确的 `backend/provider/model`。

### 5.8 书籍切换 / 多书管理

现状：`init` 会写 `active_book.json`，后续命令读 active book；`serve <other.pdf>` 可打开另一本，但需要重启进程。没有 `books` / `use` 命令和网页书库选择器。

要加书库：

1. 在 `Store` 上加扫描 `.cache/*/book.json` 的方法（只读，不建目录）。
2. CLI 加 `books` / `use <slug>` 命令（`set_active` 已存在）。
3. HTTP 加 `/api/books` 与切换接口；注意 `App` 当前在构造时绑定 `store/meta/book`，切换需要重建 `App` 或把它改成可变绑定。
4. 前端加选择器；切换后刷新 meta/页缓存/进度。

### 5.9 导出格式与其他输出

`cli.py::cmd_export` 是唯一导出入口。加格式时：

- 复用 `store.scan()`、`stats()`、`source_text()`，保持“缺译页要显式列出”的行为；
- 输出仍用原子写（`_atomic_write`）；
- `--force` 语义保持一致；
- 更新 AGENTS.md 与测试（参考 `tests/test_cli.py::test_verify_and_export`）。

## 6. 不变量与常见坑

- **stdout 纯净**：新代码不要在 `--json` 路径 `print` 日志；用 `log()`（stderr）。
- **1-based 边界**：CLI/HTTP/缓存文件名 1-based；`Book` 0-based；前端内部 0-based。越界统一用 `_check_page` / `App.validate`。
- **磁盘为准**：不要只在内存里维护译文；网页/CLI/agent 必须看到同一份磁盘记录。
- **原子写**：所有状态文件走 `Store` 的原子写；Windows 下 `os.replace` 已有重试。
- **agent 模式不自动花钱**：`auto_translate = backend == "api"`；agent 模式禁用预取、预热、后台批翻（409）。改 UI 或 server 时不要重新打开自动触发。
- **子进程隔离**：bridge 默认在 `agent-run/` 空目录运行；不要把 cwd 改成项目根，否则子 agent 可能读取 AGENTS.md 并自行调用工具。
- **安全**：默认只绑 `127.0.0.1`；agent 命令只来自本地配置，绝不接受 HTTP 传入；页码校验；POST 体限制。
- **Windows**：UTF-8 输出/读文件；`.cmd/.bat` 用 `%COMSPEC%`；子进程 `CREATE_NO_WINDOW`；路径含中文/空格要测。
- **HTML 注入**：前端译文用 `textContent` 写入段落，不要改成 `innerHTML` 拼译文。
- **单一译文集**：不要新增“按模型分目录”的隐式行为；重译必须显式 `--force`。

## 7. 测试地图

| 测试 | 覆盖 |
| --- | --- |
| `tests/test_pages.py` | 1-based 页码边界（首页、末页、0、负数、超界） |
| `tests/test_store.py` | 缓存原子读写、active book、统计口径 |
| `tests/test_prompts.py` | 清洗/校验（空、提示词回显、无中文、段落数） |
| `tests/test_bridge.py` | 假 agent：正常/围栏/BOM/失败/空输出/超时/回显 |
| `tests/test_cli.py` | `init → status → next → save → verify → export`、冲突、跳过、批量、中文路径、doctor |
| `tests/test_server.py` | 静态资源 1-based、meta/page/status 越界、API 翻译、agent 模式 409、API 回退 |

运行：

```bash
uv sync --extra dev
uv run pytest -q
uv run pytest tests/test_server.py -q     # 单文件
```

`tests/conftest.py` 会生成一个 4 页小 PDF（第 3 页为空）和假 agent；新增测试优先复用它，避免依赖真实书。

## 8. 部署与验证清单

```bash
uv sync
uv run pdftranslator doctor --json                 # 依赖、缓存、web 资源、后端
uv run pdftranslator init book.pdf --json
uv run pdftranslator next --json                   # 写 draft → 翻译 → save
uv run pdftranslator verify --strict --json
uv run pdftranslator export --out book.md --json
uv run pdftranslator serve --no-browser            # 网页验收
uv run python scripts/release.py --allow-dirty     # 发布前：测试+干净构建+审计+安装冒烟
```

改完功能/UI 后至少确认：

- [ ] `uv run pytest -q` 全绿。
- [ ] `--json` 命令 stdout 仍只有一个 JSON 对象（测试夹具会自动检查）。
- [ ] 第 1 页、最后一页、越界页码行为正确。
- [ ] agent 模式下切页/切中文不触发翻译；按 `T` 才调用一次。
- [ ] 外部 agent 落盘译文后，网页 2 秒内能刷新显示。
- [ ] 新静态资源能在安装后的 wheel 中通过 `doctor` 与 `serve` 找到。
- [ ] 若新增发布文件，更新 `scripts/release.py` 的审计清单并跑一次。
- [ ] 文档同步：`AGENTS.md`（agent 契约）、`README.md`（人）、本文件（扩展点）。

## 9. 范围边界

当前有意不做：MCP server、常驻 watcher、多译文集、自动 OCR、远程访问/鉴权、按模型分目录。扩展前先确认需求是否真的需要突破这些边界；多数需求可以通过“新增 CLI 命令 + 复用 Store/prompts + 前端小改”完成。
