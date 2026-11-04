"""Repository-level CI gates; these tests do not run Docker or publish."""
from pathlib import Path
import subprocess

import pytest
import yaml

from test_ci_delivery_manifest import ROOT, load_module


def workflow():
    return yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)


def test_single_workflow_builds_tests_and_publishes_without_nas_or_image_artifacts():
    helper = load_module("check_ci_contract")
    helper.check_repository(ROOT)


def test_release_image_installs_current_runtime_inputs():
    text = (ROOT / "Dockerfile.release").read_text()
    assert "COPY requirements-runtime.txt /tmp/requirements-runtime.txt" in text
    assert "python -m pip install --no-cache-dir -r /tmp/requirements-runtime.txt" in text


def test_docker_context_excludes_runtime_credentials_and_toolkit():
    lines = set((ROOT / ".dockerignore").read_text().splitlines())
    assert {"state/", "state.json", "*_state.json", "config.json", ".env.*", "!.env.example", "**/.env", "**/.env.*", ".nas-deploy-toolkit/", "*.sqlite3", "*.sqlite3-*", "*.db", "*.db-*"} <= lines


def test_environment_transaction_artifacts_are_not_release_inputs():
    artifacts = [".env.lock", ".env-write-fixture", "nested/.env.lock", "nested/.env-write-fixture"]
    result = subprocess.run(["git", "check-ignore", "--stdin"], input="\n".join(artifacts) + "\n", cwd=ROOT, text=True, capture_output=True)
    assert set(result.stdout.splitlines()) == set(artifacts)
    docker = set((ROOT / ".dockerignore").read_text().splitlines())
    assert {".env-write-*", "**/.env-write-*"} <= docker


@pytest.mark.parametrize("variant", ["gate", "cancel", "artifact", "pin", "skip_backend", "skip_frontend", "secret_buildarg", "extra_push", "ignore_failure", "extra_scope"])
def test_contract_rejects_dangerous_workflow_drift(variant):
    helper = load_module("check_ci_contract")
    value = workflow()
    steps = value["jobs"]["image"]["steps"]
    by_id = {step.get("id"): step for step in steps}
    if variant == "gate": by_id["publish-manifest"]["if"] = "${{ always() }}"
    if variant == "cancel": value["concurrency"]["cancel-in-progress"] = "true"
    if variant == "artifact": steps.append({"uses": "actions/upload-artifact@v4"})
    if variant == "pin": by_id["toolkit"]["with"]["ref"] = "main"
    if variant == "skip_backend": value["jobs"]["backend"]["steps"][-1]["if"] = "false"
    if variant == "skip_frontend": value["jobs"]["frontend"]["steps"][-1]["if"] = "false"
    if variant == "secret_buildarg": by_id["build-image"]["run"] += '\n# --build-arg GOOFISH_MCP_WRITE_TOKEN="$GOOFISH_MCP_WRITE_TOKEN"\n'
    if variant == "extra_push": steps.append({"run": 'docker push "ghcr.io/other/image:latest"'})
    if variant == "ignore_failure": by_id["smoke"]["run"] += " || true"
    if variant == "extra_scope": by_id["smoke"]["run"] = by_id["smoke"]["run"].replace("tasks:delete,system:read", "tasks:delete,system:read,tasks:run")
    with pytest.raises(ValueError):
        helper.check_workflow(value)


def test_workflow_yaml_rejects_duplicate_keys():
    helper = load_module("check_ci_contract")
    assert hasattr(helper, "load_workflow"), "missing duplicate-key-safe YAML parser"
    with pytest.raises(ValueError):
        helper.load_workflow("jobs:\n  image: {}\n  image: {}\n")


def test_publication_matches_fixed_master_push_without_protection_prerequisite():
    expected = "${{ github.repository == 'ColorlessCube/ai-goofish-monitor' && github.event_name == 'push' && github.ref == 'refs/heads/master' && github.event.deleted != true }}"
    steps = workflow()["jobs"]["image"]["steps"]
    for step in steps:
        if step.get("id") in {"toolkit", "publish-manifest"}:
            assert step["if"] == expected
    assert "ref_protected" not in str(steps)


def test_publication_uses_nonreserved_context_environment():
    env = next(step for step in workflow()["jobs"]["image"]["steps"] if step.get("id") == "publish-manifest")["env"]
    assert "PUBLISH_REF_PROTECTED" not in env
    assert env["PUBLISH_EVENT_DELETED"] == "${{ github.event.deleted }}"
    assert not any(key.startswith("GITHUB_") for key in env)


def test_all_ci_shell_blocks_parse_without_execution():
    for job in workflow()["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                result = subprocess.run(["bash", "-n"], input=step["run"], text=True, capture_output=True)
                assert result.returncode == 0, result.stderr
