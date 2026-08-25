from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .enums import CampaignStatus, ClaimStatus
from .store import ResearchStore
from .util import ensure_dir, utc_now


def problem_verdict(store: ResearchStore) -> str:
    root_id = store.get_meta("root_claim_id")
    if not root_id:
        return "UNINITIALIZED"
    campaign = store.latest_campaign()
    status = str(store.get_claim(root_id)["status"])
    if campaign and str(campaign.get("status")) == CampaignStatus.CONTRACT_CHANGED:
        return CampaignStatus.CONTRACT_CHANGED
    if campaign and str(campaign.get("status")) == "COMPLETE_PROOF_CANDIDATE":
        return "PROOF_CANDIDATE_UNVERIFIED"
    if campaign and str(campaign.get("status")) == "REFUTATION_CANDIDATE":
        return "REFUTATION_CANDIDATE_AUDITED"
    if status == ClaimStatus.FORMALLY_CERTIFIED:
        return "FORMALLY_CERTIFIED"
    if status == ClaimStatus.HUMAN_CHECKED:
        return "HUMAN_CHECKED"
    if status == ClaimStatus.REFUTED:
        return "REFUTED"
    if status in {ClaimStatus.AGENT_AUDITED_LOCAL, ClaimStatus.AGENT_AUDITED_GLOBAL}:
        return "PROOF_CANDIDATE_AGENT_AUDITED"
    return "UNSOLVED"


def _latex_escape(value: Any) -> str:
    replacements = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%",
        "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
        "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    def escape_char(char: str) -> str:
        if char in replacements:
            return replacements[char]
        if ord(char) < 128:
            return char
        # Journal notes are compiled with pdfLaTeX. Do not let arbitrary
        # model-emitted Unicode glyphs make a generated note unreadable.
        return f"[U+{ord(char):04X}]"

    return "".join(escape_char(char) for char in str(value or ""))


def _latex_paragraph(value: Any) -> str:
    return _latex_escape(value).replace("\n", "\n\n")


_UNSAFE_PROOF_LATEX = (
    "\\input", "\\include", "\\openin", "\\openout", "\\read", "\\write",
    "\\immediate", "\\catcode", "\\usepackage", "\\documentclass",
    "\\begin{document}", "\\end{document}",
)
_MARKDOWN_PROOF_MARKERS = ("```", "<!--", "-->")


class LatexValidationError(ValueError):
    """Raised when a model response is not a portable LaTeX proof body."""


def validate_proof_latex(value: Any) -> str:
    """Validate a proof body before it enters a ``.tex``/PDF deliverable.

    Do not silently translate mathematical Unicode or Markdown. Such a
    conversion can change notation or scope; the proof-expander must return an
    explicit, reviewable LaTeX proof body.
    """
    proof = str(value or "").strip()
    if not proof:
        raise LatexValidationError("empty proof body")
    unsafe = next((token for token in _UNSAFE_PROOF_LATEX if token in proof.casefold()), None)
    if unsafe:
        raise LatexValidationError(f"unsafe LaTeX command {unsafe!r}")
    marker = next((item for item in _MARKDOWN_PROOF_MARKERS if item in proof), None)
    if marker:
        raise LatexValidationError(f"Markdown marker {marker!r} is not LaTeX")
    unicode_chars = sorted({char for char in proof if ord(char) > 127})
    if unicode_chars:
        rendered = ", ".join(f"U+{ord(char):04X}" for char in unicode_chars[:8])
        raise LatexValidationError(
            "non-ASCII characters in proof body (use explicit LaTeX commands): " + rendered
        )
    return proof


def _safe_proof_latex(value: Any) -> str | None:
    """Return a portable proof body, never a full document or I/O code."""
    try:
        return validate_proof_latex(value)
    except LatexValidationError:
        return None


def write_proof_candidate_note(
    store: ResearchStore,
    *,
    proof_candidate: dict[str, Any],
    route_id: str,
    artifact_id: str,
) -> tuple[Path | None, Path | None]:
    '''Write a journal-style, explicitly unverified candidate-proof note.'''
    ensure_dir(store.paths.reports)
    stem = f"proof_candidate_{artifact_id.lower()}"
    tex_path = store.paths.reports / f"{stem}.tex"
    pdf_path = store.paths.reports / f"{stem}.pdf"
    root_id = store.get_meta("root_claim_id")
    root_claim = store.get_claim(root_id) if root_id else {}
    route = store.get_route(route_id)
    assumptions = proof_candidate.get("assumptions") or []
    obligations = proof_candidate.get("open_obligations") or []
    proof = proof_candidate.get("proof_latex") or proof_candidate.get("proof", "")
    proof_latex = _safe_proof_latex(proof)
    if proof_latex is None:
        proof_body = (
            "\\textit{The submitted proof text could not be safely compiled as LaTeX. "
            "It is retained verbatim below.}\\par\\medskip\n"
            "\\begin{verbatim}\n" + str(proof or "(No proof text submitted.)")
            + "\n\\end{verbatim}"
        )
    else:
        proof_body = "\\begin{proof}\n" + proof_latex + "\n\\end{proof}"
    assumption_text = ("\\begin{itemize}\n" + "\n".join(
        f"\\item {_latex_escape(item)}" for item in assumptions
    ) + "\n\\end{itemize}") if assumptions else "None stated."
    obligation_text = ("\\begin{itemize}\n" + "\n".join(
        f"\\item {_latex_escape(item)}" for item in obligations
    ) + "\n\\end{itemize}") if obligations else "None stated."
    tex = f'''\\documentclass[11pt]{{article}}
\\usepackage[margin=1in]{{geometry}}
\\usepackage{{amsmath,amssymb,amsthm}}
\\usepackage[T1]{{fontenc}}
\\usepackage{{lmodern}}
\\usepackage{{hyperref}}
\\title{{Proof Candidate Note}}
\\author{{Ariadne Mathematical Research Harness}}
\\date{{{_latex_escape(utc_now())}}}
\\begin{{document}}
\\maketitle
\\begin{{center}}
\\fbox{{\\parbox{{0.90\\textwidth}}{{\\centering\\bfseries UNVERIFIED PROOF CANDIDATE\\\\
This note records an agent-submitted candidate. It is not a proof certificate, has not necessarily passed independent audit, and must not be formalized until human approval.}}}}
\\end{{center}}
\\begin{{abstract}}
This note records the submitted proof text for route \\texttt{{{_latex_escape(route_id)}}}. The route and proof remain subject to local, global, and human review.
\\end{{abstract}}
\\section{{Problem}}
\\textbf{{Claim ID:}} \\texttt{{{_latex_escape(root_id)}}}\\par
\\textbf{{Statement:}} {_latex_paragraph(root_claim.get("statement", proof_candidate.get("statement", "")))}
\\section{{Assumptions}}
{assumption_text}
\\section{{Route}}
\\textbf{{Title:}} {_latex_escape(route.get("title", ""))}\\par
\\textbf{{Method:}} {_latex_escape(route.get("method_family", ""))}\\par
\\textbf{{Representation:}} {_latex_escape(route.get("representation", ""))}\\par
\\textbf{{Key bridge:}} {_latex_paragraph(route.get("key_lemma", ""))}
\\section{{Submitted Proof}}
{proof_body}
\\section{{Open Obligations}}
{obligation_text}
\\section{{Review Status}}
The associated artifact is \\texttt{{{_latex_escape(artifact_id)}}}. Its logical force is limited to a candidate natural-language proof. Independent verification and a complete human proof check are required before promotion or Lean formalization.
\\end{{document}}
'''
    if proof_latex is None:
        return None, None
    tex_path.write_text(tex, encoding="utf-8")
    pdf = _compile_latex(tex_path, pdf_path)
    if pdf is None:
        tex_path.unlink(missing_ok=True)
        pdf_path.unlink(missing_ok=True)
        return None, None
    return tex_path, pdf


