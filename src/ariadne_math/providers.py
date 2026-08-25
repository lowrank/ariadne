from __future__ import annotations

import os
import re
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .config import ProviderConfig
from .models import AgentCall, ProviderResponse, Usage
from .util import extract_json_object, redact_environment


class ProviderError(RuntimeError):
    pass


class BaseProvider(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    def run(self, call: AgentCall) -> ProviderResponse:
        raise NotImplementedError


class CommandProvider(BaseProvider):
    def run(self, call: AgentCall) -> ProviderResponse:
        if not self.config.command:
            raise ProviderError(f"Provider {self.config.name!r} has no command")
        if (
            call.network_policy == "deny"
            and self.config.require_os_network_isolation
            and not self.config.sandbox_prefix
        ):
            raise ProviderError(
                f"Provider {self.config.name!r} requires OS network isolation, "
                "but no sandbox_prefix is configured"
            )

        command = [*self.config.sandbox_prefix, *self.config.command]
        replacements = {
            "{role}": call.role,
            "{slot}": call.slot,
            "{project}": str(call.project_root),
            "{route_id}": call.route_id or "",
            "{epoch}": str(call.epoch or 0),
        }
        command = [self._replace_tokens(part, replacements) for part in command]

        base_env = dict(os.environ)
        if call.network_policy == "deny":
            base_env = redact_environment(base_env)
        base_env.update(self.config.env)
        base_env.update(
            {
                "ARIADNE_ROLE": call.role,
                "ARIADNE_SLOT": call.slot,
                "ARIADNE_PROJECT_ROOT": str(call.project_root),
                "ARIADNE_NETWORK_POLICY": call.network_policy,
                "ARIADNE_ROUTE_ID": call.route_id or "",
                "ARIADNE_EPOCH": str(call.epoch or 0),
            }
        )

        try:
            completed = subprocess.run(
                command,
                input=call.prompt,
                text=True,
                capture_output=True,
                cwd=call.project_root,
                env=base_env,
                timeout=self.config.timeout_seconds or None,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"Provider {self.config.name!r} timed out after "
                f"{self.config.timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise ProviderError(
                f"Could not start provider {self.config.name!r}: {exc}"
            ) from exc

        if completed.returncode != 0:
            diagnostic = "\n".join(
                part for part in (completed.stderr.strip(), completed.stdout.strip()) if part
            )
            if not diagnostic:
                diagnostic = "provider produced no diagnostic output"
            raise ProviderError(
                f"Provider {self.config.name!r} exited with {completed.returncode}: "
                f"{diagnostic[-4000:]}"
            )

        usage = Usage()
        try:
            structured = extract_json_object(completed.stdout)
            usage_raw = structured.get("usage", {})
            if isinstance(usage_raw, dict):
                reported_cost = usage_raw.get("cost_usd")
                input_details = usage_raw.get("input_tokens_details")
                cached_input = (
                    input_details.get("cached_tokens", 0)
                    if isinstance(input_details, dict) else usage_raw.get("cached_input_tokens", 0)
                )
                usage = Usage(
                    input_tokens=int(usage_raw.get("input_tokens", 0)),
                    cached_input_tokens=int(cached_input),
                    output_tokens=int(usage_raw.get("output_tokens", 0)),
                    cost_usd=float(reported_cost) if reported_cost is not None else 0.0,
                    reported_cost_usd=(
                        float(reported_cost) if reported_cost is not None else None
                    ),
                )
        except (ValueError, TypeError):
            pass

        return ProviderResponse(
            text=completed.stdout,
            usage=usage,
            returncode=completed.returncode,
            stderr=completed.stderr,
        )

    @staticmethod
    def _replace_tokens(text: str, replacements: dict[str, str]) -> str:
        for key, value in replacements.items():
            text = text.replace(key, value)
        return text


class MockProvider(BaseProvider):
    """Deterministic provider used for installation tests and the demo.

    It demonstrates the offline/literature-sentinel negotiation rather than
    pretending to solve the example problem.
    """

    def run(self, call: AgentCall) -> ProviderResponse:
        role = call.role
        epoch = call.epoch or 1
        slot = call.slot
        if role == "offline_researcher":
            data = self._offline_response(slot, epoch, call.prompt)
        elif role == "literature_researcher":
            data = self._literature_research_response(slot, epoch, call.prompt)
        elif role == "contract_author":
            data = self._contract_author_response(call.prompt)
        elif role == "literature_author":
            data = self._literature_author_response(call.prompt)
        elif role == "intervention_responder":
            data = self._intervention_response(slot, epoch, call.prompt)
        elif role == "literature_sentinel":
            data = self._sentinel_response(epoch, call.prompt)
        elif role in {"local_verifier", "global_verifier"}:
            data = {
                "verdict": "UNCERTAIN",
                "failure_class": "VERIFIER_UNCERTAINTY",
                "minimal_failed_obligation": "The mock provider does not certify proofs.",
                "local_repairable": False,
                "statement_drift": False,
                "recommended_transition": "HUMAN_REVIEW",
                "verified_object": None,
                "verified_admissibility": None,
                "verified_violation": None,
                "verified_source_independence": None,
            }
        elif role == "instruction_interpreter":
            data = {
                "action": "ADD",
                "purpose": "RESEARCH_GUIDANCE",
                "instruction": "Follow the owner request exactly; retain any required numerical evidence, data, code, and plots as reproducible artifacts.",
                "audience": "researchers",
                "route_id": "",
                "target_instruction_ids": [],
                "required_artifacts": [],
                "budget": None,
                "target_variant": "",
                "clarification_needed": False,
                "clarifying_question": "",
            }
        elif role == "result_synthesizer":
            data = {
                "verdict": "NO_MEANINGFUL_RESULT",
                "proposal": None,
                "reason_no_proposal": (
                    "The deterministic mock has no independently useful, artifact-backed partial theorem."
                ),
                "used_artifact_ids": [],
            }
        elif role == "conceptual_pivot":
            data = {
                "new_representations": [
                    {
                        "title": "Dual variational formulation",
                        "difference": "Replaces direct energy iteration by an extremal dual problem.",
                        "decisive_test": "Derive the exact dual equality before further estimates.",
                    }
                ],
                "needs_human": False,
            }
        else:
            data = {"status": "NO_OP", "summary": f"Mock role {role}"}
        import json

        text = "<ARIADNE_JSON>\n" + json.dumps(data, indent=2) + "\n</ARIADNE_JSON>\n"
        return ProviderResponse(text=text, usage=Usage())

    def _contract_author_response(self, prompt: str) -> dict[str, Any]:
        mode = self._extract_research_mode(prompt)
        return {
            "problem_contract": {
                "problem_id": "MOCK-SETUP-001",
                "title": "Mock generated research contract",
                "research_mode": mode,
                "statement": {
                    "text": "Prove or refute the exact theorem described in the owner interview.",
                    "formal_quantifier_outline": "forall admissible X, hypotheses(X) -> conclusion(X)",
                },
                "definitions": {},
                "domains": {},
                "hypotheses": [],
                "conclusion": ["the exact requested conclusion"],
                "uniformity": {
                    "constants_may_depend_on": [],
                    "constants_may_not_depend_on": [],
                },
                "success_criteria": {
                    "proof": "Complete deductive proof of the exact statement",
                    "refutation": "Exact counterexample with rigorous verification",
                },
                "formalization_policy": {
                    "lean_allowed_only_after_human_checked_proof": True,
                    "lean_role": "terminal certification only",
                },
            },
            "validation_notes": ["Mock provider generated a structurally valid contract."],
        }

    def _literature_author_response(self, prompt: str) -> dict[str, Any]:
        mode = self._extract_research_mode(prompt)
        guided = mode == "literature_guided"
        offline_only = mode == "offline_only"
        if guided:
            document_type = "shared_literature_dossier"
            markdown = (
                "# Shared literature dossier\n\n"
                "This deterministic mock dossier records one source route and one open bridge.\n"
            )
        elif offline_only:
            document_type = "parked_literature_dossier"
            markdown = (
                "# Parked literature dossier\n\n"
                "This dossier is retained for later human use and is not shared with offline researchers.\n"
            )
        else:
            document_type = "literature_sentinel"
            markdown = (
                "# Hidden literature sentinel dossier\n\n"
                "Known route R1 uses the fixed-weight mechanism; intervene only on a mechanism-level match.\n"
            )
        return {
            "document_type": document_type,
            "markdown": markdown,
            "sources": [
                {
                    "id": "SRC-MOCK-001",
                    "citation": "Mock source",
                    "status": "project demonstration",
                    "locator": "mock section 1",
                }
            ],
            "warnings": ["This is deterministic mock literature, not an external source audit."],
        }

    def _literature_research_response(
        self, slot: str, epoch: int, prompt: str
    ) -> dict[str, Any]:
        base = self._offline_response(slot.replace("literature", "offline"), epoch, prompt)
        base["source_claims"] = [
            {
                "statement": "The mock literature provides a fixed-weight baseline theorem.",
                "assumptions": ["fixed weight"],
                "scope": "mock baseline only",
                "criticality": "supporting",
                "source_ref": "SRC-MOCK-001",
                "applicability": "Applies only to the fixed-weight comparison route.",
            }
        ]
        return base

    def _offline_response(self, slot: str, epoch: int, prompt: str) -> dict[str, Any]:
        if slot.endswith("1"):
            if epoch == 1:
                return {
                    "route": {
                        "title": "Weighted coercivity route",
                        "mode": "DEDUCTIVE",
                        "method_family": "energy estimate with Gronwall",
                        "representation": "time-dependent weighted energy",
                        "key_lemma": "a cross-term produces coercivity without the standard loss",
                        "central_mechanism": "choose a dynamic weight that absorbs the bad term",
                        "decisive_test": "derive the cross-term identity exactly",
                        "difference_from_existing": "the weight evolves with the solution rather than remaining fixed",
                        "independence_cluster": "weighted-energy",
                        "revives_route_id": "",
                        "revival_certificate": "",
                    },
                    "status": "PROGRESS",
                    "summary": "A weighted energy route is proposed; the cross-term identity is the load-bearing step.",
                    "claims": [],
                    "failures": [],
                    "decisive_events": [
                        {
                            "type": "PRECISE_BRIDGE",
                            "description": "Isolated the exact cross-term identity as the decisive bridge.",
                        }
                    ],
                    "novelty_evidence": [],
                    "next_task": "Derive the identity and compare it with the unweighted energy method.",
                    "proof_candidate": None,
                    "counterexample_candidate": None,
                    "experiment_request": None,
                }
            return {
                "route": None,
                "status": "PROGRESS",
                "summary": "The dynamic weight yields an additional derivative term not present in the standard fixed-weight proof.",
                "claims": [
                    {
                        "statement": "The dynamic weight creates an exact cross-term that is absent from the fixed-weight energy identity.",
                        "assumptions": ["sufficient differentiability"],
                        "scope": "route-local identity",
                        "criticality": "load-bearing",
                    }
                ],
                "failures": [],
                "decisive_events": [
                    {
                        "type": "LOAD_BEARING_LEMMA",
                        "description": "Derived the distinct cross-term identity at the formal algebraic level.",
                    }
                ],
                "novelty_evidence": [
                    {
                        "intervention_id": self._extract_id(prompt, "INT"),
                        "evidence": "The known route fixes the weight; this route differentiates the weight and uses the resulting term.",
                    }
                ],
                "next_task": "Determine whether the new term has the required sign uniformly.",
                "proof_candidate": None,
                "counterexample_candidate": None,
                "experiment_request": None,
            }

        if epoch == 1:
            return {
                "route": {
                    "title": "Endpoint refutation route",
                    "mode": "REFUTATIONAL",
                    "method_family": "degenerate and endpoint analysis",
                    "representation": "scaled extremal family",
                    "key_lemma": "any uniform estimate must survive the singular scaling limit",
                    "central_mechanism": "construct a family concentrating at the endpoint",
                    "decisive_test": "evaluate the exact scaling of both sides",
                    "difference_from_existing": "attacks truth rather than proving the favored estimate",
                    "independence_cluster": "endpoint-refutation",
                    "revives_route_id": "",
                    "revival_certificate": "",
                },
                "status": "PROGRESS",
                "summary": "The refutation route isolates a singular family but has not produced a counterexample.",
                "claims": [],
                "failures": [],
                "decisive_events": [
                    {
                        "type": "PRECISE_BRIDGE",
                        "description": "Identified the endpoint scaling that any proof must control.",
                    }
                ],
                "novelty_evidence": [],
                "next_task": "Compute the exact leading powers symbolically, not numerically.",
                "proof_candidate": None,
                "counterexample_candidate": None,
                "experiment_request": None,
            }
        return {
            "route": None,
            "status": "BLOCKED",
            "summary": "The candidate family is inconclusive because both sides scale equally.",
            "claims": [],
            "failures": [
                {
                    "failure_class": "COMPUTATION_INCONCLUSIVE",
                    "signature": "endpoint family gives equal scaling on both sides",
                    "logical_scope": "this particular concentrating family only",
                    "revival_conditions": "find a family with an additional logarithmic or oscillatory defect",
                }
            ],
            "decisive_events": [],
            "novelty_evidence": [],
            "next_task": "Park this family and seek a structurally different obstruction.",
            "proof_candidate": None,
            "counterexample_candidate": None,
            "experiment_request": None,
        }

    def _sentinel_response(self, epoch: int, prompt: str) -> dict[str, Any]:
        route_ids = re.findall(r"RTE-[0-9a-f]+", prompt)
        if epoch == 1 and route_ids:
            return {
                "interventions": [
                    {
                        "route_id": route_ids[0],
                        "kind": "KNOWN_ROUTE",
                        "source_refs": ["SRC-MOCK-ENERGY"],
                        "message": "The route resembles the classical fixed-weight energy/Gronwall method, which is known to retain the loss under discussion.",
                        "early_stop": True,
                        "applicability_conditions": [
                            "the weight is fixed or its derivative contributes no favorable term"
                        ],
                        "confidence": "medium",
                        "evidence_status": "EXACT_VERIFIED",
                    }
                ]
            }
        if epoch == 2 and route_ids:
            return {
                "interventions": [
                    {
                        "route_id": route_ids[0],
                        "kind": "DIFFERENCE_CONFIRMED",
                        "source_refs": ["SRC-MOCK-ENERGY"],
                        "message": "The submitted cross-term shows a material difference from the fixed-weight route; withdraw the early-stop proposal.",
                        "early_stop": False,
                        "applicability_conditions": [],
                        "confidence": "medium",
                        "evidence_status": "EXACT_VERIFIED",
                    }
                ]
            }
        return {"interventions": []}

    def _intervention_response(self, slot: str, epoch: int, prompt: str) -> dict[str, Any]:
        return {
            "decision": "REJECT_DIFFERENT_ROUTE",
            "reason": "The intervention covers fixed-weight energy estimates, while this route differentiates the weight and uses the extra term.",
            "difference_certificate": {
                "assumptions_difference": "The weight is a dynamic unknown chosen along the evolution.",
                "representation_difference": "Time-dependent weighted energy rather than a fixed norm.",
                "key_lemma_difference": "A derivative-of-weight cross-term supplies the proposed coercivity.",
                "outcome_difference": "Could remove the loss rather than merely reproduce the classical bound.",
                "decisive_test": "Derive the exact cross-term and prove its sign.",
            },
            "proposed_test": "Derive and audit the dynamic-weight identity in the next epoch.",
        }

    @staticmethod
    def _extract_id(text: str, prefix: str) -> str:
        match = re.search(rf"{prefix}-[0-9a-f]+", text)
        return match.group(0) if match else ""

    @staticmethod
    def _extract_research_mode(text: str) -> str:
        match = re.search(
            r'"research_mode"\s*:\s*"(offline_sentinel|offline_only|literature_guided)"',
            text,
        )
        return match.group(1) if match else "offline_sentinel"


def create_provider(config: ProviderConfig) -> BaseProvider:
    if config.kind == "mock":
        return MockProvider(config)
    if config.kind == "command":
        return CommandProvider(config)
    raise ProviderError(f"Unknown provider kind {config.kind!r}")
