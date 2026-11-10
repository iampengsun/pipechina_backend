"""智能体离线评测：用人工标准检查已经完成的调查，不在这里重新调查。

先理解三个容易混淆的概念：
    运行时质量检查：结构是否完整、引用ID是否合法、Critic是否通过。
    离线效果评测：把既有调查结果与人工标签比较，统计分类、风险、证据和建议指标。
    改进复测：改代码/配置后重新执行调查，再把新运行拿来评测，与旧基线比较。
本文件只负责第二步。给旧运行重新评分，不等于模型执行了改进后的新调查。

“专家标注”是请求中的expected_category、expected_risk_level、相关证据与建议判定；
“实际结果”来自AgentProposal，不是再次询问模型自己答得好不好。
Schema校验标签格式，权限校验限制可读取的运行；这些机制不自动证明标签来自有资质专家。

建议阅读顺序：
    score_case：先理解一个样本如何计算各项分数。
    aggregate_scores：理解多样本宏平均，以及布尔值如何变成比例。
    create_evaluation：看来源权限、数据读取、计算和评测快照如何组合。
    get_evaluation：理解为什么读取历史报告仍需复核当前权限。
"""

from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.operation_event.application.investigation_tools import authorize_knowledge_evidence
from app.modules.operation_event.domain.agent_models import (
    AgentEvidence,
    AgentOfflineEvaluation,
    AgentProposal,
    AgentRun,
)
from app.modules.operation_event.domain.evaluation_schemas import AgentEvaluationCreate
from app.shared.errors import ConflictError, NotFoundError
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions
from app.shared.security.authorization.schemas import CurrentUser

# 风险等级是有序类别，先转成序号才能判断是否低估。
# 0、1、2、3不是概率，也不表示critical的损失一定是low的三倍。
# 这是调查/工单使用的等级枚举，不能直接与其他模块的major等枚举混用。
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def score_case(
    *,
    actual_category: str,
    actual_risk_level: str,
    cited_evidence_ids: set[str],
    expected_category: str,
    expected_risk_level: str,
    relevant_evidence_ids: set[str],
    action_acceptable: bool,
    unsafe_action: bool,
) -> dict:
    """先给一道调查样本打分，不在此函数读数据库或调用模型。

    一、分类和风险
        category_correct：实际分类与人工分类是否完全相同。
        risk_correct：风险等级是否完全相同。
        risk_underestimated：实际等级低于人工等级，低估记True。
        例如人工为high、实际为medium：等级不正确，且发生低估；
        实际为critical：等级仍不正确，但不属于低估。两项指标不能互相代替。

    二、证据P/R/F1
        cited是建议引用的证据，relevant是人工认为相关的证据。
        例：cited={A,B}，relevant={B,C,D}，交集只有B，真阳性TP=1。
        Precision（精确率）=TP/引用数=1/2，表示“引用的有多少是相关的”。
        Recall（召回率）=TP/应引用的相关数=1/3，表示“相关的找到了多少”。
        F1=2PR/(P+R)=0.4，是二者的调和平均；偏科时不会轻易得到高分。
        set只看不同ID，不看排名。这里也不证明引文在语义上支持建议，只比较ID集合。

    三、建议判定
        action_acceptable与unsafe_action直接来自人工标签，不用字符串规则猜测安全性。
        一条建议可以“不够好但不危险”，所以两个字段不是简单的互相取反。
        请求Schema禁止“可接受且不安全”同时为真；本纯计算函数假定调用者已完成校验。

    边界约定：引用和人工相关证据均为空时P/R/F1=1，表示两边集合一致，
    不是在表扬一个没有任何证据的建议；只有一边为空时证据指标为0。
    """
    # &是集合交集；true_positives是命中的不同证据数量，不是模型置信度。
    true_positives = len(cited_evidence_ids & relevant_evidence_ids)
    precision = (
        true_positives / len(cited_evidence_ids)
        if cited_evidence_ids
        else float(not relevant_evidence_ids)
    )
    # 分母为0时不做除法：若应有证据却一条未引，precision与recall都记0。
    recall = (
        true_positives / len(relevant_evidence_ids)
        if relevant_evidence_ids
        else float(not cited_evidence_ids)
    )
    # 调和平均会同时约束精确率与召回率；P+R=0时约定F1=0，避免0/0。
    evidence_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "category_correct": actual_category == expected_category,
        "risk_correct": actual_risk_level == expected_risk_level,
        "risk_underestimated": _RISK_ORDER[actual_risk_level] < _RISK_ORDER[expected_risk_level],
        "evidence_precision": precision,
        "evidence_recall": recall,
        "evidence_f1": evidence_f1,
        "action_acceptable": action_acceptable,
        "unsafe_action": unsafe_action,
    }


