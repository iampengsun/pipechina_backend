"""RAG效果评测

一、RAG有两类不同的问题，不能混成一个准确率
    检索层：正确资料有没有找全？相关资料排得是否靠前？
    回答层：模型是否应该拒答？回答是否正确、是否存在不安全建议？
    本文件先自动计算检索/拒答指标，再由独立人员复核完整回答。

二、先认识输入中的几个词
    case（样本）：一个问题及人工给定的参考标注。
    expected（期望）：人工认为相关的证据，不是模型自己声明的标准答案。
    actual（实际）：系统本次检索出的有序证据，列表越靠前，排名越高。
    mode（模式）：fulltext、vector、hybrid、graph等不同检索方案。
    development用于开发调参，held_out用于留出验证；这里只保存分集标签，
    不会自动证明分集互不泄漏，也不会自动把合成样本变成专家标注。

三、阅读顺序
    retrieval_metrics：用证据ID计算Recall、单题RR和nDCG。
    reference_metrics：按“文档编号+原文”匹配，减少重分段造成的ID变化影响。
    summarize：对各样本做算术平均。
    execute_evaluation：真正调用检索和生成，保存完整输出与自动指标。
    review_evaluation：人工逐题复核，补上正确率和不安全回答率。
"""

import hashlib
import math
import re
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID

from app.modules.knowledge.application.answers import grounded_answer
from app.modules.knowledge.application.retrieval import retrieve
from app.modules.knowledge.domain.models import KnowledgeEvaluation
from app.modules.knowledge.domain.schemas import EvaluationCreate, SearchInput
from app.shared.platform.execution import execution_lease, guard_execution
from app.shared.platform.models import AsyncJob
from app.shared.platform.runtime import text_provider_for_job
from app.shared.platform.service import update_job
from app.shared.worker_runtime import job_user


def retrieval_metrics(expected: set[str], actual: list[str]) -> dict:  # 计算检索评估指标
    """在当前返回列表的截断范围内，计算一条问题的检索指标。

    例：expected={A,B,C}，actual=[X,B,A]，X不相关。
    1. Recall（召回率）=命中相关证据数/所有应找出的相关证据数=2/3。
       它回答“找全了吗”，不负责判断返回结果是否混入大量无关内容。
    2. RR（倒数排名）=1/第一条相关证据的排名=1/2。
       多道问题的RR取平均才叫MRR；这里沿用结果字段mrr保存单题RR。
    3. DCG（折损累计增益）给靠前的相关证据更大权重。
       第r名的相关证据贡献1/log2(r+1)，不相关证据贡献0。
       本例DCG=1/log2(3)+1/log2(4)，理想排序为[A,B,C]。
       nDCG=实际DCG/理想DCG，便于比较排序质量。
    当前是二元相关性：一条证据只有相关/不相关。
    """
    matched = expected.intersection(actual)  # 获取命中的相关证据
    # set交集只计不同证据，重复返回同一ID不会提高召回率。
    # enumerate从0开始，因此i+1才是第1、2、3名；next只取首个命中，无命中为0。
    reciprocal = next((1 / (i + 1) for i, value in enumerate(actual) if value in expected), 0.0)
    # i+2等于“排名+1”，避免第1名出现1log2()=0造成除零；第1名权重恰为1。
    dcg = sum(1 / math.log2(i + 2) for i, value in enumerate(actual) if value in expected)
    ideal = sum(
        1 / math.log2(i + 2) for i in range(min(len(expected), len(actual)))
    )  # 计算理想累计增益
    # IDCG最多摆放min(应有相关数, 实际返回数)条相关证据，不凭空增加返回名额。
    # IDCG为0时区分“本来无需证据”和“漏检”，不能给空返回的漏检记满分。
    return {
        "recall": len(matched) / len(expected) if expected else float(not actual),  # 召回率
        "mrr": reciprocal,  # 倒数排名
        "ndcg": dcg / ideal if ideal else float(not expected and not actual),  # 归一化折损累计增益
    }


def normalized(value):
    """统一原文的空白和英文大小写，允许换行/排版差异，不做语义相似判断。

    例如“A B\\nC”和“abc”会得到相同字符串；同义改写不会因此匹配。
    casefold比lower更全面地处理Unicode大小写；这不等于分词、向量或模型推理。
    """
    return re.sub(r"\s+", "", value).casefold()


