"""Exact, bounded logs are the handoff; artifacts are not a prerequisite."""
import hashlib
import json

import pytest

from test_ci_delivery_manifest import RUN_ID, SHA, load_module, manifest


def record():
    raw = json.dumps(manifest(), sort_keys=True, separators=(",", ":"))
    return "GOOFISH_IMAGE_MANIFEST " + hashlib.sha256(raw.encode()).hexdigest() + " " + raw


def test_log_handoff_roundtrips_with_checksum_and_github_timestamp():
    helper = load_module("image_manifest")
    assert hasattr(helper, "manifest_from_logs"), "missing verified log handoff"
    assert helper.log_record(manifest(), RUN_ID, SHA) == record()
    logs = "2026-11-03T01:02:03.1234567Z " + record() + "\n"
    assert helper.manifest_from_logs(logs, RUN_ID, SHA) == manifest()


@pytest.mark.parametrize("variant", ["duplicate", "missing", "hash", "oversize", "malformed", "echo", "extra"])
def test_log_handoff_rejects_ambiguous_or_unverified_records(variant):
    helper = load_module("image_manifest")
    assert hasattr(helper, "manifest_from_logs"), "missing verified log handoff"
    logs = {
        "duplicate": record() + "\n" + record(),
        "missing": "nothing here",
        "hash": record().replace(record().split()[1], "0" * 64),
        "oversize": "GOOFISH_IMAGE_MANIFEST " + "x" * 17000,
        "malformed": "GOOFISH_IMAGE_MANIFEST_oops",
        "echo": "echo '" + record() + "'",
        "extra": record() + " suffix",
    }[variant]
    with pytest.raises(ValueError):
        helper.manifest_from_logs(logs, RUN_ID, SHA)
