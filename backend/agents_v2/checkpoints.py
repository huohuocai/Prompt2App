"""检查点数据模型、原子持久化仓库与会话级并发隔离。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from .common import CACHE_DIR, logger

CHECKPOINT_DIR = CACHE_DIR / "checkpoints"
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

StepName = Literal["init", "pm_done", "coder_done", "reviewer_done", "completed"]


@dataclass
class WorkflowCheckpoint:
    """跨进程断点续跑检查点：每个阶段边界原子落盘。"""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: int | None = None
    current_step: StepName = "init"

    user_msg: str = ""
    compressed_history: str = ""
    base_code: str | None = None

    route_mode: Literal["FULL", "PATCH"] = "FULL"
    pm: dict = field(default_factory=dict)          # PMResult.to_dict()
    dev_plan: str = ""
    design_spec: str = ""                            # FULL 复杂应用的预生成设计规范

    raw_coder_output: str = ""
    coder_provider: str = ""
    is_demo_mode: bool = False

    final_code: str | None = None
    code_updated: bool = False
    review_notes: str = ""
    tester_notes: str = ""                           # Tester Agent 高风险问题（若有）

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class CheckpointStore:
    """原子 JSON 落盘（临时文件 + os.replace），损坏文件自动隔离，绝不抛异常。"""

    @staticmethod
    def _path(run_id: str) -> Path:
        return CHECKPOINT_DIR / f"{run_id}.json"

    @classmethod
    def save(cls, cp: WorkflowCheckpoint) -> None:
        try:
            cp.updated_at = time.time()
            target = cls._path(cp.run_id)
            fd, tmp = tempfile.mkstemp(dir=CHECKPOINT_DIR, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(cp), fh, ensure_ascii=False)
            os.replace(tmp, target)
        except Exception as exc:  # noqa: BLE001
            logger.warning("检查点落盘失败(run_id=%s): %s", cp.run_id, exc)

    @classmethod
    def load(cls, run_id: str) -> WorkflowCheckpoint | None:
        try:
            raw = cls._path(run_id).read_text(encoding="utf-8")
            data = json.loads(raw)
            known = {f.name for f in WorkflowCheckpoint.__dataclass_fields__.values()}  # type: ignore[attr-defined]
            return WorkflowCheckpoint(**{k: v for k, v in data.items() if k in known})
        except Exception as exc:  # noqa: BLE001
            logger.warning("检查点读取失败(run_id=%s): %s", run_id, exc)
            return None

    @classmethod
    def cleanup(cls, max_age_hours: float = 24.0) -> int:
        """清理过期检查点，返回清理数量。"""
        removed = 0
        try:
            cutoff = time.time() - max_age_hours * 3600
            for p in CHECKPOINT_DIR.glob("*.json"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                        removed += 1
                except OSError:
                    continue
        except Exception:  # noqa: BLE001
            pass
        return removed


# ==============================================================================
# 会话级并发隔离：全局 LLM 信号量 + 按 session_id 互斥锁
# ==============================================================================
from .common import MAX_CONCURRENT_LLM  # noqa: E402

_LLM_SEMAPHORE: asyncio.Semaphore | None = None
_SESSION_LOCKS: dict[int, asyncio.Lock] = {}


def _get_llm_semaphore() -> asyncio.Semaphore:
    """延迟绑定当前事件循环的全局上游并发信号量，防止跨事件循环报错。"""
    global _LLM_SEMAPHORE
    if _LLM_SEMAPHORE is None:
        _LLM_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_LLM)
    return _LLM_SEMAPHORE


def _get_session_lock(session_id: int | None) -> asyncio.Lock:
    """按 session_id 细粒度互斥：不同会话完全并行，同会话连点排队防写花。"""
    if session_id is None:
        return asyncio.Lock()
    lock = _SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[session_id] = lock
    return lock