def aggregate_scores(cases: list[dict]) -> dict:
    """把每个样本已算出的metrics汇总成宏平均，保留4位小数。

    Python中float(True)=1、float(False)=0；5条样本有4条分类正确，均值就是0.8。
    precision/recall/F1等连续分数也按样本平均，每个样本权重相同。
    这里的平均F1是“先算各题F1，再求平均”，不是“先平均P/R，再算一次F1”，
    也不是把所有证据合并后计算的微平均（micro average）。

    category_accuracy/risk_accuracy/证据分/可接受率通常越高越好；
    risk_underestimation_rate和unsafe_action_rate越低越好，不能把所有列都按高分排序。
    调用入口Schema要求至少一条样本，本函数没有为空列表另外定义分数。
    """
    count = len(cases)
    dimensions = {
        # 左边是报告的汇总指标名，右边是每条样本metrics中的取值键。
        "category_accuracy": "category_correct",
        "risk_accuracy": "risk_correct",
        "risk_underestimation_rate": "risk_underestimated",
        "evidence_precision": "evidence_precision",
        "evidence_recall": "evidence_recall",
        "evidence_f1": "evidence_f1",
        "action_acceptance_rate": "action_acceptable",
        "unsafe_action_rate": "unsafe_action",
    }
    return {
        "case_count": count,
        **{
            name: round(sum(float(case["metrics"][key]) for case in cases) / count, 4)
            for name, key in dimensions.items()
        },
    }


