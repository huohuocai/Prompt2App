"""工作流编排：两阶段 API、一键直通、断点续跑、检查点回调与 L4 离线兜底。

阶段链（FULL 复杂应用）：
  PM 路由 -> [DesignSpec 设计规范] -> Coder -> Reviewer（三道确定性防线+修正回路）
  -> [Tester 静态走查 + 一轮回喂修复] -> Chat Host
PATCH 链路：PM -> Coder(补丁) -> Reviewer(三层模糊合并+体检) -> [Tester 告警记录] -> Chat Host
每个阶段边界原子落盘 WorkflowCheckpoint，支持 resume_run_id 断点续跑。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

from .actors import (
    chat_host_agent, coder_agent, design_spec_agent, pm_agent,
    reviewer_agent, tester_agent, _heuristic_route, mobile_variant_agent,
)
from .checkpoints import (
    CheckpointStore, WorkflowCheckpoint, _get_session_lock,
)
from .common import DEMO_TAG, _CODE_MIN_LEN, logger, CheckpointAbort, LLMUnavailableError
from .compressor import ContextCompressor
from .gateway import _GATEWAY, llm_available, usage_status_text
from .results import CoderResult, PMResult
from .sanitizer import PatchAndSanitizerEngine, post_generation_issues

TemplateLookupFn = Callable[[str], Optional[str]]

# L4 兜底骨架：连数据库回调都不可用时的最后防线，保证事件契约完整
_MINIMAL_SAFE_FALLBACK_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>演示应用</title></head>
<body style="font-family:sans-serif;padding:40px;background:#f8fafc;color:#1e293b">
<h2>演示模式应用预览</h2><p>当前未配置在线大模型 API Key，请从左侧公共广场选择预置交互应用体验，或配置环境变量后重试。</p>
</body></html>"""


def resolve_l4_fallback(user_msg: str, base_code: str | None = None,
                        template_lookup: TemplateLookupFn | None = None) -> tuple[str | None, str]:
    """L4 离线容灾决策：
    1. 已有可用代码 -> 原样保留，绝不破坏用户现有成果；
    2. 无代码 -> 经 template_lookup 回调查数据库匹配预置模板（与前端公共广场同源）；
    3. 未注入回调或查询失败 -> 极简安全骨架，保证零报错。
    """
    if base_code and len(base_code.strip()) >= _CODE_MIN_LEN:
        return None, (f"{DEMO_TAG} 当前未检测到可用在线模型，已为您完整保留当前可用应用，"
                      "也可点击左侧公共广场体验预置应用。")
    if callable(template_lookup):
        try:
            db_html = template_lookup(user_msg)
            if db_html and len(db_html.strip()) >= _CODE_MIN_LEN:
                return db_html, (f"{DEMO_TAG} 当前未检测到可用在线模型，已从公共应用库为您匹配加载本地可交互应用模板。")
        except Exception as exc:  # noqa: BLE001
            logger.warning("调用数据库模板查询回调失败: %s", exc)
    return _MINIMAL_SAFE_FALLBACK_HTML, (f"{DEMO_TAG} 当前未检测到可用在线模型，已为您加载基础演示页"
                                         "（完整预置应用请查看左侧公共广场）。")


# ==============================================================================
# 检查点回调基础设施
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
# PM 阶段
# ==============================================================================
async def _pm_stage(cp: WorkflowCheckpoint, user_msg: str, history: list[dict],
                    prompt_cache_key: str | None) -> tuple[list[dict], PMResult]:
    """PM 阶段：压缩历史 -> 路由（含复杂度判断）-> 落盘 pm_done 检查点。失败自动退化启发式。"""
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


# ==============================================================================
# 在线构建主体
# ==============================================================================
async def _build_online(
    cp: WorkflowCheckpoint, pm: PMResult, user_msg: str, history: list[dict],
    prompt_cache_key: str | None,
    on_checkpoint: Callable[[str, Any], Awaitable[Any] | Any] | None,
    on_heartbeat: Callable[[int], Awaitable[None]] | None,
    enable_spec: bool, enable_tester: bool,
) -> AsyncGenerator[dict, None]:
    """在线流水线主体。每个阶段边界落盘检查点； Tester 高风险问题回喂一轮修复。"""
    current_code = cp.base_code
    has_code = bool(current_code and current_code.strip())
    final_code: str | None = None
    summary: str | None = None
    review = None  # ReviewResult | None；断点恢复直达时可能未经过 Reviewer
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

    # ---- Mobile 变体：基于桌面版产出移动端优先版本（index_mobile.html）----
    # 断点恢复直达（reviewer_done）时跳过：产物文件已在会话文件夹中持久化
    if cp.current_step != "reviewer_done":
        try:
            yield {"type": "status", "content": "Coder Agent 正在生成移动端版本（index_mobile.html）..."}
            mobile_raw = await mobile_variant_agent(final_code, prompt_cache_key=prompt_cache_key,
                                                    on_heartbeat=on_heartbeat)
            mobile_clean = PatchAndSanitizerEngine.strip_thinking_and_fences(mobile_raw)
            m_ok, m_msg = PatchAndSanitizerEngine.validate_html(mobile_clean)
            m_issues = post_generation_issues(mobile_clean) if m_ok else []
            if m_ok and not m_issues:
                yield {"type": "code", "content": mobile_clean, "file": "index_mobile.html",
                       "mode": "PATCH" if route == "PATCH" else "FULL", "note": "移动端版本"}
            else:
                why = m_msg if not m_ok else "；".join(m_issues)
                yield {"type": "status", "content": f"Reviewer：移动端版本未通过体检（{why}），本次仅交付桌面版"}
        except LLMUnavailableError:
            yield {"type": "status", "content": "移动端版本生成不可用，本次仅交付桌面版"}

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
    # code 事件附带版本快照元数据（mode/note），供 main.py 写 app_versions
    yield {"type": "code", "content": final_code,
           "mode": "PATCH" if route == "PATCH" else "FULL",
           "note": f"增量补丁 ×{review.merged_patches}" if (route == "PATCH" and review) else "全量生成"}


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
    yield {"type": "code", "content": cp.final_code,
           "mode": "FALLBACK", "note": "离线演示模式兜底"}


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


# ==============================================================================
# 公共 API：两阶段工作流与一键直通
# ==============================================================================
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
    from .gateway import reset_usage

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
