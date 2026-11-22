# Prompt2App — Atoms 笔试满分交付版主控规格说明书 (MASTER_SPEC.md)

## 1. 笔试核心红线与通过基线（所有 AI 必读）
1. **双硬规矩（一票否决）：**
   * **国内可直连且无痕可开：** 监听 `0.0.0.0`，端口读取 `os.environ.get("PORT", 8000)`，严禁硬编码 `localhost`。
   * **冷打开绝不空白：** 数据库启动时必须自动注入 **4 个预置可交互应用**（房贷计算器、待办看板、个人主页、番茄钟），未登录游客打开页面即可直接点击预览和交互。
2. **四层模型防崩防线（清 Key 自测必过）：**
   * 第 1 层：请求重试（遇到 429 限流自动退避重试最多 2 次；429 细分节点过载 / RPM / TPM / TPD，见 Kimi 官方错误码文档）。
   * 第 2 层：主力调用 **Kimi API**（`KIMI_API_KEY`，兼容官方命名 `MOONSHOT_API_KEY`；端点 `https://api.moonshot.cn/v1`）。
   * 第 3 层：主通道异常时无缝切换 **备用开源模型 API**（`BACKUP_API_KEY`，默认硅基流动 SiliconFlow：`https://api.siliconflow.cn/v1`；可经 `BACKUP_BASE_URL` 指向 DeepSeek 官方等任意 OpenAI 兼容端点）。依据 SiliconFlow 官方文档（api-docs.siliconflow.cn）适配：token 上限字段为 `max_tokens`（非 `max_completion_tokens`，且不含思维链）、模型 ID 为「厂商/模型名」格式（经其官方 `GET /v1/models` 动态发现，**偏好序刻意不含 Kimi 系模型**——主通道故障多为 Kimi 侧问题，回退同厂商无容灾意义：`Pro/deepseek-ai/DeepSeek-V4` → `deepseek-ai/DeepSeek-V4` → `deepseek-ai/DeepSeek-V4-Flash` → `Pro/zai-org/GLM-5.2`）、无 `prompt_cache_key` 字段（不传）、`reasoning_effort` 仅支持 high/max 且仅少数推理模型适用（不传）。Kimi 与备用通道的请求参数按 `provider` 显式分开构造（`_build_request_params`）。
   * 第 4 层（保命层）：若无 API Key 或全部调用失败，**绝不报错 500 或白屏**，自动匹配内置的本地真实可交互 HTML 模板完成输出，并在对话中标注 `【演示模式】`。
   * **依据 Kimi 官方文档（platform.kimi.com/docs/api）的实现约束（2026-11 修正）：**
     - 模型名**禁止硬编码**：运行前调用官方 `GET /v1/models` 动态发现当前账号可用模型，按偏好序自动选择（优先 `kimi-k3` → `kimi-k3.1-pro`；K2 系列官方已公告 2026 年底下线且新账号不再开放，仅作兼容回退）；`KIMI_MODEL` 仅作显式覆盖且必须校验存在于官方列表，杜绝 `resource_not_found_error`（404）。
     - K3 系列 `temperature`/`top_p` 为固定值，**不得显式传入**（传入会报错）；`max_tokens` 已弃用，统一使用 `max_completion_tokens`；长输出采用 `stream=true` + `stream_options.include_usage` 聚合，规避官方网关 900s 超时（504）并统计用量。
     - 平台 Key 隔离：`platform.kimi.com`（中国站）与 `platform.kimi.ai`（国际站）的账户、余额与 API Key 完全独立，混用返回 401。
     - 数据隔离与保留（官方机制）：上下文缓存按组织（org）隔离、命中需请求前缀逐字节一致——用户数据一律放在消息尾部，无跨用户暴露风险；API 请求不传任何用户 PII，仅传匿名会话缓存键。企业客户可按需申请 ZDR（零数据保留）：响应返回后内容即清除、不用于训练，接口与计费不变；ZDR 不适用于 Batch/文件上传/托管智能体（本项目均不涉及）。ZDR 下平台不留存请求内容，排查依赖应用层自身日志，日志须脱敏。
     - **省 token 设计（官方「上下文缓存最佳实践」）**：system prompt 固定在前、用户变量放在消息尾部以最大化前缀缓存命中（命中价为 1/10，TTL 默认 5m 自动生效）；PM Agent 走官方 JSON Mode（`response_format={"type":"json_object"}`）避免解析重试；历史仅保留最近 6 条文本消息且 assistant 的大段 HTML 代码以占位符替代；每轮结束输出 token 用量与缓存命中率状态。
     - 本项目仅使用纯文本对话（Chat Completions），不涉及文件上传与图片输入接口。
