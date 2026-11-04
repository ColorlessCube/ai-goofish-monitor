"""Run the actual reviewed publisher; replace only Docker/GitHub boundaries."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_ci_delivery_manifest import ROOT, SHA, RUN_ID, load_module
from test_ci_delivery_publish import config, environment
from test_ci_delivery_registry import metadata, version


@pytest.fixture
def real_toolkit(tmp_path, monkeypatch):
    helper = load_module("publish_image_manifest")
    fixture = ROOT / "tests/fixtures/nas_deploy_toolkit"
    source = fixture / "infra/deploy/publish.py"
    assert source.is_file(), "reviewed publisher fixture is missing"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == helper.PUBLISH_SOURCE_SHA256
    toolkit = helper.load_toolkit(fixture)
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    compose = tmp_path / "infra/nas/compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services:\n  app:\n    image: ${AIGOOFISH_IMAGE}\n")
    state = {"pushed": False, "existing": False, "private": True, "bad_provenance": False, "bad_registry": False, "commands": [], "api_calls": []}

    def api(path, method="GET", payload=None):
        state["api_calls"].append((path, method, payload))
        assert method == "GET" and payload is None
        if "/versions?" in path:
            if not (state["existing"] or state["pushed"]):
                return []
            result = version()
            if state["bad_registry"]:
                result["name"] = "sha256:" + "d" * 64
            return [result]
        result = metadata()
        if not state["private"]:
            result["visibility"] = "public"
        return result

    def inspect(command):
        state["commands"].append(command)
        assert command[:2] == ["docker", "inspect"]
        return json.dumps([{"Os": "linux", "Architecture": "amd64", "Config": {"Labels": {
            "org.opencontainers.image.revision": "b" * 40 if state["bad_provenance"] else SHA,
            "org.opencontainers.image.source": "https://github.com/ColorlessCube/ai-goofish-monitor",
        }}, "RepoDigests": ["ghcr.io/colorlesscube/ai-goofish-monitor@sha256:" + "c" * 64]}]).encode()

    def docker(command, check):
        state["commands"].append(command)
        assert check is True and command[:2] in (["docker", "tag"], ["docker", "push"])
        if command[1] == "push":
            state["pushed"] = True
        return SimpleNamespace(returncode=0)

    toolkit.api = api
    toolkit.subprocess = SimpleNamespace(check_output=inspect, run=docker)
    return helper, toolkit, state, tmp_path


def test_real_reviewed_publisher_generates_verified_manifest_without_rebuild(real_toolkit, capsys):
    helper, toolkit, state, tmp = real_toolkit
    result = helper.publish(config(), tmp / "output", toolkit=toolkit)
    assert result["commit"] == SHA and result["run_id"] == RUN_ID
    assert result["images"]["AIGOOFISH_IMAGE"].endswith("@sha256:" + "c" * 64)
    disk = json.loads((tmp / "output/deploy.json").read_text())
    assert disk == result
    hashes = {"compose.yaml": hashlib.sha256((tmp / "infra/nas/compose.yaml").read_bytes()).hexdigest()}
    assert result["baseline"] == hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert [c[1] for c in state["commands"]] == ["inspect", "tag", "push", "inspect"]
    assert all(method == "GET" for _, method, _ in state["api_calls"])
    assert capsys.readouterr().out.startswith("GOOFISH_IMAGE_MANIFEST ")


@pytest.mark.parametrize("reason", ["existing", "private", "bad_provenance"])
def test_real_publisher_rejects_before_push(real_toolkit, reason):
    helper, toolkit, state, tmp = real_toolkit
    state[reason] = False if reason == "private" else True
    with pytest.raises(ValueError):
        helper.publish(config(), tmp / "output", toolkit=toolkit)
    assert not state["pushed"]
    assert not (tmp / "output").exists()


def test_real_publisher_postflight_failure_never_emits_release_record(real_toolkit, capsys):
    helper, toolkit, state, tmp = real_toolkit
    state["bad_registry"] = True
    with pytest.raises(ValueError, match="immutable_tag_digest_mismatch"):
        helper.publish(config(), tmp / "output", toolkit=toolkit)
    assert state["pushed"]
    assert "GOOFISH_IMAGE_MANIFEST" not in capsys.readouterr().out
