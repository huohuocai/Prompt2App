"""agents_v2 — Prompt2App 智能体流水线融合版（包）。

对 main.py 的契约（与原 agents.py 完全兼容）：
- 一键直通：``async for event in run_agent_pipeline(user_msg, history, current_code)``
- 两阶段：``run_pm_phase(...)`` -> ``run_build_phase(pm, ...)``
- 独立 Agent：pm_agent / design_spec_agent / coder_agent / reviewer_agent / tester_agent / chat_host_agent
- 检查点：``on_checkpoint(stage, result)`` + ``CheckpointAbort``；断点续跑：``resume_run_id``
- 工具：suggest_title / llm_available / usage_status_text / CheckpointStore
"""

from .actors import (
    chat_host_agent, coder_agent, design_spec_agent, pm_agent,
    reviewer_agent, suggest_title, tester_agent,
)
from .checkpoints import CheckpointStore, WorkflowCheckpoint, _get_llm_semaphore, _get_session_lock
from .common import CheckpointAbort, LLMUnavailableError
from .compressor import ContextCompressor
from .gateway import (
    KIMI_MODEL_PREFERENCE, SILICONFLOW_MODEL_PREFERENCE,
    ResilientLLMGateway, llm_available, reset_usage, usage_status_text,
)
from .pipeline import (
    resolve_l4_fallback, run_agent_pipeline, run_build_phase, run_pm_phase,
)
from .results import CoderResult, PMResult, ReviewResult
from .sanitizer import PatchAndSanitizerEngine

__all__ = [
    # 工作流
    "run_agent_pipeline", "run_pm_phase", "run_build_phase",
    # 独立 Agent
    "pm_agent", "design_spec_agent", "coder_agent", "reviewer_agent", "tester_agent", "chat_host_agent",
    # 结构化产物与检查点
    "PMResult", "CoderResult", "ReviewResult",
    "WorkflowCheckpoint", "CheckpointStore", "CheckpointAbort",
    # 引擎
    "PatchAndSanitizerEngine", "ContextCompressor", "ResilientLLMGateway",
    "resolve_l4_fallback",
    # 工具
    "suggest_title", "llm_available", "usage_status_text", "reset_usage",
    "LLMUnavailableError",
    "KIMI_MODEL_PREFERENCE", "SILICONFLOW_MODEL_PREFERENCE",
]