def reference_metrics(references, actual):
    """把稳定原文标注转换成检索指标可使用的标识。

    为什么不用片段UUID一直评分？重新分段后，同一段规范可能换了片段ID。
    人工用document_code指定资料，用quote指定关键原文，本次返回文本必须同时满足
    “文档编号相同、规范化后的引文确实出现”，才能算命中。

    这里把每条参考引文编号为0、1、2……，再按返回顺序建立一个“虚拟命中列表”。
    同一引文只算一次；没有新命中的返回项也占一个位置，不能删除无关项后虚增排名。
    一个片段可能同时覆盖多条引文，虚拟列表会展开这些命中，因此这是按引文单元的
    排名评分，不严格等于按原始片段排名计算的nDCG。
    """
    # expected是参考引文编号集合，actual_keys是按检索顺序排列的命中编号列表。
    expected = {str(i) for i in range(len(references))}
    actual_keys: list[str] = []
    seen: set[str] = set()
    for rank, row in enumerate(actual):
        # 原文包含关系在这里检查；不是让模型猜测两段文本是否“意思差不多”。
        matched = {
            str(i)
            for i, ref in enumerate(references)
            if row["code"] == ref.document_code and normalized(ref.quote) in normalized(row["text"])
        }
        fresh = matched - seen
        # 已被前面片段覆盖的引文不再得分；占位符保证该片段仍消耗一个排名位置。
        actual_keys.extend(sorted(fresh) or [f"unmatched:{rank}"])
        seen |= matched
    return retrieval_metrics(expected, actual_keys)


def summarize(cases):
    """宏平均：先给每条“问题×模式”算分，再让每条结果以相同权重参加平均。

    例如两道题Recall为1和0，平均为0.5；不是把两题命中数量合并后重算Recall。
    调用者通过Schema保证问题和模式非空，因此这里没有空列表的除零分支。
    elapsed_ms同样取平均；它是本次检索+生成/核查的耗时，不是队列等待时间。
    """
    names = ("recall", "mrr", "ndcg", "refusal_accuracy", "elapsed_ms")
    return {name: sum(row[name] for row in cases) / len(cases) for name in names}


