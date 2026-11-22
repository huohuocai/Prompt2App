# Prompt2App 多 Agent 笔试交付方案 v2.1

> 目标：在极短周期内交付一个满足《Atoms 笔试指南》的“多 Agent 协作生成可运行网页应用”项目，而不是复刻 Atoms 官网或把多 Agent 退化成单模型聊天。
>
> 版本日期：2026-11-20  
> 当前状态：**待确认版**  
> 主要输入：`MASTER_SPEC.md`、`Atoms笔试指南.docx`、Atoms 官网结构检查结论

---

## 1. 一页结论

本项目应交付一个 **Prompt2App 工作台**，核心闭环为：

**用户输入一句话需求 → PM Agent 拆解需求并判断 FULL/PATCH → Coder Agent 生成完整 HTML 或局部补丁 → Reviewer Agent 独立审查并驱动 Patcher 原子合并 → Chat Host Agent 汇总协作过程并流式反馈 → iframe 实时运行 → SQLite 服务端持久化 → 支持版本回滚与降级兜底。**

笔试的关键不是“页面像不像 Atoms”，而是：

1. 在线链接可公开访问；
2. 国内网络可直连；
3. 打开页面不空白；
4. AI 能生成真实可运行的网页应用；
5. 生成的应用能交互；
6. 数据能持久保存；
7. 模型失败时演示仍能完整跑完；
8. GitHub 仓库 public 且不含 API Key；
9. 支持基础注册登录与服务端数据隔离；
10. 手机端可正常操作。

因此，第一版应放弃 Atoms 营销官网复刻，把精力集中在 **生成闭环、持久化、稳定性、降级兜底和可部署性**。

---

## 2. 方案修正结论

### 2.1 对前一版“Atoms 官网复刻＋对话演示”方案的批判

前一版方案的问题不是视觉方向，而是目标错位：

- 过度重视落地页还原，偏离笔试题；
- 偏静态部署，难以满足服务端账号与跨设备持久化；
- “聊天回复”不等于“生成可运行产物”；
- GitHub Pages 不适合承载 FastAPI、SQLite 与 SSE 后端；
- 即使页面很像 Atoms，也无法弥补最小闭环缺失。

结论：**不再以官网复刻作为实施主线。**

### 2.2 对 `MASTER_SPEC.md` 的保留意见

原规格已经抓住笔试主线，但存在以下风险：

1. **GitHub Codespaces 只能作为开发环境，不应作为最终公开交付链接。**
2. `app.db` 只有在部署平台提供持久文件系统时才可靠。
3. 自由文本 `SEARCH/REPLACE` 补丁必须原子执行，失败时不能覆盖旧版本。
4. 必须保留真实多 Agent 协作，同时通过角色化短提示、超时、重试和单 Agent 级模型回退控制延迟、成本和 429 风险。
5. 三张表不足以支撑版本回滚与生成应用内部状态管理。
6. SHA-256 直接哈希密码偏弱，应改为标准库加盐 PBKDF2。
7. “100% 可运行”不能靠单次语法检查保证，应有运行失败回退与兜底模板。

### 2.3 最终实施原则

- 以笔试指南为唯一验收主线；
- 保留原规格的 FastAPI＋SQLite＋单文件前端架构；
- 砍掉营销官网和不必要产品功能；
- 先打通最小闭环，再确保 PM、Coder、Reviewer、Chat Host 都是真实 Agent 协作；
- 所有失败路径必须可控、可恢复、可解释；
- 演示兜底必须明确标注，不伪装成真实模型生成。

---

## 3. 决策记录与待确认事项

以下内容不能视为用户已经做出的选择，只作为当前默认建议。

| 决策项 | 当前建议 | 状态 |
|---|---|---|
| 产品方向 | 做 Prompt2App 工作台，不做 Atoms 官网 | 建议锁定 |
| 首个生成模板 | 待办看板生成器 | 建议锁定 |
| 执行节奏 | 8 小时完成核心闭环，剩余时间加固 | 待确认 |
| 最终部署 | 使用带持久文件系统的后端主机 | **待解决，外部依赖** |
| 模型接口 | Kimi 主模型＋OpenAI 兼容备用模型 | 待确认 Key |
| 模型失败 | 真实调用优先，失败后自动降级到标注清楚的模板 | 建议锁定 |
| 持久化等级 | 会话与代码服务端持久；应用状态尽量服务端持久 | 建议按高标准执行 |
| 多智能体 | PM、Coder、Reviewer、Chat Host 四 Agent 真实协作；Patcher 作为 Reviewer 的工具 | 建议锁定 |
| GitHub 仓库 | public、无 Key、有 README 和 .gitignore | 必须满足 |
| 最终域名 | 有则使用；没有时先用云主机 HTTP 地址 | 待确认 |

