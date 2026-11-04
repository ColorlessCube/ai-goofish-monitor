"""Static publish-only CI contract. Run alongside real tests, never instead of them."""
import json
import re
from pathlib import Path
import sys

import yaml

PUBLISH_IF = "${{ github.repository == 'ColorlessCube/ai-goofish-monitor' && github.event_name == 'push' && github.ref == 'refs/heads/master' && github.event.deleted != true }}"
TOOLKIT_REF = "13ae397ae0e116eeac0bf388c40f4b12a7f82855"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_workflow(raw):
    class UniqueLoader(yaml.BaseLoader):
        pass
    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            require(isinstance(key, str) and key not in result, "duplicate_yaml_key")
            result[key] = loader.construct_object(value_node)
        return result
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    return yaml.load(raw, Loader=UniqueLoader)


def check_workflow(value):
    require(value["name"] == "CI", "workflow_name")
    require(set(value["on"]) == {"push", "pull_request"}, "workflow_triggers")
    require(value["on"]["push"]["branches"] == ["**"], "all_branch_checks")
    require(value["permissions"] == {"contents": "read"}, "default_permissions")
    require(value["concurrency"]["cancel-in-progress"] == "${{ github.ref != 'refs/heads/master' }}", "master_not_cancelled")
    require("github.ref" in value["concurrency"]["group"], "concurrency_scope")
    jobs = value["jobs"]
    require(set(jobs) == {"backend", "frontend", "image"}, "no_deploy_or_duplicate_job")
    require(all("if" not in job and "continue-on-error" not in job for job in jobs.values()), "mandatory_jobs")
    backend = "\n".join(step.get("run", "") for step in jobs["backend"]["steps"])
    require("pytest -m 'not live' -q" in backend, "real_non_live_tests")
    require("scripts/check_ci_contract.py" in backend and "PyYAML==6.0.3" in backend, "ci_contract_dependency")
    frontend = jobs["frontend"]
    require(frontend["defaults"]["run"]["working-directory"] == "web-ui", "frontend_directory")
    require([step["run"] for step in frontend["steps"] if "run" in step] == ["npm ci --no-fund", "npm run build"], "frontend_lockfile_build")
    image = jobs["image"]
    require(image["needs"] == ["backend", "frontend"], "checks_before_image")
    require(image["permissions"] == {"contents": "read", "packages": "write"}, "image_permissions")
    require(image["env"]["IMAGE"] == "ai-goofish-monitor:tested", "same_image")
    steps = image["steps"]
    ids = [step.get("id") for step in steps if "id" in step]
    require(len(ids) == len(set(ids)), "unique_step_ids")
    indexed = {step.get("id"): step for step in steps if "id" in step}
    ordered = ["build-image", "provenance", "compose-template", "smoke", "toolkit", "publish-manifest"]
    require(all(name in indexed for name in ordered), "required_image_gates")
    require([ids.index(name) for name in ordered] == sorted(ids.index(name) for name in ordered), "test_before_publication")
    for name in ordered[:4]:
        require("if" not in indexed[name] and "continue-on-error" not in indexed[name], "mandatory_image_gate")
    build = indexed["build-image"]["run"]
    require("docker build --platform linux/amd64" in build and "--file Dockerfile.release" in build and '--tag "$IMAGE"' in build, "release_build")
    require("TOKEN" not in build and "SECRET" not in build and "PASSWORD" not in build, "no_build_credentials")
    for label in ("org.opencontainers.image.revision", "org.opencontainers.image.source", "linux/amd64"):
        require(label in indexed["provenance"]["run"], "image_provenance")
    require("docker compose -f infra/nas/compose.yaml" in indexed["compose-template"]["run"] and "config --quiet" in indexed["compose-template"]["run"], "compose_parse")
    smoke = indexed["smoke"]["run"]
    for fragment in ("docker run", "--network none", '"$IMAGE"', "docker exec", "scripts/smoke_native_mcp.py", "--base-url http://127.0.0.1:8000", "--read-token-env GOOFISH_MCP_READ_TOKEN", "--write-token-env GOOFISH_MCP_WRITE_TOKEN", "GOOFISH_MCP_ENABLED=true", "GOOFISH_MCP_WRITE_SCOPES=tasks:read,tasks:write,tasks:delete,system:read"):
        require(fragment in smoke, "isolated_mcp_smoke")
    scopes = re.findall(r"--env GOOFISH_MCP_WRITE_SCOPES=([^\s]+)", smoke)
    require(scopes == ["tasks:read,tasks:write,tasks:delete,system:read"], "least_privilege_smoke")
    require("--volume" not in smoke and "-v " not in smoke and "--publish" not in smoke, "no_production_mount_or_exposure")
    for name in ("toolkit", "publish-manifest"):
        require(indexed[name]["if"] == PUBLISH_IF and "continue-on-error" not in indexed[name], "protected_master_publication_only")
    checkout = indexed["toolkit"]["with"]
    require(checkout["repository"] == "ColorlessCube/nas-deploy-toolkit" and checkout["ref"] == TOOLKIT_REF, "reviewed_toolkit_pin")
    require(checkout["path"] == ".nas-deploy-toolkit" and checkout["persist-credentials"] == "false", "toolkit_checkout")
    publication = indexed["publish-manifest"]
    require(publication["env"]["PUBLISH_EVENT_DELETED"] == "${{ github.event.deleted }}", "publisher_context")
    require("PUBLISH_REF_PROTECTED" not in publication["env"], "no_unapproved_branch_protection_prerequisite")
    require("python3 scripts/publish_image_manifest.py" in publication["run"], "fail_closed_publisher")
    require("python3 .nas-deploy-toolkit/infra/deploy/publish.py" not in publication["run"], "no_visibility_mutation")
    require("handoff.py export --manifest" in publication["run"], "bounded_handoff")
    require(any(step.get("if") == "always()" and "docker rm --force" in step.get("run", "") for step in steps), "disposable_cleanup")
    combined = json.dumps(value)
    for forbidden in ("upload-artifact", "download-artifact", "docker image save", "docker load", "deploy-nas", "WireGuard", "NAS_WG", "NAS_SSH", "NAS_DEPLOY_ENABLED", "ssh ", "push: true", "docker push", "docker tag", "|| true"):
        require(forbidden not in combined, "forbidden_transport_or_artifact")
    require(combined.count("docker build ") == 1, "build_once")
    for name, job in jobs.items():
        for step in job["steps"]:
            if name in {"backend", "frontend"}:
                require("if" not in step, "mandatory_checks")
            require("continue-on-error" not in step, "mandatory_steps")
            if step.get("uses", "").startswith("actions/checkout@"):
                require(step["with"]["persist-credentials"] == "false", "checkout_credentials")


def check_repository(root):
    path = root / ".github/workflows"
    value = load_workflow((path / "ci.yml").read_text())
    check_workflow(value)
    for name in ("delivery.yml", "docker-image.yml"):
        source = path / name
        if not source.exists():
            continue
        retired = load_workflow(source.read_text())
        require(set(retired["on"]) == {"workflow_dispatch"}, "retired_no_automatic_trigger")
        require(retired["permissions"] == {"contents": "read"}, "retired_readonly")
        require(set(retired["jobs"]) == {"deprecated"}, "retired_no_publish_or_deploy")
        require(retired["jobs"]["deprecated"]["steps"] == [{"run": "echo 'Deprecated. Use CI; production deployment requires separate approval.'"}], "retired_no_effect")
    print("CI publish-only contract verified")


if __name__ == "__main__":
    try:
        check_repository(Path(__file__).resolve().parents[1])
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as error:
        print("CI contract rejected: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
