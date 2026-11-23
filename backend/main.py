"""Prompt2App 后端入口 v2（对齐 prompt2app.md / MASTER_SPEC 契约）。

运行: python main.py  （端口读取 PORT 环境变量，监听 0.0.0.0）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .database import HISTORY_ROUNDS, Database
from .storage import list_session_files, read_session_file, write_session_file

logger = logging.getLogger("prompt2app")
logging.basicConfig(level=logging.INFO)

# ==============================================================================
# 用户级生成互斥：同一用户一次只生成一个会话，不同用户可并行生成。
# ==============================================================================
import time as _time

_active_gen_by_user: dict[int, dict] = {}
_GEN_STALE_SECONDS = 15 * 60  # 进程异常残留时最长占用 15 分钟，之后自动放行


def _gen_status(user_id: int) -> dict:
    active = _active_gen_by_user.get(user_id)
    if active and _time.time() - active["started_at"] > _GEN_STALE_SECONDS:
        _gen_release(user_id, active["session_id"])
        active = None
    return {
        "busy": active is not None,
        "session_id": active["session_id"] if active else None,
        "title": active["title"] if active else "",
    }


def _gen_acquire(user_id: int, session_id: int, title: str) -> bool:
    """占用该用户的生成权；同一用户的重复生成请求会被拒绝。"""
    st = _gen_status(user_id)
    if st["busy"]:
        return False
    _active_gen_by_user[user_id] = {
        "session_id": session_id,
        "title": title,
        "started_at": _time.time(),
    }
    return True


def _gen_release(user_id: int, session_id: int) -> None:
    active = _active_gen_by_user.get(user_id)
    if active and active["session_id"] == session_id:
        _active_gen_by_user.pop(user_id, None)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
STATIC_INDEX = STATIC_DIR / "index.html"
STATIC_HOME = STATIC_DIR / "home.html"
STATIC_DASHBOARD = STATIC_DIR / "dashboard.html"
STATIC_APP = STATIC_INDEX
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"

db = Database()

app = FastAPI(title="Prompt2App", version="1.0.0")
app.mount(
    "/assets",
    StaticFiles(directory=str(FRONTEND_DIST / "assets"), check_dir=False),
    name="frontend-assets",
)


# ==============================================================================
# 请求/响应模型
# ==============================================================================
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str


class SessionSummary(BaseModel):
    id: int
    title: str
    updated_at: str


class ShowcaseItem(BaseModel):
    id: int
    title: str
    latest_code: str
    updated_at: str


class MessageItem(BaseModel):
    role: str
    agent: str | None = None
    content: str


class VersionItem(BaseModel):
    id: int
    mode: str
    note: str
    created_at: str


class SessionDetail(BaseModel):
    id: int
    title: str
    is_public: int
    messages: list[MessageItem]
    latest_code: str
    versions: list[VersionItem]


class ChatRequest(BaseModel):
    session_id: int | None = None
    message: str


class StateResponse(BaseModel):
    state: dict | None = None


class StatePutRequest(BaseModel):
    state: dict


class FileItem(BaseModel):
    name: str
    size: int
    updated_at: int


class FileListResponse(BaseModel):
    path: str
    files: list[FileItem]


class FileContentResponse(BaseModel):
    name: str
    content: str


# ==============================================================================
# 鉴权：Bearer Token（tokens 表落库）；可选鉴权用于广场回落
# ==============================================================================
def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    token = _extract_token(authorization)
    user = db.get_token_user(token) if token else None
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已失效")
    return user


def get_current_user_optional(authorization: str | None = Header(default=None)) -> dict | None:
    token = _extract_token(authorization)
    return db.get_token_user(token) if token else None


# ==============================================================================
# 基础接口
# ==============================================================================
def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": "1.0.0"}


@app.get("/api/generation/status")
def generation_status(user: dict | None = Depends(get_current_user_optional)) -> dict:
    """返回当前用户自己的生成状态，不暴露其他用户的生成信息。"""
    return _gen_status(int(user["id"])) if user else {"busy": False, "session_id": None, "title": ""}


@app.get("/")
def index():
    if FRONTEND_INDEX.is_file():
        return FileResponse(FRONTEND_INDEX)
    if STATIC_HOME.is_file():
        return FileResponse(STATIC_HOME)
    if STATIC_INDEX.is_file():
        return FileResponse(STATIC_INDEX)
    return HTMLResponse(
        "<!DOCTYPE html><html lang='zh-CN'><meta charset='utf-8'>"
        "<title>Prompt2App</title><body><p>后端已就绪。前端 static/index.html 尚未放入。</p></body></html>"
    )


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    if FRONTEND_INDEX.is_file():
        return FileResponse(FRONTEND_INDEX)
    if not STATIC_DASHBOARD.is_file():
        raise HTTPException(status_code=404, detail="项目广场页面不存在")
    return FileResponse(STATIC_DASHBOARD)


@app.get("/app", include_in_schema=False)
def app_workbench():
    if FRONTEND_INDEX.is_file():
        return FileResponse(FRONTEND_INDEX)
    if not STATIC_APP.is_file():
        raise HTTPException(status_code=404, detail="应用工作台页面不存在")
    return FileResponse(STATIC_APP)


@app.post("/api/login", response_model=LoginResponse)
def login(body: LoginRequest):
    user, error = db.authenticate(body.username, body.password)
    if error == "用户名和密码不能为空" or error == "用户名过长":
        raise HTTPException(status_code=400, detail=error)
    if user is None:
        raise HTTPException(status_code=401, detail=error or "密码错误")
    token = db.create_token(user["id"])
    return LoginResponse(token=token, username=str(user["username"]))


@app.get("/api/public/showcase", response_model=list[ShowcaseItem])
def public_showcase():
    """免登录公共广场：评委打开第一眼即有可交互应用（冷启动红线）。"""
    return db.list_public_showcase()


@app.get("/api/sessions", response_model=list[SessionSummary])
def list_sessions(user: dict | None = Depends(get_current_user_optional)):
    """有有效 Token 返回个人项目；游客回落到公共广场列表。"""
    if user is None:
        return [
            {"id": item["id"], "title": item["title"], "updated_at": item["updated_at"]}
            for item in db.list_public_showcase()
        ]
    return db.list_sessions(user["id"])


def _load_session_detail(user_id: int, session_id: int) -> dict:
    detail = db.get_session_detail(user_id, session_id)
    if detail is None:
        # 公共会话：任何用户可读（只读），私有会话保持 404 防探测
        row = db.get_session_row_any(session_id)
        if row is not None and int(row["is_public"]) == 1:
            return db.get_session_detail_any(session_id) or row
        raise HTTPException(status_code=404, detail="会话不存在")
    return detail


@app.get("/api/sessions/{session_id}", response_model=SessionDetail)
def get_session(session_id: int, user: dict | None = Depends(get_current_user_optional)):
    if user is not None:
        return _load_session_detail(user["id"], session_id)
    # 游客：仅公共会话可读（只读），私有会话 404 防探测
    row = db.get_session_row_any(session_id)
    if row is None or int(row["is_public"]) != 1:
        raise HTTPException(status_code=404, detail="会话不存在")
    return db.get_session_detail_any(session_id)


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: int, user: dict = Depends(get_current_user)):
    if not db.delete_session(user["id"], session_id):
        raise HTTPException(status_code=404, detail="会话不存在或无权删除")
    return {"ok": True}


@app.post("/api/sessions/{session_id}/versions/{version_id}/restore")
def restore_version(session_id: int, version_id: int, user: dict = Depends(get_current_user)):
    restored = db.restore_version(user["id"], session_id, version_id)
    if restored is None:
        raise HTTPException(status_code=404, detail="会话或版本不存在")
    _sync_session_file(user["id"], session_id)
    return {"ok": True, "session_id": restored["id"]}


# ==============================================================================
# 应用内部状态（沙盒同步桥）
# ==============================================================================
@app.get("/api/sessions/{session_id}/state", response_model=StateResponse)
def get_app_state(session_id: int, user: dict = Depends(get_current_user)):
    row = db.get_session_row(user["id"], session_id)
    if row is None:
        public = db.get_session_row_any(session_id)
        if public is None or int(public["is_public"]) != 1:
            raise HTTPException(status_code=404, detail="会话不存在")
        return StateResponse(state=None)  # 公共模板只读，无服务端状态
    raw = db.get_app_state(session_id)
    return StateResponse(state=json.loads(raw) if raw else None)


@app.put("/api/sessions/{session_id}/state")
def put_app_state(session_id: int, body: StatePutRequest, user: dict = Depends(get_current_user)):
    if db.get_session_row(user["id"], session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        db.save_app_state(session_id, json.dumps(body.state, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"状态不是合法 JSON: {exc}")
    return {"ok": True}


# ==============================================================================
# SSE 流式生成（仅登录用户；公共项目修改时自动 fork）
# ==============================================================================
def _session_title(message: str) -> str:
    title = " ".join(message.split())
    if len(title) > 20:
        return title[:20] + "…"
    return title or "未命名应用"


def _looks_like_html(code: str) -> bool:
    return "<html" in code.lower() and "</html>" in code.lower()


def _sync_session_file(user_id: int, session_id: int) -> None:
    """把当前 latest_code 同步为会话文件夹中的 index.html 产物文件。"""
    row = db.get_session_row_any(session_id)
    if row is None:
        return
    code = row.get("latest_code")
    if code and _looks_like_html(code):
        write_session_file(user_id, session_id, "index.html", code)


# ==============================================================================
# 会话文件（每个用户独立文件夹 / 每次会话一个子文件夹）
# ==============================================================================
def _resolve_file_access(user: dict | None, session_id: int) -> tuple[dict, int]:
    """返回 (会话行, 存储属主 user_id)；游客仅公共会话可读，私有 404。"""
    row = db.get_session_row_any(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if user is not None and int(row["user_id"]) == int(user["id"]):
        return row, int(row["user_id"])
    if int(row["is_public"]) == 1:
        return row, int(row["user_id"])
    raise HTTPException(status_code=404, detail="会话不存在")


@app.get("/api/sessions/{session_id}/files", response_model=FileListResponse)
def list_files(session_id: int, user: dict | None = Depends(get_current_user_optional)):
    row, owner_id = _resolve_file_access(user, session_id)
    files = list_session_files(owner_id, session_id)
    if not files and row.get("latest_code"):  # 老数据兜底：从 DB 补写产物文件
        write_session_file(owner_id, session_id, "index.html", row["latest_code"])
        files = list_session_files(owner_id, session_id)
    return FileListResponse(
        path=f"users/{owner_id}/sessions/{session_id}/",
        files=files,
    )


@app.get("/api/sessions/{session_id}/files/{file_name}", response_model=FileContentResponse)
def read_file(session_id: int, file_name: str, user: dict | None = Depends(get_current_user_optional)):
    _row, owner_id = _resolve_file_access(user, session_id)
    content = read_session_file(owner_id, session_id, file_name)
    if content is None:
        if file_name == "index.html":  # 老数据兜底
            _sync_session_file(owner_id, session_id)
            content = read_session_file(owner_id, session_id, file_name)
        if content is None:
            raise HTTPException(status_code=404, detail="文件不存在")
    return FileContentResponse(name=file_name, content=content)


@app.post("/api/chat")
async def chat(body: ChatRequest, user: dict = Depends(get_current_user)):
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")
    _request_t0 = _time.monotonic()

    # 同步 sqlite 调用一律经 to_thread 包装，防阻塞事件循环
    if body.session_id is None:
        session = await asyncio.to_thread(db.create_session, user["id"], _session_title(message))
    else:
        row = await asyncio.to_thread(db.get_session_row_any, body.session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if int(row["user_id"]) != int(user["id"]):
            if int(row["is_public"]) == 1:
                # 公共项目写保护：自动 fork 到当前用户名下，源模板不被篡改
                session = await asyncio.to_thread(db.fork_session, user["id"], row["id"])
            else:
                raise HTTPException(status_code=404, detail="会话不存在")
        else:
            session = row

    session_id = int(session["id"])
    user_id = int(user["id"])

    # 同一用户同时只允许一个生成；不同用户之间互不阻塞。
    if not _gen_acquire(user_id, session_id, str(session.get("title") or "")):
        busy = _gen_status(user_id)
        raise HTTPException(
            status_code=409,
            detail=f"「{busy['title'] or '另一个应用'}」正在生成中，请稍候再试",
        )
    try:
        history = await asyncio.to_thread(db.get_recent_history, user_id, session_id, HISTORY_ROUNDS)
        current_code = session.get("latest_code") or None
        await asyncio.to_thread(db.save_message, session_id, "user", message)
    except BaseException:
        _gen_release(user_id, session_id)
        raise

    queue: asyncio.Queue = asyncio.Queue()

    async def on_heartbeat(chars: int) -> None:
        await queue.put({"type": "status",
                         "content": f"⚡ Coder Agent 正在编写代码（已生成 {chars} 字符）..."})

    async def produce() -> None:
        try:
            from .agents import run_agent_pipeline  # agents.py 薄壳 -> agents_v2 包
            from .seed_data import match_seed_template

            pipeline = run_agent_pipeline(
                message,
                history,
                current_code,
                session_id=session_id,
                prompt_cache_key=f"p2a-session-{session_id}",
                on_heartbeat=on_heartbeat,
                template_lookup=match_seed_template,
            )
            async for event in pipeline:
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 流水线保险丝（agents 内部应已兜底）
            logger.exception("agent pipeline failed")
            await queue.put({"type": "status", "content": "流水线异常"})
            await queue.put({"type": "text", "content": f"生成过程出现异常：{exc}"})
        finally:
            await queue.put(None)

    async def event_stream():
        producer = asyncio.create_task(produce())
        assistant_parts: list[str] = []
        final_code = current_code
        assistant_saved = False
        try:
            yield _sse({"type": "session", "session_id": session_id, "title": session.get("title") or ""})
            while True:
                event = await queue.get()
                if event is None:
                    break
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type == "status":
                    content = "" if event.get("content") is None else str(event.get("content"))
                    await asyncio.to_thread(db.save_message, session_id, "status", content)
                    yield _sse({"type": "status", "content": content})
                elif event_type == "text":
                    text = "" if event.get("content") is None else str(event.get("content"))
                    if _looks_like_html(text) or re.search(r"```(?:html|xhtml)\b", text, re.IGNORECASE):
                        logger.warning(
                            "Code content arrived on the assistant text channel (session=%s); omitted",
                            session_id,
                        )
                        continue
                    assistant_parts.append(text)
                    yield _sse({"type": "text", "content": text})
                elif event_type == "code" and event.get("content"):
                    code = str(event["content"])
                    file_name = str(event.get("file") or "index.html")[:64]
                    if _looks_like_html(code):
                        if file_name == "index.html":
                            final_code = code
                            mode = str(event.get("mode") or "FULL")[:16]
                            note = str(event.get("note") or "")[:200]
                            await asyncio.to_thread(db.update_latest_code, session_id, code)
                            await asyncio.to_thread(db.record_version, session_id, code, mode, note)
                        await asyncio.to_thread(write_session_file, user_id, session_id,
                                                file_name, code)
                    yield _sse({"type": "code", "content": code, "file": file_name})
                # 忽略未知事件类型，保持向前兼容
            assistant_text = "".join(assistant_parts).strip()
            if assistant_text and not assistant_saved:
                await asyncio.to_thread(db.save_message, session_id, "assistant", assistant_text)
                assistant_saved = True
            yield _sse({"type": "done", "session_id": session_id})
        except asyncio.CancelledError:
            # 前端主动断开（停止生成）：保留已生成代码（code 事件已落库），补写已产出文本
            if not assistant_saved and assistant_parts:
                try:
                    await asyncio.to_thread(db.save_message, session_id, "assistant",
                                            "".join(assistant_parts).strip() or "（已中断）")
                except Exception:  # noqa: BLE001
                    logger.exception("failed to persist partial assistant message")
            producer.cancel()
            raise
        finally:
            if not producer.done():
                producer.cancel()
            _gen_release(user_id, session_id)
            logger.info("[timing] request session=%s total=%.1fs (含 SSE 全程)",
                        session_id, _time.monotonic() - _request_t0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    # 红线：监听 0.0.0.0，端口读 PORT 环境变量，严禁硬编码 localhost
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
