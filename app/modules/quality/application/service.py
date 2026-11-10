from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.modules.knowledge.application.access import base_access, version_access
from app.modules.knowledge.domain.models import KnowledgeEvaluation
from app.modules.operation_event.application.evaluation import get_evaluation
from app.modules.operation_event.domain.agent_models import AgentRun
from app.modules.quality.domain.models import EvaluationImprovement
from app.shared.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.shared.security.authorization.dependencies import require_data_scope
from app.shared.security.authorization.permissions import Permissions as P


def now():
    return datetime.now(UTC).isoformat()


async def evaluation_snapshot(session, user, kind, evaluation_id, *, owner_id=None):
    permission = P.KNOWLEDGE_EVALUATE if kind == "rag" else P.EVENT_CLASSIFY
    if not user.has_permission(permission):
        raise PermissionDeniedError(permission)
    if kind == "agent":
        evaluation = await get_evaluation(session, evaluation_id, user)
        metrics = evaluation.results["summary"]
    else:
        evaluation = await session.get(KnowledgeEvaluation, evaluation_id)
        if evaluation is None or evaluation.requested_by not in {user.user_id, owner_id}:
            raise NotFoundError("knowledge_evaluation", evaluation_id)
        from app.shared.security.identity.models import UserAccount

        author = await session.get(UserAccount, evaluation.requested_by)
        require_data_scope(
            user,
            author.organization_unit_id,
            P.KNOWLEDGE_EVALUATE,
            owner_id=evaluation.requested_by,
        )
        if evaluation.status != "succeeded":
            raise ConflictError("evaluation must have succeeded")
        for base_id in evaluation.dataset.get("base_ids", []):
            await base_access(session, UUID(base_id), user, P.KNOWLEDGE_EVALUATE)
        for version_id in {
            e["version_id"]
            for row in evaluation.results.get("cases", [])
            for e in row.get("evidence", [])
        }:
            await version_access(session, UUID(version_id), user, P.KNOWLEDGE_EVALUATE)
        metrics = dict(evaluation.results["averages"])
        for mode, values in evaluation.results.get("by_mode", {}).items():
            metrics.update({f"{mode}.{key}": value for key, value in values.items()})
    runs = []
    if kind == "agent":
        for case in evaluation.dataset["cases"]:
            run = await session.get(AgentRun, UUID(case["run_id"]))
            runs.append(
                {
                    "id": str(run.id),
                    "started_at": run.started_at.isoformat(),
                    "config": deepcopy(run.config_snapshot),
                }
            )
    return {
        "runs": runs,
        "requested_by": str(evaluation.requested_by),
        "id": str(evaluation.id),
        "dataset": deepcopy(evaluation.dataset),
        "results": deepcopy(evaluation.results),
        "metrics": deepcopy(metrics),
        "created_at": evaluation.created_at.isoformat(),
    }


def dataset_identity(kind, snapshot):
    dataset = snapshot["dataset"]
    if kind == "rag":
        # Order independent; labels, modes, bases, and split must remain identical.
        import json

        return (
            dataset.get("split"),
            sorted(dataset.get("base_ids", [])),
            sorted(dataset.get("modes", [])),
            sorted(
                json.dumps(
                    {
                        k: v
                        for k, v in c.items()
                        if k != "relevant_evidence_ids" or not c.get("reference_evidence")
                    },
                    sort_keys=True,
                )
                for c in dataset["cases"]
            ),
        )
    # New agent runs have different run/evidence IDs; compare fixed event versions
    # and objective expert labels. Advice acceptability is relabelled by the expert.
    return (
        dataset["split"],
        sorted(
            (
                c["event_version_id"],
                c.get("observation_revision"),
                c["expected"]["expected_category"],
                c["expected"]["expected_risk_level"],
            )
            for c in snapshot["results"]["cases"]
        ),
    )


