from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import subprocess
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .activity import ActivityReporter, NullActivityReporter
from .agent import AgentRunner
from .artifacts import ArtifactStore
from .config import HarnessConfig, load_config
from .contracts import validate_contract
from .models import AgentCall
from .prompt_loader import render_prompt
from .successor import enable_project_git, project_has_git_repository
from .store import ResearchStore
from .util import content_hash, extract_json_object, write_json


@dataclass(frozen=True)
class SetupAnswers:
    title: str
    statement: str
    objective: str
    hypotheses_and_domains: str
    uniformity_and_endpoints: str
    exclusions_and_statement_drift: str
    proof_success: str
    refutation_success: str
    base_source_references: str
    source_files: tuple[str, ...]
    research_mode: str
    researcher_count: int
    parallel: bool
    allow_live_literature: bool
    literature_instructions: str
    literature_source_files: tuple[str, ...] = ()
    max_epochs: int = 3
    max_calls: int = 24
    max_cost_usd: float = 30.0
    use_git: bool = True


def _ask(prompt_text: str, default: str = "") -> str:
    # This helper may execute within prompt_toolkit.run_in_terminal; built-in
    # input avoids creating a nested Application/asyncio.run call.
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt_text}{suffix}: ").strip()
    return value or default


def _find_embedded_pdf(text: str) -> Path | None:
    for match in re.finditer(r"(?P<path>(?:\.\.?/|[^\s]+/)[^\s]+?\.pdf)(?=[\s.,;:!?)]|$)", text, re.IGNORECASE):
        candidate = Path(match.group("path").rstrip(".,;:!?)]}" )).expanduser()
        if candidate.is_file():
            return candidate
    return None


def collect_setup_answers(project_root: Path | None = None) -> SetupAnswers:
    """Collect the task document and only the operator-controlled settings.

    The bounded contract and literature agents infer the title, exact statement
    structure, hypotheses, and success criteria from the supplied task file.
    """
    print("\nAriadne interactive problem setup")
    print("Provide a task description or a path to a task file.")
    print("Bounded agents will structure and title the task.")
    print("You will choose the research mode and agent settings.\n")

    task_input = _ask("Task description or task file path")
    if not task_input:
        raise ValueError("A task description or task file path is required")
    candidate = Path(task_input).expanduser()
    embedded = candidate if candidate.is_file() else _find_embedded_pdf(task_input)
    if embedded is not None:
        task_text = task_input
        source_files = (str(embedded),)
        source_reference = f"Owner-supplied task file: {embedded.name}"
    else:
        task_text = task_input
        source_files = ()
        source_reference = "Owner-supplied task description"

    existing_git = project_root is not None and project_has_git_repository(project_root)
    if existing_git:
        print("Existing Git repository detected; Ariadne will record setup and epoch snapshots.")
        use_git = True
    else:
        use_git = _ask("Enable Git version control for this project? y/n", "y").lower() in {
            "y", "yes", "true", "1"
        }

    mode_value = _ask(
        "Research mode: 1=offline+sentinel, 2=offline only, 3=literature-guided",
        "1",
    ).lower()
    mode_map = {
        "1": "offline_sentinel",
        "offline_sentinel": "offline_sentinel",
        "2": "offline_only",
        "offline_only": "offline_only",
        "3": "literature_guided",
        "literature_guided": "literature_guided",
    }
    if mode_value not in mode_map:
        raise ValueError("Research mode must be 1, 2, 3, or a supported mode name")
    research_mode = mode_map[mode_value]

    try:
        researcher_count = int(_ask("Number of independent primary research agents", "2"))
    except ValueError as exc:
        raise ValueError("Number of research agents must be an integer") from exc
    if researcher_count < 1:
        raise ValueError("At least one primary research agent is required")
    parallel = _ask("Run primary researchers in parallel? y/n", "y").lower() in {
        "y", "yes", "true", "1"
    }
    allow_live = _ask(
        "Allow live web search for literature-aware roles? y/n", "y"
    ).lower() in {"y", "yes", "true", "1"}
    literature_instructions = _ask(
        "Optional literature instructions",
        "Use exact theorem statements, versions, locators, assumptions, and known obstructions",
    )
    try:
        max_epochs = int(_ask("Maximum campaign epochs", "3"))
        max_calls = int(_ask("Maximum provider calls", "24"))
        max_cost_usd = float(_ask("Maximum campaign cost in USD", "30.0"))
    except ValueError as exc:
        raise ValueError("Budget values must be numeric") from exc
    if max_epochs < 1 or max_calls < 1 or max_cost_usd < 0:
        raise ValueError("Budget must have positive epochs/calls and non-negative cost")
    return SetupAnswers(
        title="Agent-generated title",
        statement=task_text,
        objective="Infer the exact objective from the supplied task file without weakening it.",
        hypotheses_and_domains="Extract all domains, hypotheses, quantifiers, and forbidden assumptions from the supplied task file.",
        uniformity_and_endpoints="Extract all uniformity, endpoint, degeneracy, and limiting requirements from the supplied task file.",
        exclusions_and_statement_drift="Do not weaken or silently reinterpret the supplied task.",
        proof_success="Infer the exact proof criterion from the supplied task file.",
        refutation_success="Infer the exact refutation criterion from the supplied task file.",
        base_source_references=source_reference,
        source_files=source_files,
        research_mode=research_mode,
        researcher_count=researcher_count,
        parallel=parallel,
        allow_live_literature=allow_live,
        literature_instructions=literature_instructions,
        literature_source_files=(),
        max_epochs=max_epochs,
        max_calls=max_calls,
        max_cost_usd=max_cost_usd,
        use_git=use_git,
    )


