#!/usr/bin/env python3
"""Minimal command-provider example.

Replace `respond` with a call to your chosen model. The wrapper reads Ariadne's
role prompt from stdin and writes one structured response to stdout.
"""

from __future__ import annotations

import json
import os
import sys


def respond(role: str, prompt: str) -> dict:
    if role == "offline_researcher":
        return {
            "route": {
                "title": "Example first-principles route",
                "mode": "DEDUCTIVE",
                "method_family": "direct structural argument",
                "representation": "native variables",
                "key_lemma": "state the load-bearing bridge here",
                "central_mechanism": "explain why it may work",
                "decisive_test": "one exact test",
                "difference_from_existing": "initial route; no comparison yet",
                "independence_cluster": "direct",
            },
            "status": "PROGRESS",
            "summary": "The wrapper is a protocol example, not a proof agent.",
            "claims": [],
            "failures": [],
            "decisive_events": [],
            "novelty_evidence": [],
            "next_task": "Connect a real model and return a bounded mathematical task.",
            "proof_candidate": None,
            "counterexample_candidate": None,
            "experiment_request": None,
        }
    if role == "literature_sentinel":
        return {"interventions": []}
    if role == "intervention_responder":
        return {
            "decision": "NEED_HUMAN_REVIEW",
            "reason": "Protocol example has no real mathematical model.",
            "difference_certificate": {},
            "proposed_test": "",
        }
    return {
        "verdict": "UNCERTAIN",
        "failure_class": "VERIFIER_UNCERTAINTY",
        "minimal_failed_obligation": "No real model is connected.",
        "local_repairable": False,
        "statement_drift": False,
        "recommended_transition": "HUMAN_REVIEW",
    }


def main() -> int:
    prompt = sys.stdin.read()
    role = os.environ.get("ARIADNE_ROLE", "unknown")
    result = respond(role, prompt)
    result["usage"] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    print("<ARIADNE_JSON>")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("</ARIADNE_JSON>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