def _artifact_text(store: ResearchStore, artifact_id: str) -> str | None:
    try:
        artifact = store.get_artifact(artifact_id)
        return (store.paths.root / str(artifact["relative_path"])).read_text(
            encoding="utf-8", errors="replace"
        )
    except (KeyError, OSError, UnicodeError):
        return None


def _audits_for_proof(
    store: ResearchStore, *, root_claim_id: str, proof_artifact_id: str
) -> dict[str, dict[str, Any]]:
    '''Return only audits explicitly bound to this exact proof artifact.'''
    matched: dict[str, dict[str, Any]] = {}
    for audit in store.list_audits(target_type="claim", target_id=root_claim_id):
        audit_type = str(audit.get("audit_type", ""))
        if audit_type not in {"LOCAL_PROOF_AUDIT", "GLOBAL_PROOF_AUDIT"}:
            continue
        artifact_id = audit.get("artifact_id")
        if not artifact_id:
            continue
        try:
            audit_artifact = store.get_artifact(str(artifact_id))
        except KeyError:
            continue
        if str(audit_artifact.get("metadata", {}).get("proof_artifact_id", "")) != proof_artifact_id:
            continue
        matched[audit_type] = audit
    return matched


def _compile_latex(tex_path: Path, pdf_path: Path) -> Path | None:
    if shutil.which("pdflatex") is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="ariadne-journal-proof-") as build_dir:
            build_path = Path(build_dir)
            build_tex = build_path / tex_path.name
            build_tex.write_text(tex_path.read_text(encoding="utf-8"), encoding="utf-8")
            env = dict(os.environ, openin_any="p", openout_any="p")
            subprocess.run(
                ["pdflatex", "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", build_tex.name],
                cwd=build_path, env=env, capture_output=True, text=True, timeout=120, check=True,
            )
            built_pdf = build_path / pdf_path.name
            if built_pdf.exists():
                shutil.copy2(built_pdf, pdf_path)
    except (OSError, subprocess.SubprocessError):
        return None
    return pdf_path if pdf_path.exists() else None