---

## 4. 目标与非目标

### 4.1 项目目标

交付一个可通过公网访问的 AI 应用生成器，使用户能够：

1. 使用用户名和密码自动注册／登录；
2. 输入一句自然语言需求；
3. 通过 PM、Coder、Reviewer、Chat Host 多 Agent 协作生成完整单文件 HTML 应用；
4. 在 iframe 沙盒中实时运行生成结果；
5. 通过多轮对话对已有应用进行局部修改；
6. 刷新、重开浏览器、更换设备后恢复会话；
7. 在模型不可用或额度不足时，通过降级模板继续演示；
8. 下载源码、在新窗口打开、刷新沙盒、恢复旧版本；
9. 在手机端完成登录、对话、预览与基础操作。

### 4.2 明确非目标

第一版不做以下功能：

- 完整复刻 Atoms 官网；
- Atoms 定价页、客户案例页与营销页；
- 真正的可视化拖拽编辑器；
- 自动生成并部署独立站点；
- Stripe 支付；
- Google Ads、SEO 自动化；
- 真实团队协作；
- 多模型并行赛马；
- 生产级复杂权限系统；
- 原生 App 或小程序；
- 任意需求生成任意复杂应用。

---

## 5. 硬性验收条件

以下任一项失败，都应视为未达交付标准。

### 5.1 在线交付

- 最终链接不是 `localhost`；
- 链接可被无痕窗口打开；
- 国内网络可直连，不依赖代理；
- 部署位置具备持久文件系统；
- 项目进程监听 `0.0.0.0`；
- 端口读取环境变量 `PORT`；
- 首次打开页面不空白。

### 5.2 GitHub 仓库

- 仓库为 public；
- 无痕窗口可访问；
- 不提交 API Key；
- `.env`、数据库文件和缓存文件进入 `.gitignore`；
- README 完整；
- 依赖写入 `requirements.txt`；
- 代码结构清晰，有基础测试或自测记录。

### 5.3 最小闭环

- 用户输入一句话；
- 系统生成真实 HTML；
- iframe 中真实运行；
- 生成结果可交互；
- 会话和生成结果服务端保存；
- 刷新后仍能恢复。

### 5.4 稳定性

- 主模型失败时自动切备用模型；
- 备用模型失败时使用内置模板；
- 无 Key、错 Key、断网时不能白屏；
- 补丁失败不能覆盖上一版；
- 全量兜底失败时保留上一版可用代码。

### 5.5 移动端

- 页面不出现横向滚动；
- 输入框、按钮和列表可触控；
- 手机端在“对话”和“预览”之间清晰切换；
- 登录弹窗、错误提示、加载状态均可用。

---

## 6. 范围优先级

### 6.1 P0：不完成就不能交付

1. FastAPI 后端；
2. SQLite 自动建表；
3. 用户名／密码自动注册与登录；
4. 会话与消息服务端持久化；
5. 生成代码服务端持久化；
6. 首屏不空白；
7. 输入需求并生成 HTML；
8. iframe 实时运行；
9. 手机端基础可用；
10. 公开部署链接；
11. public GitHub 仓库；
12. API Key 不入库、不入前端。

### 6.2 P1：决定能否稳定达到交付基线

1. SSE 流式输出；
2. PM Agent 判断 `FULL/PATCH` 并输出验收标准；
3. Coder Agent 输出完整代码或局部补丁；
4. Reviewer Agent 独立审查，并驱动 Patcher 原子合并补丁；
5. 补丁失败重试；
6. 补丁失败降级全量生成；
7. 全量失败保留旧版；
8. `app_versions` 版本快照；
9. 恢复上一版；
10. 3—5 个预置项目；
11. 双模型容灾；
12. 无 Key 模板兜底；
13. 下载源码、新窗口打开、刷新沙盒；
14. 多 Agent 协作过程刷新后仍可恢复展示。

### 6.3 P2：不影响第一版交付，时间充足再做

1. 生成应用内部状态跨设备保存；
2. 更丰富的预置模板；
3. 分享链接；
4. 项目重命名与删除；
5. 更细的模型用量统计；
6. 更完整的 HTML／JS 静态检查；
7. 更精致的动效与空状态；
8. 多语言界面；
9. 代码高亮；
10. PWA 支持。

---

## 7. 总体架构

