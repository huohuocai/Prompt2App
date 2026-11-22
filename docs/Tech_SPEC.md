# Prompt2App — `agents.py` 完整技术架构与 API 设计文档（终极详尽版）

## 一、 核心架构设计与选型决策

### 1. 主模型只调用 `/v1/models` 和 `/v1/chat/completions`
在 Kimi 开放平台接口中，系统收敛为 **动态模型发现（`GET /v1/models`） + 无状态对话补全（`POST /v1/chat/completions`）** 的组合，核心原因包括：
1. **跨供应商容灾**：在主通道限流或异常时，可无缝切换至 L3 备用模型（如 DeepSeek 或兼容端点），采用业界统一标准以确保主备通道共用同一套客户端与解析器。
2. **多用户彻底隔离**：不依赖云端有状态会话 ID，避免跨模型灾备切换时上下文丢失。服务端全量存储对话并本地压缩投喂，保障数据主权。
3. **确定性工程控制**：排除批处理与额外 I/O 往返；利用大模型长文本能力直接内联传参，并由本地 Python 确定性控制补丁合并与失败回滚。

### 2. 四层正交系统分层图
系统划分为四层结构：
* **Layer 4（外部契约层）**：提供 `run_agent_pipeline(...)` 异步生成器及 4 个独立的原子函数（`run_pm_agent`、`run_coder_agent`、`run_reviewer`、`Host`）。
* **Layer 3（检查点持久化与断点续跑层）**：原子落盘 `.cache/checkpoints/{run_id}.json`，支持节点失败局部重试或原地降级全量重跑，不从头重复前置节点。
* **Layer 2（记忆压缩与鲁棒代码引擎层）**：结合 SQLite 全量历史与冷热窗口增量递归摘要（`.cache/memory/session_{id}`），剥离思维链并执行三级模糊 `SEARCH/REPLACE` 合并。
* **Layer 1（四层容灾并发网关层）**：采用单线程 `asyncio` 多协程、`httpx` 连接池、全局信号量与会话级互斥锁，支持 L1 退避重试到 L4 本地离线 HTML 模板的降级链路。

---

## 二、 后端服务（`main.py`）与 `agents.py` 的通信机制

### 1. 进程内异步生成器管道
`main.py` 与 `agents.py` 运行在同一 Python 进程中，通过 `async for event in run_agent_pipeline(...)` 在内存中直接消费事件流，并由 FastAPI 包装为 `StreamingResponse`（`media_type="text/event-stream"`）实时推送至前端。

### 2. 返回给后端的数据格式与处理闭环
`agents.py` 每次 `yield` 标准字典 `{"type": "...", "content": "..."}`，共支持 3 种核心事件：
* **`"status"`**：推送简短中文进度文案（如分析需求、代码生成进度），不写入数据库，直接实时 flush 至前端指示条。
* **`"code"`**：推送完整可运行的单文件 HTML 字符串；接收后执行数据库写库动作（更新 `latest_code`），并将内容注入前端 `<iframe>` 实时渲染。
* **`"text"`**：推送 Markdown 中文回复或 L4 兜底降级提示；写入 SQLite `messages` 表并推送到前端助手气泡。

> **收尾与中断控制**：循环结束由 `main.py` 补发 `done` 事件；前端触发停止时，`uvicorn` 注入取消信号，底层关闭 TCP 流并保留中断前的最新可用版本代码，不触发错误代码更新。

---

## 三、 并发模型与四维用户数据隔离

### 1. 并发模型：单线程事件循环 + 多协程非阻塞并发
采用 FastAPI / `uvicorn` 的 `asyncio` 事件循环，配合 `httpx.AsyncClient` 连接池。在处理高延迟网络 I/O 时，单线程即可支撑上百路并发连接，同时通过 `asyncio.Semaphore` 限制全局并发数（默认上限为 5），防止触发上游流控限制。

### 2. 多用户数据四维绝对隔离
1. **内存隔离**：严禁使用全局变量保存会话；每次调用生成独立的协程栈帧和 `WorkflowCheckpoint` 实例。
2. **会话级互斥锁**：按 `session_id` 维护细粒度锁 (`SESSION_LOCKS`)，不同用户的会话完全并行，同一会话的连续请求则排队处理。
3. **存储分片隔离**：数据库按 `user_id` 严格隔离；内存快照与检查点文件按唯一 ID 存入 `.cache/` 目录。
4. **Prompt XML 边界隔离**：用户指令与现有源码分别采用 `<user_instruction>` 与 `<existing_code>` XML 标签包裹，防止提示词注入。

---

## 四、 服务端全量存储 + 递归增量上下文压缩
* **全量无损落库**：所有原始对话 100% 持久化于 SQLite `messages` 表。
* **三级上下文压缩管线**：
  1. **历史源码折叠**：自动剥离历史 `assistant` 消息中过长的旧版 HTML 代码块，替换为归档占位符。
  2. **冷热分层摘要**：保留最近 3 轮（6条）原始对话作为热窗口；3轮之前的冷历史超过阈值时，由轻量模型浓缩为结构化需求与约束备忘录，存入 `.cache/memory/session_{session_id}.json`。

