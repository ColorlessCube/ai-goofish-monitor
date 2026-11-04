"""Fail closed on private GHCR metadata and immutable tag/digest mismatch."""
import copy

import pytest

from test_ci_delivery_manifest import RUN_ID, SHA, load_module, manifest

PACKAGE = "/users/ColorlessCube/packages/container/ai-goofish-monitor"


def metadata():
    return {"name": "ai-goofish-monitor", "package_type": "container", "visibility": "private",
            "repository": {"full_name": "ColorlessCube/ai-goofish-monitor"}}


def version():
    return {"name": "sha256:" + "c" * 64, "metadata": {"container": {"tags": ["build-" + SHA]}}}


def test_registry_verifies_private_package_and_exact_immutable_tag():
    helper = load_module("manual_image_plan")
    assert hasattr(helper, "verify_registry"), "missing private registry verification"
    calls = []
    def get(path):
        calls.append(path)
        return metadata() if path == PACKAGE else [version()]
    helper.verify_registry(manifest(), get=get)
    assert calls == [PACKAGE, PACKAGE + "/versions?per_page=100&page=1", PACKAGE]


@pytest.mark.parametrize("variant", ["public", "missing", "repo", "type", "name", "digest", "tag", "duplicate", "unavailable", "race", "too_many"])
def test_registry_rejects_unverified_private_metadata(variant):
    helper = load_module("manual_image_plan")
    assert hasattr(helper, "verify_registry"), "missing private registry verification"
    package = metadata()
    versions = [version()]
    if variant == "public": package["visibility"] = "public"
    if variant == "missing": package = None
    if variant == "repo": package["repository"]["full_name"] = "other/repo"
    if variant == "type": package["package_type"] = "npm"
    if variant == "name": package["name"] = "other"
    if variant == "digest": versions[0]["name"] = "sha256:" + "d" * 64
    if variant == "tag": versions[0]["metadata"]["container"]["tags"] = ["latest"]
    if variant == "duplicate": versions *= 2
    if variant == "too_many": versions = [{"name": "sha256:" + "d" * 64, "metadata": {"container": {"tags": []}}}] * 100
    reads = 0
    def get(path):
        nonlocal reads
        if variant == "unavailable": raise OSError("private-metadata-unavailable")
        if path == PACKAGE:
            reads += 1
            result = copy.deepcopy(package)
            if variant == "race" and reads == 2: result["visibility"] = "public"
            return result
        return versions
    with pytest.raises((ValueError, OSError)):
        helper.verify_registry(manifest(), get=get)
