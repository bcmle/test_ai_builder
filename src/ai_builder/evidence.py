"""Immutable source snapshots and manifests; no untrusted path is accepted."""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


class ArtifactStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / digest
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix=".pending-", delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)  # Atomic publication without overwriting an existing object.
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except FileExistsError:
                self.get(digest)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return digest

    def get(self, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid artifact reference")
        data = (self.root / digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Artifact integrity check failed")
        return data

    def manifest(self, refs: list[str], metadata: dict) -> str:
        entries = [{"sha256": ref, "size": len(self.get(ref))} for ref in sorted(set(refs))]
        return self.put(json.dumps({"schema_version": "1.0", "entries": entries, "metadata": metadata},
                                   sort_keys=True, separators=(",", ":")).encode())

    def verify_manifest(self, ref):
        manifest = json.loads(self.get(ref))
        for entry in manifest["entries"]:
            if len(self.get(entry["sha256"])) != entry["size"]:
                raise ValueError("Manifest size mismatch")
        return manifest


def evidence_support(excerpt: str, snapshot: str) -> bool:
    """Necessary exact-text check, not a substitute for semantic applicability review."""
    return bool(excerpt.strip()) and " ".join(excerpt.split()) in " ".join(snapshot.split())