def write_agent_audited_proof_report(
    store: ResearchStore, *, proof_artifact_id: str | None = None
) -> list[tuple[Path, Path | None]]:
    '''Write journal notes only for proof artifacts passing both clean audits.'''
    root_id = store.get_meta("root_claim_id")
    if not root_id:
        return []
    if proof_artifact_id:
        try:
            proof_artifacts = [store.get_artifact(proof_artifact_id)]
        except KeyError:
            return []
    else:
        proof_artifacts = store.list_artifacts(kind="proof_candidate_latex")
    reports: list[tuple[Path, Path | None]] = []
    for proof_artifact in proof_artifacts:
        if str(proof_artifact.get("kind")) != "proof_candidate_latex":
            continue
        artifact_id = str(proof_artifact["artifact_id"])
        metadata = proof_artifact.get("metadata", {})
        claim_id = str(metadata.get("root_claim_id") or root_id)
        audits = _audits_for_proof(
            store, root_claim_id=claim_id, proof_artifact_id=artifact_id
        )
        if {
            "LOCAL_PROOF_AUDIT", "GLOBAL_PROOF_AUDIT"
        } != set(audits) or any(str(item.get("verdict")) != "PASS" for item in audits.values()):
            continue
        packet: dict[str, Any] | None = None
        for item in store.list_artifacts(kind="proof_candidate_record"):
            record_metadata = item.get("metadata", {})
            if str(record_metadata.get("proof_artifact_id", "")) != artifact_id:
                continue
            raw_packet = _artifact_text(store, str(item["artifact_id"]))
            try:
                parsed = json.loads(raw_packet or "")
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                packet = parsed
            break
        # Older proof artifacts did not retain the full packet needed for a
        # literature-aware journal report, so do not reconstruct one loosely.
        if packet is None:
            continue
        proof_latex = _safe_proof_latex(_artifact_text(store, artifact_id))
        if proof_latex is None:
            continue
        try:
            root_claim = store.get_claim(claim_id)
            route = store.get_route(str(metadata.get("route_id", "")))
        except KeyError:
            continue
        assumptions = packet.get("assumptions") or []
        sources = packet.get("sources") or []
        review = packet.get("literature_review") or "No literature review was supplied for this route."
        obligations = packet.get("open_obligations") or []
        audit_items = "\n".join(
            f"\\item \\textbf{{{_latex_escape(audit_type)}}}: PASS -- "
            f"{_latex_paragraph(audit.get('minimal_obligation') or 'No failed obligation reported.')}"
            for audit_type, audit in sorted(audits.items())
        )
        assumption_items = "\n".join(f"\\item {_latex_escape(item)}" for item in assumptions) or "\\item None stated."
        source_items = "\n".join(f"\\item {_latex_escape(item)}" for item in sources) or "\\item No sources supplied."
        obligation_items = "\n".join(f"\\item {_latex_escape(item)}" for item in obligations) or "\\item None stated."
        stem = f"agent_audited_proof_{artifact_id.lower()}"
        ensure_dir(store.paths.reports)
        tex_path = store.paths.reports / f"{stem}.tex"
        pdf_path = store.paths.reports / f"{stem}.pdf"
        tex = f'''\\documentclass[11pt]{{article}}
\\usepackage[margin=1in]{{geometry}}
\\usepackage{{amsmath,amssymb,amsthm}}
\\usepackage[T1]{{fontenc}}
\\usepackage{{lmodern}}
\\usepackage{{hyperref}}
\\title{{{_latex_escape(root_claim.get("statement", "Mathematical result"))}}}
\\author{{Ariadne Mathematical Research Harness}}
\\date{{{_latex_escape(utc_now())}}}
\\begin{{document}}
\\maketitle
\\begin{{abstract}}
This journal-style note records a complete agent-submitted proof for the stated problem. It passed one local and one fresh global agent audit bound to immutable proof artifact \\texttt{{{_latex_escape(artifact_id)}}}. This is not a human review or a formal certificate.
\\end{{abstract}}
\\section{{Statement}}
{_latex_paragraph(root_claim.get("statement", ""))}
\\section{{Assumptions and scope}}
\\begin{{itemize}}
{assumption_items}
\\end{{itemize}}
\\section{{Route and method}}
\\textbf{{Route:}} {_latex_escape(route.get("title", ""))}\\par
\\textbf{{Method:}} {_latex_escape(route.get("method_family", ""))}\\par
\\textbf{{Representation:}} {_latex_escape(route.get("representation", ""))}\\par
\\textbf{{Load-bearing bridge:}} {_latex_paragraph(route.get("key_lemma", ""))}
\\section{{Literature review and provenance}}
{_latex_paragraph(review)}
\\begin{{itemize}}
{source_items}
\\end{{itemize}}
\\section{{Complete proof}}
\\begin{{proof}}
{proof_latex}
\\end{{proof}}
\\section{{Independent agent audits}}
\\begin{{itemize}}
{audit_items}
\\end{{itemize}}
\\section{{Open obligations}}
\\begin{{itemize}}
{obligation_items}
\\end{{itemize}}
\\section{{Status and next gate}}
The proof and its two independent agent audits are retained as immutable artifacts. A human must still check the full proof before it is treated as established; only then may Lean be used as a late certification step.
\\end{{document}}
'''
        tex_path.write_text(tex, encoding="utf-8")
        pdf = _compile_latex(tex_path, pdf_path)
        if pdf is None:
            tex_path.unlink(missing_ok=True)
            pdf_path.unlink(missing_ok=True)
            continue
        reports.append((tex_path, pdf))
    return reports


def _audits_for_counterexample(
    store: ResearchStore, *, root_claim_id: str, counterexample_artifact_id: str
) -> dict[str, dict[str, Any]]:
    matched: dict[str, dict[str, Any]] = {}
    expected = {"LOCAL_COUNTEREXAMPLE_AUDIT", "GLOBAL_COUNTEREXAMPLE_AUDIT"}
    for audit in store.list_audits(target_type="claim", target_id=root_claim_id):
        audit_type = str(audit.get("audit_type", ""))
        if audit_type not in expected or not audit.get("artifact_id"):
            continue
        try:
            artifact = store.get_artifact(str(audit["artifact_id"]))
        except KeyError:
            continue
        if str(artifact.get("metadata", {}).get("counterexample_artifact_id", "")) == counterexample_artifact_id:
            matched[audit_type] = audit
    return matched


def _complete_counterexample_audit(audit: dict[str, Any], *, source_reported: bool) -> bool:
    fields = ("verified_object", "verified_admissibility", "verified_violation")
    if str(audit.get("verdict", "")) != "PASS" or bool(audit.get("statement_drift", False)):
        return False
    if not all(isinstance(audit.get(field), str) and audit[field].strip() for field in fields):
        return False
    return not source_reported or (
        isinstance(audit.get("verified_source_independence"), str)
        and bool(audit["verified_source_independence"].strip())
    )


