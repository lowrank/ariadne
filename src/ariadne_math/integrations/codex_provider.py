from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from ariadne_math.util import redact_environment


ROLE_SCHEMAS = {
    "offline_researcher": "offline_researcher.json",
    "literature_researcher": "literature_researcher.json",
    "contract_author": "contract_author.json",
    "contract_resolver": "contract_resolver.json",
    "literature_author": "literature_author.json",
    "intervention_responder": "intervention_responder.json",
    "literature_sentinel": "literature_sentinel.json",
    "local_verifier": "verifier.json",
    "global_verifier": "verifier.json",
    "proof_expander": "proof_expander.json",
    "conceptual_pivot": "conceptual_pivot.json",
    "result_synthesizer": "result_synthesizer.json",
    "instruction_interpreter": "instruction_interpreter.json",
}

DEFAULT_MODEL = "gpt-5.6-sol"

DEFAULT_EFFORT = {
    "offline_researcher": "xhigh",
    "literature_researcher": "xhigh",
    "contract_author": "xhigh",
    "contract_resolver": "high",
    "literature_author": "high",
    "intervention_responder": "high",
    "literature_sentinel": "high",
    "local_verifier": "high",
    "global_verifier": "xhigh",
    "proof_expander": "high",
    "conceptual_pivot": "xhigh",
    "result_synthesizer": "high",
    "instruction_interpreter": "medium",
}

ALLOWED_EFFORT = {"low", "medium", "high", "xhigh", "max"}
ALLOWED_WEB = {"disabled", "cached", "indexed", "live"}
# Research roles may use an isolated scratch workspace for symbolic algebra,
# exact arithmetic, small reproducible checks, and source-file processing. The
# network policy remains independent: offline researchers still have web search
# forced off, while literature researchers follow the configured literature
# policy. Contract/sentinel/author roles remain non-programming roles.
PROGRAMMING_ROLES = {
    "offline_researcher",
    "literature_researcher",
    "local_verifier",
    "global_verifier",
}


class CodexProviderError(RuntimeError):
    pass


def _read_prompt() -> str:
    prompt = sys.stdin.read()
    if not prompt.strip():
        raise CodexProviderError("Ariadne supplied an empty prompt")
    return prompt


def _role() -> str:
    role = os.environ.get("ARIADNE_ROLE", "").strip()
    if role not in ROLE_SCHEMAS:
        supported = ", ".join(sorted(ROLE_SCHEMAS))
        raise CodexProviderError(
            f"Unsupported ARIADNE_ROLE={role!r}; supported roles: {supported}"
        )
    return role


def _web_mode(role: str) -> str:
    requested = os.environ.get("ARIADNE_CODEX_WEB_SEARCH", "disabled").strip().lower()
    if requested not in ALLOWED_WEB:
        raise CodexProviderError(
            "ARIADNE_CODEX_WEB_SEARCH must be one of: "
            + ", ".join(sorted(ALLOWED_WEB))
        )

    policy = os.environ.get("ARIADNE_NETWORK_POLICY", "inherit").strip().lower()
    if policy == "deny":
        return "disabled"

    # Only explicitly literature-aware roles may be granted web search. Offline
    # research, contract authoring, and verification remain web-disabled even if
    # a provider is misconfigured.
    if role not in {"literature_sentinel", "literature_researcher", "literature_author", "contract_resolver", "proof_expander"}:
        return "disabled"
    return requested


def _effort(role: str) -> str:
    value = os.environ.get(
        "ARIADNE_CODEX_REASONING_EFFORT", DEFAULT_EFFORT[role]
    ).strip().lower()
    if value not in ALLOWED_EFFORT:
        raise CodexProviderError(
            "ARIADNE_CODEX_REASONING_EFFORT must be one of: "
            + ", ".join(sorted(ALLOWED_EFFORT))
        )
    return value