```text
浏览器（单文件 HTML）
├── 登录遮罩
├── 会话列表
├── 聊天与 SSE 流式状态
├── iframe 应用沙盒
└── 版本、下载、新窗口、刷新沙盒

        ↓ HTTP / SSE

FastAPI 主服务
├── 登录与 Token 校验
├── SQLite 数据库访问
├── 会话／消息／版本读写
├── Agent Pipeline
│   ├── Orchestrator／共享任务状态
│   ├── PM Agent
│   ├── Coder Agent
│   ├── Reviewer Agent
│   ├── Patcher／校验工具
│   └── Chat Host Agent
├── 模型调用器
│   ├── Kimi 主模型
│   └── OpenAI 兼容备用模型
└── 降级模板引擎

        ↓

SQLite 数据库
├── users
├── sessions
├── messages
├── app_versions
├── app_states（可选增强）
└── tokens
```

### 7.1 核心设计约束

- 前端不直接调用大模型；
- API Key 只存在于服务端环境变量；
- 所有数据库读写通过统一数据访问层；
- 所有模型异常必须被捕获并转换为可控降级路径；
- 补丁合并必须原子提交；
- 新版本只有通过基础质检后才更新 `latest_code`；
- iframe 使用沙盒隔离，避免生成代码直接访问主页面。

---

## 8. 建议目录结构

```text
project/
├── main.py                  # FastAPI 入口、鉴权、路由、静态托管
├── database.py              # SQLite 初始化、迁移、CRUD
├── agents.py                # 智能体流水线、模型调用、补丁、兜底
├── templates.py             # 无模型兜底模板与预置项目数据
├── security.py              # 密码哈希、Token、限流
├── static/
│   └── index.html           # 单文件工作台前端
├── tests/
│   ├── test_auth.py
│   ├── test_database.py
│   ├── test_patch.py
│   └── test_api.py
├── Dockerfile
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── REVISED_SPEC.md
```

> 目录允许微调，但必须保持后端、数据库、智能体、前端职责分离。

---

## 9. 数据模型

### 9.1 表结构建议

```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,       -- 建议格式：pbkdf2$iterations$salt$hash
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    latest_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'status')),
    agent TEXT,                        -- assistant 消息可标记 pm/coder/reviewer/host
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS app_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('FULL', 'PATCH', 'FALLBACK')),
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    parent_version_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS app_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

### 9.2 字段设计原则

- `latest_code` 用于快速读取当前最新版本；
- `app_versions` 用于审计、回滚和失败恢复；
- `messages.agent` 用于在多 Agent 刷新后仍能还原 PM、Coder、Reviewer、Chat Host 的协作过程；
- `messages` 不保存大段完整代码，完整代码保存到 `sessions.latest_code` 与 `app_versions`；
- `app_states` 为可选增强，用于保存生成应用内部状态；
- 时间统一使用 ISO 字符串，避免时区歧义；
- 所有用户数据查询必须携带 `user_id` 或先通过会话归属校验。

### 9.3 预置数据策略

- 不在 GitHub 提交真实 `app.db`；
- 首次启动时，如果项目库为空，自动写入 3—5 个预置项目；
- 预置项目应使用真实可运行 HTML，而不是截图或假数据；
- 至少包含一个待办看板、一个个人主页、一个小工具页面。

---

## 10. API 契约

### 10.1 登录／自动注册

`POST /api/login`

请求：

```json
{
  "username": "demo",
  "password": "123456"
}
```

成功响应：

```json
{
  "token": "random-token",
  "username": "demo"
}
```

失败响应：

```json
{
  "error": "密码错误"
}
```

规则：

- 用户名不存在时自动创建；
- 密码错误返回 401；
- 用户名长度建议 2—32；
- 密码最小长度建议 6；
- Token 设置过期时间；
- 登录接口增加基础限流。

### 10.2 会话列表

`GET /api/sessions`

Header：

```text
Authorization: Bearer <token>
```

响应：

```json
[
  {
    "id": 1,
    "title": "待办看板",
    "updated_at": "2026-11-20 21:00:00"
  }
]
```

### 10.3 会话详情

`GET /api/sessions/{session_id}`

响应：

```json
{
  "id": 1,
  "title": "待办看板",
  "messages": [
    {
      "role": "user",
      "agent": null,
      "content": "我要一个待办看板"
    },
    {
      "role": "assistant",
      "agent": "pm",
      "content": "已拆解出任务列表、优先级和持久化需求。"
    },
    {
      "role": "assistant",
      "agent": "host",
      "content": "多 Agent 已完成生成，右侧可预览。"
    }
  ],
  "latest_code": "<!DOCTYPE html>..."
}
```

必须校验会话属于当前用户。

### 10.4 智能体对话

`POST /api/chat`

Header：

```text
Authorization: Bearer <token>
```

请求：

```json
{
  "session_id": null,
  "message": "我要一个带优先级和截止日的待办看板"
}
```

响应：`text/event-stream`

每行格式：

```text
data: {"type":"status","agent":"pm","content":"PM 正在分析需求..."}