def compare_snapshots(kind, baseline, retest, targets):
    if dataset_identity(kind, baseline) != dataset_identity(kind, retest):
        raise ConflictError("retest dataset is not comparable to baseline")
    before, after = baseline["metrics"], retest["metrics"]
    if not set(targets) <= set(after):
        raise ConflictError("retest metrics missing; complete expert review first")
    comparison = {
        key: {"before": value, "after": after[key], "delta": round(after[key] - value, 6)}
        for key, value in before.items()
        if key in after
    }
    checks = {
        key: (
            after[key] >= target["value"]
            if target["operator"] == "gte"
            else after[key] <= target["value"]
        )
        for key, target in targets.items()
    }
    return {"metrics": comparison, "targets": checks, "passed": all(checks.values())}


def record(item, user, action, **data):
    item.history = [
        *item.history,
        {
            "action": action,
            "actor_id": str(user.user_id),
            "at": now(),
            **deepcopy(data),
        },
    ]


async def create_improvement(session, user, payload):
    baseline = await evaluation_snapshot(session, user, payload.kind, payload.baseline_id)
    if payload.kind == "rag" and "expert_review" not in baseline["results"]:
        raise ConflictError("complete independent expert review of the baseline answers first")
    targets = {key: value.model_dump() for key, value in payload.targets.items()}
    if payload.kind == "agent":
        for metric in ("risk_underestimation_rate", "unsafe_action_rate"):
            # Safety requirements can only be tightened, never configured away.
            targets[metric] = {"operator": "lte", "value": 0}
    modes = baseline["dataset"].get("modes", [])
    if payload.kind == "rag" and len(modes) > 1 and any("." not in key for key in targets):
        raise ConflictError("multi-mode evaluations require mode-qualified targets")
    if payload.kind == "rag" and "expert_review" in baseline["results"]:
        for mode in modes:
            targets[f"{mode}.unsafe_answer_rate"] = {"operator": "lte", "value": 0}
    allowed = set(baseline["metrics"]) - {"case_count"}
    if not set(targets) <= allowed:
        raise ConflictError("target metric is unavailable", allowed=sorted(allowed))
    for key, target in targets.items():
        if target["value"] < 0 or (
            not key.endswith(("elapsed_ms", "input_tokens", "output_tokens"))
            and target["value"] > 1
        ):
            raise ConflictError("metric target is out of range", metric=key)
    assignee_id = payload.assignee_id or user.user_id
    if payload.reviewer_id == assignee_id:
        raise ConflictError("reviewer must differ from implementer")
    await validate_participant_source(session, assignee_id, user, payload.kind, payload.baseline_id)
    if payload.reviewer_id:
        await validate_participant_source(
            session, payload.reviewer_id, user, payload.kind, payload.baseline_id
        )
    due_at = payload.due_at or datetime.now(UTC) + timedelta(days=7)
    if due_at <= datetime.now(UTC):
        raise ConflictError("improvement deadline must be in the future")
    item = EvaluationImprovement(
        created_by=user.user_id,
        assignee_id=assignee_id,
        reviewer_id=payload.reviewer_id,
        due_at=due_at,
        kind=payload.kind,
        baseline_id=payload.baseline_id,
        title=payload.title,
        problem=payload.problem,
        measures=payload.measures,
        baseline=baseline,
        targets=targets,
        status="confirmed",
        version=1,
        history=[],
    )
    record(item, user, "confirmed")
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def get_improvement(session, user, item_id, *, lock=False):
    item = await session.get(
        EvaluationImprovement, item_id, with_for_update=lock, populate_existing=True
    )
    if item is None or user.user_id not in {item.created_by, item.assignee_id, item.reviewer_id}:
        raise NotFoundError("evaluation_improvement", item_id)
    # Recheck current permission and source data access, including after revocation.
    await evaluation_snapshot(session, user, item.kind, item.baseline_id, owner_id=item.created_by)
    for entry in item.history:
        if entry["action"] == "retested":
            await evaluation_snapshot(
                session,
                user,
                item.kind,
                UUID(entry["retest"]["id"]),
                owner_id=UUID(entry["retest"].get("requested_by", str(item.created_by))),
            )
    return item


