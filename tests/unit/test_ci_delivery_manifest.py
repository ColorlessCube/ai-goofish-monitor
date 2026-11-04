"""Publish-only manifest contract: no registry or production access."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
RUN_ID = "123"


def load_module(name):
    path = ROOT / "scripts" / (name + ".py")
    assert path.is_file(), f"missing CI helper: {path.name}"
    spec = importlib.util.spec_from_file_location("scripts." + name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest():
    return {
        "schema": 1, "service": "ai-goofish-monitor",
        "repository": "ColorlessCube/ai-goofish-monitor", "commit": SHA,
        "run_id": RUN_ID, "baseline": "b" * 64,
        "images": {"AIGOOFISH_IMAGE": "ghcr.io/colorlesscube/ai-goofish-monitor@sha256:" + "c" * 64},
    }


def test_manifest_validates_exact_release_identity():
    helper = load_module("image_manifest")
    assert helper.decode_manifest(json.dumps(manifest()), RUN_ID, SHA) == manifest()


@pytest.mark.parametrize("key,value", [
    ("schema", True), ("schema", 2), ("schema", "1"),
    ("service", "ass"), ("repository", "attacker/ai-goofish-monitor"),
    ("commit", "d" * 40), ("commit", SHA.upper()), ("run_id", 123),
    ("run_id", "124"), ("baseline", "b" * 63), ("baseline", None),
    ("extra", "untrusted"), ("images", {}),
    ("images", {"AIGOOFISH_IMAGE": "ghcr.io/colorlesscube/ai-goofish-monitor:latest"}),
    ("images", {"AIGOOFISH_IMAGE": "ghcr.io/attacker/ai-goofish-monitor@sha256:" + "c" * 64}),
    ("images", {"OTHER_IMAGE": manifest()["images"]["AIGOOFISH_IMAGE"]}),
])
def test_manifest_rejects_wrong_context_and_noncanonical_fields(key, value):
    helper = load_module("image_manifest")
    value_manifest = manifest()
    value_manifest[key] = value
    with pytest.raises(ValueError):
        helper.decode_manifest(json.dumps(value_manifest), RUN_ID, SHA)


@pytest.mark.parametrize("raw", [
    "[]", "null", "{", "{}", '"' + "x" * 16384 + '"',
    json.dumps(manifest()).replace('"schema": 1', '"schema": 1, "schema": 1'),
    json.dumps(manifest()).replace('"AIGOOFISH_IMAGE":', '"AIGOOFISH_IMAGE": null, "AIGOOFISH_IMAGE":'),
    "[" * 2000 + "]" * 2000,
])
def test_manifest_rejects_duplicate_unbounded_or_malformed_json(raw):
    helper = load_module("image_manifest")
    with pytest.raises(ValueError):
        helper.decode_manifest(raw, RUN_ID, SHA)


@pytest.mark.parametrize("run_id,sha", [("01", SHA), ("1" * 21, SHA), (RUN_ID, "a" * 39)])
def test_manifest_rejects_malformed_expected_identity(run_id, sha):
    helper = load_module("image_manifest")
    value = manifest()
    value.update(run_id=run_id, commit=sha)
    with pytest.raises(ValueError):
        helper.decode_manifest(json.dumps(value), run_id, sha)