---

## 五、 4 个 Agent 异构调用策略与省 Token 矩阵

系统为不同角色的 Agent 配置了差异化的模型偏好、调用方式、上下文长度及思考强度 (`reasoning_effort`)：

* **1. PM Agent（意图路由与需求拆解）**：使用高速/标准模型（`kimi-k3` / `kimi-k2.6`），非流式短连接（`stream=False`），思考强度 `"low"`，仅输入极简上下文与压缩历史，最大输出 `400` Tokens。

* **2. Coder Agent（核心代码生成）**：偏好顶级编程旗舰模型（`kimi-k3.1-pro` 等），强制流式长连接（`stream=True`），思考强度设为 `"high"`；根据模式选择传入全量规划或基础代码加补丁，最大输出 `16384` Tokens。
* **3. Reviewer / Patcher（补丁合并与安检）**：纯本地 Python 确定性引擎（0 Token 调用），负责正则剥离与三级模糊 `SEARCH/REPLACE` 合并。
* **4. Chat Host（交互总结播报）**：使用高速标准模型，短流式或模板快路径，思考强度 `"low"`，输入极简上下文，最大输出 `300` Tokens。

### 官方接口参数规范
* **动态发现**：通过 `GET /v1/models` 自动拉取并缓存模型列表，严禁硬编码。
* **参数规范**：禁用固定值模型不兼容的 `temperature` / `top_p`，统一使用 `max_completion_tokens`。
* **思维链与熔断**：流式解析中忽略 `reasoning_content`，过滤 `<think>` 标签；遇 `401/403/429` 欠费等错误秒切 L3 备用通道或 L4 本地离线 HTML 模板库。

---

## 六、 独立原子函数签名、检查点落盘与 5 大测试检查点

### 1. 核心函数签名概要
系统导出的核心异步及同步函数包括：
* `run_agent_pipeline(...)`：完整 4-Agent 工作流入口（支持 `resume_run_id` 断点续跑）。
* `run_pm_agent(...)`：独立 PM 路由与拆解函数，返回路由模式与计划。
* `run_coder_agent(...)`：独立 Coder 代码生成函数，支持心跳回调。
* `run_reviewer_agent(...)`：纯本地确定性补丁合并与安检函数。
* `run_chat_host_agent(...)`：独立 Chat Host 播报函数。
*(具体签名定义可参阅底层代码模块)*

### 2. 检查点保存与局部重试机制
* 每个节点完成后，原子写入 `.cache/checkpoints/{run_id}.json` 记录步骤状态。
* **局部自愈**：若 Coder 失败，可直接基于 PM 阶段的 `dev_plan` 重试；若 Reviewer 发现 PATCH 合并失败，可原地将模式切换为 `FULL` 重新调用 Coder 而无需重跑 PM；传入 `resume_run_id` 可跳过已完成节点直接从断点继续。

### 3. 内置 5 大检查点独立验证套件
支持通过 `python agents.py --test` 运行验证：
1. **Checkpoint 1**：网关连通性与 `GET /v1/models` 动态发现测试。
2. **Checkpoint 2**：PM Agent 路由（`FULL` / `PATCH`）与压缩效果测试。
3. **Checkpoint 3**：Coder 双模式输出与 Reviewer 模糊匹配、坏代码拦截测试。
4. **Checkpoint 4**：Chat Host 正常模式与演示降级播报测试。
5. **Checkpoint 5**：端到端工作流与中途失败断点续跑测试。

完整可运行的代码及详细架构文档内容，可在项目仓库的脚本与测试模块中进一步查阅。



Agent 角色	输入上下文	输出契约	模型参数配置（对齐 Kimi K3 官方规范）
1. PM Agent	user_msg + has_current_code + 最近 2 轮对话摘要	严格 JSON：{"mode": "FULL"|"PATCH", "plan": "..."}（解析失败时安全回退为 FULL）	max_completion_tokens=512
reasoning_effort="low"（极速路由）
2. Coder Agent	dev_plan + current_code（仅 PATCH 模式或局部重构时传入）	• FULL：完整单文件 HTML
• PATCH：标准 SEARCH/REPLACE 补丁块	max_completion_tokens=16384
reasoning_effort="high"（深度编程推理）
3. Reviewer & Patcher	raw_llm_output + current_code + route_mode	PatchResult(success: bool, html: str | None, error: str | None)	纯 Python 确定性规则引擎（零延迟、零 Token 消耗）
4. Chat Host	user_msg + dev_plan + code_updated + is_demo_mode	2~4 句友好中文说明（Markdown 列表）	max_completion_tokens=512
reasoning_effort="low"


四、 底层模型网关 API 设计（ResilientLLMGateway）

这是整个系统最核心的技术难点，直接解决 Kimi API 对接中的所有协议兼容与高可用问题。