data: {"type":"status","agent":"coder","content":"Coder 正在生成代码..."}

data: {"type":"status","agent":"reviewer","content":"Reviewer 正在检查代码..."}

data: {"type":"text","agent":"host","content":"PM、Coder 和 Reviewer 已完成协作..."}

data: {"type":"code","agent":"reviewer","content":"<!DOCTYPE html>..."}

data: {"type":"done","agent":"orchestrator","session_id":1}
```

事件类型定义：

| type | 含义 |
|---|---|
| `status` | 智能体阶段与处理状态 |
| `text` | Chat Host 或其他 Agent 的自然语言反馈 |
| `code` | Reviewer 通过后的完整 HTML |
| `done` | 当前请求结束 |
| `error` | 可展示给用户的错误信息 |

`agent` 字段建议取值：

| agent | 含义 |
|---|---|
| `pm` | PM Agent |
| `coder` | Coder Agent |
| `reviewer` | Reviewer Agent |
| `host` | Chat Host Agent |
| `orchestrator` | 流水线控制与结束事件 |

规则：

- 流式过程中发生异常也必须发送 `error` 或 `done`；
- 前端不能在 SSE 中断时无限等待；
- 新会话在首次成功生成后自动创建；
- 会话标题由首条用户输入自动截取生成。

### 10.5 版本恢复

`POST /api/sessions/{session_id}/versions/{version_id}/restore`

成功后将指定版本写回 `latest_code`，并产生一条新的恢复记录。

### 10.6 生成应用状态保存

`PUT /api/sessions/{session_id}/state`

请求：

```json
{
  "state": {
    "todos": []
  }
}
```

`GET /api/sessions/{session_id}/state`

用于实现生成应用内部状态的跨设备恢复。该项属于 P2 增强，不影响 P0/P1。

### 10.7 健康检查

`GET /api/health`

响应：

```json
{
  "ok": true
}
```

用于部署平台和外部探活。

---

## 11. 多 Agent 协作流水线

### 11.1 架构原则

本项目的产品核心是 **多 Agent 共同协作**，不是单模型聊天，也不是把几个阶段名称展示在界面上。

正常执行路径必须包含四个真实 Agent：

1. **PM Agent**：理解需求、拆解任务、判断执行模式；
2. **Coder Agent**：生成完整应用或局部补丁；
3. **Reviewer Agent**：独立审查产物、识别问题、决定修复策略并驱动补丁执行；
4. **Chat Host Agent**：组织自然语言反馈、说明各 Agent 的贡献并给出下一步建议。

`Patcher`、HTML 检查器、结构校验器是 **Reviewer Agent 可调用的工具**，不能反过来替代 Reviewer Agent 的独立判断。

### 11.2 共享任务状态

四个 Agent 通过统一任务状态协作，而不是互相拼接无结构文本。

推荐共享状态：

```json
{
  "task_id": "uuid",
  "mode": "FULL",
  "user_request": "我要一个带优先级和截止日的待办看板",
  "pm_plan": {},
  "coder_output": {},
  "review_result": {},
  "final_code": "",
  "final_report": "",
  "status": "reviewing"
}
```

每个 Agent 只写入自己负责的字段，Orchestrator 负责状态流转、超时控制和失败恢复。

### 11.3 PM Agent

职责：

- 判断任务是 `FULL` 还是 `PATCH`；
- 提炼功能需求；
- 识别用户是否要求推翻重做；
- 给 Coder 输出明确的任务清单和验收点；
- 给 Reviewer 输出可检查的验收标准。

输出建议：

```json
{
  "mode": "FULL",
  "requirements": ["任务列表", "优先级", "截止日期", "完成状态", "本地保存"],
  "acceptance_criteria": ["能够新增任务", "能够切换完成状态", "刷新后数据不丢"],
  "summary": "创建一个可交互待办看板"
}
```

### 11.4 Coder Agent

`FULL` 模式：

- 根据 PM 的任务清单输出完整单文件 HTML；
- 内嵌 CSS 和 JavaScript；
- 可使用 Tailwind CDN；
- 不允许输出省略号；
- 不允许依赖无法访问的私有资源。

`PATCH` 模式：

- 根据 PM 的修改目标输出局部补丁；
- 禁止整份重写；
- 每个补丁块必须包含足够唯一的上下文；
- 补丁格式：

```text
<<<<<<< SEARCH
原代码块
=======
替换后的代码块
>>>>>>> REPLACE
```

### 11.5 Reviewer Agent

Reviewer Agent 必须独立审查 Coder 的产物，而不是无条件信任上游输出。

职责：

1. 检查是否满足 PM 的验收标准；
2. 检查 HTML 结构是否完整；
3. 检查 JavaScript 是否存在明显语法错误；
4. 检查补丁锚点是否唯一、是否能原子合并；
5. 判断“接受、要求 Coder 修复、改为全量生成”；
6. 必要时生成修复补丁并要求 Patcher 执行；
7. 只有审查通过后，才允许写入新的 `app_versions`。

审查输出建议：

```json
{
  "passed": false,
  "issues": ["删除按钮缺少确认提示"],
  "action": "request_coder_fix",
  "repair_hint": "为删除按钮增加确认步骤"
}
```

### 11.6 Patcher 工具

Patcher 是 Reviewer Agent 的工具层，职责是确定性执行：

- 解析 `SEARCH/REPLACE` 补丁块；
- 原子合并补丁；
- 返回每个补丁块的匹配结果；
- 不自行猜测，不部分写入；
- 匹配失败时向 Reviewer Agent 返回结构化错误。

也就是说：

- **Reviewer Agent 决定怎么修**；
- **Patcher 只负责可靠执行**。

### 11.7 Chat Host Agent

职责：

- 汇总 PM 的拆解、Coder 的实现和 Reviewer 的验收结论；
- 流式说明本次新建或修改了哪些内容；
- 明确说明哪些 Agent 参与了协作；
- 给出两个后续优化建议；
- 在异常降级时向用户诚实说明当前状态。

### 11.8 正常调用策略

推荐顺序：

```text
用户输入
  → PM Agent
  → Coder Agent
  → Reviewer Agent
  → Patcher / 校验工具
  → 如需修复则回到 Coder Agent
  → Reviewer Agent 复核
  → Chat Host Agent 汇报