def write_agent_audited_counterexample_note(
    store: ResearchStore, *, counterexample_artifact_id: str
) -> list[tuple[Path, Path | None]]:
    root_id = store.get_meta("root_claim_id")
    if not root_id:
        return []
    try:
        candidate_artifact = store.get_artifact(counterexample_artifact_id)
        candidate = json.loads(_artifact_text(store, counterexample_artifact_id) or "")
    except (KeyError, TypeError, ValueError):
        return []
    if str(candidate_artifact.get("kind", "")) != "counterexample_candidate" or not isinstance(candidate, dict):
        return []
    metadata = candidate_artifact.get("metadata", {})
    claim_id = str(metadata.get("root_claim_id") or root_id)
    audits = _audits_for_counterexample(store, root_claim_id=claim_id, counterexample_artifact_id=counterexample_artifact_id)
    expected = {"LOCAL_COUNTEREXAMPLE_AUDIT", "GLOBAL_COUNTEREXAMPLE_AUDIT"}
    if set(audits) != expected or any(str(value.get("verdict", "")) != "PASS" for value in audits.values()):
        return []
    source_reported = str(candidate.get("origin", "")).upper() == "REFERENCE_REPORTED" or bool(str(candidate.get("source_reference", "")).strip())
    packets: dict[str, dict[str, Any]] = {}
    for audit_type, audit in audits.items():
        try:
            packet = json.loads(_artifact_text(store, str(audit["artifact_id"])) or "")
        except (TypeError, ValueError):
            return []
        if not isinstance(packet, dict) or not _complete_counterexample_audit(packet, source_reported=source_reported):
            return []
        packets[audit_type] = packet
    try:
        root_claim = store.get_claim(claim_id)
        route = store.get_route(str(metadata.get("route_id", "")))
    except KeyError:
        return []
    audits_latex = "\n".join(
        "\\subsection*{" + _latex_escape(kind) + "}\n"
        "\\textbf{Verified object.} " + _latex_paragraph(packet["verified_object"]) + "\\par\n"
        "\\textbf{Verified admissibility.} " + _latex_paragraph(packet["verified_admissibility"]) + "\\par\n"
        "\\textbf{Verified violation.} " + _latex_paragraph(packet["verified_violation"])
        for kind, packet in sorted(packets.items())
    )
    provenance = ""
    if source_reported:
        checks = "\n".join(
            "\\item \\textbf{" + _latex_escape(kind) + "}: " + _latex_paragraph(packet["verified_source_independence"])
            for kind, packet in sorted(packets.items())
        )
        provenance = "\\section{Source provenance and independent reconstruction}\n\\textbf{Reported source:} " + _latex_paragraph(candidate.get("source_reference", "")) + "\n\\begin{itemize}\n" + checks + "\n\\end{itemize}\n"
    ensure_dir(store.paths.reports)
    stem = f"agent_audited_counterexample_{counterexample_artifact_id.lower()}"
    tex_path = store.paths.reports / f"{stem}.tex"
    pdf_path = store.paths.reports / f"{stem}.pdf"
    tex = f'''\\documentclass[11pt]{{article}}
\\usepackage[margin=1in]{{geometry}}
\\usepackage{{amsmath,amssymb,amsthm}}
\\usepackage[T1]{{fontenc}}
\\usepackage{{lmodern}}
\\usepackage{{hyperref}}
\\title{{Audited Counterexample Candidate}}
\\author{{Ariadne Mathematical Research Harness}}
\\date{{{_latex_escape(utc_now())}}}
\\begin{{document}}
\\maketitle
\\begin{{center}}
\\fbox{{\\parbox{{0.90\\textwidth}}{{\\centering\\bfseries AUDITED REFUTATION CANDIDATE\\\\
The exact counterexample below passed one local and one fresh global agent audit, both bound to one immutable candidate artifact. It is not a human review or a formal certificate.}}}}
\\end{{center}}
\\begin{{abstract}}
This journal-style note records an exact counterexample candidate. Each audit independently verifies the witness, its admissibility, and the failed conclusion.
\\end{{abstract}}
\\section{{Statement being tested}}
{_latex_paragraph(root_claim.get("statement", ""))}
\\section{{Route and scope}}
\\textbf{{Route:}} {_latex_escape(route.get("title", ""))}\\par
\\textbf{{Method:}} {_latex_escape(route.get("method_family", ""))}\\par
\\textbf{{Exact scope:}} {_latex_paragraph(candidate.get("scope", ""))}
\\section{{Counterexample candidate}}
\\textbf{{Witness and construction.}} {_latex_paragraph(candidate.get("description", ""))}\\par
\\textbf{{Submitted direct verification.}} {_latex_paragraph(candidate.get("verification", ""))}\\par
\\textbf{{Immutable candidate artifact.}} \\texttt{{{_latex_escape(counterexample_artifact_id)}}}
{provenance}\\section{{Independent counterexample audits}}
{audits_latex}
\\section{{Status and next gate}}
The harness may classify this result as \\texttt{{REFUTATION\\_CANDIDATE}} because both independent audits accepted the same immutable witness. A human must still verify the full counterexample and intended statement before it is treated as a final mathematical refutation.
\\end{{document}}
'''
    tex_path.write_text(tex, encoding="utf-8")
    pdf = _compile_latex(tex_path, pdf_path)
    if pdf is None:
        tex_path.unlink(missing_ok=True)
        pdf_path.unlink(missing_ok=True)
        return []
    return [(tex_path, pdf)]


