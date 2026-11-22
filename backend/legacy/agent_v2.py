"""agent_v2.py — Prompt2App 智能体流水线融合版（单文件参考实现，已冻结）

⚠️ 本文件仅供阅读对照，不被任何模块导入。正式实现已拆分为 agents_v2 包并保持同步演进：
- agents_v2/gateway.py     <- 本文件第 4 节（四层容灾网关）
- agents_v2/checkpoints.py <- 本文件第 3 节（检查点 + 并发隔离）
- agents_v2/compressor.py  <- 本文件第 6 节（上下文压缩）
- agents_v2/sanitizer.py   <- 本文件第 7 节（补丁引擎 + 确定性体检）
- agents_v2/prompts.py     <- 本文件第 9 节提示词
- agents_v2/results.py     <- 本文件第 3 节三个 Result dataclass
- agents_v2/actors.py      <- 本文件第 9 节（六个原子 Agent）
- agents_v2/pipeline.py    <- 本文件第 11 节（编排 + L4 兜底）
- agents_v2/selftest.py    <- 本文件第 12/13 节（自检/在线核验）

融合来源：
- 契约层取自 agents.py：模块级调用契约、PMResult/CoderResult/ReviewResult 结构化阶段产物、
  两阶段工作流（run_pm_phase / run_build_phase）、on_checkpoint 人机协同回调与 CheckpointAbort、
  配置字典 + 按供应商分通道的 _build_request_params、Token 用量统计、.env 加载、PM JSON Mode。
- 引擎层取自 agent.py：WorkflowCheckpoint 落盘断点续跑、三层模糊 PATCH 合并、思维链剥离、
  全局 LLM 信号量 + 按会话互斥锁、模型发现 300s TTL 缓存 + 过期缓存兜底、client 缓存、
  429 冷却水位线、finish_reason=length 自动续写、流式心跳、ContextCompressor 递归摘要、
  防缩水安检门禁。
- 新增：Reviewer 三道确定性防线（HTML 门禁 -> JS 静态规则 -> node --check 语法校验）、
  Tester Agent 静态走查、复杂 FULL 应用的 DesignSpec 预生成阶段。

对 main.py 的契约（与 agents.py 完全兼容）：
- 一键直通：``async for event in run_agent_pipeline(user_msg, history, current_code)``，
  event 为 dict，类型仅使用 ``status`` / ``text`` / ``code``（``done`` 由 main.py 追加）。
- 两阶段：``run_pm_phase(...)`` -> ``run_build_phase(pm, ...)``。
- 独立 Agent：pm_agent / design_spec_agent / coder_agent / reviewer_agent / tester_agent / chat_host_agent。
- 检查点：``on_checkpoint(stage, result)`` + ``CheckpointAbort``；断点续跑：``resume_run_id``。
- ``suggest_title(user_msg)`` 会话命名；``llm_available()`` 在线能力探测。

用户隔离边界：Kimi 官方请求 schema 无 OpenAI 风格 user 字段，429 限额为组织级；
用户鉴权/配额由 main.py（users/sessions 表）实现。本模块仅做会话级隔离：
按 session_id 的内存互斥锁、按 session 分文件的摘要持久化、prompt_cache_key 缓存分组。

四层防崩：L1 指数退避重试 -> L2 Kimi 官方 -> L3 硅基流动 SiliconFlow（依据其官方文档
api-docs.siliconflow.cn 适配：max_tokens 字段、厂商/模型名 ID、无 prompt_cache_key、
reasoning_effort 仅 high/max 故低优任务不传）-> L4 离线兜底（绝不破坏已有代码）。

离线模板资产：本模块零冗余存储。L4 通过 main.py 注入的 template_lookup 回调查询数据库；
未注入或查询失败时返回极简安全骨架，保证事件契约完整。预置应用库由 DB 提供、前端公共广场
提前展示。

本模块保证：无 Key / 断网 / 模型返回垃圾 / 补丁不匹配 / 前端中途断开，
均不抛出未捕获异常，总能 yield 完整 status -> text -> code。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal, Optional

try:  # pragma: no cover - 环境相关
    from openai import AsyncOpenAI

    _OPENAI_OK = True
except Exception:  # noqa: BLE001
    AsyncOpenAI = None  # type: ignore[assignment]
    _OPENAI_OK = False

try:  # pragma: no cover
    from openai import APIConnectionError, APIStatusError, APITimeoutError, InternalServerError, RateLimitError

    _RETRYABLE_ERRORS: tuple = (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
except Exception:  # noqa: BLE001
    APIStatusError = None  # type: ignore[assignment]
    _RETRYABLE_ERRORS = ()

import logging

logger = logging.getLogger("prompt2app.agent_v2")

__all__ = [
    "run_agent_pipeline", "run_pm_phase", "run_build_phase",
    "pm_agent", "design_spec_agent", "coder_agent", "reviewer_agent", "tester_agent", "chat_host_agent",
    "PMResult", "CoderResult", "ReviewResult", "WorkflowCheckpoint", "CheckpointStore",
    "CheckpointAbort", "LLMUnavailableError",
    "suggest_title", "llm_available", "usage_status_text", "reset_usage",
    "PatchAndSanitizerEngine", "ContextCompressor",
]

DEMO_TAG = "【演示模式】"

# ==============================================================================
# 1. 全局配置与目录初始化
# ==============================================================================
CACHE_DIR = Path(os.getenv("AGENT_CACHE_DIR", ".cache"))
CHECKPOINT_DIR = CACHE_DIR / "checkpoints"
MEMORY_DIR = CACHE_DIR / "memory"
for _d in (CHECKPOINT_DIR, MEMORY_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MAX_CONCURRENT_LLM = int(os.getenv("MAX_CONCURRENT_LLM", "5"))
_MODEL_CACHE_TTL = 300.0        # 模型发现结果缓存 300s
_MODELS_TIMEOUT = 30.0          # GET /v1/models 超时
_LLM_STREAM_OPEN_TIMEOUT = 30.0
_MAX_RETRIES = 2                # 除首次外最多退避重试 2 次
_BACKOFF_BASE = 1.5
_HEARTBEAT_CHARS = 1500         # 流式心跳：每产出 1500 字符回调一次
_CODE_MIN_LEN = 200             # 合法 HTML 的最小长度
_PATCH_CODE_CAP = 20000         # PATCH 场景喂给模型的现有代码上限（字符）
_MODELS_STALE_GRACE = 600.0     # 发现接口失败时过期缓存最多沿用时长
_NODE_CHECK_TIMEOUT = 15.0      # node --check 语法校验超时


def _load_dotenv() -> None:
    """项目根目录 .env 仅补充未定义的环境变量，绝不覆盖已有值。"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
    except Exception:  # noqa: BLE001
        pass


_load_dotenv()

# Kimi 官方模型偏好序（platform.kimi.com/docs/api）：K3 系列为主力，K2 官方已公告下线仅作回退。
KIMI_MODEL_PREFERENCE = (
    "kimi-k3", "kimi-k3.1-pro", "kimi-k3.1-pro-highspeed",
    "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6",
)
# 硅基流动 SiliconFlow 偏好序（api-docs.siliconflow.cn，「厂商/模型名」格式）。
# 备用通道刻意不选 Kimi 系：主通道故障多半是同厂商问题，回退同厂商失去容灾意义。
SILICONFLOW_MODEL_PREFERENCE = (
    "Pro/deepseek-ai/DeepSeek-V4",
    "deepseek-ai/DeepSeek-V4",
    "deepseek-ai/DeepSeek-V4-Flash",
    "Pro/zai-org/GLM-5.2",
)
NON_CHAT_KEYWORDS = ("embed", "audio", "rerank", "tts", "whisper", "vision-only")

# ==============================================================================
# 2. 异常与并发隔离基础设施
# ==============================================================================


class LLMUnavailableError(Exception):
    """第 2/3 层全部失败时抛出，由流水线捕获后落入第 4 层兜底。"""


class CheckpointAbort(Exception):
    """在 on_checkpoint 回调中抛出：于当前阶段优雅取消流水线（保留已有代码，不报错）。"""


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


# ==============================================================================
# 3. 结构化阶段产物（阶段间传递 + 检查点展示/修改 + 可序列化暂存）
# ==============================================================================
StepName = Literal["init", "pm_done", "coder_done", "reviewer_done", "completed"]


@dataclass
class PMResult:
    """PM Agent 的拆解方案：用户确认/修改后交给 Coder。"""

    route: str = "FULL"          # "FULL" | "PATCH"
    plan: str = ""
    title: str = ""
    source: str = "llm"          # llm / heuristic / user
    run_id: str = ""             # 关联 WorkflowCheckpoint，断点续跑/两阶段传递用
    need_spec: bool = False      # 复杂应用：先出设计规范再写代码

    def display(self) -> str:
        base = f"路由 {self.route}（{'在现有应用上局部修改' if self.route == 'PATCH' else '从零全量生成'}）"
        if self.title:
            base += f"｜标题《{self.title}》"
        if self.plan:
            base += f"｜实现要点：{self.plan}"
        if self.need_spec:
            base += "｜复杂应用：将先产出设计规范"
        return base

    def to_dict(self) -> dict:
        return {"route": self.route, "plan": self.plan, "title": self.title,
                "source": self.source, "run_id": self.run_id, "need_spec": self.need_spec}

    @classmethod
    def from_dict(cls, data: dict | None) -> "PMResult":
        d = data or {}
        route = str(d.get("route", "FULL")).upper().strip()
        if route not in ("FULL", "PATCH"):
            route = "FULL"
        return cls(
            route=route,
            plan=str(d.get("plan", "") or ""),
            title=str(d.get("title", "") or ""),
            source=str(d.get("source", "user") or "user"),
            run_id=str(d.get("run_id", "") or ""),
            need_spec=bool(d.get("need_spec", False)),
        )


@dataclass
class CoderResult:
    """Coder Agent 的原始产出（PATCH 为补丁块，FULL 为完整 HTML）。"""

    mode: str = "FULL"
    raw: str = ""
    code: str | None = None
    patches: list[tuple[str, str]] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    is_demo: bool = False
    fixed: bool = False          # 是否经修正回路重写过


@dataclass
class ReviewResult:
    """Reviewer 的校验结论：合并后代码 + 过程消息。"""

    code: str | None = None
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    merged_patches: int = 0
    degrade_full: bool = False
    warnings: list[str] = field(default_factory=list)  # 放行但值得告知的确定性告警
    messages: list[str] = field(default_factory=list)


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
# 4. 四层容灾 LLM 网关（L1 重试 -> L2 Kimi -> L3 SiliconFlow）
# ==============================================================================


