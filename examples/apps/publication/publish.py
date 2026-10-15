"""Build final v1 release assets after the image digest is known."""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from manifest import read_manifest, validate_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=Path("manifest.template.json"))
    parser.add_argument("--assets", type=Path, default=Path("workloads"))
    parser.add_argument("--output", type=Path, default=Path("publication"))
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    manifest = read_manifest(args.template, allow_placeholders=True)
    manifest["source_commit"] = os.environ["GITHUB_SHA"]
    manifest["image"] = args.image
    args.output.mkdir(exist_ok=False)
    names = {"manifest.json", "publication.json"}
    for workload in manifest.get("workloads", []):
        name = workload["asset"]
        if Path(name).name != name or name in names:
            raise ValueError("Workload assets need unique basenames")
        names.add(name)
        source = args.assets / name
        if (
            source.is_symlink()
            or not source.is_file()
            or source.stat().st_size > 20 * 1024**2
        ):
            raise ValueError("Workload asset is missing or oversized")
        workload["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        shutil.copyfile(source, args.output / name)
    final = args.output / "manifest.json"
    final_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    validate_manifest(final_bytes)
    final.write_bytes(final_bytes)
    assets = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(args.output.iterdir())
    }
    workflow_ref = os.environ["GITHUB_WORKFLOW_REF"]
    repository = os.environ["GITHUB_REPOSITORY"]
    workflow = workflow_ref.removeprefix(repository + "/").split("@", 1)[0]
    proof = {
        "publication_version": 1,
        "repository": repository,
        "branch": os.environ["GITHUB_REF_NAME"],
        "workflow": workflow,
        "source_commit": manifest["source_commit"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "manifest_sha256": assets["manifest.json"],
        "assets": assets,
    }
    (args.output / "publication.json").write_text(
        json.dumps(proof, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
