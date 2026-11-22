"""会话文件存储层：每个用户独立文件夹，每次会话一个子文件夹。

布局：
    .data/users/{user_id}/sessions/{session_id}/index.html
    .data/users/{user_id}/sessions/{session_id}/meta.json

生成结果（成功 code 事件）写入 index.html；数据库仍是代码的权威来源，
本层为产物视图（前端"文件"页签展示/下载）与部署扩展点（未来多文件应用）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("P2A_DATA_DIR", BASE_DIR / ".data"))

_FNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if not _FNAME_RE.match(name) or ".." in name:
        raise ValueError(f"非法文件名: {name!r}")
    return name


def session_dir(user_id: int, session_id: int) -> Path:
    d = DATA_ROOT / "users" / str(int(user_id)) / "sessions" / str(int(session_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_session_file(user_id: int, session_id: int, name: str, content: str) -> dict:
    """原子写入会话文件（临时文件 + os.replace），返回文件元信息。"""
    name = _safe_name(name)
    d = session_dir(user_id, session_id)
    target = d / name
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, target)
    st = target.stat()
    return {"name": name, "size": int(st.st_size), "updated_at": int(st.st_mtime)}


def list_session_files(user_id: int, session_id: int) -> list[dict]:
    d = DATA_ROOT / "users" / str(int(user_id)) / "sessions" / str(int(session_id))
    if not d.is_dir():
        return []
    files = []
    for p in sorted(d.iterdir()):
        if p.is_file() and not p.name.endswith(".tmp") and _FNAME_RE.match(p.name):
            st = p.stat()
            files.append({"name": p.name, "size": int(st.st_size), "updated_at": int(st.st_mtime)})
    return files


def read_session_file(user_id: int, session_id: int, name: str) -> str | None:
    name = _safe_name(name)
    p = DATA_ROOT / "users" / str(int(user_id)) / "sessions" / str(int(session_id)) / name
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8")


def write_session_meta(user_id: int, session_id: int, meta: dict) -> None:
    d = session_dir(user_id, session_id)
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / "meta.json")


def fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def now_ts() -> int:
    return int(time.time())
