"""六个独立原子 Agent：PM / DesignSpec / Coder / Reviewer / Tester / ChatHost。

每个函数可单独 import 测试；阶段间以 results.py 的结构化产物传递。
Reviewer 升级为三道确定性防线 + 一轮 LLM 修正回路：
  HTML 结构/缩水门禁 -> JS 静态规则（禁用 alert/prompt/confirm 等）-> node --check 语法校验，
  确定性问题以可读错误回喂 Coder 修正一轮，仍遗留则作为 warnings 放行（不阻塞交付）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from .common import _PATCH_CODE_CAP, logger, LLMUnavailableError
from .gateway import _GATEWAY, llm_available
from .prompts import (
    _CODER_FULL_FIX_SYSTEM, _CODER_FULL_SYSTEM, _CODER_PATCH_SYSTEM,
    _DESIGN_SPEC_SYSTEM, _HOST_SYSTEM,
    _PM_SYSTEM, _REVIEWER_FIX_SYSTEM, _TESTER_SYSTEM,
)
from .results import CoderResult, PMResult, ReviewResult
from .sanitizer import PatchAndSanitizerEngine, post_generation_issues


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


# ==============================================================================
# Agent 1: PM
# ==============================================================================
async def pm_agent(user_msg: str, current_code: str | None = None, *,
                   session_id: int | None = None,
                   prompt_cache_key: str | None = None) -> PMResult:
    """意图路由与需求拆解。在线走 JSON Mode（含复杂度 spec 判断）；失败退化为启发式，绝不抛异常。"""
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
        tag="pm",
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


# ==============================================================================
# Agent 1.5: DesignSpec（复杂 FULL 应用的预生成设计规范）
# ==============================================================================
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
        tag="design_spec", channel="backup",
    )
    return res.content.strip()


# ==============================================================================
# Agent 2: Coder
# ==============================================================================
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
        tag="coder_full", channel="backup",
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
        tag="coder_full_fix", channel="backup",
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
        tag="coder_patch", channel="backup",
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
        tag="reviewer_fix", channel="backup",
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


# ==============================================================================
# Agent 3: Reviewer（三道确定性防线 + 一轮修正回路）
# ==============================================================================
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
    messages: list[str] = []
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


# ==============================================================================
# Agent 3.5: Tester（低推理静态走查，高风险问题回喂修复）
# ==============================================================================
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
        tag="tester",
    )
        data = _parse_json_object(raw) or {}
        issues = data.get("issues") or []
        high = [str(i.get("desc", "")).strip() for i in issues
                if isinstance(i, dict) and i.get("severity") == "high" and i.get("desc")]
        return high
    except Exception as exc:  # noqa: BLE001
        logger.info("Tester Agent 不可用，跳过走查: %s", exc)
        return []


# ==============================================================================
# Agent 4: Chat Host
# ==============================================================================
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
            tag="host",
        )
        text = (text or "").strip().strip('"').strip()
        contains_html_output = bool(
            re.search(r"```(?:html|xhtml)\b[\s\S]*?```", text, re.IGNORECASE)
            or re.search(
                r"(?:<!doctype\s+html\b|<html\b)[\s\S]*?</html\s*>",
                text,
                re.IGNORECASE,
            )
        )
        if text and not contains_html_output:
            return text
        if contains_html_output:
            logger.warning("Chat Host returned HTML instead of a summary; using the safe fallback")
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
        tag="title",
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