3. **迭代防破坏铁律：**
   * 局部修改采用 `SEARCH / REPLACE` 补丁机制；若补丁匹配失败或生成异常，**保留上一版可用代码，绝不覆盖破坏**。

---

## 2. 系统架构与目录规范
```text
project/
├── main.py              # FastAPI 主入口、静态页托管、鉴权与 SSE 接口（AI-1 负责）
├── database.py          # SQLite 自动建表、预置 4 个种子应用数据、CRUD（AI-1 负责）
├── agents.py            # 4-Agent 流水线、四层容灾引擎、Patch 合并与内置兜底模板（AI-2 负责）
├── static/
│   └── index.html       # 类 Gemini 三栏布局 + 广场展示 + 沙盒预览（AI-3 负责）
├── requirements.txt     # fastapi, uvicorn, openai, pydantic
├── .gitignore           # 必须包含: .env, *.db, __pycache__/
└── README.md            # 包含：项目说明、技术栈、本地启动步骤、AI 协作与降级设计说明
```

---

## 3. 数据库与预置数据设计（`database.py`）
使用 Python 内置 `sqlite3`（文件名 `app.db`）：
1. **`users` 表：** `id`, `username` (唯一), `password_hash` (SHA-256), `created_at`
2. **`sessions` 表：** `id`, `user_id` (0 代表公共广场预置项目), `title`, `latest_code`, `is_public` (1 为广场可见), `updated_at`
3. **`messages` 表：** `id`, `session_id`, `role`, `content`, `created_at`
4. **启动种子数据（Seed Data）：** `init_db()` 检查若 `sessions` 表为空，自动插入 4 个完整可运行的单文件 HTML 应用（`user_id=0, is_public=1`）：
   * 《智能房贷计算器（支持提前还款对比）》
   * 《项目管理待办看板（支持优先级与状态切换）》
   * 《极简程序员个人主页》
   * 《专注番茄钟与任务计时器》

---

## 4. 前后端统一 API 契约

1. **`GET /api/public/showcase`**（无需登录，保证评委打开即有内容）
   * 返回 `sessions` 表中 `is_public=1` 的预置项目列表及最新代码。
2. **`POST /api/login`**（注册与登录合一，拿满服务端账号分）
   * Request: `{"username": "str", "password": "str"}`
   * Logic: 查表 -> 存在且密码哈希匹配则返回 Token；不存在则自动插入新用户并返回 Token；密码错误返回 401。
   * Response: `{"token": "str", "username": "str"}`
3. **`GET /api/sessions`**（Header: `Authorization: Bearer <token>`，支持未登录游客传入空 Token 返回公共广场列表）
   * Response: `[{"id": 1, "title": "...", "is_public": 1, "updated_at": "..."}]`
4. **`GET /api/sessions/{session_id}`**
   * Response: `{"id": 1, "title": "...", "messages": [...], "latest_code": "<!DOCTYPE html>..."}`
5. **`POST /api/chat`**（SSE 流式返回 `text/event-stream`，允许游客或登录用户调用）
   * Request: `{"session_id": int | null, "message": "str"}`
   * 流式事件格式 `data: <JSON>\n\n`：
     * `{"type": "status", "content": "PM Agent 正在拆解需求..."}`
     * `{"type": "text", "content": "已为你生成..."}`
     * `{"type": "code", "content": "<!DOCTYPE html>..."}`
     * `{"type": "done", "session_id": 123}`

---

## 5. 多 AI 协同开发任务包

### 【任务包 AI-1】后端与持久化工程师（`database.py`, `main.py`, `requirements.txt`, `.gitignore`）
* 实现 SQLite 自动建表，并在 `database.py` 内置 **4 个高质量可交互单文件 HTML 种子应用**（含完整 JS 交互逻辑），确保首次启动数据库不为空。
* 实现 `POST /api/login`（SHA-256 密码哈希 + Token 鉴权）、`GET /api/sessions`、`GET /api/sessions/{id}`、`POST /api/chat`。
* **关键要求：** 游客（未登录）也可体验生成和查看预置应用，登录后则将数据隔离保存到个人账号下；在 `/api/chat` 中，仅当 `agents` 返回合法非空 HTML 时才覆盖更新 `latest_code`；启动端口严格读取 `PORT` 环境变量并监听 `0.0.0.0`。

