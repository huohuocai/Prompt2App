"""递归增量上下文压缩器（HTML 折叠 + 冷热分层 + 会话级持久化摘要）。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .common import CACHE_DIR, logger

MEMORY_DIR = CACHE_DIR / "memory"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)


class ContextCompressor:
    """三级省 Token 引擎：
    1. 历史 HTML 折叠为占位符（当前版本由 base_code 单独提供，不回传旧代码）；
    2. 冷热分层：保留最近 3 轮（6 条）原文作为热窗口；
    3. 更早的冷历史用低推理强度递归浓缩，按 session 持久化，跨轮复用。
    """

    HOT_WINDOW_MESSAGES = 6

    @staticmethod
    def _memory_path(session_id: int) -> Path:
        return MEMORY_DIR / f"session_{int(session_id)}.json"

    @staticmethod
    def _fold_html_in_text(text: str) -> str:
        if not text:
            return ""
        folded = re.sub(
            r"```(?:html)?\s*<!DOCTYPE html>[\s\S]*?</html>\s*```",
            "[已生成 HTML 应用代码快照]", text, flags=re.IGNORECASE,
        )
        folded = re.sub(r"<!DOCTYPE html>[\s\S]*?</html>", "[已生成 HTML 应用代码快照]", folded, flags=re.IGNORECASE)
        return folded

    @classmethod
    def _load_snapshot(cls, session_id: int | None) -> dict[str, Any]:
        if session_id is None:
            return {}
        try:
            p = cls._memory_path(session_id)
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {}

    @classmethod
    def _save_snapshot(cls, session_id: int | None, snapshot: dict[str, Any]) -> None:
        if session_id is None:
            return
        try:
            p = cls._memory_path(session_id)
            fd, tmp = tempfile.mkstemp(dir=MEMORY_DIR, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话摘要落盘失败(session=%s): %s", session_id, exc)

    @classmethod
    async def compress(cls, messages: list[dict[str, str]], session_id: int | None = None) -> str:
        if not messages:
            return ""
        msgs = [{"role": m.get("role", "user"), "content": str(m.get("content", ""))} for m in messages]

        def _fold_pair(items: list[dict[str, str]]) -> list[dict[str, str]]:
            folded: list[dict[str, str]] = []
            for m in items:
                content = m["content"]
                if m["role"] == "assistant":
                    content = cls._fold_html_in_text(content)
                folded.append({"role": m["role"], "content": content})
            return folded

        hot = _fold_pair(msgs[-cls.HOT_WINDOW_MESSAGES:])
        cold = _fold_pair(msgs[: -cls.HOT_WINDOW_MESSAGES])

        parts: list[str] = []
        if cold:
            cold_text = "\n".join(f"{m['role']}: {m['content']}" for m in cold)
            snapshot = cls._load_snapshot(session_id)
            prev_summary = str(snapshot.get("summary", "")).strip()
            prompt = (
                "请把以下较早的多轮对话压缩成一段不超过 120 字的中文摘要，保留：用户的根本目标、"
                "已确认的功能决策、待办/偏好。若提供了上一版摘要，请在其基础上增量更新而非推倒重来。\n\n"
                f"【上一版摘要】\n{prev_summary or '（无）'}\n\n【更早的对话】\n{cold_text}"
            )
            summary = ""
            try:
                from .gateway import _GATEWAY

                summary = await _GATEWAY.call_llm(
                    [{"role": "user", "content": prompt}],
                    max_completion_tokens=512, reasoning_effort="low",
                )
                summary = summary.strip()
            except Exception as exc:  # noqa: BLE001
                logger.info("冷历史摘要失败，退化为截断原文: %s", exc)
            if summary:
                snapshot["summary"] = summary
                snapshot["updated_at"] = time.time()
                cls._save_snapshot(session_id, snapshot)
                parts.append(f"[早期对话摘要] {summary}")
            else:
                parts.append(f"[早期对话节选] {cold_text[:800]}")
        parts.extend(f"{m['role']}: {m['content']}" for m in hot)
        return "\n\n".join(parts)
