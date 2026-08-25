from __future__ import annotations

from typing import Any


CONTRACT_TEMPLATE: dict[str, Any] = {
    "problem_id": "P-001",
    "title": "Replace with the exact problem title",
    "tags": [],
    "research_mode": "offline_sentinel",
    "statement": {
        "text": "Replace with the exact theorem or prove-or-refute statement.",
        "formal_quantifier_outline": "forall X, hypotheses(X) -> conclusion(X)",
    },
    "definitions": {},
    "domains": {},
    "hypotheses": [],
    "conclusion": [],
    "uniformity": {
        "constants_may_depend_on": [],
        "constants_may_not_depend_on": [],
    },
    "coefficient_domains": [],
    "endpoints": {"included": [], "excluded": []},
    "success_criteria": {
        "proof": "Complete deductive proof of the exact statement",
        "refutation": "Exact counterexample violating hypotheses => conclusion",
    },
    "allowed_conditionals": [],
    "statement_drift_prohibitions": [],
    "formalization_policy": {
        "lean_allowed_only_after_human_checked_proof": True,
        "lean_role": "terminal certification only",
    },
}


def validate_contract(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("Problem contract must be a JSON object")
    if not str(data.get("title", "")).strip():
        raise ValueError("Problem contract requires a nonempty `title`")
    if "statement" not in data:
        raise ValueError("Problem contract requires `statement`")
    statement = data["statement"]
    if isinstance(statement, dict):
        statement_text = str(statement.get("text", ""))
    else:
        statement_text = str(statement)
    if not statement_text.strip():
        raise ValueError("Problem contract requires a nonempty statement text")
    if "success_criteria" not in data or not isinstance(
        data.get("success_criteria"), dict
    ):
        raise ValueError("Problem contract requires a `success_criteria` object")
    policy = data.get("formalization_policy", {})
    if not isinstance(policy, dict) or not policy.get(
        "lean_allowed_only_after_human_checked_proof", False
    ):
        raise ValueError(
            "Problem contract must enforce lean_allowed_only_after_human_checked_proof=true"
        )
    mode = str(data.get("research_mode", "offline_sentinel"))
    if mode not in {"offline_sentinel", "offline_only", "literature_guided"}:
        raise ValueError(
            "research_mode must be offline_sentinel, offline_only, or literature_guided"
        )
