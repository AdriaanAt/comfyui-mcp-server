#!/usr/bin/env python3
"""Inventory one or more Ollama model stores and report what a merge would cost.

Read-only. Nothing is moved, written or deleted.

Ollama stores every layer as a content-addressed blob named by its sha256
digest, so identical layers in two stores are byte-identical by definition.
That makes the overlap between two stores exactly computable rather than
guessed, which is the number that decides whether merging is cheap or
expensive.

    python scripts/ollama_survey.py "G:/Ai Model Container/.ollama" "C:/Users/Adriaan/.ollama"
    python scripts/ollama_survey.py --json ... > inventory.json

With no paths given it checks OLLAMA_MODELS and the platform default.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def default_store() -> Path:
    """Where Ollama will actually look, following its own resolution order."""
    if env := os.getenv("OLLAMA_MODELS"):
        return Path(env)
    return Path.home() / ".ollama" / "models"


def normalise(root: Path) -> Path:
    """Accept either the .ollama directory or the models directory inside it."""
    if (root / "models" / "manifests").is_dir():
        return root / "models"
    return root


def read_manifests(models_dir: Path) -> list[dict[str, Any]]:
    """Every manifest under manifests/, with its layer digests and sizes.

    The tree is walked generically rather than assuming
    registry.ollama.ai/library, because models pulled from other registries
    (Hugging Face, private ones) sit under their own host path.
    """
    manifests_root = models_dir / "manifests"
    if not manifests_root.is_dir():
        return []

    found: list[dict[str, Any]] = []
    for path in sorted(manifests_root.rglob("*")):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        if "layers" not in data:
            continue

        # Path under manifests/ is host/namespace/model/tag.
        parts = path.relative_to(manifests_root).parts
        name = "/".join(parts[:-1]) + ":" + parts[-1] if len(parts) > 1 else path.name
        # Trim the default registry prefix so names read the way ollama prints them.
        for prefix in ("registry.ollama.ai/library/", "registry.ollama.ai/"):
            if name.startswith(prefix):
                name = name[len(prefix):]
                break

        digests: dict[str, int] = {}
        for layer in data.get("layers", []):
            if digest := layer.get("digest"):
                digests[digest] = int(layer.get("size") or 0)
        config = data.get("config") or {}
        if digest := config.get("digest"):
            digests[digest] = int(config.get("size") or 0)

        found.append(
            {
                "name": name,
                "manifest_path": str(path),
                "digests": digests,
                "size": sum(digests.values()),
            }
        )
    return found


def blob_path(models_dir: Path, digest: str) -> Path:
    """Blobs are stored with the digest's colon replaced by a dash."""
    return models_dir / "blobs" / digest.replace(":", "-")


def survey(root: Path) -> dict[str, Any]:
    models_dir = normalise(root)
    manifests = read_manifests(models_dir)

    blob_sizes: dict[str, int] = {}
    missing: list[str] = []
    for entry in manifests:
        for digest in entry["digests"]:
            if digest in blob_sizes:
                continue
            blob = blob_path(models_dir, digest)
            if blob.is_file():
                blob_sizes[digest] = blob.stat().st_size
            else:
                missing.append(digest)

    # On-disk total counts each blob once, however many models reference it.
    on_disk = sum(blob_sizes.values())

    orphans = []
    blobs_dir = models_dir / "blobs"
    if blobs_dir.is_dir():
        referenced = set(blob_sizes) | set(missing)
        referenced_files = {d.replace(":", "-") for d in referenced}
        for blob in blobs_dir.iterdir():
            if blob.is_file() and blob.name not in referenced_files:
                orphans.append({"file": blob.name, "size": blob.stat().st_size})

    return {
        "root": str(root),
        "models_dir": str(models_dir),
        "exists": models_dir.is_dir(),
        "model_count": len(manifests),
        "models": sorted(manifests, key=lambda m: -m["size"]),
        "blob_sizes": blob_sizes,
        "on_disk_bytes": on_disk,
        "missing_blobs": missing,
        "orphan_blobs": sorted(orphans, key=lambda o: -o["size"]),
        "orphan_bytes": sum(o["size"] for o in orphans),
    }