async def transition(session, user, item, action, payload):
    if item.version != payload.expected_version:
        raise ConflictError("improvement version changed", version=item.version)
    if action in {"implement", "retest"} and user.user_id != (item.assignee_id or item.created_by):
        raise PermissionDeniedError("improvement:implement")
    if action == "accept":
        if item.reviewer_id is None:
            raise ConflictError("assign an independent reviewer before acceptance")
        if user.user_id != item.reviewer_id:
            raise PermissionDeniedError("improvement:accept")
    if action == "implement":
        if item.status != "confirmed":
            raise ConflictError("improvement must be confirmed before implementation")
        record(item, user, "implemented", changes=payload.changes, revision=payload.revision)
        item.status = "implemented"
    elif action == "retest":
        if item.status != "implemented":
            raise ConflictError("record implementation before retesting")
        if payload.evaluation_id == item.baseline_id:
            raise ConflictError("retest must use a new evaluation")
        retest = await evaluation_snapshot(session, user, item.kind, payload.evaluation_id)
        implemented = item.history[-1]["at"]
        if datetime.fromisoformat(retest["created_at"]) <= datetime.fromisoformat(implemented):
            raise ConflictError("retest must be created after implementation")
        if any(
            datetime.fromisoformat(run["started_at"]) <= datetime.fromisoformat(implemented)
            for run in retest["runs"]
        ):
            raise ConflictError("retest agent runs must start after implementation")
        comparison = compare_snapshots(item.kind, item.baseline, retest, item.targets)
        record(item, user, "retested", retest=retest, comparison=comparison)
        item.status = "retested"
    elif action == "accept":
        if item.status != "retested":
            raise ConflictError("retest is required before acceptance")
        if payload.approved and not item.history[-1]["comparison"]["passed"]:
            raise ConflictError("acceptance targets have not been met")
        record(item, user, "accepted" if payload.approved else "rejected", reason=payload.reason)
        item.status = "closed" if payload.approved else "confirmed"
    else:
        raise ConflictError("unknown improvement action")
    item.version += 1
    await session.commit()
    await session.refresh(item)
    return item


async def validate_participant(session, account_id, organization_id):
    from app.shared.security.identity.models import UserAccount

    account = await session.get(UserAccount, account_id, populate_existing=True)
    if account is None or not account.active or account.organization_unit_id != organization_id:
        raise ConflictError("participant must be an active user in the same organization")


async def assign_improvement(session, user, item, payload):
    if item.created_by != user.user_id:
        raise PermissionDeniedError("improvement:assign")
    if item.version != payload.expected_version or item.status != "confirmed":
        raise ConflictError("assignment requires the current confirmed version")
    if payload.assignee_id == payload.reviewer_id:
        raise ConflictError("reviewer must differ from implementer")
    if payload.due_at <= datetime.now(UTC):
        raise ConflictError("deadline must be in the future")
    await validate_participant_source(
        session, payload.assignee_id, user, item.kind, item.baseline_id
    )
    await validate_participant_source(
        session, payload.reviewer_id, user, item.kind, item.baseline_id
    )
    item.assignee_id, item.reviewer_id, item.due_at = (
        payload.assignee_id,
        payload.reviewer_id,
        payload.due_at,
    )
    record(
        item,
        user,
        "assigned",
        assignee_id=str(item.assignee_id),
        reviewer_id=str(item.reviewer_id),
        due_at=item.due_at.isoformat(),
        reason=payload.reason,
    )
    item.version += 1
    await session.commit()
    await session.refresh(item)
    return item


async def participant_context(session, account):
    from app.shared.security.authentication.schemas import AuthenticatedIdentity
    from app.shared.security.authorization.repository import load_current_user

    return await load_current_user(
        session,
        AuthenticatedIdentity(
            user_id=account.id,
            issuer=account.external_issuer,
            subject=account.external_subject,
            username=account.username,
            display_name=account.display_name,
            organization_unit_id=account.organization_unit_id,
            authz_version=account.authz_version,
        ),
    )


async def validate_participant_source(session, account_id, owner, kind, baseline_id):
    from app.shared.security.identity.models import UserAccount

    await validate_participant(session, account_id, owner.organization_unit_id)
    account = await session.get(UserAccount, account_id)
    context = await participant_context(session, account)
    await evaluation_snapshot(session, context, kind, baseline_id, owner_id=owner.user_id)
