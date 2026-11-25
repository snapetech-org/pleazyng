"""Retry failed jobs only when GitHub confirms a hosted runner failure."""

import argparse
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta


MAX_ATTEMPTS = 8
MAX_WORKER_DISK_ATTEMPTS = 3
APPLE_QUALIFICATION_WORKFLOW = "apple-candidate-qualification.yml"
APPLE_QUALIFICATION_TITLE = re.compile(
    r"apple-candidate-qualification:([1-9][0-9]*):([0-9a-f]{40})"
    r"(?::resume:([1-9][0-9]*):(all|ios|macos|tvos))?"
)
WORKFLOWS = (APPLE_QUALIFICATION_WORKFLOW, "build.yml", "ci.yml", "macos-app-store-signed.yml")
SUPPLEMENTAL_APPLE_RECOVERY = "Supplemental Apple qualification is already available or running"
APPLE_SUITE_GATE_NAMES = ("Apple native reliability", "Release CI / Apple native reliability")
APPLE_SUITE_PLATFORMS = ("iOS", "macOS", "tvOS")
IOS_NATIVE_PRODUCER = "Build and test candidate iOS app"
WORKER_DISK_ERROR = re.compile(
    r"System\.IO\.IOException: No space left on device : "
    r"'/Users/runner/actions-runner/(?:cached/)?_diag/Worker_[\w.-]+\.log'",
)


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


def interrupted_hosted_ios_suite(job):
    ios_names = {IOS_NATIVE_PRODUCER, *(f"{name} (iOS)" for name in APPLE_SUITE_GATE_NAMES)}
    return (job.get("name") in ios_names
            and job.get("status") == "completed" and job.get("conclusion") == "failure"
            and bool(job.get("runner_id")) and job.get("labels") == ["xcode-27"]
            and not any(step.get("conclusion") == "failure" for step in job["steps"])
            and any(step.get("name") == "Qualify Apple native components"
                    and step.get("status") == "in_progress" for step in job["steps"]))


def retained_siblings_verified(repository, run, retry_platform):
    """Confirm the focused retry passed its retained-sibling preflight."""
    attempt = run.get("run_attempt")
    if type(attempt) is not int or attempt < 1 or retry_platform not in {"all", "ios", "macos", "tvos"}:
        return False
    expected_platforms = {
        "ios": ("iOS",), "macos": ("macOS",), "tvos": ("tvOS",),
        "all": ("iOS", "macOS", "tvOS"),
    }[retry_platform]
    route = f"repos/{repository}/actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100"
    pages = api(route, pages=True)
    if not isinstance(pages, list) or any(
        not isinstance(page, dict) or not isinstance(page.get("jobs"), list) for page in pages
    ):
        return False
    jobs = [job for page in pages for job in page["jobs"] if isinstance(job, dict)]
    for platform in expected_platforms:
        name = f"Apple native reliability ({platform})"
        if platform == "iOS" and any(job.get("name") == IOS_NATIVE_PRODUCER for job in jobs):
            name = IOS_NATIVE_PRODUCER
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1:
            return False
        steps = matches[0].get("steps")
        if not isinstance(steps, list):
            return False
        checks = [step for step in steps if step.get("name") == "Verify retained Apple siblings"]
        if (len(checks) != 1 or checks[0].get("status") != "completed"
                or checks[0].get("conclusion") != "success"):
            return False
    return True


def supplemental_apple_candidate(repository, run):
    if not trusted_workflow(repository, run) or run["path"] != f".github/workflows/{APPLE_QUALIFICATION_WORKFLOW}":
        return None
    return APPLE_QUALIFICATION_TITLE.fullmatch(run["display_title"])


def supplemental_apple_preserves_source(repository, run, candidate):
    return (run["conclusion"] == "success" or run["status"] != "completed"
            or candidate[3] is not None and retained_siblings_verified(repository, run, candidate[4]))


def has_supplemental_apple_recovery(repository, source):
    if source.get("path") != ".github/workflows/build.yml" or not source.get("head_sha") or not source.get("created_at"):
        return False
    path = (f"repos/{repository}/actions/workflows/{APPLE_QUALIFICATION_WORKFLOW}/runs"
            f"?branch=main&event=workflow_dispatch&created=%3E%3D{source['created_at']}&per_page=100")
    for page in api(path, pages=True):
        for run in page["workflow_runs"]:
            candidate = supplemental_apple_candidate(repository, run)
            if (candidate is not None and (int(candidate[1]), candidate[2]) == (source["id"], source["head_sha"])
                    and supplemental_apple_preserves_source(repository, run, candidate)):
                return True
    return False