def compare(surveys: list[dict[str, Any]]) -> dict[str, Any]:
    """What each store holds uniquely, and what a merge would actually cost."""
    by_digest: dict[str, set[int]] = defaultdict(set)
    sizes: dict[str, int] = {}
    for index, s in enumerate(surveys):
        for digest, size in s["blob_sizes"].items():
            by_digest[digest].add(index)
            sizes[digest] = size

    shared = {d for d, where in by_digest.items() if len(where) > 1}
    shared_bytes = sum(sizes[d] for d in shared)
    union_bytes = sum(sizes.values())

    unique = []
    for index, s in enumerate(surveys):
        own = {d for d, where in by_digest.items() if where == {index}}
        names = {m["name"] for m in s["models"]}
        others = set().union(
            *[{m["name"] for m in o["models"]} for i, o in enumerate(surveys) if i != index]
        ) if len(surveys) > 1 else set()
        unique.append(
            {
                "root": s["root"],
                "unique_blob_bytes": sum(sizes[d] for d in own),
                "models_only_here": sorted(names - others),
            }
        )

    return {
        "shared_blob_bytes": shared_bytes,
        "union_blob_bytes": union_bytes,
        "sum_of_stores_bytes": sum(s["on_disk_bytes"] for s in surveys),
        "per_store": unique,
    }


def render(surveys: list[dict[str, Any]], comparison: Optional[dict[str, Any]]) -> str:
    active = default_store()
    env_set = os.getenv("OLLAMA_MODELS")

    lines = ["", "Ollama model stores", "=" * 68, ""]
    lines.append("Active store resolution:")
    if env_set:
        lines.append(f"  OLLAMA_MODELS is set -> {env_set}")
    else:
        lines.append("  OLLAMA_MODELS is NOT set, so ollama uses the default:")
        lines.append(f"  {active}")
    lines.append("")

    for s in surveys:
        lines.append("-" * 68)
        is_active = Path(s["models_dir"]).resolve() == active.resolve()
        marker = "  <-- ACTIVE" if is_active else ""
        lines.append(f"{s['root']}{marker}")
        if not s["exists"]:
            lines.append("  not found (no manifests directory here)")
            continue
        lines.append(f"  models on disk : {s['model_count']}")
        lines.append(f"  size on disk   : {human(s['on_disk_bytes'])}")
        if s["orphan_blobs"]:
            lines.append(
                f"  orphan blobs   : {len(s['orphan_blobs'])} "
                f"({human(s['orphan_bytes'])}) - referenced by no manifest, "
                f"reclaimable with 'ollama prune' or by deleting them"
            )
        if s["missing_blobs"]:
            lines.append(
                f"  MISSING blobs  : {len(s['missing_blobs'])} - these models "
                f"are incomplete and will fail to load"
            )
        lines.append("")
        for model in s["models"][:15]:
            lines.append(f"    {human(model['size']):>10}  {model['name']}")
        if len(s["models"]) > 15:
            lines.append(f"    ... and {len(s['models']) - 15} more")
        lines.append("")

    if comparison and len(surveys) > 1:
        lines.append("=" * 68)
        lines.append("Merge analysis")
        lines.append("")
        saved = comparison["sum_of_stores_bytes"] - comparison["union_blob_bytes"]
        lines.append(f"  sum of both stores      : {human(comparison['sum_of_stores_bytes'])}")
        lines.append(f"  after merge (deduped)   : {human(comparison['union_blob_bytes'])}")
        lines.append(f"  duplicated across both  : {human(comparison['shared_blob_bytes'])}")
        lines.append(f"  space reclaimed by merge: {human(saved)}")
        lines.append("")
        for entry in comparison["per_store"]:
            lines.append(f"  Only in {entry['root']}:")
            lines.append(f"    unique data: {human(entry['unique_blob_bytes'])}")
            for name in entry["models_only_here"][:10]:
                lines.append(f"      - {name}")
            if len(entry["models_only_here"]) > 10:
                lines.append(f"      ... and {len(entry['models_only_here']) - 10} more")
            lines.append("")

        lines.append("  Blobs are content-addressed by sha256, so identical layers")
        lines.append("  in both stores are byte-identical. Copying blobs/ and")
        lines.append("  manifests/ from one store into the other, skipping files")
        lines.append("  that already exist, is therefore safe and lossless.")
        lines.append("")
        lines.append("  Verify before deleting anything: point OLLAMA_MODELS at the")
        lines.append("  merged store, restart ollama, run 'ollama list', and load a")
        lines.append("  model that came from each side.")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inventory Ollama model stores and compute merge cost."
    )
    parser.add_argument(
        "roots",
        nargs="*",
        help="one or more .ollama (or .ollama/models) directories",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead")
    args = parser.parse_args(argv)

    roots = [Path(r) for r in args.roots] or [default_store()]
    surveys = [survey(root) for root in roots]
    present = [s for s in surveys if s["exists"]]
    comparison = compare(present) if len(present) > 1 else None

    if args.json:
        # blob_sizes is large and not useful downstream; drop it.
        trimmed = [{k: v for k, v in s.items() if k != "blob_sizes"} for s in surveys]
        print(json.dumps({"stores": trimmed, "comparison": comparison}, indent=2))
    else:
        print(render(surveys, comparison))

    return 0 if present else 1


if __name__ == "__main__":
    raise SystemExit(main())
