"""Orchestrator: run every adapter against the document, write each result to
the audit log, then compute consensus per criterion."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor

from adapters import ModelAdapter, ValidationResult


def _consensus_for_criterion(verdicts: list[str]) -> tuple[str, bool]:
    """Return (winning_verdict, agreement_flag). agreement=True iff unanimous."""
    if not verdicts:
        return "missing", False
    counts = Counter(verdicts)
    top, top_n = counts.most_common(1)[0]
    return top, top_n == len(verdicts)


def run_validation(
    adapters: list[ModelAdapter],
    rubric: dict,
    document: str,
) -> tuple[list[ValidationResult], dict]:
    with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
        results = list(pool.map(lambda a: a.validate(document, rubric), adapters))

    per_criterion: dict[str, dict] = {}
    for c in rubric["criteria"]:
        cid = c["id"]
        votes = []
        per_model = []
        for r in results:
            v = next((x for x in r.verdicts if x.criterion_id == cid), None)
            if v is None:
                continue
            votes.append(v.verdict)
            per_model.append(
                {
                    "adapter_id": r.adapter_id,
                    "verdict": v.verdict,
                    "confidence": v.confidence,
                    "evidence": v.evidence,
                }
            )
        winning, unanimous = _consensus_for_criterion(votes)
        per_criterion[cid] = {
            "consensus": winning,
            "unanimous": unanimous,
            "votes": dict(Counter(votes)),
            "per_model": per_model,
        }

    overall_pass = all(
        v["consensus"] in {"pass", "not_applicable"} for v in per_criterion.values()
    )
    any_disagreement = any(not v["unanimous"] for v in per_criterion.values())

    consensus = {
        "overall": "pass" if overall_pass else "fail",
        "any_disagreement": any_disagreement,
        "model_count": len(adapters),
        "models": [
            {
                "adapter_id": r.adapter_id,
                "provider": r.provider,
                "model": r.model,
                "error": r.error,
                "latency_ms": r.latency_ms,
            }
            for r in results
        ],
        "criteria": per_criterion,
    }
    return results, consensus


def result_to_dict(r: ValidationResult) -> dict:
    d = asdict(r)
    d["verdicts"] = [asdict(v) for v in r.verdicts]
    return d