def load_setup_answers(path: Path) -> SetupAnswers:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Setup answers file must contain a JSON object")
    raw["source_files"] = tuple(str(x) for x in raw.get("source_files", []))
    raw["literature_source_files"] = tuple(
        str(x) for x in raw.get("literature_source_files", [])
    )
    return SetupAnswers(**raw)



_PDF_URL_PATTERN = re.compile(r"https://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
_MAX_OPEN_PDF_BYTES = 50 * 1024 * 1024


def _open_pdf_urls(value: object) -> list[str]:
    """Return safe candidate PDF URLs from a cited locator or owner text."""
    urls: list[str] = []
    for match in _PDF_URL_PATTERN.findall(str(value)):
        raw = match.rstrip(".,;:!?")
        parsed = urlparse(raw)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        host = parsed.hostname.casefold()
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
            continue
        path = parsed.path
        if host in {"arxiv.org", "export.arxiv.org"}:
            if path.startswith("/abs/"):
                raw = f"https://arxiv.org/pdf/{path.removeprefix('/abs/')}.pdf"
            elif path.startswith("/pdf/") and not path.endswith(".pdf"):
                raw = f"https://arxiv.org{path}.pdf"
        elif not path.casefold().endswith(".pdf"):
            # Do not scrape arbitrary article pages: an accessible direct PDF
            # locator is needed to make a reproducible local source cache.
            continue
        if raw not in urls:
            urls.append(raw)
    return urls


def _download_open_pdf(url: str) -> bytes:
    """Fetch a bounded public PDF; HTML/paywall responses are deliberately rejected."""
    request = Request(url, headers={"User-Agent": "ariadne-literature-cache/1"})
    with urlopen(request, timeout=60) as response:
        data = response.read(_MAX_OPEN_PDF_BYTES + 1)
        content_type = str(response.headers.get("Content-Type", "")).casefold()
    if len(data) > _MAX_OPEN_PDF_BYTES:
        raise ValueError("PDF exceeds the 50 MiB literature-cache limit")
    if not data.startswith(b"%PDF") and "application/pdf" not in content_type:
        raise ValueError("source did not return an openly accessible PDF")
    return data


def _cache_open_literature_pdf(
    store: ResearchStore,
    *,
    url: str,
    citation: str,
    reporter: ActivityReporter,
) -> str | None:
    """Download/convert one cited public PDF once and register its reusable Markdown."""
    cache_dir = store.paths.literature / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = content_hash(url.encode("utf-8"))
    pdf_path = cache_dir / f"{key}.pdf"
    markdown_path = cache_dir / f"{key}.md"
    try:
        if not pdf_path.exists():
            reporter.emit("literature_pdf_download_started", "Downloading an openly accessible cited PDF.", source_url=url)
            pdf_path.write_bytes(_download_open_pdf(url))
            reporter.emit("literature_pdf_downloaded", "Cached cited PDF locally.", source_url=url, path=str(pdf_path.relative_to(store.paths.root)))
        else:
            reporter.emit("literature_pdf_cache_hit", "Reusing cached cited PDF.", source_url=url, path=str(pdf_path.relative_to(store.paths.root)))
        if not markdown_path.exists():
            markdown = _read_source_excerpt(pdf_path, 2_000_000)
            if markdown.startswith("[PDF "):
                raise ValueError("PDF conversion failed with all configured backends")
            markdown_path.write_text(
                f"# Cached source\n\nSource URL: {url}\n\n{markdown.rstrip()}\n",
                encoding="utf-8",
            )
            reporter.emit("literature_pdf_converted", "Converted cached PDF to reusable Markdown.", source_url=url, path=str(markdown_path.relative_to(store.paths.root)))
        source_id = store.add_literature_source(
            title=Path(urlparse(url).path).name or "Cached open PDF",
            citation=citation or url,
            source_kind="cached_open_pdf_markdown",
            exact_statement="Cached public PDF; theorem statements and applicability remain unreviewed until audited.",
            assumptions=[],
            locator=url,
            relative_path=str(markdown_path.relative_to(store.paths.root)),
            audit_status="UNREVIEWED",
        )
        return source_id
    except (OSError, ValueError) as exc:
        reporter.emit("literature_pdf_cache_failed", f"Could not cache cited PDF: {exc}", source_url=url)
        return None


def _cache_cited_open_pdfs(
    store: ResearchStore,
    *,
    values: list[tuple[object, str]],
    reporter: ActivityReporter,
) -> list[str]:
    source_ids: list[str] = []
    seen: set[str] = set()
    for value, citation in values:
        for url in _open_pdf_urls(value):
            if url in seen:
                continue
            seen.add(url)
            source_id = _cache_open_literature_pdf(
                store, url=url, citation=citation, reporter=reporter
            )
            if source_id:
                source_ids.append(source_id)
    return source_ids


def _read_pdf_with_llamaparse(path: Path, limit: int) -> str | None:
    key = os.environ.get("LLAMAPARSE_API_KEY", "").strip()
    if not key:
        return None
    endpoint = "https://api.cloud.llamaindex.ai/api/v1/parsing"
    boundary = "----ariadne-setup-llamaparse"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{path.name}"\r\nContent-Type: application/pdf\r\n\r\n'
    ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    try:
        with urlopen(Request(endpoint + "/upload", data=body, method="POST", headers=headers), timeout=60) as response:
            job = json.loads(response.read())
        job_id = job.get("id") or job.get("job_id")
        if not job_id:
            return None
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            with urlopen(Request(endpoint + f"/job/{job_id}", headers={"Authorization": f"Bearer {key}"}), timeout=30) as response:
                status = json.loads(response.read())
            state = str(status.get("status") or status.get("state") or "").upper()
            if state in {"SUCCESS", "COMPLETED", "COMPLETED_SUCCESS"}:
                break
            if state in {"ERROR", "FAILED", "CANCELED", "CANCELLED"}:
                return None
            time.sleep(2)
        else:
            return None
        with urlopen(Request(endpoint + f"/job/{job_id}/result/markdown", headers={"Authorization": f"Bearer {key}"}), timeout=60) as response:
            text = response.read().decode("utf-8", errors="replace")
        return text[:limit] if text.strip() else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _read_pdf_with_mineru(path: Path, limit: int) -> str | None:
    configured = os.environ.get("ARIADNE_MINERU_BIN", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.append(Path(__file__).resolve().parents[2] / ".venv-mineru" / "bin" / "mineru")
    executable = next((str(item) for item in candidates if item.is_file()), None)
    executable = executable or shutil.which("mineru")
    if executable is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="ariadne-mineru-") as output_dir:
            completed = subprocess.run(
                [executable, "-p", str(path), "-o", output_dir],
                capture_output=True, text=True, timeout=600, check=False,
            )
            if completed.returncode != 0:
                return None
            markdown_files = sorted(Path(output_dir).rglob("*.md"))
            if not markdown_files:
                return None
            text = "\n\n".join(item.read_text(encoding="utf-8", errors="replace") for item in markdown_files)
            return text[:limit] if text.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _read_source_excerpt(path: Path, limit: int) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt", ".tex", ".json", ".yaml", ".yml", ".py"}:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    if suffix == ".pdf":
        requested = [item.strip().lower() for item in os.environ.get(
            "ARIADNE_PDF_BACKENDS", "llamaparse,mineru,pypdf,pdftotext"
        ).split(",") if item.strip()]
        for backend in requested:
            if backend == "llamaparse":
                extracted = _read_pdf_with_llamaparse(path, limit)
                if extracted:
                    return "[LlamaParse extraction]\n" + extracted
            elif backend == "mineru":
                extracted = _read_pdf_with_mineru(path, limit)
                if extracted:
                    return "[MinerU extraction]\n" + extracted
            elif backend == "pypdf":
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(str(path))
                    chunks: list[str] = []
                    size = 0
                    for page_index, page in enumerate(reader.pages, start=1):
                        text = page.extract_text() or ""
                        block = f"\n--- page {page_index} ---\n{text}"
                        chunks.append(block)
                        size += len(block)
                        if size >= limit:
                            break
                    extracted = "".join(chunks)[:limit]
                    if extracted.strip():
                        return "[pypdf extraction]\n" + extracted
                except (ImportError, OSError, ValueError):
                    pass
            elif backend == "pdftotext":
                try:
                    completed = subprocess.run(
                        ["pdftotext", "-layout", str(path), "-"],
                        capture_output=True, text=True, timeout=120, check=False,
                    )
                    if completed.returncode == 0 and completed.stdout.strip():
                        return "[pdftotext extraction]\n" + completed.stdout[:limit]
                except (OSError, subprocess.SubprocessError):
                    pass
        return f"[PDF {path.name} could not be text-extracted; configure ARIADNE_PDF_BACKENDS or LLAMAPARSE_API_KEY]"
    return f"[Unsupported local source format: {path}]"


def collect_source_excerpts(paths: tuple[str, ...], *, total_limit: int = 80000) -> str:
    chunks: list[str] = []
    remaining = total_limit
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists() or not path.is_file():
            chunks.append(f"\n## Missing local source\n{raw}\n")
            continue
        excerpt = _read_source_excerpt(path, min(remaining, 40000))
        # The bounded worker needs the mathematical content, not an absolute host
        # path. Keeping only the basename also reduces accidental environment
        # disclosure in persisted prompts.
        chunks.append(f"\n## Local source: {path.name}\n{excerpt}\n")
        remaining -= len(excerpt)
        if remaining <= 0:
            break
    return "".join(chunks) or "No local source excerpts were supplied."


def _replace_toml_table(text: str, table: str, body: str) -> str:
    pattern = re.compile(
        rf"(?ms)^\[{re.escape(table)}\]\n.*?(?=^\[[^\n]+\]\n|\Z)"
    )
    replacement = f"[{table}]\n{body.rstrip()}\n\n"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    return text.rstrip() + "\n\n" + replacement


def _set_toml_string_in_table(
    text: str, *, table: str, key: str, value: str
) -> str:
    """Set one string key without overwriting unrelated provider environment.

    Ariadne ships with a shared ``codex_literature`` provider, but custom
    configurations may route the literature author, researcher, and sentinel to
    different providers. This helper updates each actual provider table rather
    than assuming a hard-coded name.
    """

    table_pattern = re.compile(
        rf"(?ms)^\[{re.escape(table)}\]\n(?P<body>.*?)(?=^\[[^\n]+\]\n|\Z)"
    )
    match = table_pattern.search(text)
    encoded = json.dumps(value)
    if not match:
        return text.rstrip() + f"\n\n[{table}]\n{key} = {encoded}\n"
    body = match.group("body")
    key_pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=\s*.*$")
    if key_pattern.search(body):
        body = key_pattern.sub(f"{key} = {encoded}", body, count=1)
    else:
        body = body.rstrip() + f"\n{key} = {encoded}\n"
    start, end = match.span("body")
    return text[:start] + body + text[end:]


def update_runtime_config(path: Path, answers: SetupAnswers) -> None:
    existing = load_config(path)
    text = path.read_text(encoding="utf-8")
    offline_agents = (
        answers.researcher_count
        if answers.research_mode != "literature_guided"
        else 0
    )
    research_agents = (
        answers.researcher_count
        if answers.research_mode == "literature_guided"
        else 0
    )
    sentinel = answers.research_mode == "offline_sentinel"
    budget_body = "\n".join(
        [
            f"max_epochs = {answers.max_epochs}",
            f"max_calls = {answers.max_calls}",
            f"max_cost_usd = {answers.max_cost_usd}",
            "stagnation_epochs = 2",
            "duplicate_failure_limit = 2",
        ]
    )
    text = _replace_toml_table(text, "budget", budget_body)

    mode_body = "\n".join(
        [
            f'name = "{answers.research_mode}"',
            f"offline_agents = {offline_agents}",
            f"research_agents = {research_agents}",
            f"parallel = {'true' if answers.parallel else 'false'}",
            f"literature_intervention = {'true' if sentinel else 'false'}",
            f"require_route_difference_certificate = {'true' if sentinel else 'false'}",
            "novelty_deadline_epochs = 1",
            "allow_experiments = false",
            "route_similarity_threshold = 0.82",
        ]
    )
    text = _replace_toml_table(text, "mode", mode_body)

    # Apply the requested policy to every provider actually used by a
    # literature-aware role. Packaged configs share one provider; custom configs
    # do not have to.
    value = "live" if answers.allow_live_literature else "disabled"
    literature_providers = {
        existing.roles[role].provider
        for role in (
            "literature_author",
            "literature_researcher",
            "literature_sentinel",
        )
        if role in existing.roles
    }
    for provider_name in sorted(literature_providers):
        text = _set_toml_string_in_table(
            text,
            table=f"providers.{provider_name}.env",
            key="ARIADNE_CODEX_WEB_SEARCH",
            value=value,
        )
    path.write_text(text, encoding="utf-8")


def _preserve_setup_sources(
    store: ResearchStore,
    paths: tuple[str, ...],
    *,
    source_kind: str,
) -> list[str]:
    """Copy owner-supplied source bytes into the durable project dossier."""

    source_dir = store.paths.literature / "source-materials"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_ids: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists() or not path.is_file():
            store.events.append(
                "setup_source_missing", {"path": raw, "source_kind": source_kind}
            )
            continue
        digest = content_hash(path.read_bytes())
        destination = source_dir / f"{digest[:12]}-{path.name}"
        if not destination.exists():
            shutil.copy2(path, destination)
        reusable_path = destination
        if destination.suffix.lower() == ".pdf":
            markdown_path = destination.with_suffix(".md")
            if not markdown_path.exists():
                markdown = _read_source_excerpt(destination, 2_000_000)
                if not markdown.startswith("[PDF "):
                    markdown_path.write_text(
                        f"# Owner-supplied PDF source\n\nOriginal file: {destination.name}\n\n{markdown.rstrip()}\n",
                        encoding="utf-8",
                    )
                    store.events.append(
                        "setup_pdf_cached_markdown",
                        {"source": destination.name, "path": str(markdown_path.relative_to(store.paths.root))},
                    )
            if markdown_path.exists():
                reusable_path = markdown_path
        source_id = store.add_literature_source(
            title=path.name,
            citation=f"Owner-supplied local file: {path.name}",
            source_kind=source_kind,
            exact_statement=(
                "Owner-supplied source material. Exact mathematical claims must be "
                "extracted and audited before use."
            ),
            assumptions=[],
            locator=path.name,
            relative_path=str(reusable_path.relative_to(store.paths.root)),
            audit_status="UNREVIEWED",
        )
        source_ids.append(source_id)
    return source_ids


def generate_setup(
    *,
    project_root: Path,
    config_path: Path,
    answers: SetupAnswers,
    reporter: ActivityReporter | None = None,
) -> dict[str, Any]:
    store = ResearchStore(project_root)
    if store.latest_campaign() is not None:
        raise ValueError(
            "Interactive setup cannot replace the problem contract after a campaign "
            "has been created. Initialize a new project directory for a revised theorem."
        )
    config = load_config(config_path)
    required_roles = ("contract_author",) if answers.research_mode == "offline_only" else ("contract_author", "literature_author")
    missing_roles = {role for role in required_roles if role not in config.roles}
    if missing_roles:
        raise ValueError(
            "Setup requires configured roles: " + ", ".join(sorted(missing_roles))
        )
    reporter = reporter or NullActivityReporter()
    runner = AgentRunner(store, config, reporter=reporter)
    artifacts = ArtifactStore(store.paths)
    setup_id = f"SETUP-{content_hash(str(store.paths.root).encode('utf-8'))[:10]}"
    reporter.start(campaign_id=setup_id)
    try:
        reporter.emit(
            "setup_started",
            "Preparing a route-neutral contract with one bounded agent, then a separate literature document with another agent.",
            mode=answers.research_mode,
            researcher_count=answers.researcher_count,
        )
        base_source_excerpts = collect_source_excerpts(answers.source_files)
        literature_source_excerpts = collect_source_excerpts(
            answers.source_files + answers.literature_source_files
        )
        owner_cached_source_ids: list[str] = []
        if answers.allow_live_literature:
            owner_cached_source_ids = _cache_cited_open_pdfs(
                store,
                values=[
                    (answers.statement, answers.base_source_references),
                    (answers.base_source_references, answers.base_source_references),
                    (answers.literature_instructions, answers.base_source_references),
                ],
                reporter=reporter,
            )
            if owner_cached_source_ids:
                cached_paths = [
                    str(store.paths.root / source["relative_path"])
                    for source in store.list_literature_sources()
                    if source["source_id"] in owner_cached_source_ids
                ]
                literature_source_excerpts += collect_source_excerpts(tuple(cached_paths))
        for raw in answers.source_files:
            source_path = Path(raw).expanduser()
            if source_path.suffix.lower() != ".pdf":
                continue
            source_block = base_source_excerpts.split(
                f"## Local source: {source_path.name}", 1
            )[-1]
            backend = next(
                (name for name in ("LlamaParse", "MinerU", "pypdf", "pdftotext")
                 if f"[{name} extraction]" in source_block),
                None,
            )
            reporter.emit(
                "setup_pdf_parsed" if backend else "setup_pdf_parse_failed",
                f"PDF parsed with {backend}: {source_path.name}" if backend
                else f"PDF extraction produced no text: {source_path.name}",
                source=source_path.name,
                backend=backend or "none",
            )

        base_source_ids = _preserve_setup_sources(
            store, answers.source_files, source_kind="setup_base_material"
        )
        literature_only_ids = _preserve_setup_sources(
            store,
            answers.literature_source_files,
            source_kind="setup_literature_material",
        )

        answers_artifact = artifacts.put_text(
            json.dumps(asdict(answers), ensure_ascii=False, indent=2),
            kind="setup_interview",
            suffix=".json",
        )
        store.record_artifact(answers_artifact)

        reporter.emit(
            "contract_author_stage",
            "Launching the web-disabled contract-author role on the owner interview and base materials.",
        )
        contract_prompt = render_prompt(
            "contract_author.md",
            setup_answers=json.dumps(asdict(answers), ensure_ascii=False, indent=2),
            source_excerpts=base_source_excerpts,
        )
        contract_response = runner.call(
            AgentCall(
                role="contract_author",
                slot="contract-author",
                prompt=contract_prompt,
                project_root=store.paths.root,
                network_policy=config.roles["contract_author"].network_policy,
                metadata={
                    "task_summary": "Create and self-audit the exact mathematical problem contract from the owner interview"
                },
            )
        )
        contract_payload = extract_json_object(contract_response.text)
        contract = contract_payload.get("problem_contract")
        if not isinstance(contract, dict):
            raise ValueError("Contract author did not return a problem_contract object")
        # Accept a legacy wire name if an older provider returns one.
        if "problem_definitions" in contract and "definitions" not in contract:
            contract["definitions"] = contract.pop("problem_definitions")
        if "contract_domains_json" in contract and "domains" not in contract:
            raw_domains = contract.pop("contract_domains_json")
            try:
                parsed_domains = json.loads(raw_domains)
            except (TypeError, json.JSONDecodeError):
                parsed_domains = {}
            contract["domains"] = parsed_domains if isinstance(parsed_domains, dict) else {}
        contract.setdefault("tags", [])
        if not isinstance(contract["tags"], list):
            contract["tags"] = []
        contract["tags"] = [str(tag) for tag in contract["tags"] if str(tag).strip()]
        contract["research_mode"] = answers.research_mode
        validate_contract(contract)
        write_json(store.paths.contract, contract)
        store.set_meta(
            "problem_contract_sha256", content_hash(store.paths.contract.read_bytes())
        )
        store.set_meta("title", str(contract.get("title", answers.title)))
        store.events.append(
            "problem_contract_generated",
            {
                "sha256": content_hash(store.paths.contract.read_bytes()),
                "mode": answers.research_mode,
                "validation_notes": contract_payload.get("validation_notes", []),
                "base_source_ids": base_source_ids,
            },
        )

        # Rewrite and validate the operational mode before launching the separate
        # literature agent, so its web-search policy and role routing are already
        # in effect for this call.
        update_runtime_config(config_path, answers)
        config = load_config(config_path)
        runner = AgentRunner(store, config, reporter=reporter)

        if answers.research_mode == "offline_only":
            reporter.emit(
                "literature_author_skipped",
                "Offline-only mode: creating a parked literature placeholder without invoking literature_author.",
            )
            literature_payload = {
                "document_type": "parked_literature_dossier",
                "markdown": (
                    "# Parked literature dossier\n\n"
                    "This dossier was intentionally left unpopulated because the project is configured as `offline_only`.\n\n"
                    "A human may add and audit literature later; offline researchers do not receive literature material.\n"
                ),
                "warnings": [
                    "Literature author skipped by offline_only policy; human literature review remains optional."
                ],
            }
        else:
            reporter.emit(
                "literature_author_stage",
                "Launching the separate literature-author role after the problem contract has been frozen.",
            )
            literature_prompt = render_prompt(
                "literature_author.md",
                problem_contract=json.dumps(contract, ensure_ascii=False, indent=2),
                source_request=json.dumps(
                    {
                        "base_source_references": answers.base_source_references,
                        "literature_instructions": answers.literature_instructions,
                        "research_mode": answers.research_mode,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_excerpts=literature_source_excerpts,
            )
            literature_response = runner.call(
                AgentCall(
                    role="literature_author",
                    slot="literature-author",
                    prompt=literature_prompt,
                    project_root=store.paths.root,
                    network_policy=config.roles["literature_author"].network_policy,
                    metadata={
                        "task_summary": {
                            "offline_sentinel": "Create the hidden sentinel dossier",
                            "literature_guided": "Create the shared literature dossier",
                        }[answers.research_mode]
                    },
                )
            )
            literature_payload = extract_json_object(literature_response.text)
        markdown = str(literature_payload.get("markdown", "")).strip()
        if not markdown:
            raise ValueError("Literature author returned an empty markdown document")
        expected_document_type = {
            "offline_sentinel": "literature_sentinel",
            "offline_only": "parked_literature_dossier",
            "literature_guided": "shared_literature_dossier",
        }[answers.research_mode]
        actual_document_type = str(literature_payload.get("document_type", ""))
        if actual_document_type != expected_document_type:
            raise ValueError(
                "Literature author returned document_type="
                f"{actual_document_type!r}; expected {expected_document_type!r} "
                f"for mode {answers.research_mode!r}"
            )
        filename = {
            "offline_sentinel": "literature_sentinel_note.md",
            "offline_only": "parked_literature_dossier.md",
            "literature_guided": "shared_literature_dossier.md",
        }[answers.research_mode]
        dossier_path = store.paths.literature / filename
        dossier_path.write_text(markdown + "\n", encoding="utf-8")
        source_id = store.add_literature_source(
            title={
                "offline_sentinel": "Hidden literature sentinel dossier",
                "offline_only": "Parked literature dossier",
                "literature_guided": "Shared literature-guided dossier",
            }[answers.research_mode],
            citation=answers.base_source_references,
            source_kind=actual_document_type,
            exact_statement="Generated theorem-level literature map; inspect the dossier for exact statements and warnings.",
            assumptions=[],
            locator=filename,
            relative_path=str(dossier_path.relative_to(store.paths.root)),
        )
        cited_cached_source_ids: list[str] = []
        if answers.allow_live_literature:
            cited_values: list[tuple[object, str]] = []
            for source in literature_payload.get("sources", []):
                if isinstance(source, dict):
                    citation = str(source.get("citation", ""))
                    cited_values.extend([
                        (source.get("locator", ""), citation),
                        (source.get("citation", ""), citation),
                    ])
            cited_cached_source_ids = _cache_cited_open_pdfs(
                store, values=cited_values, reporter=reporter
            )
        store.events.append(
            "literature_dossier_generated",
            {
                "source_id": source_id,
                "document_type": actual_document_type,
                "warnings": literature_payload.get("warnings", []),
                "literature_only_source_ids": literature_only_ids,
                "owner_cached_source_ids": owner_cached_source_ids,
                "cited_cached_source_ids": cited_cached_source_ids,
            },
        )

        git_enabled = False
        git_commit = ""
        if answers.use_git:
            git_enabled, git_commit = enable_project_git(store.paths.root)
            if git_enabled:
                reporter.emit(
                    "git_versioning_enabled",
                    "Initialized local Git versioning for this Ariadne project.",
                    commit=git_commit,
                )
            else:
                reporter.emit(
                    "git_versioning_unavailable",
                    "Git versioning was requested but Git is unavailable; continuing with Ariadne lineage records only.",
                )

        reporter.emit(
            "setup_finished",
            f"Created the frozen contract and {expected_document_type} for mode {answers.research_mode}.",
        )
        return {
            "contract": str(store.paths.contract),
            "literature_document": str(dossier_path),
            "config": str(config_path),
            "mode": answers.research_mode,
            "researcher_count": answers.researcher_count,
            "contract_validation_notes": contract_payload.get("validation_notes", []),
            "literature_warnings": literature_payload.get("warnings", []),
            "preserved_base_source_ids": base_source_ids,
            "preserved_literature_source_ids": literature_only_ids,
            "owner_cached_source_ids": owner_cached_source_ids,
            "cited_cached_source_ids": cited_cached_source_ids,
            "git_versioning_enabled": git_enabled,
            "git_commit": git_commit or None,
        }
    finally:
        reporter.stop()
