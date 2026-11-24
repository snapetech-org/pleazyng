"""Retry failed jobs only when GitHub confirms a hosted allocation failure."""

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta


MAX_ATTEMPTS = 8
WORKFLOWS = ("build.yml", "ci.yml", "macos-app-store-signed.yml")
APPLE_SUITE_GATE_NAMES = ("Apple native reliability", "Release CI / Apple native reliability")
APPLE_SUITE_PLATFORMS = ("iOS", "macOS", "tvOS")


def api(path, *, pages=False):
    args = ["gh", "api", path]
    if pages:
        args.extend(["--paginate", "--slurp"])
    return json.loads(subprocess.check_output(args))


def trusted_workflow(repository, run):
    return (run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main"
            and (run.get("head_repository") or {}).get("full_name") == repository
            and run.get("path") in {f".github/workflows/{name}" for name in WORKFLOWS})


def unassigned_hosted_job(job):
    return (job.get("runner_id") in (None, 0) and not job.get("runner_name")
            and job.get("steps") == [] and isinstance(job.get("labels"), list)
            and bool(job["labels"]) and "self-hosted" not in job["labels"])


def failed_apple_suite_gate(job):
    return (job["name"] in APPLE_SUITE_GATE_NAMES and job["status"] == "completed"
            and job["conclusion"] == "failure" and job.get("labels") == ["ubuntu-24.04"]
            and [step["name"] for step in job["steps"] if step["conclusion"] == "failure"]
            == ["Require all Apple native suites"])


def classify_attempt(repository, run_id, attempt):
    """Return failed job names only if every failure has hosted allocation evidence."""
    base = f"repos/{repository}"
    route = f"{base}/actions/runs/{run_id}"
    jobs = [job for page in api(f"{route}/attempts/{attempt}/jobs?per_page=100", pages=True)
            for job in page["jobs"] if job["conclusion"] not in {"success", "skipped", "neutral"}]
    if not jobs:
        return [], "No unsuccessful jobs"
    capacity_failures = set()
    suite_gates = []
    for job in jobs:
        if failed_apple_suite_gate(job):
            suite_gates.append(job["name"])
            continue
        if job["conclusion"] not in {"failure", "cancelled"} or not unassigned_hosted_job(job):
            return [], f"Requires investigation: {job['name']}"
        annotations = api(f"{base}/check-runs/{job['id']}/annotations?per_page=100", pages=True)
        if not any("not acquired by Runner of type hosted" in item["message"]
                   for page in annotations for item in page):
            return [], f"No hosted allocation failure recorded for {job['name']}"
        capacity_failures.add(job["name"])
    # The aggregate runs after the matrix and only tests its result. Its failure
    # is a consequence of an unassigned suite when every other failure above
    # was verified. The same failed-jobs request also reruns this dependency.
    for name in suite_gates:
        if not any(f"{name} ({platform})" in capacity_failures for platform in APPLE_SUITE_PLATFORMS):
            return [], f"No hosted suite allocation failure recorded for {name}"
    return [job["name"] for job in jobs], ""


def recover(repository, run_id, attempt, retry):
    route = f"repos/{repository}/actions/runs/{run_id}"
    if not trusted_workflow(repository, api(route)):
        return "Outside trusted release and CI workflows"

    def current_failure():
        latest = api(route)
        return (latest["run_attempt"] == attempt and latest["status"] == "completed"
                and latest["conclusion"] == "failure")

    if retry and not current_failure():
        return "Run has already advanced"
    failed_jobs, reason = classify_attempt(repository, run_id, attempt)
    if not failed_jobs:
        return reason
    names = ", ".join(failed_jobs)
    if attempt >= MAX_ATTEMPTS:
        raise SystemExit(f"Hosted capacity still unavailable after {attempt} attempts: {names}")
    if not retry:
        return f"Confirmed hosted allocation failure: {names}"
    if not current_failure():
        return "Run has already advanced"
    try:
        subprocess.run(["gh", "api", "--method", "POST", f"{route}/rerun-failed-jobs"], check=True)
    except subprocess.CalledProcessError:
        # A concurrent requester can win after our last read, or the POST response can be lost.
        for delay in (0, 3, 6):
            if delay:
                time.sleep(delay)
            if not current_failure():
                return "Run has already advanced"
        raise
    return f"Requested retry for {names}"


def recover_recent(repository, retry):
    """Consider recent failures only, with the same per-attempt checks as manual recovery."""
    cutoff = datetime.now(UTC) - timedelta(hours=12)
    query = f"branch=main&event=workflow_dispatch&created=%3E{cutoff:%Y-%m-%dT%H:%M:%SZ}"
    for workflow in WORKFLOWS:
        route = f"repos/{repository}/actions/workflows/{workflow}/runs?{query}&per_page=100"
        latest = {}
        for page in api(route, pages=True):
            for run in page["workflow_runs"]:
                title = run["display_title"]
                if title not in latest or run["id"] > latest[title]["id"]:
                    latest[title] = run
        for run in latest.values():
            if run["conclusion"] == "failure" and run["run_attempt"] < MAX_ATTEMPTS:
                result = recover(repository, run["id"], run["run_attempt"], retry)
                print(f'{run["id"]}: {result}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=int, nargs="?")
    parser.add_argument("attempt", type=int, nargs="?")
    parser.add_argument("--recent", action="store_true")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    if args.recent == (args.run_id is not None):
        parser.error("choose --recent or supply a run ID")
    repository = os.environ["GITHUB_REPOSITORY"]
    if args.recent:
        recover_recent(repository, args.retry)
    else:
        attempt = args.attempt or api(f"repos/{repository}/actions/runs/{args.run_id}")["run_attempt"]
        print(recover(repository, args.run_id, attempt, args.retry))