```

说明：

- PM、Coder、Reviewer、Chat Host 均应进行真实模型调用；
- Reviewer 调用的模型重点应偏向代码审查和结构化判断；
- 可通过角色化短提示、限制输出长度、设置超时来降低延迟；
- 不建议为了省事把 Reviewer 降级为普通字符串函数。

### 11.9 多 Agent 故障策略

单个 Agent 模型失败时，不应立即放弃整个协作链。

推荐顺序：

1. 当前 Agent 使用主模型重试；
2. 切换到备用模型重新执行当前 Agent 的职责；
3. 如果是结构化输出解析失败，带错误信息要求同一 Agent 修复输出；
4. 如果 Coder 失败，可由 Reviewer 根据 PM 计划生成一次修复建议，再让 Coder 重试；
5. 只有主备模型全部不可用，才进入明确标注的“演示模板模式”。

在无 Key 兜底模式下，系统可回放确定性的四阶段决策流，但必须明确标注：

> “当前为演示模板模式，远程多模型协作不可用。”

不得把模板输出伪装成真实多 Agent 生成结果。

### 11.10 研发侧多 Agent 分工

研发和集成过程也按多 Agent 协作拆分：

| 角色 | 负责范围 | 主要产物 |
|---|---|---|
| AI-1 后端与数据库工程师 | FastAPI、鉴权、SQLite、API、SSE、部署 | `main.py`、`database.py`、`security.py` |
| AI-2 多智能体引擎工程师 | 四 Agent 流水线、模型调用、Reviewer、Patcher、兜底 | `agents.py`、`templates.py` |
| AI-3 前端与沙盒工程师 | 登录、会话列表、聊天流、移动端、iframe 工具栏 | `static/index.html` |
| 集成工程师／主 Agent | 契约对齐、端到端联调、验收与修复 | README、测试记录、交付清单 |

三个研发 Agent 必须遵守统一接口契约；任何一方不得私自改变 SSE 事件、数据库字段或补丁格式。

---

## 12. 补丁合并算法

### 12.1 原子性要求

- 先解析全部补丁块；
- 任何一个块无法匹配，则整个补丁失败；
- 不允许部分成功、部分写入；
- 补丁不直接修改 `latest_code`；
- 新代码先写入 `app_versions`，成功后再更新会话。

### 12.2 容错规则

支持：

- 忽略行首尾空格差异；
- 忽略 Windows／Unix 换行差异；
- 对连续空行做有限容错；
- 多个补丁块按出现顺序一次性替换。

不支持：

- 模糊猜测补丁位置；
- 在无法定位时强行替换；
- 覆盖上一版本；
- 将失败补丁写入历史版本。

### 12.3 推荐函数

```python
def apply_patch(original_code: str, patch_text: str) -> tuple[bool, str]:
    """
    返回：
    - 是否全部补丁块成功匹配并替换
    - 替换后的完整代码
    """
