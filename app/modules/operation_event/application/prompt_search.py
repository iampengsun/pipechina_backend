"""Bridge the synchronous GEPA engine to the existing asynchronous investigation evaluator."""

import asyncio
import hashlib
import threading
from importlib.metadata import version
from typing import cast

import gepa
from gepa.core.adapter import EvaluationBatch, GEPAAdapter

from app.shared.errors import ConflictError


class SearchLogger:
    def log(self, message):
        # GEPA messages may contain case traces. Persist only the authorized business results.
        return None


def objective_scores(outcome):
    metrics = outcome["metrics"]
    return {
        "category": float(metrics["category_correct"]),
        "risk": float(metrics["risk_correct"]),
        "evidence": float(metrics["evidence_f1"]),
        "risk_not_underestimated": 1 - float(metrics["risk_underestimated"]),
        "valid_references": float(
            not outcome.get("invalid_references") and not outcome.get("error")
        ),
    }


def proxy_score(objectives):
    if min(objectives["risk_not_underestimated"], objectives["valid_references"]) == 0:
        return 0.0
    return sum(objectives.values()) / len(objectives)


def paired_comparison(baseline, candidate):
    pairs = []
    for before, after in zip(baseline["outcomes"], candidate["outcomes"], strict=True):
        if before["run_id"] != after["run_id"]:
            raise ConflictError("candidate case order changed")
        old, new = before["objectives"], after["objectives"]
        pairs.append(
            {
                "run_id": before["run_id"],
                "before": old,
                "after": new,
                "delta": {key: new[key] - old[key] for key in old},
                "regression": any(
                    new[key] < old[key] for key in ("risk_not_underestimated", "valid_references")
                ),
            }
        )
    return {
        "cases": pairs,
        "safety_proxy_passed": not any(p["regression"] for p in pairs),
        "expert_safety_verified": False,
    }


async def search(*, seed, case_ids, repetitions, generations, evaluate_case, reflect, save):
    loop = asyncio.get_running_loop()
    stopped = threading.Event()
    candidates: list[dict] = []
    reflections: dict[str, dict] = {}
    cache = {}
    proposals = 0

    def invoke(function, *args):
        if stopped.is_set():
            raise ConflictError("prompt search stopped")
        # Sessions and providers stay on the owning event loop, never in the GEPA thread.
        return asyncio.run_coroutine_threadsafe(function(*args), loop).result()

    class Adapter:
        def evaluate(self, batch, candidate, capture_traces=False):
            instructions = candidate["system_prompt"]
            digest = hashlib.sha256(instructions.encode()).hexdigest()
            outcomes = []
            for identifier in batch:
                key = (digest, identifier)
                if key not in cache:
                    repeats = [
                        invoke(evaluate_case, instructions, identifier) for _ in range(repetitions)
                    ]
                    objectives = {
                        name: sum(objective_scores(r)[name] for r in repeats) / repetitions
                        for name in objective_scores(repeats[0])
                    }
                    # A repeated dangerous result must not be averaged away.
                    objectives["risk_not_underestimated"] = min(
                        objective_scores(r)["risk_not_underestimated"] for r in repeats
                    )
                    objectives["valid_references"] = min(
                        objective_scores(r)["valid_references"] for r in repeats
                    )
                    cache[key] = {
                        "run_id": repeats[0]["run_id"],
                        "repeats": repeats,
                        "objectives": objectives,
                        "score_range": [
                            min(proxy_score(objective_scores(r)) for r in repeats),
                            max(proxy_score(objective_scores(r)) for r in repeats),
                        ],
                    }
                outcomes.append(cache[key])
            complete = all((digest, i) in cache for i in case_ids)
            if complete and not any(c["digest"] == digest for c in candidates):
                full = [cache[(digest, i)] for i in case_ids]
                record = {
                    "generation": len(candidates),
                    "instructions": instructions,
                    "digest": digest,
                    "reflection": reflections.get(digest),
                    "outcomes": full,
                    "scores": [proxy_score(o["objectives"]) for o in full],
                }
                if candidates:
                    record["comparison"] = paired_comparison(candidates[0], record)
                candidates.append(record)
                invoke(save, list(candidates))
            batch_objectives = [o["objectives"] for o in outcomes]
            scores = [proxy_score(o) for o in batch_objectives]
            return EvaluationBatch(
                outputs=outcomes,
                scores=scores,
                trajectories=outcomes if capture_traces else None,
                objective_scores=batch_objectives,
            )

        def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
            return {
                name: [
                    {"trajectory": t, "score": score}
                    for t, score in zip(eval_batch.outputs, eval_batch.scores, strict=True)
                ]
                for name in components_to_update
            }

        def propose_new_texts(self, candidate, reflective_dataset, components_to_update):
            nonlocal proposals
            proposals += 1
            response = invoke(reflect, candidate, reflective_dataset)
            instructions = response.instructions
            reflections[hashlib.sha256(instructions.encode()).hexdigest()] = response.model_dump()
            return {"system_prompt": instructions}

    def optimize():
        return gepa.optimize(
            seed_candidate={"system_prompt": seed},
            trainset=case_ids,
            valset=case_ids,
            adapter=cast(GEPAAdapter, Adapter()),
            candidate_selection_strategy="pareto",
            frontier_type="hybrid",
            reflection_minibatch_size=len(case_ids),
            skip_perfect_score=False,
            stop_callbacks=lambda state: stopped.is_set() or proposals >= generations,
            max_metric_calls=len(case_ids) * (1 + 3 * generations),
            use_merge=False,
            logger=SearchLogger(),
            raise_on_exception=True,
            seed=0,
        )

    worker = asyncio.create_task(asyncio.to_thread(optimize))
    try:
        result = await asyncio.shield(worker)
    except BaseException:
        stopped.set()
        try:
            await worker
        except BaseException:
            pass
        raise
    best_text = result.best_candidate["system_prompt"]
    selected = next(i for i, c in enumerate(candidates) if c["instructions"] == best_text)
    if (
        selected
        and not paired_comparison(candidates[0], candidates[selected])["safety_proxy_passed"]
    ):
        selected = 0
    return {
        "algorithm": "gepa",
        "library_version": version("gepa"),
        "candidates": candidates,
        "selected": selected,
        "engine": {
            "candidates": result.candidates,
            "parents": result.parents,
            "validation_scores": result.val_aggregate_scores,
            "pareto_front": {
                str(k): sorted(v) for k, v in result.per_val_instance_best_candidates.items()
            },
        },
        "repetitions": repetitions,
        "selection_reason": "baseline_retained" if selected == 0 else "candidate_selected",
        "publication_requires": "fresh held_out expert evaluation and closed improvement",
    }
