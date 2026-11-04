"""Bounded Goofish image manifest codec; no network and no deployment."""
import hashlib
import json
import re

REPO = "ColorlessCube/ai-goofish-monitor"
SERVICE = "ai-goofish-monitor"
IMAGE = "ghcr.io/colorlesscube/ai-goofish-monitor"
MAX_BYTES = 16384


def require(condition, code="invalid_source"):
    if not condition:
        raise ValueError(code)


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def decode_manifest(raw, run_id, sha):
    require(matches(r"[1-9][0-9]{0,19}", run_id))
    require(matches(r"[0-9a-f]{40}", sha))
    require(isinstance(raw, (str, bytes)))
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    require(0 < len(data) <= MAX_BYTES, "manifest_size")
    try:
        value = json.loads(data, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid_manifest") from None
    require(isinstance(value, dict) and set(value) == {
        "schema", "service", "repository", "commit", "run_id", "baseline", "images"
    })
    require(type(value["schema"]) is int and value["schema"] == 1)
    require(value["service"] == SERVICE and value["repository"] == REPO)
    require(value["commit"] == sha and value["run_id"] == run_id)
    require(matches(r"[0-9a-f]{64}", value["baseline"]))
    require(isinstance(value["images"], dict) and set(value["images"]) == {"AIGOOFISH_IMAGE"})
    require(matches(re.escape(IMAGE) + r"@sha256:[0-9a-f]{64}", value["images"]["AIGOOFISH_IMAGE"]))
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def log_record(value, run_id, sha):
    raw = canonical(value)
    decode_manifest(raw, run_id, sha)
    return "GOOFISH_IMAGE_MANIFEST " + hashlib.sha256(raw.encode()).hexdigest() + " " + raw


def manifest_from_logs(logs, run_id, sha):
    require(isinstance(logs, str) and len(logs.encode()) <= 8 * 1024 * 1024, "log_size")
    records = []
    for line in logs.splitlines():
        line = re.sub(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ", "", line)
        if not line.startswith("GOOFISH_IMAGE_MANIFEST"):
            continue
        parts = line.split(" ", 2)
        require(len(parts) == 3 and parts[0] == "GOOFISH_IMAGE_MANIFEST", "invalid_record")
        _, checksum, raw = parts
        require(matches(r"[0-9a-f]{64}", checksum), "invalid_checksum")
        value = decode_manifest(raw, run_id, sha)
        require(raw == canonical(value), "noncanonical_record")
        require(hashlib.sha256(raw.encode()).hexdigest() == checksum, "checksum_mismatch")
        records.append(value)
    require(len(records) == 1, "ambiguous_manifest")
    return records[0]
