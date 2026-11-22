"""SQLite 记忆层 v2：六表 + 种子应用（版本同步） + 版本快照 + 应用状态同步。

表结构（对齐 prompt2app.md / MASTER_SPEC，外加一期纳入的 app_states）：
- users            用户（PBKDF2-HMAC-SHA256 加盐哈希，兼容旧 SHA-256 自动升级）
- tokens           登录态落库（进程重启不丢登录）
- sessions         会话及当前最新代码（user_id=0 + is_public=1 = 公共广场种子）
- messages         用户/助手/状态消息（agent 列标注来源 Agent）
- app_versions     每次成功生成的代码快照（支持一键回滚）
- app_states       生成应用内部状态（沙盒 localStorage 同步桥，每会话一份 upsert）

种子数据：启动时自动注入/同步 seed_data.SEED_APPS（冷启动红线「绝不空白」 +
种子版本更新 + 旧标题迁移），公共模板有 fork 写保护，同步不会覆盖用户数据。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .seed_data import SEED_APPS

# 一次「轮」= 一条用户消息及其后紧跟的助手回复。传给智能体时只保留最近 N 轮。
HISTORY_ROUNDS = 4

# PBKDF2 参数（对齐 SPEC：加盐哈希）
_PBKDF2_ITERATIONS = 120_000

_TOKEN_TTL_HOURS = 72

# 旧版种子标题 -> 新版（启动时自动迁移替换）
LEGACY_SEED_TITLES = {"极简程序员个人主页": "个人主页生成器"}


def hash_password(password: str, salt: bytes | None = None) -> str:
    """PBKDF2-HMAC-SHA256 加盐哈希，格式 pbkdf2$iterations$salt_hex$hash_hex。"""
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(stored: str, given: str) -> bool:
    """校验密码；兼容旧版裸 SHA-256（64 位十六进制），命中即视为通过（登录后由上层升级）。"""
    if stored.startswith("pbkdf2$"):
        try:
            _, iterations, salt_hex, hash_hex = stored.split("$")
            digest = hashlib.pbkdf2_hmac(
                "sha256", given.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
            )
            return hmac.compare_digest(digest.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    legacy = hashlib.sha256(given.encode("utf-8")).hexdigest()
    return len(stored) == 64 and hmac.compare_digest(stored, legacy)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _expires_at(hours: int = _TOKEN_TTL_HOURS) -> str:
    return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _seed_demo_apps_impl(conn: sqlite3.Connection) -> None:
    """冷启动红线 + 种子版本同步：
    1. 公共广场为空时注入全部种子（user_id=0, is_public=1）；
    2. 已存在的种子与 seed_data 当前版本不一致时自动更新（公共模板有 fork 写保护，同步安全）；
    3. 旧版标题自动改名替换（如「极简程序员个人主页」->「个人主页生成器」）。
    """
    conn.execute(
        "INSERT OR IGNORE INTO users (id, username, password_hash, created_at) VALUES (0, '__public__', '-', ?)",
        (_now(),),
    )
    rows = conn.execute(
        "SELECT id, title, latest_code FROM sessions WHERE is_public = 1"
    ).fetchall()
    by_title = {row["title"]: row for row in rows}
    now = _now()

    # 3. 旧标题迁移：改名并替换为新版内容
    for old_title, new_title in LEGACY_SEED_TITLES.items():
        old = by_title.pop(old_title, None)
        if old is None:
            continue
        new_html = dict(SEED_APPS).get(new_title)
        if new_html is None:
            continue
        if new_title in by_title:
            conn.execute("DELETE FROM sessions WHERE id = ?", (old["id"],))
        else:
            conn.execute(
                "UPDATE sessions SET title = ?, latest_code = ?, updated_at = ? WHERE id = ?",
                (new_title, new_html, now, old["id"]),
            )
            by_title[new_title] = old

    # 1. / 2. 注入或更新种子
    for title, html in SEED_APPS:
        existing = by_title.get(title)
        if existing is None:
            conn.execute(
                """
                INSERT INTO sessions (user_id, title, latest_code, is_public, created_at, updated_at)
                VALUES (0, ?, ?, 1, ?, ?)
                """,
                (title, html, now, now),
            )
        elif (existing["latest_code"] or "") != html:
            conn.execute(
                "UPDATE sessions SET latest_code = ?, updated_at = ? WHERE id = ?",
                (html, now, existing["id"]),
            )
    conn.commit()


class Database:
    """单连接 + 锁，保证多线程下的读写串行提交；async 端须经 asyncio.to_thread 调用。"""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = os.environ.get(
                "APP_DB_PATH",
                Path(__file__).resolve().parent.parent / "app.db",
            )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._init_schema()
            self._migrate_schema()
            _seed_demo_apps_impl(self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema 初始化与轻量迁移
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                latest_code TEXT,
                is_public INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'status')),
                agent TEXT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS app_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('FULL', 'PATCH', 'FALLBACK')),
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS app_states (
                session_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
                ON sessions(user_id, updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, id ASC);
            CREATE INDEX IF NOT EXISTS idx_versions_session
                ON app_versions(session_id, id ASC);
            CREATE INDEX IF NOT EXISTS idx_tokens_user
                ON tokens(user_id);
            """
        )
        self._conn.commit()

    def _table_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r["name"]) for r in rows}

    def _migrate_schema(self) -> None:
        """对旧版 app.db 做增量加列（幂等）。"""
        with self._lock:
            cols = self._table_columns("sessions")
            if "is_public" not in cols:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0")
            if "created_at" not in cols:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN created_at TEXT")
                self._conn.execute("UPDATE sessions SET created_at = updated_at WHERE created_at IS NULL")
            mcols = self._table_columns("messages")
            if "agent" not in mcols:
                self._conn.execute("ALTER TABLE messages ADD COLUMN agent TEXT")
            self._conn.commit()

    # ------------------------------------------------------------------
    # 用户与鉴权
    # ------------------------------------------------------------------
    def authenticate(self, username: str, password: str) -> tuple[dict | None, str | None]:
        """登录与自动注册合一；旧哈希登录成功后自动升级为 PBKDF2。"""
        username = username.strip()
        if not username or password == "":
            return None, "用户名和密码不能为空"
        if len(username) > 64:
            return None, "用户名过长"
        with self._lock:
            row = self._find_user(username)
            if row is None:
                digest = hash_password(password)
                try:
                    cur = self._conn.execute(
                        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                        (username, digest, _now()),
                    )
                    self._conn.commit()
                except sqlite3.IntegrityError:
                    self._conn.rollback()
                    row = self._find_user(username)
                else:
                    return {"id": int(cur.lastrowid), "username": username}, None
            if row is not None and _verify_password(row["password_hash"], password):
                if not row["password_hash"].startswith("pbkdf2$"):
                    self._conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (hash_password(password), row["id"]),
                    )
                    self._conn.commit()
                return {"id": int(row["id"]), "username": row["username"]}, None
            return None, "密码错误"

    def _find_user(self, username: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    def create_token(self, user_id: int, hours: int = _TOKEN_TTL_HOURS) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tokens (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token, user_id, _expires_at(hours), _now()),
            )
            self._conn.execute("DELETE FROM tokens WHERE expires_at < ?", (_now(),))
            self._conn.commit()
        return token

    def get_token_user(self, token: str) -> dict | None:
        """返回有效 token 对应的用户；过期即失效并清理。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT t.token, t.expires_at, u.id, u.username
                FROM tokens t JOIN users u ON u.id = t.user_id
                WHERE t.token = ?
                """,
                (token,),
            ).fetchone()
            if row is None:
                return None
            if str(row["expires_at"]) < _now():
                self._conn.execute("DELETE FROM tokens WHERE token = ?", (token,))
                self._conn.commit()
                return None
            return {"id": int(row["id"]), "username": row["username"]}

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def create_session(
        self, user_id: int, title: str, latest_code: str | None = None, is_public: int = 0
    ) -> dict:
        title = title.strip() or "未命名应用"
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO sessions (user_id, title, latest_code, is_public, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, title, latest_code, is_public, now, now),
            )
            self._conn.commit()
            session_id = int(cur.lastrowid)
        if latest_code:
            self.record_version(session_id, latest_code, "FULL", "初始版本")
        return self.get_session_row_any(session_id) or {
            "id": session_id, "user_id": user_id, "title": title,
            "latest_code": latest_code, "is_public": is_public, "updated_at": now,
        }

    def get_session_row(self, user_id: int, session_id: int) -> dict | None:
        """按属主取会话（私有会话鉴权用）。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, user_id, title, latest_code, is_public, created_at, updated_at
                FROM sessions WHERE id = ? AND user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
        return self._row_to_session(row)

    def get_session_row_any(self, session_id: int) -> dict | None:
        """不按属主过滤取会话（公共会话/内部 fork 用，权限判断由调用方负责）。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, user_id, title, latest_code, is_public, created_at, updated_at
                FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._row_to_session(row)

    @staticmethod
    def _row_to_session(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "title": row["title"],
            "latest_code": row["latest_code"],
            "is_public": int(row["is_public"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_sessions(self, user_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, title, updated_at FROM sessions
                WHERE user_id = ? ORDER BY updated_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
        return [
            {"id": int(row["id"]), "title": row["title"], "updated_at": row["updated_at"]}
            for row in rows
        ]

    def list_public_showcase(self) -> list[dict]:
        """公共广场（免登录）：满足「评委打开不能空白」。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, title, latest_code, updated_at FROM sessions
                WHERE is_public = 1 ORDER BY id ASC
                """
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "title": row["title"],
                "latest_code": row["latest_code"] or "",
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def _messages_and_versions(self, session_id: int) -> tuple[list[dict], list[dict]]:
        messages = self._conn.execute(
            """
            SELECT role, agent, content FROM messages
            WHERE session_id = ? ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
        versions = self._conn.execute(
            """
            SELECT id, mode, note, created_at FROM app_versions
            WHERE session_id = ? ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
        return (
            [{"role": m["role"], "agent": m["agent"], "content": m["content"]} for m in messages],
            [
                {
                    "id": int(v["id"]),
                    "mode": v["mode"],
                    "note": v["note"] or "",
                    "created_at": v["created_at"],
                }
                for v in versions
            ],
        )

    def get_session_detail(self, user_id: int, session_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, title, latest_code, is_public
                FROM sessions WHERE id = ? AND user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            if row is None:
                return None
            messages, versions = self._messages_and_versions(session_id)
        return {
            "id": int(row["id"]),
            "title": row["title"],
            "is_public": int(row["is_public"]),
            "messages": messages,
            "latest_code": row["latest_code"] or "",
            "versions": versions,
        }

    def get_session_detail_any(self, session_id: int) -> dict | None:
        """不按属主过滤的详情（公共会话只读访问用，权限判断由调用方负责）。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, title, latest_code, is_public
                FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            messages, versions = self._messages_and_versions(session_id)
        return {
            "id": int(row["id"]),
            "title": row["title"],
            "is_public": int(row["is_public"]),
            "messages": messages,
            "latest_code": row["latest_code"] or "",
            "versions": versions,
        }

    def fork_session(self, user_id: int, source_session_id: int, new_title: str | None = None) -> dict | None:
        """公共项目写保护：复制一份到用户名下（含首版本快照），源项目不被触碰。"""
        source = self.get_session_row_any(source_session_id)
        if source is None:
            return None
        title = (new_title or source["title"]).strip() or "未命名应用"
        return self.create_session(user_id, f"{title}（副本）", source["latest_code"], is_public=0)

    def delete_session(self, user_id: int, session_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE id = ? AND user_id = ? AND is_public = 0",
                (session_id, user_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 消息与历史
    # ------------------------------------------------------------------
    def get_recent_history(
        self,
        user_id: int,
        session_id: int,
        rounds: int = HISTORY_ROUNDS,
    ) -> list[dict]:
        """返回属于该用户的最近 `rounds` 轮对话，按时间正序。"""
        with self._lock:
            owner = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if owner is None:
                return []
            rows = self._conn.execute(
                """
                SELECT role, agent, content FROM messages
                WHERE session_id = ? AND role IN ('user', 'assistant')
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return _last_rounds(
            [{"role": row["role"], "content": row["content"]} for row in rows],
            rounds,
        )

    def save_message(self, session_id: int, role: str, content: str,
                     agent: str | None = None, created_at: str | None = None) -> None:
        if role not in ("user", "assistant", "status"):
            raise ValueError(f"非法消息角色: {role}")
        now = created_at or _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO messages (session_id, role, agent, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, role, agent, content, now),
            )
            self._conn.commit()

    def save_turn(
        self,
        user_id: int,
        session_id: int,
        user_content: str,
        assistant_content: str,
        latest_code: str | None,
    ) -> None:
        """在同一事务中写入本轮用户消息、助手回复，并更新最新代码。"""
        now = _now()
        with self._lock:
            owner = self._conn.execute(
                "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if owner is None:
                raise LookupError("会话不存在")
            try:
                self._insert_message(session_id, "user", user_content, now)
                self._insert_message(session_id, "assistant", assistant_content, now)
                self._conn.execute(
                    "UPDATE sessions SET latest_code = ?, updated_at = ? WHERE id = ?",
                    (latest_code, now, session_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _insert_message(self, session_id: int, role: str, content: str, created_at: str) -> None:
        self._conn.execute(
            """
            INSERT INTO messages (session_id, role, agent, content, created_at)
            VALUES (?, ?, NULL, ?, ?)
            """,
            (session_id, role, content, created_at),
        )

    # ------------------------------------------------------------------
    # 版本快照与回滚
    # ------------------------------------------------------------------
    def record_version(self, session_id: int, code: str, mode: str, note: str | None = None) -> int:
        if mode not in ("FULL", "PATCH", "FALLBACK"):
            raise ValueError(f"非法版本模式: {mode}")
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO app_versions (session_id, code, mode, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, code, mode, (note or "").strip() or None, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def restore_version(self, user_id: int, session_id: int, version_id: int) -> dict | None:
        """版本回滚：把指定快照写回 latest_code，并追加一条「回滚自 #id」新快照。"""
        with self._lock:
            owner = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if owner is None:
                return None
            version = self._conn.execute(
                "SELECT id, code, mode FROM app_versions WHERE id = ? AND session_id = ?",
                (version_id, session_id),
            ).fetchone()
            if version is None:
                return None
            now = _now()
            try:
                self._conn.execute(
                    "UPDATE sessions SET latest_code = ?, updated_at = ? WHERE id = ?",
                    (version["code"], now, session_id),
                )
                self._conn.execute(
                    """
                    INSERT INTO app_versions (session_id, code, mode, note, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, version["code"], version["mode"], f"回滚自版本 #{version_id}", now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_session_row(user_id, session_id)

    def update_latest_code(self, session_id: int, code: str) -> None:
        """仅更新会话最新代码与刷新时间（流式 code 事件到达时调用，不触碰消息表）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET latest_code = ?, updated_at = ? WHERE id = ?",
                (code, _now(), session_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # 应用内部状态（沙盒同步桥）
    # ------------------------------------------------------------------
    def save_app_state(self, session_id: int, state_json: str) -> None:
        """state_json 为 JSON 字符串（{localStorage_key: value} 快照），每会话一份 upsert。"""
        parsed = json.dumps(json.loads(state_json), ensure_ascii=False)  # 校验合法 JSON
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO app_states (session_id, state, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at
                """,
                (session_id, parsed, now),
            )
            self._conn.commit()

    def get_app_state(self, session_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM app_states WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row["state"] if row else None


def _last_rounds(messages: list[dict], rounds: int) -> list[dict]:
    if rounds <= 0 or not messages:
        return []
    grouped: list[list[dict]] = []
    current: list[dict] = []
    for message in messages:
        if message["role"] == "user" and current:
            grouped.append(current)
            current = []
        current.append(message)
    if current:
        grouped.append(current)
    selected = grouped[-rounds:]
    return [item for group in selected for item in group]