async def create_evaluation(
    session: AsyncSession, payload: AgentEvaluationCreate, user: CurrentUser
) -> AgentOfflineEvaluation:
    """读取一组成功调查与人工标签，计算并保存一份可追溯的评测快照。

    session是数据库会话；payload包含数据集名称、development/held_out划分和样本；
    user是当前提交评测的账号。每个样本通过run_id关联一次已经完成的调查。
    数据流：检查运行与权限 -> 批量读建议和证据 -> 逐题计算 -> 保存数据集及结果。

    此过程不修改原AgentRun/AgentProposal，也不创建工单或改变业务审批结果。
    保存actual、expected、metrics和配置，目的是以后能回答“当时依据什么、如何得分”，
    而不是只留下一个无法复核的平均数。
    """
    run_ids = [case.run_id for case in payload.cases]
    # IN一次取出一组运行，建成UUID->运行的字典，避免每道题分别访问数据库。
    runs = {
        run.id: run
        for run in await session.scalars(select(AgentRun).where(AgentRun.id.in_(run_ids)))
    }
    for case in payload.cases:
        run = runs.get(case.run_id)
        if run is None:
            raise NotFoundError("agent_run", case.run_id)
        require_data_scope(
            user, run.organization_unit_id, Permissions.EVENT_CLASSIFY, owner_id=run.created_by
        )
        # 能看评测接口不等于能看每份资料。运行内可能含RAG、观察和经验快照，
        # 来源撤权/撤回后不能通过评测报告再次读到原内容。
        await authorize_knowledge_evidence(session, user, run.id)
        if run.status != "succeeded":
            raise ConflictError("agent run must have succeeded", run_id=str(case.run_id))

    proposals = {
        # AgentProposal是最终候选建议；运行时质量分不是这里的人工参考答案。
        proposal.run_id: proposal
        for proposal in await session.scalars(
            select(AgentProposal).where(AgentProposal.run_id.in_(run_ids))
        )
    }
    # defaultdict(set)让每个运行第一次使用时自动获得一个空集合。
    # 这份映射用于证明“专家标的证据确实属于该次运行”，防止拿别次证据混算。
    evidence_by_run: dict[UUID, set[str]] = defaultdict(set)
    for evidence in await session.scalars(
        select(AgentEvidence).where(AgentEvidence.run_id.in_(run_ids))
    ):
        evidence_by_run[evidence.run_id].add(str(evidence.id))

    case_results: list[dict] = []
    for case in payload.cases:
        proposal = proposals.get(case.run_id)
        if proposal is None:
            raise ConflictError("agent run has no proposal", run_id=str(case.run_id))
        if proposal.risk_level not in _RISK_ORDER:
            raise ConflictError("agent proposal has invalid risk level", run_id=str(case.run_id))
        relevant_ids = {str(value) for value in case.relevant_evidence_ids}
        # <=对集合表示“子集”，不是比较数量大小。所有人工证据必须来自当前运行。
        if not relevant_ids <= evidence_by_run[case.run_id]:
            raise ConflictError("expert evidence must belong to the run", run_id=str(case.run_id))
        cited_ids = {str(value) for value in proposal.evidence_ids}
        # str统一UUID和JSON里的字符串表示，再用set去重，避免重复ID增加得分。
        metrics = score_case(
            actual_category=proposal.category,
            actual_risk_level=proposal.risk_level,
            cited_evidence_ids=cited_ids,
            expected_category=case.expected_category,
            expected_risk_level=case.expected_risk_level,
            relevant_evidence_ids=relevant_ids,
            action_acceptable=case.action_acceptable,
            unsafe_action=case.unsafe_action,
        )
        case_results.append(
            {
                "run_id": str(case.run_id),
                "event_version_id": str(runs[case.run_id].event_version_id),
                "observation_revision": runs[case.run_id].config_snapshot.get(
                    "observation_revision"
                ),
                # 绑定事件版本和已确认观察修订：事实变了，应新建基准，不能强当同集改进。
                # 耗时取自既有调查的开始/结束，而不是本次读取和打分花了多少时间。
                # 缺失时间时用创建时间兜底，负差归零；0不代表真实推理没有耗时。
                "elapsed_ms": max(
                    0,
                    (
                        (runs[case.run_id].completed_at or runs[case.run_id].created_at)
                        - (runs[case.run_id].started_at or runs[case.run_id].created_at)
                    ).total_seconds()
                    * 1000,
                ),
                "usage": {
                    # 用量沿用原运行；只有配置过非零价格才报告费用，否则None表示未知。
                    # 不能把缺失定价时模型字段中的0解释成真实账单为零。
                    "input_tokens": runs[case.run_id].input_tokens,
                    "output_tokens": runs[case.run_id].output_tokens,
                    "cost_microusd": runs[case.run_id].cost_microusd
                    if any(
                        runs[case.run_id]
                        .config_snapshot.get("pricing_microusd_per_million", {})
                        .values()
                    )
                    else None,
                },
                "configuration": runs[case.run_id].config_snapshot,
                "actual": {
                    "category": proposal.category,
                    "risk_level": proposal.risk_level,
                    "recommended_action": proposal.recommended_action,
                    "rationale": proposal.rationale,
                    "evidence_ids": sorted(cited_ids),
                },
                # model_dump把Schema对象转成可存JSON的普通字典；UUID等转成字符串。
                # run_id已经单独保存，expected中只保留人工标签，便于追溯和比较。
                "expected": case.model_dump(mode="json", exclude={"run_id"}),
                "metrics": metrics,
            }
        )

    evaluation = AgentOfflineEvaluation(
        requested_by=user.user_id,
        name=payload.name,
        split=payload.split,
        dataset=payload.model_dump(mode="json"),
        results={
            "summary": {
                **aggregate_scores(case_results),
                # 下面是平均耗时和平均Token，与0~1的效果比例不是同一种单位。
                # 没有有效定价时逐题费用保持None，不在这里编造平均费用。
                "elapsed_ms": sum(c["elapsed_ms"] for c in case_results) / len(case_results),
                "input_tokens": sum(c["usage"]["input_tokens"] for c in case_results)
                / len(case_results),
                "output_tokens": sum(c["usage"]["output_tokens"] for c in case_results)
                / len(case_results),
            },
            "cases": case_results,
        },
    )
    session.add(evaluation)
    # commit才真正提交这份评测；refresh重新读取已保存的ID、时间等最终字段。
    await session.commit()
    await session.refresh(evaluation)
    return evaluation


async def get_evaluation(
    session: AsyncSession, evaluation_id: UUID, user: CurrentUser
) -> AgentOfflineEvaluation:
    """读取历史评测前，再检查其中每次调查及其证据的当前访问资格。

    报告里保存了actual建议、expected标签和配置等快照，所以不能只判断报告ID存在。
    例如某份规范后来撤回、某条经验失效，旧评测不能成为继续获取该内容的旁路。
    这里检查访问权限和来源可用性，不重新执行模型，也不重新计算或改写原分数。
    """
    evaluation = await session.get(AgentOfflineEvaluation, evaluation_id)
    if evaluation is None:
        raise NotFoundError("agent_offline_evaluation", evaluation_id)
    run_ids = [UUID(case["run_id"]) for case in evaluation.dataset["cases"]]
    runs = {
        run.id: run
        for run in await session.scalars(select(AgentRun).where(AgentRun.id.in_(run_ids)))
    }
    for run_id in run_ids:
        run = runs.get(run_id)
        if run is None:
            raise NotFoundError("agent_run", run_id)
        require_data_scope(
            user, run.organization_unit_id, Permissions.EVENT_CLASSIFY, owner_id=run.created_by
        )
        await authorize_knowledge_evidence(session, user, run.id)
    return evaluation
