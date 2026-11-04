"""Publish one CI-tested image via the reviewed toolkit, never deploy.

The pinned toolkit can PATCH package visibility. This caller deliberately removes
that capability: the existing package must already be private, before AND after
push. A failure after pushing requires a NEW commit, never overwrite build-<SHA>.
"""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import types

if __package__:
    from .image_manifest import IMAGE, MAX_BYTES, REPO, SERVICE, decode_manifest, log_record, matches, require, unique_object
    from .manual_image_plan import private_package, verify_registry
else:
    from image_manifest import IMAGE, MAX_BYTES, REPO, SERVICE, decode_manifest, log_record, matches, require, unique_object
    from manual_image_plan import private_package, verify_registry
import json

TOOLKIT_REF = "13ae397ae0e116eeac0bf388c40f4b12a7f82855"
PUBLISH_SOURCE_SHA256 = "8797dc8e806853bfc3b0df039bdeef49deb9110a2f02f9dfa08b4061b81a2b9e"
CONFIG = {
    "service": SERVICE, "branch": "master", "compose_files": ["infra/nas/compose.yaml"],
    "images": {"AIGOOFISH_IMAGE": {"local": "ai-goofish-monitor:tested", "repository": IMAGE}},
}


def load_toolkit(directory):
    path = directory / "infra/deploy/publish.py"
    source = path.read_bytes()
    require(hashlib.sha256(source).hexdigest() == PUBLISH_SOURCE_SHA256, "toolkit_source_mismatch")
    module = types.ModuleType("reviewed_goofish_publish")
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def publish(config, output, *, toolkit, environment=None):
    env = os.environ if environment is None else environment
    require(env.get("GITHUB_REPOSITORY") == REPO)
    require(env.get("GITHUB_EVENT_NAME") == "push" and env.get("GITHUB_REF") == "refs/heads/master")
    require(env.get("PUBLISH_EVENT_DELETED") == "false")
    require(env.get("OWNER_TYPE") == "User")
    require(matches(r"[0-9a-f]{40}", env.get("GITHUB_SHA")))
    require(matches(r"[1-9][0-9]{0,19}", env.get("GITHUB_RUN_ID")))
    require(config == CONFIG, "release_configuration_mismatch")
    original_api = toolkit.api
    def read_only_api(path, method="GET", payload=None):
        require(method == "GET" and payload is None, "package_mutation_forbidden")
        return original_api(path, method="GET", payload=None)
    toolkit.api = read_only_api
    def existing_private(path, unused_write_path):
        return private_package(read_only_api(path))
    toolkit.ensure_private_package = existing_private
    # Preflight is independent of the toolkit to forbid onboarding or privacy repair.
    existing_private("/users/ColorlessCube/packages/container/ai-goofish-monitor", "")
    toolkit.publish(config, output)
    with (output / "deploy.json").open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    target = decode_manifest(raw, env["GITHUB_RUN_ID"], env["GITHUB_SHA"])
    verify_registry(target, get=read_only_api)
    print(log_record(target, env["GITHUB_RUN_ID"], env["GITHUB_SHA"]))
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--toolkit", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        toolkit = load_toolkit(args.toolkit)
        config = json.loads(args.config.read_text(), object_pairs_hook=unique_object)
        publish(config, args.output, toolkit=toolkit)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        print("image_publication_rejected")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
