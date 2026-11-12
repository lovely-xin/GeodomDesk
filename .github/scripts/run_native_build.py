"""Dispatch an exact GeodomDesk commit and require its native build checks."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def api(path, payload=None):
    command = ["gh", "api", path, "-H", "X-GitHub-Api-Version: 2026-03-10"]
    if payload is not None:
        command.extend(["--method", "POST", "--input", "-"])
    result = subprocess.check_output(
        command, input=json.dumps(payload) if payload is not None else None, text=True
    )
    return json.loads(result)


def run_build(ref, commit, validation):
    repository = os.environ["GITHUB_REPOSITORY"]
    endpoint = f"repos/{repository}/actions"
    dispatch = api(
        f"{endpoint}/workflows/build.yml/dispatches",
        {"ref": ref, "return_run_details": True, "inputs": {
            "expected_sha": commit,
            "validation_only": "true" if validation else "false",
        }},
    )
    run_id = dispatch["workflow_run_id"]
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        summary.write(f"Native build for `{commit}`: {dispatch['html_url']}\n\n")
    run = f"{endpoint}/runs/{run_id}"
    for attempt in range(1, 6):
        try:
            head = api(run)["head_sha"]
            break
        except subprocess.CalledProcessError:
            if attempt == 5:
                raise
            time.sleep(attempt)
    if head != commit:
        raise ValueError("The dispatched build selected a different source commit")
    subprocess.run(
        ["gh", "run", "watch", str(run_id), "--repo", repository, "--interval", "30", "--exit-status"],
        check=True,
    )
    required = {"plan", "bridge", "windows", "ubuntu"}
    if not validation:
        required.update({"android", "release"})
    jobs = api(run + "/jobs?per_page=100")["jobs"]
    passed = {job["name"] for job in jobs if job["conclusion"] == "success"}
    if required - passed:
        raise ValueError("Required build checks: " + ", ".join(sorted(required - passed)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref")
    parser.add_argument("commit")
    parser.add_argument("--validation-only", action="store_true")
    args = parser.parse_args()
    run_build(args.ref, args.commit, args.validation_only)