```

### 12.4 失败处理顺序

```text
PATCH 失败
  → 保留旧代码
  → 使用更强锚点重试一次
  → 仍失败则 FULL 重新生成
  → FULL 失败则使用对应模板兜底
  → 全部失败则保留旧版本并向用户展示明确错误
```

---

## 13. 模型调用与降级策略

### 13.1 环境变量

```bash
KIMI_API_KEY=
KIMI_BASE_URL=https://api.moonshot.cn/v1
KIMI_MODEL=moonshot-v1-8k

BACKUP_API_KEY=
BACKUP_BASE_URL=
BACKUP_MODEL=

APP_ENV=production
PORT=8000
```

`.env.example` 只保留变量名，不保留真实 Key。

### 13.2 按 Agent 进行主备调用

建议统一封装模型能力，但由每个 Agent 以自己的角色调用：

```python
async def call_model(
    agent_name: str,
    messages: list[dict],
    purpose: str,
    response_schema: dict | None = None
) -> str:
    ...
```

行为：

1. PM、Coder、Reviewer、Chat Host 分别以自己的角色提示词调用模型；
2. 每个 Agent 优先使用 Kimi；
3. 当前 Agent 超时、限流或解析失败时，先针对该 Agent 重试；
4. 重试仍失败时，为该 Agent 切换到备用模型；
5. 某一 Agent 失败不应未经恢复就跳过整个协作链；
6. 只有主备模型都不可用，才进入明确标注的本地兜底；
7. 所有异常写入服务端日志，但不向前端泄露密钥。

这样可以保留多 Agent 独立决策，同时避免一个模型故障直接退化成单模型链路。

### 13.3 本地兜底模板

兜底模板必须真实可运行，至少包括：

- 待办看板；
- 个人主页；
- 房贷或通用计算器；
- 习惯追踪器；
- 简单倒计时器。

根据用户输入选择最相近模板，并在 UI 明确标注：

> “当前为演示模板模式，未调用远程模型。”

不得把兜底输出伪装成真实生成结果。

---

## 14. 上下文与成本控制

每次智能体请求传入：

1. 最近 4 轮对话；
2. 当前 `latest_code`；
3. 用户最新输入；
4. 当前任务模式；
5. 生成格式约束。

建议限制：

- 单条用户输入最大长度；
- 上下文总 Token 或字符上限；
- 单次请求最大执行时间；
- 单次响应最大长度；
- 登录和聊天接口限流。

当 `latest_code` 过长时，应优先保证用户最新修改目标明确，而不是无限制携带完整历史。

---

## 15. 前端设计

### 15.1 布局

#### 桌面端

- 左侧：Logo、新建应用、历史会话；
- 中间：对话流、输入框、模型状态；
- 右侧：iframe 实时预览与工具栏。

#### 移动端

- 顶部：品牌、新建、菜单；
- 中部：聊天和预览二选一；
- 底部：固定“对话／预览”切换；
- 预览页保留下载、刷新、新窗口入口。

### 15.2 必备交互状态

必须实现：

- 登录／自动注册；
- 加载历史会话；
- 空会话状态；
- 生成中状态；
- 阶段状态气泡；
- 流式文字；
- iframe 加载成功；
- iframe 加载失败；
- 输入为空提示；
- 网络错误提示；
- 模型降级提示；
- 恢复上一版成功／失败；
- 移动端切换状态。

不得出现无响应按钮。

### 15.3 iframe 约定

建议使用：

```html
<iframe
  sandbox="allow-scripts allow-forms allow-popups allow-modals"
  srcdoc="..."
