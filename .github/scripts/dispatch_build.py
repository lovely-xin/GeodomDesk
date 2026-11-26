"""Dispatch a native GeodomDesk build for an exact commit and verify its jobs."""

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


def run_build(ref, commit, validation, reuse_desktop_run=""):
    repository = os.environ["GITHUB_REPOSITORY"]
    endpoint = f"repos/{repository}/actions"
    inputs = {
        "expected_sha": commit,
        "validation_only": "true" if validation else "false",
    }
    if reuse_desktop_run:
        inputs["reuse_desktop_run"] = reuse_desktop_run
    dispatch = api(
        f"{endpoint}/workflows/build-client.yml/dispatches",
        {"ref": ref, "return_run_details": True, "inputs": inputs},
    )
    run_id = dispatch["workflow_run_id"]
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"run_id={run_id}\n")
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
    if validation:
        required = {"plan", "bridge", "windows", "ubuntu"}
    elif reuse_desktop_run:
        required = {"plan", "bridge", "reused_windows", "android", "release"}
    else:
        required = {"plan", "bridge", "windows", "ubuntu", "android", "release"}
    jobs = api(run + "/jobs?per_page=100")["jobs"]
    passed = {job["name"] for job in jobs if job["conclusion"] == "success"}
    if required - passed:
        raise ValueError("Required build checks: " + ", ".join(sorted(required - passed)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref")
    parser.add_argument("commit")
    parser.add_argument("--validation-only", action="store_true")
    parser.add_argument("--reuse-desktop-run", default="")
    args = parser.parse_args()
    run_build(args.ref, args.commit, args.validation_only, args.reuse_desktop_run)
