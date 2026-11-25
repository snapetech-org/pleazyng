"""Focused safety checks for direct stable Homebrew recovery."""

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import recover_macos_dmg_for_homebrew_direct as recovery  # noqa: E402


class DirectHomebrewRecoveryTests(unittest.TestCase):
    def source_facts(self):
        source = "a" * 40
        run_id = 45632643040
        attempt = 1
        run = {
            "id": run_id,
            "head_repository": {"full_name": recovery.REPOSITORY},
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/build.yml@refs/heads/main",
            "head_sha": source,
            "run_attempt": attempt,
            "display_title": "action:release:pool:public:tag:v3.0.52",
            "status": "completed",
            "conclusion": "success",
        }
        macos_job = {
            "id": 1001,
            "name": "build-macos",
            "head_sha": source,
            "run_attempt": attempt,
            "status": "completed",
            "conclusion": "success",
            "steps": [{"name": "Build macOS", "conclusion": "success"}],
        }
        release_job = {
            "id": 1002,
            "name": "create-release",
            "head_sha": source,
            "run_attempt": attempt,
            "status": "completed",
            "conclusion": "success",
            "steps": [{"name": "Create Release", "conclusion": "success"}],
        }
        checkpoint = {
            "id": 8001,
            "name": "macos-compiled-app-45632643040-1",
            "expired": False,
            "digest": "sha256:" + "b" * 64,
            "workflow_run": {"id": run_id, "head_sha": source},
        }
        release = {
            "id": 901,
            "tag_name": "v3.0.52",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-11-25T12:00:00Z",
            "assets": [
                {"name": "pleazyng-macos.dmg", "digest": "sha256:" + "c" * 64},
                {"name": "SHA256SUMS.txt", "digest": "sha256:" + "d" * 64},
            ],
        }
        pubspec = b"version: 3.0.52+211\n"
        encoded = base64.b64encode(pubspec).decode()

        def fake_api(path):
            if path == "git/ref/tags/v3.0.52":
                return {"object": {"type": "commit", "sha": source}}
            if path == "actions/runs/45632643040/attempts/1":
                return run
            if path == "actions/runs/45632643040/attempts/1/jobs?per_page=30&page=1":
                return {"jobs": [macos_job, release_job], "total_count": 2}
            if path == "compare/" + source + "..." + source:
                return {"status": "identical"}
            if path == f"contents/pubspec.yaml?ref={source}":
                return {"content": encoded}
            if path == "releases/tags/v3.0.52":
                return release
            if path == "actions/runs/45632643040/artifacts?per_page=100":
                return {"artifacts": [checkpoint]}
            raise AssertionError(path)

        return source, run, macos_job, release_job, checkpoint, release, fake_api

    def test_resolves_only_published_direct_stable_release_and_exact_jobs(self):
        source, *_rest, fake_api = self.source_facts()
        with patch.object(recovery, "api", side_effect=fake_api), patch.dict(
            os.environ, {"GITHUB_SHA": source}, clear=False
        ):
            facts = recovery.verify_direct_source("v3.0.52", 45632643040, 1)
        self.assertEqual(facts["source_sha"], source)
        self.assertEqual(facts["version"], "3.0.52")
        self.assertEqual(facts["app_build"], "211")
        self.assertEqual(facts["macos_job"]["id"], 1001)
        self.assertEqual(facts["release_job"]["id"], 1002)

    def test_rejects_wrong_tag_source_and_unpublished_or_assembled_release(self):
        source, run, macos_job, release_job, checkpoint, release, fake_api = self.source_facts()
        with self.subTest(case="wrong direct run title"):
            with patch.object(recovery, "api", side_effect=lambda path: (
                {**run, "display_title": "action:release:pool:private:tag:v3.0.52"}
                if path == "actions/runs/45632643040/attempts/1" else fake_api(path)
            )), patch.dict(os.environ, {"GITHUB_SHA": source}, clear=False):
                with self.assertRaisesRegex(ValueError, "exact-version public release build"):
                    recovery.verify_direct_source("v3.0.52", 45632643040, 1)
        with self.subTest(case="unpublished release"):
            with patch.object(recovery, "api", side_effect=lambda path: (
                {**release, "draft": True} if path == "releases/tags/v3.0.52" else fake_api(path)
            )), patch.dict(os.environ, {"GITHUB_SHA": source}, clear=False):
                with self.assertRaisesRegex(ValueError, "published stable release"):
                    recovery.verify_direct_source("v3.0.52", 45632643040, 1)
        with self.subTest(case="platform reuse assembly"):
            with patch.object(recovery, "api", side_effect=lambda path: (
                {**release, "assets": [*release["assets"], {"name": "platform-reuse-provenance.json"}]}
                if path == "releases/tags/v3.0.52" else fake_api(path)
            )), patch.dict(os.environ, {"GITHUB_SHA": source}, clear=False):
                with self.assertRaisesRegex(ValueError, "explicit assembly run identity"):
                    recovery.verify_direct_source("v3.0.52", 45632643040, 1)

    def test_resolves_platform_assembly_to_exact_source_checkpoint_and_signed_proof(self):
        source = "a" * 40
        assembly_sha = "e" * 40
        source_run_id = 556
        source_attempt = 2
        assembly_run_id = 889
        assembly_attempt = 1
        source_run = {
            "id": source_run_id,
            "head_repository": {"full_name": recovery.REPOSITORY},
            "head_branch": "main",
            "event": "workflow_dispatch",
            "path": ".github/workflows/build.yml@refs/heads/main",
            "head_sha": source,
            "run_attempt": source_attempt,
            "display_title": "action:release:pool:public:tag:v3.0.52",
            "status": "completed",
            "conclusion": "failure",
        }
        macos_job = {
            "id": 2101, "name": "build-macos", "head_sha": source,
            "run_attempt": source_attempt, "status": "completed", "conclusion": "success",
            "steps": [{"name": "Build macOS", "conclusion": "success"}],
        }
        assembly_run = {
            "id": assembly_run_id,
            "head_repository": {"full_name": recovery.REPOSITORY},
            "head_branch": "main", "event": "workflow_dispatch",
            "path": ".github/workflows/build.yml@refs/heads/main",
            "head_sha": assembly_sha, "run_attempt": assembly_attempt,
            "display_title": "action:release:pool:public:tag:v3.0.52:assembly:source:556:qualification:42:rebuild-tvos",
            "status": "completed", "conclusion": "success",
        }
        release_job = {
            "id": 2201, "name": "create-release", "head_sha": assembly_sha,
            "run_attempt": assembly_attempt, "status": "completed", "conclusion": "success",
            "steps": [{"name": "Create Release", "conclusion": "success"}],
        }
        checkpoint = {
            "id": 2301, "name": f"macos-compiled-app-{source_run_id}-{source_attempt}",
            "expired": False, "digest": "sha256:" + "b" * 64,
            "workflow_run": {"id": source_run_id, "head_sha": source},
        }
        release = {
            "id": 2401, "tag_name": "v3.0.52", "draft": False,
            "prerelease": False, "published_at": "2026-11-25T12:00:00Z",
            "assets": [
                {"name": "pleazyng-macos.dmg", "digest": "sha256:" + "c" * 64},
                {"name": "SHA256SUMS.txt", "digest": "sha256:" + "d" * 64},
                {"name": "platform-reuse-provenance.json", "digest": "sha256:" + "f" * 64},
            ],
        }
        encoded = base64.b64encode(b"version: 3.0.52+211\n").decode()

        def fake_api(path):
            responses = {
                "git/ref/tags/v3.0.52": {"object": {"type": "commit", "sha": source}},
                f"actions/runs/{source_run_id}/attempts/{source_attempt}": source_run,
                f"actions/runs/{source_run_id}/attempts/{source_attempt}/jobs?per_page=30&page=1": {
                    "jobs": [macos_job], "total_count": 1,
                },
                f"actions/runs/{assembly_run_id}/attempts/{assembly_attempt}": assembly_run,
                f"actions/runs/{assembly_run_id}/attempts/{assembly_attempt}/jobs?per_page=30&page=1": {
                    "jobs": [release_job], "total_count": 1,
                },
                f"compare/{source}...{source}": {"status": "identical"},
                f"contents/pubspec.yaml?ref={source}": {"content": encoded},
                "releases/tags/v3.0.52": release,
                f"actions/runs/{source_run_id}/artifacts?per_page=100": {"artifacts": [checkpoint]},
                f"actions/runs/{source_run_id}": {"run_attempt": 3, "head_sha": source},
            }
            if path not in responses:
                raise AssertionError(path)
            return responses[path]

        with patch.object(recovery, "api", side_effect=fake_api), patch.dict(
            os.environ, {"GITHUB_SHA": source}, clear=False
        ):
            facts = recovery.verify_direct_source(
                "v3.0.52", source_run_id, source_attempt,
                assembly_run_id=assembly_run_id, assembly_attempt=assembly_attempt,
            )
            self.assertEqual(facts["run"]["conclusion"], "failure")
            self.assertEqual(facts["macos_job"]["id"], 2101)
            self.assertEqual(facts["core_run"]["id"], assembly_run_id)
            self.assertEqual(facts["release_job"]["id"], 2201)
            self.assertEqual(facts["checkpoint_name"], f"macos-compiled-app-{source_run_id}-{source_attempt}")

            with tempfile.TemporaryDirectory() as temporary:
                proof = Path(temporary) / "platform-reuse-provenance.json"
                proof.write_text(json.dumps({
                    "source_run_id": source_run_id,
                    "source_sha": source,
                    "head_sha": assembly_sha,
                    "run_attempt": 3,
                    "version": "3.0.52",
                    "reused_jobs": ["build-macos"],
                    "macos_cask_qualified": False,
                }))
                recovery.verify_platform_reuse(proof, facts)
                wrong_source = json.loads(proof.read_text())
                wrong_source["source_run_id"] = source_run_id + 1
                proof.write_text(json.dumps(wrong_source))
                with self.assertRaisesRegex(ValueError, "does not bind the exact Mac source"):
                    recovery.verify_platform_reuse(proof, facts)

    def test_build_attestation_must_name_exact_workflow_attempt(self):
        run = {"id": 42, "run_attempt": 3, "head_sha": "a" * 40}
        expected_invocation = "https://github.com/snapetech-org/pleazyng/actions/runs/42/attempts/3"
        attestation = {
            "verificationResult": {"signature": {"certificate": {"runInvocationURI": expected_invocation}}}
        }
        with patch.object(recovery.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps([attestation]))) as command:
            recovery.verify_build_attestation(Path("SHA256SUMS.txt"), run)
        self.assertIn("--signer-workflow", command.call_args.args[0])
        self.assertIn("--signer-digest", command.call_args.args[0])

        wrong = {"verificationResult": {"signature": {"certificate": {
            "runInvocationURI": expected_invocation.replace("attempts/3", "attempts/2")
        }}}}
        with patch.object(recovery.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps([wrong]))):
            with self.assertRaisesRegex(ValueError, "not from the selected release assembly"):
                recovery.verify_build_attestation(Path("SHA256SUMS.txt"), run)

    def test_rejects_non_stable_tag(self):
        for tag in ("v3.0.52-rc.1", "3.0.52", "v3.0.52+211"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                recovery.stable_version(tag)

    def test_upload_appends_only_absent_or_digest_matching_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            contents = {}
            for name in recovery.ASSET_NAMES:
                payload = name.encode()
                (directory / name).write_bytes(payload)
                contents[name] = "sha256:" + hashlib.sha256(payload).hexdigest()
            remote = [{"name": recovery.SIGNED_DMG, "digest": contents[recovery.SIGNED_DMG]}]
            commands = []
            with patch.object(recovery.subprocess, "check_output", return_value=json.dumps({"assets": remote})), \
                 patch.object(recovery.subprocess, "run", side_effect=lambda argv, check: commands.append(argv)):
                missing = recovery.upload("v3.0.52", recovery.REPOSITORY, directory)
            self.assertEqual(missing, [name for name in recovery.ASSET_NAMES if name != recovery.SIGNED_DMG])
            self.assertEqual(len(commands), 1)
            self.assertNotIn("--clobber", commands[0])

            mismatch = [{"name": recovery.SIGNED_DMG, "digest": "sha256:" + "0" * 64}]
            with patch.object(recovery.subprocess, "check_output", return_value=json.dumps({"assets": mismatch})):
                with self.assertRaisesRegex(ValueError, "differs from the preserved"):
                    recovery.upload("v3.0.52", recovery.REPOSITORY, directory)


if __name__ == "__main__":
    unittest.main()