></iframe>
```

说明：

- 不默认开放 `allow-same-origin`；
- 生成代码无法直接读写主页面；
- 下载源码使用当前 `latest_code`；
- 新窗口打开使用 Blob URL；
- 刷新沙盒仅重新渲染 iframe，不重发模型请求。

### 15.4 生成应用状态桥接

建议向生成应用注入一段轻量桥接脚本：

- 应用状态变化时发送 `postMessage`；
- 父页面收到后保存到 `app_states`；
- iframe 加载时读取服务端状态并注入；
- 未启用服务端状态时退回 iframe 内本地存储。

此功能属于增强项，但会显著提高“生成应用内部数据持久化”的验收表现。

---

## 16. 安全设计

### 16.1 密码

使用 Python 标准库：

- 随机盐；
- PBKDF2-HMAC-SHA256；
- 建议至少 100,000 次迭代；
- 数据库存储格式：

```text
pbkdf2$iterations$salt_hex$hash_hex
```

不再使用无盐 SHA-256。

### 16.2 Token

- 使用 `secrets.token_urlsafe()`；
- 设置过期时间；
- 可存入 SQLite；
- 支持后续扩展到刷新和注销。

### 16.3 接口保护

- 所有会话、聊天、版本和状态接口必须鉴权；
- 登录接口做基础频率限制；
- 输入长度限制；
- SQL 全部参数化；
- 不向前端返回堆栈和密钥；
- 生成代码在 iframe 沙盒中运行。

### 16.4 仓库安全

- `.env` 不提交；
- `.env.example` 不含真实值；
- `.db` 不提交；
- 缓存和日志不提交；
- README 不包含任何密钥；
- GitHub 仓库公开前执行一次密钥扫描。

---

## 17. 部署方案

### 17.1 部署环境要求

最终主机必须满足：

- 支持长期运行 HTTP 服务；
- 提供持久文件系统；
- 能保存 `app.db`；
- 支持环境变量；
- 支持 Python 运行环境或 Docker；
- 国内网络可直连；
- 可绑定域名或使用主机 HTTP 地址。

### 17.2 部署方式优先级

#### 优先级 A：带持久磁盘的云主机

适合最终笔试交付。优点是 SQLite 和预置数据稳定，部署方式直接。

#### 优先级 B：已有国内服务器

如果已有服务器和域名，优先考虑。没有域名时，可临时使用 `http://IP:端口` 验证。

#### 优先级 C：支持持久卷的平台

只要平台明确提供持久文件系统，也可以考虑。必须确认 SQLite 文件不会在重启或重新部署后丢失。

### 不优先

- GitHub Codespaces：适合开发，不适合作为最终交付链接；
- GitHub Pages：不能承载 FastAPI、SQLite 和 SSE；
- 无持久化文件系统的免费实例：SQLite 数据可能丢失；
- 本地隧道：只能作为备用，不能作为唯一交付方式。

### 17.3 Docker 启动要求

Dockerfile 至少应完成：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

实际版本可按最终实现微调。

### 17.4 部署顺序

1. 本地跑通；
2. 构建 Dockerfile；
3. 准备 `.env.example`；
4. 启动后访问 `/api/health`；
5. 部署最小占位版本；
6. 用无痕窗口访问；
7. 用手机流量访问；
8. 再增量更新功能；
9. 每次更新前保留上一版本镜像或代码 Tag。

---

## 18. 推荐实施计划

### 18.1 8 小时核心冲刺

| 时间 | 任务 | 完成标准 |
|---|---|---|
| 0:00—0:30 | 锁定范围、仓库和选题 | 明确只做 Prompt2App |
| 0:30—1:30 | FastAPI、SQLite、静态页面骨架 | 本地可启动 |
| 1:30—2:00 | 登录、Token、数据隔离 | 可创建用户并访问会话 |
| 2:00—2:30 | 部署占位版本 | 拿到公开链接 |
| 2:30—4:00 | 完整生成与 iframe 预览 | 输入后能看到可运行应用 |
| 4:00—5:00 | 会话、消息、代码持久化 | 刷新和重开后恢复 |
| 5:00—6:00 | 四 Agent 协作流水线 | PM、Coder、Reviewer、Host 真实协作且状态流完整 |
| 6:00—6:45 | 补丁修改与失败不覆盖旧版 | 可演示局部修改 |
| 6:45—7:15 | 双模型与无 Key 模板兜底 | 清 Key 仍能演示 |
| 7:15—8:00 | 预置项目、移动端、README | 满足提交前基础检查 |

### 18.2 三天使用方式

#### 第一天

- 完成 8 小时核心闭环；
- 解决部署和持久化；
- 不追求视觉细节。

#### 第二天

- 强化补丁、版本回滚、状态保存；
- 测试主备模型与无 Key 兜底；
- 完善手机端。

#### 第三天

- 只做验收加固；
- 跑无痕、手机流量、换设备测试；
- 整理 README、提交记录和交付说明；
- 不再加入新大功能。

---

## 19. 测试与验收

### 19.1 功能测试

- 新用户名自动注册；
- 已存在用户名正确登录；
- 错误密码返回 401；
- 新会话创建；
- 首句生成完整 HTML；
- PM、Coder、Reviewer、Chat Host 都产生真实协作事件；
- Reviewer 能驳回 Coder 并要求修复；
- iframe 能交互；
- 刷新后代码仍在；
- 刷新后多 Agent 协作历史仍能区分展示；
- 多轮修改后历史正确；
- 补丁成功更新局部；
- 补丁失败保留旧版；
- 恢复上一版成功；
- 下载源码内容正确；
- 新窗口打开可运行。

### 19.2 故障测试

