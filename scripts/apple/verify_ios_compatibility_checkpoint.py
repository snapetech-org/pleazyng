#!/usr/bin/env python3
"""Verify the exact iOS test producer and its same-run simulator app checkpoint."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import sys
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from verify_embedded_release_ci import fetch_attempt_history  # noqa: E402
from verify_release_ci import GateError  # noqa: E402


BUNDLE_ID = "org.snapetech.pleazyng"
APP_NAME = "Pleazyng.app"


def passing_producer_attempt(run_id, run_attempt, token, producer_names, *, expected_workflow_sha=None,
                             allow_nonpassing=False):
    """Return the latest successful producer attempt from this run's immutable history."""
    if type(run_id) is not int or run_id < 1 or type(run_attempt) is not int or run_attempt < 1:
        raise GateError("The iOS checkpoint run identity is invalid.")
    names = set(producer_names)
    if not names or any(not isinstance(name, str) or not name for name in names):
        raise GateError("At least one exact iOS producer job name is required.")
    if expected_workflow_sha is not None and re.fullmatch(r"[0-9a-f]{40}", expected_workflow_sha) is None:
        raise GateError("The iOS producer workflow SHA is invalid.")

    jobs = fetch_attempt_history(run_id, run_attempt, token)
    producers = [job for job in jobs if job.get("name") in names]
    if len(producers) != 1:
        raise GateError("The current run must contain one uniquely named iOS app producer.")
    producer = producers[0]
    if producer.get("status") != "completed":
        raise GateError("The iOS app producer must complete before compatibility launch.")
    if producer.get("conclusion") != "success":
        if allow_nonpassing:
            return None
        raise GateError("The iOS app producer must complete successfully before compatibility launch.")
    if type(producer.get("run_attempt")) is not int or not 1 <= producer["run_attempt"] <= run_attempt:
        raise GateError("The iOS app producer attempt is invalid.")
    if producer.get("run_id") not in (None, run_id):
        raise GateError("The iOS app producer belongs to a different workflow run.")
    if expected_workflow_sha is not None and producer.get("head_sha") != expected_workflow_sha:
        raise GateError("The iOS app producer belongs to a different workflow source SHA.")
    return producer["run_attempt"]


