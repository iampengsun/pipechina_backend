"""Production-event investigation output contracts."""

from typing import Literal

from pydantic import Field

from app.modules.operation_event.domain.investigation_schemas import SubInvestigation
from app.ports.models import AssessmentCandidate, StrictProviderOutput


class CoordinatorPlan(StrictProviderOutput):
    """约束协调角色返回的专业角色列表和调查重点。"""

    roles: list[Literal["evidence", "risk", "plan", "critic"]] = Field(
        min_length=4, max_length=4
    )  # 需要包含的专业角色列表
    skills: list[str] = Field(default_factory=list, max_length=3)
    investigations: list[SubInvestigation] = Field(default_factory=list, max_length=3)
    focus: list[str] = Field(default_factory=list, max_length=10)  # 协调角色提出的调查重点


class EvidenceDecision(StrictProviderOutput):
    """约束取证角色下一步查询或结束取证的决策。"""

    action: Literal[
        "search_events",
        "search_work_orders",
        "search_handover",
        "search_knowledge",
        "search_experiences",
        "trace_case_relations",
        "find_recurrent_hazards",
        "assess_change_impact",
        "verify_work_order_outcomes",
        "read_investigation_evidence",
        "get_weather_alerts",
        "get_map_context",
        "query_enterprise_gis",
        "get_access_route",
        "finish",
    ]  # 步骤动作或取证决策
    questions: list[str] = Field(default_factory=list, max_length=10)
    query: str = Field(max_length=100)  # 最长一百字符的普通查询词
    unknowns: list[str] = Field(default_factory=list, max_length=20)  # 尚未确定的事项


class EvidenceReport(StrictProviderOutput):
    """汇总已保存的证据标识、事实文字和未知事项。"""

    evidence_ids: list[str]  # 引用的已保存证据标识
    facts: list[str]  # 证据中的事实文字
    unknowns: list[str]  # 尚未确定的事项


class SubInvestigationDecision(EvidenceDecision):
    conclusion: str = Field(default="", max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=40)
    conflicts: list[str] = Field(default_factory=list, max_length=10)
    resolved_unknowns: list[str] = Field(default_factory=list, max_length=20)
    complete: bool = False


class RiskReport(AssessmentCandidate):
    """约束风险角色输出的分类分级结论与证据引用。"""

    evidence_ids: list[str]  # 引用的已保存证据标识
    unknowns: list[str] = Field(default_factory=list)  # 尚未确定的事项


class PlanReport(StrictProviderOutput):
    """约束方案角色输出的处置建议、优先级和参与职责。"""

    recommended_action: str  # 建议的处置措施
    priority: Literal["low", "medium", "high", "critical"]  # 建议的处置优先级
    required_roles: list[str] = Field(default_factory=list)  # 建议参与处置的职责
    evidence_ids: list[str]  # 引用的已保存证据标识
    high_risk_actions: list[str] = Field(default_factory=list)  # 方案识别的高风险动作


class CriticReport(StrictProviderOutput):
    """约束复核角色的审核结论、问题、冲突和证据引用。"""

    decision: Literal[
        "approved_for_human_review", "needs_revision", "insufficient_evidence"
    ]  # 复核结论
    issues: list[str] = Field(default_factory=list)  # 需要修正的问题
    conflicts: list[str] = Field(default_factory=list)  # 复核发现的冲突
    evidence_ids: list[str]  # 引用的已保存证据标识