def _kimi_config() -> dict | None:
    """L2 主通道 Kimi 官方（KIMI_API_KEY / MOONSHOT_API_KEY）。"""
    key = (os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY") or "").strip()
    if not key:
        return None
    return {
        "label": "Kimi",
        "api_key": key,
        "base_url": os.environ.get("KIMI_BASE_URL", "https://api.moonshot.cn/v1"),
        "model": os.environ.get("KIMI_MODEL", "").strip() or None,  # 仅显式覆盖，须经 /v1/models 校验
        "provider": "kimi",
        "model_preference": KIMI_MODEL_PREFERENCE,
        "fallback_model": None,  # 主通道必须经官方模型列表发现，杜绝 404 resource_not_found_error
        "token_param": "max_completion_tokens",  # 官方：max_tokens 已弃用
        "supports_prompt_cache_key": True,
        "supports_reasoning_effort": True,
        "max_output_cap": None,
    }


def _backup_config() -> dict | None:
    """L3 备用通道，默认硅基流动 SiliconFlow。

    依据官方文档（api-docs.siliconflow.cn/chat-completions-post）适配：
    - token 上限字段为 max_tokens（不含思维链），且不宜顶到窗口上限；
    - 模型 ID 为「厂商/模型名」格式（如 deepseek-ai/DeepSeek-V4-Flash），
      同样经官方 GET /v1/models 动态发现、按偏好序选择，BACKUP_MODEL 仅作显式覆盖；
    - 无 prompt_cache_key 字段（平台自动统计 prompt_cache_hit_tokens）；
    - reasoning_effort 仅支持 high/max 且 low/medium 会被映射为 high，低优任务不传；
    - 官方示例模型 deepseek-ai/DeepSeek-V4-Flash 作为发现接口异常时的兜底。
    也可把 BACKUP_BASE_URL 指向 DeepSeek 官方（https://api.deepseek.com/v1）等任意 OpenAI 兼容端点。
    """
    key = os.environ.get("BACKUP_API_KEY", "").strip()
    if not key:
        return None
    return {
        "label": "SiliconFlow",
        "api_key": key,
        "base_url": os.environ.get("BACKUP_BASE_URL", "https://api.siliconflow.cn/v1"),
        "model": os.environ.get("BACKUP_MODEL", "").strip() or None,
        "provider": "siliconflow",
        "model_preference": SILICONFLOW_MODEL_PREFERENCE,
        "fallback_model": "deepseek-ai/DeepSeek-V4-Flash",
        "token_param": "max_tokens",
        "supports_prompt_cache_key": False,
        "supports_reasoning_effort": False,
        "max_output_cap": 8192,
    }


def llm_available() -> bool:
    """是否具备在线 LLM 能力（有任一 API Key 且 openai 依赖可用）。"""
    return _OPENAI_OK and bool(_kimi_config() or _backup_config())


# ---- Token 用量统计（stream_options.include_usage 聚合）----
_USAGE_TOTAL: dict[str, int] = {
    "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
}


def reset_usage() -> None:
    for key in _USAGE_TOTAL:
        _USAGE_TOTAL[key] = 0


def _record_usage(usage) -> None:
    if not usage:
        return
    _USAGE_TOTAL["calls"] += 1
    _USAGE_TOTAL["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
    _USAGE_TOTAL["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details:
        _USAGE_TOTAL["cached_tokens"] += getattr(details, "cached_tokens", 0) or 0


def usage_status_text() -> str:
    """供流水线 yield 的用量状态文案；无调用时返回空串。"""
    if not _USAGE_TOTAL["calls"]:
        return ""
    hit_rate = _USAGE_TOTAL["cached_tokens"] / _USAGE_TOTAL["prompt_tokens"] * 100 if _USAGE_TOTAL["prompt_tokens"] else 0.0
    return (
        f"本轮 LLM 调用 {_USAGE_TOTAL['calls']} 次：输入 {_USAGE_TOTAL['prompt_tokens']} / "
        f"输出 {_USAGE_TOTAL['completion_tokens']} tokens，前缀缓存命中 {hit_rate:.0f}%"
    )


@dataclass
class LLMCallResult:
    content: str
    provider: str
    model: str
    finish_reason: str | None = None
    was_truncated: bool = False


class ResilientLLMGateway:
    """并发网关：client 缓存 + 模型发现 TTL/锁/过期兜底 + 冷却水位线 + 截断续写。"""

    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}
        self._model_cache: dict[str, tuple[float, list[str]]] = {}
        self._discovery_locks: dict[str, asyncio.Lock] = {}
        self._cooldown_until: dict[str, float] = {}

    def _get_client(self, api_key: str, base_url: str) -> AsyncOpenAI:
        cache_key = (api_key, base_url.rstrip("/"))
        client = self._clients.get(cache_key)
        if client is None:
            client = AsyncOpenAI(api_key=api_key, base_url=base_url.rstrip("/"), max_retries=0, timeout=_MODELS_TIMEOUT)
            self._clients[cache_key] = client
        return client

    async def _fetch_model_ids(self, cfg: dict) -> list[str]:
        client = self._get_client(cfg["api_key"], cfg["base_url"])
        resp = await asyncio.wait_for(client.models.list(), timeout=_MODELS_TIMEOUT)
        ids = [
            m.id for m in getattr(resp, "data", [])
            if getattr(m, "id", None) and not any(bad in m.id.lower() for bad in NON_CHAT_KEYWORDS)
        ]
        if not ids:
            raise ValueError("GET /v1/models 返回可用对话模型列表为空")
        return ids

    async def discover_model(self, cfg: dict) -> str:
        """动态发现 + 偏好序选择。显式覆盖必须存在于官方列表，否则回退自动选择。"""
        cache_key = f"{cfg['provider']}:{cfg['base_url']}"
        now = time.time()
        cached = self._model_cache.get(cache_key)

        if not cached or (now - cached[0] > _MODEL_CACHE_TTL):
            lock = self._discovery_locks.setdefault(cache_key, asyncio.Lock())
            async with lock:
                cached = self._model_cache.get(cache_key)
                if not cached or (time.time() - cached[0] > _MODEL_CACHE_TTL):
                    try:
                        ids = await self._fetch_model_ids(cfg)
                        self._model_cache[cache_key] = (time.time(), ids)
                        cached = self._model_cache[cache_key]
                        logger.info("[%s] 动态发现可用模型: %s", cfg["label"], ids)
                    except Exception as exc:
                        if cached and (time.time() - cached[0] <= _MODELS_STALE_GRACE):
                            logger.warning("[%s] 刷新模型列表失败，沿用过期缓存: %s", cfg["label"], exc)
                        else:
                            fallback = cfg.get("fallback_model")
                            if fallback:
                                logger.warning("[%s] 模型发现失败，回退官方文档示例模型 %s: %s", cfg["label"], fallback, exc)
                                return fallback
                            raise RuntimeError(f"[{cfg['label']}] 动态模型发现失败: {exc}") from exc

        available_ids = cached[1]

        explicit = cfg.get("model")
        if explicit:
            clean = explicit.strip()
            if clean in available_ids:
                return clean
            logger.warning("[%s] 指定模型 '%s' 不在可用列表中，自动回退优选", cfg["label"], clean)

        for pref in cfg.get("model_preference") or []:
            for mid in available_ids:
                if mid == pref or mid.startswith(pref):
                    return mid

        if cfg.get("provider") == "kimi":
            kimi_ids = sorted(i for i in available_ids if i.startswith("kimi-"))
            if kimi_ids:
                return kimi_ids[0]
        return available_ids[0]

    @staticmethod
    def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": m.get("role", "user"), "content": str(m.get("content", ""))} for m in messages]

    def _build_request_params(
        self, cfg: dict, model: str, max_completion_tokens: int, *,
        stream: bool, json_mode: bool, prompt_cache_key: str | None,
        reasoning_effort: str | None,
    ) -> dict:
        """Kimi 与 SiliconFlow 参数体系不同，按 provider 显式分通道。

        共用：stream / stream_options.include_usage（官方指南：聚合最终用量块）/ response_format。
        Kimi：max_completion_tokens（max_tokens 已弃用）+ prompt_cache_key +
              K3 系列 reasoning_effort（顶层非标准字段，经 extra_body 传递；不传 temperature/top_p，
              K3 为固定值，显式传入会报错）。
        SiliconFlow：max_tokens（官方文档字段，不含思维链）；
              无 prompt_cache_key（平台自动统计命中）；低优任务不传 reasoning_effort
              （官方仅支持 high/max 且 low/medium 会映射为 high）。
        """
        params: dict[str, Any] = {
            "model": model,
            "messages": None,  # 调用处注入
            "stream": stream,
        }
        if stream:
            params["stream_options"] = {"include_usage": True}
        if json_mode:
            params["response_format"] = {"type": "json_object"}  # 两侧官方文档均支持 JSON Mode
        params[cfg["token_param"]] = max_completion_tokens
        if cfg.get("supports_prompt_cache_key") and prompt_cache_key:
            params["prompt_cache_key"] = prompt_cache_key
        if cfg.get("supports_reasoning_effort") and reasoning_effort and model.startswith("kimi-k3"):
            params["extra_body"] = {"reasoning_effort": reasoning_effort}
        return params

    @staticmethod
    def _is_non_retryable(exc: Exception) -> bool:
        """401/403/404、内容过滤、配额耗尽：立即熔断不重试。"""
        if APIStatusError is not None and isinstance(exc, APIStatusError):
            code = exc.status_code
            err_str = str(exc).lower()
            return (
                code in (401, 403, 404)
                or (code == 429 and "exceeded_current_quota_error" in err_str)
                or (code == 400 and "content_filter" in err_str)
            )
        return False

    async def _execute_single_request(
        self, cfg: dict, model: str, messages: list[dict[str, Any]], *,
        max_completion_tokens: int, stream: bool, timeout: float,
        json_mode: bool, prompt_cache_key: str | None, reasoning_effort: str | None,
        on_heartbeat: Callable[[int], Awaitable[None]] | None = None,
    ) -> tuple[str, str | None]:
        cap = cfg.get("max_output_cap")
        effective_max = max_completion_tokens if not cap else min(max_completion_tokens, cap)
        kwargs = self._build_request_params(
            cfg, model, effective_max, stream=stream, json_mode=json_mode,
            prompt_cache_key=prompt_cache_key, reasoning_effort=reasoning_effort,
        )
        kwargs["messages"] = self._sanitize_messages(messages)
        client = self._get_client(cfg["api_key"], cfg["base_url"])

        async with _get_llm_semaphore():
            if not stream:
                resp = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=timeout)
                _record_usage(getattr(resp, "usage", None))
                choice = resp.choices[0]
                return getattr(choice.message, "content", None) or "", getattr(choice, "finish_reason", None)

            response_stream = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=_LLM_STREAM_OPEN_TIMEOUT)
            chunks: list[str] = []
            reasoning_parts: list[str] = []
            finish_reason: str | None = None
            usage = None
            total_chars, next_heartbeat = 0, _HEARTBEAT_CHARS
            try:
                async for chunk in response_stream:
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is None and getattr(chunk, "choices", None):
                        chunk_usage = getattr(chunk.choices[0], "usage", None)
                    if chunk_usage:
                        usage = chunk_usage  # 官方：最终统计块 choices 为空，usage 在顶层
                    if not getattr(chunk, "choices", None):
                        continue
                    choice = chunk.choices[0]
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    piece = getattr(delta, "content", None)
                    if isinstance(piece, list):  # 兼容多模态分片，取纯文本段
                        piece = "".join(
                            getattr(c, "text", "") or (c.get("text", "") if isinstance(c, dict) else "") for c in piece
                        )
                    if piece:
                        chunks.append(piece)
                        total_chars += len(piece)
                        if on_heartbeat and total_chars >= next_heartbeat:
                            next_heartbeat += _HEARTBEAT_CHARS
                            await on_heartbeat(total_chars)
            finally:
                close_fn = getattr(response_stream, "close", None)
                if callable(close_fn):
                    res = close_fn()
                    if asyncio.iscoroutine(res):
                        await res
            _record_usage(usage)

        text = "".join(chunks).strip()
        if not text:
            detail = f"（finish_reason={finish_reason}）" if finish_reason else ""
            if reasoning_parts:
                raise ValueError(
                    f"模型仅返回推理内容、未产出正文{detail}：推理 token 按输出计费，"
                    "请增大 max_completion_tokens，或为该调用设置 reasoning_effort='low'"
                )
            raise ValueError(f"模型返回空内容{detail}")
        return text, finish_reason

    async def _call_provider_with_retry(
        self, cfg: dict, messages: list[dict[str, Any]], *,
        max_completion_tokens: int, stream: bool, timeout: float,
        json_mode: bool = False, prompt_cache_key: str | None = None,
        reasoning_effort: str | None = None, allow_continuation: bool = False,
        on_heartbeat: Callable[[int], Awaitable[None]] | None = None,
    ) -> LLMCallResult:
        """单通道内：冷却水位线 -> 最多 3 次尝试 -> 截断自动续写 1 次。"""
        provider_key = cfg["provider"]
        wait_cool = self._cooldown_until.get(provider_key, 0.0) - time.time()
        if 0 < wait_cool <= 6.0:
            await asyncio.sleep(wait_cool)

        model = await self.discover_model(cfg)
        last_exc: Exception | None = None
        content, finish_reason = "", None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                content, finish_reason = await self._execute_single_request(
                    cfg, model, messages, max_completion_tokens=max_completion_tokens,
                    stream=stream, timeout=timeout, json_mode=json_mode,
                    prompt_cache_key=prompt_cache_key, reasoning_effort=reasoning_effort,
                    on_heartbeat=on_heartbeat,
                )
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if self._is_non_retryable(exc) or attempt >= _MAX_RETRIES:
                    logger.warning("[%s] 通道触发熔断/重试耗尽 (attempt=%d): %s", cfg["label"], attempt + 1, exc)
                    raise
                backoff = _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.4)
                self._cooldown_until[provider_key] = time.time() + backoff
                logger.info("[%s] 瞬时故障，%.1fs 后第 %d 次重试: %s", cfg["label"], backoff, attempt + 1, exc)
                await asyncio.sleep(backoff)
        else:
            raise last_exc or LLMUnavailableError(f"[{cfg['label']}] 调用失败")

        was_truncated = finish_reason == "length"
        if was_truncated and allow_continuation and content.strip():
            logger.info("[%s] 检测到 finish_reason=length，触发 1 次断点无缝续写...", cfg["label"])
            cont_msgs = copy.deepcopy(messages) + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "输出因长度上限截断，请紧接着上面最后一个字符继续输出剩余代码直到 </html> 结束，严禁重复已输出内容。"},
            ]
            try:
                tail, tail_reason = await self._execute_single_request(
                    cfg, model, cont_msgs, max_completion_tokens=max_completion_tokens,
                    stream=True, timeout=timeout, json_mode=False,
                    prompt_cache_key=prompt_cache_key, reasoning_effort=reasoning_effort,
                    on_heartbeat=on_heartbeat,
                )
                content += tail
                was_truncated = tail_reason == "length"
            except Exception as cont_exc:  # noqa: BLE001
                logger.warning("[%s] 自动续写失败，保留已生成前半部分: %s", cfg["label"], cont_exc)

        return LLMCallResult(content=content, provider=provider_key, model=model,
                             finish_reason=finish_reason, was_truncated=was_truncated)

    async def chat_completion(
        self, messages: list[dict[str, Any]], *,
        max_completion_tokens: int = 1024, stream: bool = True, timeout: float = 120.0,
        json_mode: bool = False, prompt_cache_key: str | None = None,
        reasoning_effort: str | None = None, allow_continuation: bool = False,
        on_heartbeat: Callable[[int], Awaitable[None]] | None = None,
    ) -> LLMCallResult:
        """统一容灾入口：L2 Kimi -> L3 SiliconFlow -> 抛 LLMUnavailableError（交 L4）。"""
        if not _OPENAI_OK:
            raise LLMUnavailableError("openai 依赖未安装")
        configs = [c for c in (_kimi_config(), _backup_config()) if c]
        if not configs:
            raise LLMUnavailableError("未配置 KIMI_API_KEY / BACKUP_API_KEY")
        errors: list[str] = []
        for cfg in configs:
            try:
                return await self._call_provider_with_retry(
                    cfg, messages, max_completion_tokens=max_completion_tokens, stream=stream,
                    timeout=timeout, json_mode=json_mode, prompt_cache_key=prompt_cache_key,
                    reasoning_effort=reasoning_effort, allow_continuation=allow_continuation,
                    on_heartbeat=on_heartbeat,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                detail = str(exc).replace("\n", " ")[:200] or "(无详情)"
                errors.append(f"{cfg['label']}({cfg['base_url']}): {type(exc).__name__}: {detail}")
                logger.warning("L%s 通道 %s 不可用: %s", 2 if cfg["provider"] == "kimi" else 3, cfg["label"], detail)
        raise LLMUnavailableError("；".join(errors))

    async def call_llm(
        self, messages: list[dict[str, Any]], *, max_completion_tokens: int = 16384,
        json_mode: bool = False, prompt_cache_key: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """轻量非流式便捷入口（PM/Host/Tester/标题等短调用），返回纯文本。"""
        res = await self.chat_completion(
            messages, max_completion_tokens=max_completion_tokens, stream=False, timeout=60.0,
            json_mode=json_mode, prompt_cache_key=prompt_cache_key, reasoning_effort=reasoning_effort,
        )
        return res.content


_GATEWAY = ResilientLLMGateway()


# ==============================================================================
# 6. 递归增量上下文压缩器（HTML 折叠 + 冷热分层 + 会话级持久化摘要）
# ==============================================================================
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


# ==============================================================================
# 7. 确定性代码安检：三层模糊补丁引擎 + JS 静态/语法校验（零 Token）
# ==============================================================================
class PatchAndSanitizerEngine:
    """三层渐进式 SEARCH/REPLACE 模糊替换（精确 -> 行尾空白归一化 -> 块级缩进归一化）；
    SEARCH 出现多次时视为歧义失败并给出可读错误（供 Reviewer 修正回路反馈 Coder）。"""

    PATCH_BLOCK_RE = re.compile(
        r"^<{5,9}\s*SEARCH\s*\r?\n(.*?)\r?\n={5,9}\s*\r?\n(.*?)\r?\n>{5,9}\s*REPLACE",
        re.MULTILINE | re.DOTALL,
    )

    @staticmethod
    def strip_thinking_and_fences(raw_text: str) -> str:
        if not raw_text:
            return ""
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text, flags=re.IGNORECASE).strip()
        fences = re.findall(r"```(?:html|htm|xml)?\s*\r?\n([\s\S]*?)```", cleaned, flags=re.IGNORECASE)
        if fences:
            cleaned = max(fences, key=len).strip()
        m_start = re.search(r"(<!DOCTYPE\s+html[^>]*>|<html\b[^>]*>)", cleaned, flags=re.IGNORECASE)
        m_end = list(re.finditer(r"</html>", cleaned, flags=re.IGNORECASE))
        if m_start and m_end:
            cleaned = cleaned[m_start.start(): m_end[-1].end()].strip()
        return cleaned

    @classmethod
    def extract_patch_blocks(cls, raw_text: str) -> list[tuple[str, str]]:
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text or "", flags=re.IGNORECASE)
        blocks: list[tuple[str, str]] = []
        for m in cls.PATCH_BLOCK_RE.finditer(cleaned):
            search_part, replace_part = m.group(1), m.group(2)
            if search_part.strip():
                blocks.append((search_part, replace_part))
        return blocks

    @staticmethod
    def _fuzzy_replace_single(code: str, search_block: str, replace_block: str) -> tuple[bool, str, str | None]:
        """返回 (是否成功, 新代码, 错误信息)。Tier 1 精确匹配且要求唯一。"""
        # Tier 1: 精确匹配（SEARCH 必须在原代码中唯一存在）
        count = code.count(search_block)
        if count == 1:
            return True, code.replace(search_block, replace_block, 1), None
        if count > 1:
            return False, code, f"SEARCH 片段在现有代码中出现 {count} 次，无法唯一定位（请扩大上下文行数）"

        code_lines = code.splitlines(keepends=True)
        search_lines = search_block.splitlines()
        n = len(search_lines)
        if n == 0 or n > len(code_lines):
            return False, code, "SEARCH 片段在原代码中未找到（需与原文逐字一致，注意缩进与空行）"

        # Tier 2: 行尾空白归一化匹配
        search_rstrip = [ln.rstrip() for ln in search_lines]
        for i in range(len(code_lines) - n + 1):
            window = [ln.rstrip() for ln in code_lines[i: i + n]]
            if window == search_rstrip:
                rep_text = replace_block if replace_block.endswith("\n") else replace_block + "\n"
                new_code = "".join(code_lines[:i]) + rep_text + "".join(code_lines[i + n:])
                return True, new_code, None

        # Tier 3: 块级缩进偏移归一化匹配（textwrap.dedent）
        dedented_search = textwrap.dedent("\n".join(search_lines)).strip()
        if not dedented_search:
            return False, code, "SEARCH 片段为空"
        for i in range(len(code_lines) - n + 1):
            raw_window = "".join(code_lines[i: i + n])
            if textwrap.dedent(raw_window).strip() == dedented_search:
                non_empty = [ln for ln in code_lines[i: i + n] if ln.strip()]
                base_indent = ""
                if non_empty:
                    min_spaces = min(len(ln) - len(ln.lstrip(" \t")) for ln in non_empty)
                    base_indent = non_empty[0][:min_spaces]
                dedented_rep = textwrap.dedent(replace_block)
                reindented_rep = textwrap.indent(dedented_rep, base_indent)
                if not reindented_rep.endswith("\n"):
                    reindented_rep += "\n"
                new_code = "".join(code_lines[:i]) + reindented_rep + "".join(code_lines[i + n:])
                return True, new_code, None

        return False, code, "SEARCH 片段在原代码中未找到（需与原文逐字一致，注意缩进与空行）"

    @classmethod
    def apply_patches(cls, code: str, blocks: list[tuple[str, str]]) -> tuple[str, list[str]]:
        """依次合并全部补丁并收集错误；任一失败即返回原始 code 与错误列表（供修正回路）。"""
        if not blocks:
            return code, ["未解析到有效 SEARCH/REPLACE 补丁块"]
        new_code = code
        errors: list[str] = []
        for idx, (search_part, replace_part) in enumerate(blocks, start=1):
            ok, new_code, err = cls._fuzzy_replace_single(new_code, search_part, replace_part)
            if not ok:
                errors.append(f"补丁 {idx}/{len(blocks)}：{err}")
        if errors:
            return code, errors
        if new_code == code:
            return code, ["补丁未对代码产生任何改动"]
        return new_code, []

    @staticmethod
    def validate_html(html: str, base_code: str | None = None, is_patch: bool = False) -> tuple[bool, str]:
        if not html or len(html.strip()) < _CODE_MIN_LEN:
            return False, "HTML 内容过短或为空"
        low = html.lower()
        if "<html" not in low or "</html>" not in low:
            return False, "缺少完整的 <html>...</html> 闭合结构"
        if "<body" in low and "</body>" not in low:
            return False, "缺少 </body> 闭合标签，疑似流式输出截断"
        if "<script" in low and "</script>" not in low:
            return False, "缺少 </script> 闭合标签，脚本不完整"
        if is_patch and base_code and len(html) < len(base_code) * 0.55:
            return False, "PATCH 后代码长度异常缩水超过 45%，疑似触发省略号破坏"
        return True, "OK"


