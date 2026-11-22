# Prompt2App — 一句话生成可运行的网页应用

借助 AI 多智能体协作，把一句自然语言需求变成一个「能打开、能点、能存数据」的单文件 HTML 应用：
PM 拆解路由 →（复杂应用先出设计规范）→ Coder 编写代码 → Reviewer 三层模糊补丁合并与确定性安检
→ Tester 静态走查 → Chat Host 播报，右侧沙盒即时渲染。

## 功能一览

- **灵感启动台**：首页和预置模板公开访问，无需注册或登录；只有主动提交登录表单时才会按需创建账号；登录用户可描述需求、生成并迭代自己的应用；内置模板只读展示，不开放对话迭代
- **多 Agent 工作台**：对话/报告与应用预览分为独立视图；支持桌面版与手机版预览；Agent 阶段步进器 + 实时字符心跳 + 一键停止，生成未完成时锁定继续迭代
- **增量迭代**：`SEARCH/REPLACE` 补丁局部修改，合并失败自动降级全量，全失败保留上一版（防破坏铁律）
- **版本回滚**：每次成功生成自动快照（FULL / PATCH / FALLBACK），一键恢复
- **应用状态云同步**：沙盒 localStorage 状态经 postMessage 桥同步服务端，登录用户跨设备数据仍在
- **注册登录合一**：PBKDF2 加盐哈希 + Token 落库；用户主动提交用户名和非空密码（例如 `demo` / `demo123`）时，若用户名不存在则自动注册；匿名访客可浏览首页与公共模板，生成接口仅接受已登录用户，公共项目修改自动 fork
- **四层容灾**：重试退避 → Kimi 主通道 → SiliconFlow 备用通道 → 离线模板兜底（标注【演示模式】），
  无 API Key / 断网 / 429 限流均不白屏

## 技术栈

- 后端：FastAPI + uvicorn（SSE 流式）、SQLite（WAL，标准库 sqlite3）、openai SDK（异步）
- 智能体：`agents_v2/` 包（网关 / 检查点 / 压缩器 / 补丁引擎 / 六原子 Agent / 编排），`agents.py` 为兼容薄壳
- 前端：React + Vite（深空主题），复用既有 FastAPI REST/SSE API；构建产物由 FastAPI 提供，后端仍为 Python
- 部署：任意支持持久化文件系统的主机，`PORT` 环境变量 + `0.0.0.0` 监听

## 本地启动

```bash
python -m venv .venv && source .venv/bin/activate   # Python 3.10+
pip install -r requirements.txt
cd frontend && npm ci && npm run build && cd ..

# 可选：配置模型 Key（不配也能跑，自动进入演示模式）
export KIMI_API_KEY=sk-...          # 主通道：Kimi 官方（兼容 MOONSHOT_API_KEY）
export BACKUP_API_KEY=sk-...        # 备用通道：硅基流动（默认 https://api.siliconflow.cn/v1）

python main.py                      # 或 uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
# 打开 http://127.0.0.1:8000
```

Node.js 20.19+ 或 22.12+ 用于本地前端依赖安装与构建；生产运行只需要 Python，因为构建产物是由 FastAPI 提供的静态文件。

开发时可先在一个终端运行 `python main.py`，再于另一个终端运行 `cd frontend && npm run dev`，并打开 `http://127.0.0.1:5173`。Vite 会将 `/api` 请求代理到 FastAPI。

## 测试与自检

```bash
python -m backend.agents --test               # 智能体层：离线六大检查点（无需 Key）
python -m backend.agents --live-test          # 智能体层：在线核验（直连主备模型 + 全局/PATCH 端到端）
python tests/api_smoke_test.py                # API 层：离线冒烟（种子/游客/fork/回滚/状态桥/越权隔离，无需 Key）
```

## 部署（国内云服务器）

```bash
docker build -t prompt2app .
docker run -d --name prompt2app -p 8000:8000 -v p2a_data:/app/data \
  -e KIMI_API_KEY=sk-... -e BACKUP_API_KEY=sk-... prompt2app
# 或不用 Docker：nohup uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
```

- 数据库文件默认 `./app.db`（可用 `APP_DB_PATH` 环境变量改到挂载卷），重启不丢数据
- 提交前检查：`.gitignore` 已排除 `.env` 与 `*.db`；仓库内无任何 API Key

## AI 协作与降级设计说明

- 模型名一律运行时经官方 `GET /v1/models` 动态发现（禁止硬编码），Kimi 与 SiliconFlow 参数分通道构造
  （`max_completion_tokens` vs `max_tokens`、K3 不传 temperature/top_p、低优任务不传 reasoning_effort）
- 前端「停止生成」经 AbortController 断开 SSE → 服务端取消协程 → 上游流式连接关闭，
  已生成代码已按 code 事件逐次落库，中断不丢成果
- 同步 sqlite 调用全部经 `asyncio.to_thread` 包装，不阻塞事件循环
- 生成代码的确定性防线：HTML 结构/缩水门禁 → JS 静态规则（禁用 alert/prompt/confirm）→ node --check 语法校验

## 目录结构

```
backend/           后端 Python 包
  main.py          FastAPI 入口：鉴权、REST、SSE、静态页托管
  database.py      SQLite 六表 + 种子注入 + 版本/状态 CRUD
  storage.py       会话文件存储
  seed_data.py     预置应用模板（广场种子 + L4 兜底关键词匹配）
  agents.py        智能体兼容薄壳（再导出 agents_v2 公共 API）
  agents_v2/       智能体包：gateway / checkpoints / compressor / sanitizer /
                   prompts / results / actors / pipeline / selftest / common
  legacy/          仅供参考的单文件旧 Agent 实现
tests/
  api_smoke_test.py API 层离线冒烟测试
main.py            兼容启动入口（转发到 backend.main）
static/home.html         旧版首页（React 构建产物缺失时的兼容回退）
static/dashboard.html    旧版项目页（React 构建产物缺失时的兼容回退）
static/index.html        旧版工作台（React 构建产物缺失时的兼容回退）
frontend/                React + Vite 前端源码
  src/App.jsx            首页、项目页、工作台和 SSE 客户端交互
  src/styles.css         深空主题与响应式布局
  dist/                  Vite 构建产物（由 FastAPI 提供，不提交）
```
