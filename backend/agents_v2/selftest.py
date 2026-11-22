"""自检与在线核验：

- ``python -m backend.agents --test``        离线六大检查点（无需 Key，强制清 Key 环境）
- ``python -m backend.agents --live-test``   在线核验：直连主备模型 + 全局（含 DesignSpec/Tester 阶段）/PATCH 端到端
"""

from __future__ import annotations

import asyncio
import os

from .checkpoints import CheckpointStore, WorkflowCheckpoint, _get_session_lock
from .common import _CODE_MIN_LEN, logger
from .gateway import (
    KIMI_MODEL_PREFERENCE, SILICONFLOW_MODEL_PREFERENCE,
    LLMUnavailableError, ResilientLLMGateway, _GATEWAY, _USAGE_TOTAL,
    _backup_config, _kimi_config, llm_available, reset_usage, usage_status_text,
)
from .pipeline import run_agent_pipeline, run_build_phase
from .results import CoderResult, PMResult
from .sanitizer import PatchAndSanitizerEngine, post_generation_issues, static_js_issues
from .actors import chat_host_agent, pm_agent, reviewer_agent, tester_agent

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
    print("🚀 开始执行 agents_v2 六大检查点离线自检...\n")
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

    async def _abort_cb(stage: str, result) -> object:
        if stage == "pm":
            from .common import CheckpointAbort

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
    print("\n🎉 全部 6 大检查点验证通过！agents_v2 离线自检达标。")


# ==============================================================================
# 在线核验：直连主备模型 + 全局（含 DesignSpec/Tester 阶段）+ PATCH 端到端
# ==============================================================================
_LIVE_PATCH_BASE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>个人书签页</title>
<style>body{font-family:sans-serif;background:#0f2027;color:#eaf2f8;padding:40px;text-align:center}
h1{color:#2680c2}</style></head>
<body><h1>⭐ 我的书签导航</h1><ul id="links"><li>示例书签</li></ul>
<script>var links=["示例书签"];function render(){document.getElementById("links").innerHTML=links.map(function(l){return "<li>"+l+"</li>";}).join("");}render();</script>
</body></html>"""


def _show(resp, label: str, raw_limit: int = 400) -> None:
    """统一打印 API 返回要点，便于人工核验。"""
    print(f"    通道     : {label}")
    print(f"    provider : {resp.provider}   model: {resp.model}")
    print(f"    finish   : {resp.finish_reason}   truncated: {resp.was_truncated}")
    print(f"    正文长度 : {len(resp.content)} 字符")
    print(f"    正文节选 : {resp.content[:raw_limit]!r}")
    print(f"    累计用量 : {usage_status_text() or '（本段未返回 usage）'}")


async def _run_live_test() -> None:
    print("🔬 agents_v2 在线核验：直连主备模型 + 端到端全局（含 DesignSpec/Tester）/PATCH\n")
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


def main() -> None:
    import sys

    if "--live-test" in sys.argv:
        asyncio.run(_run_live_test())
    elif "--test" in sys.argv:
        asyncio.run(_run_self_test())
    else:
        print("agents_v2 自检。用法：")
        print("  python -m backend.agents --test        离线六大检查点（无需 Key）")
        print("  python -m backend.agents --live-test   在线核验（直连主备 + 全局/PATCH，展示 API 返回）")


if __name__ == "__main__":
    main()