def write_unsolved_campaign_note(store: ResearchStore, *, campaign_id: str) -> tuple[Path, Path | None] | None:
    try:
        campaign = store.get_campaign(campaign_id)
    except KeyError:
        return None
    if str(campaign.get("status")) not in {
        CampaignStatus.COMPLETED_UNSOLVED,
        CampaignStatus.BUDGET_EXHAUSTED,
    }:
        return None
    root_id = store.get_meta("root_claim_id")
    if not root_id:
        return None
    try:
        root_claim = store.get_claim(root_id)
    except KeyError:
        return None
    routes = store.list_routes(campaign_id)
    attempts = [item for item in store.list_attempts() if str(item.get("campaign_id")) == campaign_id]
    audits = store.list_audits(target_type="claim", target_id=root_id)
    failures = store.list_failures()
    synthesis_artifacts = [
        item for item in store.list_artifacts(kind="strongest_partial_result")
        if str(item.get("metadata", {}).get("campaign_id", "")) == campaign_id
    ]
    synthesis_latex = "\n".join(
        "\\item \textbf{" + _latex_escape(item.get("artifact_id", "")) + "}\\par\n"
        + "Stored artifact: \texttt{" + _latex_escape(item.get("relative_path", "")) + "}. "
        + "This is a non-decisive, artifact-backed scoped result; it does not settle the immutable target."
        for item in synthesis_artifacts
    ) or "\\item No meaningful artifact-backed partial result was proposed."
    routes_latex = "\n".join(
        "\\item \\textbf{" + _latex_escape(route.get("route_id", "")) + " -- " + _latex_escape(route.get("title", "")) + "} [" + _latex_escape(route.get("status", "")) + "]\\par\n"
        + "Method: " + _latex_paragraph(route.get("method_family", "")) + "\\par\n"
        + "Load-bearing bridge: " + _latex_paragraph(route.get("key_lemma", "")) + "\\par\n"
        + "Decisive test: " + _latex_paragraph(route.get("decisive_test", ""))
        for route in routes
    ) or "\\item No research routes were recorded."
    attempts_latex = "\n".join(
        "\\item \\textbf{" + _latex_escape(attempt.get("attempt_id", "")) + "} (epoch " + _latex_escape(attempt.get("epoch", "")) + ", " + _latex_escape(attempt.get("agent_slot", "")) + ", " + _latex_escape(attempt.get("result_kind", "")) + ")\\par\n"
        + "Task: " + _latex_paragraph(attempt.get("task", "")) + "\\par\n"
        + "Outcome: " + _latex_paragraph(attempt.get("summary", ""))
        for attempt in attempts
    ) or "\\item No bounded research attempts were recorded."
    failures_latex = "\n".join(
        "\\item \\textbf{" + _latex_escape(failure.get("failure_class", "")) + " -- " + _latex_escape(failure.get("signature", "")) + "}\\par\n"
        + "Logical scope: " + _latex_paragraph(failure.get("logical_scope", "")) + "\\par\n"
        + "Revival condition: " + _latex_paragraph(failure.get("revival_conditions", ""))
        for failure in failures
    ) or "\\item No canonical failure clusters were recorded."
    audits_latex = "\n".join(
        "\\item \\textbf{" + _latex_escape(audit.get("audit_type", "")) + "}: " + _latex_escape(audit.get("verdict", "")) + "\\par\n"
        + "Remaining obligation: " + _latex_paragraph(audit.get("minimal_obligation", ""))
        for audit in audits
    ) or "\\item No independent audit records were produced."
    ensure_dir(store.paths.reports)
    stem = f"unsolved_campaign_{campaign_id.lower()}"
    tex_path = store.paths.reports / f"{stem}.tex"
    pdf_path = store.paths.reports / f"{stem}.pdf"
    tex = f'''\\documentclass[11pt]{{article}}
\\usepackage[margin=1in]{{geometry}}
\\usepackage{{amsmath,amssymb,amsthm}}
\\usepackage[T1]{{fontenc}}
\\usepackage{{lmodern}}
\\usepackage{{hyperref}}
\\title{{Unresolved Campaign Research Record}}
\\author{{Ariadne Mathematical Research Harness}}
\\date{{{_latex_escape(utc_now())}}}
\\begin{{document}}
\\maketitle
\\begin{{center}}
\\fbox{{\\parbox{{0.90\\textwidth}}{{\\centering\\bfseries UNRESOLVED CAMPAIGN HANDOFF\\\\
This note is a research record. Failed routes and unverified evidence do not refute the stated proposition.}}}}
\\end{{center}}
\\section{{Problem}}
{_latex_paragraph(root_claim.get("statement", ""))}
\\section{{Campaign scope and budget}}
\\begin{{itemize}}
\\item Campaign: \\texttt{{{_latex_escape(campaign_id)}}}
\\item Mode: {_latex_escape(campaign.get("mode", ""))}
\\item Completed epochs: {int(campaign.get("epoch", 0))} / {int(campaign.get("max_epochs", 0))}
\\item Provider calls: {int(campaign.get("calls_used", 0))} / {int(campaign.get("max_calls", 0))}
\\item Recorded cost: \\${float(campaign.get("cost_used", 0.0)):.4f} / \\${float(campaign.get("max_cost_usd", 0.0)):.2f}
\\end{{itemize}}
\\section{{Research routes attempted}}
\\begin{{itemize}}
{routes_latex}
\\end{{itemize}}
\\section{{Bounded research attempts and outcomes}}
\\begin{{itemize}}
{attempts_latex}
\\end{{itemize}}
\\section{{Why attempted routes did not settle the problem}}
\\begin{{itemize}}
{failures_latex}
\\end{{itemize}}
\\section{{Independent audits}}
\\begin{{itemize}}
{audits_latex}
\\end{{itemize}}
\\section{{Final partial-result synthesis}}
\\begin{{itemize}}
{synthesis_latex}
\\end{{itemize}}
\\section{{Conclusion and future work}}
The campaign ended without a complete proof or an independently audited exact counterexample. This record preserves the attempted routes, their bounded outcomes, and conditions under which a future campaign may revisit them. A continuation must preserve the immutable problem contract and target a recorded remaining obligation or genuinely new route.
\\end{{document}}
'''
    tex_path.write_text(tex, encoding="utf-8")
    pdf = _compile_latex(tex_path, pdf_path)
    if pdf is None:
        tex_path.unlink(missing_ok=True)
        pdf_path.unlink(missing_ok=True)
        return None
    return tex_path, pdf


