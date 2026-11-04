"""Publication boundary: exercise the wrapper with external effects replaced."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_ci_delivery_manifest import RUN_ID, SHA, load_module, manifest
from test_ci_delivery_registry import metadata, version


def environment():
    return dict(GITHUB_REPOSITORY="ColorlessCube/ai-goofish-monitor", GITHUB_SHA=SHA,
                GITHUB_RUN_ID=RUN_ID, GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/master",
                PUBLISH_REF_PROTECTED="true", PUBLISH_EVENT_DELETED="false", OWNER_TYPE="User")


def config():
    return {"service": "ai-goofish-monitor", "branch": "master",
            "compose_files": ["infra/nas/compose.yaml"],
            "images": {"AIGOOFISH_IMAGE": {"local": "ai-goofish-monitor:tested",
                "repository": "ghcr.io/colorlesscube/ai-goofish-monitor"}}}


def toolkit_fixture():
    calls = []
    def api(path, method="GET", payload=None):
        calls.append((path, method, payload))
        return [version()] if "/versions?" in path else metadata()
    toolkit = SimpleNamespace(api=api)
    def publish(configuration, output):
        toolkit.ensure_private_package("/users/colorlesscube/packages/container/ai-goofish-monitor", "unused")
        output.mkdir()
        (output / "deploy.json").write_text(json.dumps(manifest()))
    toolkit.publish = publish
    return toolkit, calls


def test_publish_uses_pinned_toolkit_without_mutating_package_visibility(tmp_path, capsys):
    helper = load_module("publish_image_manifest")
    toolkit, calls = toolkit_fixture()
    result = helper.publish(config(), tmp_path / "result", toolkit=toolkit, environment=environment())
    assert result == manifest()
    assert capsys.readouterr().out.startswith("GOOFISH_IMAGE_MANIFEST ")
    assert calls and all(method == "GET" and payload is None for _, method, payload in calls)
    with pytest.raises(ValueError):
        toolkit.api("/user/packages/container/ai-goofish-monitor", method="PATCH", payload={"visibility": "private"})


@pytest.mark.parametrize("protection", ["false", None, "true"])
def test_publish_contract_does_not_require_unconfigured_branch_protection(tmp_path, capsys, protection):
    helper = load_module("publish_image_manifest")
    toolkit, calls = toolkit_fixture()
    env = environment()
    if protection is None:
        env.pop("PUBLISH_REF_PROTECTED", None)
    else:
        env["PUBLISH_REF_PROTECTED"] = protection
    target = helper.publish(config(), tmp_path / "result", toolkit=toolkit, environment=env)
    assert target == manifest()
    assert calls and all(method == "GET" for _, method, _ in calls)
    assert capsys.readouterr().out.startswith("GOOFISH_IMAGE_MANIFEST ")


@pytest.mark.parametrize("key,value", [
    ("GITHUB_REPOSITORY", "attacker/repo"), ("GITHUB_EVENT_NAME", "pull_request"),
    ("GITHUB_REF", "refs/heads/main"), ("GITHUB_REF", "refs/heads/feature"),
    ("PUBLISH_EVENT_DELETED", "true"), ("PUBLISH_EVENT_DELETED", ""),
    ("GITHUB_SHA", "a" * 39), ("GITHUB_RUN_ID", "0123"), ("OWNER_TYPE", "Organization"),
])
def test_publish_rejects_unapproved_context_before_any_toolkit_use(tmp_path, key, value):
    helper = load_module("publish_image_manifest")
    env = environment()
    env[key] = value
    with pytest.raises(ValueError):
        helper.publish(config(), tmp_path / "result", toolkit=SimpleNamespace(), environment=env)
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("package", [None, {**metadata(), "visibility": "public"}])
def test_publish_missing_or_public_package_fails_before_publication(tmp_path, package, capsys):
    helper = load_module("publish_image_manifest")
    toolkit, _ = toolkit_fixture()
    toolkit.api = lambda *args, **kwargs: package
    with pytest.raises(ValueError):
        helper.publish(config(), tmp_path / "result", toolkit=toolkit, environment=environment())
    assert not (tmp_path / "result").exists()
    assert "GOOFISH_IMAGE_MANIFEST" not in capsys.readouterr().out


def test_publish_rejects_configuration_drift_before_toolkit_use(tmp_path):
    helper = load_module("publish_image_manifest")
    changed = config()
    changed["images"]["AIGOOFISH_IMAGE"]["repository"] = "ghcr.io/attacker/image"
    with pytest.raises(ValueError):
        helper.publish(changed, tmp_path / "result", toolkit=SimpleNamespace(), environment=environment())


def test_pinned_toolkit_loader_rejects_other_source_without_executing(tmp_path):
    helper = load_module("publish_image_manifest")
    path = tmp_path / "infra/deploy/publish.py"
    path.parent.mkdir(parents=True)
    path.write_text("raise RuntimeError('UNTRUSTED')")
    with pytest.raises(ValueError, match="toolkit_source_mismatch"):
        helper.load_toolkit(tmp_path)
