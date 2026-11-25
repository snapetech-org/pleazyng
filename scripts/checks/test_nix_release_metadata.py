import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nix_release_metadata as nix
from release_coverage import REQUIRED


class NixMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / nix.METADATA_PATH
        self.path.parent.mkdir(parents=True)
        self.current = {"repository": nix.REPOSITORY, "tag": "v3.0.7", "version": "3.0.7",
                        "sha256": {"x64": "c" * 64, "arm64": "d" * 64}}
        self.path.write_text(json.dumps(self.current))
        self.commit = "e" * 40
        self.raw = ("a" * 64 + "  pleazyng-linux-x64.tar.gz\n" +
                    "b" * 64 + "  pleazyng-linux-arm64.tar.gz\n").encode()
        self.digest = hashlib.sha256(self.raw).hexdigest()
        names = {p.replace("*", "org.snapetech.pleazyng") for ps in REQUIRED.values() for p in ps}
        names.add("SHA256SUMS.txt")
        self.metadata = {"id": 1, "tag_name": "v3.0.33", "draft": False, "prerelease": False,
                         "published_at": "2026-11-11T00:00:00Z", "assets": [
                             {"id": i, "name": name, "size": 100} for i, name in enumerate(sorted(names), 1)]}
        for asset in self.metadata["assets"]:
            if asset["name"] == "SHA256SUMS.txt":
                asset.update(size=len(self.raw), digest="sha256:" + self.digest)
            elif asset["name"] in nix.ASSETS.values():
                asset["digest"] = "sha256:" + ("a" if "x64" in asset["name"] else "b") * 64
        self.assembly = {"status": "completed", "conclusion": "success", "head_sha": self.commit,
                         "path": ".github/workflows/build.yml", "run_attempt": 1,
                         "repository": {"full_name": nix.REPOSITORY}}
        self.attestation_source = self.commit
        self.source = {"encoding": "base64", "size": len(b"version: 3.0.33+192\n"),
                       "content": base64.b64encode(b"version: 3.0.33+192\n").decode()}
        self.proof = [{"verificationResult": {"statement": {
            "subject": [{"name": "SHA256SUMS.txt", "digest": {"sha256": self.digest}}],
            "predicate": {"buildDefinition": {
                "externalParameters": {"workflow": {"repository": "https://github.com/" + nix.REPOSITORY,
                                                       "path": ".github/workflows/build.yml"}},
                "resolvedDependencies": [{"digest": {"gitCommit": self.commit},
                                           "uri": "git+https://github.com/" + nix.REPOSITORY + "@refs/heads/main"}]},
                "runDetails": {"metadata": {"invocationId":
                    "https://github.com/" + nix.REPOSITORY + "/actions/runs/123/attempts/1"}}}}}}]

    def github(self, path):
        if path.startswith("compare/"):
            return {"status": "ahead"}
        if path.startswith("contents/"):
            return self.source
        if path.endswith("/artifacts?per_page=100"):
            return self.artifact_inventory
        if path.startswith("actions/"):
            return self.assembly
        self.assertEqual(path, "releases/latest")
        return copy.deepcopy(self.metadata)

    def subprocess(self, args, **kwargs):
        if args[1:3] == ["release", "download"]:
            pattern = args[args.index("--pattern") + 1]
            filename = Path(args[args.index("--dir") + 1]) / pattern
            filename.write_bytes(self.recovery_raw if pattern == "platform-reuse-provenance.json" else self.raw)
            return SimpleNamespace(returncode=0)
        self.assertEqual(args[1:3], ["attestation", "verify"])
        self.assertEqual(args[args.index("--source-digest") + 1], self.attestation_source)
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.proof))

    def prepare(self, github=None):
        with patch.object(nix, "require_repository_identity"), patch.object(nix.subprocess, "run", side_effect=self.subprocess):
            return nix.prepare(self.root, self.metadata, self.commit, github or self.github)

    def ordinary_recovery(self):
        workflow = "f" * 40
        source_run = 99
        reused = [
            "Validate release request", "Build tvOS developer package (arm64)", "build-ios",
            "Build Linux (x64)", "Build Linux (arm64)", "Build Windows (x64)",
            "Build Windows (arm64)", "package-windows",
            "Build Samsung Tizen TV package (ARM) / validate",
            "Build Samsung Tizen TV package (ARM) / package",
        ]
        artifacts = [
            {"name": name, "artifact_id": artifact_id, "digest": "sha256:" + digest,
             "source_run_id": source_run, "source_sha": self.commit}
            for name, artifact_id, digest in (
                ("linux-x64", 100, "a" * 64),
                ("linux-arm64", 101, "b" * 64),
            )
        ]
        ci = {"mode": "embedded", "run_id": source_run, "attempt": 1,
              "workflow": ".github/workflows/build.yml", "ci_workflow": ".github/workflows/ci.yml",
              "jobs": [{"id": 1, "name": "Release CI / Unit Tests", "attempt": 1,
                        "status": "completed", "conclusion": "success"}]}
        receipt = {
            "source_run_id": source_run, "source_sha": self.commit,
            "head_sha": workflow, "version": "3.0.33",
            "reused_jobs": reused, "rebuilt_jobs": ["build-macos"],
            "rebuild_linux": False, "rebuild_macos": True,
            "rebuild_tvos": False, "rebuild_ios": False,
            "macos_cask_qualified": False, "platforms_complete": True,
            "pending_jobs": [], "webos_link_compatibility_repaired": False,
            "ci_mode": "embedded", "ci_run_id": source_run,
            "ci_run_url": f"https://github.com/{nix.REPOSITORY}/actions/runs/{source_run}",
            "ci_source_sha": self.commit, "embedded_ci": ci,
            "artifacts": artifacts,
        }
        proof = {key: receipt[key] for key in (
            "source_run_id", "source_sha", "head_sha", "version",
            "reused_jobs", "rebuild_linux", "rebuild_macos", "rebuild_tvos", "rebuild_ios",
            "macos_cask_qualified", "platforms_complete", "pending_jobs",
            "webos_link_compatibility_repaired", "ci_mode", "ci_run_id", "ci_run_url",
            "ci_source_sha", "embedded_ci",
        )}
        inventory = {"total_count": 2, "artifacts": [
            {"id": item["artifact_id"], "name": item["name"], "digest": item["digest"],
             "expired": False,
             "workflow_run": {"id": source_run, "head_sha": self.commit}}
            for item in artifacts
        ]}
        return receipt, proof, inventory, workflow

    def test_exact_source_manifest_and_asset_digests_prepare_only_metadata(self):
        text, receipt = self.prepare()
        self.assertEqual(json.loads(text)["sha256"], {"x64": "a" * 64, "arm64": "b" * 64})
        self.assertEqual(receipt["assembly_run"], 123)
        self.assertEqual(receipt["application_build"], "192")
        self.assertEqual(json.loads(self.path.read_text()), self.current)

    def test_approved_frozen_repair_uses_workflow_attestation_and_product_source(self):
        workflow = "f" * 40
        packaging = "d" * 40
        receipt = {"product_source_sha": self.commit, "source_sha": self.commit,
                   "workflow_source_sha": workflow, "packaging_source_sha": packaging,
                   "actual_checkout_sha": packaging, "source_run_id": 99,
                   "assembly_run_id": 123, "packaging_approval": {"approved": True}}
        receipt_raw = json.dumps(receipt).encode()
        receipt_digest = hashlib.sha256(receipt_raw).hexdigest()
        self.raw += (receipt_digest + "  platform-reuse-provenance.json\n").encode()
        self.digest = hashlib.sha256(self.raw).hexdigest()
        next(a for a in self.metadata["assets"] if a["name"] == "SHA256SUMS.txt").update(
            size=len(self.raw), digest="sha256:" + self.digest)
        self.metadata["assets"].append({"id": 900, "name": "platform-reuse-provenance.json",
                                        "size": len(receipt_raw), "digest": "sha256:" + receipt_digest})
        self.proof[0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = self.digest
        self.proof[0]["verificationResult"]["statement"]["predicate"]["buildDefinition"][
            "resolvedDependencies"][0]["digest"]["gitCommit"] = workflow
        self.assembly["head_sha"] = workflow

        def run(args, **kwargs):
            if args[1:3] == ["release", "download"]:
                pattern = args[args.index("--pattern") + 1]
                (Path(args[args.index("--dir") + 1]) / pattern).write_bytes(
                    receipt_raw if pattern == "platform-reuse-provenance.json" else self.raw)
                return SimpleNamespace(returncode=0)
            self.assertEqual(args[args.index("--source-digest") + 1], workflow)
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.proof))

        with patch.object(nix, "require_repository_identity"), \
             patch.object(nix, "verify_receipt", return_value=receipt["packaging_approval"]) as verify, \
             patch.object(nix.subprocess, "run", side_effect=run):
            _, prepared = nix.prepare(self.root, self.metadata, self.commit, self.github)
        self.assertEqual(prepared["assembly_workflow_source"], workflow)
        verify.assert_called_once()

    def test_ordinary_recovery_reauthenticates_source_ci_artifacts_and_final_attestation(self):
        receipt, source_proof, inventory, workflow = self.ordinary_recovery()
        with patch("recover_release_platforms.verify_source_run", return_value=source_proof) as verify:
            self.assertEqual(nix.verify_ordinary_recovery(
                receipt, self.commit, "v3.0.33", lambda _path: inventory,
            ), workflow)
        verify.assert_called_once_with(
            receipt["source_run_id"], workflow, "3.0.33", nix.REPOSITORY, rebuild_macos=True,
        )

        receipt_raw = json.dumps(receipt).encode()
        receipt_digest = hashlib.sha256(receipt_raw).hexdigest()
        self.recovery_raw = receipt_raw
        self.raw += (receipt_digest + "  platform-reuse-provenance.json\n").encode()
        self.digest = hashlib.sha256(self.raw).hexdigest()
        next(a for a in self.metadata["assets"] if a["name"] == "SHA256SUMS.txt").update(
            size=len(self.raw), digest="sha256:" + self.digest)
        self.metadata["assets"].append({"id": 900, "name": "platform-reuse-provenance.json",
                                        "size": len(receipt_raw), "digest": "sha256:" + receipt_digest})
        self.proof[0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = self.digest
        self.proof[0]["verificationResult"]["statement"]["predicate"]["buildDefinition"][
            "resolvedDependencies"][0]["digest"]["gitCommit"] = workflow
        self.attestation_source = workflow
        self.assembly.update(head_sha=workflow, head_branch="main",
                             head_repository={"full_name": nix.REPOSITORY})
        self.artifact_inventory = inventory
        with patch("recover_release_platforms.verify_source_run", return_value=source_proof):
            _, prepared = self.prepare()
        self.assertEqual(prepared["assembly_workflow_source"], workflow)
        self.assertEqual(prepared["assembly_run"], 123)

    def test_supplemental_recovery_binds_receipt_to_verified_jobs_and_artifact_digests(self):
        proof = {"source_run_id": 99, "qualification_run_id": 100, "head_sha": self.commit,
                 "source_sha": self.commit, "version": "3.0.33", "platforms_complete": True,
                 "rebuilt_jobs": []}
        artifact = {"name": "linux-x64", "artifact_id": 200, "source_run_id": 99,
                    "source_sha": self.commit, "digest": "sha256:" + "a" * 64}
        receipt = {**proof, "artifacts": [{**artifact, "workflow_source_sha": self.commit}]}
        with patch("recover_release_platforms.verify_source_run", return_value=proof) as verify, \
             patch("recover_release_platforms.compose_proof", return_value=proof), \
             patch("recover_release_platforms.preflight_platform_artifacts", return_value=[artifact]):
            self.assertEqual(nix.verify_supplemental_recovery(receipt, self.commit, "v3.0.33"),
                             "action:release:pool:public:tag:v3.0.33:assembly:source:99:qualification:100")
            verify.assert_called_once_with(99, self.commit, "3.0.33", nix.REPOSITORY, qualification_run=100)
            with self.assertRaisesRegex(ValueError, "independently verified candidate"):
                nix.verify_supplemental_recovery({**receipt, "rebuilt_jobs": ["build-ios"]},
                                                 self.commit, "v3.0.33")
            receipt["artifacts"][0]["digest"] = "sha256:" + "b" * 64
            with self.assertRaisesRegex(ValueError, "IDs or digests"):
                nix.verify_supplemental_recovery(receipt, self.commit, "v3.0.33")

    def test_supplemental_recovery_reauthenticates_qualified_assembly_continuation(self):
        source_run, qualification_run = 99, 100
        prior_assembly, final_assembly = 101, 102
        prior_workflow, final_workflow, current_main = "f" * 40, "b" * 40, "c" * 40
        names = {
            "pleazyng-tvos-unsigned.ipa": "1" * 64,
            "pleazyng-windows-x64-portable.7z": "2" * 64,
            "pleazyng-windows-installer.exe": "3" * 64,
            "pleazyng.3.0.33.nupkg": "4" * 64,
        }
        windows_artifacts = {
            "windows-chocolatey": {"artifact_id": 201, "digest": "sha256:" + "a" * 64},
            "windows-installer": {"artifact_id": 202, "digest": "sha256:" + "b" * 64},
            "windows-x64-portable": {"artifact_id": 203, "digest": "sha256:" + "c" * 64},
        }
        proof = {
            "source_run_id": source_run, "source_sha": self.commit, "head_sha": self.commit,
            "version": "3.0.33", "qualification_run_id": qualification_run,
            "platforms_complete": True,
            "qualification_assembly_run_id": prior_assembly, "repackage_windows": False,
            "windows_repackaging": {"assembly_artifacts": windows_artifacts},
            "qualification_assembly_source": {
                "source_run_id": prior_assembly, "source_sha": self.commit,
                "workflow_source_sha": prior_workflow, "run_attempt": 1,
                "version": "3.0.33", "assembly_artifacts": windows_artifacts,
            },
            "tvos_source": {
                "source_run_id": prior_assembly, "source_sha": self.commit,
                "workflow_source_sha": prior_workflow, "run_attempt": 1,
                "version": "3.0.33", "include_apple_store_build": False,
                "runtime_dependency_receipt_required": True,
                "artifact_id": 204, "artifact_digest": "sha256:" + "d" * 64,
            },
        }
        artifacts = [
            {"name": "linux-x64", "artifact_id": 205, "source_run_id": source_run,
             "source_sha": self.commit, "digest": "sha256:" + "e" * 64},
            {"name": "tvos-ipa", "artifact_id": 204, "source_run_id": prior_assembly,
             "source_sha": self.commit, "digest": "sha256:" + "d" * 64},
            *[{
                "name": name, "artifact_id": record["artifact_id"],
                "source_run_id": prior_assembly, "source_sha": self.commit,
                "digest": record["digest"],
            } for name, record in windows_artifacts.items()],
        ]
        receipt_artifacts = [
            {**item, "workflow_source_sha": (
                prior_workflow if item["name"] in {"tvos-ipa", *windows_artifacts} else self.commit)}
            for item in artifacts
        ]
        receipt = {
            **proof,
            "tvos_source": {**proof["tvos_source"], "package_sha256": {
                "pleazyng-tvos-unsigned.ipa": names["pleazyng-tvos-unsigned.ipa"],
            }},
            "qualification_assembly_source": {
                **proof["qualification_assembly_source"],
                "package_sha256": {name: digest for name, digest in names.items()
                                   if name.startswith("pleazyng-windows-") or name.endswith(".nupkg")},
            },
            "assembly_run_id": final_assembly, "actual_checkout_sha": final_workflow,
            "assembly_scope": "qualified-assembly-continuation", "artifacts": receipt_artifacts,
        }
        manifest = "".join(f"{digest}  {name}\n" for name, digest in names.items())
        branch = {"commit": {"sha": current_main}}
        with patch("recover_release_platforms.verify_source_run", return_value=proof) as verify, \
             patch("recover_release_platforms.compose_proof", return_value=proof), \
             patch("recover_release_platforms.preflight_platform_artifacts", return_value=artifacts):
            title = nix.verify_supplemental_recovery(
                receipt, self.commit, "v3.0.33", manifest=manifest,
                github=lambda path: branch if path == "branches/main" else None,
            )
        verify.assert_called_once_with(
            source_run, self.commit, "3.0.33", nix.REPOSITORY,
            qualification_run=qualification_run,
            qualification_assembly_run=prior_assembly, current_main_sha=current_main,
        )
        self.assertEqual(
            title,
            f"action:release:pool:public:tag:v3.0.33:assembly:source:{source_run}:"
            f"qualification:{qualification_run}:assembly:{prior_assembly}",
        )
        nix.verify_supplemental_assembly_identity(receipt, final_assembly, final_workflow)
        with self.assertRaisesRegex(ValueError, "does not bind"):
            nix.verify_supplemental_assembly_identity(receipt, final_assembly + 1, final_workflow)
        with self.assertRaisesRegex(ValueError, "does not bind"):
            nix.verify_supplemental_assembly_identity(receipt, final_assembly, "a" * 40)

        bad_package_hashes = copy.deepcopy(receipt)
        bad_package_hashes["tvos_source"]["package_sha256"]["pleazyng-tvos-unsigned.ipa"] = "9" * 64
        with patch("recover_release_platforms.verify_source_run", return_value=proof), \
             patch("recover_release_platforms.compose_proof", return_value=proof), \
             patch("recover_release_platforms.preflight_platform_artifacts", return_value=artifacts), \
             self.assertRaisesRegex(ValueError, "package hashes differ"):
            nix.verify_supplemental_recovery(
                bad_package_hashes, self.commit, "v3.0.33", manifest=manifest,
                github=lambda path: branch if path == "branches/main" else None,
            )

        bad_workflow = copy.deepcopy(receipt)
        bad_workflow["artifacts"][1]["workflow_source_sha"] = self.commit
        with patch("recover_release_platforms.verify_source_run", return_value=proof), \
             patch("recover_release_platforms.compose_proof", return_value=proof), \
             patch("recover_release_platforms.preflight_platform_artifacts", return_value=artifacts), \
             self.assertRaisesRegex(ValueError, "artifact IDs or digests"):
            nix.verify_supplemental_recovery(
                bad_workflow, self.commit, "v3.0.33", manifest=manifest,
                github=lambda path: branch if path == "branches/main" else None,
            )

    def test_ordinary_recovery_rejects_receipt_source_run_ci_and_artifact_mismatches(self):
        original, source_proof, inventory, workflow = self.ordinary_recovery()
        cases = []

        source = copy.deepcopy(original)
        source["source_sha"] = "d" * 40
        cases.append(("source", source, source_proof, inventory))

        run = copy.deepcopy(original)
        run["source_run_id"] += 1
        cases.append(("run", run, source_proof, inventory))

        ci = copy.deepcopy(original)
        ci["ci_source_sha"] = "d" * 40
        cases.append(("ci", ci, source_proof, inventory))

        artifact = copy.deepcopy(original)
        artifact["artifacts"][0]["digest"] = "sha256:" + "c" * 64
        cases.append(("artifact", artifact, source_proof, inventory))

        for kind, receipt, proof, live in cases:
            with self.subTest(kind=kind), patch(
                    "recover_release_platforms.verify_source_run", return_value=proof):
                with self.assertRaises(ValueError):
                    nix.verify_ordinary_recovery(
                        receipt, self.commit, "v3.0.33", lambda _path: live,
                    )

        with patch("recover_release_platforms.verify_source_run", return_value=source_proof):
            with self.assertRaisesRegex(ValueError, "artifact ID, digest"):
                nix.verify_ordinary_recovery(
                    original, self.commit, "v3.0.33",
                    lambda _path: {**inventory, "artifacts": [
                        {**inventory["artifacts"][0], "id": 999}, inventory["artifacts"][1],
                    ]},
                )

    def test_ordinary_recovery_requires_the_attested_main_assembly_source(self):
        receipt, source_proof, inventory, workflow = self.ordinary_recovery()
        receipt_raw = json.dumps(receipt).encode()
        receipt_digest = hashlib.sha256(receipt_raw).hexdigest()
        self.recovery_raw = receipt_raw
        self.raw += (receipt_digest + "  platform-reuse-provenance.json\n").encode()
        self.digest = hashlib.sha256(self.raw).hexdigest()
        next(a for a in self.metadata["assets"] if a["name"] == "SHA256SUMS.txt").update(
            size=len(self.raw), digest="sha256:" + self.digest)
        self.metadata["assets"].append({"id": 900, "name": "platform-reuse-provenance.json",
                                        "size": len(receipt_raw), "digest": "sha256:" + receipt_digest})
        self.proof[0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = self.digest
        self.proof[0]["verificationResult"]["statement"]["predicate"]["buildDefinition"][
            "resolvedDependencies"][0]["digest"]["gitCommit"] = workflow
        self.attestation_source = workflow
        self.assembly.update(head_sha=workflow, head_branch="other",
                             head_repository={"full_name": nix.REPOSITORY})
        self.artifact_inventory = inventory
        with patch("recover_release_platforms.verify_source_run", return_value=source_proof):
            with self.assertRaisesRegex(ValueError, "ordinary recovery workflow source"):
                self.prepare()


    def test_macos_packaging_recovery_reauthenticates_source_and_artifact_inventory(self):
        import recover_release_platforms

        source_run, macos_run = 441, 442
        workflow = "f" * 40
        artifact_records = [{"name": "linux-x64", "artifact_id": 7,
                             "source_run_id": source_run, "source_sha": self.commit,
                             "digest": "sha256:" + "a" * 64,
                             "workflow_source_sha": self.commit}]
        proof = {
            "profile": "macos-packaging", "source_run_id": source_run,
            "source_sha": self.commit, "head_sha": workflow, "version": "3.0.33",
            "reused_jobs": ["Build Linux (x64)"], "rebuilt_jobs": [],
            "recovered_jobs": ["build-macos"], "platforms_complete": True,
            "pending_jobs": [], "excluded_optional_jobs": [],
            "compiled_macos_job": 9,
            "macos_source": {"source_run_id": macos_run, "source_sha": workflow,
                             "job_id": 8, "compilation_reused": True},
        }
        receipt = {**proof, "artifacts": artifact_records}
        with patch.object(recover_release_platforms, "verify_macos_packaging_recovery",
                          return_value=proof) as verify, \
             patch.object(recover_release_platforms, "preflight_platform_artifacts",
                          return_value=artifact_records) as preflight:
            self.assertEqual(nix.verify_macos_packaging_recovery(
                receipt, self.commit, "v3.0.33"), workflow)
        verify.assert_called_once_with(source_run, macos_run, workflow, "3.0.33", nix.REPOSITORY)
        preflight.assert_called_once_with(proof, nix.REPOSITORY)

        altered = copy.deepcopy(receipt)
        altered["artifacts"][0]["digest"] = "sha256:" + "b" * 64
        with patch.object(recover_release_platforms, "verify_macos_packaging_recovery",
                          return_value=proof), \
             patch.object(recover_release_platforms, "preflight_platform_artifacts",
                          return_value=artifact_records):
            with self.assertRaisesRegex(ValueError, "artifact IDs or digests"):
                nix.verify_macos_packaging_recovery(altered, self.commit, "v3.0.33")

    def test_publication_and_coverage_fail_closed_before_network(self):
        for kind in ("draft", "prerelease", "incomplete"):
            with self.subTest(kind=kind):
                metadata = copy.deepcopy(self.metadata)
                if kind == "incomplete":
                    metadata["assets"] = [a for a in metadata["assets"] if "windows-arm64" not in a["name"]]
                else:
                    metadata[kind] = True
                with self.assertRaises(ValueError):
                    nix.validate_release(metadata)

    def test_old_release_floor_downgrade_same_version_mutation_and_noop(self):
        with self.assertRaisesRegex(ValueError, "floor"):
            nix.proposed_metadata(self.current, "v3.0.32", self.raw.decode())
        proposed = nix.proposed_metadata(self.current, "v3.0.33", self.raw.decode())
        self.assertEqual(nix.proposed_metadata(proposed, "v3.0.33", self.raw.decode()), proposed)
        with self.assertRaisesRegex(ValueError, "same version"):
            nix.proposed_metadata(proposed, "v3.0.33", self.raw.decode().replace("a" * 64, "f" * 64))
        newer = {**proposed, "tag": "v3.0.34", "version": "3.0.34"}
        with self.assertRaisesRegex(ValueError, "downgrade"):
            nix.proposed_metadata(newer, "v3.0.33", self.raw.decode())

    def test_immutable_version_mismatch_and_unmerged_source_rejected(self):
        self.source["content"] = base64.b64encode(b"version: 3.0.31+190\n").decode()
        with self.assertRaisesRegex(ValueError, "immutable application version"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "canonical main"):
            self.prepare(lambda path: {"status": "diverged"})

    def test_statement_source_subject_workflow_and_run_identity_are_bound(self):
        original = copy.deepcopy(self.proof)
        mutations = [lambda s: s["subject"][0]["digest"].update(sha256="f" * 64),
                     lambda s: s["predicate"]["buildDefinition"]["resolvedDependencies"][0]["digest"].update(gitCommit="f" * 40),
                     lambda s: s["predicate"]["buildDefinition"]["externalParameters"]["workflow"].update(path="other.yml"),
                     lambda s: s["predicate"]["runDetails"]["metadata"].update(invocationId="https://example.com/123")]
        for mutate in mutations:
            self.proof = copy.deepcopy(original)
            mutate(self.proof[0]["verificationResult"]["statement"])
            with self.assertRaisesRegex(ValueError, "attestation"):
                self.prepare()

    def test_failed_wrong_source_or_foreign_assembly_and_asset_replacement_rejected(self):
        original = copy.deepcopy(self.assembly)
        for changes in ({"conclusion": "failure"}, {"head_sha": "f" * 40}, {"run_attempt": 2},
                        {"repository": {"full_name": "other/repo"}}):
            self.assembly = {**original, **changes}
            with self.assertRaisesRegex(ValueError, "assembly"):
                self.prepare()
        self.assembly = original
        next(a for a in self.metadata["assets"] if a["name"] == nix.ASSETS["x64"])["digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ValueError, "Linux release asset"):
            self.prepare()

    def test_download_counts_are_ignored_but_asset_changes_are_rejected(self):
        def changed(path):
            result = self.github(path)
            if path == "releases/latest":
                result["assets"][0]["download_count"] = 100
            return result
        self.prepare(changed)
        def replaced(path):
            result = changed(path)
            if path == "releases/latest":
                result["assets"][0]["id"] += 1
            return result
        with self.assertRaisesRegex(ValueError, "changed during"):
            self.prepare(replaced)

    def test_failed_signature_and_malformed_or_duplicate_hashes_never_prepare(self):
        def invalid_signature(args, **kwargs):
            if args[1:3] == ["attestation", "verify"]:
                raise subprocess.CalledProcessError(1, "gh")
            return self.subprocess(args, **kwargs)
        with patch.object(nix, "require_repository_identity"), patch.object(nix.subprocess, "run", side_effect=invalid_signature):
            with self.assertRaises(subprocess.CalledProcessError):
                nix.prepare(self.root, self.metadata, self.commit, self.github)
        for manifest in (self.raw.decode() + self.raw.decode(), self.raw.decode().splitlines()[0],
                         self.raw.decode().replace("a" * 64, "0" * 64)):
            with self.assertRaises(ValueError):
                nix.proposed_metadata(self.current, "v3.0.33", manifest)


if __name__ == "__main__":
    unittest.main()
