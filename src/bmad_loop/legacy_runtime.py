"""Verified legacy skill/script bytes for retained sprint and stories queues.

The bundle is immutable input. Project configuration is overlaid only in a
throwaway surface; neither the project nor a retained worker is rewritten.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .policy import Policy


class RuntimeError(ValueError):
    """The selected legacy runtime cannot be verified."""


def selected(project: Path, policy: Policy, source: str) -> Path | None:
    """Use the caller's persisted source on resume, never the current initiative."""
    location = policy.stories.legacy_runtime
    if source == "tickets":
        return None
    if not location:
        # A project can carry v7 scripts beside a still-compatible legacy skill.
        # Only a ticket-only primitive needs the separate preserved runtime.
        if source in {"sprint-status", "stories"}:
            for tree in (".agents/skills", ".claude/skills", ".opencode/skills"):
                step = project / tree / "bmad-build-auto/step-01-clarify-and-route.md"
                try:
                    text = step.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue
                except (OSError, UnicodeDecodeError) as exc:
                    raise RuntimeError(f"cannot inspect runtime capability {step}: {exc}") from exc
                if "method/scripts/tickets.py" in text and "folder+id dispatch" not in text:
                    raise RuntimeError(
                        "ticket-only skills require stories.legacy_runtime for sprint/stories; no v7 fallback"
                    )
        return None
    root = Path(location).expanduser()
    if not root.is_absolute():
        root = project / root
    verify(root)
    return root


def verify(root: Path) -> None:
    """Reject missing, altered, linked or unlisted bundle bytes."""
    try:
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError(f"legacy runtime is not a real directory: {root}")
        manifest_path = root / "manifest.json"
        if manifest_path.is_symlink():
            raise RuntimeError("legacy runtime manifest must not be a symlink")
        raw_manifest = manifest_path.read_bytes()
        if re.fullmatch(r"legacy-[0-9a-f]{16,64}", root.name):
            if (
                not hashlib.sha256(raw_manifest)
                .hexdigest()
                .startswith(root.name.removeprefix("legacy-"))
            ):
                raise RuntimeError(
                    "legacy runtime manifest does not match its content-addressed location"
                )
        manifest = json.loads(raw_manifest)
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, dict) or not files:
            raise RuntimeError("legacy runtime manifest needs a nonempty files map")
        found: set[str] = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise RuntimeError(f"legacy runtime contains a symlink: {path}")
            if path.is_file() and path != manifest_path:
                rel = path.relative_to(root).as_posix()
                if (
                    rel in {"_bmad/config.toml", "_bmad/config.user.toml"}
                    or rel.startswith(("_bmad/custom/", "_bmad/render/"))
                    or rel.endswith(".user.toml")
                ):
                    raise RuntimeError(
                        f"legacy runtime contains live configuration or generated renders: {rel}"
                    )
                found.add(rel)
        if found != set(files):
            raise RuntimeError("legacy runtime file set does not match manifest")
        for rel, digest in files.items():
            parts = PurePosixPath(rel)
            if parts.is_absolute() or ".." in parts.parts or "\\" in rel:
                raise RuntimeError(f"unsafe legacy runtime path: {rel!r}")
            if hashlib.sha256((root / rel).read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"legacy runtime hash mismatch: {rel}")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot verify legacy runtime {root}: {exc}") from exc


@contextmanager
def surface(project: Path, runtime: Path | None) -> Iterator[Path]:
    """Compose runtime bytes with authoritative live project configuration."""
    if runtime is None:
        yield project
        return
    verify(runtime)
    with tempfile.TemporaryDirectory(prefix="bmad-loop-legacy-") as tmp:
        root = Path(tmp)
        shutil.copytree(runtime, root, dirs_exist_ok=True)
        for rel in ("_bmad/config.toml", "_bmad/custom", "_bmad/config.user.toml"):
            src, dst = project / rel, root / rel
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        yield root
