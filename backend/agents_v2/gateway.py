"""四层容灾 LLM 网关：L1 指数退避重试 -> L2 Kimi 官方 -> L3 硅基流动 SiliconFlow。

SiliconFlow 通道严格依据其官方文档（api-docs.siliconflow.cn/chat-completions-post）：
- token 上限字段为 max_tokens（不含思维链），Kimi 通道为 max_completion_tokens（max_tokens 已弃用）；
- 模型 ID 为「厂商/模型名」格式，经官方 GET /v1/models 动态发现，发现异常时回退官方示例模型
  deepseek-ai/DeepSeek-V4-Flash（主通道不设兜底，杜绝 404 resource_not_found_error）；
- 无 prompt_cache_key 字段（平台自动统计 prompt_cache_hit_tokens）；
- reasoning_effort 官方仅支持 high/max 且 low/medium 会被映射为 high，低优任务不传；
- 响应中的 reasoning_content 增量单独收集；只出推理不出正文时给出可读报错。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .common import (
    _BACKOFF_BASE, _HEARTBEAT_CHARS, _LLM_STREAM_OPEN_TIMEOUT, _MAX_RETRIES,
    _MODEL_CACHE_TTL, _MODELS_STALE_GRACE, _MODELS_TIMEOUT, logger,
    LLMUnavailableError,
)

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

# Kimi 官方模型偏好序（platform.kimi.com/docs/api）：K3 系列为主力，K2 已公告下线仅作回退。
KIMI_MODEL_PREFERENCE = (
    "kimi-k3", "kimi-k3.1-pro", "kimi-k3.1-pro-highspeed",
    "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6",
)
# 硅基流动偏好序：备用通道刻意不选 Kimi 系（主通道故障时回退同厂商失去容灾意义）。
SILICONFLOW_MODEL_PREFERENCE = (
    "Pro/deepseek-ai/DeepSeek-V4",
    "deepseek-ai/DeepSeek-V4",
    "deepseek-ai/DeepSeek-V4-Flash",
    "Pro/zai-org/GLM-5.2",
)
NON_CHAT_KEYWORDS = ("embed", "audio", "rerank", "tts", "whisper", "vision-only")


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
        "fallback_model": None,
        "token_param": "max_completion_tokens",
        "supports_prompt_cache_key": True,
        "supports_reasoning_effort": True,
        "max_output_cap": None,
    }


def _backup_config() -> dict | None:
    """L3 备用通道，默认硅基流动 SiliconFlow（可用 BACKUP_BASE_URL 指向任意 OpenAI 兼容端点）。"""
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


# ==============================================================================
# Token 用量统计（stream_options.include_usage 聚合）
# ==============================================================================
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
    ttfb_ms: float | None = None       # 模型首 token 时间（None = 未建立流）
    duration_ms: float | None = None   # 调用总耗时
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0             # >0 视为热缓存命中


class ResilientLLMGateway:
    """并发网关：client 缓存 + 模型发现 TTL/锁/过期兜底 + 冷却水位线 + 截断续写。"""

    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}
        self._model_cache: dict[str, tuple[float, list[str]]] = {}
        self._discovery_locks: dict[str, asyncio.Lock] = {}
        self._cooldown_until: dict[str, float] = {}

    # ---- client 缓存 ----
    def _get_client(self, api_key: str, base_url: str) -> AsyncOpenAI:
        cache_key = (api_key, base_url.rstrip("/"))
        client = self._clients.get(cache_key)
        if client is None:
            client = AsyncOpenAI(api_key=api_key, base_url=base_url.rstrip("/"), max_retries=0, timeout=_MODELS_TIMEOUT)
            self._clients[cache_key] = client
        return client

    # ---- 模型发现：GET /v1/models，TTL 300s + 锁 + 过期缓存兜底 + fallback_model ----
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

    # ---- 按供应商分通道构造请求参数 ----
    @staticmethod
    def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": m.get("role", "user"), "content": str(m.get("content", ""))} for m in messages]

    def _build_request_params(
        self, cfg: dict, model: str, max_completion_tokens: int, *,
        stream: bool, json_mode: bool, prompt_cache_key: str | None,
        reasoning_effort: str | None,
    ) -> dict:
        """Kimi 与 SiliconFlow 参数体系不同，按 provider 显式分通道（差异详见模块文档）。"""
        params: dict[str, Any] = {"model": model, "messages": None, "stream": stream}
        if stream:
            params["stream_options"] = {"include_usage": True}
        if json_mode:
            params["response_format"] = {"type": "json_object"}  # 两侧官方文档均支持 JSON Mode
        params[cfg["token_param"]] = max_completion_tokens
        if cfg.get("supports_prompt_cache_key") and prompt_cache_key:
            params["prompt_cache_key"] = prompt_cache_key
        if cfg.get("supports_reasoning_effort") and reasoning_effort and model.startswith("kimi-k3"):
            params["extra_body"] = {"reasoning_effort": reasoning_effort}
        if cfg.get("provider") == "siliconflow":
            # 官方字段：关闭思考模式（默认开启时首 token 前空想可达 3 分钟，Coder 等长生成不可用）。
            # 注意：非标准字段必须经 extra_body 传递（openai SDK 会拒绝未知顶层参数）
            extra = params.get("extra_body") or {}
            extra["enable_thinking"] = False
            params["extra_body"] = extra
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
    ) -> tuple[str, str | None, float | None, dict[str, int]]:
        """返回 (正文, finish_reason, 首token毫秒, 用量快照{prompt,completion,cached})。"""
        call_start = time.monotonic()
        cap = cfg.get("max_output_cap")
        effective_max = max_completion_tokens if not cap else min(max_completion_tokens, cap)
        kwargs = self._build_request_params(
            cfg, model, effective_max, stream=stream, json_mode=json_mode,
            prompt_cache_key=prompt_cache_key, reasoning_effort=reasoning_effort,
        )
        kwargs["messages"] = self._sanitize_messages(messages)
        client = self._get_client(cfg["api_key"], cfg["base_url"])
        ttfb_ms: float | None = None
        usage_stats = {"prompt": 0, "completion": 0, "cached": 0}

        async with _get_llm_semaphore():
            if not stream:
                resp = await asyncio.wait_for(client.chat.completions.create(**kwargs), timeout=timeout)
                _record_usage(getattr(resp, "usage", None))
                u = getattr(resp, "usage", None)
                if u:
                    usage_stats = {
                        "prompt": getattr(u, "prompt_tokens", 0) or 0,
                        "completion": getattr(u, "completion_tokens", 0) or 0,
                        "cached": getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
                    }
                ttfb_ms = (time.monotonic() - call_start) * 1000
                choice = resp.choices[0]
                return getattr(choice.message, "content", None) or "", getattr(choice, "finish_reason", None), ttfb_ms, usage_stats

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
                        if ttfb_ms is None:
                            ttfb_ms = (time.monotonic() - call_start) * 1000  # 首个内容 token 到达
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
            if usage:
                usage_stats = {
                    "prompt": getattr(usage, "prompt_tokens", 0) or 0,
                    "completion": getattr(usage, "completion_tokens", 0) or 0,
                    "cached": getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
                }

        text = "".join(chunks).strip()
        if not text:
            detail = f"（finish_reason={finish_reason}）" if finish_reason else ""
            if reasoning_parts:
                raise ValueError(
                    f"模型仅返回推理内容、未产出正文{detail}：推理 token 按输出计费，"
                    "请增大 max_completion_tokens，或为该调用设置 reasoning_effort='low'"
                )
            raise ValueError(f"模型返回空内容{detail}")
        return text, finish_reason, ttfb_ms, usage_stats

    async def _call_provider_with_retry(
        self, cfg: dict, messages: list[dict[str, Any]], *,
        max_completion_tokens: int, stream: bool, timeout: float,
        json_mode: bool = False, prompt_cache_key: str | None = None,
        reasoning_effort: str | None = None, allow_continuation: bool = False,
        on_heartbeat: Callable[[int], Awaitable[None]] | None = None,
    ) -> LLMCallResult:
        """单通道内：冷却水位线 -> 最多 3 次尝试 -> 截断自动续写 1 次。"""
        call_start = time.monotonic()
        provider_key = cfg["provider"]
        wait_cool = self._cooldown_until.get(provider_key, 0.0) - time.time()
        if 0 < wait_cool <= 6.0:
            await asyncio.sleep(wait_cool)

        model = await self.discover_model(cfg)
        last_exc: Exception | None = None
        content, finish_reason = "", None
        ttfb_ms: float | None = None
        usage_stats = {"prompt": 0, "completion": 0, "cached": 0}

        for attempt in range(_MAX_RETRIES + 1):
            try:
                content, finish_reason, ttfb_ms, usage_stats = await self._execute_single_request(
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
                tail, tail_reason, _tail_ttfb, tail_usage = await self._execute_single_request(
                    cfg, model, cont_msgs, max_completion_tokens=max_completion_tokens,
                    stream=True, timeout=timeout, json_mode=False,
                    prompt_cache_key=prompt_cache_key, reasoning_effort=reasoning_effort,
                    on_heartbeat=on_heartbeat,
                )
                content += tail
                was_truncated = tail_reason == "length"
                for key in usage_stats:
                    usage_stats[key] += tail_usage.get(key, 0)
            except Exception as cont_exc:  # noqa: BLE001
                logger.warning("[%s] 自动续写失败，保留已生成前半部分: %s", cfg["label"], cont_exc)

        return LLMCallResult(
            content=content, provider=provider_key, model=model,
            finish_reason=finish_reason, was_truncated=was_truncated,
            ttfb_ms=ttfb_ms,
            duration_ms=(time.monotonic() - call_start) * 1000,
            prompt_tokens=usage_stats["prompt"], completion_tokens=usage_stats["completion"],
            cached_tokens=usage_stats["cached"],
        )

    async def chat_completion(
        self, messages: list[dict[str, Any]], *,
        max_completion_tokens: int = 1024, stream: bool = True, timeout: float = 120.0,
        json_mode: bool = False, prompt_cache_key: str | None = None,
        reasoning_effort: str | None = None, allow_continuation: bool = False,
        on_heartbeat: Callable[[int], Awaitable[None]] | None = None,
        tag: str = "", channel: str = "auto",
    ) -> LLMCallResult:
        """统一容灾入口：L2 Kimi -> L3 SiliconFlow -> 抛 LLMUnavailableError（交 L4）。

        tag   用于 [timing] 日志标注调用方阶段（pm/coder_full/tester/host...）。
        channel="backup" 时备用通道优先（Coder 等长生成场景：DeepSeek 非重度思考模型出活快），
        备用失败仍自动回落主通道，容灾语义不变。
        """
        if not _OPENAI_OK:
            raise LLMUnavailableError("openai 依赖未安装")
        kimi_cfg, backup_cfg = _kimi_config(), _backup_config()
        if channel == "backup":
            configs = [c for c in (backup_cfg, kimi_cfg) if c]
        else:
            configs = [c for c in (kimi_cfg, backup_cfg) if c]
        if not configs:
            raise LLMUnavailableError("未配置 KIMI_API_KEY / BACKUP_API_KEY")
        errors: list[str] = []
        for cfg in configs:
            try:
                result = await self._call_provider_with_retry(
                    cfg, messages, max_completion_tokens=max_completion_tokens, stream=stream,
                    timeout=timeout, json_mode=json_mode, prompt_cache_key=prompt_cache_key,
                    reasoning_effort=reasoning_effort, allow_continuation=allow_continuation,
                    on_heartbeat=on_heartbeat,
                )
                cache_state = "热缓存" if result.cached_tokens > 0 else "冷缓存"
                logger.info(
                    "[timing] tag=%s provider=%s model=%s ttfb=%.1fs dur=%.1fs in=%d out=%d cached=%d(%s)",
                    tag or "-", result.provider, result.model,
                    (result.ttfb_ms or 0) / 1000, (result.duration_ms or 0) / 1000,
                    result.prompt_tokens, result.completion_tokens,
                    result.cached_tokens, cache_state,
                )
                return result
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
        reasoning_effort: str | None = None, tag: str = "", channel: str = "auto",
    ) -> str:
        """轻量非流式便捷入口（PM/Host/Tester/标题等短调用），返回纯文本。"""
        res = await self.chat_completion(
            messages, max_completion_tokens=max_completion_tokens, stream=False, timeout=60.0,
            json_mode=json_mode, prompt_cache_key=prompt_cache_key, reasoning_effort=reasoning_effort,
            tag=tag, channel=channel,
        )
        return res.content


# 延迟导入避免循环：checkpoints 不依赖 gateway，这里仅为信号量
from .checkpoints import _get_llm_semaphore  # noqa: E402

_GATEWAY = ResilientLLMGateway()
