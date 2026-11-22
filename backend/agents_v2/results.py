"""各 Agent 的结构化阶段产物（阶段间传递 + 检查点展示/修改 + 可序列化暂存）。"""

from __future__ import annotations

from dataclasses import dataclass, field


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
