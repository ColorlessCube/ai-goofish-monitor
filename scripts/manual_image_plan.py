"""Read-only Goofish image planning. No apply, SSH, Docker or production access."""
import argparse
import json
import subprocess
import tempfile

if __package__:
    from .image_manifest import REPO, manifest_from_logs, matches, require, unique_object
else:
    from image_manifest import REPO, manifest_from_logs, matches, require, unique_object

ROOT = "/repos/" + REPO + "/actions/"


def positive(value):
    return type(value) is int and value > 0


def candidate(run_id, *, get):
    require(matches(r"[1-9][0-9]{0,19}", run_id))
    run = get(ROOT + "runs/" + run_id)
    require(positive(run["id"]) and str(run["id"]) == run_id)
    require(run["status"] == "completed" and run["conclusion"] == "success")
    require(run["event"] == "push" and run["head_branch"] == "master")
    require(run["repository"]["full_name"] == REPO)
    require(matches(r"[0-9a-f]{40}", run["head_sha"]))
    require(positive(run["workflow_id"]) and positive(run["run_attempt"]))
    workflow = get(ROOT + "workflows/" + str(run["workflow_id"]))
    require(positive(workflow["id"]) and workflow["id"] == run["workflow_id"])
    require(workflow["path"] == ".github/workflows/ci.yml")
    jobs = get(ROOT + f"runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100")
    require(isinstance(jobs["jobs"], list))
    require(type(jobs["total_count"]) is int and jobs["total_count"] == len(jobs["jobs"]))
    selected = [job for job in jobs["jobs"] if job["name"] == "image"]
    require(len(selected) == 1)
    job = selected[0]
    require(positive(job["id"]) and positive(job["run_id"]) and positive(job["run_attempt"]))
    require(job["run_id"] == run["id"] and job["run_attempt"] == run["run_attempt"])
    require(job["head_sha"] == run["head_sha"])
    require(job["status"] == "completed" and job["conclusion"] == "success")
    logs = get(ROOT + f"jobs/{job['id']}/logs", raw=True)
    target = manifest_from_logs(logs, run_id, run["head_sha"])
    latest = get(ROOT + "runs/" + run_id)
    require(all(latest[key] == run[key] for key in (
        "id", "status", "conclusion", "event", "head_branch", "head_sha", "workflow_id",
        "run_attempt", "repository",
    )))
    return target


def private_package(value):
    require(isinstance(value, dict), "private_package_unverified")
    require(value.get("visibility") == "private", "private_package_unverified")
    require(value.get("name") == "ai-goofish-monitor" and value.get("package_type") == "container")
    require(isinstance(value.get("repository"), dict) and value["repository"].get("full_name") == REPO)
    return value


def verify_registry(target, *, get):
    path = "/users/ColorlessCube/packages/container/ai-goofish-monitor"
    private_package(get(path))
    selected = []
    expected = target["images"]["AIGOOFISH_IMAGE"].split("@", 1)[1]
    tag = "build-" + target["commit"]
    for page in range(1, 101):
        versions = get(path + "/versions?per_page=100&page=" + str(page))
        require(isinstance(versions, list) and len(versions) <= 100)
        for version in versions:
            require(isinstance(version, dict) and matches(r"sha256:[0-9a-f]{64}", version["name"]))
            tags = version["metadata"]["container"]["tags"]
            require(isinstance(tags, list) and all(isinstance(item, str) for item in tags))
            if tag in tags:
                selected.append(version["name"])
        if len(versions) < 100:
            break
    else:
        raise ValueError("registry_pagination_incomplete")
    require(selected == [expected], "immutable_tag_digest_mismatch")
    private_package(get(path))


def get(path, raw=False):
    """GET only; raw logs are never printed and memory consumption is bounded."""
    require(isinstance(path, str) and path.startswith((ROOT, "/users/ColorlessCube/packages/")))
    limit = 8 * 1024 * 1024 if raw else 2 * 1024 * 1024
    with tempfile.TemporaryFile() as output:
        subprocess.run(
            ["gh", "api", "--hostname", "github.com", "--method", "GET", path],
            stdout=output, stderr=subprocess.DEVNULL, timeout=60, check=True,
        )
        require(output.tell() <= limit, "response_size")
        output.seek(0)
        data = output.read(limit + 1)
    require(len(data) <= limit, "response_size")
    return data.decode("utf-8") if raw else json.loads(data, object_pairs_hook=unique_object)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid_arguments")


def main(argv=None, *, get=get):
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan", action="store_true", help="Read-only (the only mode)")
    try:
        args = parser.parse_args(argv)
        require(matches(r"[1-9][0-9]{0,19}", args.run_id))
    except ValueError:
        print(json.dumps({"error": "invalid_arguments"}))
        return 2
    try:
        target = candidate(args.run_id, get=get)
        verify_registry(target, get=get)
        # Repeat source attestation after registry reads: fail if an attempt changed.
        require(candidate(args.run_id, get=get) == target)
    except (OSError, subprocess.SubprocessError):
        print(json.dumps({"error": "read_failed"}))
        return 1
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, RecursionError):
        print(json.dumps({"error": "invalid_source"}))
        return 1
    print(json.dumps({
        "mode": "plan-only", "target": target, "current": None,
        "deployment_performed": False, "confirmation_required": True,
        "registry_verification": "private-package-immutable-tag-and-digest",
        "prerequisites": ["verified backup", "runtime and migration compatibility review",
                          "retain previous digest for rollback", "explicit user approval"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
