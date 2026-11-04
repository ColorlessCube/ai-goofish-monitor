"""Read-only planner tests use API fixtures, never GitHub or a NAS."""
import copy

import pytest

from test_ci_delivery_manifest import RUN_ID, SHA, load_module, manifest
from test_ci_delivery_logs import record

REPO = "ColorlessCube/ai-goofish-monitor"
ROOT = "/repos/" + REPO + "/actions/"


def responses():
    run = dict(id=123, status="completed", conclusion="success", event="push", head_branch="master",
               head_sha=SHA, workflow_id=10, run_attempt=2, repository={"full_name": REPO})
    job = dict(id=20, name="image", run_id=123, run_attempt=2, head_sha=SHA,
               status="completed", conclusion="success")
    return {
        ROOT + "runs/123": run,
        ROOT + "workflows/10": {"id": 10, "path": ".github/workflows/ci.yml"},
        ROOT + "runs/123/attempts/2/jobs?per_page=100": {"total_count": 1, "jobs": [job]},
        ROOT + "jobs/20/logs": record(),
    }


def test_candidate_requires_latest_successful_master_image_job():
    helper = load_module("manual_image_plan")
    values = responses()
    calls = []
    def get(path, raw=False):
        calls.append(path)
        return copy.deepcopy(values[path])
    assert helper.candidate(RUN_ID, get=get) == manifest()
    assert calls[-1] == ROOT + "runs/123"
    assert all("artifacts" not in path for path in calls)


@pytest.mark.parametrize("scope,key,value", [
    ("run", "event", "pull_request"), ("run", "head_branch", "main"),
    ("run", "status", "in_progress"), ("run", "conclusion", "failure"),
    ("run", "id", 124), ("run", "head_sha", "a" * 39),
    ("run", "repository", {"full_name": "attacker/repo"}),
    ("run", "run_attempt", True), ("workflow", "path", ".github/workflows/delivery.yml"),
    ("workflow", "id", 11), ("job", "run_attempt", 1), ("job", "name", "publish"),
    ("job", "run_id", 1), ("job", "id", True), ("job", "head_sha", "b" * 40),
    ("job", "status", "in_progress"), ("job", "conclusion", "skipped"),
])
def test_candidate_rejects_wrong_source(scope, key, value):
    helper = load_module("manual_image_plan")
    values = responses()
    target = {"run": values[ROOT + "runs/123"], "workflow": values[ROOT + "workflows/10"],
              "job": values[ROOT + "runs/123/attempts/2/jobs?per_page=100"]["jobs"][0]}[scope]
    target[key] = value
    with pytest.raises(ValueError):
        helper.candidate(RUN_ID, get=lambda path, raw=False: copy.deepcopy(values[path]))


@pytest.mark.parametrize("variant", ["duplicate", "incomplete", "race", "missing"])
def test_candidate_rejects_incomplete_or_racing_job_list(variant):
    helper = load_module("manual_image_plan")
    values = responses()
    jobs = values[ROOT + "runs/123/attempts/2/jobs?per_page=100"]
    if variant == "duplicate":
        jobs["jobs"] *= 2
        jobs["total_count"] = 2
    elif variant == "incomplete":
        jobs["total_count"] = 101
    elif variant == "missing":
        jobs.update(jobs=[], total_count=0)
    reads = 0
    def get(path, raw=False):
        nonlocal reads
        result = copy.deepcopy(values[path])
        if path.endswith("runs/123"):
            reads += 1
            if variant == "race" and reads == 2:
                result["run_attempt"] = 3
        return result
    with pytest.raises(ValueError):
        helper.candidate(RUN_ID, get=get)