def _continuation_handoff_lines(store: ResearchStore) -> list[str]:
    campaign = store.latest_campaign()
    root_id = store.get_meta("root_claim_id")
    root_claim = store.get_claim(root_id) if root_id else None
    lines = ["## Continuation handoff", ""]
    if campaign is None or root_claim is None:
        return lines + ["No campaign and immutable root claim are available yet.", ""]
    campaign_id = str(campaign["campaign_id"])
    fingerprint = store.get_meta("problem_contract_sha256", "UNSEALED")
    routes = store.list_routes(campaign_id)
    attempts = [item for item in store.list_attempts() if str(item.get("campaign_id")) == campaign_id]
    latest_attempt: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        if attempt.get("route_id"):
            latest_attempt[str(attempt["route_id"])] = attempt
    active_tasks = store.list_tasks(campaign_id, statuses=["RUNNING", "QUEUED"], limit=8)
    instructions = store.list_human_instructions(campaign_id, active_only=True)
    evidence = store.list_evidence(str(root_id))
    audits = store.list_audits(target_type="claim", target_id=str(root_id))
    failures = store.list_failures()

    lines += [
        "Use this section as the restart checklist. It separates immutable facts, unfinished work, and route-local failures.",
        "",
        "### Immutable anchor",
        "",
        f"- Statement: {root_claim['statement']}",
        f"- Root claim: `{root_id}`",
        f"- Frozen contract SHA-256: `{fingerprint}`",
        f"- Latest campaign: `{campaign_id}` -- `{campaign['status']}`",
        f"- Remaining budget: {max(0, int(campaign['max_epochs']) - int(campaign['epoch']))} epoch(s), "
        f"{max(0, int(campaign['max_calls']) - int(campaign['calls_used']))} call(s), "
        f"${max(0.0, float(campaign['max_cost_usd']) - float(campaign['cost_used'])):.4f}",
        "",
        "### First actions",
        "",
    ]
    if str(campaign["status"]) == CampaignStatus.CONTRACT_CHANGED:
        lines += [
            "1. Restore the exact frozen contract before any continuation, or create a new project for the revised statement.",
            "2. Do not resume this campaign with altered premises.",
        ]
    elif active_tasks:
        for index, task in enumerate(active_tasks[:3], 1):
            route = f" on route `{task['route_id']}`" if task.get("route_id") else ""
            lines.append(
                f"{index}. `{task['status']}` `{task['role']}`{route}: {task['summary']}"
            )
    else:
        active_routes = [route for route in routes if str(route.get("status")) == "ACTIVE"]
        if active_routes:
            for index, route in enumerate(active_routes[:3], 1):
                attempt = latest_attempt.get(str(route["route_id"]))
                prior = f" Last outcome: {attempt['summary']}" if attempt else ""
                lines.append(
                    f"{index}. Continue `{route['route_id']}` -- {route['title']}: "
                    f"test `{route['decisive_test']}`.{prior}"
                )
        elif failures:
            for index, failure in enumerate(failures[:3], 1):
                lines.append(
                    f"{index}. Revive `{failure['failure_id']}` only if: "
                    f"{failure['revival_conditions'] or 'a new route-specific obligation is supplied.'}"
                )
        else:
            lines.append("1. Define a fresh route with a falsifiable decisive test before spending further budget.")
    lines += ["", "### Route ledger", ""]
    if not routes:
        lines.append("No routes recorded.")
    for route in routes:
        attempt = latest_attempt.get(str(route["route_id"]))
        last = attempt["summary"] if attempt else "No bounded attempt recorded."
        lines += [
            f"- `{route['route_id']}` **{route['status']}** -- {route['title']}",
            f"  - Method: {route['method_family']}; decisive test: {route['decisive_test']}",
            f"  - Last outcome: {last}",
        ]
    lines += ["", "### Do not repeat without new evidence", ""]
    if not failures:
        lines.append("No compressed failure clusters recorded.")
    for failure in failures[:12]:
        lines += [
            f"- `{failure['failure_id']}` `{failure['failure_class']}`: {failure['signature']}",
            f"  - Scope: {failure['logical_scope']}",
            f"  - Revival condition: {failure['revival_conditions'] or 'not specified'}",
        ]
    lines += ["", "### Evidence and audit checkpoint", ""]
    if evidence:
        for item in evidence[-8:]:
            lines.append(
                f"- `{item['evidence_type']}` / `{item['status']}`: {item['logical_force']} "
                f"(artifact `{item['artifact_id'] or 'none'}`)"
            )
    else:
        lines.append("No evidence records for the root claim.")
    if audits:
        lines.append("")
        for audit in audits[-8:]:
            lines.append(
                f"- Audit `{audit['audit_type']}` by `{audit['auditor_profile']}`: "
                f"`{audit['verdict']}` -- {audit['minimal_obligation'] or 'no failed obligation recorded'}"
            )
    lines += ["", "### Active human directions", ""]
    if instructions:
        for item in instructions:
            route = f" route `{item['route_id']}`" if item.get("route_id") else " campaign-wide"
            lines.append(f"- `{item['instruction_id']}` [{item['audience']};{route}]: {item['instruction_text']}")
    else:
        lines.append("No active human directions.")
    lines += ["", "### Artifact quick index", ""]
    references = []
    for attempt in attempts[-12:]:
        if attempt.get("artifact_id"):
            references.append((str(attempt["artifact_id"]), "attempt", str(attempt.get("summary", ""))))
    for item in evidence[-8:]:
        if item.get("artifact_id"):
            references.append((str(item["artifact_id"]), "evidence", str(item.get("evidence_type", ""))))
    for audit in audits[-8:]:
        if audit.get("artifact_id"):
            references.append((str(audit["artifact_id"]), "audit", str(audit.get("audit_type", ""))))
    seen: set[str] = set()
    for artifact_id, kind, description in references:
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        lines.append(f"- `{artifact_id}` ({kind}): {description}")
    if not seen:
        lines.append("No linked artifacts yet.")
    lines += ["", "### Safe restart rule", ""]
    lines.append(
        "Preserve the frozen contract. Reuse only recorded evidence and artifact IDs; treat numerical data, failed routes, and unverified candidates as non-decisive until their stated audit gate is satisfied."
    )
    lines.append("")
    return lines


