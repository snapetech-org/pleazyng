#!/usr/bin/env python3
"""Verify one completed Mac app-build attempt and its immutable workflow artifact."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


REPOSITORY = "snapetech-org/pleazyng"
WORKFLOW_PATH = ".github/workflows/apple-store-screenshots.yml"
MACOS_CAPTURE_TARGETS = {
    "app-in-use-capture-only",
    "macos-in-use-capture-only",
    "macos-in-use-capture-macos26-only",
    "macos-in-use-diagnostic-only",
}
SUCCESSFUL_MAC_BUILD_STEPS = (
    "Verify the exact product source",
    "Build the unsigned Mac App Store candidate",
    "Preserve the compiled unsigned Mac app before capture",
)


class ProducerVerificationError(ValueError):
    """The selected artifact cannot be tied to a completed producer attempt."""


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProducerVerificationError(f"Unable to read {label} JSON: {error}") from error


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProducerVerificationError(f"{label} must be a JSON object")
    return value


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProducerVerificationError(message)


def validate_macos_producer_attempt(
    run: dict[str, Any],
    attempt: dict[str, Any],
    jobs_document: dict[str, Any],
    artifacts_document: dict[str, Any],
    *,
    run_id: str,
    attempt_number: int,
    workflow_id: int,
    artifact_name: str,
    capture_target: str,
    consumer_workflow_sha: str,
    product_source_sha: str,
    product_version: str,
    product_build: str,
) -> dict[str, Any]:
    """Validate an explicit historical attempt without relaxing latest-attempt checks.

    A selected older attempt is accepted only when that attempt itself is complete and
    contains a successful Mac build, app-preservation and artifact-upload sequence.
    The artifact listing must contain one active entry with the matching immutable
    GitHub artifact ID and digest. The later `restore-build` command independently
    checks the artifact's embedded producer run/attempt and app bundle hash.
    """
    _require(capture_target in MACOS_CAPTURE_TARGETS,
             "An explicit Mac producer attempt is valid only for a Mac artifact capture target")
    _require(re.fullmatch(r"[0-9]+", run_id) is not None, "Producer run ID must be numeric")
    _require(_positive_int(attempt_number), "Selected producer attempt must be a positive integer")
    _require(_positive_int(workflow_id), "Expected workflow ID must be a positive integer")
    _require(re.fullmatch(r"[0-9a-f]{40}", consumer_workflow_sha) is not None,
             "Consumer workflow SHA must be a full lowercase Git commit")
    _require(re.fullmatch(r"[0-9a-f]{40}", product_source_sha) is not None,
             "Product source SHA must be a full lowercase Git commit")
    _require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", product_version) is not None,
             "Product version must be a semantic version")
    _require(re.fullmatch(r"[0-9]+", product_build) is not None,
             "Product build must be numeric")

    run = _object(run, "Producer run")
    attempt = _object(attempt, "Selected producer attempt")
    jobs_document = _object(jobs_document, "Selected attempt jobs response")
    artifacts_document = _object(artifacts_document, "Producer artifacts response")
    run_number = int(run_id)

    _require(run.get("id") == run_number, "Producer run response has a different run ID")
    _require(run.get("workflow_id") == workflow_id, "Producer run is from a different workflow")
    _require(run.get("event") == "workflow_dispatch", "Producer must be a workflow_dispatch run")
    _require(run.get("head_branch") == "main", "Producer run must use the main branch")
    run_repository = run.get("repository")
    _require(isinstance(run_repository, dict) and run_repository.get("full_name") == REPOSITORY,
             "Producer run belongs to a different repository")
    _require(re.fullmatch(r"[0-9a-f]{40}", str(run.get("head_sha", ""))) is not None,
             "Producer workflow source SHA is invalid")
    latest_attempt = run.get("run_attempt")
    _require(_positive_int(latest_attempt), "Producer run has an invalid latest attempt number")
    _require(attempt_number <= latest_attempt, "Selected attempt is newer than the producer run")

    _require(attempt.get("id") == run_number, "Attempt response has a different run ID")
    _require(attempt.get("run_attempt") == attempt_number,
             "Attempt response number does not match the selected attempt")
    _require(attempt.get("workflow_id") == workflow_id, "Selected attempt is from a different workflow")
    _require(attempt.get("event") == "workflow_dispatch", "Selected attempt is not a workflow_dispatch")
    _require(attempt.get("head_branch") == "main", "Selected attempt did not run on main")
    _require(attempt.get("head_sha") == run.get("head_sha"),
             "Selected attempt workflow source differs from the producer run")
    _require(attempt.get("status") == "completed", "Selected producer attempt is not completed")
    if attempt_number == latest_attempt:
        _require(run.get("status") == "completed",
                 "The latest producer attempt must still pass the completed-run check")

    jobs = jobs_document.get("jobs")
    _require(isinstance(jobs, list), "Selected attempt jobs response is malformed")
    mac_jobs = [job for job in jobs if isinstance(job, dict) and job.get("name") == "macos"]
    _require(len(mac_jobs) == 1, "Selected attempt must contain exactly one Mac producer job")
    mac_job = mac_jobs[0]
    _require(_positive_int(mac_job.get("id")), "Mac producer job ID is invalid")
    _require(mac_job.get("run_id") == run_number, "Mac producer job belongs to a different run")
    _require(mac_job.get("run_attempt") == attempt_number,
             "Mac producer job belongs to a different run attempt")
    _require(mac_job.get("status") == "completed", "Mac producer job is not completed")
    steps = mac_job.get("steps")
    _require(isinstance(steps, list), "Mac producer job steps are missing")
    for required_name in SUCCESSFUL_MAC_BUILD_STEPS:
        matching = [step for step in steps if isinstance(step, dict) and step.get("name") == required_name]
        _require(len(matching) == 1, f"Expected exactly one producer step named {required_name!r}")
        _require(matching[0].get("conclusion") == "success",
                 f"Mac producer step did not succeed: {required_name}")

    preserve_step = next(step for step in steps if step.get("name") == SUCCESSFUL_MAC_BUILD_STEPS[-1])
    preserve_number = preserve_step.get("number")
    _require(_positive_int(preserve_number), "Mac app-preservation step has an invalid step number")
    upload_steps = [step for step in steps if isinstance(step, dict)
                    and step.get("number") == preserve_number + 1]
    _require(len(upload_steps) == 1
             and isinstance(upload_steps[0].get("name"), str)
             and upload_steps[0]["name"].startswith("Run actions/upload-artifact@")
             and upload_steps[0].get("conclusion") == "success",
             "The artifact-upload step immediately after Mac app preservation did not succeed")

    artifacts = artifacts_document.get("artifacts")
    _require(isinstance(artifacts, list), "Producer artifacts response is malformed")
    matches = [artifact for artifact in artifacts
               if isinstance(artifact, dict)
               and artifact.get("name") == artifact_name
               and artifact.get("expired") is False]
    _require(len(matches) == 1, "Expected exactly one active Mac build artifact for this producer run")
    artifact = matches[0]
    _require(_positive_int(artifact.get("id")), "Mac build artifact ID is invalid")
    digest = artifact.get("digest")
    _require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None,
             "Mac build artifact has no valid SHA-256 digest")
    _require(_positive_int(artifact.get("size_in_bytes")), "Mac build artifact has an invalid size")
    workflow_run = artifact.get("workflow_run")
    _require(isinstance(workflow_run, dict), "Mac build artifact is missing workflow identity")
    _require(workflow_run.get("id") == run_number, "Mac build artifact belongs to a different run")
    _require(workflow_run.get("head_branch") == "main", "Mac build artifact was not produced from main")
    _require(workflow_run.get("head_sha") == run.get("head_sha"),
             "Mac build artifact workflow SHA differs from the producer run")
    repository_id = run_repository.get("id") if isinstance(run_repository, dict) else None
    _require(_positive_int(repository_id)
             and workflow_run.get("repository_id") == repository_id
             and workflow_run.get("head_repository_id") == repository_id,
             "Mac build artifact repository identity does not match the producer repository")

    return {
        "schema_version": 1,
        "purpose": "selected completed Mac app producer attempt and unique GitHub artifact identity",
        "repository": REPOSITORY,
        "workflow_path": WORKFLOW_PATH,
        "workflow_id": workflow_id,
        "producer_run_id": run_number,
        "producer_run_attempt": attempt_number,
        "producer_run_latest_attempt": latest_attempt,
        "selected_attempt_is_latest": attempt_number == latest_attempt,
        "producer_attempt_status": attempt.get("status"),
        "producer_attempt_conclusion": attempt.get("conclusion"),
        "producer_branch": attempt.get("head_branch"),
        "producer_workflow_source_sha": attempt.get("head_sha"),
        "producer_job": {
            "id": mac_job["id"],
            "name": mac_job["name"],
            "status": mac_job.get("status"),
            "conclusion": mac_job.get("conclusion"),
            "successful_build_steps": list(SUCCESSFUL_MAC_BUILD_STEPS),
            "successful_artifact_upload_step": upload_steps[0].get("name"),
            "artifact_upload_step_number": upload_steps[0].get("number"),
        },
        "artifact": {
            "id": artifact["id"],
            "name": artifact["name"],
            "sha256": digest.removeprefix("sha256:"),
            "size_in_bytes": artifact["size_in_bytes"],
            "created_at": artifact.get("created_at"),
            "workflow_run_id": workflow_run.get("id"),
            "workflow_branch": workflow_run.get("head_branch"),
            "workflow_source_sha": workflow_run.get("head_sha"),
        },
        "product": {
            "source_sha": product_source_sha,
            "version": product_version,
            "build": product_build,
        },
        "consumer_workflow_source_sha": consumer_workflow_sha,
    }


def _write_github_output(path: Path, receipt: dict[str, Any], receipt_path: Path) -> None:
    values = {
        "tool_sha": receipt["producer_workflow_source_sha"],
        "run_attempt": str(receipt["producer_run_attempt"]),
        "artifact_id": str(receipt["artifact"]["id"]),
        "artifact_digest": receipt["artifact"]["sha256"],
        "selection_path": str(receipt_path),
    }
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", required=True, type=Path)
    parser.add_argument("--attempt-json", required=True, type=Path)
    parser.add_argument("--jobs-json", required=True, type=Path)
    parser.add_argument("--artifacts-json", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-number", required=True, type=int)
    parser.add_argument("--workflow-id", required=True, type=int)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--capture-target", required=True)
    parser.add_argument("--consumer-workflow-sha", required=True)
    parser.add_argument("--product-source-sha", required=True)
    parser.add_argument("--product-version", required=True)
    parser.add_argument("--product-build", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    arguments = parser.parse_args()

    try:
        if arguments.output.is_symlink() or arguments.output.exists():
            raise ProducerVerificationError(
                f"Refusing to replace producer selection receipt: {arguments.output}"
            )
        run = _read_json(arguments.run_json, "producer run")
        attempt = _read_json(arguments.attempt_json, "producer attempt")
        jobs = _read_json(arguments.jobs_json, "producer attempt jobs")
        artifacts = _read_json(arguments.artifacts_json, "producer artifacts")
        receipt = validate_macos_producer_attempt(
            run,
            attempt,
            jobs,
            artifacts,
            run_id=arguments.run_id,
            attempt_number=arguments.attempt_number,
            workflow_id=arguments.workflow_id,
            artifact_name=arguments.artifact_name,
            capture_target=arguments.capture_target,
            consumer_workflow_sha=arguments.consumer_workflow_sha,
            product_source_sha=arguments.product_source_sha,
            product_version=arguments.product_version,
            product_build=arguments.product_build,
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if arguments.github_output:
            _write_github_output(arguments.github_output, receipt, arguments.output)
        print(
            f"Verified Mac producer run {arguments.run_id} attempt "
            f"{arguments.attempt_number} and artifact {receipt['artifact']['id']}"
        )
    except (OSError, ProducerVerificationError) as error:
        print(f"Mac producer verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