- 清空 `KIMI_API_KEY`；
- 填入错误 Key；
- 主模型超时；
- 主模型 429；
- 备用模型不可用；
- 单个 Agent 输出无法解析；
- 演示模板模式明确标注；
- 数据库文件暂时不可写；
- 生成内容不完整；
- 补丁锚点无法匹配；
- 网络中断后恢复。

任何情况下都不能白屏，不能丢失上一版可运行代码。

### 19.3 环境测试

- 桌面 Chrome；
- 桌面 Safari 或 Edge；
- 手机浏览器；
- 微信内置浏览器（如目标用户会使用）；
- 无痕窗口；
- 手机流量；
- 另一台电脑；
- 清缓存后的冷启动。

### 19.4 仓库测试

- public 可访问；
- README 可完整阅读；
- 无 API Key；
- 无 `.env`；
- 无提交数据库文件；
- `.gitignore` 生效；
- 依赖能一次安装；
- 本地启动命令与实际一致。

---

## 20. README 必须包含

1. 项目一句话介绍；
2. 在线演示地址；
3. 核心功能；
4. 技术栈；
5. 目录结构；
6. 本地运行步骤；
7. Docker 运行步骤；
8. 环境变量说明；
9. 多 Agent 架构与各角色职责；
10. 模型主备与降级策略；
11. 已知限制；
12. 测试方式；
13. AI 工具使用说明；
14. 实现取舍说明；
15. 后续扩展方向。

---

## 21. 当前最重要的风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 最终主机没有持久文件系统 | 数据丢失，直接违反硬要求 | 部署前确认磁盘持久性 |
| 只有 GitHub，没有可用主机 | 无法稳定提供公开后端链接 | 尽早确定主机方案 |
| 没有模型 Key | 真实生成不可用 | 演示模板兜底，后续再补 Key |
| 补丁匹配失败 | 迭代功能卡死 | 原子补丁、重试、全量降级、版本回滚 |
| 生成代码运行失败 | 预览不可用 | iframe 错误检测、运行兜底、预置模板 |
| 手机端布局问题 | 影响页面美观与可用性 | 移动端单独验收，不做三栏硬塞 |
| 过度追求 Atoms 视觉 | 偏离笔试 | 官网复刻整体移出范围 |
| 多 Agent 退化成单模型聊天 | 丢失核心差异化，影响 Agent 维度评分 | 四个 Agent 正常路径均真实调用模型，工具只辅助执行 |
| 仓库泄露密钥 | 直接失去提交资格 | 环境变量、密钥扫描、公开前检查 |

---

## 22. 提交前最终 Checklist

### 在线链接

- [ ] 无痕窗口可打开；
- [ ] 手机流量可打开；
- [ ] 首次打开不空白；
- [ ] 预置项目可见；
- [ ] 生成流程完整；
- [ ] 刷新后数据仍在；
- [ ] 清 Key 后仍可演示；
- [ ] 补丁失败不覆盖旧版；
- [ ] 移动端无横向滚动。

### GitHub 仓库

- [ ] public；
- [ ] README 完整；
- [ ] 无 API Key；
- [ ] 无 `.env`；
- [ ] 无提交数据库文件；
- [ ] `.gitignore` 完整；
- [ ] 本地启动步骤可复现。

### 交付说明

- [ ] 说明做了什么；
- [ ] 说明没做什么；
- [ ] 说明关键取舍；
- [ ] 附上在线链接；
- [ ] 附上仓库地址；
- [ ] 附上自测结果。

---

## 23. 待用户确认的问题

以下问题仍需确认，但不应阻塞本地开发：

1. 实际可用工时按 8 小时冲刺，还是三天分阶段执行？
2. 最终是否允许新增一台带持久磁盘的云主机？
3. 是否已有 Kimi API Key 或备用模型 Key？
4. 首个演示选题是否确定为“待办看板生成器”？
5. 是否接受“真实模型优先、失败明确降级为演示模板”的策略？
6. 是否需要将生成应用内部状态也做到跨设备持久？
7. 是否需要绑定自有域名，还是允许临时使用云主机 HTTP 地址？

---

## 24. 最终建议

当前最稳妥的执行策略是：

1. **不做 Atoms 官网复刻；**
2. **严格围绕 Prompt2App 最小闭环开发；**
3. **第一小时内确定持久化主机方案；**
4. **第二小时前部署占位版本；**
5. **先生成、预览、持久化，再重点验证 PM、Coder、Reviewer、Chat Host 的真实协作；**
6. **所有失败路径都必须降级到可用版本；**
7. **剩余时间只做加固，不再扩范围。**

只要最终链接、持久化主机和基础模型问题得到落实，这套方案既保留“多 Agent 共同协作”的核心，又比“复刻 Atoms 页面＋简单聊天”更接近笔试通过标准，也更容易在三天内稳定交付。