def build_continuation_brief(store: ResearchStore) -> str:
    return "\n".join([
        "# Ariadne Continuation Brief",
        "",
        f"Generated: {utc_now()}",
        "",
        *_continuation_handoff_lines(store),
    ])


def write_continuation_brief(store: ResearchStore, output: Path | None = None) -> Path:
    if output is None:
        ensure_dir(store.paths.reports)
        output = store.paths.reports / "continuation_brief.md"
    ensure_dir(output.parent)
    output.write_text(build_continuation_brief(store), encoding="utf-8")
    return output


def publish_continuation_brief_as_literature(store: ResearchStore) -> Path | None:
    """Publish one terminal campaign's handoff as a stable local dossier source.

    The source is explicitly operational context, not a mathematical citation. It is
    published once per campaign to avoid writes on every TUI startup and is supplied
    only to literature-aware roles through the existing dossier policy.
    """
    campaign = store.latest_campaign()
    if campaign is None:
        return None
    campaign_id = str(campaign["campaign_id"])
    filename = f"continuation-{campaign_id.lower()}.md"
    destination = store.paths.literature / filename
    relative_path = str(destination.relative_to(store.paths.root))
    for source in store.list_literature_sources():
        if (
            str(source.get("source_kind", "")) == "campaign_continuation_handoff"
            and str(source.get("relative_path", "")) == relative_path
            and destination.exists()
        ):
            return destination
    ensure_dir(destination.parent)
    destination.write_text(build_continuation_brief(store), encoding="utf-8")
    source_id = store.add_literature_source(
        title=f"Continuation handoff for campaign {campaign_id}",
        citation=(
            "Ariadne project-local continuation brief; operational research context, "
            "not a mathematical authority."
        ),
        source_kind="campaign_continuation_handoff",
        exact_statement=(
            "Campaign-local handoff only. Route summaries, failed attempts, and "
            "candidate evidence must be rechecked against their referenced artifacts "
            "and do not constitute cited mathematical results."
        ),
        assumptions=[],
        locator=filename,
        relative_path=relative_path,
        audit_status="CONTEXT_ONLY",
    )
    store.events.append(
        "continuation_brief_published_as_literature",
        {
            "campaign_id": campaign_id,
            "source_id": source_id,
            "relative_path": relative_path,
        },
    )
    return destination