def verify_checkpoint(artifact_dir, source_sha, run_id, producer_attempt, *, bundle_id=BUNDLE_ID):
    """Validate the uploaded receipt and ZIP before extracting the tested app."""
    artifact_dir = Path(artifact_dir)
    if (re.fullmatch(r"[0-9a-f]{40}", str(source_sha)) is None
            or type(run_id) is not int or run_id < 1
            or type(producer_attempt) is not int or producer_attempt < 1):
        raise GateError("The iOS simulator app checkpoint identity is invalid.")
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        raise GateError("The downloaded iOS simulator app artifact directory is missing or redirected.")
    archive_path = artifact_dir / "ios-simulator-app.zip"
    receipt_path = artifact_dir / "ios-simulator-app.json"
    if any(path.is_symlink() or not path.is_file() for path in (archive_path, receipt_path)):
        raise GateError("The iOS simulator app archive or receipt is missing or redirected.")

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise GateError("The iOS simulator app receipt is malformed.") from None
    if not isinstance(receipt, dict):
        raise GateError("The iOS simulator app receipt is malformed.")
    if receipt.get("product_source_sha") != source_sha:
        raise GateError("The iOS app artifact was built from a different product source SHA.")
    if type(receipt.get("run_id")) is not int or receipt["run_id"] != run_id:
        raise GateError("The iOS app artifact belongs to a different workflow run.")
    if type(receipt.get("run_attempt")) is not int or receipt["run_attempt"] != producer_attempt:
        raise GateError("The iOS app receipt does not match the latest successful producer attempt.")
    if receipt.get("bundle_id") != bundle_id:
        raise GateError("The iOS simulator app artifact has an unexpected bundle ID.")
    expected_digest = receipt.get("sha256")
    if not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        raise GateError("The iOS simulator app archive has an invalid SHA-256 receipt.")

    digest = hashlib.sha256()
    try:
        with archive_path.open("rb") as archive_file:
            for chunk in iter(lambda: archive_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise GateError("The iOS simulator app archive could not be read.") from None
    if digest.hexdigest() != expected_digest:
        raise GateError("The iOS simulator app archive does not match its SHA-256 receipt.")

    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile):
        raise GateError("The iOS simulator app archive is not a valid ZIP file.") from None
    with archive:
        members = archive.infolist()
        member_names = [member.filename for member in members]
        if not members or len(member_names) != len(set(member_names)):
            raise GateError("The iOS simulator app ZIP is empty or has duplicate paths.")
        roots = set()
        for member in members:
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise GateError("The iOS simulator app ZIP contains an unsafe path.")
            roots.add(path.parts[0])
        info_path = f"{APP_NAME}/Info.plist"
        if roots != {APP_NAME} or info_path not in member_names:
            raise GateError("The iOS simulator app ZIP must contain only Pleazyng.app and its Info.plist.")
        try:
            import plistlib

            info = plistlib.loads(archive.read(info_path))
        except (KeyError, ValueError, plistlib.InvalidFileException):
            raise GateError("Pleazyng.app has an invalid Info.plist in the ZIP.") from None
        if info.get("CFBundleIdentifier") != bundle_id:
            raise GateError("Pleazyng.app Info.plist does not match the receipt bundle ID.")
        if not any(name.startswith(f"{APP_NAME}/Frameworks/App.framework/") for name in member_names):
            raise GateError("Pleazyng.app is missing Frameworks/App.framework.")
        for member in members:
            mode = (member.external_attr >> 16) & 0o170000
            if not stat.S_ISLNK(mode):
                continue
            link_target = archive.read(member).decode("utf-8")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(member.filename), link_target))
            if posixpath.isabs(link_target) or not (resolved == APP_NAME or resolved.startswith(APP_NAME + "/")):
                raise GateError("Pleazyng.app contains a ZIP symlink that escapes the app bundle.")
    return APP_NAME


def _append_output(name, value):
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise GateError("GITHUB_OUTPUT is required to publish the iOS checkpoint result.")
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    producer = subparsers.add_parser("producer", help="verify the exact successful iOS app producer")
    producer.add_argument("--run-id", type=int, required=True)
    producer.add_argument("--run-attempt", type=int, required=True)
    producer.add_argument("--job-name", action="append", required=True)
    producer.add_argument("--workflow-sha")
    producer.add_argument("--allow-nonpassing", action="store_true")

    checkpoint = subparsers.add_parser("checkpoint", help="verify the downloaded app and its receipt")
    checkpoint.add_argument("--artifact-dir", type=Path, required=True)
    checkpoint.add_argument("--source-sha", required=True)
    checkpoint.add_argument("--run-id", type=int, required=True)
    checkpoint.add_argument("--producer-attempt", type=int, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "producer":
            token = os.environ.get("GH_TOKEN")
            if not token:
                raise GateError("GH_TOKEN is required to inspect the iOS producer attempt.")
            attempt = passing_producer_attempt(
                args.run_id,
                args.run_attempt,
                token,
                args.job_name,
                expected_workflow_sha=args.workflow_sha,
                allow_nonpassing=args.allow_nonpassing,
            )
            if attempt is None:
                print("The iOS producer did not pass. Its failed job remains the qualification result.")
            else:
                _append_output("attempt", attempt)
        else:
            app_name = verify_checkpoint(
                args.artifact_dir,
                args.source_sha,
                args.run_id,
                args.producer_attempt,
            )
            _append_output("app_name", app_name)
            print(f"Verified the same-run iOS app artifact from producer attempt {args.producer_attempt}.")
    except (GateError, OSError, UnicodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
