#!/usr/bin/env python3
"""Find every ComfyUI install under the given roots and compare them.

Read-only. Nothing is moved, written or deleted. The point is to decide which
install should become the parent from evidence rather than memory, and to see
exactly what the others hold that it does not.

    python scripts/comfy_survey.py "G:/1.CumfyUI" "G:/Workspace" "G:/Onyx_MCP" ^
        "C:/Users/Adriaan/AppData/Local/Comfy-Desktop"
    python scripts/comfy_survey.py --quick ...      # skip byte totals, much faster
    python scripts/comfy_survey.py --json ... > comfy.json

Model files are compared by name and size rather than by hash: hashing
terabytes is impractical, and a name-and-size match is a strong enough signal
to plan a merge from. Confirm with a hash before deleting anything.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

MODEL_SUFFIXES = {
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf",
    ".onnx", ".sft", ".vae", ".yaml_model",
}

# Directories never worth walking - they are caches or environments, not content.
SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".venv", "venv",
    "site-packages", "python_embeded", "standalone-env", ".cache",
}


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def looks_like_comfy(path: Path) -> bool:
    """A ComfyUI root has main.py alongside the comfy package or nodes.py."""
    if not path.is_dir():
        return False
    has_main = (path / "main.py").is_file()
    has_comfy = (path / "comfy").is_dir() or (path / "nodes.py").is_file()
    return has_main and has_comfy


def find_installs(root: Path, max_depth: int = 6) -> list[Path]:
    """Walk down looking for ComfyUI roots, without descending into one found."""
    found: list[Path] = []
    if not root.exists():
        return found

    def walk(current: Path, depth: int):
        if depth > max_depth:
            return
        try:
            if looks_like_comfy(current):
                found.append(current)
                return  # nested copies inside an install are its own business
            for child in current.iterdir():
                if child.is_dir() and child.name not in SKIP_DIRS:
                    walk(child, depth + 1)
        except (PermissionError, OSError):
            return

    walk(root, 0)
    return found


def git_version(path: Path) -> Optional[str]:
    if not (path / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "log", "-1", "--format=%h %cs"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def declared_version(path: Path) -> Optional[str]:
    """ComfyUI writes its version into comfyui_version.py."""
    for candidate in (path / "comfyui_version.py", path / "comfy" / "comfyui_version.py"):
        if candidate.is_file():
            try:
                for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                    if "__version__" in line and "=" in line:
                        return line.split("=", 1)[1].strip().strip("\"'")
            except OSError:
                pass
    return None


def scan_models(models_dir: Path, quick: bool) -> dict[str, Any]:
    """Model files grouped by their category directory."""
    categories: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []
    if not models_dir.is_dir():
        return {"categories": {}, "files": [], "total_bytes": 0, "file_count": 0}

    for category_dir in sorted(models_dir.iterdir()):
        if not category_dir.is_dir():
            continue
        count = 0
        size = 0
        for path in category_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in MODEL_SUFFIXES:
                continue
            count += 1
            try:
                stat_size = 0 if quick else path.stat().st_size
            except OSError:
                stat_size = 0
            size += stat_size
            files.append(
                {"name": path.name, "size": stat_size, "category": category_dir.name}
            )
        if count:
            categories[category_dir.name] = {"count": count, "bytes": size}

    return {
        "categories": categories,
        "files": files,
        "total_bytes": sum(c["bytes"] for c in categories.values()),
        "file_count": sum(c["count"] for c in categories.values()),
    }


def count_workflows(path: Path) -> int:
    """Saved workflows live under user/ in current ComfyUI, or workflows/."""
    total = 0
    for candidate in (path / "user" / "default" / "workflows", path / "workflows"):
        if candidate.is_dir():
            total += sum(1 for p in candidate.rglob("*.json") if p.is_file())
    return total


def survey(path: Path, quick: bool) -> dict[str, Any]:
    custom_nodes_dir = path / "custom_nodes"
    custom_nodes = []
    if custom_nodes_dir.is_dir():
        for entry in sorted(custom_nodes_dir.iterdir()):
            if entry.is_dir() and entry.name not in {"__pycache__"}:
                custom_nodes.append(entry.name)
            elif entry.is_file() and entry.suffix == ".py" and entry.name != "example_node.py.example":
                custom_nodes.append(entry.name)

    models = scan_models(path / "models", quick)

    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        mtime = "?"

    # Which flavour of install this is changes how it should be treated.
    flavour = "git clone" if (path / ".git").exists() else "unknown"
    parent_names = {p.name.lower() for p in path.parents}
    if (path.parent / "python_embeded").is_dir() or "comfyui_windows_portable" in parent_names:
        flavour = "windows portable"
    if "comfy-desktop" in parent_names or path.parent.name == "Comfy-Desktop":
        flavour = "desktop-managed"

    return {
        "path": str(path),
        "flavour": flavour,
        "git": git_version(path),
        "version": declared_version(path),
        "last_modified": mtime,
        "custom_node_count": len(custom_nodes),
        "custom_nodes": custom_nodes,
        "model_file_count": models["file_count"],
        "model_bytes": models["total_bytes"],
        "model_categories": models["categories"],
        "model_files": models["files"],
        "workflow_count": count_workflows(path),
        "has_extra_model_paths": (path / "extra_model_paths.yaml").is_file(),
        "has_manager": any(
            "manager" in n.lower() for n in custom_nodes
        ),
    }


def compare(installs: list[dict[str, Any]]) -> dict[str, Any]:
    node_sources: dict[str, list[str]] = defaultdict(list)
    for inst in installs:
        for node in inst["custom_nodes"]:
            node_sources[node].append(inst["path"])

    model_sources: dict[tuple, list[str]] = defaultdict(list)
    for inst in installs:
        for f in inst["model_files"]:
            model_sources[(f["name"], f["size"])].append(inst["path"])

    duplicated = {k: v for k, v in model_sources.items() if len(set(v)) > 1}
    duplicate_bytes = sum(
        name_size[1] * (len(set(paths)) - 1) for name_size, paths in duplicated.items()
    )

    # A sensible parent has the most custom nodes and the most workflows; models
    # can be relocated far more easily than a working node set can be rebuilt.
    def score(inst):
        return (inst["custom_node_count"], inst["workflow_count"], inst["model_bytes"])

    ranked = sorted(installs, key=score, reverse=True)

    return {
        "install_count": len(installs),
        "total_model_bytes": sum(i["model_bytes"] for i in installs),
        "reclaimable_duplicate_model_bytes": duplicate_bytes,
        "duplicate_model_files": len(duplicated),
        "unique_custom_nodes": len(node_sources),
        "suggested_parent": ranked[0]["path"] if ranked else None,
        "ranking": [i["path"] for i in ranked],
        "nodes_missing_from_parent": (
            sorted(set(node_sources) - set(ranked[0]["custom_nodes"])) if ranked else []
        ),
    }


def render(installs: list[dict[str, Any]], comparison: dict[str, Any], quick: bool) -> str:
    lines = ["", "ComfyUI installs", "=" * 72, ""]
    if quick:
        lines.append("  (--quick: file counts only, byte totals not computed)")
        lines.append("")

    for inst in sorted(installs, key=lambda i: -i["model_bytes"]):
        lines.append("-" * 72)
        lines.append(inst["path"])
        bits = [inst["flavour"]]
        if inst["version"]:
            bits.append(f"v{inst['version']}")
        if inst["git"]:
            bits.append(f"git {inst['git']}")
        lines.append(f"  {' | '.join(bits)}   last modified {inst['last_modified']}")
        lines.append(
            f"  custom nodes {inst['custom_node_count']:<4} "
            f"workflows {inst['workflow_count']:<5} "
            f"model files {inst['model_file_count']:<5} "
            f"{human(inst['model_bytes'])}"
        )
        flags = []
        if inst["has_manager"]:
            flags.append("ComfyUI-Manager present")
        if inst["has_extra_model_paths"]:
            flags.append("extra_model_paths.yaml present")
        if flags:
            lines.append(f"  {', '.join(flags)}")
        if inst["model_categories"]:
            top = sorted(
                inst["model_categories"].items(), key=lambda kv: -kv[1]["bytes"]
            )[:5]
            summary = ", ".join(
                f"{name} {v['count']} ({human(v['bytes'])})" for name, v in top
            )
            lines.append(f"  models: {summary}")
        lines.append("")

    lines.append("=" * 72)
    lines.append("Consolidation analysis")
    lines.append("")
    lines.append(f"  installs found            : {comparison['install_count']}")
    lines.append(f"  total model data          : {human(comparison['total_model_bytes'])}")
    lines.append(
        f"  duplicated model files    : {comparison['duplicate_model_files']} "
        f"({human(comparison['reclaimable_duplicate_model_bytes'])} reclaimable)"
    )
    lines.append(f"  distinct custom nodes     : {comparison['unique_custom_nodes']}")
    lines.append("")
    lines.append(f"  Suggested parent: {comparison['suggested_parent']}")
    lines.append("    ranked by custom nodes, then workflows, then model volume -")
    lines.append("    a working node set is harder to rebuild than models are to move.")
    lines.append("")
    missing = comparison["nodes_missing_from_parent"]
    if missing:
        lines.append(f"  Custom nodes the parent lacks ({len(missing)}):")
        for node in missing[:20]:
            lines.append(f"    - {node}")
        if len(missing) > 20:
            lines.append(f"    ... and {len(missing) - 20} more")
    else:
        lines.append("  The suggested parent already has every custom node found.")
    lines.append("")
    lines.append("  Next: point every install at one models directory with")
    lines.append("  extra_model_paths.yaml before moving a single file. Nothing")
    lines.append("  needs deleting until the parent runs with the shared models.")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Survey ComfyUI installs and plan a consolidation."
    )
    parser.add_argument("roots", nargs="+", help="directories to search")
    parser.add_argument(
        "--quick", action="store_true",
        help="count files but skip byte totals (much faster on large drives)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead")
    args = parser.parse_args(argv)

    paths: list[Path] = []
    for root in args.roots:
        for found in find_installs(Path(root)):
            if found not in paths:
                paths.append(found)

    if not paths:
        print("No ComfyUI installs found under the given roots.", file=sys.stderr)
        return 1

    installs = [survey(p, args.quick) for p in paths]
    comparison = compare(installs)

    if args.json:
        trimmed = [{k: v for k, v in i.items() if k != "model_files"} for i in installs]
        print(json.dumps({"installs": trimmed, "comparison": comparison}, indent=2))
    else:
        print(render(installs, comparison, args.quick))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