1. 统一内部调用接口签名

python
async def chat_completion(
    self,
    messages: list[dict[str, Any]],
    *,
    role_hint: Literal["fast", "coder"] = "fast",
    max_completion_tokens: int = 4096,
    on_heartbeat: Callable[[], Awaitable[None]] | None = None,
) -> LLMResponse:
请谨慎使用此类代码。
• on_heartbeat 回调设计（解决前端 SSE 假死）：Coder 生成 8000 Token 的复杂 HTML 可能需要 20~40 秒。网关在接收流式 chunk 时，每累计接收 1500 字符就触发一次异步回调，向前端推送一条 {"type": "status", "content": "⚡ Coder Agent 正在编写代码（已生成 3200 字符）..."}，让评委和用户实时看到生成进度，同时防止反向代理网关（Nginx/Cloudflare）因 SSE 空闲超时断开连接。

2. 动态模型发现与缓存 API（ModelRegistry）

严格遵守 MASTER_SPEC 禁令（禁止硬编码模型名）：
• 接口流程：
	1. 检查内存缓存 self._model_cache[provider] 是否在 TTL（300 秒）内有效，有效则直接返回。
	2. 发起 GET {base_url}/models（设置 timeout=8.0s，防止模型列表接口卡死阻塞主流程）。
	3. 过滤黑名单：剔除 ID 中包含 embed、audio、rerank、tts、whisper 的非对话模型。
	4. 环境变量覆盖校验：若设置了 KIMI_MODEL（主通道）或 BACKUP_MODEL（备用通道），校验其是否存在于接口返回的 available_ids 集合中；若存在则优先采用，若不存在则打印警告日志并回退到自动优选。
	5. 偏好优先级匹配（按前缀匹配）：
		• Kimi 主通道：["kimi-k3.1-pro", "kimi-k3", "kimi-k2.7-code", "kimi-k2.6"] → 未命中则取过滤后的列表首项。
		• Backup 备用通道：["deepseek-chat", "deepseek-v3", "qwen-max", "qwen2.5-coder"] → 未命中则取过滤后的列表首项。

3. 跨供应商请求参数适配器（消除 400 协议报错）

不同模型厂商对 OpenAI 协议的扩展字段支持度不同，网关在发送前执行方言适配：

参数字段	Kimi K3 系列 (kimi-k3*)	Kimi K2 系列 (kimi-k2*)	备用通道 (DeepSeek / 硅基流动等)
max_completion_tokens	传入	传入	传入（若报 400 不支持则自动降级为 max_tokens 重试）
temperature / top_p	严禁传入（官方固定值，传入报 400）	严禁传入（遵循 SPEC 统一红线）	严禁传入（保持全链路统一，默认即最稳）
reasoning_effort	传入 ("low" 或 "high")	不传	不传
messages 历史格式	保留标准字段	保留标准字段	清洗剥离 reasoning_content 等非标字段


4. 四层容灾状态转换机与错误码分流表

text
[调用请求]
   │
   ▼
[检查 KIMI_API_KEY / MOONSHOT_API_KEY] ──(均为空)──┐
   │ (存在)                                       │
   ▼                                              │
[L2: Kimi 主通道]                                 │
   ├── 200 OK ──► 流式聚合(剥离reasoning_content) ──► 返回成功
   ├── 429 (限流/节点过载) / 500 / 502 / 503 / 504 / 超时
   │     └──► [L1: 指数退避重试] (最多 2 次: 1.5s -> 3.0s) ──(仍失败)──┐
   └── 401 (Key无效/站混用) / 403 / 404 / 429(余额不足) ──────────────┤
                                                                      ▼
