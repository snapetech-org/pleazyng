#!/usr/bin/env python3
"""Reconcile one published Pleazyng release with App Store Connect."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from build_apple_store_archive import AppleProfileAPI, _apple_api_token
from release_coverage import missing_assets
from stores.prepare_apple_review_draft import (
    EDITABLE, LIVE, PLATFORMS, REVIEWING_OR_RELEASED, platform_versions, purchase_states,
    version_key, version_state,
)


REPO = 'snapetech-org/pleazyng'
STORE = 'publish-app-stores.yml'
MAC_STORE = 'macos-app-store-signed.yml'
REVIEW = 'prepare-apple-review.yml'
BUILD = 'build.yml'
TAG = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+')
ASSETS = {'IOS': 'pleazyng-ios-appstore.ipa', 'TV_OS': 'pleazyng-tvos-appstore.ipa'}
CHANNELS = {'IOS': 'app-store-ios', 'TV_OS': 'app-store-tvos'}


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True)


def release_for(tag):
    if tag and not TAG.fullmatch(tag):
        raise ValueError('Expected a stable release tag')
    endpoint = f'tags/{tag}' if tag else 'latest'
    release = json.loads(gh('api', f'repos/{REPO}/releases/{endpoint}'))
    actual = release['tag_name']
    if (not TAG.fullmatch(actual) or (tag and actual != tag) or release['draft'] is not False
            or release['prerelease'] is not False or not release['published_at']):
        raise ValueError('Apple delivery requires a published stable GitHub release')
    names = {asset['name'] for asset in release['assets'] if asset['size'] > 0}
    missing = missing_assets(names)
    if missing or 'SHA256SUMS.txt' not in names:
        raise ValueError(f'Apple delivery requires complete release assets ({", ".join(missing)})')
    return release, names


class RunInventory:
    def __init__(self, first_date, head):
        self.first_date = first_date
        self.head = head
        self.workflows = {}

    def find(self, workflow, title):
        if workflow not in self.workflows:
            pages = json.loads(gh(
                'api', '--method', 'GET', '--paginate', '--slurp', f'repos/{REPO}/actions/workflows/{workflow}/runs',
                '-f', 'branch=main', '-f', 'event=workflow_dispatch', '-f', 'per_page=100',
                '-f', f'created=>={self.first_date}',
            ))
            self.workflows[workflow] = [run for page in pages for run in page['workflow_runs']]
        matching = [run for run in self.workflows[workflow]
                    if run['display_title'] == title and run['path'] == f'.github/workflows/{workflow}'
                    and run['head_branch'] == 'main' and run['event'] == 'workflow_dispatch'
                    and run['head_repository']['full_name'] == REPO]
        if any(run['status'] != 'completed' for run in matching):
            return 'running', None
        successes = [run['id'] for run in matching if run['conclusion'] == 'success']
        if successes:
            return 'succeeded', max(successes)
        if any(run['head_sha'] == self.head and run['conclusion'] != 'cancelled' for run in matching):
            return 'failed_current_revision', None
        return 'missing', None


def dispatch(workflow, **fields):
    args = [item for key, value in fields.items() for item in ('-f', f'{key}={value}')]
    gh('workflow', 'run', workflow, '--repo', REPO, '--ref', 'main', *args)
    return 'dispatched'


def ensure_stage(inventory, workflow, title, *, retry=False, **fields):
    state, run = inventory.find(workflow, title)
    if state == 'missing' or (retry and state == 'failed_current_revision'):
        state = dispatch(workflow, **fields)
    return state, run


def processing_run(inventory, platform, tag, assets):
    if platform == 'MAC_OS':
        return ensure_stage(inventory, MAC_STORE, f'macos-app-store:signed-upload:{tag}', release_tag=tag)
    channel = CHANNELS[platform]
    title = f'action:publish-app-stores:pool:public:tag:{tag}:channel:{channel}'
    state, run = inventory.find(STORE, title)
    if state != 'missing':
        return state, run
    fields = {'action': 'publish-app-stores', 'runner_pool': 'public', 'release_tag': tag,
              'channel': channel, 'play_track': 'internal', 'play_release_status': 'draft'}
    if ASSETS[platform] not in assets:
        mode = channel.removeprefix('app-store-') + '-store'
        state, build_run = ensure_stage(
            inventory, BUILD, f'action:release:pool:public:mode:{mode}:tag:{tag}',
            action='release', runner_pool='public', mode=mode, build_version=tag[1:],
            include_apple_store_build='true',
        )
        if not build_run:
            return f'signing_{state}', None
        fields['apple_source_run'] = build_run
    return dispatch(STORE, **fields), None


def reconcile_platform(api, platform, release, assets, *, allow_bootstrap):
    requested_tag = release['tag_name']
    versions = sorted(platform_versions(api, platform),
                      key=lambda item: version_key(item['attributes']['versionString']), reverse=True)
    if not versions:
        return {'state': 'waiting_for_first_store_draft', 'tag': requested_tag}
    current = versions[0]
    current_version = current['attributes']['versionString']
    current_tag = f'v{current_version}'
    current_state = version_state(current)
    if current_tag == requested_tag and current_state in REVIEWING_OR_RELEASED:
        return {'state': current_state, 'tag': current_tag}
    if current_tag != requested_tag and version_key(current_version) > version_key(requested_tag[1:]):
        return {'state': 'newer_version_present', 'tag': current_tag}
    if current_tag != requested_tag and current_state not in LIVE:
        if not allow_bootstrap or current_state not in EDITABLE or any(version_state(item) in LIVE for item in versions):
            return {'state': 'waiting_for_previous_store_version', 'tag': current_tag}
        release, assets = release_for(current_tag)
    elif current_tag == requested_tag and current_state not in EDITABLE:
        return {'state': current_state, 'tag': current_tag}
    tag = release['tag_name']
    inventory = RunInventory(release['created_at'].split('T')[0], os.environ['GITHUB_SHA'])
    state, run = processing_run(inventory, platform, tag, assets)
    if not run:
        return {'state': state, 'tag': tag}
    state, review_run = ensure_stage(
        inventory, REVIEW, f'submit-apple-review:{tag}:{platform}', retry=True,
        release_tag=tag, platform=platform, processing_run=run, submit_for_review='true',
        automatic_update='true',
    )
    return {'state': f'review_{state}', 'tag': tag, 'processing_run': run, 'review_run': review_run}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', default='')
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get('GITHUB_REPOSITORY') != REPO:
        raise ValueError('Apple delivery requires the canonical repository')
    release, assets = release_for(args.tag)
    receipt = {'release_tag': release['tag_name'], 'platforms': {}}
    try:
        with tempfile.TemporaryDirectory(prefix='pleazyng-apple-delivery-') as directory:
            api = AppleProfileAPI(_apple_api_token(Path(directory)))
            states = purchase_states(api)
            receipt['purchase_states'] = states
            if any(state != 'APPROVED' for state in states.values()):
                receipt['state'] = 'waiting_for_first_purchases'
                print('Apple delivery is enabled and waiting for first purchase approval.')
                return
            for platform in PLATFORMS:
                try:
                    result = reconcile_platform(api, platform, release, assets, allow_bootstrap=not args.tag)
                except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
                    result = {'state': 'error', 'error': str(error)}
                receipt['platforms'][platform] = result
                print(f'{platform} {result["state"]}')
        if any(item['state'] == 'error' for item in receipt['platforms'].values()):
            raise ValueError('One or more Apple platform lanes could not be reconciled')
    finally:
        args.receipt.write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
