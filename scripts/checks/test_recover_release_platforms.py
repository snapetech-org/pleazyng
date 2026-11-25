#!/usr/bin/env python3
"""Recovery must prove product equivalence, source checks, and artifact integrity."""

import copy
import base64
import hashlib
import io
import json
import plistlib
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recover_release_platforms as recovery
import release_all_platforms as controller


class ArtifactPreflightTests(unittest.TestCase):
    def setUp(self):
        self.run_id = 77
        self.sha = 'a' * 40
        self.inventory = [
            {'id': index + 1, 'name': name, 'expired': False,
             'digest': 'sha256:' + format(index + 1, '064x'),
             'workflow_run': {'id': self.run_id, 'head_sha': self.sha}}
            for index, name in enumerate(recovery.ARTIFACTS)
        ]
        self.proof = {'source_run_id': self.run_id, 'source_sha': self.sha,
                      'platforms_complete': True}

    def preflight(self, inventory=None, metadata_change=None):
        inventory = self.inventory if inventory is None else inventory
        def metadata(path):
            artifact_id = int(path.rsplit('/', 1)[-1])
            artifact = next(item for item in inventory if item['id'] == artifact_id)
            result = {**artifact, 'workflow_run': dict(artifact['workflow_run'])}
            if metadata_change:
                metadata_change(result)
            return result
        with patch.object(recovery, 'collection', return_value=inventory), \
                patch.object(recovery, 'api', side_effect=metadata):
            return recovery.preflight_platform_artifacts(self.proof)

    def test_all_reused_artifact_metadata_is_bound_before_download(self):
        records = self.preflight()
        self.assertEqual({item['name'] for item in records}, set(recovery.ARTIFACTS))
        self.assertTrue(all(item['source_run_id'] == self.run_id for item in records))

    def test_missing_duplicate_expired_wrong_source_and_malformed_digest_fail_closed(self):
        cases = [self.inventory[1:]]
        cases.append(self.inventory + [dict(self.inventory[0])])
        cases.append([{**self.inventory[0], 'expired': True}, *self.inventory[1:]])
        cases.append([{**self.inventory[0], 'workflow_run': {'id': 88, 'head_sha': self.sha}}, *self.inventory[1:]])
        cases.append([{**self.inventory[0], 'digest': 'sha256:bad'}, *self.inventory[1:]])
        for inventory in cases:
            with self.subTest(first=inventory[0]), self.assertRaises(recovery.RecoveryError):
                self.preflight(inventory)

    def test_tvos_rebuild_preflights_every_original_artifact_except_the_missing_ipa(self):
        proof = {**self.proof, 'rebuild_tvos': True}
        inventory = [item for item in self.inventory if item['name'] != 'tvos-ipa']

        def metadata(path):
            artifact_id = int(path.rsplit('/', 1)[-1])
            artifact = next(item for item in inventory if item['id'] == artifact_id)
            return {**artifact, 'workflow_run': dict(artifact['workflow_run'])}

        with patch.object(recovery, 'collection', return_value=inventory), \
                patch.object(recovery, 'api', side_effect=metadata):
            records = recovery.preflight_platform_artifacts(proof)
        self.assertEqual({item['name'] for item in records}, set(recovery.ARTIFACTS) - {'tvos-ipa'})
        with patch.object(recovery, 'collection', return_value=inventory[1:]), \
                self.assertRaisesRegex(recovery.RecoveryError, 'missing, expired, or ambiguous'):
            recovery.preflight_platform_artifacts(proof)

    def test_metadata_endpoint_must_still_match_the_inventory(self):
        with self.assertRaisesRegex(recovery.RecoveryError, 'metadata changed'):
            self.preflight(metadata_change=lambda item: item.update(digest='sha256:' + 'f' * 64))

    def test_mac_recovery_preflights_dmg_from_recovery_run_and_webos_from_source(self):
        mac_run = 88
        mac_sha = 'b' * 40
        proof = {**self.proof,
                 'webos_source': {'source_run_id': self.run_id, 'source_sha': self.sha},
                 'macos_source': {'source_run_id': mac_run, 'source_sha': mac_sha}}
        mac_artifact = {'id': 900, 'name': 'macos-dmg', 'expired': False,
                        'digest': 'sha256:' + '9' * 64,
                        'workflow_run': {'id': mac_run, 'head_sha': mac_sha}}
        source_inventory = [item for item in self.inventory if item['name'] != 'macos-dmg']
        source_inventory.append({
            'id': 1000, 'name': 'webos-ipk', 'expired': False,
            'digest': 'sha256:' + 'a' * 64,
            'workflow_run': {'id': self.run_id, 'head_sha': self.sha},
        })
        inventories = {self.run_id: source_inventory, mac_run: [mac_artifact]}

        def collection(path, key):
            run_id = int(path.split('/runs/')[1].split('/')[0])
            return inventories[run_id]

        def metadata(path):
            artifact_id = int(path.rsplit('/', 1)[-1])
            artifact = next(item for inventory in inventories.values()
                            for item in inventory if item['id'] == artifact_id)
            return {**artifact, 'workflow_run': dict(artifact['workflow_run'])}

        with patch.object(recovery, 'collection', side_effect=collection), \
                patch.object(recovery, 'api', side_effect=metadata):
            records = recovery.preflight_platform_artifacts(proof)

        by_name = {item['name']: item for item in records}
        self.assertEqual(by_name['macos-dmg']['source_run_id'], mac_run)
        self.assertEqual(by_name['webos-ipk']['source_run_id'], self.run_id)

        profile_proof = {**self.proof, 'profile': 'macos-packaging',
                         'macos_source': {'source_run_id': mac_run, 'source_sha': mac_sha}}
        with patch.object(recovery, 'collection', side_effect=collection), \
                patch.object(recovery, 'api', side_effect=metadata):
            profile_records = recovery.preflight_platform_artifacts(profile_proof)
        profile_by_name = {item['name']: item for item in profile_records}
        self.assertEqual(profile_by_name['webos-ipk']['source_run_id'], self.run_id)

    def test_windows_repackage_preflights_the_original_raw_x64_checkpoint(self):
        inventory = [item for item in self.inventory
                     if item['name'] not in recovery.WINDOWS_REPACKAGED_ARTIFACTS]
        raw = {'id': 900, 'name': recovery.WINDOWS_RAW_X64_ARTIFACT, 'expired': False,
               'digest': 'sha256:' + '9' * 64,
               'workflow_run': {'id': self.run_id, 'head_sha': self.sha}}
        inventory.append(raw)
        proof = {**self.proof, 'repackage_windows': True,
                 'windows_repackaging': {'artifact_id': raw['id'], 'artifact_digest': raw['digest']}}

        def metadata(path):
            artifact_id = int(path.rsplit('/', 1)[-1])
            artifact = next(item for item in inventory if item['id'] == artifact_id)
            return {**artifact, 'workflow_run': dict(artifact['workflow_run'])}

        with patch.object(recovery, 'collection', return_value=inventory), \
                patch.object(recovery, 'api', side_effect=metadata):
            records = recovery.preflight_platform_artifacts(proof)
        names = {item['name'] for item in records}
        self.assertEqual(names, (set(recovery.ARTIFACTS) - recovery.WINDOWS_REPACKAGED_ARTIFACTS)
                         | {recovery.WINDOWS_RAW_X64_ARTIFACT})


class WindowsPackageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.run_id = 55
        self.sha = 'c' * 40
        self.source = {'id': self.run_id, 'head_sha': self.sha, 'run_attempt': 2}
        self.raw = {'id': 900, 'name': recovery.WINDOWS_RAW_X64_ARTIFACT, 'expired': False,
                    'digest': 'sha256:' + '9' * 64,
                    'workflow_run': {'id': self.run_id, 'head_sha': self.sha}}
        package = {'name': 'package-windows', 'id': 101, 'status': 'completed',
                   'conclusion': 'skipped', 'runner_id': 0, 'runner_name': '', 'steps': [],
                   'run_id': self.run_id, 'run_attempt': 2, 'head_sha': self.sha}
        chocolatey = {'name': 'Package Windows Chocolatey', 'id': 102, 'status': 'completed',
                      'conclusion': 'skipped', 'runner_id': 0, 'runner_name': '', 'steps': [],
                      'run_id': self.run_id, 'run_attempt': 2, 'head_sha': self.sha}
        x64 = {'name': 'Build Windows (x64)', 'id': 103, 'status': 'completed',
               'conclusion': 'success', 'run_id': self.run_id, 'run_attempt': 2,
               'head_sha': self.sha, 'steps': [
                   {'name': name, 'status': 'completed', 'conclusion': 'success'}
                   for name in ('Build Windows x64', 'Verify Windows bundle contents', 'Upload x64 build')
               ]}
        self.jobs = [package, chocolatey, x64]

    def verify(self, jobs=None, inventory=None, metadata=None):
        inventory = [self.raw] if inventory is None else inventory
        if metadata is None:
            metadata = lambda _path: {**self.raw, 'workflow_run': dict(self.raw['workflow_run'])}
        with patch.object(recovery, 'collection', return_value=inventory), \
                patch.object(recovery, 'api', side_effect=metadata):
            return recovery.verify_skipped_windows_packaging(self.source, self.jobs if jobs is None else jobs)

    def test_only_exact_unassigned_skip_can_repackage_the_verified_x64_checkpoint(self):
        proof = self.verify()
        self.assertEqual(proof['source_run_id'], self.run_id)
        self.assertEqual(proof['source_x64_build_job_id'], 103)
        self.assertEqual(proof['artifact_id'], self.raw['id'])
        self.assertEqual(proof['artifact_digest'], self.raw['digest'])

    def test_runner_assignment_attempt_drift_or_partial_outputs_reject_repackaging(self):
        for mutate in (
                lambda jobs: jobs[0].update(conclusion='failure'),
                lambda jobs: jobs[0].update(runner_id=123),
                lambda jobs: jobs[0].update(steps=[{'name': 'Set up job'}]),
                lambda jobs: jobs[2].update(run_attempt=1),
        ):
            jobs = copy.deepcopy(self.jobs)
            mutate(jobs)
            with self.subTest(jobs=jobs), self.assertRaises(recovery.RecoveryError):
                self.verify(jobs=jobs)
        with self.assertRaisesRegex(recovery.RecoveryError, 'unexpected original package artifacts'):
            self.verify(inventory=[self.raw, {'name': 'windows-installer'}])

    def test_windows_checkpoint_download_verifies_digest_and_safe_extracts(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('pleazyng.exe', b'compiled application')
            archive.writestr('data/app.so', b'compiled runtime')
        payload = content.getvalue()
        digest = 'sha256:' + hashlib.sha256(payload).hexdigest()
        self.raw['digest'] = digest
        proof = {'repackage_windows': True, 'source_run_id': self.run_id, 'source_sha': self.sha,
                 'windows_repackaging': {'artifact_id': self.raw['id'], 'artifact_digest': digest}}

        def gh_download(_command, **kwargs):
            kwargs['stdout'].write(payload)
            return SimpleNamespace(returncode=0, stderr='')

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(recovery, 'collection', return_value=[self.raw]), \
                patch.object(recovery, 'api', return_value=self.raw), \
                patch.object(recovery.subprocess, 'run', side_effect=gh_download):
            destination = Path(directory) / 'build-x64'
            evidence = recovery.download_windows_build_artifact(proof, destination)
            self.assertEqual(evidence['artifact_id'], self.raw['id'])
            self.assertEqual((destination / 'pleazyng.exe').read_bytes(), b'compiled application')
            self.assertEqual((destination / 'data/app.so').read_bytes(), b'compiled runtime')

    def test_assembly_proof_binds_repackage_jobs_and_outputs_to_current_attempt(self):
        assembly_id = 77
        assembly_sha = 'd' * 40
        title = ('action:release:pool:public:tag:v3.0.52:assembly:source:55:'
                 'qualification:88:rebuild-tvos')
        assembly = {'id': assembly_id, 'display_title': title, 'event': 'workflow_dispatch',
                    'head_branch': 'main', 'head_repository': {'full_name': recovery.REPOSITORY},
                    'path': '.github/workflows/build.yml@refs/heads/main',
                    'head_sha': assembly_sha, 'run_attempt': 2}
        jobs = [
            {'name': 'package-windows', 'id': 701, 'status': 'completed', 'conclusion': 'success',
             'run_id': assembly_id, 'run_attempt': 1, 'head_sha': assembly_sha,
             'started_at': '2026-11-25T00:00:00Z', 'completed_at': '2026-11-25T01:00:00Z', 'steps': [
                 {'name': name, 'status': 'completed', 'conclusion': 'success'}
                 for name in ('Recover verified immutable Windows x64 checkpoint',
                              'Build installer and portables', 'Attest Windows artifacts',
                              'Upload x64 portable', 'Upload installer',
                              'Verify immutable Windows package source')
             ]},
            {'name': 'Package Windows Chocolatey', 'id': 702, 'status': 'completed',
             'conclusion': 'success', 'run_id': assembly_id, 'run_attempt': 1,
             'head_sha': assembly_sha, 'started_at': '2026-11-25T00:00:00Z',
             'completed_at': '2026-11-25T01:00:00Z', 'steps': [
                 {'name': name, 'status': 'completed', 'conclusion': 'success'}
                 for name in ('Package Chocolatey release', 'Attest Chocolatey package',
                              'Upload Chocolatey package')
             ]},
        ]
        artifacts = [
            {'id': index, 'name': name, 'expired': False,
             'digest': 'sha256:' + str(index) * 64,
             'created_at': '2026-11-25T00:30:00Z',
             'workflow_run': {'id': assembly_id, 'head_sha': assembly_sha}}
            for index, name in enumerate(sorted(recovery.WINDOWS_REPACKAGED_ARTIFACTS), start=1)
        ]
        rerun_jobs = [{'name': 'create-release', 'id': 703, 'status': 'in_progress',
                       'conclusion': None, 'run_id': assembly_id, 'run_attempt': 2,
                       'head_sha': assembly_sha}]
        proof = {'source_run_id': 55, 'source_sha': self.sha, 'version': '3.0.52',
                 'qualification_run_id': 88, 'rebuild_tvos': True, 'repackage_windows': True,
                 'windows_repackaging': {'artifact_id': 900, 'artifact_digest': self.raw['digest']}}

        def metadata(path):
            artifact_id = int(path.rsplit('/', 1)[-1])
            artifact = next(item for item in artifacts if item['id'] == artifact_id)
            return {**artifact, 'workflow_run': dict(artifact['workflow_run'])}

        def api_response(path):
            return assembly if path.endswith(f'/actions/runs/{assembly_id}') else metadata(path)

        with patch.dict('os.environ', {'GITHUB_SHA': assembly_sha}), \
                patch.object(recovery, 'api', side_effect=api_response), \
                patch.object(recovery, 'collection', side_effect=[jobs, rerun_jobs, artifacts]):
            evidence = recovery.verify_windows_repackaging_assembly(proof, assembly_id)
        self.assertEqual(evidence['assembly_run_id'], assembly_id)
        self.assertEqual(evidence['assembly_run_attempt'], 2)
        self.assertEqual(evidence['product_checkout_sha'], self.sha)
        self.assertEqual(evidence['package_job_id'], 701)
        self.assertEqual(evidence['chocolatey_job_id'], 702)
        self.assertEqual(set(evidence['assembly_artifacts']), recovery.WINDOWS_REPACKAGED_ARTIFACTS)

        failed = copy.deepcopy(jobs[0])
        failed.update(id=704, status='completed', conclusion='failure', run_attempt=2)
        with patch.dict('os.environ', {'GITHUB_SHA': assembly_sha}), \
                patch.object(recovery, 'api', return_value={**assembly, 'run_attempt': 2}), \
                patch.object(recovery, 'collection', side_effect=[jobs, [failed]]), \
                self.assertRaisesRegex(recovery.RecoveryError, 'did not succeed in the exact assembly attempt'):
            recovery.verify_windows_repackaging_assembly(proof, assembly_id)


class SourceProofTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "head_sha": "a" * 40, "status": "completed", "event": "workflow_dispatch",
            "head_branch": "main", "head_repository": {"full_name": recovery.REPOSITORY},
            "path": ".github/workflows/build.yml",
            "display_title": "action:release:pool:private:tag:v3.0.0",
        }
        self.compare = {"status": "ahead", "merge_base_commit": {"sha": "a" * 40},
                        "files": [{"filename": "scripts/prepare_gradle_repositories.py"}]}
        self.jobs = [{"name": name, "status": "completed", "conclusion": "success"}
                     for name in recovery.REQUIRED_JOBS]
        tvos = next(job for job in self.jobs if job['name'] == 'Build tvOS developer package (arm64)')
        tvos['steps'] = [
            {'name': name, 'status': 'completed', 'conclusion': 'success'}
            for name in ('Package unsigned tvOS developer IPA', 'Verify unsigned tvOS runtime dependencies',
                         'Attest unsigned tvOS checkpoint', 'Upload durable unsigned tvOS checkpoint',
                         'Verify tvOS runtime dependencies',
                         'Attest unsigned tvOS developer package', 'Upload tvOS package')]
        self.jobs += [{"name": "build-android", "status": "completed", "conclusion": "failure"},
                      {"name": "create-release", "status": "completed", "conclusion": "skipped"}]

    def test_infrastructure_recovery_accepts_ci_fixes_already_allowlisted_for_frozen_tags(self):
        paths = {
            "scripts/cache_macos_flutter_sdk.py",
            "scripts/checks/test_cache_macos_flutter_sdk.py",
            "scripts/cleanup_apple_jit_scratch.sh",
            "scripts/clean_appledouble_sidecars.sh",
            "scripts/checks/test_clean_appledouble_sidecars.py",
            "scripts/checks/check_build_workflow.py",
            "scripts/checks/test_recover_release_platforms.py",
            "scripts/checks/test_release_ci_gate.py",
            "scripts/checks/test_release_signing_options.py",
            "AGENTS.md",
            "scripts/tv_mpv/build.py",
            "scripts/checks/test_tv_mpv_build.py",
            "scripts/tv_mpv/video.patch",
            ".github/release-packaging-repairs.json",
            "scripts/apple_jit_process_tree.py",
            "scripts/checks/test_apple_jit_process_tree.py",
            "scripts/checks/test_verify_frozen_packaging_repair.py",
            "scripts/verify_frozen_packaging_repair.py",
            "tools/webos/tests/test_build_release_candidate_dirs.py",
        }
        self.assertTrue(paths <= recovery.RECOVERY_FILES)
        self.assertTrue(paths <= controller.RELEASE_TAG_REUSE_ALLOWLIST)

    def prove(self, *, run_id=42, head_sha="b" * 40, version="3.0.0", **options):
        with patch.object(recovery, "api", side_effect=[self.source, self.compare]), \
             patch.object(recovery, "collection", return_value=self.jobs), \
             patch.object(recovery, "ci_proof", return_value={"ci_run_id": 23}):
            return recovery.verify_source_run(run_id, head_sha, version, **options)

    def test_supplemental_qualification_reuses_only_the_exact_original_outputs(self):
        sha = self.source['head_sha']
        self.source.update(conclusion='failure', run_attempt=3)
        self.compare.update(status='identical', files=[])
        tvos = next(job for job in self.jobs if job['name'] == 'Build tvOS developer package (arm64)')
        tvos.update(run_attempt=2, run_id=42, head_sha=sha)
        self.jobs[-2].update(conclusion='success', id=101)
        self.jobs.extend([
            {'name': 'Build WebOS TV package (ARMv7)', 'id': 102,
             'status': 'completed', 'conclusion': 'success'},
            {'name': 'Release CI / Apple native reliability (iOS)',
             'status': 'completed', 'conclusion': 'failure'},
        ])
        with patch.object(recovery, 'supplemental_ci_proof', return_value={'ci_run_id': 42}) as gate:
            proof = self.prove(head_sha=sha, qualification_run=84, freeze=True)
        gate.assert_called_once_with(recovery.REPOSITORY, sha, '3.0.0', 42, 84, freeze=True)
        with patch.object(recovery, 'collection', return_value=self.jobs):
            assembled = recovery.compose_proof(proof, android_run=42, webos_run=42)
            self.assertEqual(assembled['rebuilt_jobs'], [])
            self.assertEqual(assembled['original_tvos_attempt'], 2)
            self.assertEqual(assembled['android_source']['job_id'], 101)
            self.assertEqual(assembled['webos_source']['job_id'], 102)
            with self.assertRaisesRegex(recovery.RecoveryError, 'original release run'):
                recovery.compose_proof(proof, android_run=74, webos_run=42)
        with self.assertRaisesRegex(recovery.RecoveryError, 'different product revision'):
            self.prove(qualification_run=84)
        with self.assertRaisesRegex(recovery.RecoveryError, 'original unsigned release assets'):
            self.prove(head_sha=sha, qualification_run=84, rebuild_ios=True)

    def test_supplemental_release_auto_selects_only_the_verified_skipped_windows_package_route(self):
        sha = self.source['head_sha']
        self.source.update(id=42, status='completed', conclusion='failure', run_attempt=1,
                           display_title='action:release:pool:public:tag:v3.0.0')
        self.compare.update(status='identical', files=[])
        tvos = next(job for job in self.jobs if job['name'] == 'Build tvOS developer package (arm64)')
        tvos.update(id=100, run_id=42, run_attempt=1, head_sha=sha)
        package = next(job for job in self.jobs if job['name'] == 'package-windows')
        package.update(id=101, status='completed', conclusion='skipped', runner_id=0,
                       runner_name='', steps=[], run_id=42, run_attempt=1, head_sha=sha)
        chocolatey = {'name': 'Package Windows Chocolatey', 'id': 102, 'status': 'completed',
                      'conclusion': 'skipped', 'runner_id': 0, 'runner_name': '', 'steps': [],
                      'run_id': 42, 'run_attempt': 1, 'head_sha': sha}
        x64 = next(job for job in self.jobs if job['name'] == 'Build Windows (x64)')
        x64.update(id=103, run_id=42, run_attempt=1, head_sha=sha, steps=[
            {'name': name, 'status': 'completed', 'conclusion': 'success'}
            for name in ('Build Windows x64', 'Verify Windows bundle contents', 'Upload x64 build')
        ])
        self.jobs.extend([
            chocolatey,
            {'name': 'Build WebOS TV package (ARMv7)', 'id': 104,
             'status': 'completed', 'conclusion': 'success'},
            {'name': 'Release CI / Apple native reliability (iOS)',
             'status': 'completed', 'conclusion': 'failure'},
        ])
        next(job for job in self.jobs if job['name'] == 'build-android').update(
            conclusion='success', id=105)
        raw = {'id': 900, 'name': recovery.WINDOWS_RAW_X64_ARTIFACT, 'expired': False,
               'digest': 'sha256:' + '9' * 64,
               'workflow_run': {'id': 42, 'head_sha': sha}}
        with patch.object(recovery, 'api', side_effect=[self.source, self.compare, raw]), \
                patch.object(recovery, 'collection', side_effect=[self.jobs, [raw]]), \
                patch.object(recovery, 'supplemental_ci_proof', return_value={'ci_run_id': 84}):
            proof = recovery.verify_source_run(42, sha, '3.0.0', qualification_run=84)
        self.assertTrue(proof['repackage_windows'])
        self.assertEqual(proof['windows_repackaging']['artifact_id'], raw['id'])
        self.assertNotIn('package-windows', proof['reused_jobs'])
        self.assertIn('Build Windows (x64)', proof['reused_jobs'])

    def test_supplemental_qualification_does_not_allow_an_unrelated_ci_failure(self):
        sha = self.source['head_sha']
        self.source['run_attempt'] = 3
        self.compare.update(status='identical', files=[])
        tvos = next(job for job in self.jobs if job['name'] == 'Build tvOS developer package (arm64)')
        tvos.update(run_attempt=2, run_id=42, head_sha=sha)
        self.jobs.extend([{'name': 'Release CI / Code Analysis',
                           'status': 'completed', 'conclusion': 'failure'}])
        with patch.object(recovery, 'supplemental_ci_proof', return_value={'ci_run_id': 42}):
            with self.assertRaisesRegex(recovery.RecoveryError, 'Release CI / Code Analysis'):
                self.prove(head_sha=sha, qualification_run=84)

    def test_supplemental_qualification_allows_only_a_terminal_unassigned_tvos_package_rebuild(self):
        sha = self.source['head_sha']
        self.source.update(status='completed', conclusion='failure', run_attempt=1)
        self.compare.update(status='identical', files=[])
        tvos = next(job for job in self.jobs if job['name'] == 'Build tvOS developer package (arm64)')
        tvos.update(status='completed', conclusion='cancelled', labels=['macos-26'],
                    runner_id=0, runner_name='', steps=[], run_id=42, run_attempt=1,
                    head_sha=sha)
        next(job for job in self.jobs if job['name'] == 'build-android').update(conclusion='success', id=101)
        self.jobs.extend([
            {'name': 'Build WebOS TV package (ARMv7)', 'id': 102,
             'status': 'completed', 'conclusion': 'success'},
            {'name': 'Release CI / Apple native reliability (tvOS)',
             'status': 'completed', 'conclusion': 'cancelled'},
        ])
        with patch.object(recovery, 'supplemental_ci_proof', return_value={'ci_run_id': 42}):
            proof = self.prove(head_sha=sha, qualification_run=84, rebuild_tvos=True, freeze=True)
            with patch.object(recovery, 'collection', return_value=self.jobs):
                assembled = recovery.compose_proof(proof, android_run=42, webos_run=42)
        self.assertEqual(assembled['rebuilt_jobs'], ['Build tvOS developer package (arm64)'])
        self.assertTrue(assembled['rebuild_tvos'])
        self.assertEqual(assembled['android_source']['job_id'], 101)
        self.assertEqual(assembled['webos_source']['job_id'], 102)

        tvos.update(status='in_progress', conclusion=None, runner_id=0)
        with patch.object(recovery, 'supplemental_ci_proof', return_value={'ci_run_id': 42}), \
                self.assertRaisesRegex(recovery.RecoveryError, 'cancelled before macos-26 runner acquisition'):
            self.prove(head_sha=sha, qualification_run=84, rebuild_tvos=True, freeze=True)
        tvos.update(status='completed', conclusion='failure', runner_id=1000088520,
                    runner_name='GitHub Actions 1000088520', steps=[{'name': 'Set up job'}])
        with patch.object(recovery, 'supplemental_ci_proof', return_value={'ci_run_id': 42}), \
                self.assertRaisesRegex(recovery.RecoveryError, 'cancelled before macos-26 runner acquisition'):
            self.prove(head_sha=sha, qualification_run=84, rebuild_tvos=True, freeze=True)

    def test_actual_public_candidate_run_shape_is_accepted_for_mac_rebuild(self):
        source_sha = "2388a06cc74d03e69ac7178fcd927a54460a5e3a"
        self.source.update(
            id=43826939425,
            head_sha=source_sha,
            status="completed",
            conclusion="failure",
            display_title="action:release:pool:public:tag:v3.0.36",
        )
        self.compare = {"status": "identical", "merge_base_commit": {"sha": source_sha}, "files": []}
        self.jobs = [
            {"name": name, "status": "completed",
             "conclusion": "failure" if name == "build-macos" else "success"}
            for name in recovery.REQUIRED_JOBS
        ]
        self.jobs.extend([
            {"name": "build-android", "status": "completed", "conclusion": "success"},
            {"name": "Build WebOS TV package (ARMv7)", "status": "completed", "conclusion": "success"},
            {"name": "create-release", "status": "completed", "conclusion": "skipped"},
        ])

        proof = self.prove(
            run_id=43826939425, head_sha=source_sha, version="3.0.36",
            rebuild_macos=True, rebuild_tvos=True,
        )

        self.assertEqual(proof["source_sha"], source_sha)
        self.assertIn("build-macos", proof["rebuilt_jobs"])
        self.assertNotIn("build-macos", proof["reused_jobs"])

    def test_append_only_apple_mode_reuses_release_macos_and_ignores_only_original_mac_failure(self):
        source_sha = "2388a06cc74d03e69ac7178fcd927a54460a5e3a"
        repair_sha = "312df6d14938ce530c6eb2c88ef4d6dda0c7610b"
        workflow_sha = "f9985742e9cb0c72f4c5a7ab7abce40a11828f37"
        self.source.update(
            id=43826939425, head_sha=source_sha, status="completed", conclusion="failure",
            display_title="action:release:pool:public:tag:v3.0.36",
        )
        self.compare = {"status": "identical", "merge_base_commit": {"sha": source_sha}, "files": []}
        self.jobs = [
            {"name": name, "status": "completed",
             "conclusion": "failure" if name == "build-macos" else "success"}
            for name in recovery.REQUIRED_JOBS
        ]
        self.jobs.extend([
            {"name": "build-android", "status": "completed", "conclusion": "success"},
            {"name": "Build WebOS TV package (ARMv7)", "status": "completed", "conclusion": "success"},
            {"name": "create-release", "status": "completed", "conclusion": "skipped"},
        ])
        approval = {
            "recovery_policy": "frozen-apple-packaging-v1",
            "product_source_sha": source_sha,
            "packaging_source_sha": repair_sha,
            "workflow_source_sha": workflow_sha,
            "approval_source_sha": workflow_sha,
            "approved_files": {"scripts/verify_release_attestation.py": "a" * 40},
        }
        with patch("verify_frozen_packaging_repair.verify_frozen_packaging_repair",
                   return_value=approval):
            proof = self.prove(
                run_id=43826939425, head_sha=repair_sha, version="3.0.36",
                rebuild_ios=True, rebuild_tvos=True, packaging_source_sha=repair_sha,
                workflow_source_sha=workflow_sha, include_apple_store_build=True,
                append_apple_only=True,
            )
        self.assertTrue(proof["append_apple_only"])
        self.assertFalse(proof["rebuild_macos"])
        self.assertNotIn("build-macos", proof["rebuilt_jobs"])
        self.assertEqual(proof["reused_release_assets"], ["SHA256SUMS.txt", "pleazyng-macos.dmg"])

    def test_append_only_apple_mode_rejects_non_mac_failure_and_unapproved_use(self):
        with self.assertRaisesRegex(recovery.RecoveryError, "Append-only Apple recovery"):
            self.prove(append_apple_only=True)

        source_sha = "2388a06cc74d03e69ac7178fcd927a54460a5e3a"
        repair_sha = "312df6d14938ce530c6eb2c88ef4d6dda0c7610b"
        workflow_sha = "f9985742e9cb0c72f4c5a7ab7abce40a11828f37"
        self.source.update(
            id=43826939425, head_sha=source_sha, status="completed", conclusion="failure",
            display_title="action:release:pool:public:tag:v3.0.36",
        )
        self.compare = {"status": "identical", "merge_base_commit": {"sha": source_sha}, "files": []}
        self.jobs = [
            {"name": name, "status": "completed",
             "conclusion": "failure" if name in {"build-macos", "Build Linux (x64)"} else "success"}
            for name in recovery.REQUIRED_JOBS
        ]
        self.jobs.extend([
            {"name": "build-android", "status": "completed", "conclusion": "success"},
            {"name": "Build WebOS TV package (ARMv7)", "status": "completed", "conclusion": "success"},
            {"name": "create-release", "status": "completed", "conclusion": "skipped"},
        ])
        with patch("verify_frozen_packaging_repair.verify_frozen_packaging_repair",
                   return_value={"approved_files": {"scripts/file.py": "a" * 40}}):
            with self.assertRaisesRegex(recovery.RecoveryError, "Source platform job is not uniquely successful"):
                self.prove(
                    run_id=43826939425, head_sha=repair_sha, version="3.0.36",
                    rebuild_ios=True, rebuild_tvos=True, packaging_source_sha=repair_sha,
                    workflow_source_sha=workflow_sha, include_apple_store_build=True,
                    append_apple_only=True,
                )

    def test_unexpected_public_title_still_fails_closed(self):
        for title in (
            "action:release:pool:public:tag:v3.0.1",
            "action:release:pool:unknown:tag:v3.0.0",
            "action:release:pool:public:tag:v3.0.0:recovery:source:42",
        ):
            with self.subTest(title=title):
                original = self.source["display_title"]
                self.source["display_title"] = title
                with self.assertRaises(recovery.RecoveryError):
                    self.prove()
                self.source["display_title"] = original

    def test_all_platforms_can_be_recovered_after_only_bootstrap_fix(self):
        self.assertEqual(self.prove()["source_sha"], "a" * 40)

    def test_android_heap_and_tv_mirror_fixes_are_infrastructure_only(self):
        self.compare["files"] = [
            {"filename": path}
            for path in (
                "CHANGELOG.md",
                "android/gradle.properties",
                "scripts/checks/check_build_workflow.py",
                "scripts/prepare_apple_jit_scratch.sh",
                "scripts/tv_mpv/build.py",
                "scripts/tv_mpv/video.patch",
            )
        ]
        self.assertEqual(self.prove()["source_sha"], "a" * 40)

    def test_cancelled_or_timed_out_android_requires_a_new_build(self):
        for conclusion in ("cancelled", "timed_out"):
            with self.subTest(conclusion=conclusion):
                self.jobs[-2]["conclusion"] = conclusion
                proof = recovery.compose_proof(self.prove())
                self.assertIn("build-android", proof["rebuilt_jobs"])
                self.assertNotIn("build-android", proof["reused_jobs"])
                self.assertNotIn("android_source", proof)

    def test_webos_only_failure_keeps_a_successful_android_build_reusable(self):
        self.jobs[-2]["conclusion"] = "success"
        self.jobs.append({"name": "Build WebOS TV package (ARMv7)", "status": "completed", "conclusion": "failure"})
        self.assertEqual(self.prove()["source_sha"], "a" * 40)

    def test_unstarted_cancelled_release_assembly_is_safe_for_platform_recovery(self):
        self.jobs[-1] = {"name": "create-release", "status": "completed", "conclusion": "cancelled",
                         "runner_id": 0, "runner_name": "", "steps": []}
        self.assertEqual(self.prove()["source_sha"], "a" * 40)
        self.jobs[-1]["steps"] = [{"name": "Create release"}]
        with self.assertRaisesRegex(recovery.RecoveryError, "Other source jobs failed"):
            self.prove()

    def test_ci_tag_reuse_requires_complete_product_equivalence(self):
        tag = "c" * 40
        comparison = {"status": "ahead", "merge_base_commit": {"sha": tag}, "files": [{"filename": "scripts/prepare_native_downloads.py"}]}
        for changed in (comparison, {**comparison, "files": [{"filename": "pubspec.lock"}]},
                        {**comparison, "files": [{"filename": "CHANGELOG.md", "previous_filename": "lib/main.dart"}]},
                        {**comparison, "files": comparison["files"] * 300}):
            with self.subTest(changed=changed), patch.object(recovery, "api", side_effect=[{"object": {"type": "commit", "sha": tag}}, changed]):
                if changed is comparison:
                    self.assertEqual(recovery.immutable_ci_source(recovery.REPOSITORY, "a" * 40, "3.0.0"), tag)
                else:
                    with self.assertRaises(recovery.RecoveryError):
                        recovery.immutable_ci_source(recovery.REPOSITORY, "a" * 40, "3.0.0")

    def test_failed_linux_must_be_rebuilt_and_cannot_be_reused(self):
        next(job for job in self.jobs if job["name"] == "Build Linux (x64)")["conclusion"] = "failure"
        with self.assertRaises(recovery.RecoveryError):
            self.prove()
        proof = self.prove(rebuild_linux=True)
        self.assertNotIn("Build Linux (x64)", proof["reused_jobs"])
        self.assertIn("Build Linux (x64)", proof["rebuilt_jobs"])
        self.assertIn("Build Linux (arm64)", proof["rebuilt_jobs"])

    def test_failed_tvos_requires_explicit_rebuild_and_keeps_other_jobs_strict(self):
        name = "Build tvOS developer package (arm64)"
        next(job for job in self.jobs if job["name"] == name)["conclusion"] = "failure"
        with self.assertRaises(recovery.RecoveryError):
            self.prove()
        proof = self.prove(rebuild_tvos=True)
        self.assertTrue(proof["rebuild_tvos"])
        self.assertNotIn(name, proof["reused_jobs"])
        self.assertIn(name, proof["rebuilt_jobs"])
        self.assertIn("build-ios", proof["reused_jobs"])
        next(job for job in self.jobs if job["name"] == "build-ios")["conclusion"] = "failure"
        with self.assertRaises(recovery.RecoveryError):
            self.prove(rebuild_tvos=True)

    def test_failed_tizen_package_can_be_rebuilt_without_reusing_its_artifact(self):
        package = next(job for job in self.jobs if job["name"] == recovery.TIZEN_PACKAGE_JOB)
        package["conclusion"] = "failure"
        self.source["conclusion"] = "failure"
        with self.assertRaises(recovery.RecoveryError):
            self.prove()
        proof = self.prove(rebuild_tizen=True)
        self.assertTrue(proof["rebuild_tizen"])
        self.assertNotIn(recovery.TIZEN_PACKAGE_JOB, proof["reused_jobs"])
        self.assertIn(recovery.TIZEN_PACKAGE_JOB, proof["rebuilt_jobs"])
        validation = next(job for job in self.jobs if job["name"].endswith("/ validate"))
        validation["conclusion"] = "failure"
        with self.assertRaises(recovery.RecoveryError):
            self.prove(rebuild_tizen=True)

    def test_tizen_recovery_can_reuse_qualified_signed_apple_and_notarized_mac_outputs(self):
        package = next(job for job in self.jobs if job["name"] == recovery.TIZEN_PACKAGE_JOB)
        package["conclusion"] = "failure"
        self.source["conclusion"] = "failure"
        ios = next(job for job in self.jobs if job["name"] == "build-ios")
        ios["steps"] = [{"name": "Build signed iOS App Store IPA", "status": "completed",
                          "conclusion": "success"}]
        tvos = next(job for job in self.jobs if job["name"] == "Build tvOS developer package (arm64)")
        tvos["steps"].append({"name": "Build signed tvOS App Store IPA", "status": "completed",
                               "conclusion": "success"})
        macos = next(job for job in self.jobs if job["name"] == "build-macos")
        macos["steps"] = [{"name": name, "status": "completed", "conclusion": "success"}
                          for name in ("Notarize Application", "Wait for notarization acceptance",
                                       "Verify Gatekeeper acceptance for Homebrew Cask")]
        proof = self.prove(rebuild_tizen=True, include_apple_store_build=True,
                           require_homebrew_cask=True)
        self.assertTrue(proof["rebuild_tizen"])
        self.assertTrue(proof["macos_cask_qualified"])
        self.assertIn("build-ios", proof["reused_jobs"])
        self.assertIn("Build tvOS developer package (arm64)", proof["reused_jobs"])
        tvos["steps"] = [step for step in tvos["steps"]
                          if step["name"] != "Build signed tvOS App Store IPA"]
        with self.assertRaisesRegex(recovery.RecoveryError, "signed Store qualification"):
            self.prove(rebuild_tizen=True, include_apple_store_build=True,
                       require_homebrew_cask=True)

    def test_tizen_recovery_cli_accepts_and_verifies_qualified_apple_outputs(self):
        proof = {"source_sha": "a" * 40, "ci_run_id": 44907167387}
        args = ["recover_release_platforms.py", "44907167387", "--head-sha", "a" * 40,
                "--version", "3.0.46", "--allow-building", "--rebuild-tizen",
                "--include-apple-store-build", "--require-homebrew-cask"]
        with patch.object(sys, "argv", args), \
             patch.object(recovery, "verify_source_run", return_value=proof) as verify, \
             patch.object(recovery, "compose_proof", return_value=proof), \
             patch.dict(recovery.os.environ, {"GITHUB_OUTPUT": ""}):
            recovery.main()

        verify.assert_called_once_with(
            "44907167387", "a" * 40, "3.0.46", allow_pending=True,
            rebuild_linux=False, rebuild_macos=False, rebuild_tvos=False, rebuild_ios=False,
            rebuild_tizen=True, repaired_run=None, require_homebrew_cask=True,
            include_apple_store_build=True, ios_developer_run=None,
        )

    def test_reused_tvos_requires_runtime_receipt_production_and_attestation_steps(self):
        tvos = next(job for job in self.jobs if job['name'] == 'Build tvOS developer package (arm64)')
        self.assertTrue(self.prove()['runtime_dependency_receipt_required'])
        original = copy.deepcopy(tvos['steps'])
        for missing in ('Package unsigned tvOS developer IPA', 'Verify unsigned tvOS runtime dependencies',
                        'Attest unsigned tvOS checkpoint', 'Upload durable unsigned tvOS checkpoint',
                        'Verify tvOS runtime dependencies',
                        'Attest unsigned tvOS developer package', 'Upload tvOS package'):
            tvos['steps'] = [step for step in original if step['name'] != missing]
            with self.subTest(step=missing), self.assertRaisesRegex(
                    recovery.RecoveryError, 'runtime dependency qualification'):
                self.prove()
        tvos['steps'] = original

    def test_failed_ios_requires_explicit_rebuild_and_keeps_tvos_strict(self):
        next(job for job in self.jobs if job["name"] == "build-ios")["conclusion"] = "failure"
        with self.assertRaises(recovery.RecoveryError):
            self.prove()
        proof = self.prove(rebuild_ios=True)
        self.assertTrue(proof["rebuild_ios"])
        self.assertNotIn("build-ios", proof["reused_jobs"])
        self.assertIn("build-ios", proof["rebuilt_jobs"])
        next(job for job in self.jobs if job["name"] == "Build tvOS developer package (arm64)")["conclusion"] = "failure"
        with self.assertRaises(recovery.RecoveryError):
            self.prove(rebuild_ios=True)
        proof = self.prove(rebuild_ios=True, rebuild_tvos=True)
        self.assertIn("build-ios", proof["rebuilt_jobs"])
        self.assertIn("Build tvOS developer package (arm64)", proof["rebuilt_jobs"])

    def test_tvos_rebuild_rejects_product_drift_and_repaired_substitution(self):
        self.compare["files"] = [{"filename": "lib/main.dart"}]
        with self.assertRaises(recovery.RecoveryError):
            self.prove(rebuild_tvos=True)
        with self.assertRaises(recovery.RecoveryError):
            self.prove(rebuild_tvos=True, repaired_run=84)

    def test_successful_android_and_webos_reuse_leaves_only_tvos_to_build(self):
        name = "Build tvOS developer package (arm64)"
        next(job for job in self.jobs if job["name"] == name)["conclusion"] = "failure"
        next(job for job in self.jobs if job["name"] == "build-android")["conclusion"] = "success"
        proof = self.prove(rebuild_tvos=True)
        source = {"source_run_id": 42, "source_sha": "a" * 40, "platforms_complete": True}
        with patch.object(recovery, "verify_android_run", return_value=source), \
             patch.object(recovery, "verify_repaired_run", return_value=source):
            final = recovery.compose_proof(proof, android_run=42, webos_run=42)
        self.assertEqual(final["rebuilt_jobs"], [name])

    def test_cask_recovery_requires_the_reused_mac_qualification(self):
        with self.assertRaisesRegex(recovery.RecoveryError, "qualification"):
            self.prove(require_homebrew_cask=True)
        mac = next(job for job in self.jobs if job["name"] == "build-macos")
        mac["steps"] = [{"name": name, "status": "completed", "conclusion": "success"} for name in ("Notarize Application", "Wait for notarization acceptance", "Verify Gatekeeper acceptance for Homebrew Cask")]
        self.assertTrue(self.prove(require_homebrew_cask=True)["macos_cask_qualified"])

    def test_original_failed_linux_and_mac_require_verified_repair_sources(self):
        for job in self.jobs:
            if job["name"] in {"build-macos", "Build Linux (x64)"}:
                job["conclusion"] = "failure"
        repaired = {"source_run_id": 84, "source_sha": "c" * 40,
                    "reused_jobs": ["build-macos", "Build Linux (x64)", "Build Linux (arm64)"],
                    "pending_jobs": [], "platforms_complete": True, "macos_cask_qualified": True}
        with patch.object(recovery, "verify_repaired_run", return_value=repaired):
            proof = self.prove(repaired_run=84, require_homebrew_cask=True)
        self.assertEqual(proof["repaired_source"]["source_run_id"], 84)
        self.assertTrue(proof["macos_cask_qualified"])
        self.assertNotIn("build-macos", proof["rebuilt_jobs"])

    def test_failed_mac_requires_a_fresh_build_and_qualification(self):
        next(job for job in self.jobs if job["name"] == "build-macos")["conclusion"] = "failure"
        with self.assertRaises(recovery.RecoveryError):
            self.prove()
        proof = self.prove(rebuild_macos=True, require_homebrew_cask=True)
        self.assertNotIn("build-macos", proof["reused_jobs"])
        self.assertIn("build-macos", proof["rebuilt_jobs"])
        self.assertFalse(proof["macos_cask_qualified"])

    def test_preflight_allows_running_native_jobs_but_never_a_failure(self):
        self.source["status"] = "queued"
        self.jobs[0].update(status="in_progress", conclusion=None)
        proof = self.prove(allow_pending=True)
        self.assertFalse(proof["platforms_complete"])
        self.assertIn(self.jobs[0]["name"], proof["pending_jobs"])
        with self.assertRaises(recovery.RecoveryError):
            self.prove()
        self.jobs[0].update(status="completed", conclusion="failure")
        with self.assertRaises(recovery.RecoveryError):
            self.prove(allow_pending=True)

    def test_android_may_remain_pending_only_during_preflight(self):
        self.source["status"] = "in_progress"
        next(job for job in self.jobs if job["name"] == "build-android").update(status="in_progress", conclusion=None)
        proof = self.prove(allow_pending=True)
        self.assertIn("build-android", proof["pending_jobs"])
        self.assertFalse(proof["platforms_complete"])
        with self.assertRaises(recovery.RecoveryError):
            self.prove()
        self.source["status"] = "completed"
        with self.assertRaises(recovery.RecoveryError):
            self.prove(allow_pending=True)

    def test_assembly_waits_for_completion_and_has_a_deadline(self):
        with patch.object(recovery, "verify_source_run", side_effect=[
            {"platforms_complete": False, "pending_jobs": ["build-ios"]},
            {"platforms_complete": True},
        ]), patch.object(recovery.time, "sleep"):
            self.assertTrue(recovery.wait_for_source_run(42, "b" * 40, "3.0.0", 60)["platforms_complete"])
        with patch.object(recovery, "verify_source_run", return_value={"platforms_complete": False}), \
             self.assertRaisesRegex(recovery.RecoveryError, "Timed out"):
            recovery.wait_for_source_run(42, "b" * 40, "3.0.0", 0)

    def test_rejects_untrusted_active_or_wrong_version_source(self):
        for change in ({"head_branch": "feature"}, {"status": "in_progress"},
                       {"display_title": "action:release:pool:private:tag:v2.0.0"},
                       {"head_repository": {"full_name": "fork/Pleazyng"}}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.source)
                self.source.update(change)
                with self.assertRaises(recovery.RecoveryError):
                    self.prove()
                self.source = original

    def test_product_changes_and_renames_are_rejected(self):
        for entry in ({"filename": "lib/main.dart"},
                      {"filename": "CHANGELOG.md", "previous_filename": "pubspec.lock"},
                      {"filename": "tools/webos/compatibility.json"}):
            with self.subTest(entry=entry):
                self.compare["files"] = [entry]
                with self.assertRaisesRegex(recovery.RecoveryError, "inputs changed"):
                    self.prove()

    def test_divergent_or_truncated_comparisons_are_rejected(self):
        for change in ({"status": "diverged"}, {"merge_base_commit": {"sha": "c" * 40}},
                       {"files": [{"filename": "CHANGELOG.md"}] * 300}):
            original = copy.deepcopy(self.compare)
            self.compare.update(change)
            with self.assertRaises(recovery.RecoveryError):
                self.prove()
            self.compare = original

    def test_missing_failed_or_ambiguous_platform_is_rejected(self):
        for jobs in (self.jobs[1:], self.jobs + [self.jobs[0]],
                     [{**job, "conclusion": "failure"} if job["name"] == "build-macos" else job
                      for job in self.jobs]):
            with self.subTest(jobs=jobs):
                original, self.jobs = self.jobs, jobs
                with self.assertRaises(recovery.RecoveryError):
                    self.prove()
                self.jobs = original

    def test_source_that_attempted_publication_is_rejected(self):
        self.jobs[-1]["conclusion"] = "success"
        with self.assertRaisesRegex(recovery.RecoveryError, "side effects"):
            self.prove()

    def test_transport_recovery_cannot_change_native_pins_outside_python_bootstrap(self):
        original = "import sys\nprint('verified')\nPDFIUM_PINS = {'asset': 'pinned'}\ndef prepare_webos_python(cache):\n return 1\n"
        loader_fix = original.replace("return 1", "return 2").replace("print('verified')", "print('verified', file=sys.stderr)")
        native_change = loader_fix.replace("'pinned'", "'different'")
        def encoded(code):
            return {"encoding": "base64", "content": base64.b64encode(code.encode()).decode()}
        for replacement in (loader_fix, native_change):
            recovery.immutable_content.cache_clear()  # Each fixture models different immutable commits.
            with self.subTest(replacement=replacement), patch.object(recovery, "api", side_effect=[encoded(original), encoded(replacement)]):
                if replacement == loader_fix:
                    recovery.verify_webos_transport_patch(recovery.REPOSITORY, "a" * 40, "b" * 40, [{"filename": "scripts/prepare_native_downloads.py"}])
                else:
                    with self.assertRaisesRegex(recovery.RecoveryError, "Native download pins"):
                        recovery.verify_webos_transport_patch(recovery.REPOSITORY, "a" * 40, "b" * 40, [{"filename": "scripts/prepare_native_downloads.py"}])

    def test_source_ci_requires_complete_successful_exact_sha_checks(self):
        required = ["Historical exact-source required job"]
        manifest = {"release": {"requiredCiJobs": required, "releaseGate": {
            "branch": "main", "event": "workflow_dispatch", "workflowId": "ci.yml",
            "requiredJobs": required,
        }}}
        with patch.dict("os.environ", {"GH_TOKEN": "test-token"}), \
             patch.object(recovery, "immutable_content", return_value=json.dumps(manifest)) as source_manifest, \
             patch.object(recovery.release_ci, "_latest_run", return_value={"status": "completed", "conclusion": "success"}), \
             patch.object(recovery.release_ci, "_check_jobs", return_value=["Windows arm64: skipped"]) as check_jobs:
            with self.assertRaisesRegex(recovery.RecoveryError, "CI is incomplete"):
                recovery.ci_proof(recovery.REPOSITORY, "a" * 40)
        source_manifest.assert_called_once_with(
            recovery.REPOSITORY, "a" * 40, "docs/maintainers/capability-manifest.json"
        )
        self.assertEqual(check_jobs.call_args.args[3]["requiredJobs"], required)


    def frozen(self, **options):
        self.compare['files'] = [{'filename': 'tools/dv-engine/build_apple_runtime.py'}]
        next(job for job in self.jobs if job['name'] == 'build-android')['conclusion'] = 'success'
        return self.prove(packaging_source_sha='b' * 40, workflow_source_sha='c' * 40,
                          include_apple_store_build=True, **options)

    def test_frozen_exception_requires_independent_exact_approval(self):
        with patch('verify_frozen_packaging_repair.verify_frozen_packaging_repair',
                   side_effect=ValueError('unapproved blob')):
            with self.assertRaisesRegex(recovery.RecoveryError, 'approval failed'):
                self.frozen(rebuild_ios=True, rebuild_tvos=True)
        with patch('verify_frozen_packaging_repair.verify_frozen_packaging_repair',
                   return_value={'approved_files': {'reviewed': 'blob'}}) as verify:
            proof = self.frozen(rebuild_ios=True, rebuild_tvos=True)
        self.assertEqual(proof['product_source_sha'], 'a' * 40)
        self.assertEqual(proof['packaging_source_sha'], 'b' * 40)
        self.assertEqual(proof['workflow_source_sha'], 'c' * 40)
        verify.assert_called_once()
        with self.assertRaisesRegex(recovery.RecoveryError, 'inputs changed'):
            self.prove()

    def test_frozen_reuse_requires_actual_signed_store_steps(self):
        with patch('verify_frozen_packaging_repair.verify_frozen_packaging_repair', return_value={}):
            with self.assertRaisesRegex(recovery.RecoveryError, 'signed Store qualification'):
                self.frozen()
            next(job for job in self.jobs if job['name'] == 'build-ios')['steps'] = [
                {'name': 'Build signed iOS App Store IPA', 'conclusion': 'success', 'status': 'completed'}]
            tvos_job = next(job for job in self.jobs if job['name'] == 'Build tvOS developer package (arm64)')
            tvos_job['steps'] = [
                {'name': step, 'conclusion': 'success', 'status': 'completed'}
                for step in ('Build signed tvOS App Store IPA', 'Package unsigned tvOS developer IPA',
                             'Attest unsigned tvOS developer package')]
            with self.assertRaisesRegex(recovery.RecoveryError, 'Verify unsigned tvOS runtime dependencies'):
                self.frozen()
            tvos_job['steps'] = [
                {'name': step, 'conclusion': 'success', 'status': 'completed'}
                for step in ('Build signed tvOS App Store IPA', 'Package unsigned tvOS developer IPA',
                             'Verify unsigned tvOS runtime dependencies', 'Attest unsigned tvOS checkpoint',
                             'Upload durable unsigned tvOS checkpoint', 'Attest unsigned tvOS developer package',
                             'Verify tvOS runtime dependencies',
                             'Upload tvOS package')]
            proof = self.frozen()
            self.assertTrue(proof['include_apple_store_build'])
            self.assertTrue(proof['runtime_dependency_receipt_required'])
            with self.assertRaisesRegex(recovery.RecoveryError, 'full signed Apple builds'):
                self.frozen(rebuild_linux=True)

    def test_frozen_aliases_keep_original_successful_android_and_webos_only(self):
        proof = {'recovery_policy': 'frozen-apple-packaging-v1', 'source_run_id': 42,
                 'source_sha': 'a' * 40, 'rebuilt_jobs': list(recovery.REBUILT_JOBS)}
        jobs = [{'id': i, 'name': name, 'status': 'completed', 'conclusion': 'success'}
                for i, name in enumerate(['build-android', 'Build WebOS TV package (ARMv7)'], 1)]
        with patch.object(recovery, 'collection', return_value=jobs):
            result = recovery.compose_proof(proof, android_run=42, webos_run=42)
            self.assertEqual(result['android_source']['source_sha'], 'a' * 40)
            self.assertEqual(result['webos_source']['job_id'], 2)
            self.assertNotIn('build-android', result['rebuilt_jobs'])
            for options in ({'android_run': 43, 'webos_run': 42},
                            {'android_run': 42, 'webos_run': 43},
                            {'android_run': 42, 'webos_run': 42, 'include_webos_build': False}):
                with self.assertRaises(recovery.RecoveryError):
                    recovery.compose_proof(proof, **options)
            jobs[0]['conclusion'] = 'failure'
            with self.assertRaisesRegex(recovery.RecoveryError, 'not uniquely successful'):
                recovery.compose_proof(proof, android_run=42, webos_run=42)

    def test_frozen_repair_rebuilds_webos_without_aliasing_the_failed_source(self):
        proof = {'recovery_policy': 'frozen-apple-packaging-v1', 'source_run_id': 42,
                 'source_sha': 'a' * 40, 'rebuilt_jobs': list(recovery.REBUILT_JOBS)}
        jobs = [{'id': 7, 'name': 'build-android', 'status': 'completed', 'conclusion': 'success'},
                {'id': 8, 'name': 'Build WebOS TV package (ARMv7)',
                 'status': 'completed', 'conclusion': 'failure'}]
        with patch.object(recovery, 'collection', return_value=jobs):
            result = recovery.compose_proof(proof, android_run=42, webos_run=None)
        self.assertEqual(result['android_source']['job_id'], 7)
        self.assertNotIn('webos_source', result)
        self.assertIn('Build WebOS TV package (ARMv7)', result['rebuilt_jobs'])

    def test_tv_sibling_reuse_replaces_original_failed_tv_without_rebuilding_it(self):
        tv = {'source_run_id': 84, 'source_sha': 'd' * 40, 'workflow_source_sha': 'e' * 40}
        with patch('verify_frozen_packaging_repair.verify_frozen_packaging_repair', return_value={'approved': True}), \
                patch.object(recovery, 'verify_frozen_tvos_run', return_value=tv) as reused:
            proof = self.frozen(rebuild_ios=True, tvos_run=84)
        self.assertEqual(proof['tvos_source'], tv)
        self.assertFalse(proof['rebuild_tvos'])
        self.assertNotIn('Build tvOS developer package (arm64)', proof['rebuilt_jobs'])
        self.assertIn('Build tvOS developer package (arm64)', proof['reused_jobs'])
        reused.assert_called_once()

    def test_two_signed_apple_siblings_replace_both_failed_original_jobs(self):
        tv = {'source_run_id': 84, 'source_sha': 'd' * 40}
        ios = {'source_run_id': 85, 'source_sha': 'e' * 40}
        with patch('verify_frozen_packaging_repair.verify_frozen_packaging_repair', return_value={'approved': True}), \
                patch.object(recovery, 'verify_frozen_tvos_run', return_value=tv), \
                patch.object(recovery, 'verify_frozen_ios_run', return_value=ios):
            proof = self.frozen(tvos_run=84, ios_run=85)
        self.assertEqual(proof['tvos_source'], tv)
        self.assertEqual(proof['ios_source'], ios)
        self.assertFalse(proof['rebuild_ios'])
        self.assertFalse(proof['rebuild_tvos'])
        self.assertNotIn('build-ios', proof['rebuilt_jobs'])
        self.assertNotIn('Build tvOS developer package (arm64)', proof['rebuilt_jobs'])

    def test_one_combined_signed_run_may_supply_one_artifact_for_each_apple_platform(self):
        tv = {'source_run_id': 84, 'source_sha': 'd' * 40, 'artifact_id': 7}
        ios = {'source_run_id': 84, 'source_sha': 'd' * 40, 'artifact_id': 8}
        combined = {'tvos_source': tv, 'ios_source': ios}
        with patch('verify_frozen_packaging_repair.verify_frozen_packaging_repair', return_value={'approved': True}), \
                patch.object(recovery, 'verify_frozen_apple_run', return_value=combined) as verify:
            proof = self.frozen(tvos_run=84, ios_run=84)
        self.assertEqual(proof['tvos_source'], tv)
        self.assertEqual(proof['ios_source'], ios)
        self.assertEqual(verify.call_args.args[0], 84)