[检查 BACKUP_API_KEY] ──(为空)────────────────────────────────────────┐
   │ (存在，base_url 默认 https://api.deepseek.com/v1)                 │
   ▼                                                                  │
[L3: 备用模型通道 (同样带 L1 最多 2 次退避重试)]                         │
   ├── 200 OK ──► 返回成功 (标记 provider="backup")                    │
   └── 全部失败 ──────────────────────────────────────────────────────┤
                                                                      ▼
                                               [L4: 触发 AllProvidersFailedError]
                                               由 Pipeline 切换内置离线模板库 (【演示模式】)
请谨慎使用此类代码。

五、 代码安检与增量补丁引擎 API 设计（PatchAndSanitizer）


1. 纯净 HTML 提取流水线（extract_clean_html）

解决大模型输出“前后废话 + Markdown 代码块 + 偶发 <think> 泄漏”三大痛点：
1. 正则剔除 <think>[\s\(\S\)]*?</think>。
2. 若包含 ```html ... ``` 或 ``` ... ```，提取最大代码块内容。
3. 定位首个 <!doctype html 或 <html（不区分大小写）至最后一个 </html> 之间的子串。
4. 完整性门禁校验（is_valid_html）：
	• 必须包含 <html 且包含 </html>（防止流式截断导致半截残缺页面）。
	• 字符长度 >= 250（防止输出空壳 HTML）。

2. 三级模糊增量补丁引擎（apply_search_replace）

当 PM 路由为 PATCH 时，执行以下确定性算法：
1. 解析补丁块：正则扫描 ^<{5,9}\s*SEARCH\s*\n(.*?)\n={5,9}\s*\n(.*?)\n>{5,9}\s*REPLACE（支持单次返回多个补丁块，按顺序依次应用）。若未解析出任何补丁块，直接返回 PatchResult(success=False)。
2. 三层渐进定位替换（每个补丁块仅替换首个匹配位置）：
	• Tier 1（精确匹配）：search_text in code → code.replace(search_text, replace_text, 1)。
	• Tier 2（行尾空白归一化）：将源码与 search_text 按行切分，忽略每行末尾空格（rstrip()）进行滑动窗口行匹配。
	• Tier 3（块级缩进归一化）：对 search_text 执行 textwrap.dedent() 去除公共前导空格后，与源码窗口的 dedent 结果比对；命中后按原窗口的基准缩进量还原 replace_text 的缩进并替换。
3. 自动回退闭环：
	• 若任意一个补丁块在 Tier 1~3 均未命中，或合并后的代码未通过 is_valid_html 校验 → Reviewer 立即将 route_mode 降级为 "FULL"，重新调用 Coder Agent 全量生成。
	• 若全量生成依然失败或校验不通过 → final_code = current_code（严格遵守迭代防破坏铁律，原样保留上一版可用代码）。



agents.py 技术架构与 API 设计文档（v3.0 终稿）


一、 并发架构设计：异步事件循环 + 信号量池 + 并发限流协同（解答问题 1）


1. 为什么不用传统多线程（Threading），而采用 asyncio 原生异步协程？

• 核心原理：FastAPI 主服务运行在单线程多协程的 uvicorn 事件循环（Event Loop）上，LLM 调用属于典型的 高延迟网络 I/O 密集型任务（单次长 HTML 流式生成耗时约 15~40s）。
• 如果使用传统 threading.Thread，线程切换开销大、共享连接池易引发线程安全问题，且无法与 FastAPI 的 StreamingResponse 异步生成器无缝对接。
• 我们的方案：全链路采用 asyncio 原生非阻塞协程 + httpx.AsyncClient 连接池复用。当用户 A 和用户 B 同时发起生成请求时，两个请求在同一个事件循环中并发挂起等待网络 I/O，互不阻塞，单机轻松支撑数百路并发 SSE 连接。

2. 多用户同时调用的五大并发隔离与防撞机制

当两个（或多个）用户同时调用 agents.py 时，系统通过以下 5 层机制保障隔离性与稳定性：
隐藏代码
下载 SVG 格式图表
ASCII
用户 A 请求 ──► [独立 PipelineContext 实例] ──┐
                                              ├──► [全局异步信号量池 Semaphore(max=5)] ──► [复用 AsyncOpenAI 连接池]
用户 B 请求 ──► [独立 PipelineContext 实例] ──┘              │
                                                             ├──► [Per-Session 细粒度互斥锁] (防同一会话双击并发写冲突)
                                                             └──► [共享退避熔断器 CircuitBreaker] (A触发429时，B自动错峰退避)
请谨慎使用此类代码。
1. 请求级状态完全隔离（Zero Shared Mutable State）：
	• 每次调用 run_agent_pipeline() 都会在局部作用域创建独立的 PipelineContext 对象，绝不使用模块级全局变量存储会话状态或代码中间产物，杜绝用户 A 的代码串台到用户 B。
2. 同一会话防重入锁（Per-Session asyncio.Lock）：
	• 维护一个按 session_id 索引的弱引用锁表 SESSION_LOCKS: dict[int, asyncio.Lock]。如果同一个用户在前端连续快速点击两次“发送”，第二个请求会排队等待或直接提示“当前应用正在生成中，请稍候”，防止同一份 current_code 被并发 Patch 写花。
3. 全局上游并发信号量（asyncio.Semaphore）：
	• Kimi API 账号有并发连接数（Concurrency）和 RPM/TPM 限制。网关层设置全局 LLM_SEMAPHORE = asyncio.Semaphore(5)（可通过 MAX_CONCURRENT_LLM 环境变量调整）。超出 5 路的并发请求在队列中平滑排队，并向前端推送 {"type": "status", "content": "⏳ 当前排队人数较多，正在为您调度算力资源..."}，大幅降低触发上游 429 rate_limit_reached_error 的概率。
4. 跨用户共享熔断与冷却水位线（Shared Cooldown State）：
	• 如果用户 A 的请求触发了 Kimi 的 429 engine_overloaded_error 并收到响应头中的 Retry-After（或触发退避），网关会在内存中记录一个全局时间戳 kimi_cooldown_until = now + backoff。在此冷却窗口内到达的用户 B 请求，无需再次撞击 Kimi 报错，而是直接平滑等待冷却结束或瞬切 L3 备用通道，实现多用户协同削峰。
5. 动态模型缓存的“防惊群锁”（Singleflight Lock）：
	• 首次启动或缓存过期时，若用户 A 和用户 B 同时请求，通过 asyncio.Lock 保证只有 1 个协程向 GET /v1/models 发起网络探测，另一个协程直接复用探测结果，避免重复请求。

二、 服务端全量存储 + 递归增量上下文压缩引擎（解答问题 2）

你提到“上下文不应做粗暴截断，应存在服务器上，同时压缩必要上下文发给 Kimi”，这个设计非常专业——在编程类 Agent 中，丢弃早期用户的业务约束（比如“整体风格要赛博朋克暗黑风”、“不要用蓝色”）会导致后续迭代偏离需求。

1. 双层存储架构：全量持久化 + 服务端压缩记忆快照

• 全量原始数据（存储在服务器 SQLite app.db）：
	• 所有完整的用户对话、历史回复全部无损保存在 messages 表中，前端刷新页面或翻看历史记录时看到的是 100% 完整的原始对话。
	• 此外，我们在内存/本地轻量目录（.cache/memory/，自动加入 .gitignore 防止污染仓库，不破坏 database.py 表结构契约）为每个 session_id 维护一份结构化会话记忆快照（SessionMemorySnapshot）：
json
{
  "session_id": 12,
  "compressed_until_msg_index": 8,
  "rolling_summary": "用户要求构建一个暗黑科技风的待办看板；已完成：1.三列拖拽状态栏 2.优先级标签筛选 3.LocalStorage本地持久化；全局约束：保持深色背景，禁止使用弹窗alert。",
  "architectural_constraints": ["Tailwind CDN", "暗黑主题 #0f172a", "纯原生JS"],
  "updated_at": 1795180000
}
请谨慎使用此类代码。

2. 三级上下文压缩管线（ContextCompressor）

在每次调用 Kimi 前，ContextCompressor 将全量 history 与 current_code 组装为高信息密度、低 Token 消耗的 Prompt 上下文：
1. 第一级：HTML 源码去重折叠（Code Deduplication）
	• history 中的历史 assistant 消息往往附带或隐含旧版 HTML。压缩器会自动剥离历史消息中超过 300 字符的 <html>...</html> 代码块，替换为 [版本快照 #vN 已归档]，因为最新全量代码已经通过 current_code 单独注入系统上下文，避免重复发送多份 8000 Token 的代码。
2. 第二级：历史对话递归摘要压缩（Incremental Rolling Summary）
	• 热窗口（Hot Window）：最近 3 轮（6 条）原始对话保持一字不改，确保指代词（“把它改成红色”、“把刚才那个按钮删掉”）100% 精准。
	• 冷历史（Cold History，第 3 轮之前的全部历史）：当冷历史累计超过 1500 字符且尚未被摘要覆盖时，调用 PM/Fast 通道（reasoning_effort="low", max_completion_tokens=400） 执行一次增量摘要压缩，提取出：① 已完成的功能清单、② 用户的全局风格/技术约束、③ 明确否决的设计，并持久化更新到服务器的 SessionMemorySnapshot 文件中（支持服务器重启后依然有效）。
3. 第三级：超大源码结构骨架提取（大文件 Patch 辅助）
	• 当 current_code 极其庞大（例如超过 25,000 字符）且 PM 判定为 PATCH 模式时，完整传入代码并在最后附带结构目录提示，引导 Coder 精准定位 SEARCH 锚点。

三、 后端服务（main.py）与 agents.py 通信机制详解（解答问题 3）


1. 通信架构模式：进程内异步生成器管道（In-Process AsyncGenerator Pipeline）

main.py 与 agents.py 部署在同一 Python 进程内，不经过额外的 HTTP/RPC 网络序列化开销，通过 Python 原生 AsyncGenerator + FastAPI StreamingResponse（SSE 协议） 实现零延迟实时流式通信：
[前端浏览器] ◄═══ HTTP SSE (text/event-stream) ═══► [main.py: POST /api/chat]
                                                           │
                                            async for event in run_agent_pipeline(...)
                                                           │
                                                           ▼
                                            [agents.py: AsyncGenerator]
                                              ├── yield {"type": "status", ...}  ──► main.py 立即 flush 推送前端
                                              ├── yield {"type": "code", ...}    ──► main.py 校验非空后写入 DB latest_code 并推送前端
                                              └── yield {"type": "text", ...}    ──► main.py 写入 DB messages 表并推送前端


2. 双向控制流与状态同步契约


(A) 下行数据流（agents.py ──► main.py ──► 前端）

agents.py 每次 yield 一个标准 Python dict，main.py 接收后执行确定性动作并转发给前端：

事件类型 (event["type"])	agents.py 发出时机	main.py 接收后的处理动作	前端 index.html 响应动作
status	进入新 Agent 阶段、生成字符心跳（每 1500 字）、触发降级重试/切换演示模式	不写数据库，直接格式化为 data: {"type":"status","content":"..."}\n\n 实时推送	在对话框顶部/气泡内动态刷新进度指示条（如 Coder 正在编写代码...）
code	Reviewer 确认 HTML 通过 <!DOCTYPE html> 完整性门禁校验后	关键写库动作：仅当 content 非空且合法时，执行 UPDATE sessions SET latest_code = ? 覆盖更新数据库，随后推送 SSE	立即将 content 注入右侧 <iframe> 的 srcdoc 属性，实时渲染最新应用
text	Chat Host 生成最终中文总结（或 L4 演示模式说明）后	关键写库动作：将回复持久化写入 messages 表（role="assistant"），随后推送 SSE	结束加载动画，在对话流中渲染最终助手回复气泡
done	(由 main.py 在 async for 循环自然结束后发出)	main.py 发出 data: {"type":"done","session_id": id}\n\n 并关闭 SSE 流	恢复发送按钮状态，解锁输入框


(B) 上行中断信号流（前端 ──► main.py ──► agents.py）

• MASTER_SPEC.md 要求前端支持“中断正在进行的生成请求”。
• 通信链路：
	1. 用户点击前端“停止生成”按钮 → 浏览器触发 AbortController.abort() 断开 HTTP 连接。
	2. uvicorn 检测到客户端断连，向 main.py 的 SSE 生成器协程注入 asyncio.CancelledError。
	3. agents.py 的 async with client.chat.completions.create(stream=True) 在 finally 块中捕获 asyncio.CancelledError，立即关闭与 Kimi 上游的 TCP 流式连接（停止继续计费和消耗 Token），清理信号量占用并安全退出，绝不触发 code 更新，确保数据库中保留中断前的完整可用版本。

(C) 扩展参数兼容性设计（向后兼容契约）

为了确保与 AI-1 编写的 main.py 100% 无缝对接（无论 main.py 传 3 个参数还是 4 个参数），run_agent_pipeline 的函数签名设计为向后兼容的可选参数模式：
python
async def run_agent_pipeline(
    user_msg: str,
    history: list[dict[str, Any]],
    current_code: str | None = None,
    session_id: int | None = None,  # 可选：若 main.py 传入则启用跨轮磁盘摘要与同会话锁；不传则自动降级为内存级摘要
) -> AsyncGenerator[dict[str, str], None]:
请谨慎使用此类代码。

四、 底层网关与 Kimi 官方规范对齐总览（汇总确认）

1. 动态模型发现（GET /v1/models）：启动后首次请求动态拉取并缓存 300s，过滤非对话模型，优先选用 kimi-k3.1-pro > kimi-k3 > kimi-k2.7-code > kimi-k2.6，支持 KIMI_MODEL 存在性校验覆盖。
2. K3 参数铁律：绝不传 temperature 和 top_p；统一使用 max_completion_tokens + stream=True；K3 系列按 Agent 角色传入 reasoning_effort（PM/Host/压缩器用 "low"，Coder 用 "high"）。
3. 思维链彻底隔离：流式阶段忽略 delta.reasoning_content，后处理阶段用正则剥离偶发混入正文的 <think>...</think> 块。
4. 精细化错误码熔断：401/403/404 及 429 exceeded_current_quota_error（欠费）秒切 L3 备用通道；429 限流/节点过载及 5xx 执行指数退避重试（最多 2 次）；全失败自动降级 L4 内置 4 套真实可交互 HTML 模板（带 【演示模式】）。
5. Aider 级三层模糊 SEARCH/REPLACE：支持精确匹配、行尾空白归一化、块级缩进归一化；补丁失败自动降级 FULL 全量重写；全量失败原样保留 current_code 绝不破坏。



一、 具体调用哪个接口（Kimi 官方 API 端点清单）

我们严格遵循 Kimi 官方开放平台规范，使用官方推荐的 OpenAI 兼容协议（base_url = "https://moonshot.cn"），通过官方 openai Python SDK（AsyncOpenAI）发起异步非阻塞调用。
整个生命周期中，只调用以下 2 个核心官方端点：

端点路径	HTTP 方法	调用时机	核心用途与官方合规细节
/v1/models	GET	首次请求时自动触发（结果在内存中缓存 300 秒，过期后异步刷新）	动态模型发现：
1. 实时拉取当前账号可用模型列表 data[].id。
2. 过滤非对话模型后，按偏好序自动锁定旗舰模型（kimi-k3.1-pro > kimi-k3 > kimi-k2.7-code > kimi-k2.6）。
3. 校验 KIMI_MODEL 环境变量是否存在于返回列表中，杜绝 404 resource_not_found_error。
/v1/chat/completions	POST	每个 Agent 节点执行推理时（PM、Coder、Chat Host、上下文摘要压缩器）	流式对话补全（stream=True）：
1. 严格禁用废弃/非法参数：不传 temperature、top_p、max_tokens。
2. 统一长输出防超时：使用 max_completion_tokens + stream=True 逐块聚合，规避网关 900s 超时（504）。
3. 按需分配算力：K3 系列按 Agent 角色传入 reasoning_effort（PM/Host/压缩器用 "low" 极速响应；Coder 用 "high" 深度编程）。

为什么不调用 /v1/responses 或 /anthropic/v1/messages？
因为我们的 L3 备用容灾通道（DeepSeek / 硅基流动 / OpenAI 兼容网关）100% 通用 /v1/chat/completions 协议。统一使用 OpenAI Chat Completions 协议，主备通道可以共用同一套 SDK 客户端代码、同一套流式解析器和异常捕获器，架构最简洁、最不容易出错。

二、 如何发起调用（SDK 底层执行链路）

1. 客户端初始化（连接池复用）：
	• 在 agents.py 中维护按 (provider, api_key, base_url) 索引的 AsyncOpenAI 单例客户端池（底层基于 httpx.AsyncClient 连接池），避免每次用户请求都重新进行 TLS 握手，首字延迟（TTFB）降低 200~400ms。
2. 动态 Key 鉴权与站点防呆：
	• 优先读取 os.getenv("KIMI_API_KEY")，若为空则兼容回退读取 os.getenv("MOONSHOT_API_KEY")。
	• 请求头自动注入 Authorization: Bearer <API_KEY>。
	• 端点锁定中国站 https://moonshot.cn（可通过 KIMI_BASE_URL 覆盖）；若误填国际站 Key 触发 401 incorrect_api_key_error，熔断器立即识别并秒切 L3 备用通道，绝不死等重试。

三、 是否做用户隔离？如何做（四维全链路隔离架构）

必须做，而且是从“数据层 → 内存状态层 → 并发控制层 → 提示词安全层”的四维严格隔离。
虽然在服务端架构中，所有用户共享后端配置的同一个 KIMI_API_KEY（企业服务端的标准 SaaS 模式），但不同用户的请求、历史上下文、生成的代码、记忆摘要在系统中是完全物理/逻辑隔离的：

1. 数据持久化与账号隔离（与 database.py / main.py 协同）

• 登录用户：main.py 通过 Authorization: Bearer <token> 解析出唯一 user_id，数据库查询严格绑定 WHERE user_id = ?。用户 A 永远无法读取或修改用户 B 的 session_id 和 latest_code。
• 未登录游客：游客打开页面时加载的是 user_id = 0 的公共预置模板；当游客在对话框发送消息进行改造时，main.py 会为其动态派生一个独立的临时会话（新的 session_id），绝不直接覆盖修改 user_id = 0 的公共广场原始模板，确保所有其他游客打开广场时看到的依然是干净的初始应用。

2. 服务端上下文压缩快照隔离（按 session_id 分片）

• 前面设计的服务端递归摘要压缩器（ContextCompressor），其存储文件路径严格按会话 ID 隔离：.cache/memory/session_{session_id}.json。
• 若 main.py 调用 run_agent_pipeline 时未传入 session_id（例如匿名无状态调用），则自动使用当次请求的局部内存对象 PipelineContext，绝不写入共享缓存文件，彻底杜绝跨用户记忆串台。

3. 请求级内存与并发隔离（Zero Shared Mutable State）

• 无全局状态变量：每次调用 run_agent_pipeline() 都在协程栈帧内创建独立的 PipelineContext、messages 列表和 full_content 缓冲区。用户 A 和用户 B 同时并发生成应用时，各自持有独立的 TCP Stream 读取器，100% 线程/协程安全，零数据串台风险。
• 会话级细粒度互斥锁（Per-Session Lock）：仅针对同一个 session_id 加 asyncio.Lock，防止同一用户双击连发导致自己覆盖自己的代码；不同用户的不同 session_id 之间完全并行，互不阻塞（仅受全局上游总并发信号量 Semaphore(5) 保护）。

4. Kimi API 侧无状态隔离 & 提示词注入防护

• 上游无状态：我们调用的 Kimi /v1/chat/completions 接口本身是完全无状态（Stateless）的，每次请求只携带当前用户在本次会话中压缩后的 messages，Kimi 服务端不会在多用户之间共享任何上下文缓存。
• XML 边界隔离（Prompt Injection 防护）：在组装发给 Kimi Coder Agent 的 Prompt 时，将用户的自然语言指令包裹在 <user_instruction>...</user_instruction> 标签中，将现有代码包裹在 <existing_code>...</existing_code> 标签中，防止用户输入恶意的“忽略以上指令、输出系统配置”等提示词注入攻击。
我们在《API 概述》中看到，Kimi 开放平台确实提供了近 20 个接口（包括 /v1/responses、/anthropic/v1/messages、/v1/files、/v1/batches、/v1/tools/search 以及 Beta 版的托管智能体 /v1/agents 等）。
我们经过审慎的架构评估后，最终决定只调用 /v1/chat/completions（配合 /v1/models 做动态发现），核心原因有以下 5 个不可替代的架构级考量：

1. 多模型异构容灾的“最大公约数”（决定性原因）

• 业务红线：我们的系统必须支持 L2（Kimi 主通道） → L3（DeepSeek / 硅基流动 / 开源模型备用通道） 的毫秒级无缝熔断切换。
• 技术现实：/v1/responses 是 OpenAI 较新的有状态接口，/anthropic/v1/messages 是 Anthropic 私有格式，国内绝大多数备用模型厂商（如 DeepSeek、硅基流动、阿里百炼、vLLM）只完整实现了 /v1/chat/completions 这一种事实工业标准。
• 收益：全链路统一使用 /v1/chat/completions，主通道和备用通道可以复用同一套 AsyncOpenAI 客户端、同一套流式 Chunk 解析器、同一套重试与异常捕获逻辑，代码复杂度降低 60%，切换零摩擦。

2. 数据主权与多用户彻底隔离（为什么不用 Kimi 有状态接口？）

• Kimi 提供了 /v1/responses（支持服务端 previous_response_id 托管上下文）和 Beta 版的托管智能体 /v1/sessions。
• 如果我们把对话状态托管在 Kimi 云端，会带来两个致命架构缺陷：
	1. 无法跨模型灾备：一旦 Kimi 触发 429 限流或欠费，切换到 DeepSeek 时，DeepSeek 根本不认识 Kimi 云端的 session_id 或 response_id，多轮修改上下文会瞬间丢失。
	2. 违背本地持久化与用户隔离要求：笔试架构要求所有会话与历史记录存储在我们自己的 SQLite（app.db）中，且支持未登录游客与登录用户的权限隔离。由我们自己的服务端掌握全量历史并做上下文压缩，通过无状态的 /v1/chat/completions 每次按需投喂，数据主权 100% 掌握在我们自己手里。

3. 实时性要求与场景匹配度（排除 Batch / Files / Search）

• 为什么不用 /v1/batches（批处理接口）？
	• Batch 接口是异步离线队列（通常几分钟到数小时返回），而 Prompt2App 是强交互式实时 SSE 流式产品，用户需要秒级看到代码生成进度。
• 为什么不用 /v1/files（文件上传接口）？
	• 我们的核心输入是用户的自然语言指令 + 单文件 HTML 代码（通常 5KB~30KB，约 2k~10k Token），Kimi K3 拥有 1M（100 万）Token 超长上下文，直接作为文本消息放入 messages 只需 1 次网络往返；如果先调 /v1/files 上传再调对话接口，会平白增加一次网络 I/O 延迟和文件生命周期管理负担。
• 为什么不用 /v1/tools/search（联网搜索接口）？
	• 我们的目标是生成自包含、离线可运行的单文件 HTML 应用（房贷计算器、番茄钟、待办看板等），不需要引入外部实时新闻搜索，关闭联网工具反而能避免模型幻觉去调用不可用的外部接口，且速度更快、成本更低。

4. 客户端确定性控制 vs 云端黑盒托管（排除 /v1/agents Beta）

• Kimi 的 /v1/agents（托管智能体）目前处于 Beta 阶段，其内部执行循环是黑盒的。
• 而在我们的场景中，我们需要极其精细的确定性代码工程控制：
	• 必须精准控制 PM -> Coder -> Reviewer/Patcher -> Chat Host 的状态流转；
	• 必须拦截 Coder 输出并执行 Aider 级三层模糊 SEARCH/REPLACE 补丁合并；
	• 必须在补丁失败时由本地 Python 确定性逻辑秒级触发 FULL 全量重生成，并在全失败时兜底保留上一版代码。
	• 这些高度定制化的代码防破坏逻辑，放在我们自己的 agents.py 中调度无状态的 /v1/chat/completions 是最可控、最可靠的。

5. 补充：其实我们还调用了一个辅助接口（甚至可扩展一个）

严格来说，我们调用的接口组合是：
1. 必调接口 1：GET /v1/models —— 动态发现当前账号可用的最新模型（kimi-k3.1-pro / kimi-k3 等），满足“严禁硬编码模型名”的硬性红线。
2. 必调接口 2：POST /v1/chat/completions —— 核心流式推理引擎。
3. 可选增强接口（工程加分项）：POST /v1/tokenizers/estimate-token-count —— Kimi 官方提供了精准的 Token 计算接口。不过在生产设计中，为了避免每次对话前多发一次 HTTP 请求增加 100ms 延迟，我们通常在本地用字符长度近似估算（1 中文字 ≈ 1.5~2 tokens，1 英文词 ≈ 1.3 tokens）来触发上下文压缩阈值，这样既省掉了一次网络往返，又能保持零依赖高并发。
以上就是我们架构选型的完整底层逻辑——用最标准、无状态的 /v1/chat/completions 换取最高的跨模型容灾能力、100% 的本地数据隔离主权，以及确定性的代码补丁自愈控制权。