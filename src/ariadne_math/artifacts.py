from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import ProjectPaths
from .util import content_hash, ensure_dir, utc_now, write_json


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    path: Path
    kind: str
    media_type: str
    size: int
    metadata: dict[str, Any] = field(default_factory=dict)


class ArtifactStore:
    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        ensure_dir(self.paths.artifacts)

    def put_text(
        self,
        text: str,
        *,
        kind: str,
        suffix: str = ".md",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        return self.put_bytes(
            text.encode("utf-8"),
            kind=kind,
            suffix=suffix,
            media_type="text/markdown" if suffix == ".md" else "text/plain",
            metadata=metadata,
        )

    def put_bytes(
        self,
        data: bytes,
        *,
        kind: str,
        suffix: str = "",
        media_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        digest = content_hash(data)
        artifact_id = f"ART-{digest[:16]}"
        shard = ensure_dir(self.paths.artifacts / digest[:2])
        path = shard / f"{digest}{suffix}"
        if not path.exists():
            path.write_bytes(data)
        meta_path = shard / f"{digest}.meta.json"
        if not meta_path.exists():
            guessed = media_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            write_json(
                meta_path,
                {
                    "artifact_id": artifact_id,
                    "sha256": digest,
                    "kind": kind,
                    "media_type": guessed,
                    "size": len(data),
                    "created_at": utc_now(),
                    "metadata": metadata or {},
                    "relative_path": str(path.relative_to(self.paths.root)),
                },
            )
        guessed = media_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return ArtifactRecord(
            artifact_id=artifact_id,
            path=path,
            kind=kind,
            media_type=guessed,
            size=len(data),
            metadata=dict(metadata or {}),
        )
