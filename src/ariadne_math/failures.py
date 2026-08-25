from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .util import normalize_signature


@dataclass(frozen=True)
class FailureFingerprint:
    failure_class: str
    signature: str
    logical_scope: str

    @property
    def canonical_key(self) -> str:
        return "|".join(
            [
                self.failure_class,
                normalize_signature(self.signature),
                normalize_signature(self.logical_scope),
            ]
        )


def fingerprint_failure(item: dict[str, Any]) -> FailureFingerprint:
    return FailureFingerprint(
        failure_class=str(item.get("failure_class", "REPEATED_NO_NEW_INFORMATION")),
        signature=str(item.get("signature", "unspecified obstruction")),
        logical_scope=str(item.get("logical_scope", "unspecified scope")),
    )


def novelty_certificate_present(certificate: dict[str, Any] | None) -> bool:
    if not isinstance(certificate, dict):
        return False
    fields = (
        "assumptions_difference",
        "representation_difference",
        "key_lemma_difference",
        "outcome_difference",
        "decisive_test",
    )
    substantive = [str(certificate.get(field, "")).strip() for field in fields]
    return bool(substantive[-1]) and any(substantive[:-1])