def _timeout(role: str) -> int:
    default = "0" if role in {"offline_researcher", "literature_researcher", "proof_expander"} else "1500"
    raw = os.environ.get("ARIADNE_CODEX_TIMEOUT_SECONDS", default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise CodexProviderError(
            "ARIADNE_CODEX_TIMEOUT_SECONDS must be an integer"
        ) from exc
    if value < 0:
        raise CodexProviderError("ARIADNE_CODEX_TIMEOUT_SECONDS must be nonnegative (0 means unlimited)")
    return value


def _codex_binary() -> str:
    value = os.environ.get("ARIADNE_CODEX_BIN", "codex").strip()
    if not value:
        raise CodexProviderError("ARIADNE_CODEX_BIN cannot be empty")
    resolved = shutil.which(value)
    if resolved is None:
        raise CodexProviderError(
            f"Codex CLI executable {value!r} was not found on PATH. "
            "Install Codex and run `codex login` first."
        )
    return resolved


def _config_override(key: str, value: str) -> list[str]:
    return ["-c", f'{key}={json.dumps(value)}']


def _bool_override(key: str, value: bool) -> list[str]:
    return ["-c", f"{key}={'true' if value else 'false'}"]


def _build_command(
    *,
    binary: str,
    role: str,
    workspace: Path,
    schema_path: Path,
    instructions_path: Path,
    output_path: Path,
) -> list[str]:
    command = [
        binary,
        "--ask-for-approval",
        "never",
        "--strict-config",
        "exec",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "workspace-write" if role in PROGRAMMING_ROLES else "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--cd",
        str(workspace),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
    ]

    command += _config_override("web_search", _web_mode(role))
    command += _bool_override("agents.enabled", False)
    command += _bool_override("memories.generate_memories", False)
    command += _bool_override("hide_agent_reasoning", True)
    command += _bool_override("show_raw_agent_reasoning", False)
    command += _config_override("model_reasoning_summary", "none")
    command += _bool_override("allow_login_shell", False)
    programming_tools = role in PROGRAMMING_ROLES
    command += _bool_override("features.shell_tool", programming_tools)
    command += _bool_override("features.unified_exec", programming_tools)
    command += _bool_override("features.apps", False)
    command += _bool_override("features.skill_mcp_dependency_install", False)
    command += _bool_override("check_for_update_on_startup", False)
    command += _bool_override("feedback.enabled", False)
    command += _config_override("model_instructions_file", str(instructions_path))
    command += _config_override("model_reasoning_effort", _effort(role))

    model = os.environ.get("ARIADNE_CODEX_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    command.extend(["--model", model])

    # A literal '-' tells `codex exec` to read the task from stdin.
    command.append("-")
    return command


def _tool_activity(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    marker = "ARIADNE_TOOL_EVENT "
    for line in stdout.splitlines():
        position = line.find(marker)
        if position < 0:
            continue
        payload = line[position + len(marker):].strip()
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and str(event.get("kind", "")) in {
            "arxiv_download_started", "llamaparse_started",
        }:
            events.append(event)
    return events


def _parse_usage(stdout: str) -> dict[str, int]:
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            turn = event.get("turn")
            if isinstance(turn, dict):
                usage = turn.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens = max(
            input_tokens,
            _coerce_int(usage.get("input_tokens", usage.get("prompt_tokens", 0))),
        )
        input_details = usage.get("input_tokens_details", usage.get("prompt_tokens_details"))
        if isinstance(input_details, dict):
            cached_input_tokens = max(
                cached_input_tokens, _coerce_int(input_details.get("cached_tokens", 0))
            )
        else:
            cached_input_tokens = max(
                cached_input_tokens, _coerce_int(usage.get("cached_input_tokens", 0))
            )
        output_tokens = max(
            output_tokens,
            _coerce_int(
                usage.get("output_tokens", usage.get("completion_tokens", 0))
            ),
        )
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
    }


def _coerce_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _load_result(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CodexProviderError(
            "Codex exited successfully but did not write the structured final response"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexProviderError(
            f"Could not parse Codex structured response at {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise CodexProviderError("Codex structured response must be a JSON object")
    return data


def run() -> dict[str, Any]:
    prompt = _read_prompt()
    role = _role()
    binary = _codex_binary()
    timeout_seconds = _timeout(role)

    package_root = files("ariadne_math.integrations.codex")
    schema_resource = package_root.joinpath("schemas", ROLE_SCHEMAS[role])
    instructions_resource = package_root.joinpath("role_instructions.md")

    with (
        as_file(schema_resource) as schema_path,
        as_file(instructions_resource) as instructions_path,
        tempfile.TemporaryDirectory(prefix=f"ariadne-codex-{role}-") as temp_dir,
    ):
        workspace = Path(temp_dir) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        output_path = Path(temp_dir) / "final.json"
        command = _build_command(
            binary=binary,
            role=role,
            workspace=workspace,
            schema_path=schema_path,
            instructions_path=instructions_path,
            output_path=output_path,
        )

        codex_env = dict(os.environ)
        if os.environ.get("ARIADNE_NETWORK_POLICY", "inherit").strip().lower() == "deny":
            # Defense in depth. Ariadne's outer CommandProvider already removes
            # common API-key/token variables for deny roles. Preserve CODEX_HOME
            # so saved Codex authentication remains available.
            codex_env = redact_environment(codex_env)
        if role not in {"literature_researcher", "literature_author", "literature_sentinel", "proof_expander"}:
            # LlamaParse is an external service. Never expose its credential to
            # offline, verifier, authoring, or intervention roles, even if a
            # caller accidentally supplies an allow/inherit process policy.
            codex_env.pop("LLAMAPARSE_API_KEY", None)

        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout_seconds or None,
                check=False,
                env=codex_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexProviderError(
                f"Codex role {role!r} timed out after {timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise CodexProviderError(f"Could not launch Codex: {exc}") from exc

        if completed.returncode != 0:
            diagnostic = (completed.stderr or completed.stdout)[-4000:]
            raise CodexProviderError(
                f"Codex role {role!r} exited with {completed.returncode}:\n{diagnostic}"
            )

        result = _load_result(output_path)
        result["usage"] = _parse_usage(completed.stdout)
        activity = _tool_activity(completed.stdout)
        if activity:
            result["tool_activity"] = activity
        return result


def _check_installation() -> int:
    try:
        binary = _codex_binary()
    except CodexProviderError as exc:
        print(f"FAIL: {exc}")
        return 2

    print(f"Codex CLI: {binary}")
    try:
        version = subprocess.run(
            [binary, "--version"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: could not query Codex version: {exc}")
        return 2
    if version.returncode != 0:
        print(f"FAIL: `codex --version` exited with {version.returncode}")
        return 2
    print((version.stdout or version.stderr).strip())

    auth = subprocess.run(
        [binary, "login", "status"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if auth.returncode != 0:
        detail = (auth.stderr or auth.stdout).strip()
        print(f"FAIL: Codex is not logged in. {detail}")
        print("Run: codex login")
        return 2
    print((auth.stdout or auth.stderr).strip())

    package_root = files("ariadne_math.integrations.codex")
    for role, schema_name in sorted(ROLE_SCHEMAS.items()):
        resource = package_root.joinpath("schemas", schema_name)
        try:
            json.loads(resource.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"FAIL: invalid packaged schema for {role}: {exc}")
            return 2
    print("Ariadne Codex schemas: OK")
    print("Ariadne Codex provider: READY")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one bounded Ariadne role through the Codex CLI."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check Codex installation, authentication, and packaged schemas",
    )
    args = parser.parse_args(argv)
    if args.check:
        return _check_installation()

    try:
        result = run()
    except CodexProviderError as exc:
        print(f"ariadne-codex-provider: {exc}", file=sys.stderr)
        return 2
    print("<ARIADNE_JSON>")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print("</ARIADNE_JSON>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