class FrozenTvosReuseTests(unittest.TestCase):
    def setUp(self):
        self.product, self.repair, self.prior_repair, self.workflow = ('a' * 40, 'b' * 40, 'd' * 40, 'e' * 40)
        self.proof = {'source_run_id': 42, 'product_source_sha': self.product,
                      'packaging_source_sha': self.repair, 'version': '3.0.31'}
        self.run = {'id': 84, 'head_sha': self.workflow, 'status': 'completed', 'conclusion': 'failure',
                    'event': 'workflow_dispatch', 'head_branch': 'main', 'path': '.github/workflows/build.yml',
                    'repository': {'id': 1433487464}, 'head_repository': {'full_name': recovery.REPOSITORY},
                    'run_attempt': 1,
                    'display_title': 'action:release:pool:public:tag:v3.0.31:repair:' + self.prior_repair}
        def job(name, conclusion, steps=()):
            value = {'id': len(name), 'name': name, 'status': 'completed', 'conclusion': conclusion,
                    'steps': [{'name': step, 'status': 'completed', 'conclusion': 'success'} for step in steps]}
            if name == 'Build tvOS developer package (arm64)':
                value.update(runner_id=100, runner_name='GitHub Actions 100',
                             runner_group_name='GitHub Actions', labels=['macos-26'])
            return value
        self.jobs = [job('Validate release request', 'success', (
            'Verify approved frozen packaging checkout', 'Verify reusable platform source')),
            job('Build tvOS developer package (arm64)', 'success', (
                'Build tvOS app', 'Package unsigned tvOS developer IPA', 'Build signed tvOS App Store IPA',
                'Verify tvOS runtime dependencies', 'Attest unsigned tvOS developer package',
                'Attest signed tvOS App Store package', 'Upload tvOS package')),
            job('build-ios', 'failure'), job('create-release', 'skipped'), job('build-macos', 'skipped')]
        self.artifact = {'id': 7, 'name': 'tvos-ipa', 'expired': False, 'digest': 'sha256:' + 'f' * 64,
                         'workflow_run': {'id': 84, 'head_sha': self.workflow}}
        self.prior = {'source_sha': self.product, 'platforms_complete': True, 'packaging_approval': {'exact': True}}

    def prove(self):
        with patch.object(recovery, 'api', side_effect=[self.run, self.artifact]), \
                patch.object(recovery, 'collection', side_effect=[self.jobs, [self.artifact]]), \
                patch.object(recovery, 'verify_source_run', return_value=self.prior) as source:
            result = recovery.verify_frozen_tvos_run(84, self.proof)
        self.assertEqual(source.call_args.args[:2], (42, self.prior_repair))
        self.assertEqual(source.call_args.kwargs['workflow_source_sha'], self.workflow)
        self.assertTrue(source.call_args.kwargs['include_apple_store_build'])
        return result

    def test_signed_tv_uses_actual_packaging_source_but_workflow_artifact_identity(self):
        result = self.prove()
        self.assertEqual(result['source_sha'], self.prior_repair)
        self.assertEqual(result['workflow_source_sha'], self.workflow)
        self.assertEqual(result['artifact_id'], 7)
        self.assertEqual(result['artifact_digest'], self.artifact['digest'])
        self.assertEqual(result['packaging_approval'], self.prior['packaging_approval'])
        self.assertEqual(result['packaging_source_sha'], self.prior_repair)
        self.assertEqual(result['version'], '3.0.31')
        self.assertTrue(result['runtime_dependency_receipt_required'])

    def test_explicit_private_apple_route_requires_run_and_job_bound_labels(self):
        self.jobs[1].update(
            runner_id=101,
            runner_name='pleazyng-private-apple-101',
            runner_group_name='private-apple-builds',
            labels=['self-hosted', 'macOS', 'ARM64', 'private-apple',
                    'ng-shared-main-pleazyng', 'ng-main-run-84', 'gha-84-build-tvos'],
        )
        self.prove()
        self.jobs[1]['labels'].remove('gha-84-build-tvos')
        with self.assertRaisesRegex(recovery.RecoveryError, 'expected public hosted or exact private'):
            self.prove()

    def test_tvos_only_sibling_can_fail_assembly_before_publication(self):
        self.jobs[2]['conclusion'] = 'skipped'
        self.jobs[3]['conclusion'] = 'failure'
        self.jobs[3]['steps'] = [
            {'name': 'Recover verified platform artifacts', 'status': 'completed', 'conclusion': 'failure'},
            {'name': 'Create Release', 'status': 'completed', 'conclusion': 'skipped'},
        ]
        self.prove()
        self.jobs[3]['steps'][1]['conclusion'] = 'success'
        with self.assertRaisesRegex(recovery.RecoveryError, 'before publication'):
            self.prove()

    def test_untrusted_wrong_product_unsigned_and_publication_attempt_sources_are_rejected(self):
        original = {key: copy.deepcopy(getattr(self, key)) for key in ('run', 'jobs', 'artifact', 'prior')}
        for change in ('foreign', 'wrong-product', 'failed-ci', 'missing-signed', 'missing-attestation', 'missing-runtime',
                       'publication', 'unrelated-built', 'duplicate-tv', 'expired', 'bad-artifact-source',
                       'wrong-runner-route', 'private-run-title'):
            self.__dict__.update(copy.deepcopy(original))
            if change == 'foreign':
                self.run['head_repository']['full_name'] = 'other/project'
            elif change == 'wrong-product':
                self.prior['source_sha'] = 'c' * 40
            elif change == 'failed-ci':
                self.prior['platforms_complete'] = False
            elif change in ('missing-signed', 'missing-attestation', 'missing-runtime'):
                target_step = {
                    'missing-signed': 'Build signed tvOS App Store IPA',
                    'missing-attestation': 'Attest signed tvOS App Store package',
                    'missing-runtime': 'Verify tvOS runtime dependencies',
                }[change]
                next(step for step in self.jobs[1]['steps'] if step['name'] == target_step)['conclusion'] = 'skipped'
            elif change == 'publication':
                self.jobs[3]['conclusion'] = 'failure'
            elif change == 'unrelated-built':
                self.jobs[4]['conclusion'] = 'success'
            elif change == 'duplicate-tv':
                self.jobs.append(copy.deepcopy(self.jobs[1]))
            elif change == 'expired':
                self.artifact['expired'] = True
            elif change == 'bad-artifact-source':
                self.artifact['workflow_run']['head_sha'] = self.prior_repair
            elif change == 'wrong-runner-route':
                self.jobs[1]['labels'] = ['macos-26-intel']
            elif change == 'private-run-title':
                self.run['display_title'] = self.run['display_title'].replace('pool:public', 'pool:private')
            with self.subTest(change=change), self.assertRaises(recovery.RecoveryError):
                self.prove()

    def test_tv_reuse_never_bypasses_independent_original_product_approval(self):
        with patch.object(recovery, 'api', return_value=self.run), \
                patch.object(recovery, 'verify_source_run', side_effect=recovery.RecoveryError('unapproved')):
            with self.assertRaisesRegex(recovery.RecoveryError, 'unapproved'):
                recovery.verify_frozen_tvos_run(84, self.proof)
        with self.assertRaisesRegex(recovery.RecoveryError, 'iOS-only'):
            recovery.verify_source_run(42, self.repair, '3.0.31', tvos_run=84)

    def test_both_original_tv_package_certificates_must_bind_workflow_and_run(self):
        source = {'source_run_id': 84, 'workflow_source_sha': self.workflow, 'run_attempt': 1}
        certificate = {'sourceRepositoryIdentifier': '1433487464', 'sourceRepositoryDigest': self.workflow,
                       'runInvocationURI': 'https://github.com/' + recovery.REPOSITORY + '/actions/runs/84/attempts/1'}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ('pleazyng-tvos-unsigned.ipa', 'pleazyng-tvos-appstore.ipa'):
                (directory / name).write_bytes(name.encode())
            for change in (None, {'sourceRepositoryDigest': self.prior_repair},
                           {'runInvocationURI': 'https://github.com/' + recovery.REPOSITORY + '/actions/runs/85'}):
                cert = {**certificate, **(change or {})}
                verified = SimpleNamespace(stdout=json.dumps([{'verificationResult': {
                    'signature': {'certificate': cert}}}]))
                with self.subTest(change=change), patch.object(recovery.subprocess, 'run', return_value=verified) as verify:
                    if change:
                        with self.assertRaisesRegex(recovery.RecoveryError, 'attestation'):
                            recovery.verify_tvos_package_attestations(directory, source)
                    else:
                        digests = recovery.verify_tvos_package_attestations(directory, source)
                        self.assertEqual(len(digests), 2)
                        self.assertEqual(verify.call_count, 2)
                        for name, digest in digests.items():
                            self.assertEqual(digest, hashlib.sha256(name.encode()).hexdigest())


