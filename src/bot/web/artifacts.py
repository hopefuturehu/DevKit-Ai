"""Bounded, immutable file snapshots for actual workspace changes during a Run."""

from __future__ import annotations

import difflib
import hashlib
import json
import mimetypes
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bot.observability import Redactor

MAX_FILE_BYTES = 5_000_000
MAX_SNAPSHOT_BYTES = 100_000_000
MAX_FILES = 20_000
IGNORED = {
    ".git",
    ".bot",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".ssh",
    ".aws",
    ".codex",
    ".agents",
}


class ArtifactStore:
    def __init__(self, directory: Path, redactor: Redactor, denied: tuple[Path, ...] = ()):
        self.directory = directory.resolve()
        self.redactor = redactor
        self.denied = (*denied, self.directory)
        self.failures: dict[str, str] = {}
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs = self.directory / "contents"
        self.blobs.mkdir(exist_ok=True, mode=0o700)

    def _manifest_path(self, run_id: str) -> Path:
        return self.directory / (hashlib.sha256(run_id.encode()).hexdigest() + ".json")

    def _load(self, run_id: str) -> dict[str, Any]:
        path = self._manifest_path(run_id)
        if not path.is_file():
            return {"run_id": run_id, "before": None, "after": None, "warnings": []}
        return json.loads(path.read_text())

    def _save(self, run_id: str, manifest: dict[str, Any]) -> None:
        path = self._manifest_path(run_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def _allowed(self, root: Path, relative: Path) -> bool:
        if any(
            part in IGNORED
            or part == ".env"
            or part.startswith(".env.")
            or part in {"id_rsa", "id_ed25519"}
            for part in relative.parts
        ):
            return False
        if relative.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            return False
        candidate = root / relative
        return not any(candidate == denied or denied in candidate.parents for denied in self.denied)

    @staticmethod
    def _read(root: Path, relative: Path) -> bytes:
        # Walk using directory descriptors so replacing a parent with a symlink
        # between listing and reading cannot escape the selected workspace.
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
            file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            with os.fdopen(file_fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise ValueError("文件超过预览上限或不是普通文件")
                content = stream.read(MAX_FILE_BYTES + 1)
                if len(content) > MAX_FILE_BYTES:
                    raise ValueError("文件超过预览上限")
                return content
        finally:
            os.close(descriptor)

    def _snapshot(self, workspace: Path):
        root = workspace.resolve()
        entries = {}
        warnings = []
        total = 0
        visited = 0
        complete = True

        def directory_error(error):
            nonlocal complete
            complete = False
            warnings.append(f"目录读取失败: {error}")

        for current, directories, files in os.walk(
            root, followlinks=False, onerror=directory_error
        ):
            relative_dir = Path(current).relative_to(root)
            directories[:] = sorted(
                d
                for d in directories
                if self._allowed(root, relative_dir / d) and not (Path(current) / d).is_symlink()
            )
            for name in sorted(files):
                relative = relative_dir / name
                if not self._allowed(root, relative):
                    continue
                visited += 1
                if visited > MAX_FILES or total >= MAX_SNAPSHOT_BYTES:
                    warnings.append("工作区快照达到数量或容量上限，变化列表可能不完整")
                    return entries, warnings, False
                try:
                    raw = self._read(root, relative)
                except (OSError, ValueError) as exc:
                    warnings.append(f"{relative.as_posix()}: {exc}")
                    # Keep an explicit unavailable entry: do not report its
                    # disappearance or appearance as a deletion/new file.
                    entries[relative.as_posix()] = {"unavailable": True}
                    continue
                total += len(raw)
                original_hash = hashlib.sha256(raw).hexdigest()
                text = None
                if b"\0" not in raw:
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                redacted = False
                if text is not None:
                    safe_text = str(self.redactor.redact(text))
                    redacted = safe_text != text
                    raw = safe_text.encode()
                digest = hashlib.sha256(raw).hexdigest()
                blob = self.blobs / digest
                if not blob.exists():
                    blob.write_bytes(raw)
                    blob.chmod(0o600)
                entries[relative.as_posix()] = {
                    "sha256": original_hash,
                    "content_sha256": digest,
                    "size": len(raw),
                    "text": text is not None,
                    "redacted": redacted,
                    "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                }
        return entries, warnings, complete

    def capture(self, run_id: str, workspace: Path, *, initial: bool = False) -> None:
        manifest = self._load(run_id)
        snapshot, warnings, complete = self._snapshot(workspace)
        if initial and manifest["before"] is None:
            manifest["before"] = snapshot
            manifest["before_complete"] = complete
        manifest["after"] = snapshot
        manifest["after_complete"] = complete
        manifest["warnings"] = list(dict.fromkeys([*manifest["warnings"], *warnings]))[:200]
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        manifest["workspace"] = str(workspace)
        self._save(run_id, manifest)
        self.failures.pop(run_id, None)

    def failed(self, run_id: str) -> None:
        message = "最新文件快照读取失败，现有内容可能不是任务的最终状态"
        self.failures[run_id] = message
        try:
            manifest = self._load(run_id)
            manifest["warnings"] = list(dict.fromkeys([*manifest["warnings"], message]))
            self._save(run_id, manifest)
        except OSError:
            pass  # Still report the failure when storage itself is unavailable.

    def listing(self, run_id: str):
        manifest = self._load(run_id)
        before, after = manifest["before"], manifest["after"]
        result = {
            "run_id": run_id,
            "artifacts": [],
            "warnings": list(
                dict.fromkeys(
                    [
                        *manifest["warnings"],
                        *([self.failures[run_id]] if run_id in self.failures else []),
                    ]
                )
            ),
            "available": before is not None and after is not None,
            "updated_at": manifest.get("updated_at"),
            "scope": "workspace_changes_during_run",
        }
        if not result["available"]:
            return result
        for path in sorted(before.keys() | after.keys()):
            old, new = before.get(path), after.get(path)
            if old is None and not manifest.get("before_complete", True):
                continue
            if new is None and not manifest.get("after_complete", True):
                continue
            if (old or {}).get("unavailable") or (new or {}).get("unavailable"):
                continue
            if old and new and old["sha256"] == new["sha256"]:
                continue
            result["artifacts"].append(
                {
                    "id": hashlib.sha256(path.encode()).hexdigest(),
                    "path": path,
                    "change": "added" if old is None else "deleted" if new is None else "modified",
                    "before": old,
                    "after": new,
                }
            )
        return result

    def detail(self, run_id: str, artifact_id: str):
        record = next(
            (item for item in self.listing(run_id)["artifacts"] if item["id"] == artifact_id), None
        )
        if record is None:
            raise LookupError("文件产物不存在")
        old, new = record["before"], record["after"]
        diff = None
        if all(item is None or item["text"] for item in (old, new)):
            old_text = self._content(old).decode() if old else ""
            new_text = self._content(new).decode() if new else ""
            diff = "".join(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile="before/" + record["path"],
                    tofile="after/" + record["path"],
                )
            )
        return {
            **record,
            "diff": diff[:200000] if diff else diff,
            "diff_truncated": bool(diff and len(diff) > 200000),
        }

    def _content(self, record):
        digest = record["content_sha256"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise LookupError("文件内容引用无效")
        return (self.blobs / digest).read_bytes()

    def content(self, run_id: str, artifact_id: str, side: str = "after"):
        record = self.detail(run_id, artifact_id)
        item = record[side]
        if item is None:
            raise LookupError("该版本的文件不存在")
        return record, self._content(item)
