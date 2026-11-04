import json
import subprocess
import sys

import pytest

from test_ci_delivery_manifest import ROOT as REPOSITORY, RUN_ID, load_module, manifest
from test_ci_delivery_plan import responses
from test_ci_delivery_registry import PACKAGE, metadata, version


def test_plan_cli_verifies_source_and_registry_without_applying(capsys):
    helper = load_module("manual_image_plan")
    assert hasattr(helper, "main"), "missing read-only planner CLI"
    values = responses()
    values[PACKAGE] = metadata()
    values[PACKAGE + "/versions?per_page=100&page=1"] = [version()]
    calls = []
    def get(path, raw=False):
        calls.append(path)
        return values[path]
    assert helper.main(["--plan", "--run-id", RUN_ID], get=get) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "plan-only"
    assert result["deployment_performed"] is False
    assert result["confirmation_required"] is True
    assert result["target"] == manifest()
    assert PACKAGE in calls and len(calls) >= 8


@pytest.mark.parametrize("argv", [["--apply"], ["--inspect-nas", "--run-id", RUN_ID], ["--run-id", "01"], ["--run-id", "123;echo SECRET"]])
def test_cli_rejects_unsafe_arguments_before_any_read(argv, capsys):
    helper = load_module("manual_image_plan")
    assert hasattr(helper, "main"), "missing read-only planner CLI"
    def blocked(*args, **kwargs):
        pytest.fail("invalid arguments must not access any remote system")
    assert helper.main(argv, get=blocked) != 0
    output = capsys.readouterr()
    assert "SECRET" not in output.out + output.err


def test_cli_read_failure_is_redacted(capsys):
    helper = load_module("manual_image_plan")
    assert hasattr(helper, "main"), "missing read-only planner CLI"
    def failed(*args, **kwargs):
        raise OSError("SECRET signed download URL")
    assert helper.main(["--plan", "--run-id", RUN_ID], get=failed) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "read_failed"}


def test_gh_transport_only_get_and_bounded_output(tmp_path, monkeypatch):
    helper = load_module("manual_image_plan")
    assert hasattr(helper, "get"), "missing bounded GET transport"
    executable = tmp_path / "gh"
    executable.write_text("#!" + sys.executable + "\nimport sys,json\nassert sys.argv[1:6] == ['api','--hostname','github.com','--method','GET']\nprint(json.dumps({'safe':True}))\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert helper.get(PACKAGE) == {"safe": True}
    executable.write_text("#!" + sys.executable + "\nprint('x'* (8*1024*1024+1))\n")
    with pytest.raises(ValueError):
        helper.get(PACKAGE, raw=True)


def test_real_cli_rejects_apply_without_network():
    path = REPOSITORY / "scripts/manual_image_plan.py"
    result = subprocess.run([sys.executable, str(path), "--apply"], capture_output=True, text=True)
    assert result.returncode != 0
    assert json.loads(result.stdout) == {"error": "invalid_arguments"}