class CompletedTvosAssemblyReuseTests(unittest.TestCase):
    def setUp(self):
        self.product, self.packaging, self.producer_workflow, self.current_workflow = (
            'a' * 40, 'b' * 40, 'd' * 40, 'e' * 40)
        self.approval = {'approved_files': {'scripts/recover_release_platforms.py': 'f' * 40},
                         'approval_blob_sha': '1' * 40}
        self.proof = {'source_run_id': 42, 'product_source_sha': self.product,
                      'packaging_source_sha': self.packaging,
                      'workflow_source_sha': self.current_workflow,
                      'packaging_approval': copy.deepcopy(self.approval), 'version': '3.0.37'}
        self.run = {'id': 84, 'head_sha': self.producer_workflow, 'status': 'completed',
                    'conclusion': 'failure', 'event': 'workflow_dispatch', 'head_branch': 'main',
                    'path': '.github/workflows/build.yml',
                    'repository': {'id': 1433487464},
                    'head_repository': {'full_name': recovery.REPOSITORY}, 'run_attempt': 2,
                    'display_title': 'action:release:pool:public:tag:v3.0.37:repair:' + self.packaging}

        def job(name, conclusion, steps=(), platform=None):
            result = {'id': 500 + len(name), 'name': name, 'status': 'completed',
                      'conclusion': conclusion,
                      'steps': [{'name': step, 'status': 'completed', 'conclusion': 'success'}
                                for step in steps]}
            if platform == 'tvos':
                result.update(runner_id=501, runner_name='GitHub Actions 501',
                              runner_group_name='GitHub Actions', labels=['macos-26'])
            return result

        self.validate_steps = (
            'Verify approved frozen packaging checkout', 'Verify reusable platform source',
            'Validate immutable release tag against trusted main', 'Validate release tag before building',
        )
        self.tvos_steps = (
            'Build tvOS app', 'Package unsigned tvOS developer IPA', 'Build signed tvOS App Store IPA',
            'Verify tvOS runtime dependencies',
            'Attest unsigned tvOS developer package', 'Attest signed tvOS App Store package',
            'Retain tvOS signing report', 'Remove tvOS signing archive outputs',
            'Upload tvOS package', 'Clean tvOS source and Xcode scratch',
        )
        self.jobs = [
            job('Validate release request', 'success', self.validate_steps),
            job('Build tvOS developer package (arm64)', 'success', self.tvos_steps, 'tvos'),
            job('build-ios', 'skipped'), job('build-macos', 'skipped'),
            job('create-release', 'failure'), job('build-android', 'skipped'),
        ]
        self.jobs[4]['steps'] = [
            {'name': 'Recover verified platform artifacts', 'status': 'completed', 'conclusion': 'success'},
            {'name': 'Prepare append-only Apple Store recovery assets', 'status': 'completed', 'conclusion': 'failure'},
            {'name': 'Attest append-only Apple Store recovery assets', 'status': 'completed', 'conclusion': 'skipped'},
            {'name': 'Verify existing immutable release tag', 'status': 'completed', 'conclusion': 'skipped'},
            {'name': 'Append signed Apple assets without replacing existing release assets', 'status': 'completed', 'conclusion': 'skipped'},
            {'name': 'Create Release', 'status': 'completed', 'conclusion': 'skipped'},
        ]
        self.artifact = {'id': 9, 'name': 'tvos-ipa', 'expired': False,
                         'digest': 'sha256:' + 'f' * 64,
                         'workflow_run': {'id': 84, 'head_sha': self.producer_workflow}}
        self.artifact_metadata = copy.deepcopy(self.artifact)
        self.prior = {'source_sha': self.product, 'platforms_complete': True,
                      'packaging_approval': copy.deepcopy(self.approval)}

    def prove(self):
        lineage = {'status': 'ahead', 'merge_base_commit': {'sha': self.producer_workflow}}
        with patch.object(recovery, 'api', side_effect=[self.run, lineage, self.artifact_metadata]), \
                patch.object(recovery, 'collection', side_effect=[self.jobs, [self.artifact]]), \
                patch.object(recovery, 'verify_source_run', return_value=self.prior) as verify_source:
            result = recovery.verify_completed_tvos_run(84, self.proof)
        self.assertEqual(verify_source.call_args.args[:3],
                         (self.proof['source_run_id'], self.packaging, '3.0.37'))
        self.assertTrue(verify_source.call_args.kwargs['rebuild_tvos'])
        self.assertTrue(verify_source.call_args.kwargs['include_apple_store_build'])
        self.assertEqual(verify_source.call_args.kwargs['workflow_source_sha'], self.producer_workflow)
        return result

    def test_reuses_signed_artifact_from_same_approved_packaging(self):
        source = self.prove()
        self.assertEqual(source['source_run_id'], 84)
        self.assertEqual(source['artifact_id'], self.artifact['id'])
        self.assertEqual(source['artifact_digest'], self.artifact['digest'])
        self.assertEqual(source['workflow_source_sha'], self.producer_workflow)
        self.assertEqual(source['product_source_sha'], self.product)
        self.assertEqual(source['packaging_source_sha'], self.packaging)
        self.assertEqual(source['version'], '3.0.37')
        self.assertTrue(source['runtime_dependency_receipt_required'])
        self.assertEqual(source['packaging_approval'], self.approval)

    def test_full_recovery_reclassifies_tv_as_reused_and_keeps_full_release_scope(self):
        base = {'recovery_policy': 'frozen-apple-packaging-v1', 'source_run_id': 42,
                'source_sha': self.product,
                'rebuilt_jobs': sorted(set(recovery.REBUILT_JOBS) |
                                       {'Build tvOS developer package (arm64)'}),
                'reused_jobs': [name for name in recovery.REQUIRED_JOBS
                                if name != 'Build tvOS developer package (arm64)']}
        tvos = {'source_run_id': 84, 'artifact_id': 9}
        original = recovery.verify_source_run
        with patch.object(recovery, 'verify_source_run', return_value=base) as verify_base, \
                patch.object(recovery, 'verify_completed_tvos_run', return_value=tvos):
            proof = original(42, self.packaging, '3.0.37',
                             packaging_source_sha=self.packaging,
                             workflow_source_sha=self.current_workflow,
                             include_apple_store_build=True, built_tvos_run=84)
        self.assertTrue(verify_base.call_args.kwargs['rebuild_tvos'])
        self.assertEqual(proof['tvos_source'], tvos)
        self.assertFalse(proof['rebuild_tvos'])
        self.assertNotIn('Build tvOS developer package (arm64)', proof['rebuilt_jobs'])
        self.assertIn('Build tvOS developer package (arm64)', proof['reused_jobs'])
        self.assertNotIn('append_apple_only', proof)
        source_jobs = [
            {'id': 1, 'name': 'build-android', 'status': 'completed', 'conclusion': 'success'},
            {'id': 2, 'name': 'Build WebOS TV package (ARMv7)', 'status': 'completed', 'conclusion': 'success'},
        ]
        with patch.object(recovery, 'collection', return_value=source_jobs):
            assembled = recovery.compose_proof(proof, android_run=42, webos_run=42)
        self.assertEqual(assembled['rebuilt_jobs'], [])
        self.assertEqual(assembled['tvos_source'], tvos)

    def test_wrong_identity_lineage_or_approval_is_rejected(self):
        original_run = copy.deepcopy(self.run)
        for change in ('wrong-title', 'wrong-owner', 'wrong-lineage', 'different-approval'):
            self.run = copy.deepcopy(original_run)
            lineage = {'status': 'ahead', 'merge_base_commit': {'sha': self.producer_workflow}}
            if change == 'wrong-title':
                self.run['display_title'] = self.run['display_title'].replace(self.packaging, 'c' * 40)
            elif change == 'wrong-owner':
                self.run['head_repository']['full_name'] = 'example/untrusted'
            elif change == 'wrong-lineage':
                lineage = {'status': 'behind', 'merge_base_commit': {'sha': '0' * 40}}
            elif change == 'different-approval':
                self.prior['packaging_approval']['approval_blob_sha'] = '2' * 40
            with self.subTest(change=change), \
                    patch.object(recovery, 'api', side_effect=[self.run, lineage, self.artifact_metadata]), \
                    patch.object(recovery, 'collection', side_effect=[self.jobs, [self.artifact]]), \
                    patch.object(recovery, 'verify_source_run', return_value=self.prior), \
                    self.assertRaises(recovery.RecoveryError):
                recovery.verify_completed_tvos_run(84, self.proof)
        self.run = original_run

    def test_missing_signature_or_any_post_prepare_mutation_rejects_source(self):
        original_jobs = copy.deepcopy(self.jobs)
        for change in ('missing-dependency', 'missing-signature', 'missing-upload', 'missing-cleanup', 'uploaded', 'published'):
            self.jobs = copy.deepcopy(original_jobs)
            if change == 'missing-dependency':
                next(step for step in self.jobs[1]['steps']
                     if step['name'] == 'Verify tvOS runtime dependencies')['conclusion'] = 'skipped'
            elif change == 'missing-signature':
                next(step for step in self.jobs[1]['steps']
                     if step['name'] == 'Attest signed tvOS App Store package')['conclusion'] = 'skipped'
            elif change == 'missing-upload':
                next(step for step in self.jobs[1]['steps']
                     if step['name'] == 'Upload tvOS package')['conclusion'] = 'skipped'
            elif change == 'missing-cleanup':
                next(step for step in self.jobs[1]['steps']
                     if step['name'] == 'Clean tvOS source and Xcode scratch')['conclusion'] = 'skipped'
            elif change == 'uploaded':
                next(step for step in self.jobs[4]['steps']
                     if step['name'] == 'Append signed Apple assets without replacing existing release assets')['conclusion'] = 'success'
            else:
                next(step for step in self.jobs[4]['steps']
                     if step['name'] == 'Create Release')['conclusion'] = 'success'
            with self.subTest(change=change), self.assertRaises(recovery.RecoveryError):
                self.prove()
        self.jobs = original_jobs