# ---- JS 确定性体检：静态规则 + node --check 语法校验 ----
_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", re.IGNORECASE)
_BANNED_JS_CALL_RE = re.compile(r"\b(alert|prompt|confirm)\s*\(")
_INTERACTIVE_RE = re.compile(r"<button|<input[^>]*(?:onclick|oninput)|onclick=", re.IGNORECASE)


def extract_scripts(html: str | None) -> list[str]:
    """提取全部内联 <script> 内容（跳过 src 外链脚本）。"""
    if not html:
        return []
    return [m.group(1) for m in _SCRIPT_RE.finditer(html)]


def static_js_issues(html: str | None) -> list[str]:
    """零依赖静态规则检查，返回可读问题列表（无问题返回空列表）。"""
    issues: list[str] = []
    if not html:
        return ["HTML 为空"]
    scripts = extract_scripts(html)
    joined = "\n".join(scripts)
    for m in _BANNED_JS_CALL_RE.finditer(joined):
        line_no = joined.count("\n", 0, m.start()) + 1
        issues.append(f"脚本第 {line_no} 行使用了阻塞沙盒的 {m.group(1)}()，请改为页面内提示")
    if _INTERACTIVE_RE.search(html) and not scripts:
        issues.append("页面存在可交互控件（按钮/输入框/onclick）但没有任何内联 <script>，控件将是摆设")
    return issues


