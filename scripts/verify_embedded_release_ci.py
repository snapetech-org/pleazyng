#!/usr/bin/env python3
"""Verify reusable CI from the same captured main build before freezing a release."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

from pubspec_version import parse_pubspec_version
from verify_release_ci import API_BASE, GateError, _manifest_gate, _pages, _required_environment

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'snapetech-org/pleazyng'
REPOSITORY_ID = 1433487464
PREFIX = 'Release CI / '
APPLE_QUALIFICATION_WORKFLOW = '.github/workflows/apple-candidate-qualification.yml'
APPLE_PLATFORM_JOBS = (
    'Apple native reliability (iOS)',
    'Apple native reliability (macOS)',
    'Apple native reliability (tvOS)',
)
APPLE_JOB_BY_SLUG = dict(zip(('ios', 'macos', 'tvos'), APPLE_PLATFORM_JOBS, strict=True))
APPLE_IOS_NATIVE_PRODUCER = 'Build and test candidate iOS app'
APPLE_AGGREGATE_JOB = 'Apple native reliability'
APPLE_QUALIFICATION_STEPS = (
    'Verify frozen candidate source',
    'Require native Apple Silicon host',
    'Qualify Apple native components',
)
APPLE_FOCUSED_QUALIFICATION_STEPS = (
    APPLE_QUALIFICATION_STEPS[0],
    'Verify retained Apple siblings',
    *APPLE_QUALIFICATION_STEPS[1:],
)
APPLE_IOS_COMPATIBILITY_STEPS = ('Download candidate iOS app artifact', 'Extract candidate iOS app')
APPLE_HOSTED_RUNNER_LABELS = {('xcode-27',), ('macos-26',)}


class QualificationNotFound(GateError):
    """A narrowly retryable read-after-create GitHub response."""


def api(path, token, *, payload=None, missing=False):
    request = urllib.request.Request(f'{API_BASE}/repos/{REPOSITORY}/{path}',
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28',
                 'User-Agent': 'Pleazyng-release-qualification'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(16 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if missing and status == 404:
            return None
        failure = QualificationNotFound if status == 404 else GateError
        raise failure(f'Qualification API request {path} failed (HTTP {status}).') from None
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise GateError(f'Qualification API request failed ({type(error).__name__}).') from None
    if len(data) > 16 * 1024 * 1024:
        raise GateError('Qualification API response exceeded its safety limit.')
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise GateError('Qualification API returned invalid JSON.') from None
    if not isinstance(result, dict):
        raise GateError('Qualification API returned an unexpected response.')
    return result


def resolve_ref(tag, token, *, missing=False):
    reference = api(f'git/ref/tags/{tag}', token, missing=missing)
    if reference is None:
        return None
    target = reference.get('object', {})
    if target.get('type') == 'tag':
        target = api(f"git/tags/{target.get('sha')}", token).get('object', {})
    if target.get('type') != 'commit' or re.fullmatch(r'[0-9a-f]{40}', str(target.get('sha'))) is None:
        raise GateError('Release reference must resolve directly to a commit.')
    return target['sha']


def verify_candidate(token, repository, sha, tag, candidate, *, root=ROOT, pubspec_text=None):
    if repository != REPOSITORY or re.fullmatch(r'[0-9a-f]{40}', sha) is None:
        raise GateError('Qualification requires the canonical repository and exact source SHA.')
    version, _ = parse_pubspec_version((root / 'pubspec.yaml').read_text() if pubspec_text is None else pubspec_text)
    if tag != 'v' + version.split('+')[0] or candidate != f'{tag}-candidate.{sha}':
        raise GateError('Candidate must bind the checked-out version and exact source SHA.')
    if resolve_ref(candidate, token) != sha:
        raise GateError('Candidate reference does not match the captured source.')
    ancestry = api(f'compare/{sha}...main', token)
    if ancestry.get('status') not in ('ahead', 'identical') or ancestry.get('base_commit', {}).get('sha') != sha:
        raise GateError('Captured source is not an ancestor of canonical main.')
    stable = resolve_ref(tag, token, missing=True)
    if stable is not None and stable != sha:
        raise GateError('Existing stable release tag is immutable and points to different source.')


def _supplemental_apple_jobs(proof, sha, run_id, attempt):
    """Validate the in-memory proof before allowing any Apple CI substitutions."""
    if proof is None:
        return None
    if (not isinstance(proof, dict) or proof.get('mode') != 'supplemental-apple'
            or proof.get('repository') != REPOSITORY or proof.get('source_sha') != sha
            or proof.get('source_run_id') != run_id
            or type(proof.get('source_run_attempt')) is not int
            or proof.get('source_run_attempt') != attempt
            or proof.get('workflow') != APPLE_QUALIFICATION_WORKFLOW
            or proof.get('status') != 'completed' or proof.get('conclusion') != 'success'
            or type(proof.get('qualification_run_id')) is not int
            or proof['qualification_run_id'] < 1
            or type(proof.get('qualification_attempt')) is not int
            or proof['qualification_attempt'] < 1
            or type(proof.get('workflow_id')) is not int or proof['workflow_id'] < 1
            or type(proof.get('source_run_attempt')) is not int or proof['source_run_attempt'] < 1
            or re.fullmatch(r'[0-9a-f]{40}', str(proof.get('tooling_sha', ''))) is None):
        raise GateError('Supplemental Apple proof is not bound to this frozen source run.')
    records = proof.get('jobs')
    if not isinstance(records, list) or len(records) != len(APPLE_PLATFORM_JOBS):
        raise GateError('Supplemental Apple proof must contain exactly the three platform suites.')
    retained_run, retry_platform = _apple_qualification_title(proof.get('run_name'), run_id, sha)
    retry_job = APPLE_JOB_BY_SLUG.get(retry_platform)
    if (retained_run != proof.get('retained_run_id') or retained_run == proof['qualification_run_id']
            or proof.get('head_repository') != REPOSITORY or proof.get('head_branch') != 'main'):
        raise GateError('Supplemental Apple proof has an invalid workflow run binding.')
    result = {}
    for record in records:
        if not isinstance(record, dict) or record.get('name') not in APPLE_PLATFORM_JOBS:
            raise GateError('Supplemental Apple proof contains an unexpected platform suite.')
        name = record['name']
        inherited = retained_run is not None and name != retry_job
        origin = retained_run if inherited else proof['qualification_run_id']
        origin_attempt = record.get('qualification_attempt')
        if (name in result or type(record.get('id')) is not int or record['id'] < 1
                or record.get('run_id') != origin
                or type(origin_attempt) is not int or not 1 <= origin_attempt <= 100
                or re.fullmatch(r'[0-9a-f]{40}', str(record.get('tooling_sha', ''))) is None
                or (not inherited and (origin_attempt != proof['qualification_attempt']
                                       or record['tooling_sha'] != proof['tooling_sha']))
                or type(record.get('attempt')) is not int
                or not 1 <= record['attempt'] <= origin_attempt
                or record.get('status') != 'completed' or record.get('conclusion') != 'success'):
            raise GateError(f'Supplemental Apple suite is malformed or unsuccessful: {name}')
        labels = record.get('labels')
        normalized = tuple(str(label).lower() for label in labels) if isinstance(labels, list) else ()
        if (normalized not in APPLE_HOSTED_RUNNER_LABELS
                or record.get('runner_group_name') != 'GitHub Actions'):
            raise GateError(f'Supplemental Apple suite lacks public Apple Silicon runner evidence: {name}')
        passed_steps = record.get('passed_steps')
        expected_steps = APPLE_FOCUSED_QUALIFICATION_STEPS if name == retry_job else APPLE_QUALIFICATION_STEPS
        native = record.get('native_producer')
        if native is not None:
            if (name != APPLE_JOB_BY_SLUG['ios'] or normalized != ('macos-26',)
                    or not isinstance(native, dict) or native.get('name') != APPLE_IOS_NATIVE_PRODUCER
                    or type(native.get('id')) is not int or native['id'] < 1 or native['id'] == record['id']
                    or native.get('run_id') != origin or native.get('qualification_attempt') != origin_attempt
                    or native.get('tooling_sha') != record['tooling_sha']
                    or type(native.get('attempt')) is not int or not 1 <= native['attempt'] <= record['attempt']
                    or native.get('labels') != ['xcode-27'] or native.get('runner_group_name') != 'GitHub Actions'
                    or native.get('status') != 'completed' or native.get('conclusion') != 'success'
                    or native.get('passed_steps') != list(expected_steps)):
                raise GateError('The iOS compatibility proof lacks its passing native test execution.')
            expected_steps = (*expected_steps, *APPLE_IOS_COMPATIBILITY_STEPS)
        if not isinstance(passed_steps, list) or passed_steps != list(expected_steps):
            raise GateError(f'Supplemental Apple suite omitted its required passing steps: {name}')
        result[name] = record
    if set(result) != set(APPLE_PLATFORM_JOBS):
        raise GateError('Supplemental Apple proof omitted a required platform suite.')
    return result


def verify_jobs(run, jobs, sha, run_id, attempt, required, *, require_candidate_guards=False,
                supplemental_apple=None):
    repository = run.get('repository') or {}
    head_repository = run.get('head_repository') or {}
    if (not isinstance(required, list) or not required
            or any(not isinstance(name, str) or not name for name in required)
            or len(required) != len(set(required))):
        raise GateError('Embedded CI requires a nonempty unique job contract.')
    if (run.get('id') != run_id or run.get('run_attempt') != attempt
            or run.get('event') != 'workflow_dispatch' or run.get('head_branch') != 'main'
            or run.get('head_sha') != sha or not isinstance(run.get('path'), str)
            or run['path'].split('@')[0] != '.github/workflows/build.yml'
            or not isinstance(repository, dict) or repository.get('id') != REPOSITORY_ID
            or not isinstance(head_repository, dict) or head_repository.get('id') != REPOSITORY_ID):
        raise GateError('Embedded CI is not from the captured canonical main build and current attempt.')
    records = []
    supplemental_jobs = _supplemental_apple_jobs(supplemental_apple, sha, run_id, attempt)
    replaced_apple_jobs = set()
    qualified_apple_jobs = set()
    if supplemental_jobs is not None:
        for platform in APPLE_PLATFORM_JOBS:
            originals = [job for job in jobs if job.get('name') == PREFIX + platform]
            if (len(originals) == 1 and type(originals[0].get('id')) is int
                    and ((originals[0].get('status'), originals[0].get('conclusion')) == ('completed', 'success')
                         or platform in supplemental_jobs)):
                qualified_apple_jobs.add(platform)
    else:
        qualified_apple_jobs.update(
            platform for platform in APPLE_PLATFORM_JOBS
            if any(job.get('name') == PREFIX + platform
                   and job.get('status') == 'completed' and job.get('conclusion') == 'success'
                   for job in jobs)
        )
    for name in required:
        matches = [job for job in jobs if job.get('name') == PREFIX + name]
        if name == 'Linux package smoke build' and len(matches) == 1 and matches[0].get('conclusion') == 'skipped':
            # Captured candidates run the package checks on the production bundle.
            for producer, step in (
                    ('Compile Linux (x64)', 'Verify every unbundled library the bundle needs is declared'),
                    ('Package Linux (x64, distro)', 'Verify the declared dependencies reached the package metadata'),
                    ('Build Linux (x64)', 'Upload Linux x64 artifacts')):
                evidence = [j for j in jobs if j['name'] == producer and j.get('conclusion') == 'success']
                if len(evidence) != 1 or not any(st.get('name') == step and st.get('conclusion') == 'success'
                                                for st in evidence[0].get('steps', [])):
                    raise GateError(f'Release package qualification missing {producer}: {step}')
                job = evidence[0]
                records.append({'id': job['id'], 'name': producer, 'attempt': job['run_attempt'],
                                'status': job['status'], 'conclusion': job['conclusion']})
            continue
        if (len(matches) != 1 or type(matches[0].get('id')) is not int):
            raise GateError(f'Required embedded CI job did not pass uniquely: {name}')
        job = matches[0]
        record = {'id': job['id'], 'name': job['name'], 'attempt': job.get('run_attempt'),
                  'status': job.get('status'), 'conclusion': job.get('conclusion')}
        if name in APPLE_PLATFORM_JOBS:
            if (job.get('status'), job.get('conclusion')) == ('completed', 'success'):
                pass
            elif supplemental_jobs is not None:
                replacement = supplemental_jobs[name]
                record['supplemental_qualification'] = {
                    'run_id': replacement['run_id'],
                    'attempt': replacement['attempt'], 'job_id': replacement['id'],
                    'status': replacement['status'], 'conclusion': replacement['conclusion'],
                    'tooling_sha': replacement['tooling_sha'],
                    'workflow': supplemental_apple['workflow'],
                }
                replaced_apple_jobs.add(name)
            else:
                raise GateError(f'Required embedded CI job did not pass uniquely: {name}')
        elif (name == APPLE_AGGREGATE_JOB
              and (job.get('status'), job.get('conclusion')) != ('completed', 'success')
              and supplemental_jobs is not None
              and set(APPLE_PLATFORM_JOBS) <= qualified_apple_jobs):
            record['derived_conclusion'] = 'success'
            record['derived_from_supplemental_apple_jobs'] = [
                supplemental_jobs[platform]['id'] for platform in APPLE_PLATFORM_JOBS]
        elif job.get('status') != 'completed' or job.get('conclusion') != 'success':
            raise GateError(f'Required embedded CI job did not pass uniquely: {name}')
        records.append(record)
    if require_candidate_guards or any(job.get('name') == 'Record qualified source' for job in jobs):
        for prefix, minimum in (('Candidate isolation / ', 4), ('Candidate security / ', 2)):
            guards = sorted((job for job in jobs if job['name'].startswith(prefix)), key=lambda job: job['name'])
            if len(guards) < minimum or any(job.get('status') != 'completed' or job.get('conclusion') != 'success'
                                            or type(job.get('id')) is not int for job in guards):
                raise GateError(f'Candidate qualification is incomplete: {prefix.rstrip(" /")}')
            records.extend({'id': job['id'], 'name': job['name'], 'attempt': job['run_attempt'],
                            'status': job['status'], 'conclusion': job['conclusion']} for job in guards)
    replaceable_names = {PREFIX + name for name in APPLE_PLATFORM_JOBS if name in replaced_apple_jobs}
    if supplemental_jobs is not None and set(APPLE_PLATFORM_JOBS) <= qualified_apple_jobs:
        replaceable_names.add(PREFIX + APPLE_AGGREGATE_JOB)
    for job in jobs:
        if str(job.get('name', '')).startswith(PREFIX) and (job.get('status') != 'completed'
                or job.get('conclusion') not in ('success', 'skipped')) and job.get('name') not in replaceable_names:
            raise GateError('A reusable CI job failed or remains unfinished.')
    proof = {'mode': 'embedded', 'source_sha': sha, 'run_id': run_id, 'attempt': attempt,
             'workflow': '.github/workflows/build.yml', 'ci_workflow': '.github/workflows/ci.yml', 'jobs': records}
    if supplemental_apple is not None:
        proof['supplemental_apple'] = supplemental_apple
    return proof


def same_completed_execution(current, earlier):
    """GitHub can copy a passing job into a rerun with a new job ID."""
    identity = ('run_id', 'head_sha', 'runner_id', 'started_at', 'completed_at')
    return (current.get('status') == earlier.get('status') == 'completed'
            and current.get('conclusion') == earlier.get('conclusion') == 'success'
            and all(current.get(field) and current[field] == earlier.get(field) for field in identity)
            and bool(current.get('steps')) and current['steps'] == earlier.get('steps'))


def latest_attempt_jobs(attempt_pages):
    """Resolve rerun history by logical job, retaining each newest execution."""
    latest = {}
    seen = set()
    for attempt, jobs in enumerate(attempt_pages, start=1):
        if not isinstance(jobs, list):
            raise GateError('A CI attempt omitted its job inventory.')
        names_in_attempt = set()
        for raw in jobs:
            if not isinstance(raw, dict) or not isinstance(raw.get('name'), str):
                raise GateError('A CI attempt contains a malformed job record.')
            name = raw['name']
            if name in names_in_attempt:
                raise GateError(f'CI attempt {attempt} contains an ambiguous duplicate job: {name}')
            names_in_attempt.add(name)
            job = dict(raw)
            recorded_attempt = job.get('run_attempt', attempt)
            if recorded_attempt != attempt:
                raise GateError('GitHub returned a job under a different attempt than its endpoint.')
            job['run_attempt'] = attempt
            key = (name, attempt)
            if key in seen:
                raise GateError('CI attempt history contains a duplicate job record.')
            seen.add(key)
            if name in latest and same_completed_execution(job, latest[name]):
                continue
            latest[name] = job
    return list(latest.values())


def fetch_attempt_history(run_id, attempt, token):
    """Fetch every immutable attempt so successful sibling suites remain reusable."""
    if type(attempt) is not int or not 1 <= attempt <= 100:
        raise GateError('Captured CI attempt count is outside the supported bound.')
    pages = []
    for number in range(1, attempt + 1):
        jobs = _pages(f'{API_BASE}/repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{number}/jobs', token, 'jobs',
                      page_size=30)
        pages.append(jobs)
    return latest_attempt_jobs(pages)


def _require_main_ancestor(sha, token, description):
    comparison = api(f'compare/{sha}...main', token)
    if (comparison.get('status') not in ('ahead', 'identical')
            or comparison.get('base_commit', {}).get('sha') != sha):
        raise GateError(f'{description} is not an ancestor of canonical main.')


def _require_canonical_run_repository(run, *, description):
    for key in ('repository', 'head_repository'):
        identity = run.get(key)
        if (not isinstance(identity, dict) or identity.get('id') != REPOSITORY_ID
                or identity.get('full_name') != REPOSITORY):
            raise GateError(f'{description} is not from the canonical repository.')


def _positive_run_id(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value):
        return int(value)
    return None


def _apple_qualification_title(title, source_run_id, source_sha):
    base = f'apple-candidate-qualification:{source_run_id}:{source_sha}'
    if title == base:
        return None, None
    resume = re.fullmatch(re.escape(base) + r':resume:([1-9][0-9]*):(ios|macos|tvos)', str(title))
    if resume is None:
        raise GateError('Supplemental Apple run title does not bind this frozen product.')
    return int(resume[1]), resume[2]


def _validate_apple_qualification_run(run, run_id, expected_title, token, *, conclusions):
    _require_canonical_run_repository(run, description='Supplemental Apple run')
    attempt = run.get('run_attempt')
    tooling_sha = run.get('head_sha')
    path = run.get('path')
    workflow_id = run.get('workflow_id')
    if (run.get('id') != run_id or run.get('event') != 'workflow_dispatch'
            or run.get('head_branch') != 'main'
            or not isinstance(path, str) or path.split('@')[0] != APPLE_QUALIFICATION_WORKFLOW
            or run.get('display_title') != expected_title
            or run.get('status') != 'completed' or run.get('conclusion') not in conclusions
            or type(attempt) is not int or not 1 <= attempt <= 100
            or re.fullmatch(r'[0-9a-f]{40}', str(tooling_sha)) is None
            or type(workflow_id) is not int or workflow_id < 1):
        raise GateError('Supplemental Apple run is not the expected completed trusted-main workflow run.')
    workflow = api(f'actions/workflows/{workflow_id}', token)
    if (workflow.get('id') != workflow_id or workflow.get('path') != APPLE_QUALIFICATION_WORKFLOW
            or workflow.get('state') != 'active'):
        raise GateError('Supplemental Apple run does not resolve to the active canonical workflow file.')
    _require_main_ancestor(tooling_sha, token, 'Supplemental Apple workflow source')


def _passing_apple_job_record(run, job, required_steps, runner_labels=APPLE_HOSTED_RUNNER_LABELS):
    name = job['name']
    if (job.get('status') != 'completed' or job.get('conclusion') != 'success'
            or type(job.get('id')) is not int or job['id'] < 1
            or type(job.get('run_attempt')) is not int
            or not 1 <= job['run_attempt'] <= run['run_attempt']):
        raise GateError(f'Supplemental Apple suite did not pass in its latest attempt: {name}')
    labels = job.get('labels')
    normalized = tuple(str(label).lower() for label in labels) if isinstance(labels, list) else ()
    if normalized not in runner_labels or job.get('runner_group_name') != 'GitHub Actions':
        raise GateError(f'Supplemental Apple suite did not use a public hosted Apple Silicon runner: {name}')
    steps = job.get('steps')
    if not isinstance(steps, list):
        raise GateError(f'Supplemental Apple suite omitted step evidence: {name}')
    for step_name in required_steps:
        matches = [step for step in steps if step.get('name') == step_name]
        if (len(matches) != 1 or matches[0].get('status') != 'completed'
                or matches[0].get('conclusion') != 'success'):
            raise GateError(f'Supplemental Apple suite step did not pass uniquely: {name}: {step_name}')
    return {
        'id': job['id'], 'name': name, 'attempt': job['run_attempt'],
        'run_id': run['id'], 'qualification_attempt': run['run_attempt'], 'tooling_sha': run['head_sha'],
        'status': job['status'], 'conclusion': job['conclusion'],
        'labels': labels, 'runner_group_name': job['runner_group_name'],
        'passed_steps': list(required_steps),
    }


def _qualifying_apple_suites(run, expected_names, token, *, failed_job=None, focused=False):
    jobs = fetch_attempt_history(run['id'], run['run_attempt'], token)
    ios = APPLE_JOB_BY_SLUG['ios']
    suite_jobs = [job for job in jobs if str(job.get('name', '')).startswith('Apple native reliability')
                  and not (focused and ios not in expected_names and job.get('name') == ios
                           and job.get('conclusion') == 'skipped')]
    if len(suite_jobs) != len(expected_names) or {job.get('name') for job in suite_jobs} != set(expected_names):
        raise GateError('Supplemental Apple workflow does not contain exactly its expected platform suites.')
    producers = [job for job in jobs if job.get('name') == APPLE_IOS_NATIVE_PRODUCER]
    if len(producers) > 1 or (producers and ios not in expected_names):
        raise GateError('Supplemental Apple workflow contains an unexpected iOS native producer.')
    required_steps = APPLE_FOCUSED_QUALIFICATION_STEPS if focused else APPLE_QUALIFICATION_STEPS
    failed = {'failure', 'cancelled', 'timed_out'}
    records = []
    for job in suite_jobs:
        name = job['name']
        if name == failed_job:
            failed_stage = job
            if name == ios and producers and producers[0].get('conclusion') in failed:
                failed_stage = producers[0]
            if failed_stage.get('status') != 'completed' or failed_stage.get('conclusion') not in failed:
                raise GateError(f'Retained Apple run did not record the requested failed suite: {name}')
            continue
        if name == ios and producers:
            compatibility_steps = (*required_steps, *APPLE_IOS_COMPATIBILITY_STEPS)
            record = _passing_apple_job_record(run, job, compatibility_steps, {('macos-26',)})
            native = _passing_apple_job_record(run, producers[0], required_steps, {('xcode-27',)})
            if native['attempt'] > record['attempt'] or native['id'] == record['id']:
                raise GateError('The iOS compatibility job does not follow its native test execution.')
            record['native_producer'] = native
        else:
            record = _passing_apple_job_record(run, job, required_steps)
        records.append(record)
    return records


def verify_retained_apple_siblings(source_run_id, source_sha, retained_run_id, retry_platform, token):
    """Accept only successful siblings from a full failed run of this exact candidate."""
    source_run_id = _positive_run_id(source_run_id)
    retained_run_id = _positive_run_id(retained_run_id)
    retry_job = APPLE_JOB_BY_SLUG.get(retry_platform)
    if (source_run_id is None or retained_run_id is None or retained_run_id == source_run_id
            or retry_job is None or re.fullmatch(r'[0-9a-f]{40}', str(source_sha)) is None):
        raise GateError('Retaining Apple siblings requires exact run IDs, product SHA, and one retry platform.')
    retained = api(f'actions/runs/{retained_run_id}', token)
    base = f'apple-candidate-qualification:{source_run_id}:{source_sha}'
    _validate_apple_qualification_run(
        retained, retained_run_id, base, token, conclusions={'failure', 'cancelled', 'timed_out'})
    return _qualifying_apple_suites(retained, APPLE_PLATFORM_JOBS, token, failed_job=retry_job)


def verify_supplemental_apple_ci(repository, source_sha, source_run_id, qualification_run_id, token):
    """Verify hosted Apple qualification for one frozen build source."""
    source_run_id = _positive_run_id(source_run_id)
    qualification_run_id = _positive_run_id(qualification_run_id)
    if (repository != REPOSITORY or re.fullmatch(r'[0-9a-f]{40}', str(source_sha)) is None
            or source_run_id is None or qualification_run_id is None
            or not isinstance(token, str) or not token.strip()):
        raise GateError('Supplemental Apple qualification requires exact canonical run identities.')

    source = api(f'actions/runs/{source_run_id}', token)
    _require_canonical_run_repository(source, description='Frozen source run')
    source_attempt = source.get('run_attempt')
    if (source.get('id') != source_run_id or source.get('event') != 'workflow_dispatch'
            or source.get('head_branch') != 'main' or source.get('head_sha') != source_sha
            or not isinstance(source.get('path'), str)
            or source['path'].split('@')[0] != '.github/workflows/build.yml'
            or type(source_attempt) is not int or not 1 <= source_attempt <= 100):
        raise GateError('Frozen source run is not the exact canonical main build for this product SHA.')
    _require_main_ancestor(source_sha, token, 'Frozen product source')

    qualification = api(f'actions/runs/{qualification_run_id}', token)
    title = qualification.get('display_title')
    retained_run, retry_platform = _apple_qualification_title(title, source_run_id, source_sha)
    retry_job = APPLE_JOB_BY_SLUG.get(retry_platform)
    _validate_apple_qualification_run(qualification, qualification_run_id, title, token, conclusions={'success'})
    records = _qualifying_apple_suites(
        qualification, (retry_job,) if retry_job else APPLE_PLATFORM_JOBS, token, focused=retry_job is not None)
    if retained_run is not None:
        if retained_run == qualification_run_id:
            raise GateError('Supplemental Apple qualification cannot retain results from itself.')
        records.extend(verify_retained_apple_siblings(
            source_run_id, source_sha, retained_run, retry_platform, token))

    return {
        'mode': 'supplemental-apple', 'repository': repository,
        'source_sha': source_sha, 'source_run_id': source_run_id,
        'source_run_attempt': source_attempt,
        'qualification_run_id': qualification_run_id,
        'qualification_attempt': qualification['run_attempt'],
        'retained_run_id': retained_run,
        'tooling_sha': qualification['head_sha'], 'workflow': APPLE_QUALIFICATION_WORKFLOW,
        'workflow_id': qualification['workflow_id'], 'run_name': title,
        'head_repository': REPOSITORY, 'head_branch': 'main',
        'status': qualification['status'], 'conclusion': qualification['conclusion'],
        'jobs': records,
    }


def verify_created_tag(token, tag, sha):
    # GitHub may accept the ref before either its ref or annotated object is
    # visible to a subsequent read. Retry only those 404s, never the POSTs.
    for attempt in range(5):
        try:
            visible = resolve_ref(tag, token, missing=True)
        except QualificationNotFound:
            visible = None
        if visible is not None:
            if visible != sha:
                raise GateError('Stable tag did not resolve to the qualified source.')
            return
        if attempt < 4:
            time.sleep(2)
    raise GateError('New stable tag was not visible after bounded verification; preserve it and retry qualification.')


def create_stable_tag(token, tag, sha):
    existing = resolve_ref(tag, token, missing=True)
    if existing is not None:
        if existing != sha:
            raise GateError('Existing stable tag cannot be moved.')
        return
    annotated = api('git/tags', token, payload={'tag': tag, 'message': f'Pleazyng {tag} qualified source',
                                               'object': sha, 'type': 'commit'})
    try:
        api('git/refs', token, payload={'ref': f'refs/tags/{tag}', 'sha': annotated['sha']})
    except GateError as creation_error:
        try:
            verify_created_tag(token, tag, sha)
        except GateError as verification_error:
            raise GateError(f'{creation_error} Subsequent tag verification failed: {verification_error}') from None
        return
    verify_created_tag(token, tag, sha)


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--candidate-only', action='store_true')
    mode.add_argument('--create-tag', action='store_true')
    mode.add_argument('--qualification-only', action='store_true')
    parser.add_argument('--proof', type=Path)
    args = parser.parse_args()
    token, repository, sha, _ = _required_environment()
    tag, candidate = os.environ.get('RELEASE_TAG', ''), os.environ.get('CANDIDATE_REF', '')
    if os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise GateError('Captured release qualification requires the authorized main workflow.')
    verify_candidate(token, repository, sha, tag, candidate)
    if args.candidate_only:
        return 0
    run_id, attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    run = api(f'actions/runs/{run_id}', token)
    jobs = fetch_attempt_history(run_id, attempt, token)
    proof = verify_jobs(run, jobs, sha, run_id, attempt, _manifest_gate()['requiredJobs'], require_candidate_guards=True)
    if args.create_tag:
        create_stable_tag(token, tag, sha)
    elif not args.qualification_only and resolve_ref(tag, token) != sha:
        raise GateError('Stable tag does not match the qualified source.')
    proof.update({'tag': tag, 'candidate_ref': candidate})
    if args.proof:
        args.proof.write_text(json.dumps(proof, indent=2) + '\n')
    print(f'Embedded CI verified {len(proof["jobs"])} required jobs for {sha}, run {run_id}, attempt {attempt}.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (GateError, ValueError, KeyError) as error:
        print(f'Embedded release qualification failed: {error}', file=sys.stderr)
        raise SystemExit(1)