class FrozenCombinedAppleReuseTests(unittest.TestCase):
    def setUp(self):
        self.product = 'a' * 40
        self.packaging = 'b' * 40
        self.source_workflow = 'c' * 40
        self.assembly_workflow = 'd' * 40
        self.source_run = 84
        self.proof = {
            'source_run_id': 42, 'product_source_sha': self.product,
            'packaging_source_sha': self.packaging,
            'workflow_source_sha': self.assembly_workflow, 'version': '3.0.36',
        }
        self.run = {
            'id': self.source_run, 'head_sha': self.source_workflow, 'status': 'completed',
            'conclusion': 'failure', 'event': 'workflow_dispatch', 'head_branch': 'main',
            'path': '.github/workflows/build.yml',
            'repository': {'id': 1433487464},
            'head_repository': {'full_name': recovery.REPOSITORY}, 'run_attempt': 1,
            'display_title': f'action:release:pool:public:tag:v3.0.36:repair:{self.packaging}',
        }

        def job(name, conclusion, steps=(), platform=None):
            item = {
                'id': len(name), 'name': name, 'status': 'completed',
                'conclusion': conclusion,
                'steps': [{'name': step, 'status': 'completed', 'conclusion': 'success'}
                          for step in steps],
            }
            if platform:
                item.update(
                    runner_id=100 + len(name), runner_name=f'GitHub Actions {100 + len(name)}',
                    runner_group_name='GitHub Actions',
                    labels=['macos-26-intel' if platform == 'ios' else 'macos-26'],
                )
            return item

        validate_steps = (
            'Verify approved frozen packaging checkout', 'Verify reusable platform source',
            'Validate immutable release tag against trusted main', 'Validate release tag before building',
        )
        ios_steps = (
            'Build iOS (no codesign)', 'Create unsigned developer IPA',
            'Attest unsigned developer IPA', 'Retain unsigned iOS developer checkpoint',
            'Build signed iOS App Store IPA', 'Attest signed iOS App Store IPA',
            'Retain iOS signing report', 'Remove iOS signing archive outputs',
            'Upload IPA', 'Remove short iOS build workspace',
        )
        tvos_steps = (
            'Build tvOS app', 'Package unsigned tvOS developer IPA',
            'Build signed tvOS App Store IPA', 'Verify tvOS runtime dependencies',
            'Attest unsigned tvOS developer package',
            'Attest signed tvOS App Store package', 'Retain tvOS signing report',
            'Remove tvOS signing archive outputs', 'Upload tvOS package',
            'Clean tvOS source and Xcode scratch',
        )
        mutation_steps = (
            'Prepare append-only Apple Store recovery assets',
            'Attest append-only Apple Store recovery assets',
            'Verify existing immutable release tag',
            'Append signed Apple assets without replacing existing release assets',
            'Create Release',
        )
        create_steps = [
            {'name': 'Recover verified platform artifacts', 'status': 'completed', 'conclusion': 'failure'},
            *[{'name': name, 'status': 'completed', 'conclusion': 'skipped'} for name in mutation_steps],
            {'name': 'Post Run actions/checkout', 'status': 'completed', 'conclusion': 'success'},
            {'name': 'Complete job', 'status': 'completed', 'conclusion': 'success'},
        ]
        self.jobs = [
            job('Validate release request', 'success', validate_steps),
            job('build-ios', 'success', ios_steps, 'ios'),
            job('Build tvOS developer package (arm64)', 'success', tvos_steps, 'tvos'),
            job('build-macos', 'success', ('Build macOS',)),
            job('create-release', 'failure'),
            job('build-android', 'skipped'),
        ]
        self.jobs[4]['steps'] = create_steps
        self.artifacts = [
            {'id': 7, 'name': 'ios-ipa', 'expired': False, 'digest': 'sha256:' + 'e' * 64,
             'workflow_run': {'id': self.source_run, 'head_sha': self.source_workflow}},
            {'id': 8, 'name': 'tvos-ipa', 'expired': False, 'digest': 'sha256:' + 'f' * 64,
             'workflow_run': {'id': self.source_run, 'head_sha': self.source_workflow}},
        ]
        self.metadata = [dict(item) for item in self.artifacts]
        self.prior = {
            'source_sha': self.product, 'platforms_complete': True,
            'packaging_approval': {
                'product_source_sha': self.product,
                'packaging_source_sha': self.packaging,
                'workflow_source_sha': self.source_workflow,
                'approval_source_sha': self.source_workflow,
                'approved_files': {'scripts/verified-packaging.py': '1' * 40},
            },
        }

    def prove(self):
        lineage = {'status': 'ahead', 'merge_base_commit': {'sha': self.source_workflow}}
        with patch.object(recovery, 'api', side_effect=[self.run, lineage, *self.metadata]), \
                patch.object(recovery, 'collection', side_effect=[self.jobs, self.artifacts]), \
                patch.object(recovery, 'verify_source_run', return_value=self.prior) as verify_source:
            result = recovery.verify_frozen_apple_run(self.source_run, self.proof)
        self.assertEqual(verify_source.call_args.args[:3], (42, self.packaging, '3.0.36'))
        self.assertEqual(verify_source.call_args.kwargs['workflow_source_sha'], self.source_workflow)
        return result

    def test_same_run_supplies_separate_attested_ipa_artifacts_for_both_platforms(self):
        result = self.prove()
        self.assertEqual(result['ios_source']['artifact_id'], 7)
        self.assertEqual(result['tvos_source']['artifact_id'], 8)
        self.assertEqual(result['ios_source']['source_run_id'], self.source_run)
        self.assertEqual(result['tvos_source']['source_run_id'], self.source_run)
        self.assertEqual(result['ios_source']['job_id'], self.jobs[1]['id'])
        self.assertEqual(result['tvos_source']['job_id'], self.jobs[2]['id'])
        self.assertEqual(result['tvos_source']['packaging_source_sha'], self.packaging)
        self.assertTrue(result['tvos_source']['runtime_dependency_receipt_required'])

    def test_missing_failed_signing_upload_or_cleanup_steps_are_rejected(self):
        original_jobs = copy.deepcopy(self.jobs)
        for job_name, step_name in (
                ('build-ios', 'Build signed iOS App Store IPA'),
                ('build-ios', 'Attest signed iOS App Store IPA'),
                ('build-ios', 'Upload IPA'),
                ('build-ios', 'Remove short iOS build workspace'),
                ('Build tvOS developer package (arm64)', 'Build signed tvOS App Store IPA'),
                ('Build tvOS developer package (arm64)', 'Verify tvOS runtime dependencies'),
                ('Build tvOS developer package (arm64)', 'Attest signed tvOS App Store package'),
                ('Build tvOS developer package (arm64)', 'Upload tvOS package'),
                ('Build tvOS developer package (arm64)', 'Clean tvOS source and Xcode scratch')):
            self.jobs = copy.deepcopy(original_jobs)
            job = next(item for item in self.jobs if item['name'] == job_name)
            next(step for step in job['steps'] if step['name'] == step_name)['conclusion'] = 'failure'
            with self.subTest(job=job_name, step=step_name), \
                    self.assertRaises(recovery.RecoveryError):
                self.prove()

    def test_wrong_product_packaging_workflow_or_artifact_identity_is_rejected(self):
        original_proof = copy.deepcopy(self.proof)
        original_run = copy.deepcopy(self.run)
        original_artifacts = copy.deepcopy(self.artifacts)
        original_prior = copy.deepcopy(self.prior)
        for kind in ('product', 'packaging', 'workflow', 'artifact_id', 'artifact_digest', 'artifact_source'):
            self.proof = copy.deepcopy(original_proof)
            self.run = copy.deepcopy(original_run)
            self.artifacts = copy.deepcopy(original_artifacts)
            self.metadata = [dict(item) for item in self.artifacts]
            self.prior = copy.deepcopy(original_prior)
            if kind == 'product':
                self.prior['source_sha'] = '9' * 40
            elif kind == 'packaging':
                self.run['display_title'] = self.run['display_title'].replace(self.packaging, '9' * 40)
            elif kind == 'workflow':
                self.run['head_sha'] = '9' * 40
            elif kind == 'artifact_id':
                self.metadata[0]['id'] = 99
            elif kind == 'artifact_digest':
                self.metadata[0]['digest'] = 'sha256:' + '9' * 64
            else:
                self.metadata[0]['workflow_run']['head_sha'] = self.packaging
            with self.subTest(kind=kind), self.assertRaises(recovery.RecoveryError):
                self.prove()

    def test_unexpected_release_mutation_duplicate_platform_or_shared_artifact_is_rejected(self):
        original_jobs = copy.deepcopy(self.jobs)
        create = next(job for job in self.jobs if job['name'] == 'create-release')
        next(step for step in create['steps']
             if step['name'] == 'Append signed Apple assets without replacing existing release assets')['conclusion'] = 'success'
        with self.assertRaisesRegex(recovery.RecoveryError, 'later mutation'):
            self.prove()

        for platform_name in ('build-ios', 'Build tvOS developer package (arm64)'):
            self.jobs = copy.deepcopy(original_jobs)
            self.jobs.append(copy.deepcopy(next(job for job in self.jobs if job['name'] == platform_name)))
            with self.subTest(platform=platform_name), \
                    self.assertRaisesRegex(recovery.RecoveryError, 'missing or ambiguous'):
                self.prove()

        self.jobs = copy.deepcopy(original_jobs)
        self.artifacts[1]['id'] = self.artifacts[0]['id']
        self.metadata = [dict(item) for item in self.artifacts]
        with self.assertRaisesRegex(recovery.RecoveryError, 'distinct IPA artifacts'):
            self.prove()


class TvosRuntimeDependencyReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.product = 'a' * 40
        self.packaging = 'b' * 40
        self.workflow = 'c' * 40
        self.source = {
            'source_run_id': 84, 'run_attempt': 2,
            'product_source_sha': self.product, 'packaging_source_sha': self.packaging,
            'workflow_source_sha': self.workflow, 'source_sha': self.packaging,
            'version': '3.0.37', 'runtime_dependency_receipt_required': True,
        }
        self.ipa_paths = {}
        self.receipt = self._make_receipt()

    def _make_ipa(self, name, executable):
        path = self.root / name
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('Payload/Runner.app/Info.plist', plistlib.dumps({
                'CFBundleIdentifier': 'org.snapetech.pleazyng',
                'CFBundleExecutable': 'Runner',
                'CFBundleShortVersionString': '3.0.37',
                'CFBundleVersion': '196',
                'DTPlatformName': 'appletvos',
            }))
            archive.writestr('Payload/Runner.app/Runner', executable)
        self.ipa_paths[name] = path
        return path

    def _make_receipt(self):
        executable = b'\xcf\xfa\xed\xfe' + b'fixture-runner'
        records = []
        for name, contents in (
                ('pleazyng-tvos-unsigned.ipa', executable + b'-unsigned'),
                ('pleazyng-tvos-appstore.ipa', executable + b'-signed')):
            path = self._make_ipa(name, contents)
            macho_sha = hashlib.sha256(contents).hexdigest()
            records.append({
                'artifact_name': 'tvos-ipa', 'filename': name,
                'ipa_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'platform': 'tvos', 'status': 'passed', 'errors': [],
                'bundle_id': 'org.snapetech.pleazyng', 'product_version': '3.0.37',
                'product_build': '196', 'product_source_sha': self.product,
                'packaging_source_sha': self.packaging, 'workflow_source_sha': self.workflow,
                'run_id': '84', 'run_attempt': '2',
                'macho_images': [{
                    'path': 'Payload/Runner.app/Runner', 'architectures': ['arm64'],
                    'sha256': macho_sha,
                }],
                'dependencies': [], 'unresolved_weak_dependencies': [],
                'macho_image_count': 1,
            })
        return {
            'schema_version': 1, 'workflow_step': 'Verify tvOS runtime dependencies',
            'repository': recovery.REPOSITORY, 'product_source_sha': self.product,
            'packaging_source_sha': self.packaging, 'workflow_source_sha': self.workflow,
            'actual_checkout_sha': self.packaging, 'run_id': '84', 'run_attempt': '2',
            'artifact_name': 'tvos-ipa', 'platform': 'tvos', 'status': 'passed',
            'sdk_root': '/Applications/Xcode.app/Platforms/AppleTVOS.platform/Developer/SDKs/AppleTVOS.sdk',
            'sdk_version': '27.2', 'product_version': '3.0.37', 'product_build': '196',
            'inputs': records, 'errors': [], 'created_at_utc': '2026-11-17T00:00:00+00:00',
            'verification_policy': {
                'architectures': 'all bundled Mach-O slices',
                'strong_dependencies': 'must resolve to a same-architecture Mach-O inside the app bundle or an allowed system path',
                'weak_dependencies': 'unresolved optional weak loads are recorded and do not fail',
                'system_prefixes': list(recovery.TVOS_RUNTIME_SYSTEM_PREFIXES),
            },
        }

    def _write_receipt(self, receipt=None):
        path = self.root / recovery.TVOS_RUNTIME_RECEIPT
        path.write_text(json.dumps(self.receipt if receipt is None else receipt), encoding='utf-8')
        return path

    def _verify(self):
        self._write_receipt()
        certificate = {
            'sourceRepositoryIdentifier': '1433487464',
            'sourceRepositoryDigest': self.workflow,
            'runInvocationURI': f'https://github.com/{recovery.REPOSITORY}/actions/runs/84/attempts/2',
        }
        with patch.object(recovery, 'immutable_content', return_value='version: 3.0.37+196\n'), \
                patch.object(recovery.verify_release_attestation, 'verify',
                              return_value=recovery.REPOSITORY) as general_verify, \
                patch.object(recovery.subprocess, 'run', return_value=SimpleNamespace(
                    stdout=json.dumps([{'verificationResult': {'signature': {'certificate': certificate}}}]),
                    returncode=0)) as exact_verify:
            digest = recovery.verify_tvos_runtime_dependency_receipt(self.root, self.source)
        general_verify.assert_called_once_with(self.root / recovery.TVOS_RUNTIME_RECEIPT, recovery.REPOSITORY)
        self.assertEqual(exact_verify.call_args.args[0][:3], [
            'gh', 'attestation', 'verify',
        ][:3])
        self.assertIn('--source-digest', exact_verify.call_args.args[0])
        self.assertEqual(self.workflow, exact_verify.call_args.args[0][
            exact_verify.call_args.args[0].index('--source-digest') + 1])
        return digest

    def test_verified_receipt_binds_both_signed_and_unsigned_ipa_bytes(self):
        digest = self._verify()
        self.assertEqual(hashlib.sha256((self.root / recovery.TVOS_RUNTIME_RECEIPT).read_bytes()).hexdigest(), digest)

    def test_missing_receipt_is_rejected_before_trust_checks(self):
        with self.assertRaisesRegex(recovery.RecoveryError, 'missing its attested'):
            recovery.verify_tvos_runtime_dependency_receipt(self.root, self.source)

    def test_tampered_ipa_hash_and_missing_image_are_rejected(self):
        original = copy.deepcopy(self.receipt)
        for mutate in (
                lambda item: item['inputs'][0].update(ipa_sha256='0' * 64),
                lambda item: item['inputs'][0].update(macho_images=[])):
            changed = copy.deepcopy(original)
            mutate(changed)
            self.receipt = changed
            with self.subTest(changed=changed['inputs'][0]), self.assertRaises(recovery.RecoveryError):
                self._verify()

    def test_wrong_product_packaging_workflow_or_run_identity_is_rejected(self):
        original = copy.deepcopy(self.receipt)
        for key, value in (
                ('product_source_sha', 'd' * 40), ('packaging_source_sha', 'd' * 40),
                ('workflow_source_sha', 'd' * 40), ('run_id', '85'), ('run_attempt', '3'),
                ('actual_checkout_sha', 'd' * 40)):
            changed = copy.deepcopy(original)
            changed[key] = value
            self.receipt = changed
            with self.subTest(key=key), self.assertRaises(recovery.RecoveryError):
                self._verify()

    def test_each_ipa_input_binds_product_packaging_and_workflow_sources(self):
        original = copy.deepcopy(self.receipt)
        for key in ('product_source_sha', 'packaging_source_sha', 'workflow_source_sha'):
            changed = copy.deepcopy(original)
            changed['inputs'][0][key] = 'd' * 40
            self.receipt = changed
            with self.subTest(key=key), self.assertRaisesRegex(
                    recovery.RecoveryError, 'input identity mismatch'):
                self._verify()

    def test_malformed_dependency_types_fail_closed(self):
        original = copy.deepcopy(self.receipt)
        for mutate in (
                lambda item: item['inputs'][0].update(dependencies=[{
                    'architecture': 'arm64', 'command': [], 'install_name': 'libSystem.B.dylib',
                    'from_image': 'Payload/Runner.app/Runner', 'resolution': 'system',
                }]),
                lambda item: item['inputs'][0].update(unresolved_weak_dependencies=[{
                    'architecture': 'arm64', 'install_name': '@rpath/optional.dylib',
                    'from_image': [],
                }]),
        ):
            changed = copy.deepcopy(original)
            mutate(changed)
            self.receipt = changed
            with self.assertRaises(recovery.RecoveryError):
                self._verify()

    def test_unhashable_input_filename_is_rejected(self):
        changed = copy.deepcopy(self.receipt)
        changed['inputs'][0]['filename'] = []
        self.receipt = changed
        with self.assertRaisesRegex(recovery.RecoveryError, 'unexpected IPA input'):
            self._verify()

    def test_failed_or_unresolved_strong_receipts_are_rejected(self):
        for mutate in (
                lambda item: item.update(status='failed'),
                lambda item: item['inputs'][0].update(status='failed'),
                lambda item: item['inputs'][0].update(dependencies=[{
                    'architecture': 'arm64', 'command': 'LC_LOAD_DYLIB',
                    'install_name': '@rpath/AMSMB2.framework/AMSMB2',
                    'from_image': 'Payload/Runner.app/Runner', 'resolution': 'missing',
                    'resolved_path': None,
                }]),
        ):
            changed = copy.deepcopy(self.receipt)
            mutate(changed)
            self.receipt = changed
            with self.assertRaises(recovery.RecoveryError):
                self._verify()

    def test_wrong_receipt_attestation_publisher_is_rejected(self):
        self._write_receipt()
        with patch.object(recovery.verify_release_attestation, 'verify', return_value='snapetech-org/PlezyNG'):
            with self.assertRaisesRegex(recovery.RecoveryError, 'canonical repository'):
                recovery.verify_tvos_runtime_dependency_receipt(self.root, self.source)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, name="payload.exe", contents=b"binary", symlink=False):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            info = zipfile.ZipInfo(name)
            if symlink:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, contents)
        return stream.getvalue()

    def test_rejects_zip_traversal_symlinks_and_collisions(self):
        for name in ("../escape", "/absolute", "folder\\escape", "C:/escape"):
            with self.subTest(name=name), self.assertRaises(recovery.RecoveryError):
                recovery.extract_verified(io.BytesIO(self.archive(name)), self.root / "out")
        with self.assertRaises(recovery.RecoveryError):
            recovery.extract_verified(io.BytesIO(self.archive(symlink=True)), self.root / "out")
        recovery.extract_verified(io.BytesIO(self.archive()), self.root / "out")
        with self.assertRaises(recovery.RecoveryError):
            recovery.extract_verified(io.BytesIO(self.archive()), self.root / "out")

    def test_existing_destination_symlink_is_rejected(self):
        (self.root / "out").mkdir()
        (self.root / "out" / "nested").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(recovery.RecoveryError):
            recovery.extract_verified(io.BytesIO(self.archive("nested/escape")), self.root / "out")

    def test_android_artifact_uses_its_own_verified_run_identity(self):
        archive = self.archive()
        item = {"id": 7, "name": "android-apk", "expired": False,
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                "workflow_run": {"id": 84, "head_sha": "b" * 40}}
        proof = {"source_run_id": 42, "source_sha": "a" * 40,
                 "android_source": {"source_run_id": 84, "source_sha": "b" * 40}}
        def download(_args, stdout):
            stdout.write(archive)
            return SimpleNamespace(returncode=0)
        with patch.object(recovery, "ARTIFACTS", {}), \
             patch.object(recovery, "collection", side_effect=[[], [item]]), \
             patch.object(recovery.subprocess, "run", side_effect=download):
            result = recovery.download_platforms(proof, self.root)
        self.assertEqual(result["artifacts"][0]["source_run_id"], 84)
        self.assertEqual((self.root / "android-apk/payload.exe").read_bytes(), b"binary")

    def test_tvos_is_not_downloaded_from_failed_original_when_rebuilt(self):
        proof = {"source_run_id": 42, "source_sha": "a" * 40, "rebuild_tvos": True}
        with patch.object(recovery, "ARTIFACTS", {"tvos-ipa": "tvos-ipa"}), \
             patch.object(recovery, "collection", return_value=[]), \
             patch.object(recovery.subprocess, "run") as download:
            result = recovery.download_platforms(proof, self.root)
            download.assert_not_called()
            self.assertEqual(result["artifacts"], [])
        proof["rebuild_tvos"] = False
        with patch.object(recovery, "ARTIFACTS", {"tvos-ipa": "tvos-ipa"}), \
             patch.object(recovery, "collection", return_value=[]):
            with self.assertRaises(recovery.RecoveryError):
                recovery.download_platforms(proof, self.root)

    def test_tizen_is_not_downloaded_from_failed_original_when_rebuilt(self):
        proof = {"source_run_id": 42, "source_sha": "a" * 40, "rebuild_tizen": True}
        with patch.object(recovery, "ARTIFACTS", {"pleazyng-tizen-arm-experimental": "tizen"}), \
             patch.object(recovery, "collection", return_value=[]), \
             patch.object(recovery.subprocess, "run") as download:
            result = recovery.download_platforms(proof, self.root)
            download.assert_not_called()
            self.assertEqual(result["artifacts"], [])

    def test_rebuilt_tv_artifact_is_downloaded_from_verified_sibling_when_reused(self):
        archive = self.archive('pleazyng-tvos-appstore.ipa', b'signed tvOS IPA')
        artifact = {'id': 9, 'name': 'tvos-ipa', 'expired': False,
                    'digest': 'sha256:' + hashlib.sha256(archive).hexdigest(),
                    'workflow_run': {'id': 84, 'head_sha': 'd' * 40}}
        proof = {'source_run_id': 42, 'source_sha': 'a' * 40, 'rebuild_tvos': False,
                 'tvos_source': {'source_run_id': 84, 'source_sha': 'b' * 40,
                                 'workflow_source_sha': 'd' * 40, 'artifact_id': 9,
                                 'artifact_digest': artifact['digest'], 'product_source_sha': 'c' * 40,
                                 'packaging_source_sha': 'b' * 40, 'version': '3.0.37',
                                 'run_attempt': 2, 'runtime_dependency_receipt_required': True}}

        def download(_command, stdout):
            stdout.write(archive)
            return SimpleNamespace(returncode=0)

        with patch.object(recovery, 'ARTIFACTS', {'tvos-ipa': 'tvos-ipa'}), \
                patch.object(recovery, 'collection', side_effect=[[], [artifact]]), \
                patch.object(recovery.subprocess, 'run', side_effect=download), \
                patch.object(recovery, 'verify_tvos_package_attestations', return_value={'signed': 'c' * 64}), \
                patch.object(recovery, 'verify_tvos_runtime_dependency_receipt', return_value='e' * 64):
            result = recovery.download_platforms(proof, self.root)
        self.assertEqual(result['artifacts'][0]['source_run_id'], 84)
        self.assertEqual(result['artifacts'][0]['artifact_id'], 9)
        self.assertEqual((self.root / 'tvos-ipa/pleazyng-tvos-appstore.ipa').read_bytes(), b'signed tvOS IPA')

    def test_completed_tv_reuse_checks_attested_receipt_after_artifact_download(self):
        archive_stream = io.BytesIO()
        with zipfile.ZipFile(archive_stream, 'w', compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr('pleazyng-tvos-unsigned.ipa', b'unsigned')
            package.writestr('pleazyng-tvos-appstore.ipa', b'signed')
            package.writestr(recovery.TVOS_RUNTIME_RECEIPT, b'{}')
        archive = archive_stream.getvalue()
        digest = 'sha256:' + hashlib.sha256(archive).hexdigest()
        artifact = {'id': 9, 'name': 'tvos-ipa', 'expired': False, 'digest': digest,
                    'workflow_run': {'id': 84, 'head_sha': 'e' * 40}}
        tvos_source = {
            'source_run_id': 84, 'source_sha': 'b' * 40, 'product_source_sha': 'c' * 40,
            'packaging_source_sha': 'b' * 40, 'workflow_source_sha': 'e' * 40,
            'version': '3.0.37', 'run_attempt': 2, 'artifact_id': 9,
            'artifact_digest': digest, 'runtime_dependency_receipt_required': True,
        }
        proof = {'source_run_id': 42, 'source_sha': 'a' * 40, 'rebuild_tvos': False,
                 'tvos_source': tvos_source}

        def download(_command, stdout):
            stdout.write(archive)
            return SimpleNamespace(returncode=0)

        with patch.object(recovery, 'ARTIFACTS', {'tvos-ipa': 'tvos-ipa'}), \
                patch.object(recovery, 'collection', side_effect=[[], [artifact]]), \
                patch.object(recovery.subprocess, 'run', side_effect=download), \
                patch.object(recovery, 'verify_tvos_package_attestations', return_value={'signed': 'c' * 64}), \
                patch.object(recovery, 'verify_tvos_runtime_dependency_receipt', return_value='d' * 64) as receipt:
            result = recovery.download_platforms(proof, self.root)
        receipt.assert_called_once_with(self.root / 'tvos-ipa', tvos_source, recovery.REPOSITORY)
        self.assertEqual(result['tvos_source'], tvos_source)

    def test_ios_is_not_downloaded_from_failed_original_when_rebuilt(self):
        proof = {"source_run_id": 42, "source_sha": "a" * 40, "rebuild_ios": True}
        with patch.object(recovery, "ARTIFACTS", {"ios-ipa": "ios-ipa"}), \
             patch.object(recovery, "collection", return_value=[]), \
             patch.object(recovery.subprocess, "run") as download:
            result = recovery.download_platforms(proof, self.root)
            download.assert_not_called()
            self.assertEqual(result["artifacts"], [])
        proof["rebuild_ios"] = False
        with patch.object(recovery, "ARTIFACTS", {"ios-ipa": "ios-ipa"}), \
             patch.object(recovery, "collection", return_value=[]):
            with self.assertRaises(recovery.RecoveryError):
                recovery.download_platforms(proof, self.root)

    def test_repaired_linux_and_mac_artifacts_use_the_repaired_run_and_digest(self):
        archive = self.archive()
        names = ("linux-x64", "macos-dmg")
        items = [{"id": index, "name": name, "expired": False,
                  "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                  "workflow_run": {"id": 84, "head_sha": "b" * 40}} for index, name in enumerate(names, 1)]
        proof = {"source_run_id": 42, "source_sha": "a" * 40,
                 "repaired_source": {"source_run_id": 84, "source_sha": "b" * 40}}
        def download(_args, stdout):
            stdout.write(archive)
            return SimpleNamespace(returncode=0)
        with patch.object(recovery, "ARTIFACTS", {name: name for name in names}), \
             patch.object(recovery, "collection", side_effect=[[], items]), \
             patch.object(recovery.subprocess, "run", side_effect=download):
            result = recovery.download_platforms(proof, self.root)
        self.assertEqual([record["source_run_id"] for record in result["artifacts"]], [84, 84])

    def test_webos_artifact_uses_its_successful_run_identity(self):
        archive = self.archive()
        item = {"id": 7, "name": "webos-ipk", "expired": False,
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                "workflow_run": {"id": 84, "head_sha": "b" * 40}}
        proof = {"source_run_id": 42, "source_sha": "a" * 40,
                 "webos_source": {"source_run_id": 84, "source_sha": "b" * 40}}
        def download(_args, stdout):
            stdout.write(archive)
            return SimpleNamespace(returncode=0)
        with patch.object(recovery, "ARTIFACTS", {}), \
             patch.object(recovery, "collection", side_effect=[[], [item]]), \
             patch.object(recovery.subprocess, "run", side_effect=download):
            result = recovery.download_platforms(proof, self.root)
        self.assertEqual(result["artifacts"][0]["source_run_id"], 84)
        self.assertEqual((self.root / "webos/payload.exe").read_bytes(), b"binary")

    def test_tv_download_rechecks_exact_artifact_and_workflow_then_verifies_both_ipas(self):
        archive = self.archive()
        item = {'id': 7, 'name': 'tvos-ipa', 'expired': False,
                'digest': 'sha256:' + hashlib.sha256(archive).hexdigest(),
                'workflow_run': {'id': 84, 'head_sha': 'e' * 40}}
        source = {'source_run_id': 84, 'source_sha': 'd' * 40, 'workflow_source_sha': 'e' * 40,
                  'artifact_id': 7, 'artifact_digest': item['digest'], 'product_source_sha': 'f' * 40,
                  'packaging_source_sha': 'd' * 40, 'version': '3.0.37', 'run_attempt': 2,
                  'runtime_dependency_receipt_required': True}
        proof = {'source_run_id': 42, 'source_sha': 'a' * 40, 'tvos_source': source}
        def download(_args, stdout):
            stdout.write(archive)
            return SimpleNamespace(returncode=0)
        with patch.object(recovery, 'ARTIFACTS', {'tvos-ipa': 'tvos-ipa'}), \
                patch.object(recovery, 'collection', side_effect=[[], [item]]), \
                patch.object(recovery.subprocess, 'run', side_effect=download), \
                patch.object(recovery, 'verify_tvos_package_attestations', return_value={'verified': 'hash'}) as cert, \
                patch.object(recovery, 'verify_tvos_runtime_dependency_receipt', return_value='f' * 64) as receipt:
            result = recovery.download_platforms(proof, self.root)
        cert.assert_called_once()
        receipt.assert_called_once_with(self.root / 'tvos-ipa', source, recovery.REPOSITORY)
        self.assertEqual(result['tvos_source']['package_sha256'], {'verified': 'hash'})
        self.assertEqual(result['artifacts'][0]['source_sha'], 'd' * 40)
        for change in ({'artifact_id': 8}, {'artifact_digest': 'sha256:' + 'f' * 64}):
            with self.subTest(change=change), patch.object(recovery, 'ARTIFACTS', {'tvos-ipa': 'tvos-ipa'}), \
                    patch.object(recovery, 'collection', side_effect=[[], [item]]), \
                    patch.object(recovery.subprocess, 'run') as download:
                with self.assertRaisesRegex(recovery.RecoveryError, 'ID/digest changed'):
                    recovery.download_platforms({**proof, 'tvos_source': {**source, **change}}, self.root)
                download.assert_not_called()

    def test_append_only_apple_assembly_downloads_only_the_two_signed_ipa_artifacts(self):
        ios_archive = self.archive('ios/payload', b'iOS package')
        tvos_archive = self.archive('tvos/payload', b'tvOS package')
        source_run = 84
        workflow = 'c' * 40
        ios_digest = 'sha256:' + hashlib.sha256(ios_archive).hexdigest()
        tvos_digest = 'sha256:' + hashlib.sha256(tvos_archive).hexdigest()
        ios = {'source_run_id': source_run, 'source_sha': 'b' * 40,
               'workflow_source_sha': workflow, 'artifact_id': 7,
               'artifact_digest': ios_digest}
        tvos = {'source_run_id': source_run, 'source_sha': 'b' * 40,
                'workflow_source_sha': workflow, 'artifact_id': 8,
                'artifact_digest': tvos_digest, 'product_source_sha': 'a' * 40,
                'packaging_source_sha': 'b' * 40, 'version': '3.0.37', 'run_attempt': 2,
                'runtime_dependency_receipt_required': True}
        artifacts = [
            {'id': 7, 'name': 'ios-ipa', 'expired': False, 'digest': ios_digest,
             'workflow_run': {'id': source_run, 'head_sha': workflow}},
            {'id': 8, 'name': 'tvos-ipa', 'expired': False, 'digest': tvos_digest,
             'workflow_run': {'id': source_run, 'head_sha': workflow}},
            {'id': 9, 'name': 'macos-dmg', 'expired': False, 'digest': 'sha256:' + '9' * 64,
             'workflow_run': {'id': source_run, 'head_sha': workflow}},
            {'id': 10, 'name': 'linux-x64', 'expired': False, 'digest': 'sha256:' + 'a' * 64,
             'workflow_run': {'id': source_run, 'head_sha': workflow}},
        ]
        proof = {
            'source_run_id': 42, 'source_sha': 'a' * 40,
            'product_source_sha': 'a' * 40,
            'packaging_source_sha': 'b' * 40,
            'workflow_source_sha': workflow,
            'recovery_policy': 'frozen-apple-packaging-v1',
            'packaging_approval': {
                'recovery_policy': 'frozen-apple-packaging-v1',
                'product_source_sha': 'a' * 40, 'packaging_source_sha': 'b' * 40,
                'workflow_source_sha': workflow, 'approval_source_sha': workflow,
                'approved_files': {'scripts/verified.py': 'd' * 40},
            },
            'append_apple_only': True, 'ios_source': ios, 'tvos_source': tvos,
        }
        packages = {7: ios_archive, 8: tvos_archive}
        downloaded = []

        def download(args, stdout):
            artifact_id = int(args[-1].rsplit('/', 2)[-2])
            downloaded.append(artifact_id)
            stdout.write(packages[artifact_id])
            return SimpleNamespace(returncode=0)

        with patch.object(recovery, 'ARTIFACTS', {'macos-dmg': 'macos-dmg', 'linux-x64': 'linux-x64'}), \
                patch.object(recovery, 'collection', return_value=artifacts) as inventory, \
                patch.object(recovery.subprocess, 'run', side_effect=download), \
                patch.object(recovery.subprocess, 'check_output', return_value=workflow), \
                patch.dict('os.environ', {'GITHUB_SHA': workflow, 'GITHUB_RUN_ID': '99'}), \
                patch.object(recovery, 'verify_ios_package_attestations', return_value={'verified': 'ios'}), \
                patch.object(recovery, 'verify_tvos_package_attestations', return_value={'verified': 'tvos'}), \
                patch.object(recovery, 'verify_tvos_runtime_dependency_receipt', return_value='f' * 64):
            result = recovery.download_platforms(proof, self.root)

        self.assertEqual(inventory.call_count, 1)
        self.assertEqual(set(downloaded), {7, 8})
        self.assertEqual({item['name'] for item in result['artifacts']}, {'ios-ipa', 'tvos-ipa'})
        self.assertEqual({item['workflow_source_sha'] for item in result['artifacts']}, {workflow})
        self.assertTrue(result['append_apple_only'])
        self.assertEqual(result['assembly_scope'], 'apple-store-append-only')
        self.assertNotIn('linux_archive_sha256', result)

    def test_original_artifact_uses_product_head_even_with_later_workflow(self):
        archive = self.archive()
        item = {'id': 7, 'name': 'pleazyng-tizen-arm-experimental', 'expired': False,
                'digest': 'sha256:' + hashlib.sha256(archive).hexdigest(),
                'workflow_run': {'id': 42, 'head_sha': 'a' * 40}}
        proof = {'source_run_id': 42, 'source_sha': 'a' * 40,
                 'workflow_source_sha': 'c' * 40}
        def download(_args, stdout):
            stdout.write(archive)
            return SimpleNamespace(returncode=0)
        with patch.object(recovery, 'ARTIFACTS', {item['name']: 'tizen'}), \
                patch.object(recovery, 'collection', return_value=[item]), \
                patch.object(recovery.subprocess, 'run', side_effect=download):
            result = recovery.download_platforms(proof, self.root)
        self.assertEqual(result['artifacts'][0]['source_sha'], 'a' * 40)
        self.assertEqual(result['artifacts'][0]['workflow_source_sha'], 'a' * 40)

    def test_frozen_assembly_binds_actual_checkout_workflow_run_and_linux_hash(self):
        proof = {'source_run_id': 42, 'source_sha': 'a' * 40,
                 'recovery_policy': 'frozen-apple-packaging-v1',
                 'product_source_sha': 'a' * 40,
                 'packaging_source_sha': 'b' * 40, 'workflow_source_sha': 'c' * 40,
                 'packaging_approval': {
                     'recovery_policy': 'frozen-apple-packaging-v1',
                     'product_source_sha': 'a' * 40,
                     'packaging_source_sha': 'b' * 40,
                     'workflow_source_sha': 'c' * 40,
                     'approval_source_sha': 'c' * 40,
                     'approved_files': {'scripts/example.py': 'd' * 40},
                 }}
        linux = self.root / 'linux-x64/pleazyng-linux-x64.tar.gz'
        linux.parent.mkdir(); linux.write_bytes(b'original immutable Linux archive')
        with patch.object(recovery, 'ARTIFACTS', {}), \
             patch.object(recovery, 'collection', return_value=[]), \
             patch.object(recovery.subprocess, 'check_output', return_value='c' * 40), \
             patch.dict('os.environ', {'GITHUB_SHA': 'c' * 40, 'GITHUB_RUN_ID': '84'}):
            result = recovery.download_platforms(proof, self.root)
            self.assertEqual(result['assembly_run_id'], 84)
            self.assertEqual(result['actual_checkout_sha'], 'c' * 40)
            self.assertEqual(result['linux_archive_sha256'], hashlib.sha256(linux.read_bytes()).hexdigest())
            with patch.dict('os.environ', {'GITHUB_SHA': 'd' * 40}):
                with self.assertRaisesRegex(recovery.RecoveryError, 'workflow identity'):
                    recovery.download_platforms(proof, self.root)
            with patch.object(recovery.subprocess, 'check_output', return_value='b' * 40):
                with self.assertRaisesRegex(recovery.RecoveryError, 'did not check out'):
                    recovery.download_platforms(proof, self.root)
            mismatched_repair = {**proof, 'packaging_approval': {
                **proof['packaging_approval'], 'packaging_source_sha': 'e' * 40}}
            with patch.object(recovery.subprocess, 'check_output', return_value='c' * 40):
                with self.assertRaisesRegex(recovery.RecoveryError, 'does not bind'):
                    recovery.download_platforms(mismatched_repair, self.root)

    def test_verified_download_records_actual_source_and_digests(self):
        archive = self.archive()
        digest = "sha256:" + hashlib.sha256(archive).hexdigest()
        item = {"id": 7, "name": "native", "expired": False, "digest": digest,
                "workflow_run": {"id": 42, "head_sha": "a" * 40}}
        proof = {"source_run_id": 42, "source_sha": "a" * 40}

        def download(_args, stdout):
            stdout.write(archive)
            return SimpleNamespace(returncode=0)

        with patch.object(recovery, "ARTIFACTS", {"native": "native"}), \
             patch.object(recovery, "collection", return_value=[item]), \
             patch.object(recovery.subprocess, "run", side_effect=download):
            result = recovery.download_platforms(proof, self.root)
            self.assertEqual(result["artifacts"][0]["digest"], digest)
            self.assertEqual((self.root / "native/payload.exe").read_bytes(), b"binary")
            self.assertTrue((self.root / "platform-reuse-provenance.json").is_file())

    def test_artifact_identity_expiry_ambiguity_and_digest_fail_closed(self):
        archive = self.archive()
        item = {"id": 7, "name": "native", "expired": False,
                "digest": "sha256:" + "0" * 64,
                "workflow_run": {"id": 42, "head_sha": "a" * 40}}
        inventories = [[item], [item, item], [{**item, "expired": True}],
                       [{**item, "workflow_run": {"id": 43, "head_sha": "a" * 40}}],
                       [{**item, "digest": None}]]
        for inventory in inventories:
            with self.subTest(inventory=inventory), \
                 patch.object(recovery, "ARTIFACTS", {"native": "native"}), \
                 patch.object(recovery, "collection", return_value=inventory), \
                 patch.object(recovery.subprocess, "run", side_effect=lambda args, stdout: (stdout.write(archive), SimpleNamespace(returncode=0))[1]), \
                 self.assertRaises(recovery.RecoveryError):
                recovery.download_platforms({"source_run_id": 42, "source_sha": "a" * 40}, self.root)


class ControllerTests(unittest.TestCase):
    def test_supplemental_qualification_dispatches_one_assembly_at_the_tooling_revision(self):
        product_sha, tooling_sha = 'a' * 40, 'b' * 40
        title = 'action:release:pool:public:tag:v3.0.51:assembly:source:42:qualification:84'
        completed = {'databaseId': 85, 'status': 'completed', 'conclusion': 'success'}
        proof = {'source_sha': product_sha, 'version': '3.0.51', 'qualification_run_id': 84,
                 'platforms_complete': True, 'artifact_preflight': {'ios': {'id': 7}}}
        with patch.object(controller, 'verify_platform_recovery', return_value=proof) as gate, \
             patch.object(recovery, 'github_token', return_value='fixture-token'), \
             patch('verify_embedded_release_ci.create_stable_tag') as freeze, \
             patch.object(controller, 'matching_packaging_repair_run', side_effect=[None, completed]) as matches, \
             patch.object(controller, 'gh_json', return_value={'commit': {'sha': tooling_sha}}), \
             patch.object(controller, 'ensure_remote_main_sha'), \
             patch.object(controller, 'dispatch', return_value=completed) as dispatch:
            proof['artifact_preflight'] = {}
            with self.assertRaisesRegex(controller.ReleaseError, 'artifact proof is incomplete'):
                controller.ensure_release('v3.0.51', product_sha, include_webos_release=True,
                                          reuse_platform_run_id=42, reuse_webos_run_id=42,
                                          reuse_repaired_run_id='qualification:84')
            freeze.assert_not_called()
            proof['artifact_preflight'] = {'ios': {'id': 7}}
            for _ in range(2):
                result = controller.ensure_release(
                    'v3.0.51', product_sha, include_webos_release=True, reuse_platform_run_id=42,
                    reuse_webos_run_id=42, reuse_repaired_run_id='qualification:84')
                self.assertEqual(result, completed)
        self.assertEqual([call.args for call in freeze.call_args_list],
                         [('fixture-token', 'v3.0.51', product_sha)] * 2)
        gate.assert_called_with(42, product_sha, '3.0.51', qualification_run=84, webos_run=42, android_run=42)
        self.assertEqual([call.args for call in matches.call_args_list], [(title,), (title,)])
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.kwargs['expected_sha'], tooling_sha)
        self.assertEqual(dispatch.call_args.kwargs['expected_title'], title)
        self.assertEqual(dispatch.call_args.args[3]['reuse_repaired_run_id'], 'qualification:84')

    def test_supplemental_qualification_dispatches_distinct_hosted_tvos_rebuild(self):
        product_sha, tooling_sha = 'a' * 40, 'b' * 40
        tvos_job = 'Build tvOS developer package (arm64)'
        title = ('action:release:pool:public:tag:v3.0.51:assembly:source:42:'
                 'qualification:84:rebuild-tvos')
        completed = {'databaseId': 86, 'status': 'completed', 'conclusion': 'success'}
        proof = {'source_sha': product_sha, 'version': '3.0.51', 'qualification_run_id': 84,
                 'platforms_complete': True, 'rebuild_tvos': True, 'rebuilt_jobs': [tvos_job],
                 'artifact_preflight': [{'name': 'ios-ipa', 'source_run_id': 42}]}
        with patch.object(controller, 'verify_platform_recovery', return_value=proof) as gate, \
             patch.object(recovery, 'github_token', return_value='fixture-token'), \
             patch('verify_embedded_release_ci.create_stable_tag') as freeze, \
             patch.object(controller, 'matching_packaging_repair_run', return_value=None), \
             patch.object(controller, 'gh_json', return_value={'commit': {'sha': tooling_sha}}), \
             patch.object(controller, 'ensure_remote_main_sha'), \
             patch.object(controller, 'dispatch', return_value=completed) as dispatch, \
             patch.object(controller, 'run_has_successful_job', return_value=True) as successful_job, \
             patch.object(controller, 'run_has_successful_step', return_value=True) as successful_step:
            result = controller.ensure_release(
                'v3.0.51', product_sha, include_webos_release=True,
                reuse_platform_run_id=42, reuse_webos_run_id=42,
                reuse_repaired_run_id='qualification:84', rebuild_tvos=True,
            )
        self.assertEqual(result, completed)
        gate.assert_called_once_with(42, product_sha, '3.0.51', qualification_run=84,
                                     rebuild_tvos=True, webos_run=42, android_run=42)
        freeze.assert_called_once_with('fixture-token', 'v3.0.51', product_sha)
        self.assertEqual(dispatch.call_args.kwargs['expected_title'], title)
        self.assertEqual(dispatch.call_args.kwargs['expected_sha'], tooling_sha)
        fields = dispatch.call_args.args[3]
        self.assertEqual(fields['reuse_repaired_run_id'], 'qualification:84')
        self.assertEqual(fields['reuse_platform_run_id'], '42')
        self.assertEqual(fields['reuse_android_run_id'], '42')
        self.assertEqual(fields['reuse_webos_run_id'], '42')
        self.assertEqual(fields['rebuild_tvos'], 'true')
        self.assertEqual(fields['tvos_runner_pool'], 'hosted')
        self.assertEqual(
            {call.args[3] for call in successful_step.call_args_list},
            {
                'Package unsigned tvOS developer IPA',
                'Verify unsigned tvOS runtime dependencies',
                'Attest unsigned tvOS checkpoint',
                'Upload durable unsigned tvOS checkpoint',
                'Verify tvOS runtime dependencies',
                'Attest unsigned tvOS developer package',
                'Upload tvOS package',
            },
        )
        successful_job.assert_called_with(controller.PLEZY_REPO, completed, tvos_job)

    def test_supplemental_tvos_rebuild_requires_only_the_rebuilt_tvos_in_its_proof(self):
        product_sha = 'a' * 40
        tvos_job = 'Build tvOS developer package (arm64)'
        valid_proof = {
            'source_sha': product_sha,
            'version': '3.0.51',
            'qualification_run_id': 84,
            'platforms_complete': True,
            'rebuild_tvos': True,
            'rebuilt_jobs': [tvos_job],
            'artifact_preflight': [{'name': 'ios-ipa', 'source_run_id': 42}],
        }
        invalid_proofs = [
            {**valid_proof, 'rebuild_tvos': False},
            {**valid_proof, 'rebuilt_jobs': []},
            {**valid_proof, 'artifact_preflight': [
                {'name': 'ios-ipa', 'source_run_id': 42},
                {'name': 'tvos-ipa', 'source_run_id': 42},
            ]},
        ]
        for proof in invalid_proofs:
            with self.subTest(proof=proof), \
                    patch.object(controller, 'verify_platform_recovery', return_value=proof), \
                    self.assertRaisesRegex(controller.ReleaseError, 'artifact proof is incomplete'):
                controller.ensure_release(
                    'v3.0.51', product_sha, include_webos_release=True,
                    reuse_platform_run_id=42, reuse_webos_run_id=42,
                    reuse_repaired_run_id='qualification:84', rebuild_tvos=True,
                )

    def test_supplemental_qualification_rejects_signing_or_sibling_substitution_with_tvos_rebuild(self):
        with self.assertRaisesRegex(controller.ReleaseError, 'only permits a verified tvOS acquisition rebuild'):
            controller.ensure_release(
                'v3.0.51', 'a' * 40, include_webos_release=True, include_apple_store_build=True,
                reuse_platform_run_id=42, reuse_webos_run_id=42,
                reuse_repaired_run_id='qualification:84', rebuild_tvos=True,
            )
        with self.assertRaisesRegex(controller.ReleaseError, 'only permits a verified tvOS acquisition rebuild'):
            controller.ensure_release(
                'v3.0.51', 'a' * 40, include_webos_release=True,
                reuse_platform_run_id=42, reuse_webos_run_id=99,
                reuse_repaired_run_id='qualification:84', rebuild_tvos=True,
            )

    @patch.object(controller, "verify_platform_recovery")
    @patch.object(controller, "verify_unacquired_hosted_tvos_run", return_value={"run_id": 77})
    @patch.object(controller, "matching_run")
    @patch.object(controller, "ensure_remote_main_sha")
    @patch.object(controller, "dispatch", return_value={
        "databaseId": 78, "status": "completed", "conclusion": "success",
    })
    def test_hosted_tvos_non_acquisition_does_not_auto_retry_private(
        self, dispatch, ensure_main, matching, _allocation_evidence, verify
    ):
        matching.side_effect = [
            {"databaseId": 77, "status": "completed", "conclusion": "failure"},
            None,
        ]

        with self.assertRaisesRegex(controller.ReleaseError, "never reroute to private JIT automatically"):
            controller.ensure_release("v3.0.0", "b" * 40, include_webos_release=True)

        ensure_main.assert_not_called()
        verify.assert_not_called()
        dispatch.assert_not_called()

    @patch.object(controller, "verify_platform_recovery")
    @patch.object(controller, "matching_run", return_value=None)
    @patch.object(controller, "ensure_remote_main_sha")
    @patch.object(controller, "dispatch")
    def test_tvos_only_recovery_explicitly_reuses_android_and_webos(self, dispatch, _main, _matching, verify):
        controller.ensure_release("v3.0.0", "b" * 40, include_webos_release=True,
                                  reuse_platform_run_id=42, reuse_android_run_id=42,
                                  reuse_webos_run_id=42, rebuild_tvos=True)
        verify.assert_called_once_with(42, "b" * 40, "3.0.0", rebuild_tvos=True,
                                       webos_run=42, android_run=42)
        fields = dispatch.call_args.args[3]
        self.assertEqual(fields["rebuild_tvos"], "true")
        self.assertEqual(fields["reuse_android_run_id"], "42")
        self.assertEqual(fields["reuse_webos_run_id"], "42")
        self.assertNotIn("rebuild_macos", fields)
        self.assertNotIn("rebuild_linux", fields)

    @patch.object(controller, "verify_platform_recovery")
    @patch.object(controller, "matching_run", return_value=None)
    @patch.object(controller, "ensure_remote_main_sha")
    @patch.object(controller, "dispatch")
    def test_tizen_only_recovery_reuses_successful_platform_outputs(self, dispatch, _main, _matching, verify):
        controller.ensure_release("v3.0.46", "b" * 40, include_webos_release=True,
                                  include_apple_store_build=True, prepare_homebrew_cask=True,
                                  reuse_platform_run_id=44907167387, rebuild_tizen=True)
        verify.assert_called_once_with(44907167387, "b" * 40, "3.0.46", rebuild_tizen=True,
                                       include_apple_store_build=True, require_homebrew_cask=True,
                                       webos_run=44907167387, android_run=44907167387)
        fields = dispatch.call_args.args[3]
        self.assertEqual(fields["reuse_repaired_run_id"], "tizen-rebuild")
        self.assertEqual(fields["reuse_platform_run_id"], "44907167387")
        self.assertEqual(fields["include_apple_store_build"], "true")
        self.assertEqual(fields["prepare_homebrew_cask"], "true")
        self.assertNotIn("rebuild_android", fields)
        self.assertNotIn("rebuild_linux", fields)

    @patch.object(controller, "verify_platform_recovery")
    @patch.object(controller, "matching_run", return_value=None)
    @patch.object(controller, "ensure_remote_main_sha")
    @patch.object(controller, "dispatch")
    def test_macos_recovery_reuses_android_and_gets_a_distinct_run_name(
        self, dispatch, _main, _matching, verify
    ):
        controller.ensure_release(
            "v3.0.44", "b" * 40, include_webos_release=True,
            reuse_platform_run_id=44809271478, rebuild_macos=True,
        )

        verify.assert_called_once_with(
            44809271478, "b" * 40, "3.0.44", rebuild_macos=True,
            webos_run=44809271478, android_run=44809271478,
        )
        fields = dispatch.call_args.args[3]
        self.assertEqual(fields["rebuild_macos"], "true")
        self.assertEqual(fields["reuse_platform_run_id"], "44809271478")
        self.assertEqual(fields["reuse_android_run_id"], "44809271478")
        self.assertEqual(fields["reuse_webos_run_id"], "44809271478")
        self.assertEqual(
            dispatch.call_args.kwargs["expected_title"],
            "action:release:pool:public:tag:v3.0.44:recovery:source:44809271478:macos",
        )

    @patch.object(controller, "verify_platform_recovery")
    @patch.object(controller, "matching_run", return_value=None)
    @patch.object(controller, "ensure_remote_main_sha")
    @patch.object(controller, "dispatch")
    def test_webos_recovery_dispatches_only_webos_rebuild(self, dispatch, _main, _matching, verify):
        controller.ensure_release("v3.0.0", "b" * 40, include_webos_release=True,
                                  reuse_platform_run_id=42, reuse_android_run_id=42,
                                  rebuild_webos=True)
        verify.assert_called_once_with(42, "b" * 40, "3.0.0", android_run=42)
        fields = dispatch.call_args.args[3]
        self.assertEqual(fields["reuse_platform_run_id"], "42")
        self.assertEqual(fields["reuse_android_run_id"], "42")
        self.assertEqual(fields["include_webos_build"], "true")
        self.assertEqual(dispatch.call_args.kwargs["expected_title"],
                         "action:release:pool:public:tag:v3.0.0")

    @patch.object(controller, "verify_platform_recovery")
    @patch.object(controller, "matching_run", return_value=None)
    @patch.object(controller, "ensure_remote_main_sha")
    @patch.object(controller, "dispatch")
    def test_recovery_dispatch_reuses_android_and_webos_from_verified_run(self, dispatch, _main, _matching, verify):
        controller.ensure_release("v3.0.0", "b" * 40, include_webos_release=True,
                                  reuse_platform_run_id=42)
        verify.assert_called_once_with(42, "b" * 40, "3.0.0", webos_run=42, android_run=42)
        self.assertEqual(dispatch.call_args.args[3]["reuse_platform_run_id"], "42")
        self.assertEqual(dispatch.call_args.args[3]["reuse_android_run_id"], "42")
        self.assertEqual(dispatch.call_args.kwargs["expected_title"],
                         "action:release:pool:public:tag:v3.0.0")
        self.assertEqual(dispatch.call_args.kwargs["expected_sha"], "b" * 40)
        self.assertEqual(dispatch.call_args.args[3]["include_webos_release"], "true")
        self.assertEqual(dispatch.call_args.args[3]["include_webos_build"], "true")

    def test_recovery_rejects_signed_store_mode(self):
        with self.assertRaises(controller.ReleaseError):
            controller.ensure_release("v3.0.0", "b" * 40, include_webos_release=True,
                                      include_apple_store_build=True, reuse_platform_run_id=42)

    @patch.object(controller, "verify_platform_recovery")
    @patch.object(controller, "matching_run", return_value=None)
    @patch.object(controller, "ensure_remote_main_sha")
    @patch.object(controller, "dispatch")
    def test_recovery_cannot_omit_required_webos_artifact_or_build(
        self, dispatch, _main, _matching, verify
    ):
        with self.assertRaises(controller.ReleaseError):
            controller.ensure_release("v3.0.0", "b" * 40,
                include_webos_release=False, include_webos_build=True,
                reuse_platform_run_id=42)
        verify.assert_not_called()

        controller.ensure_release("v3.0.0", "b" * 40,
            include_webos_release=True, include_webos_build=False,
            reuse_platform_run_id=42)
        verify.assert_called_once_with(42, "b" * 40, "3.0.0", webos_run=42, android_run=42)
        self.assertEqual(dispatch.call_args.args[3]["reuse_webos_run_id"], "42")
        self.assertEqual(dispatch.call_args.args[3]["reuse_android_run_id"], "42")
        self.assertEqual(dispatch.call_args.args[3]["include_webos_build"], "true")


class AndroidSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = {"status": "completed", "event": "workflow_dispatch", "head_branch": "main",
                       "head_repository": {"full_name": recovery.REPOSITORY},
                       "path": ".github/workflows/build.yml", "head_sha": "a" * 40,
                       "display_title": "action:release:pool:private:tag:v3.0.0"}
        self.compare = {"status": "ahead", "merge_base_commit": {"sha": "a" * 40}, "files": []}
        self.jobs = [{"id": 71, "name": "build-android", "status": "completed", "conclusion": "success"},
                     {"name": "create-release", "conclusion": "skipped"}]

    def prove(self, *, run_id=84, head_sha="b" * 40, version="3.0.0", **options):
        with patch.object(recovery, "api", side_effect=[self.source, self.compare]), \
             patch.object(recovery, "collection", return_value=self.jobs):
            return recovery.verify_android_run(run_id, head_sha, version, **options)

    def test_actual_public_candidate_run_shape_is_accepted_for_android_reuse(self):
        source_sha = "2388a06cc74d03e69ac7178fcd927a54460a5e3a"
        self.source.update(
            id=43826939425,
            head_sha=source_sha,
            display_title="action:release:pool:public:tag:v3.0.36",
        )
        self.compare = {"status": "identical", "merge_base_commit": {"sha": source_sha}, "files": []}

        proof = self.prove(run_id=43826939425, head_sha=source_sha, version="3.0.36")

        self.assertEqual(proof["source_run_id"], 43826939425)
        self.assertEqual(proof["source_sha"], source_sha)

    def test_android_reuse_rejects_unexpected_public_title(self):
        self.source["display_title"] = "action:release:pool:public:tag:v3.0.1"
        with self.assertRaises(recovery.RecoveryError):
            self.prove()

    def test_pending_android_is_deferred_preflight_evidence_and_never_a_final_success(self):
        self.source["status"] = "in_progress"
        self.jobs[0].update(status="in_progress", conclusion=None)
        self.assertTrue(self.prove(allow_pending=True)["pending"])
        with self.assertRaises(recovery.RecoveryError):
            self.prove()
        self.jobs[0].update(status="completed", conclusion="failure")
        with self.assertRaisesRegex(recovery.RecoveryError, "uniquely successful"):
            self.prove(allow_pending=True)
        self.jobs[0]["conclusion"] = "success"
        self.assertNotIn("pending", self.prove(allow_pending=True))

    def test_reuses_only_successful_android_from_equivalent_product_inputs(self):
        self.assertEqual(self.prove()["job_id"], 71)
        self.compare["files"] = [{"filename": "android/app/build.gradle.kts"}]
        with self.assertRaisesRegex(recovery.RecoveryError, "input equivalence"):
            self.prove()

    def test_failed_or_untrusted_android_cannot_be_reused(self):
        for conclusion in ("failure", "cancelled", "timed_out"):
            with self.subTest(conclusion=conclusion):
                self.jobs[0]["conclusion"] = conclusion
                with self.assertRaisesRegex(recovery.RecoveryError, "uniquely successful"):
                    self.prove()
        self.source["head_branch"] = "feature"
        with self.assertRaisesRegex(recovery.RecoveryError, "trusted-main"):
            self.prove()

    def test_composition_records_optional_exclusions_and_real_android_source(self):
        with patch.object(recovery, "verify_android_run", return_value={"source_run_id": 84}):
            proof = recovery.compose_proof({"head_sha": "b" * 40, "version": "3.0.0"}, 84, False)
        self.assertEqual(proof["rebuilt_jobs"], [])
        self.assertEqual(proof["android_source"]["source_run_id"], 84)
        self.assertEqual(proof["excluded_optional_jobs"], ["Build WebOS TV package (ARMv7)"])

    def test_pending_source_android_cannot_trigger_a_duplicate_fresh_android_build(self):
        with self.assertRaisesRegex(recovery.RecoveryError, "duplicate build"):
            recovery.compose_proof({"pending_jobs": ["build-android"]})



class UnsignedIOSReuseTests(unittest.TestCase):
    def test_verifies_trusted_successful_developer_ipa_source(self):
        source = {"id": 44691563399, "status": "completed", "event": "workflow_dispatch",
                  "head_branch": "main", "head_repository": {"full_name": recovery.REPOSITORY},
                  "path": ".github/workflows/build.yml@refs/heads/main", "head_sha": "a" * 40,
                  "run_attempt": 1, "display_title": "action:release:pool:public:tag:v3.0.42"}
        lineage = {"status": "ahead", "merge_base_commit": {"sha": "a" * 40},
                   "files": [{"filename": "scripts/tv_mpv/build.py"}]}
        steps = [{"name": name, "status": "completed", "conclusion": "success"} for name in (
            "Build iOS (no codesign)", "Create unsigned developer IPA",
            "Retain unsigned iOS developer checkpoint", "Attest unsigned developer IPA", "Upload IPA")]
        job = {"id": 10, "name": "build-ios", "status": "completed", "conclusion": "success", "steps": steps}
        artifact = {"id": 11, "name": "ios-ipa", "expired": False,
                    "digest": "sha256:" + "c" * 64,
                    "workflow_run": {"id": 44691563399, "head_sha": "a" * 40}}
        with patch.object(recovery, "api", side_effect=[source, lineage]), \
             patch.object(recovery, "collection", side_effect=[[job], [artifact]]):
            result = recovery.verify_unsigned_ios_run(44691563399, "b" * 40, "3.0.42")
        self.assertEqual(result["artifact_id"], 11)
        self.assertEqual(result["source_sha"], "a" * 40)

    @patch.object(controller, "verify_platform_recovery")
    @patch.object(controller, "matching_run", return_value=None)
    @patch.object(controller, "ensure_remote_main_sha")
    @patch.object(controller, "dispatch")
    def test_unsigned_ios_reuse_dispatches_typed_provenance_and_skips_rebuild(
            self, dispatch, _main, _matching, verify):
        controller.ensure_release("v3.0.42", "b" * 40, include_webos_release=True,
                                  reuse_platform_run_id=42, reuse_android_run_id=42,
                                  reuse_ios_developer_run_id=43)
        verify.assert_called_once_with(42, "b" * 40, "3.0.42", android_run=42,
                                       ios_developer_run=43, webos_run=42)
        fields = dispatch.call_args.args[3]
        self.assertEqual(fields["reuse_repaired_run_id"], "ios-developer:43")
        self.assertEqual(fields["reuse_platform_run_id"], "42")
        self.assertEqual(fields["reuse_webos_run_id"], "42")
        self.assertEqual(fields["include_webos_build"], "true")
        self.assertNotIn("rebuild_ios", fields)



class RepairedSourceTests(unittest.TestCase):
    def setUp(self):
        recovery.immutable_content.cache_clear()
        self.run = {"status": "completed", "event": "workflow_dispatch", "head_branch": "main",
                    "head_repository": {"full_name": recovery.REPOSITORY}, "head_sha": "a" * 40,
                    "path": ".github/workflows/build.yml", "display_title": "action:release:pool:private:tag:v3.0.7"}
        self.compare = {"status": "ahead", "merge_base_commit": {"sha": "a" * 40}, "files": []}
        self.jobs = [{"name": name, "status": "completed", "conclusion": "success"} for name in
                     ("Validate release request", "Build Linux (x64)", "Build Linux (arm64)", "build-macos")]
        self.jobs[-1]["steps"] = [{"name": name, "status": "completed", "conclusion": "success"} for name in
                                  ("Build macOS", "Notarize Application", "Wait for notarization acceptance", "Verify Gatekeeper acceptance for Homebrew Cask")]
        self.jobs += [{"name": "Build WebOS TV package (ARMv7)", "status": "completed", "conclusion": "failure"},
                      {"name": "create-release", "status": "completed", "conclusion": "skipped"}]
        self.workflow = 'env:\n  SDK: pinned\njobs:\n  build-macos:\n    run: compile-mac\n  build-linux:\n    run: compile-linux\n  other:\n'

    def prove(self, *, workflows=None, run_id=84, head_sha="b" * 40, version="3.0.7", **options):
        with patch.object(recovery, "api", side_effect=[self.run, self.compare]), \
             patch.object(recovery, "collection", return_value=self.jobs), \
             patch.object(recovery, "immutable_content", side_effect=workflows or [self.workflow, self.workflow]):
            return recovery.verify_repaired_run(run_id, head_sha, version, recovery.REPOSITORY, **options)

    def test_unrelated_failure_does_not_erase_the_three_verified_repaired_jobs(self):
        proof = self.prove(require_homebrew_cask=True)
        self.assertTrue(proof["platforms_complete"])
        self.assertTrue(proof["macos_cask_qualified"])
        self.assertEqual(len(proof["reused_jobs"]), 3)

    def test_pending_mac_is_allowed_only_in_preflight(self):
        self.run["status"] = "in_progress"
        self.jobs[3].update(status="queued", conclusion=None, steps=[])
        self.assertEqual(self.prove(allow_pending=True)["pending_jobs"], ["build-macos"])
        with self.assertRaises(recovery.RecoveryError):
            self.prove()

    def test_missing_mac_qualification_or_a_failed_repaired_job_is_rejected(self):
        self.jobs[3]["steps"].pop()
        with self.assertRaisesRegex(recovery.RecoveryError, "qualification"):
            self.prove(require_homebrew_cask=True)
        self.jobs[1]["conclusion"] = "failure"
        with self.assertRaisesRegex(recovery.RecoveryError, "did not succeed"):
            self.prove()

    def test_changed_compiler_contract_inputs_or_publication_are_rejected(self):
        with self.assertRaisesRegex(recovery.RecoveryError, "contracts changed"):
            self.prove(workflows=[self.workflow, self.workflow.replace('compile-mac', 'different-flags')])
        self.compare["files"] = [{"filename": "lib/main.dart"}]
        with self.assertRaisesRegex(recovery.RecoveryError, "inputs changed"):
            self.prove()
        self.compare["files"] = []
        self.jobs[-1]["conclusion"] = "success"
        with self.assertRaisesRegex(recovery.RecoveryError, "publication"):
            self.prove()

    def test_webos_can_be_reused_after_assembly_failed_before_publishing(self):
        source_sha = "2388a06cc74d03e69ac7178fcd927a54460a5e3a"
        self.run.update(
            id=43826939425,
            head_sha=source_sha,
            conclusion="failure",
            display_title="action:release:pool:public:tag:v3.0.36",
        )
        self.compare = {"status": "identical", "merge_base_commit": {"sha": source_sha}, "files": []}
        self.jobs = [self.jobs[0], {"name": "Build WebOS TV package (ARMv7)", "status": "completed", "conclusion": "success"},
                     {"name": "create-release", "status": "completed", "conclusion": "failure",
                      "steps": [{"name": "Create Release", "conclusion": "skipped"}]}]
        before = 'env:\n  SDK: pinned\njobs:\n  build-webos:\n    if: ${{ trusted }}\n    run: compile-webos\n  other:\n'
        after = before.replace('if: ${{ ', "if: ${{ inputs.reuse_webos_run_id == '' && ")
        proof = self.prove(workflows=[before, after], webos_only=True,
                           run_id=43826939425, head_sha=source_sha, version="3.0.36")
        self.assertEqual(proof["reused_jobs"], ["Build WebOS TV package (ARMv7)"])
        self.jobs[-1]["steps"][0]["conclusion"] = "failure"
        with self.assertRaisesRegex(recovery.RecoveryError, "publication"):
            self.prove(workflows=[before, after], webos_only=True,
                       run_id=43826939425, head_sha=source_sha, version="3.0.36")

    def test_webos_reuse_rejects_unexpected_public_title(self):
        self.run["display_title"] = "action:release:pool:public:tag:v3.0.8"
        with self.assertRaises(recovery.RecoveryError):
            self.prove(webos_only=True)


class AssemblyCheckoutTests(unittest.TestCase):
    def test_actual_sparse_checkout_imports_recovery_and_all_transitive_helpers(self):
        import subprocess
        root = Path(recovery.__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/build.yml').read_text().split('\n  create-release:\n', 1)[1]
        selection = workflow.split('          sparse-checkout: |\n', 1)[1].split('          sparse-checkout-cone-mode:', 1)[0]
        patterns = [line.strip() for line in selection.splitlines() if line.strip()]
        tracked = sorted(set(
            subprocess.check_output(['git', 'ls-files', '-z', '--', 'scripts'], cwd=root).decode().split('\0')
            + ['scripts/verify_tvos_runner_acquisition.py']
        ))
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            for name in [name for name in tracked if name] + [name for name in patterns if '*' not in name]:
                target = checkout / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / name, target)
            def git(*args, **kwargs):
                return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                                       '-c', 'core.autocrlf=false', *args], cwd=checkout, capture_output=True, text=True, check=True, **kwargs)
            git('init', '-q')
            git('add', '.')
            git('-c', 'user.name=Assembly Test', '-c', 'user.email=assembly-test@example.invalid', 'commit', '-qm', 'fixture')
            git('sparse-checkout', 'set', '--no-cone', '--stdin', input='\n'.join(patterns) + '\n')
            code = "import sys; sys.path.insert(0, 'scripts'); import recover_release_platforms, release_all_platforms, verify_webos_link_patch; recover_release_platforms.release_ci._manifest_gate()"
            result = subprocess.run([sys.executable, '-I', '-c', code], cwd=checkout, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
