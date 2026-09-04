"""Tests for the consolidation survey scripts.

These produce the numbers a destructive cleanup gets planned from - which
install becomes the parent, how much a merge reclaims - so the arithmetic is
tested against real directory trees rather than trusted.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import comfy_survey  # noqa: E402
import ollama_survey  # noqa: E402


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def _blob(store: Path, content: str, size: int) -> str:
    digest = hashlib.sha256(content.encode()).hexdigest()
    path = store / "models" / "blobs" / f"sha256-{digest}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return f"sha256:{digest}"


def _model(store: Path, name: str, tag: str, layers: list[tuple[str, int]]) -> None:
    manifest = store / "models" / "manifests" / "registry.ollama.ai" / "library" / name / tag
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {},
                "layers": [
                    {"digest": _blob(store, content, size), "size": size}
                    for content, size in layers
                ],
            }
        )
    )


@pytest.fixture
def two_stores(tmp_path):
    g, c = tmp_path / "G", tmp_path / "C"
    # Same content in both, so the same digest - this is what dedupes.
    _model(g, "llama3", "70b", [("llama3-weights", 4000)])
    _model(c, "llama3", "70b", [("llama3-weights", 4000)])
    _model(g, "mixtral", "8x7b", [("mixtral-weights", 3000)])
    _model(c, "qwen", "32b", [("qwen-weights", 2000)])
    return g, c


def test_merge_saving_is_exactly_the_shared_bytes(two_stores):
    g, c = two_stores
    surveys = [ollama_survey.survey(g), ollama_survey.survey(c)]
    result = ollama_survey.compare(surveys)

    assert result["sum_of_stores_bytes"] == 13000  # 7000 + 6000
    assert result["union_blob_bytes"] == 9000      # deduped
    assert result["shared_blob_bytes"] == 4000
    saved = result["sum_of_stores_bytes"] - result["union_blob_bytes"]
    assert saved == result["shared_blob_bytes"]


def test_models_unique_to_each_store_are_named(two_stores):
    g, c = two_stores
    surveys = [ollama_survey.survey(g), ollama_survey.survey(c)]
    per_store = {e["root"]: e for e in ollama_survey.compare(surveys)["per_store"]}

    assert per_store[str(g)]["models_only_here"] == ["mixtral:8x7b"]
    assert per_store[str(c)]["models_only_here"] == ["qwen:32b"]
    # The shared model must not be claimed as unique by either side - deleting
    # a store on that basis would be a real data loss.
    for entry in per_store.values():
        assert "llama3:70b" not in entry["models_only_here"]


def test_orphan_blobs_are_reported_separately(tmp_path):
    store = tmp_path / "G"
    _model(store, "llama3", "70b", [("weights", 1000)])
    (store / "models" / "blobs" / "sha256-orphaned").write_bytes(b"y" * 500)

    result = ollama_survey.survey(store)
    assert result["orphan_bytes"] == 500
    # An orphan is not part of any model, so it must not inflate model sizes.
    assert result["on_disk_bytes"] == 1000


def test_missing_blob_is_flagged_because_the_model_will_not_load(tmp_path):
    store = tmp_path / "G"
    _model(store, "broken", "latest", [("weights", 1000)])
    for blob in (store / "models" / "blobs").iterdir():
        blob.unlink()

    result = ollama_survey.survey(store)
    assert len(result["missing_blobs"]) == 1


def test_active_store_follows_ollama_models_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "elsewhere"))
    assert ollama_survey.default_store() == tmp_path / "elsewhere"

    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    assert ollama_survey.default_store().parts[-2:] == (".ollama", "models")


def test_survey_accepts_either_the_dot_ollama_dir_or_models_dir(two_stores):
    g, _ = two_stores
    assert ollama_survey.survey(g)["model_count"] == 2
    assert ollama_survey.survey(g / "models")["model_count"] == 2


# ---------------------------------------------------------------------------
# ComfyUI
# ---------------------------------------------------------------------------


def _install(root: Path, nodes: list[str], models: dict, workflows: int = 0) -> Path:
    (root / "comfy").mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("# comfy")
    for node in nodes:
        (root / "custom_nodes" / node).mkdir(parents=True, exist_ok=True)
    for category, files in models.items():
        directory = root / "models" / category
        directory.mkdir(parents=True, exist_ok=True)
        for name, size in files:
            (directory / name).write_bytes(b"m" * size)
    wf = root / "user" / "default" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    for i in range(workflows):
        (wf / f"wf{i}.json").write_text("{}")
    return root


def test_finds_installs_and_does_not_recurse_into_one(tmp_path):
    outer = _install(tmp_path / "outer", ["a"], {})
    # A nested copy inside an install is that install's business, not a peer.
    _install(outer / "nested" / "ComfyUI", ["b"], {})
    separate = _install(tmp_path / "separate", ["c"], {})

    found = comfy_survey.find_installs(tmp_path)
    assert set(found) == {outer, separate}


def test_parent_is_chosen_by_node_set_not_model_volume(tmp_path):
    """Models move easily; a working custom-node set does not."""
    rich = _install(
        tmp_path / "rich", ["manager", "impact", "was", "efficiency"],
        {"checkpoints": [("a.safetensors", 100)]}, workflows=12,
    )
    _install(
        tmp_path / "bulky", ["manager"],
        {"checkpoints": [("b.safetensors", 900_000)]}, workflows=1,
    )

    installs = [comfy_survey.survey(p, quick=False)
                for p in comfy_survey.find_installs(tmp_path)]
    result = comfy_survey.compare(installs)

    assert result["suggested_parent"] == str(rich)


def test_duplicate_model_bytes_count_only_the_redundant_copies(tmp_path):
    shared = ("sdxl.safetensors", 5000)
    for name in ("one", "two", "three"):
        _install(tmp_path / name, [], {"checkpoints": [shared]})

    installs = [comfy_survey.survey(p, quick=False)
                for p in comfy_survey.find_installs(tmp_path)]
    result = comfy_survey.compare(installs)

    assert result["duplicate_model_files"] == 1
    # Three copies of one file: two are redundant, not three.
    assert result["reclaimable_duplicate_model_bytes"] == 10000


def test_nodes_missing_from_the_parent_are_listed(tmp_path):
    _install(tmp_path / "parent", ["manager", "impact"], {}, workflows=5)
    _install(tmp_path / "other", ["manager", "videohelper"], {}, workflows=1)

    installs = [comfy_survey.survey(p, quick=False)
                for p in comfy_survey.find_installs(tmp_path)]
    result = comfy_survey.compare(installs)

    assert result["nodes_missing_from_parent"] == ["videohelper"]


def test_quick_mode_counts_files_without_reading_sizes(tmp_path):
    _install(tmp_path / "one", [], {"checkpoints": [("a.safetensors", 5000)]})
    path = comfy_survey.find_installs(tmp_path)[0]

    full = comfy_survey.survey(path, quick=False)
    quick = comfy_survey.survey(path, quick=True)

    assert full["model_file_count"] == quick["model_file_count"] == 1
    assert full["model_bytes"] == 5000
    assert quick["model_bytes"] == 0


def test_non_model_files_are_not_counted_as_models(tmp_path):
    root = _install(tmp_path / "one", [], {"checkpoints": [("a.safetensors", 100)]})
    (root / "models" / "checkpoints" / "put_checkpoints_here.txt").write_text("hi")
    (root / "models" / "checkpoints" / "notes.md").write_text("hi")

    assert comfy_survey.survey(root, quick=False)["model_file_count"] == 1