async def execute_evaluation(
    factory, knowledge, text, settings, job_id: UUID
):  # 执行知识检索评估任务
    """异步评测任务：固定数据集与配置，逐模式重新检索、生成并保存结果。

    factory创建数据库会话；knowledge提供解析/索引/向量/重排资源；text提供文字模型；
    settings控制租约时间；job_id定位任务和它关联的KnowledgeEvaluation。
    这与Agent评测文件不同：本函数确实调用检索和模型，不只是给旧回答重新打分。

    执行租约可以理解为“有期限的独占执行证”：后台心跳续期，提交前再比较token。
    Worker失联后即使恢复，也不能凭旧token覆盖新执行者的结果。
    """
    token = None  # 初始化执行令牌
    try:
        async with execution_lease(
            factory,
            job_id,
            lease_seconds=settings.job_lease_seconds,
            interval_seconds=settings.job_heartbeat_interval_seconds,
        ) as token:  # 获取执行租约并维持心跳
            async with factory() as session:  # 创建数据库会话
                job = await session.get(AsyncJob, job_id)  # 获取异步任务
                from app.modules.knowledge.application.observability import observed_knowledge

                knowledge = observed_knowledge(
                    knowledge, session, job
                )  # 包装知识服务以记录观测数据
                # 从Job快照选择模型参数，避免评测排队期间配置变化造成结果不可追溯。
                text = text_provider_for_job(session, job, text)  # 获取任务专用文本服务
                evaluation = await session.get(KnowledgeEvaluation, job.resource_id)  # 获取评估记录
                payload = EvaluationCreate.model_validate(
                    evaluation.dataset
                )  # 验证并解析评估数据集
                evaluation.status = "running"  # 标记评估运行中
                await session.commit()  # 保存评估状态

                results = []  # 初始化评估结果
                # 同一道题用每个mode各跑一遍，得到“问题×模式”的对照数据。
                for mode in payload.modes:  # 遍历检索模式
                    for ordinal, case in enumerate(payload.cases):  # 遍历测试用例
                        user = await job_user(session, job)  # 获取任务执行用户
                        # 每题重新加载授权：排队前可访问，不代表执行时仍可读取资料。
                        # perf_counter适合测时间差，不受系统时钟校时影响；单位为秒。
                        start = perf_counter()  # 记录用例开始时间
                        evidence, trace = await retrieve(
                            session,
                            user,
                            SearchInput(
                                question=case.question, base_ids=payload.base_ids, mode=mode
                            ),
                            knowledge,
                            text,
                        )  # 执行知识检索

                        metrics = retrieval_metrics(
                            {str(i) for i in case.relevant_evidence_ids},
                            [e["id"] for e in evidence],
                        )  # 计算召回率、MRR和NDCG

                        if case.reference_evidence:
                            # 有稳定引文时覆盖UUID指标，不把两种标注的得分相加。
                            metrics = reference_metrics(case.reference_evidence, evidence)
                        # 参考答案只保留给人复核，没有传给生成模型，避免直接泄漏标准答案。
                        answer = await grounded_answer(
                            text, case.question, evidence
                        )  # 生成并核查证据回答
                        # 比较模型refused与人工should_refuse，布尔值转成1或0再参加平均。
                        # “该拒答时拒答”或“不该拒答时未拒答”均得1；回答内容可能仍然错误，
                        # 所以拒答匹配率不等于答案正确率，后者留给独立人工标注。
                        metrics["refusal_accuracy"] = float(answer.refused == case.should_refuse)

                        results.append(
                            {
                                # 没有显式case_id时用序号和问题生成可重复ID。
                                # 调整题目顺序会改变该后备ID，长期比较应显式指定case_id。
                                "case_id": case.case_id
                                or "case-"
                                + hashlib.sha256(f"{ordinal}:{case.question}".encode()).hexdigest()[
                                    :16
                                ],
                                "question": case.question,
                                "reference_answer": case.reference_answer,
                                "answer": answer.model_dump(mode="json"),
                                "evidence": evidence,
                                "retrieval_trace": trace,
                                "mode": mode,
                                **metrics,
                                "elapsed_ms": (perf_counter() - start) * 1000,
                                "generation": trace.get("generations", []),
                                "evidence_ids": [e["id"] for e in evidence],
                            }
                        )  # 保存当前用例评估结果

                        # 长任务逐题检查执行权，取消或租约失效时不继续发布最终结果。
                        # results此时仍是内存列表，逐题commit不等于逐题持久化完整评测结果。
                        job = await guard_execution(
                            session, job_id, token
                        )  # 校验任务执行权限和状态
                        await session.commit()  # 提交当前事务

                job = await guard_execution(session, job_id, token)  # 最终校验执行状态
                evaluation.results = {
                    "cases": results,
                    "scope": "retrieval_and_refusal_metrics",
                    "dataset_split": payload.split,
                    "configuration": job.config_snapshot,
                    "averages": summarize(results),
                    # averages混合所有模式；比较方案优劣应看by_mode，避免平均值掩盖差异。
                    "by_mode": {
                        mode: summarize([r for r in results if r["mode"] == mode])
                        for mode in payload.modes
                    },
                }  # 汇总各项评估指标

                evaluation.status = "succeeded"  # 标记评估成功
                await update_job(session, job, status="succeeded", progress=100)  # 更新异步任务状态
                await session.commit()  # 保存评估结果及任务状态

    except BaseException:  # 捕获执行异常或取消
        # 内层session退出已回滚未提交事务。使用独立会话收尾，并检查仍属于本次token，
        # 防止旧Worker把接手者的任务误标为失败。最后raise让任务系统也能感知失败。
        async with factory() as session:  # 创建独立事务处理异常
            job = await session.get(AsyncJob, job_id, with_for_update=True)  # 加锁获取任务
            # 验证当前执行权
            if job and token and job.execution_token == token and job.status == "running":
                evaluation = await session.get(
                    KnowledgeEvaluation, job.resource_id
                )  # 获取关联评估记录
                evaluation.status = (
                    "cancelled" if job.cancel_requested else "failed"
                )  # 区分取消与失败
                await update_job(
                    session,
                    job,
                    status=evaluation.status,
                    progress=job.progress,
                    error_code=None if job.cancel_requested else "KNOWLEDGE_EVALUATION_FAILED",
                )  # 更新异常任务状态
                await session.commit()  # 保存失败或取消状态
        raise  # 继续向上抛出异常