### 【任务包 AI-2】智能体与四层容灾引擎工程师（`agents.py`）
* **一键直通工作流：** `async def run_agent_pipeline(user_msg, history, current_code, prompt_cache_key=None, on_checkpoint=None)`，依次 yield `status` / `text` / `code` 事件（`done` 由 main.py 追加）。
* **独立 Agent 函数（可单独测试，2026-11 新增）：** `pm_agent(user_msg, current_code)` → `PMResult`；`coder_agent(pm, user_msg, history, current_code)` → `CoderResult`；`reviewer_agent(coder, user_msg, current_code)` → `ReviewResult`；`chat_host_agent(user_msg, route, ok)` → `str`。阶段间以结构化结果（dataclass）传递，`PMResult.to_dict()/from_dict()` 可入库暂存。
* **两阶段人机协同工作流（2026-11 新增）：** `events, pm = await run_pm_phase(user_msg, history, current_code)` 只跑 PM 并返回待播放事件与拆解方案 → main.py 播放后结束 SSE、前端展示方案 → 用户确认/修改后以新 `PMResult`（或 dict）调 `run_build_phase(pm, ...)` 流式继续 Coder → Reviewer → Chat Host。直通模式下 `on_checkpoint(stage, result)` 回调在 PM/Coder/Reviewer 每个阶段边界触发：返回 None 直通、返回修改后 result 生效、`raise CheckpointAbort` 优雅取消（保留已有代码，事件契约完整）。
* 实现 **4-Agent 流程**：PM（路由 `FULL` 或 `PATCH`） -> Coder -> Reviewer/Patcher -> Chat Host。
* **局部 Patch 机制：** 修改已有应用时，要求 Coder 输出 `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE` 补丁块并合并；若合并失败，自动降级为 `FULL` 全量生成；若仍失败，保留 `current_code` 原样返回并提示用户。
* **四层防崩保命机制（重中之重）：** 封装 LLM 调用器（重试 -> Kimi -> 备用开源模型 -> **内置离线真实可交互 HTML 模板库**）。确保在环境变量无任何 API Key 或断网报错时，依然能正常 `yield` 出完整的 `status`、`code` 和 `text`（带 `【演示模式】` 提示），绝不抛出未捕获异常。
* **模型发现（依据官方文档强制）：** 主力模型名必须从官方 `GET /v1/models` 接口动态获取当前支持的模型，**禁止硬编码、禁止依赖环境变量默认值**；`KIMI_MODEL`/`BACKUP_MODEL` 仅作显式覆盖且须校验在官方列表内；不传 `temperature`/`top_p`，用 `max_completion_tokens` + `stream=true`。详见第 1 节四层防线约束。
* **会话缓存键与用户区分：** `run_agent_pipeline` 支持可选参数 `prompt_cache_key`，main.py 应传 `f"p2a-session-{session_id}"`（官方建议多轮对话 Agent 传入会话标识以提升前缀缓存命中率）。Kimi 官方请求 schema **无 `user` 端到用户字段**，用户隔离完全由应用层（`users`/`sessions` 表 + 登录鉴权）完成；429 限速为组织级，如需单用户限流/配额由 main.py 在应用层实现。
* **并发纪律：** agents.py 为 asyncio 单线程协程模型（请求间并发、流水线内串行）；main.py 在 async 端点中执行**同步** sqlite3 查询时必须经 `asyncio.to_thread` 包装，避免阻塞事件循环导致所有用户的 SSE 流卡顿。

### 【任务包 AI-3】前端交互与沙盒预览工程师（`static/index.html`）
* 使用单文件 HTML + Tailwind CSS CDN + Alpine.js/原生 JS 实现类 Gemini 现代三栏界面（左侧案例广场/历史记录、中间流式对话区、右侧 `<iframe>` 沙盒预览区）。
* **首屏体验：** 页面加载后**不弹出强制遮挡登录框**，自动加载并默认选中第 1 个预置广场项目，让用户/评委一打开就能在右侧直接操作应用；右上角提供明显的“登录 / 注册”按钮及当前账号状态。
* 右侧预览区配备“刷新沙盒”、“新窗口打开”、“下载 HTML 源码”三个按钮，并支持中断正在进行的生成请求。