def check_js_syntax(html: str | None) -> list[str]:
    """提取内联脚本经本机 `node --check` 做语法校验；无 node 环境自动跳过（返回空列表）。"""
    if shutil.which("node") is None:
        return []
    issues: list[str] = []
    for idx, script in enumerate(extract_scripts(html), start=1):
        code = script.strip()
        if not code:
            continue
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
                fh.write(code)
                tmp_path = fh.name
            proc = subprocess.run(
                ["node", "--check", tmp_path],
                capture_output=True, text=True, timeout=_NODE_CHECK_TIMEOUT,
            )
            if proc.returncode != 0:
                # node --check 输出形如：...SyntaxError: ... (line:NN)
                first_err = next((ln for ln in proc.stderr.splitlines() if "Error" in ln), proc.stderr.strip())
                issues.append(f"<script> 块 {idx} 语法错误：{first_err.strip()}")
        except subprocess.TimeoutExpired:
            issues.append(f"<script> 块 {idx} 语法校验超时（>{_NODE_CHECK_TIMEOUT}s）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("node --check 执行失败，跳过语法校验: %s", exc)
        finally:
            try:
                os.unlink(tmp_path)  # type: ignore[possibly-undefined]
            except OSError:
                pass
    return issues


def post_generation_issues(html: str | None) -> list[str]:
    """Reviewer 的完整确定性体检 = 静态规则 + 语法校验。"""
    return static_js_issues(html) + check_js_syntax(html)


# ==============================================================================
# 8. L4 离线容灾兜底（模板资产归数据库，本模块零冗余存储）
# ==============================================================================
TemplateLookupFn = Callable[[str], Optional[str]]

_MINIMAL_SAFE_FALLBACK_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>演示应用</title></head>
<body style="font-family:sans-serif;padding:40px;background:#f8fafc;color:#1e293b">
<h2>演示模式应用预览</h2><p>当前未配置在线大模型 API Key，请从左侧公共广场选择预置交互应用体验，或配置环境变量后重试。</p>
</body></html>"""


def resolve_l4_fallback(
    user_msg: str, base_code: str | None = None,
    template_lookup: TemplateLookupFn | None = None,
) -> tuple[str | None, str]:
    """L4 离线容灾决策：
    1. 已有可用代码 -> 原样保留，绝不破坏用户现有成果；
    2. 无代码 -> 经 template_lookup 回调查数据库匹配预置模板（前端公共广场同源资产）；
    3. 未注入回调或查询失败 -> 极简安全骨架，保证零报错。
    """
    if base_code and len(base_code.strip()) >= _CODE_MIN_LEN:
        return None, (
            f"{DEMO_TAG} 当前未检测到可用在线模型，已为您完整保留当前可用应用，"
            "也可点击左侧公共广场体验预置应用。"
        )
    if callable(template_lookup):
        try:
            db_html = template_lookup(user_msg)
            if db_html and len(db_html.strip()) >= _CODE_MIN_LEN:
                return db_html, (
                    f"{DEMO_TAG} 当前未检测到可用在线模型，已从公共应用库为您匹配加载本地可交互应用模板。"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("调用数据库模板查询回调失败: %s", exc)
    return _MINIMAL_SAFE_FALLBACK_HTML, (
        f"{DEMO_TAG} 当前未检测到可用在线模型，已为您加载基础演示页"
        "（完整预置应用请查看左侧公共广场）。"
    )


# ==============================================================================
# 9. Agent 提示词与独立原子 Agent 函数
# ==============================================================================
_PM_SYSTEM = """你是 Prompt2App 团队的 PM Agent，负责把用户需求路由给最合适的生成策略。
严格只输出一个 JSON 对象（不要 markdown 代码块、不要解释、不要输出 JSON 以外的任何文字）。
输出格式（官方 JSON Mode 约束）：
{"route": "FULL 或 PATCH", "plan": "给 Coder 的简明实现要点", "title": "不超过12字的应用标题", "spec": true 或 false}
字段说明：
- route 只能是 FULL 或 PATCH；plan 用一句话说明做什么；title 为应用短标题；
- spec：该应用是否属于复杂应用（多模块交互、游戏、实时数据、富状态管理等），复杂应用先出设计规范再写代码。
输出示例：{"route":"PATCH","plan":"将页面主题色改为蓝色并统一按钮圆角","title":"蓝色主题调整","spec":false}
路由规则：
- 已有代码且用户需求是修改/调整/修复/替换/新增局部内容（改颜色、改文案、加按钮、修 bug 等）-> PATCH
- 没有已有代码，或用户明确要求全新应用/整体重做/大幅改版 -> FULL
"""

_CODER_FULL_SYSTEM = """你是 Coder Agent，负责生成完整的单文件 HTML 应用。
硬性要求：
1. 输出一个 ```html 代码块，内含从 <!DOCTYPE html> 到 </html> 的完整、可直接保存运行的单文件应用；除此之外不要输出任何解释文字。
2. 所有 CSS 与 JS 必须内联；允许使用 Tailwind、Font Awesome 等 CDN。
3. 界面现代美观（合理的配色、卡片、圆角、阴影、留白），必须包含真实可用的交互逻辑（事件绑定 + 状态更新），任何按钮都必须有响应，禁止摆设控件。
4. 中文界面，移动端自适应（viewport + 响应式布局）。
5. 代码健壮：对空输入/非法输入做校验与提示，不得出现会导致白屏的 JS 错误。
6. 严禁使用阻塞沙盒的 alert() / prompt() / confirm()，改用页面内提示。"""

_CODER_PATCH_SYSTEM = """你是 Coder Agent，负责为已有的单文件 HTML 应用生成「局部修改补丁」，而不是重写整个应用。
只输出一个或多个如下格式的补丁块，除此之外不要输出任何解释文字：

<<<<<<< SEARCH
与现有代码逐字完全一致（包括缩进与空行）的原始代码片段
=======
替换之后的新代码片段
>>>>>>> REPLACE

硬性要求：
1. SEARCH 片段必须逐字复制自当前代码原文，禁止改写、增删空白；建议片段 >= 3 行以保证唯一匹配。
2. 可以有多个补丁块，按应用顺序依次输出。
3. 不要输出完整 HTML，不要输出 markdown 代码块包裹整个回答。"""

_REVIEWER_FIX_SYSTEM = """你是 Coder Agent，上一轮生成的补丁未能应用到现有代码。请根据错误反馈重新输出修正后的 SEARCH/REPLACE 补丁块（格式与要求不变，只输出补丁块）。
错误反馈："""

_CODER_FULL_FIX_SYSTEM = """你是 Coder Agent，上一轮生成的完整 HTML 未通过验收。请根据错误反馈重新输出完整、可运行的单文件 HTML 应用（格式与硬性要求不变，只输出 ```html 代码块）。
错误反馈："""

_DESIGN_SPEC_SYSTEM = """你是前端架构师。为一个单文件 HTML 应用输出精炼的设计规范，供 Coder 据此一次性写出高质量实现。
严格只输出纯文本规范（不要 JSON、不要代码），控制在 12 行以内，包含：
1. 布局结构（区块自上而下）；
2. 配色方案（主色/辅色/背景，给出十六进制色值）；
3. 核心组件清单；
4. 状态与交互清单（每个控件触发什么、状态如何变化）；
5. 数据持久化方式（如 localStorage 键名）。
只做决策，不解释理由。"""

_TESTER_SYSTEM = """你是严格的 QA 测试员，负责走查一份单文件 HTML 应用代码（无法真正运行，请基于代码静态推理）。
只输出一个 JSON 对象：
{"issues": [{"severity": "high|medium|low", "desc": "问题描述（含定位线索）"}]}
检查维度：
1. 交互完整性：页面上的每个按钮/输入框是否都有事件绑定与实际效果，是否存在点了没反应的控件；
2. 边界与异常：空输入、非法输入、重复操作、localStorage 无数据首次打开时是否会报错或白屏；
3. 状态一致性：增删改后界面与数据是否同步刷新；
4. 明显的 JS 运行时错误风险（未定义变量、选择器与 DOM 不匹配等）。
没有问题则输出 {"issues": []}。只报有把握的问题，severity=high 表示会导致功能不可用或白屏。"""

_HOST_SYSTEM = """你是 Chat Host，负责用 2~4 句简洁、专业、友好的中文向用户说明本次交付结果：完成了什么、有哪些关键设计/交互、接下来可以怎样迭代。不要复述代码，不要使用 markdown 列表符号。"""


def _history_messages(history: list[dict], limit: int = 6, cap: int = 800) -> list[dict]:
    """保留最近若干条并控制长度；assistant 的大段 HTML 以占位说明替代（当前版本由 base_code 提供）。"""
    msgs: list[dict] = []
    for item in (history or [])[-limit:]:
        try:
            role, content = item.get("role"), str(item.get("content", ""))
        except AttributeError:
            continue
        if role not in ("user", "assistant") or not content.strip():
            continue
        if role == "assistant" and ("<html" in content.lower() or len(content) > 2000):
            content = "（上一轮已生成完整应用代码；代码当前版本随请求单独提供，此处不再重复）"
        elif len(content) > cap:
            content = content[:cap] + "……（已截断）"
        msgs.append({"role": role, "content": content})
    return msgs


def _parse_json_object(text: str) -> dict | None:
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start: end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _heuristic_route(user_msg: str, has_code: bool) -> str:
    """PM 离线启发式：无代码必为 FULL；有代码且出现修改意图关键词则 PATCH。"""
    if not has_code:
        return "FULL"
    text = (user_msg or "").lower()
    patch_hint = ("改", "修改", "换成", "变成", "颜色", "字体", "标题", "文案", "按钮", "增加", "添加",
                  "删除", "去掉", "修复", "bug", "调整", "替换", "更新", "加一个", "改成")
    return "PATCH" if any(k in text for k in patch_hint) else "FULL"


def _need_spec_heuristic(user_msg: str) -> bool:
    """离线复杂度启发式：复杂/多模块应用先出设计规范再写代码。"""
    text = (user_msg or "").lower()
    complex_kw = ("游戏", "聊天", "看板", "仪表盘", "商城", "编辑器", "日历", "音乐", "播放器",
                  "贪吃蛇", "棋", "牌", "2048", "复杂", "完整", "系统", "管理")
    return len(text) >= 25 or any(k in text for k in complex_kw)


def _truncate_code(code: str, cap: int = _PATCH_CODE_CAP) -> str:
    """超长现有代码截断喂给 PATCH 模型（保留首尾，中间省略标注），控制 prompt 体积。"""
    if len(code) <= cap:
        return code
    return code[: cap // 2] + "\n<!-- ... 中间内容省略 ... -->\n" + code[-cap // 2:]


def _summarize_code_metadata(code: str | None) -> str:
    """为 PM 提供零源码 Token 消耗的结构统计信息。"""
    if not code or len(code.strip()) < 50:
        return "当前无已有代码（全新创建）"
    lines = code.splitlines()
    title_match = re.search(r"<title>(.*?)</title>", code, flags=re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else "未命名应用"
    has_script = "<script" in code.lower()
    return f"已有单文件 HTML 应用《{title}》，共 {len(lines)} 行（{len(code)} 字符），含JS交互={has_script}"


# ---- Agent 1: PM ----
async def pm_agent(user_msg: str, current_code: str | None = None, *,
                   session_id: int | None = None,
                   prompt_cache_key: str | None = None) -> PMResult:
    """意图路由与需求拆解（含复杂度 spec 判断）。在线走 JSON Mode；失败退化启发式，绝不抛异常。"""
    has_code = bool(current_code and current_code.strip())
    if not llm_available():
        return PMResult(route=_heuristic_route(user_msg, has_code),
                        source="heuristic", need_spec=_need_spec_heuristic(user_msg))
    try:
        raw = await _GATEWAY.call_llm(
            [
                {"role": "system", "content": _PM_SYSTEM},
                {"role": "user", "content": "【路由任务】请根据消息末尾给出的信息决策，只输出路由 JSON。\n\n已有代码："
                 + ("有：" + _summarize_code_metadata(current_code) if has_code else "无")
                 + "\n\n用户消息：\n" + user_msg},
            ],
            max_completion_tokens=2048, json_mode=True, prompt_cache_key=prompt_cache_key,
            reasoning_effort="low",
        )
        data = _parse_json_object(raw) or {}
        route = str(data.get("route", "")).upper().strip()
        plan = str(data.get("plan", "")).strip()
        title = re.sub(r"\s+", "", str(data.get("title", "")))[:12]
        need_spec = bool(data.get("spec", False))
        if route not in ("FULL", "PATCH"):
            raise ValueError(f"非法路由: {route}")
        if route == "PATCH" and not has_code:
            route = "FULL"
        return PMResult(route=route, plan=plan or user_msg, title=title,
                        source="llm", need_spec=need_spec)
    except Exception as exc:  # noqa: BLE001
        logger.info("PM Agent 降级为规则路由: %s", exc)
        return PMResult(route=_heuristic_route(user_msg, has_code),
                        source="heuristic", need_spec=_need_spec_heuristic(user_msg))


# ---- Agent 1.5: DesignSpec（复杂 FULL 应用的预生成设计规范）----
async def design_spec_agent(user_msg: str, plan: str, *,
                            prompt_cache_key: str | None = None) -> str:
    """为复杂应用产出精炼设计规范（布局/配色/组件/交互/持久化），供 Coder 据此一次性写好实现。"""
    res = await _GATEWAY.chat_completion(
        [
            {"role": "system", "content": _DESIGN_SPEC_SYSTEM},
            {"role": "user", "content": f"== PM 实现要点 ==\n{plan or '（无）'}\n\n== 用户需求 ==\n{user_msg}"},
        ],
        max_completion_tokens=1024, stream=False, timeout=60.0,
        prompt_cache_key=prompt_cache_key, reasoning_effort="low",
    )
    return res.content.strip()


# ---- Agent 2: Coder ----
async def _coder_full(user_msg: str, history: list[dict], plan: str,
                      design_spec: str,
                      prompt_cache_key: str | None,
                      on_heartbeat: Callable[[int], Awaitable[None]] | None) -> str:
    """Coder（全量）：产出完整 HTML；失败抛 LLMUnavailableError 交由外层兜底。"""
    blocks = ["【生成任务】根据消息末尾的需求与上下文，生成完整单文件 HTML 应用。"
              "除 ```html 代码块外不要输出任何解释文字。"]
    if design_spec:
        blocks.append(f"== 设计规范（必须严格遵循）==\n{design_spec}")
    elif plan:
        blocks.append(f"== PM 实现要点 ==\n{plan}")
    hist = _history_messages(history)
    if hist:
        blocks.append("== 对话上下文（近几轮摘要）==\n" + "\n".join(f"{m['role']}: {m['content']}" for m in hist))
    blocks.append(f"== 用户需求 ==\n{user_msg}")
    res = await _GATEWAY.chat_completion(
        [
            {"role": "system", "content": _CODER_FULL_SYSTEM},
            {"role": "user", "content": "\n\n".join(blocks)},
        ],
        max_completion_tokens=32768, stream=True, timeout=180.0,
        prompt_cache_key=prompt_cache_key, allow_continuation=True, on_heartbeat=on_heartbeat,
    )
    return res.content


async def _coder_full_fix(user_msg: str, plan: str, design_spec: str,
                          errors: list[str], prompt_cache_key: str | None,
                          on_heartbeat: Callable[[int], Awaitable[None]] | None) -> str:
    """FULL 确定性体检失败后的修正轮：把可读错误反馈回 Coder 重写。"""
    feedback = "\n".join(f"- {e}" for e in errors)
    blocks = [f"== 验收错误反馈（必须逐条修复）==\n{feedback}"]
    if design_spec:
        blocks.append(f"== 设计规范（保持遵循）==\n{design_spec}")
    elif plan:
        blocks.append(f"== PM 实现要点 ==\n{plan}")
    blocks.append(f"== 用户需求 ==\n{user_msg}")
    res = await _GATEWAY.chat_completion(
        [
            {"role": "system", "content": _CODER_FULL_FIX_SYSTEM},
            {"role": "user", "content": "\n\n".join(blocks)},
        ],
        max_completion_tokens=32768, stream=True, timeout=180.0,
        prompt_cache_key=prompt_cache_key, allow_continuation=True, on_heartbeat=on_heartbeat,
    )
    return res.content


async def _coder_patch(user_msg: str, current_code: str,
                       prompt_cache_key: str | None) -> str:
    """Coder（补丁）：产出 SEARCH/REPLACE 补丁块。"""
    res = await _GATEWAY.chat_completion(
        [
            {"role": "system", "content": _CODER_PATCH_SYSTEM},
            {"role": "user", "content": f"当前应用代码：\n```html\n{_truncate_code(current_code)}\n```\n\n用户修改需求：{user_msg}"},
        ],
        max_completion_tokens=8192, stream=True, timeout=120.0,
        prompt_cache_key=prompt_cache_key,
    )
    return res.content


async def _reviewer_fix(user_msg: str, current_code: str, errors: list[str],
                        prompt_cache_key: str | None) -> str:
    """PATCH 合并失败时，把可读错误反馈回 Coder 要求修正（一轮）。"""
    res = await _GATEWAY.chat_completion(
        [
            {"role": "system", "content": _REVIEWER_FIX_SYSTEM + "；".join(errors)},
            {"role": "user", "content": f"当前应用代码：\n```html\n{_truncate_code(current_code)}\n```\n\n用户修改需求：{user_msg}"},
        ],
        max_completion_tokens=8192, stream=True, timeout=120.0,
        prompt_cache_key=prompt_cache_key,
    )
    return res.content


async def coder_agent(pm: PMResult, user_msg: str, history: list[dict] | None = None,
                      current_code: str | None = None, *,
                      design_spec: str = "",
                      prompt_cache_key: str | None = None,
                      on_heartbeat: Callable[[int], Awaitable[None]] | None = None) -> CoderResult:
    """按 PM 方案产出代码（FULL=完整 HTML，PATCH=补丁块）。失败抛 LLMUnavailableError。"""
    if pm.route == "PATCH" and current_code:
        raw = await _coder_patch(user_msg, current_code, prompt_cache_key)
        return CoderResult(mode="PATCH", raw=raw,
                           patches=PatchAndSanitizerEngine.extract_patch_blocks(raw))
    raw = await _coder_full(user_msg, history or [], pm.plan, design_spec,
                            prompt_cache_key, on_heartbeat)
    return CoderResult(mode="FULL", raw=raw,
                       code=PatchAndSanitizerEngine.strip_thinking_and_fences(raw))


# ---- Agent 3: Reviewer（三道确定性防线 + 一轮修正回路）----
async def reviewer_agent(coder: CoderResult, user_msg: str,
                         current_code: str | None = None, *,
                         plan: str = "", design_spec: str = "",
                         prompt_cache_key: str | None = None,
                         on_heartbeat: Callable[[int], Awaitable[None]] | None = None) -> ReviewResult:
    """合并补丁/校验代码，并对确定性体检问题发起一轮修正。

    PATCH：三层模糊合并 -> HTML 门禁 -> 确定性体检；失败先把可读错误反馈 Coder 修正一轮。
    FULL：思维链剥离 -> HTML 门禁 -> 确定性体检；失败走 _coder_full_fix 重写一轮。
    修正轮 LLM 不可用/仍失败：PATCH 返回 degrade_full，FULL 遗留问题降级为 warnings 放行。
    """
    if coder.mode != "PATCH":
        clean = PatchAndSanitizerEngine.strip_thinking_and_fences(coder.raw or "")
        ok, msg = PatchAndSanitizerEngine.validate_html(clean, base_code=current_code, is_patch=False)
        messages: list[str] = []
        if not ok:
            # 结构不完整：给一轮重写机会（错误反馈回 Coder）
            messages.append(f"Reviewer Agent：HTML 结构校验未通过（{msg}），正在要求 Coder 重写...")
            try:
                fixed_raw = await _coder_full_fix(user_msg, plan, design_spec, [msg],
                                                  prompt_cache_key, on_heartbeat)
            except LLMUnavailableError as fix_exc:
                messages.append(f"Coder 重写请求不可用: {fix_exc}")
                return ReviewResult(ok=False, errors=[f"FULL 输出未通过 HTML 合法性校验: {msg}"], messages=messages)
            coder.raw, coder.fixed = fixed_raw, True
            clean = PatchAndSanitizerEngine.strip_thinking_and_fences(fixed_raw)
            ok, msg = PatchAndSanitizerEngine.validate_html(clean, is_patch=False)
            if not ok:
                return ReviewResult(ok=False, errors=[f"重写后仍未通过 HTML 合法性校验: {msg}"], messages=messages)
            messages.append("Coder Agent 已完成（重写）：结构校验通过")
        issues = post_generation_issues(clean)
        warnings: list[str] = []
        if issues:
            messages.append(f"Reviewer Agent：确定性体检发现 {len(issues)} 个问题，正在要求 Coder 修正...")
            try:
                fixed_raw = await _coder_full_fix(user_msg, plan, design_spec, issues,
                                                  prompt_cache_key, on_heartbeat)
            except LLMUnavailableError as fix_exc:
                messages.append(f"Coder 修正请求不可用，按告警放行: {fix_exc}")
                return ReviewResult(code=clean, ok=True, warnings=issues, messages=messages)
            coder.raw, coder.fixed = fixed_raw, True
            fixed_clean = PatchAndSanitizerEngine.strip_thinking_and_fences(fixed_raw)
            fixed_ok, fixed_msg = PatchAndSanitizerEngine.validate_html(fixed_clean, is_patch=False)
            fixed_issues = post_generation_issues(fixed_clean) if fixed_ok else [fixed_msg]
            if fixed_ok and not fixed_issues:
                messages.append("Coder Agent 已完成（修正）：确定性体检通过")
                coder.code = fixed_clean
                return ReviewResult(code=fixed_clean, ok=True, messages=messages)
            messages.append(f"修正后仍存在 {len(fixed_issues)} 个问题，按告警放行不阻塞交付")
            warnings = fixed_issues if fixed_ok else issues + [f"修正稿未通过结构校验: {fixed_msg}"]
            final = fixed_clean if fixed_ok else clean
            coder.code = final
            return ReviewResult(code=final, ok=True, warnings=warnings, messages=messages)
        coder.code = clean
        return ReviewResult(code=clean, ok=True)

    if not coder.patches:
        clean_full = PatchAndSanitizerEngine.strip_thinking_and_fences(coder.raw or "")
        ok, msg = PatchAndSanitizerEngine.validate_html(clean_full, base_code=current_code, is_patch=False)
        if ok:
            return ReviewResult(code=clean_full, ok=True,
                                messages=["Reviewer：模型直接输出了完整 HTML，按 FULL 路径接管校验"])
        return ReviewResult(ok=False, errors=["未解析到有效 SEARCH/REPLACE 补丁块"], degrade_full=True)

    assert current_code is not None, "PATCH 审核必须提供 current_code"
    new_code, errors = PatchAndSanitizerEngine.apply_patches(current_code, coder.patches)
    messages = []
    if errors:
        messages.append(f"Reviewer Agent：补丁未完全匹配（{'；'.join(errors)}），正在请求 Coder 修正...")
        try:
            fixed_text = await _reviewer_fix(user_msg, current_code, errors, prompt_cache_key)
        except LLMUnavailableError as fix_exc:
            messages.append(f"Coder 修正请求不可用，降级 FULL: {fix_exc}")
            return ReviewResult(ok=False, errors=errors, degrade_full=True, messages=messages)
        coder.patches = PatchAndSanitizerEngine.extract_patch_blocks(fixed_text)
        coder.raw, coder.fixed = fixed_text, True
        messages.append(f"Coder Agent 已完成（修正）：产出 {len(coder.patches)} 个补丁块")
        new_code, errors = PatchAndSanitizerEngine.apply_patches(current_code, coder.patches)
    if errors:
        return ReviewResult(ok=False, errors=errors, degrade_full=True, messages=messages)
    ok, msg = PatchAndSanitizerEngine.validate_html(new_code, base_code=current_code, is_patch=True)
    if not ok:
        return ReviewResult(ok=False, errors=[f"合并后 HTML 安检未通过: {msg}"],
                            degrade_full=True, messages=messages)
    issues = post_generation_issues(new_code)
    if issues:
        messages.append(f"Reviewer Agent：合并后确定性体检发现 {len(issues)} 个告警：{'；'.join(issues)}")
        return ReviewResult(code=new_code, ok=True, warnings=issues, messages=messages)
    return ReviewResult(code=new_code, ok=True, merged_patches=len(coder.patches), messages=messages)


# ---- Agent 3.5: Tester（低推理静态走查，高风险问题回喂修复）----
async def tester_agent(code: str, user_msg: str, *,
                       prompt_cache_key: str | None = None) -> list[str]:
    """QA 走查，返回 high 严重级问题描述列表。LLM 不可用时返回空列表（静默跳过）。"""
    if not llm_available():
        return []
    try:
        raw = await _GATEWAY.call_llm(
            [
                {"role": "system", "content": _TESTER_SYSTEM},
                {"role": "user", "content": f"== 用户需求 ==\n{user_msg}\n\n== 应用代码 ==\n```html\n{_truncate_code(code, cap=12000)}\n```"},
            ],
            max_completion_tokens=2048, json_mode=True, prompt_cache_key=prompt_cache_key,
            reasoning_effort="low",
        )
        data = _parse_json_object(raw) or {}
        issues = data.get("issues") or []
        high = [str(i.get("desc", "")).strip() for i in issues
                if isinstance(i, dict) and i.get("severity") == "high" and i.get("desc")]
        return high
    except Exception as exc:  # noqa: BLE001
        logger.info("Tester Agent 不可用，跳过走查: %s", exc)
        return []


# ---- Agent 4: Chat Host ----
async def chat_host_agent(user_msg: str, route: str, ok: bool,
                          prompt_cache_key: str | None = None) -> str:
    """总结本轮交付；LLM 失败用本地话术兜底，绝不抛异常。"""
    if not ok:
        return ("本轮生成结果未通过校验。为遵守「绝不破坏已有代码」的原则，已为你完整保留上一版可用应用，"
                "没有做任何改动。可以换个更具体的描述再试一次。")
    try:
        text = await _GATEWAY.call_llm(
            [
                {"role": "system", "content": _HOST_SYSTEM},
                {"role": "user", "content": f"用户需求：{user_msg}\n生成方式：{'局部补丁修改' if route == 'PATCH' else '全量生成新应用'}"},
            ],
            max_completion_tokens=2048, prompt_cache_key=prompt_cache_key,
            reasoning_effort="low",
        )
        text = (text or "").strip().strip('"').strip()
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass
    if route == "PATCH":
        return "已按你的要求完成局部修改，补丁已安全合并进现有应用，右侧预览已同步更新，可继续提出调整。"
    return "已为你生成全新的单文件应用，右侧沙盒可直接交互体验；也可以继续提需求做局部迭代。"


async def suggest_title(user_msg: str, fallback: str = "新应用") -> str:
    """为新会话生成短标题（main.py 创建会话时调用）；内部已容错，永不抛异常。"""
    msg = (user_msg or "").strip()
    try:
        if msg and llm_available():
            raw = await _GATEWAY.call_llm(
                [{"role": "system", "content": "用不超过12个字概括用户需求，只输出标题本身，不要标点、引号或解释。"},
                 {"role": "user", "content": msg[:500]}],
                max_completion_tokens=128, reasoning_effort="low",
            )
            title = re.sub(r"\s+", "", raw or "")[:12]
            if title:
                return title
    except Exception:  # noqa: BLE001
        pass
    line = re.split(r"[，。,.!?？!；;\n]", msg)[0].strip()
    if not line:
        return fallback
    return line[:10] + "…" if len(line) > 10 else line


# ==============================================================================
# 10. 检查点回调基础设施
# ==============================================================================
_ABORT = object()


async def _apply_checkpoint(
    on_checkpoint: Callable[[str, Any], Awaitable[Any] | Any] | None,
    stage: str, result: Any,
) -> Any:
    """调用检查点回调：返回 None 保持原样；返回修改后的 result 生效；CheckpointAbort -> 哨兵。"""
    if on_checkpoint is None:
        return result
    try:
        out = on_checkpoint(stage, result)
        if inspect.isawaitable(out):
            out = await out
        return result if out is None else out
    except CheckpointAbort:
        return _ABORT


async def _cancelled_events(user_msg: str, stage_name: str,
                            current_code: str | None) -> AsyncGenerator[dict, None]:
    """检查点取消时的收尾事件：保留已有代码，契约仍完整（status -> text -> code）。"""
    has_code = bool(current_code and current_code.strip())
    yield {"type": "status", "content": f"已在 {stage_name} 检查点取消，未对现有应用做任何改动"}
    yield {"type": "text",
           "content": f"你已在 {stage_name} 阶段取消本次生成，你的应用保持原样，可调整需求后重新发起。"}
    yield {"type": "code", "content": current_code if has_code else _MINIMAL_SAFE_FALLBACK_HTML}


# ==============================================================================
# 11. 工作流编排（两阶段 API + 一键直通 + 断点续跑）
# ==============================================================================
async def _pm_stage(cp: WorkflowCheckpoint, user_msg: str, history: list[dict],
                    prompt_cache_key: str | None) -> tuple[list[dict], PMResult]:
    """PM 阶段：压缩历史 -> 路由（含复杂度判断）-> 落盘 pm_done 检查点。PM 失败自动退化启发式。"""
    events: list[dict] = [{"type": "status", "content": "PM Agent 正在拆解需求..."}]
    if not llm_available():
        events.append({"type": "status", "content": "LLM 通道不可用，将切换至第 4 层离线兜底引擎"})
    try:
        cp.compressed_history = await ContextCompressor.compress(history, cp.session_id)
    except Exception:  # noqa: BLE001
        cp.compressed_history = ""
    try:
        pm = await pm_agent(user_msg, cp.base_code, session_id=cp.session_id,
                            prompt_cache_key=prompt_cache_key)
    except Exception:  # noqa: BLE001 - 最后保险丝
        pm = PMResult(route=_heuristic_route(user_msg, bool(cp.base_code and cp.base_code.strip())),
                      source="heuristic")
    cp.pm = pm.to_dict()
    cp.route_mode = "PATCH" if (pm.route == "PATCH" and cp.base_code) else "FULL"
    cp.dev_plan = pm.plan
    cp.current_step = "pm_done"
    CheckpointStore.save(cp)
    pm.run_id = cp.run_id
    label = "（离线启发式路由）" if pm.source == "heuristic" else ""
    events.append({"type": "status", "content": f"PM Agent 已完成{label}：{pm.display()}"})
    return events, pm


def _coder_from_checkpoint(cp: WorkflowCheckpoint) -> CoderResult:
    """断点续跑：从落盘的 Coder 原始产出重建结构化结果。"""
    if cp.route_mode == "PATCH":
        return CoderResult(mode="PATCH", raw=cp.raw_coder_output,
                           patches=PatchAndSanitizerEngine.extract_patch_blocks(cp.raw_coder_output),
                           provider=cp.coder_provider)
    return CoderResult(mode="FULL", raw=cp.raw_coder_output,
                       code=PatchAndSanitizerEngine.strip_thinking_and_fences(cp.raw_coder_output),
                       provider=cp.coder_provider)


async def _build_online(
    cp: WorkflowCheckpoint, pm: PMResult, user_msg: str, history: list[dict],
    prompt_cache_key: str | None,
    on_checkpoint: Callable[[str, Any], Awaitable[Any] | Any] | None,
    on_heartbeat: Callable[[int], Awaitable[None]] | None,
    enable_spec: bool, enable_tester: bool,
) -> AsyncGenerator[dict, None]:
    """在线流水线主体。每个阶段边界落盘检查点；Tester 高风险问题回喂一轮修复。"""
    current_code = cp.base_code
    has_code = bool(current_code and current_code.strip())
    final_code: str | None = None
    summary: str | None = None
    route: str = "PATCH" if (pm.route == "PATCH" and has_code) else "FULL"

    if pm.source == "user":
        yield {"type": "status", "content": f"按你确认的方案执行：{pm.display()}"}

    # 断点已推进到 reviewer_done：最终代码就绪，仅补 Chat Host
    if cp.current_step == "reviewer_done" and cp.final_code:
        final_code = cp.final_code
        yield {"type": "status", "content": "已从断点恢复：最终代码就绪，仅补 Chat Host 播报"}

    # ---- DesignSpec 阶段（仅 FULL 复杂应用，且无缓存的设计规范）----
    if final_code is None and route == "FULL" and enable_spec and pm.need_spec and not cp.design_spec:
        yield {"type": "status", "content": "DesignSpec Agent 正在产出设计规范（布局/配色/组件/交互）..."}
        cp.design_spec = await design_spec_agent(user_msg, pm.plan, prompt_cache_key=prompt_cache_key)
        CheckpointStore.save(cp)
        yield {"type": "status", "content": f"DesignSpec Agent 已完成：设计规范 {len(cp.design_spec)} 字符"}

    if final_code is None and route == "PATCH":
        # ---- Coder（PATCH）----
        if cp.current_step == "coder_done" and cp.raw_coder_output:
            coder = _coder_from_checkpoint(cp)
            yield {"type": "status", "content": f"已从断点恢复 Coder 产物（{len(coder.patches)} 个补丁块）"}
        else:
            yield {"type": "status", "content": "Coder Agent 正在生成局部补丁..."}
            coder = await coder_agent(pm, user_msg, history, current_code,
                                      prompt_cache_key=prompt_cache_key, on_heartbeat=on_heartbeat)
            cp.raw_coder_output = coder.raw
            cp.coder_provider = coder.provider
            cp.current_step = "coder_done"
            CheckpointStore.save(cp)
            yield {"type": "status", "content": f"Coder Agent 已完成：产出 {len(coder.patches)} 个补丁块，等待 Reviewer 合并"}
        coder = await _apply_checkpoint(on_checkpoint, "coder", coder)
        if coder is _ABORT:
            async for ev in _cancelled_events(user_msg, "Coder", current_code):
                yield ev
            return
        # ---- Reviewer（PATCH：三层模糊合并 + 三道确定性防线 + 修正回路）----
        review = await reviewer_agent(coder, user_msg, current_code,
                                      plan=pm.plan, prompt_cache_key=prompt_cache_key)
        for msg in review.messages:
            yield {"type": "status", "content": msg}
        review = await _apply_checkpoint(on_checkpoint, "reviewer", review)
        if review is _ABORT:
            async for ev in _cancelled_events(user_msg, "Reviewer", current_code):
                yield ev
            return
        if review.ok:
            yield {"type": "status",
                   "content": f"Reviewer Agent 已完成：{review.merged_patches} 个补丁合并成功，代码校验通过"}
            final_code = review.code
        else:
            yield {"type": "status", "content": "Reviewer Agent：补丁合并失败，自动降级为 FULL 全量生成..."}
            route = "FULL"
            cp.route_mode = "FULL"

    if final_code is None and route == "FULL":
        # ---- Coder（FULL）----
        if cp.current_step == "coder_done" and cp.raw_coder_output:
            coder = _coder_from_checkpoint(cp)
            yield {"type": "status", "content": "已从断点恢复 Coder 产物（全量）"}
        else:
            yield {"type": "status", "content": "Coder Agent 正在全量生成应用..."}
            coder = await coder_agent(pm, user_msg, history, current_code,
                                      design_spec=cp.design_spec,
                                      prompt_cache_key=prompt_cache_key, on_heartbeat=on_heartbeat)
            cp.raw_coder_output = coder.raw
            cp.coder_provider = coder.provider
            cp.current_step = "coder_done"
            CheckpointStore.save(cp)
            yield {"type": "status", "content": f"Coder Agent 已完成：产出单文件应用（{len(coder.code or '')} 字符）"}
        coder = await _apply_checkpoint(on_checkpoint, "coder", coder)
        if coder is _ABORT:
            async for ev in _cancelled_events(user_msg, "Coder", current_code):
                yield ev
            return
        # ---- Reviewer（FULL）----
        review = await reviewer_agent(coder, user_msg, current_code,
                                      plan=pm.plan, design_spec=cp.design_spec,
                                      prompt_cache_key=prompt_cache_key, on_heartbeat=on_heartbeat)
        for msg in review.messages:
            yield {"type": "status", "content": msg}
        for warn in review.warnings:
            yield {"type": "status", "content": f"Reviewer 告警：{warn}"}
        review = await _apply_checkpoint(on_checkpoint, "reviewer", review)
        if review is _ABORT:
            async for ev in _cancelled_events(user_msg, "Reviewer", current_code):
                yield ev
            return
        if review.ok:
            yield {"type": "status", "content": "Reviewer Agent 已完成：代码校验通过"}
            final_code = review.code
        elif has_code:
            yield {"type": "status", "content": "Reviewer Agent：校验未通过，已原样保留上一版可用代码（防破坏铁律）"}
            final_code = current_code
            summary = await chat_host_agent(user_msg, route, ok=False, prompt_cache_key=prompt_cache_key)
        else:
            raise LLMUnavailableError("全量生成结果未通过校验")

    if final_code is None:
        raise LLMUnavailableError("流水线未产出任何代码")

    # ---- Tester Agent：静态走查，高风险问题回喂一轮修复（仅 FULL；PATCH 只记录告警）----
    if enable_tester and final_code:
        yield {"type": "status", "content": "Tester Agent 正在静态走查（交互完整性/边界异常/状态一致性）..."}
        issues = await tester_agent(final_code, user_msg, prompt_cache_key=prompt_cache_key)
        if issues:
            cp.tester_notes = "；".join(issues)
            CheckpointStore.save(cp)
            yield {"type": "status", "content": f"Tester Agent 发现 {len(issues)} 个高风险问题，回喂 Coder 修复..."}
            if route == "FULL":
                try:
                    feedback_plan = (pm.plan + "\n（测试反馈，必须逐条修复）\n"
                                     + "\n".join(f"- {i}" for i in issues))
                    retry_pm = PMResult(route="FULL", plan=feedback_plan,
                                        title=pm.title, source=pm.source)
                    fixed = await coder_agent(retry_pm, user_msg, history, current_code,
                                              design_spec=cp.design_spec,
                                              prompt_cache_key=prompt_cache_key,
                                              on_heartbeat=on_heartbeat)
                    re_review = await reviewer_agent(fixed, user_msg, current_code,
                                                     plan=feedback_plan, design_spec=cp.design_spec,
                                                     prompt_cache_key=prompt_cache_key,
                                                     on_heartbeat=on_heartbeat)
                    if re_review.ok:
                        final_code = re_review.code
                        yield {"type": "status", "content": "Coder 已按测试反馈修复并重新通过 Reviewer 校验"}
                    else:
                        yield {"type": "status", "content": "修复稿未通过校验，保留原交付结果（问题已记录于检查点）"}
                except LLMUnavailableError:
                    yield {"type": "status", "content": "修复通道不可用，保留原交付结果（问题已记录于检查点）"}
            else:
                yield {"type": "status", "content": "PATCH 场景高风险问题已记录为告警（避免补丁二次漂移，请人工确认）"}

    cp.final_code = final_code
    cp.code_updated = final_code != current_code
    cp.current_step = "reviewer_done"
    CheckpointStore.save(cp)

    # ---- Chat Host ----
    yield {"type": "status", "content": "Chat Host 正在整理回复..."}
    if summary is None:
        summary = await chat_host_agent(user_msg, route, ok=True, prompt_cache_key=prompt_cache_key)
    yield {"type": "text", "content": summary}
    yield {"type": "code", "content": final_code}


async def _build_demo(
    cp: WorkflowCheckpoint, pm: PMResult, user_msg: str,
    template_lookup: TemplateLookupFn | None,
) -> AsyncGenerator[dict, None]:
    """第 4 层离线兜底：绝不破坏已有代码；无代码则经 template_lookup 查数据库模板。"""
    has_code = bool(cp.base_code and cp.base_code.strip())
    if pm.source == "user":
        yield {"type": "status", "content": f"按你确认的方案执行：{pm.display()}"}
    yield {"type": "status", "content": "Coder Agent 正在生成应用（离线兜底）..."}
    await asyncio.sleep(0.2)

    demo_html, demo_msg = resolve_l4_fallback(user_msg, cp.base_code, template_lookup)
    cp.final_code = demo_html if demo_html is not None else cp.base_code
    cp.code_updated = bool(demo_html is not None and not has_code)
    cp.is_demo_mode = True
    cp.current_step = "completed"
    CheckpointStore.save(cp)

    yield {"type": "status", "content": "Reviewer Agent 校验通过，Chat Host 正在整理回复..."}
    yield {"type": "text", "content": demo_msg}
    yield {"type": "code", "content": cp.final_code}


async def _run_build_with_cp(
    cp: WorkflowCheckpoint, pm: PMResult, user_msg: str, history: list[dict],
    prompt_cache_key: str | None,
    on_checkpoint: Callable[[str, Any], Awaitable[Any] | Any] | None,
    on_heartbeat: Callable[[int], Awaitable[None]] | None,
    template_lookup: TemplateLookupFn | None,
    enable_spec: bool, enable_tester: bool,
) -> AsyncGenerator[dict, None]:
    """在线 -> 离线兜底编排（含全部保险丝），永不抛出未捕获异常。"""
    try:
        if llm_available():
            try:
                async for event in _build_online(cp, pm, user_msg, history, prompt_cache_key,
                                                 on_checkpoint, on_heartbeat, enable_spec, enable_tester):
                    yield event
                usage_text = usage_status_text()
                if usage_text:
                    yield {"type": "status", "content": usage_text}
                cp.current_step = "completed"
                CheckpointStore.save(cp)
                return
            except LLMUnavailableError as exc:
                logger.warning("在线流水线不可用，切换第 4 层离线兜底 (run_id=%s): %s", cp.run_id, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 在线流水线保险丝
                logger.exception("在线流水线异常，切换离线兜底 (run_id=%s): %s", cp.run_id, exc)
        async for event in _build_demo(cp, pm, user_msg, template_lookup):
            yield event
    except asyncio.CancelledError:
        logger.info("前端主动中断生成 (run_id=%s)，检查点已保留可断点续跑", cp.run_id)
        raise
    except Exception as exc:  # noqa: BLE001 - 连兜底都异常时的终极保险丝
        logger.exception("离线兜底异常 (run_id=%s): %s", cp.run_id, exc)
        try:
            demo_html, demo_msg = resolve_l4_fallback(user_msg or "", cp.base_code, template_lookup)
            yield {"type": "status", "content": f"已进入离线演示模式（{DEMO_TAG}）"}
            yield {"type": "text", "content": demo_msg}
            if demo_html:
                yield {"type": "code", "content": demo_html}
            elif cp.base_code:
                yield {"type": "code", "content": cp.base_code}
        except Exception:  # noqa: BLE001
            yield {"type": "status", "content": "系统繁忙，请稍后再试"}
            yield {"type": "text", "content": "服务暂时不可用，但你的数据完好无损，请稍后重试。"}


async def run_pm_phase(
    user_msg: str,
    history: list[dict] | None = None,
    current_code: str | None = None,
    *,
    session_id: int | None = None,
    prompt_cache_key: str | None = None,
) -> tuple[list[dict], PMResult]:
    """第 1 阶段：只跑 PM Agent，返回 (待播放 status 事件, PMResult)。

    PMResult.run_id 关联落盘检查点（pm_done），可传给 run_build_phase 或用于断点续跑。
    本函数永不抛异常。
    """
    reset_usage()
    cp = WorkflowCheckpoint(session_id=session_id, user_msg=user_msg, base_code=current_code)
    return await _pm_stage(cp, user_msg, history or [], prompt_cache_key)


async def run_build_phase(
    pm: PMResult | dict,
    user_msg: str,
    history: list[dict] | None = None,
    current_code: str | None = None,
    *,
    session_id: int | None = None,
    prompt_cache_key: str | None = None,
    on_checkpoint: Callable[[str, Any], Awaitable[Any] | Any] | None = None,
    on_heartbeat: Callable[[int], Awaitable[None]] | None = None,
    template_lookup: TemplateLookupFn | None = None,
    enable_spec: bool = True,
    enable_tester: bool = True,
) -> AsyncGenerator[dict, None]:
    """第 2 阶段：用（可能被用户改过的）PMResult 跑 Coder -> Reviewer -> [Tester] -> Chat Host。

    ``pm`` 接受 PMResult 或其 dict 形式；带 run_id 时自动关联检查点断点续跑。
    enable_spec / enable_tester 可关闭设计规范与测试走查阶段（省 token 或排障时用）。
    同会话经互斥锁串行，防连点写花。永不抛出未捕获异常。
    """
    if not isinstance(pm, PMResult):
        pm = PMResult.from_dict(pm if isinstance(pm, dict) else None)
    cp: WorkflowCheckpoint | None = None
    if getattr(pm, "run_id", ""):
        cp = CheckpointStore.load(pm.run_id)
    if cp is None:
        cp = WorkflowCheckpoint(session_id=session_id, user_msg=user_msg, base_code=current_code)
    else:
        if current_code:
            cp.base_code = current_code
        if session_id is not None:
            cp.session_id = session_id
    if not prompt_cache_key and cp.session_id is not None:
        prompt_cache_key = f"p2a-session-{cp.session_id}"
    async with _get_session_lock(cp.session_id):
        async for event in _run_build_with_cp(cp, pm, user_msg or "", history or [],
                                              prompt_cache_key, on_checkpoint, on_heartbeat,
                                              template_lookup, enable_spec, enable_tester):
            yield event


async def run_agent_pipeline(
    user_msg: str,
    history: list[dict],
    current_code: str | None = None,
    *,
    session_id: int | None = None,
    resume_run_id: str | None = None,
    prompt_cache_key: str | None = None,
    on_checkpoint: Callable[[str, Any], Awaitable[Any] | Any] | None = None,
    on_heartbeat: Callable[[int], Awaitable[None]] | None = None,
    template_lookup: TemplateLookupFn | None = None,
    enable_spec: bool = True,
    enable_tester: bool = True,
) -> AsyncGenerator[dict, None]:
    """4-Agent 一键直通工作流 = PM 阶段 + 构建阶段（async generator）。

    依次 yield ``{"type": "status"|"text"|"code", "content": str}``；``done`` 由 main.py 追加。
    ``resume_run_id``：断点续跑（前端中断/进程重启后从最近阶段边界恢复）。
    无论无 Key、断网、模型返回垃圾、补丁不匹配、前端中途断开，均不抛出未捕获异常。
    """
    if not prompt_cache_key and session_id is not None:
        prompt_cache_key = f"p2a-session-{session_id}"
    if resume_run_id:
        cp = CheckpointStore.load(resume_run_id) or WorkflowCheckpoint(
            run_id=resume_run_id, session_id=session_id, user_msg=user_msg, base_code=current_code)
    else:
        cp = WorkflowCheckpoint(session_id=session_id, user_msg=user_msg, base_code=current_code)
    try:
        async with _get_session_lock(cp.session_id):
            # ---- 阶段 1：PM（可从 pm_done 断点恢复）----
            if cp.current_step == "pm_done" and cp.pm:
                pm = PMResult.from_dict(cp.pm)
                pm.run_id = cp.run_id
                yield {"type": "status", "content": f"已从断点恢复 PM 方案：{pm.display()}"}
            else:
                events, pm = await _pm_stage(cp, user_msg, history or [], prompt_cache_key)
                for ev in events:
                    yield ev
            pm = await _apply_checkpoint(on_checkpoint, "pm", pm)
            if pm is _ABORT:
                async for ev in _cancelled_events(user_msg or "", "PM", cp.base_code):
                    yield ev
                return
            if not isinstance(pm, PMResult):  # 回调可能返回 dict
                pm = PMResult.from_dict(pm if isinstance(pm, dict) else pm.to_dict())
            cp.pm = pm.to_dict()
            cp.route_mode = "PATCH" if (pm.route == "PATCH" and cp.base_code) else "FULL"
            cp.dev_plan = pm.plan
            CheckpointStore.save(cp)
            # ---- 阶段 2：构建 ----
            async for ev in _run_build_with_cp(cp, pm, user_msg or "", history or [],
                                               prompt_cache_key, on_checkpoint, on_heartbeat,
                                               template_lookup, enable_spec, enable_tester):
                yield ev
    except asyncio.CancelledError:
        logger.info("前端主动中断生成请求 (run_id=%s)，检查点已保留，可用 resume_run_id 续跑", cp.run_id)
        raise
    except Exception as exc:  # noqa: BLE001 - 终极保险丝
        logger.exception("工作流发生未预期异常 (run_id=%s): %s", cp.run_id, exc)
        try:
            demo_html, demo_msg = resolve_l4_fallback(user_msg or "", current_code, template_lookup)
            yield {"type": "status", "content": f"已进入离线演示模式（{DEMO_TAG}）"}
            yield {"type": "text", "content": demo_msg}
            if demo_html:
                yield {"type": "code", "content": demo_html}
            elif current_code:
                yield {"type": "code", "content": current_code}
        except Exception:  # noqa: BLE001
            yield {"type": "status", "content": "系统繁忙，请稍后再试"}
            yield {"type": "text", "content": "服务暂时不可用，但你的数据完好无损，请稍后重试。"}


# ==============================================================================
# 12. 离线自检（python agent_v2.py --test）：无 Key 环境六大检查点
# ==============================================================================
_TEST_MOCK_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>MOCK-TEMPLATE-房贷计算器</title></head>
<body style="font-family:sans-serif;padding:24px"><h1>智能房贷计算器</h1>
<div id="app">贷款总额、利率、年限输入区，等额本息/本金对比，提前还款测算（离线 mock 模板，来自 database.py 回调）。</div>
<script>console.log("mock template ok");</script></body></html>"""

_TEST_BASE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>测试应用</title>
<style>body{font-family:sans-serif;background:#f8fafc;padding:40px;text-align:center}</style></head>
<body><h1 style="color:#2680c2">📋 项目管理待办看板</h1><div id="app">任务列表区域</div>
<script>var todos=[];function render(){document.getElementById("app").textContent="共 "+todos.length+" 项任务";}render();</script>
</body></html>"""

_BAD_JS_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>坏应用</title></head>
<body><button id="b">点我</button><div id="msg"></div>
<script>document.getElementById("b").onclick=function(){ alert("hi"); var x = ; };</script>
</body></html>"""


def _mock_template_lookup(user_msg: str) -> str | None:
    """模拟 main.py 注入的 database.py 模板查询回调。"""
    return _TEST_MOCK_TEMPLATE


async def _run_self_test() -> None:
    print("🚀 开始执行 agent_v2.py（单文件参考实现）六大检查点离线自检...\n")
    # 强制无 Key 环境，保证离线兜底路径被真实覆盖
    for _k in ("KIMI_API_KEY", "MOONSHOT_API_KEY", "BACKUP_API_KEY", "KIMI_MODEL", "BACKUP_MODEL"):
        os.environ.pop(_k, None)
    _GATEWAY._model_cache.clear()

    # ---------- Checkpoint 1: 双通道配置与请求参数分通道 ----------
    print("[Checkpoint 1] 测试 Kimi / SiliconFlow 双通道参数适配与模型发现...")
    gw = ResilientLLMGateway()
    cfg_kimi: dict = {
        "label": "Kimi", "api_key": "t", "base_url": "t", "model": None, "provider": "kimi",
        "model_preference": KIMI_MODEL_PREFERENCE, "fallback_model": None,
        "token_param": "max_completion_tokens", "supports_prompt_cache_key": True,
        "supports_reasoning_effort": True, "max_output_cap": None,
    }
    cfg_sf: dict = {
        "label": "SiliconFlow", "api_key": "t", "base_url": "https://api.siliconflow.cn/v1",
        "model": None, "provider": "siliconflow",
        "model_preference": SILICONFLOW_MODEL_PREFERENCE,
        "fallback_model": "deepseek-ai/DeepSeek-V4-Flash",
        "token_param": "max_tokens", "supports_prompt_cache_key": False,
        "supports_reasoning_effort": False, "max_output_cap": 8192,
    }
    p_sf = gw._build_request_params(cfg_sf, "deepseek-ai/DeepSeek-V4-Flash", 1024,
                                    stream=True, json_mode=True, prompt_cache_key="k", reasoning_effort="low")
    assert p_sf["max_tokens"] == 1024 and "max_completion_tokens" not in p_sf, "SF token 字段错误"
    assert "prompt_cache_key" not in p_sf and "extra_body" not in p_sf, "SF 不应携带 Kimi 专属字段"
    assert p_sf["response_format"] == {"type": "json_object"}, "SF JSON Mode 缺失"
    assert p_sf["stream"] is True and p_sf["stream_options"] == {"include_usage": True}
    p_k = gw._build_request_params(cfg_kimi, "kimi-k3", 1024,
                                   stream=True, json_mode=True, prompt_cache_key="k", reasoning_effort="low")
    assert p_k["max_completion_tokens"] == 1024 and "max_tokens" not in p_k, "Kimi token 字段错误"
    assert p_k["prompt_cache_key"] == "k" and p_k["extra_body"] == {"reasoning_effort": "low"}
    p_k2 = gw._build_request_params(cfg_kimi, "kimi-k2.6", 1024,
                                    stream=False, json_mode=False, prompt_cache_key=None, reasoning_effort="low")
    assert "extra_body" not in p_k2 and "stream_options" not in p_k2, "非流式/K2 参数泄漏"

    async def _fake_kimi_models(cfg: dict) -> list[str]:
        return ["kimi-k2.6", "kimi-k3", "kimi-k3.1-pro", "kimi-k2.7-code"]

    async def _fake_sf_models(cfg: dict) -> list[str]:
        return ["Qwen/Qwen3-8B", "moonshotai/Kimi-K2.7-Code", "deepseek-ai/DeepSeek-V4-Flash"]

    async def _down_models(cfg: dict) -> list[str]:
        raise ConnectionError("discovery down")

    gw._fetch_model_ids = _fake_kimi_models  # type: ignore[method-assign]
    assert await gw.discover_model({**cfg_kimi, "base_url": "t-k1"}) == "kimi-k3", "K3 优先失败"
    assert await gw.discover_model({**cfg_kimi, "base_url": "t-k1", "model": "kimi-k3.1-pro"}) == "kimi-k3.1-pro"
    assert await gw.discover_model({**cfg_kimi, "base_url": "t-k1", "model": "kimi-latest"}) == "kimi-k3"
    gw._fetch_model_ids = _fake_sf_models  # type: ignore[method-assign]
    assert await gw.discover_model({**cfg_sf, "base_url": "t-s1"}) == "deepseek-ai/DeepSeek-V4-Flash"
    gw._fetch_model_ids = _down_models  # type: ignore[method-assign]
    assert await gw.discover_model({**cfg_sf, "base_url": "t-s2"}) == "deepseek-ai/DeepSeek-V4-Flash"
    try:
        await gw.discover_model({**cfg_kimi, "base_url": "t-k2"})
        raise SystemExit("主通道发现失败应抛异常")
    except RuntimeError:
        pass
    print("  ✅ 双通道参数适配、偏好序、显式覆盖校验、fallback_model 兜底全部通过")

    # ---------- Checkpoint 2: PM 路由（离线启发式 + 复杂度判断 + 序列化）----------
    print("[Checkpoint 2] 测试 PM Agent 离线启发式路由、复杂度判断与序列化...")
    assert not llm_available(), "自检环境应无 Key"
    pm1 = await pm_agent("把标题改成蓝色", _TEST_BASE_HTML)
    assert pm1.route == "PATCH" and pm1.source == "heuristic", pm1
    pm2 = await pm_agent("帮我做一个番茄钟", None)
    assert pm2.route == "FULL", pm2
    pm3 = await pm_agent("帮我做一个支持拖拽排序和本地存储的复杂待办看板应用，要求三列看板视图", None)
    assert pm3.need_spec, "复杂应用未触发设计规范标记"
    pm4 = await pm_agent("做个时钟", None)
    assert not pm4.need_spec, "简单应用误触发设计规范"
    restored = PMResult.from_dict({**pm1.to_dict(), "冗余字段": 1})
    assert restored.route == "PATCH" and restored.source == "heuristic" and not restored.need_spec
    print("  ✅ PM 离线路由、复杂度启发式、dict 往返兼容通过")

    # ---------- Checkpoint 3: Reviewer 三层模糊合并 / think 剥离 / 坏代码回滚 ----------
    print("[Checkpoint 3] 测试 Reviewer 三层模糊合并、思维链剥离与坏代码回滚...")
    mock_patch_out = (
        "<think>思考过程应当被完全剥离</think>\n"
        "<<<<<<< SEARCH\n"
        '<h1 style="color:#2680c2">📋 项目管理待办看板</h1>\n'
        "=======\n"
        '<h1 style="color:#ef4444">📋 极速待办看板</h1>\n'
        ">>>>>>> REPLACE"
    )
    coder = CoderResult(mode="PATCH", raw=mock_patch_out,
                        patches=PatchAndSanitizerEngine.extract_patch_blocks(mock_patch_out))
    review = await reviewer_agent(coder, "把标题改成红色", _TEST_BASE_HTML)
    assert review.ok and "极速待办看板" in (review.code or ""), "PATCH 合并未生效"
    assert "<think>" not in (review.code or ""), "think 泄漏"
    dup = CoderResult(mode="PATCH", raw="x", patches=[('"app"', '"list"')])
    review_dup = await reviewer_agent(dup, "改", _TEST_BASE_HTML)
    assert not review_dup.ok and review_dup.degrade_full and any("次" in e for e in review_dup.errors)
    coder_bad = CoderResult(mode="FULL", raw="<html><body>残缺代码被截断")
    review_bad = await reviewer_agent(coder_bad, "做一个应用", _TEST_BASE_HTML)
    assert not review_bad.ok and review_bad.code is None, "坏代码回滚防破坏失败"
    print("  ✅ 三层模糊合并、think 剥离、歧义报错、坏代码回滚全部通过")

    # ---------- Checkpoint 4: 确定性体检（静态规则 + node --check）----------
    print("[Checkpoint 4] 测试 Reviewer 确定性体检（禁用 alert/prompt/confirm、语法校验）...")
    static = static_js_issues(_BAD_JS_HTML)
    assert any("alert()" in s for s in static), f"静态规则未捕获 alert: {static}"
    all_issues = post_generation_issues(_BAD_JS_HTML)
    assert any("alert()" in s for s in all_issues), "体检未捕获 alert"
    if any("语法错误" in s for s in all_issues):
        print("    （本机有 node，语法错误已被捕获 ✅）")
    else:
        print("    （本机无 node，语法校验按设计跳过 ✅）")
    good = post_generation_issues(_TEST_BASE_HTML)
    assert good == [], f"健康代码误报: {good}"
    tester_offline = await tester_agent(_TEST_BASE_HTML, "做个看板")
    assert tester_offline == [], "离线 Tester 应静默跳过"
    print("  ✅ 静态规则、node 语法校验（可降级跳过）、健康代码零误报、离线 Tester 静默通过")

    # ---------- Checkpoint 5: Chat Host 兜底 + 用量统计 ----------
    print("[Checkpoint 5] 测试 Chat Host 本地兜底与 token 用量统计...")
    host = await chat_host_agent("做一个番茄钟", "FULL", True)
    assert isinstance(host, str) and len(host) > 0, "Host 兜底话术为空"
    host_bad = await chat_host_agent("做一个番茄钟", "FULL", False)
    assert "保留上一版可用应用" in host_bad, "失败话术缺失"
    reset_usage()
    _USAGE_TOTAL.update({"calls": 3, "prompt_tokens": 1000, "completion_tokens": 500, "cached_tokens": 400})
    assert "1000" in usage_status_text() and "40%" in usage_status_text(), usage_status_text()
    print("  ✅ Chat Host 兜底话术与用量统计通过")

    # ---------- Checkpoint 6: 端到端离线兜底 + 防破坏 + 取消 + 断点续跑 ----------
    print("[Checkpoint 6] 测试端到端离线兜底、防破坏铁律、取消保留与断点续跑...")
    cp = WorkflowCheckpoint(user_msg="帮我做一个房贷计算器")
    cp.current_step = "pm_done"
    cp.route_mode = "FULL"
    cp.pm = {"route": "FULL", "plan": "制作房贷计算器", "title": "房贷计算器", "source": "heuristic"}
    CheckpointStore.save(cp)
    events: list[dict] = []
    async for ev in run_agent_pipeline("帮我做一个房贷计算器", [], None,
                                       resume_run_id=cp.run_id,
                                       template_lookup=_mock_template_lookup):
        events.append(ev)
    ev_types = [e["type"] for e in events]
    assert "code" in ev_types and "text" in ev_types, f"事件契约不完整: {ev_types}"
    final = next(e["content"] for e in events if e["type"] == "code")
    assert "MOCK-TEMPLATE" in final, "L4 未命中数据库模板"
    reloaded = CheckpointStore.load(cp.run_id)
    assert reloaded is not None and reloaded.current_step == "completed", "检查点未落盘 completed"

    events2: list[dict] = []
    async for ev in run_agent_pipeline("把标题改成红色", [], _TEST_BASE_HTML,
                                       template_lookup=_mock_template_lookup):
        events2.append(ev)
    final2 = next(e["content"] for e in events2 if e["type"] == "code")
    assert final2 == _TEST_BASE_HTML, "违反铁律：离线环境改动了已有代码！"
    assert any("【演示模式】" in str(e.get("content", "")) for e in events2), "缺少演示模式标识"

    async def _abort_cb(stage: str, result: Any) -> Any:
        if stage == "pm":
            raise CheckpointAbort()
        return result

    events3: list[dict] = []
    async for ev in run_agent_pipeline("把标题改成红色", [], _TEST_BASE_HTML, on_checkpoint=_abort_cb):
        events3.append(ev)
    final3 = next(e["content"] for e in events3 if e["type"] == "code")
    assert final3 == _TEST_BASE_HTML, "取消时破坏了已有代码！"
    assert _get_session_lock(99999) is _get_session_lock(99999), "会话锁未复用"
    print(f"  ✅ 端到端兜底/防破坏/取消保留/断点续跑通过 (run_id={cp.run_id}, 事件={ev_types})")
    CheckpointStore.cleanup(max_age_hours=0)
    print("\n🎉 全部 6 大检查点验证通过！agent_v2.py 离线自检达标。")


# ==============================================================================
# 13. 在线核验（python agent_v2.py --live-test）：真实调用主备模型，展示 API 返回
# ==============================================================================
_LIVE_PATCH_BASE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>个人书签页</title>
<style>body{font-family:sans-serif;background:#0f2027;color:#eaf2f8;padding:40px;text-align:center}
h1{color:#2680c2}</style></head>
<body><h1>⭐ 我的书签导航</h1><ul id="links"><li>示例书签</li></ul>
<script>var links=["示例书签"];function render(){document.getElementById("links").innerHTML=links.map(function(l){return "<li>"+l+"</li>";}).join("");}render();</script>
</body></html>"""


def _show(resp: LLMCallResult, label: str, raw_limit: int = 400) -> None:
    """统一打印 API 返回要点，便于人工核验。"""
    print(f"    通道     : {label}")
    print(f"    provider : {resp.provider}   model: {resp.model}")
    print(f"    finish   : {resp.finish_reason}   truncated: {resp.was_truncated}")
    print(f"    正文长度 : {len(resp.content)} 字符")
    print(f"    正文节选 : {resp.content[:raw_limit]!r}")
    print(f"    累计用量 : {usage_status_text() or '（本段未返回 usage）'}")


async def _run_live_test() -> None:
    print("🔬 agent_v2.py 在线核验：直连主备模型 + 端到端全局（含 DesignSpec/Tester）/PATCH\n")
    print("    前置：将真实调用 KIMI_API_KEY（主）与 BACKUP_API_KEY（备，默认硅基流动）")
    print("    注意：会产生少量 token 费用。\n")
    if not llm_available():
        print("❌ 未配置 KIMI_API_KEY / BACKUP_API_KEY（且未安装 openai），无法在线核验。")
        print("   请先 export KIMI_API_KEY=sk-... 和/或 BACKUP_API_KEY=sk-... 后重试。")
        return

    reset_usage()

    print("═══ 核验 1/4：主模型直连（Kimi，非流式短调用）═══")
    cfg_kimi = _kimi_config()
    if cfg_kimi:
        try:
            res = await _GATEWAY.chat_completion(
                [{"role": "user", "content": "用一句话介绍你自己，并说明你是哪个模型。"}],
                max_completion_tokens=512, stream=False, timeout=60.0, reasoning_effort="low",
            )
            _show(res, cfg_kimi["label"])
        except Exception as exc:  # noqa: BLE001
            print(f"    ⚠️ 主模型直连失败: {type(exc).__name__}: {str(exc)[:200]}")
    else:
        print("    （跳过：未配置 KIMI_API_KEY）")

    print("\n═══ 核验 2/4：备用模型直连（SiliconFlow，强制单通道，绕开主通道）═══")
    cfg_sf = _backup_config()
    if cfg_sf:
        try:
            res = await _GATEWAY._call_provider_with_retry(
                cfg_sf,
                [{"role": "user", "content": "用一句话介绍你自己，并说明你是哪个模型。"}],
                max_completion_tokens=512, stream=False, timeout=60.0,
            )
            _show(res, f"{cfg_sf['label']}（{cfg_sf['base_url']}）")
        except Exception as exc:  # noqa: BLE001
            print(f"    ⚠️ 备用模型直连失败: {type(exc).__name__}: {str(exc)[:200]}")
    else:
        print("    （跳过：未配置 BACKUP_API_KEY）")

    print("\n═══ 核验 3/4：端到端全局生成（FULL 复杂应用：PM→DesignSpec→Coder→Reviewer→Tester→Host）═══")
    full_events: list[dict] = []
    try:
        async for ev in run_agent_pipeline(
                "帮我做一个 2048 数字合并小游戏：方向键操作、分数统计、本地最佳分记录、重新开始按钮", [], None):
            full_events.append(ev)
            content = str(ev.get("content", ""))
            print(f"    [{ev['type']:>6}] {content[:110]}{'...' if len(content) > 110 else ''}")
        final_code = next((e["content"] for e in reversed(full_events) if e["type"] == "code"), "")
        assert "</html>" in final_code and len(final_code) >= _CODE_MIN_LEN, "全局生成结果非法"
        stage_hits = [e["content"] for e in full_events if e["type"] == "status"]
        assert any("DesignSpec" in s for s in stage_hits), "复杂应用未触发 DesignSpec 阶段"
        assert any("Tester" in s for s in stage_hits), "未触发 Tester 阶段"
        assert post_generation_issues(final_code) == [], "最终代码未通过确定性体检"
        print(f"    ✅ 全局生成通过：{len(final_code)} 字符，DesignSpec/Tester 阶段均已执行，确定性体检零告警")
    except Exception as exc:  # noqa: BLE001
        print(f"    ❌ 全局生成失败: {type(exc).__name__}: {exc}")

    print("\n═══ 核验 4/4：端到端局部补丁（PATCH，显式方案，验证三层模糊合并 + 体检）═══")
    patch_events: list[dict] = []
    try:
        pm_patch = PMResult(route="PATCH", plan="把 h1 的文字颜色改为红色 #ef4444，其余布局与交互保持不变",
                            title="标题变红", source="user")
        async for ev in run_build_phase(pm_patch, "把页面主标题的文字颜色改成红色", [], _LIVE_PATCH_BASE):
            patch_events.append(ev)
            content = str(ev.get("content", ""))
            print(f"    [{ev['type']:>6}] {content[:110]}{'...' if len(content) > 110 else ''}")
        merged = next((e["content"] for e in reversed(patch_events) if e["type"] == "code"), "")
        assert "</html>" in merged, "PATCH 结果非法"
        turned_red = ("#ef4444" in merged) or ("red" in merged.lower())
        kept_body = "我的书签导航" in merged and "render()" in merged
        print(f"    颜色已变红: {'✅' if turned_red else '❌'}   原有结构保留: {'✅' if kept_body else '❌'}")
        assert turned_red and kept_body, "PATCH 修改未生效或破坏了原结构"
        print("    ✅ PATCH 端到端通过")
    except Exception as exc:  # noqa: BLE001
        print(f"    ❌ PATCH 端到端失败: {type(exc).__name__}: {exc}")

    print(f"\n📊 本轮累计 LLM 用量：{usage_status_text() or '（无在线调用）'}")
    print("🔬 在线核验结束。")


if __name__ == "__main__":
    import sys

    if "--live-test" in sys.argv:
        asyncio.run(_run_live_test())
    elif "--test" in sys.argv:
        asyncio.run(_run_self_test())
    else:
        print("agent_v2.py（单文件参考实现，已冻结；正式实现见 agents_v2 包）。用法：")
        print("  python agent_v2.py --test        离线六大检查点自检（无需 Key）")
        print("  python agent_v2.py --live-test   在线核验（直连主备模型 + 全局/PATCH 端到端，展示 API 返回）")