def classify_attempt(repository, run_id, attempt):
    """Return failed job names only when each root failure is infrastructure."""
    base = f"repos/{repository}"
    route = f"{base}/actions/runs/{run_id}"
    jobs = [job for page in api(f"{route}/attempts/{attempt}/jobs?per_page=30", pages=True)
            for job in page["jobs"] if job["conclusion"] not in {"success", "skipped", "neutral"}]
    if not jobs:
        return [], "No unsuccessful jobs"
    capacity_failures = set()
    suite_gates = []
    for job in jobs:
        if failed_apple_suite_gate(job):
            suite_gates.append(job["name"])
            continue
        unassigned = job["conclusion"] in {"failure", "cancelled"} and unassigned_hosted_job(job)
        interrupted = interrupted_hosted_ios_suite(job)
        if not (unassigned or interrupted):
            return [], f"Requires investigation: {job['name']}"
        annotations = api(f"{base}/check-runs/{job['id']}/annotations?per_page=100", pages=True)
        if interrupted:
            failures = [item["message"] for page in annotations for item in page
                        if item.get("annotation_level") == "failure"]
            if not failures or not all(WORKER_DISK_ERROR.search(message) for message in failures):
                return [], f"No hosted worker disk exhaustion recorded for {job['name']}"
            if attempt >= MAX_WORKER_DISK_ATTEMPTS:
                return [], f"Requires investigation after hosted worker disk exhaustion: {job['name']}"
        elif not any("not acquired by Runner of type hosted" in item["message"]
                     for page in annotations for item in page):
            return [], f"No hosted allocation failure recorded for {job['name']}"
        capacity_failures.add(job["name"])
    # The aggregate runs after the matrix and only tests its result. Its failure
    # is a consequence of an unavailable suite when every other failure above
    # was verified. The same failed-jobs request also reruns this dependency.
    for name in suite_gates:
        if not any(f"{name} ({platform})" in capacity_failures for platform in APPLE_SUITE_PLATFORMS):
            return [], f"No hosted suite allocation failure recorded for {name}"
    return [job["name"] for job in jobs], ""


def recover(repository, run_id, attempt, retry):
    route = f"repos/{repository}/actions/runs/{run_id}"
    run = api(route)
    if not trusted_workflow(repository, run):
        return "Outside trusted release and CI workflows"
    conclusions = {"failure"}
    if run["path"] == f".github/workflows/{APPLE_QUALIFICATION_WORKFLOW}":
        conclusions.add("cancelled")

    def current_failure():
        latest = api(route)
        return (latest["run_attempt"] == attempt and latest["status"] == "completed"
                and latest["conclusion"] in conclusions)

    if retry and not current_failure():
        return "Run has already advanced"
    failed_jobs, reason = classify_attempt(repository, run_id, attempt)
    if not failed_jobs:
        return reason
    names = ", ".join(failed_jobs)
    if attempt >= MAX_ATTEMPTS:
        raise SystemExit(f"Hosted capacity still unavailable after {attempt} attempts: {names}")
    if not retry:
        return f"Confirmed hosted runner failure: {names}"
    if has_supplemental_apple_recovery(repository, run):
        return SUPPLEMENTAL_APPLE_RECOVERY
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
    supplemental_candidates = set()
    for workflow in WORKFLOWS:
        route = f"repos/{repository}/actions/workflows/{workflow}/runs?{query}&per_page=100"
        latest = {}
        for page in api(route, pages=True):
            for run in page["workflow_runs"]:
                title = run["display_title"]
                if title not in latest or run["id"] > latest[title]["id"]:
                    latest[title] = run
        for run in latest.values():
            candidate = None
            if workflow == APPLE_QUALIFICATION_WORKFLOW:
                candidate = supplemental_apple_candidate(repository, run)
                if candidate is not None and supplemental_apple_preserves_source(repository, run, candidate):
                    supplemental_candidates.add((int(candidate[1]), candidate[2]))
            if workflow == "build.yml" and (run["id"], run.get("head_sha")) in supplemental_candidates:
                print(f'{run["id"]}: {SUPPLEMENTAL_APPLE_RECOVERY}')
                continue
            recoverable = (run["conclusion"] == "failure"
                           or candidate is not None and run["conclusion"] == "cancelled")
            if recoverable and run["run_attempt"] < MAX_ATTEMPTS:
                result = recover(repository, run["id"], run["run_attempt"], retry)
                if candidate is not None and (result.startswith("Requested retry")
                                              or result == "Run has already advanced"):
                    supplemental_candidates.add((int(candidate[1]), candidate[2]))
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