def build_report(store: ResearchStore) -> str:
    campaign = store.latest_campaign()
    root_id = store.get_meta("root_claim_id")
    root_claim = store.get_claim(root_id) if root_id else None
    routes = store.list_routes(campaign["campaign_id"] if campaign else None)
    claims = store.list_claims()
    failures = store.list_failures()
    interventions = store.list_interventions(
        campaign["campaign_id"] if campaign else None
    )
    evidence = store.list_evidence()
    audits = store.list_audits(target_type="claim", target_id=root_id) if root_id else []
    human_instructions = (
        store.list_human_instructions(
            str(campaign["campaign_id"]), active_only=False
        )
        if campaign
        else []
    )
    campaign_control = (
        store.get_campaign_control(str(campaign["campaign_id"]))
        if campaign
        else None
    )
    config_revisions = (
        store.list_campaign_config_revisions(str(campaign["campaign_id"]))
        if campaign
        else []
    )

    lines: list[str] = [
        "# Ariadne Mathematical Research Report",
        "",
        f"Generated: {utc_now()}",
        "",
        "## Problem status",
        "",
        f"**Verdict: {problem_verdict(store)}**",
        "",
    ]
    if root_claim:
        lines += [
            f"Root claim: `{root_claim['claim_id']}`",
            "",
            root_claim["statement"],
            "",
            f"Epistemic status: `{root_claim['status']}`",
            "",
        ]
    lines += [
        "> Campaign completion is not a proof. Numerical evidence, literature matches, and LLM audits remain distinct artifact types.",
        "",
    ]

    if campaign:
        lines += [
            "## Campaign",
            "",
            f"- ID: `{campaign['campaign_id']}`",
            f"- Mode: `{campaign['mode']}`",
            f"- Orchestration status: `{campaign['status']}`",
            f"- Epoch: {campaign['epoch']} / {campaign['max_epochs']}",
            f"- Calls: {campaign['calls_used']} / {campaign['max_calls']}",
            f"- Recorded cost: ${float(campaign['cost_used']):.4f} / ${float(campaign['max_cost_usd']):.4f}",
            "",
        ]

    lines += ["## Operational configuration revisions", ""]
    if not config_revisions:
        lines += ["No configuration snapshot recorded.", ""]
    else:
        for revision in config_revisions:
            changes = revision.get("changes", {})
            changed_keys = ", ".join(sorted(changes)) or "source-only change"
            lines += [
                f"- Revision {revision['revision_number']} (`{revision['revision_id']}`): "
                f"{changed_keys}",
                f"  - Effective configuration SHA-256: `{revision['effective_sha256']}`",
                f"  - Source configuration SHA-256: `{revision['source_sha256']}`",
                f"  - Reason: {revision['reason']}",
            ]
        lines.append("")

    lines += ["## Human steering", ""]
    if campaign_control and bool(campaign_control.get("pause_requested")):
        lines += [
            f"- Pause requested by `{campaign_control.get('requested_by', '')}`: "
            f"{campaign_control.get('reason', '')}",
            "",
        ]
    if not human_instructions:
        lines += ["No human instructions recorded.", ""]
    else:
        for item in human_instructions:
            route = f" route `{item['route_id']}`" if item.get("route_id") else " campaign-wide"
            lines += [
                f"- `{item['instruction_id']}` [{item['status']}; {item['audience']};{route}] "
                f"{item['instruction_text']}",
            ]
        lines.append("")

    lines += ["## Routes", ""]
    if not routes:
        lines += ["No routes recorded.", ""]
    for route in routes:
        lines += [
            f"### {route['route_id']} -- {route['title']}",
            "",
            f"- Status: `{route['status']}`",
            f"- Mode: `{route['mode']}`",
            f"- Method: {route['method_family']}",
            f"- Representation: {route['representation']}",
            f"- Key bridge: {route['key_lemma']}",
            f"- Decisive test: {route['decisive_test']}",
            f"- Offline owner: `{route['owner_slot']}`",
            "",
        ]
        obligation = route.get("novelty_obligation", {})
        if obligation:
            lines += [
                "Literature-sentinel novelty obligation:",
                "",
                "```json",
                json.dumps(obligation, ensure_ascii=False, indent=2),
                "```",
                "",
            ]

    lines += ["## Failure clusters", ""]
    if not failures:
        lines += ["No canonical failure clusters recorded.", ""]
    for failure in failures:
        lines += [
            f"### {failure['failure_id']} -- {failure['failure_class']}",
            "",
            f"- Attempts compressed into this cluster: {failure['attempts_count']}",
            f"- Signature: {failure['signature']}",
            f"- Logical scope: {failure['logical_scope']}",
            f"- Revival condition: {failure['revival_conditions'] or 'not specified'}",
            "",
        ]

    lines += ["## Literature interventions", ""]
    if not interventions:
        lines += ["No literature interventions recorded.", ""]
    for item in interventions:
        lines += [
            f"### {item['intervention_id']} -- {item['kind']}",
            "",
            f"- Route: `{item['route_id']}`",
            f"- Early stop proposed: `{bool(item['early_stop'])}`",
            f"- Status: `{item['status']}`",
            f"- Message: {item['message']}",
            f"- Sources: {', '.join(item['source_refs']) or 'none'}",
            "",
        ]
        if item.get("response"):
            lines += ["Offline response:", "", "```json", json.dumps(item["response"], indent=2), "```", ""]

    lines += ["## Evidence inventory", ""]
    if not evidence:
        lines += ["No evidence artifacts recorded.", ""]
    else:
        evidence_counts = Counter(str(item["evidence_type"]) for item in evidence)
        lines += [
            *[f"- `{kind}`: {count}" for kind, count in sorted(evidence_counts.items())],
            "",
        ]

    lines += ["## Proof candidates", ""]
    candidates = [
        item for item in evidence
        if str(item.get("evidence_type")) == "DEDUCTIVE_PROOF_CANDIDATE"
        and item.get("artifact_id")
    ]
    if not candidates:
        lines += ["No proof candidate artifacts recorded.", ""]
    else:
        lines += [
            "> The following are complete submitted natural-language candidates as recorded by agents. They are not verified proofs and do not establish the theorem.",
            "",
        ]
        for index, item in enumerate(candidates, start=1):
            artifact_id = str(item["artifact_id"])
            try:
                artifact = store.get_artifact(artifact_id)
                path = store.paths.root / str(artifact["relative_path"])
                proof = path.read_text(encoding="utf-8")
            except (KeyError, OSError, UnicodeError) as exc:
                proof = f"[Candidate artifact unavailable: {exc}]"
            lines += [
                f"### Candidate {index} -- `{artifact_id}`",
                "",
                f"- Evidence status: `{item.get('status', 'CANDIDATE')}`",
                f"- Logical force: {item.get('logical_force', '')}",
                f"- Scope: {item.get('scope', '')}",
                "",
                "```text",
                proof.rstrip(),
                "```",
                "",
            ]

    lines += ["## Counterexample audits", ""]
    counterexample_audits = [
        item
        for item in audits
        if str(item.get("audit_type"))
        in {"LOCAL_COUNTEREXAMPLE_AUDIT", "GLOBAL_COUNTEREXAMPLE_AUDIT"}
    ]
    if not counterexample_audits:
        lines += ["No independent counterexample audits recorded.", ""]
    else:
        lines += [
            "> An audit pass checks the submitted candidate only; it does not itself promote the claim to REFUTED.",
            "",
        ]
        for item in counterexample_audits:
            lines += [
                f"- `{item['audit_type']}` by `{item['auditor_profile']}`: "
                f"`{item['verdict']}` -- {item['minimal_obligation'] or 'no failed obligation reported'}",
            ]
        lines.append("")

    lines += _continuation_handoff_lines(store)
    status_counts = Counter(str(claim["status"]) for claim in claims)
    lines += [
        "## Claim inventory",
        "",
        *[f"- `{status}`: {count}" for status, count in sorted(status_counts.items())],
        "",
        "## Required next gate",
        "",
    ]
    verdict = problem_verdict(store)
    if verdict == CampaignStatus.CONTRACT_CHANGED:
        lines.append(
            "The frozen problem contract changed after the campaign began. Restore the exact sealed contract before any resumption, or create a new project for the revised statement."
        )
    elif verdict == "UNSOLVED":
        lines.append(
            "The problem remains unsolved. Continue only with a route that bypasses a recorded obstruction, supplies a novelty certificate, or performs an exact refutation test."
        )
    elif verdict in {"PROOF_CANDIDATE_UNVERIFIED", "PROOF_CANDIDATE_AGENT_AUDITED"}:
        lines.append(
            "A complete independent and human proof audit is required before the theorem can be treated as established or any Lean formalization may begin."
        )
    elif verdict == "HUMAN_CHECKED":
        lines.append(
            "The late formalization gate is open. Lean may now be used solely to encode and certify the human-checked proof."
        )
    elif verdict == "FORMALLY_CERTIFIED":
        lines.append(
            "The encoded statement has a recorded formal certification artifact; semantic correspondence with the intended theorem should remain part of publication review."
        )
    else:
        lines.append("Review the exact counterexample or certification artifact.")
    lines.append("")
    return "\n".join(lines)


def write_report(store: ResearchStore, output: Path | None = None) -> Path:
    if output is None:
        ensure_dir(store.paths.reports)
        output = store.paths.reports / "research_report.md"
    ensure_dir(output.parent)
    output.write_text(build_report(store), encoding="utf-8")
    return output
