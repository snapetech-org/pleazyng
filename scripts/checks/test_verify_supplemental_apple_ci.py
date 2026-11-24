"""Supplemental hosted Apple CI must prove the exact frozen source and suites."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import verify_embedded_release_ci as embedded
from verify_release_ci import GateError, _manifest_gate


SOURCE_SHA = 'c' * 40
TOOLING_SHA = 'd' * 40
SOURCE_RUN_ID = 45421474803
QUALIFICATION_RUN_ID = 45421479999
RETAINED_RUN_ID = 45421479988
RETAINED_TOOLING_SHA = 'b' * 40
QUALIFICATION_WORKFLOW_ID = 887766


def _step(name, conclusion='success'):
    return {'name': name, 'status': 'completed', 'conclusion': conclusion}


def _suite(name, job_id):
    return {
        'id': job_id, 'run_attempt': 1, 'name': name,
        'status': 'completed', 'conclusion': 'success',
        'labels': ['xcode-27'],
        'runner_group_name': 'GitHub Actions',
        'steps': [_step(step) for step in embedded.APPLE_QUALIFICATION_STEPS],
    }


class SupplementalAppleQualificationTests(unittest.TestCase):
    def setUp(self):
        self.source_run = {
            'id': SOURCE_RUN_ID, 'run_attempt': 3,
            'event': 'workflow_dispatch', 'head_branch': 'main', 'head_sha': SOURCE_SHA,
            'path': '.github/workflows/build.yml@refs/heads/main',
            'repository': {'id': embedded.REPOSITORY_ID, 'full_name': embedded.REPOSITORY},
            'head_repository': {'id': embedded.REPOSITORY_ID, 'full_name': embedded.REPOSITORY},
            'status': 'queued', 'conclusion': None,
        }
        self.qualification_run = {
            'id': QUALIFICATION_RUN_ID, 'run_attempt': 1,
            'workflow_id': QUALIFICATION_WORKFLOW_ID,
            'event': 'workflow_dispatch', 'head_branch': 'main', 'head_sha': TOOLING_SHA,
            'path': embedded.APPLE_QUALIFICATION_WORKFLOW + '@refs/heads/main',
            'display_title': f'apple-candidate-qualification:{SOURCE_RUN_ID}:{SOURCE_SHA}',
            'repository': {'id': embedded.REPOSITORY_ID, 'full_name': embedded.REPOSITORY},
            'head_repository': {'id': embedded.REPOSITORY_ID, 'full_name': embedded.REPOSITORY},
            'status': 'completed', 'conclusion': 'success',
        }
        self.workflow = {
            'id': QUALIFICATION_WORKFLOW_ID,
            'path': embedded.APPLE_QUALIFICATION_WORKFLOW,
            'state': 'active',
        }
        self.jobs = [_suite(name, index + 1) for index, name in enumerate(embedded.APPLE_PLATFORM_JOBS)]

    def _api(self, path, _token):
        if path == f'actions/runs/{SOURCE_RUN_ID}':
            return deepcopy(self.source_run)
        if path == f'actions/runs/{QUALIFICATION_RUN_ID}':
            return deepcopy(self.qualification_run)
        if path == f'actions/workflows/{QUALIFICATION_WORKFLOW_ID}':
            return deepcopy(self.workflow)
        if path == f'compare/{SOURCE_SHA}...main':
            return {'status': 'ahead', 'base_commit': {'sha': SOURCE_SHA}}
        if path == f'compare/{TOOLING_SHA}...main':
            return {'status': 'ahead', 'base_commit': {'sha': TOOLING_SHA}}
        raise AssertionError(f'Unexpected API path {path}')

    def _pages(self, url, _token, collection, **_options):
        self.assertEqual(collection, 'jobs')
        self.assertIn(
            f'/actions/runs/{QUALIFICATION_RUN_ID}/attempts/1/jobs', url
        )
        return deepcopy(self.jobs)

    def verify(self, *, repository=embedded.REPOSITORY, source_sha=SOURCE_SHA,
               source_run_id=SOURCE_RUN_ID, qualification_run_id=QUALIFICATION_RUN_ID):
        with patch.object(embedded, 'api', side_effect=self._api), \
                patch.object(embedded, '_pages', side_effect=self._pages):
            return embedded.verify_supplemental_apple_ci(
                repository, source_sha, source_run_id, qualification_run_id, 'fixture-token'
            )

    def test_returns_run_binding_and_three_real_suite_records(self):
        proof = self.verify()
        self.assertEqual(proof['mode'], 'supplemental-apple')
        self.assertEqual(proof['source_sha'], SOURCE_SHA)
        self.assertEqual(proof['source_run_id'], SOURCE_RUN_ID)
        self.assertEqual(proof['qualification_run_id'], QUALIFICATION_RUN_ID)
        self.assertEqual(proof['tooling_sha'], TOOLING_SHA)
        self.assertEqual({job['name'] for job in proof['jobs']}, set(embedded.APPLE_PLATFORM_JOBS))
        self.assertTrue(all(job['passed_steps'] == list(embedded.APPLE_QUALIFICATION_STEPS)
                            for job in proof['jobs']))

    def test_accepts_exact_hosted_macos_26_label_with_runtime_arm_check(self):
        self.jobs[0]['labels'] = ['macos-26']
        proof = self.verify()
        self.assertEqual(proof['jobs'][0]['labels'], ['macos-26'])

    def _focused_inputs(self):
        previous = {
            **self.qualification_run, 'id': RETAINED_RUN_ID, 'head_sha': RETAINED_TOOLING_SHA,
            'run_attempt': 2, 'conclusion': 'failure',
        }
        self.qualification_run['display_title'] += f':resume:{RETAINED_RUN_ID}:ios'
        older_failure = {**self.jobs[0], 'run_attempt': 2, 'conclusion': 'failure'}
        retained_attempts = [deepcopy(self.jobs), [older_failure]]
        self.jobs = [{**self.jobs[0], 'id': 40,
                      'steps': [_step(name) for name in embedded.APPLE_FOCUSED_QUALIFICATION_STEPS]}]
        return previous, retained_attempts

    def _verify_focused(self, previous, retained_attempts):
        normal_api, normal_pages = self._api, self._pages

        def fixture_api(path, token):
            if path == f'actions/runs/{RETAINED_RUN_ID}':
                return deepcopy(previous)
            if path == f'compare/{RETAINED_TOOLING_SHA}...main':
                return {'status': 'ahead', 'base_commit': {'sha': RETAINED_TOOLING_SHA}}
            return normal_api(path, token)

        def fixture_pages(url, token, collection, **options):
            for number, jobs in enumerate(retained_attempts, 1):
                if f'/runs/{RETAINED_RUN_ID}/attempts/{number}/jobs' in url:
                    return deepcopy(jobs)
            return normal_pages(url, token, collection, **options)

        with (
            patch.object(embedded, 'api', side_effect=fixture_api),
            patch.object(embedded, '_pages', side_effect=fixture_pages),
        ):
            return embedded.verify_supplemental_apple_ci(
                embedded.REPOSITORY, SOURCE_SHA, SOURCE_RUN_ID, QUALIFICATION_RUN_ID, 'fixture-token')

    def test_focused_repair_preserves_exact_sibling_run_and_attempt_provenance(self):
        previous, attempts = self._focused_inputs()
        proof = self._verify_focused(previous, attempts)
        by_name = embedded._supplemental_apple_jobs(proof, SOURCE_SHA, SOURCE_RUN_ID, 3)
        ios, macos, tvos = (by_name[name] for name in embedded.APPLE_PLATFORM_JOBS)
        self.assertEqual((ios['id'], ios['run_id'], ios['tooling_sha']), (40, QUALIFICATION_RUN_ID, TOOLING_SHA))
        for job in (macos, tvos):
            self.assertEqual((job['run_id'], job['attempt'], job['qualification_attempt'], job['tooling_sha']),
                             (RETAINED_RUN_ID, 1, 2, RETAINED_TOOLING_SHA))

    def test_focused_repair_rejects_mismatched_retained_source_or_superseded_pass(self):
        previous, attempts = self._focused_inputs()
        with self.assertRaises(GateError):
            self._verify_focused({**previous, 'display_title': previous['display_title'].replace(SOURCE_SHA, 'a' * 40)},
                                 attempts)
        attempts[1].append({**attempts[0][1], 'run_attempt': 2, 'conclusion': 'failure'})
        with self.assertRaises(GateError):
            self._verify_focused(previous, attempts)

    def test_focused_repair_requires_retained_sibling_preflight(self):
        previous, attempts = self._focused_inputs()
        self.jobs[0]['steps'][1] = _step('Verify retained Apple siblings', 'skipped')
        with self.assertRaises(GateError):
            self._verify_focused(previous, attempts)

    def test_rejects_wrong_repository_source_or_run_ids(self):
        for arguments in (
            {'repository': 'somewhere/else'}, {'source_sha': 'e' * 40},
            {'source_run_id': True}, {'qualification_run_id': 0},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(GateError):
                self.verify(**arguments)

    def test_rejects_incompatible_original_source_run_identity(self):
        for field, value in (
            ('id', SOURCE_RUN_ID + 1), ('event', 'push'), ('head_branch', 'feature'),
            ('head_sha', 'e' * 40), ('path', '.github/workflows/ci.yml'),
            ('repository', {'id': 1, 'full_name': 'someone/else'}),
            ('head_repository', {'id': 1, 'full_name': 'someone/else'}),
            ('run_attempt', 101),
        ):
            with self.subTest(field=field):
                self.source_run[field] = value
                with self.assertRaises(GateError):
                    self.verify()
                self.setUp()

    def test_rejects_source_or_tooling_commit_outside_main_ancestry(self):
        original = self._api

        def divergent(path, token):
            if path == f'compare/{SOURCE_SHA}...main':
                return {'status': 'diverged', 'base_commit': {'sha': SOURCE_SHA}}
            return original(path, token)

        with patch.object(embedded, 'api', side_effect=divergent), \
                patch.object(embedded, '_pages', side_effect=self._pages), \
                self.assertRaises(GateError):
            embedded.verify_supplemental_apple_ci(
                embedded.REPOSITORY, SOURCE_SHA, SOURCE_RUN_ID, QUALIFICATION_RUN_ID, 'fixture-token'
            )

        def tooling_divergent(path, token):
            if path == f'compare/{TOOLING_SHA}...main':
                return {'status': 'diverged', 'base_commit': {'sha': TOOLING_SHA}}
            return original(path, token)

        with patch.object(embedded, 'api', side_effect=tooling_divergent), \
                patch.object(embedded, '_pages', side_effect=self._pages), \
                self.assertRaises(GateError):
            embedded.verify_supplemental_apple_ci(
                embedded.REPOSITORY, SOURCE_SHA, SOURCE_RUN_ID, QUALIFICATION_RUN_ID, 'fixture-token'
            )

    def test_rejects_foreign_or_renamed_supplemental_run(self):
        changes = (
            {'id': QUALIFICATION_RUN_ID + 1}, {'event': 'pull_request'},
            {'head_branch': 'feature'},
            {'path': '.github/workflows/ci.yml'},
            {'display_title': f'apple-candidate-qualification:{SOURCE_RUN_ID}:frozen'},
            {'status': 'in_progress', 'conclusion': None},
            {'conclusion': 'failure'},
            {'repository': {'id': 1, 'full_name': 'someone/else'}},
            {'head_repository': {'id': 1, 'full_name': 'someone/else'}},
        )
        for change in changes:
            with self.subTest(change=change):
                self.qualification_run.update(change)
                with self.assertRaises(GateError):
                    self.verify()
                self.setUp()

    def test_rejects_disabled_or_mispointed_workflow(self):
        for change in ({'path': '.github/workflows/ci.yml'}, {'state': 'disabled'}, {'id': 3}):
            with self.subTest(change=change):
                self.workflow.update(change)
                with self.assertRaises(GateError):
                    self.verify()
                self.setUp()

    def test_rejects_missing_duplicate_or_unexpected_suites(self):
        variants = [self.jobs[:-1], self.jobs + [deepcopy(self.jobs[0])],
                    self.jobs + [_suite('Apple native reliability', 99)]]
        for jobs in variants:
            with self.subTest(names=[job['name'] for job in jobs]):
                self.jobs = jobs
                with self.assertRaises(GateError):
                    self.verify()
                self.setUp()

    def test_rejects_failed_job_steps_and_non_hosted_or_unapproved_runner(self):
        variants = []
        variants.append([{**self.jobs[0], 'conclusion': 'failure'}, *self.jobs[1:]])
        variants.append([{**self.jobs[0], 'steps': self.jobs[0]['steps'][:-1]}, *self.jobs[1:]])
        variants.append([{**self.jobs[0], 'steps': [_step(embedded.APPLE_QUALIFICATION_STEPS[0], 'failure'),
                                                      *self.jobs[0]['steps'][1:]]}, *self.jobs[1:]])
        variants.append([{**self.jobs[0], 'labels': ['xcode-26']}, *self.jobs[1:]])
        variants.append([{**self.jobs[0], 'labels': ['self-hosted', 'xcode-27']}, *self.jobs[1:]])
        variants.append([{**self.jobs[0], 'steps': [_step(embedded.APPLE_QUALIFICATION_STEPS[0]),
                                                      _step(embedded.APPLE_QUALIFICATION_STEPS[1], 'failure'),
                                                      *self.jobs[0]['steps'][2:]]}, *self.jobs[1:]])
        variants.append([{**self.jobs[0], 'runner_group_name': 'Private JIT'}, *self.jobs[1:]])
        for jobs in variants:
            with self.subTest(job=jobs[0]):
                self.jobs = jobs
                with self.assertRaises(GateError):
                    self.verify()
                self.setUp()

    def test_failed_original_apple_jobs_are_preserved_and_only_apple_can_be_replaced(self):
        apple = self.verify()
        required = _manifest_gate()['requiredJobs']
        required = ['Apple native reliability'] + [
            name for name in required if name != 'Apple native reliability'
        ]
        original_jobs = [
            {'id': i + 100, 'run_attempt': 3, 'name': embedded.PREFIX + name,
             'status': 'completed', 'conclusion': 'success'}
            for i, name in enumerate(required)
        ]
        failed_names = {
            'Apple native reliability (iOS)': ('completed', 'failure'),
            'Apple native reliability (macOS)': ('completed', 'cancelled'),
            'Apple native reliability (tvOS)': ('completed', 'cancelled'),
            'Apple native reliability': ('completed', 'failure'),
        }
        for job in original_jobs:
            name = job['name'][len(embedded.PREFIX):]
            if name in failed_names:
                job['status'], job['conclusion'] = failed_names[name]
        original_jobs.extend(
            {'id': 900 + i, 'run_attempt': 3, 'name': f'Candidate isolation / guard {i}',
             'status': 'completed', 'conclusion': 'success'} for i in range(4)
        )
        original_jobs.extend(
            {'id': 910 + i, 'run_attempt': 3, 'name': f'Candidate security / guard {i}',
             'status': 'completed', 'conclusion': 'success'} for i in range(2)
        )
        source_run = {
            'id': SOURCE_RUN_ID, 'run_attempt': 3, 'event': 'workflow_dispatch',
            'head_branch': 'main', 'head_sha': SOURCE_SHA, 'path': '.github/workflows/build.yml',
            'repository': {'id': embedded.REPOSITORY_ID},
            'head_repository': {'id': embedded.REPOSITORY_ID},
        }
        proof = embedded.verify_jobs(
            source_run, original_jobs, SOURCE_SHA, SOURCE_RUN_ID, 3, required,
            require_candidate_guards=True, supplemental_apple=apple,
        )
        by_name = {job['name'].removeprefix(embedded.PREFIX): job for job in proof['jobs']
                   if job['name'].startswith(embedded.PREFIX)}
        self.assertEqual(by_name['Apple native reliability (iOS)']['conclusion'], 'failure')
        self.assertEqual(by_name['Apple native reliability (iOS)']['supplemental_qualification']['job_id'], 1)
        self.assertEqual(by_name['Apple native reliability (macOS)']['conclusion'], 'cancelled')
        self.assertEqual(by_name['Apple native reliability (tvOS)']['conclusion'], 'cancelled')
        self.assertEqual(by_name['Apple native reliability']['conclusion'], 'failure')
        self.assertEqual(by_name['Apple native reliability']['derived_conclusion'], 'success')
        self.assertEqual(len(proof['supplemental_apple']['jobs']), 3)

    def test_original_non_apple_failure_and_missing_candidate_guards_still_block(self):
        apple = self.verify()
        required = _manifest_gate()['requiredJobs']
        jobs = [{'id': i + 1, 'run_attempt': 3, 'name': embedded.PREFIX + name,
                 'status': 'completed', 'conclusion': 'success'} for i, name in enumerate(required)]
        jobs[0]['conclusion'] = 'failure'
        run = {'id': SOURCE_RUN_ID, 'run_attempt': 3, 'event': 'workflow_dispatch',
               'head_branch': 'main', 'head_sha': SOURCE_SHA, 'path': '.github/workflows/build.yml',
               'repository': {'id': embedded.REPOSITORY_ID},
               'head_repository': {'id': embedded.REPOSITORY_ID}}
        with self.assertRaises(GateError):
            embedded.verify_jobs(run, jobs, SOURCE_SHA, SOURCE_RUN_ID, 3, required,
                                 supplemental_apple=apple)
        with self.assertRaises(GateError):
            embedded.verify_jobs(run, jobs, SOURCE_SHA, SOURCE_RUN_ID, 3, required,
                                 require_candidate_guards=True, supplemental_apple=apple)

    def test_supplemental_proof_cannot_bind_to_another_source_or_non_apple_job(self):
        apple = self.verify()
        required = _manifest_gate()['requiredJobs']
        jobs = [{'id': i + 1, 'run_attempt': 3, 'name': embedded.PREFIX + name,
                 'status': 'completed', 'conclusion': 'success'} for i, name in enumerate(required)]
        run = {'id': SOURCE_RUN_ID, 'run_attempt': 3, 'event': 'workflow_dispatch',
               'head_branch': 'main', 'head_sha': SOURCE_SHA, 'path': '.github/workflows/build.yml',
               'repository': {'id': embedded.REPOSITORY_ID},
               'head_repository': {'id': embedded.REPOSITORY_ID}}
        for change in ({'source_sha': 'f' * 40}, {'source_run_id': SOURCE_RUN_ID + 1},
                       {'source_run_attempt': 2}, {'workflow': '.github/workflows/build.yml'}):
            with self.subTest(change=change), self.assertRaises(GateError):
                embedded.verify_jobs(run, jobs, SOURCE_SHA, SOURCE_RUN_ID, 3, required,
                                     supplemental_apple={**apple, **change})
        malformed = deepcopy(apple)
        malformed['jobs'][0]['name'] = 'Code Analysis'
        with self.assertRaises(GateError):
            embedded.verify_jobs(run, jobs, SOURCE_SHA, SOURCE_RUN_ID, 3, required,
                                 supplemental_apple=malformed)
        with self.assertRaises(GateError):
            embedded.verify_jobs(run, jobs, SOURCE_SHA, SOURCE_RUN_ID, 4, required,
                                 supplemental_apple=apple)


if __name__ == '__main__':
    unittest.main()