async def review_evaluation(session, user, evaluation_id, payload):
    """独立人工复核：给每条已保存回答标注“正确”和“不安全”两个维度。

    自动检索指标无法证明自然语言答案正确。这里接收人工answer_correct、unsafe_answer
    及reason，计算答案正确率/不安全回答率；本函数不会自动调用模型生成专家标签。
    评测创建人不能给自己的评测提交这份独立复核，且复核人仍需具有来源访问权限。

    行锁串行处理同时到达的写入；expected_revision防止用户拿旧版本表单覆盖新标注。
    主键使用(case_id, mode)：同一问题的不同模式输出是两条需要分别复核的回答。
    """
    from app.modules.knowledge.application.access import base_access, version_access
    from app.shared.errors import ConflictError, NotFoundError
    from app.shared.security.authorization.permissions import Permissions as P

    evaluation = await session.get(
        KnowledgeEvaluation, evaluation_id, with_for_update=True, populate_existing=True
    )
    if evaluation is None:
        raise NotFoundError("knowledge_evaluation", evaluation_id)
    if evaluation.status != "succeeded":
        raise ConflictError("evaluation must have succeeded")
    if evaluation.requested_by == user.user_id:
        raise ConflictError("expert review must be performed by a different evaluator")
    from app.shared.security.authorization.dependencies import require_data_scope
    from app.shared.security.identity.models import UserAccount

    author = await session.get(UserAccount, evaluation.requested_by)
    require_data_scope(
        user, author.organization_unit_id, P.KNOWLEDGE_EVALUATE, owner_id=evaluation.requested_by
    )
    # 评测结果内含回答和证据快照，不能因为它是“报告”就绕过知识库/文档权限。
    for base_id in evaluation.dataset.get("base_ids", []):
        await base_access(session, UUID(base_id), user, P.KNOWLEDGE_EVALUATE)
    cases = evaluation.results["cases"]
    if not cases or any("case_id" not in case or "answer" not in case for case in cases):
        raise ConflictError("legacy evaluation lacks reviewable answers; run a new evaluation")
    for version_id in {e["version_id"] for case in cases for e in case.get("evidence", [])}:
        await version_access(session, UUID(version_id), user, P.KNOWLEDGE_EVALUATE)
    revision = evaluation.options.get("review_revision", 0)
    if payload.expected_revision != revision:
        raise ConflictError("evaluation review version changed", revision=revision)
    supplied = {(row.case_id, row.mode): row for row in payload.cases}
    expected = {(row["case_id"], row["mode"]) for row in cases}
    # dict会合并重复键，所以先比较长度发现重复，再比较键集合发现漏标/多标。
    if len(supplied) != len(payload.cases) or set(supplied) != expected:
        raise ConflictError("expert review must cover each generated answer exactly once")
    results = dict(evaluation.results)
    # 复制外层和嵌套指标字典后整体赋回JSON列，避免原地改嵌套字典未被ORM识别。
    results["by_mode"] = {mode: dict(metrics) for mode, metrics in results["by_mode"].items()}
    for mode, metrics in results["by_mode"].items():
        # 每种模式单独统计。例如10条里8条正确，answer_accuracy=0.8；
        # 其中1条不安全，unsafe_answer_rate=0.1。正确率越高越好，不安全率越低越好。
        labels = [label for (_, m), label in supplied.items() if m == mode]
        metrics["answer_accuracy"] = sum(row.answer_correct for row in labels) / len(labels)
        metrics["unsafe_answer_rate"] = sum(row.unsafe_answer for row in labels) / len(labels)
    results["averages"] = {
        **results["averages"],
        "answer_accuracy": sum(row.answer_correct for row in supplied.values()) / len(supplied),
        "unsafe_answer_rate": sum(row.unsafe_answer for row in supplied.values()) / len(supplied),
    }
    review = {
        "reviewer_id": str(user.user_id),
        "at": datetime.now(UTC).isoformat(),
        "revision": revision + 1,
        "cases": [row.model_dump(mode="json") for row in payload.cases],
    }
    results["expert_review"] = review
    evaluation.results = results
    evaluation.options = {
        **evaluation.options,
        "review_revision": revision + 1,
        # 最新标注放在expert_review，同时追加完整历史，不把旧复核记录直接丢弃。
        "review_history": [*evaluation.options.get("review_history", []), review],
    }
    await session.commit()
    return evaluation
