"""Bounded ArXiv and LlamaParse tools for literature-aware roles."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LLAMAPARSE_ENDPOINT = "https://api.cloud.llamaindex.ai/api/v1/parsing"
LITERATURE_ROLES = {"literature_researcher", "literature_author", "literature_sentinel", "proof_expander"}


def _event(kind: str, message: str, **data: object) -> None:
    print("ARIADNE_TOOL_EVENT " + json.dumps({"kind": kind, "message": message, **data}), flush=True)


def _network_guard() -> None:
    if (os.environ.get("ARIADNE_ROLE", "") not in LITERATURE_ROLES or
            os.environ.get("ARIADNE_NETWORK_POLICY", "deny").lower() != "allow"):
        raise RuntimeError("network tools require a literature-aware role with network_policy=allow")


def _output(path: str) -> Path:
    target = Path(path).resolve()
    workspace = Path.cwd().resolve()
    if workspace not in target.parents:
        raise RuntimeError("output must be inside the ephemeral workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _request(request: Request, timeout: float = 60) -> bytes:
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"research service request failed: {exc}") from exc


def download_arxiv(url: str, output: str) -> None:
    _network_guard()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"arxiv.org", "export.arxiv.org"}:
        raise RuntimeError("only https://arxiv.org and https://export.arxiv.org are allowed")
    target = _output(output)
    _event("arxiv_download_started", "ArXiv source downloader called", source_url=url)
    data = _request(Request(url, headers={"User-Agent": "ariadne-research-tool/1"}))
    target.write_bytes(data)
    _event("arxiv_download_completed", "ArXiv source download completed", source_url=url, output=target.name, bytes=len(data))
    print(json.dumps({"source_url": url, "output": str(target), "bytes": len(data)}))


def _multipart(filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----ariadne-llamaparse"
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n')
    body = head.encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def parse_pdf(input_path: str, output: str, timeout: int) -> None:
    _network_guard()
    key = os.environ.get("LLAMAPARSE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("LLAMAPARSE_API_KEY is required")
    source = Path(input_path).resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise RuntimeError("parse-pdf requires an existing local PDF")
    target = _output(output)
    body, content_type = _multipart(source.name, source.read_bytes())
    _event("llamaparse_started", "LlamaParse called", source=source.name)
    upload = Request(LLAMAPARSE_ENDPOINT + "/upload", data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": content_type,
    })
    try:
        job = json.loads(_request(upload))
    except json.JSONDecodeError as exc:
        raise RuntimeError("LlamaParse upload returned invalid JSON") from exc
    job_id = job.get("id") or job.get("job_id")
    if not job_id:
        raise RuntimeError("LlamaParse upload did not return a job id")
    _event("llamaparse_job_submitted", "LlamaParse job submitted", job_id=str(job_id), source=source.name)
    deadline = time.monotonic() + max(1, timeout)
    while True:
        status_raw = _request(Request(LLAMAPARSE_ENDPOINT + f"/job/{job_id}", headers={"Authorization": f"Bearer {key}"}))
        status = json.loads(status_raw)
        state = str(status.get("status") or status.get("state") or "").upper()
        if state in {"SUCCESS", "COMPLETED", "COMPLETED_SUCCESS"}:
            break
        if state in {"ERROR", "FAILED", "CANCELED", "CANCELLED"}:
            raise RuntimeError(f"LlamaParse job failed with status {state}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"LlamaParse job timed out (job_id={job_id})")
        time.sleep(2)
    result = _request(Request(LLAMAPARSE_ENDPOINT + f"/job/{job_id}/result/markdown", headers={
        "Authorization": f"Bearer {key}", "Accept": "text/markdown",
    }))
    target.write_bytes(result)
    _event("llamaparse_completed", "LlamaParse Markdown conversion completed", job_id=str(job_id), source=source.name, output=target.name, bytes=len(result))
    print(json.dumps({"source": str(source), "job_id": job_id, "output": str(target), "bytes": len(result)}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded Ariadne literature tools")
    sub = parser.add_subparsers(dest="command", required=True)
    dl = sub.add_parser("download-arxiv")
    dl.add_argument("url")
    dl.add_argument("--output", required=True)
    pp = sub.add_parser("parse-pdf")
    pp.add_argument("input")
    pp.add_argument("--output", required=True)
    pp.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        if args.command == "download-arxiv":
            download_arxiv(args.url, args.output)
        else:
            parse_pdf(args.input, args.output, args.timeout)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ariadne-research-tool: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
