#!/usr/bin/env python3
"""Inspect or attach one valid, already uploaded build to its matching store version."""

import argparse
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlencode

from build_apple_store_archive import AppleProfileAPI, BUNDLE, _apple_api_token


APP_ID = '6826955231'
PLATFORMS = ('IOS', 'MAC_OS', 'TV_OS')


def exact_record(records, kind, matches):
    selected = [record for record in records if record.get('type') == kind and matches(record['attributes'])]
    if len(selected) != 1 or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', selected[0].get('id', '')):
        raise ValueError(f'Expected exactly one matching Apple {kind} resource')
    return selected[0]


def prepare(api, platform, version, build_number, *, inspect_only=False):
    app = api.request(f'/v1/apps/{APP_ID}').get('data', {})
    if (app.get('type') != 'apps' or app.get('id') != APP_ID
            or app.get('attributes', {}).get('bundleId') != BUNDLE):
        raise ValueError('App Store Connect returned an unexpected app')

    version_filters = urlencode({'filter[platform]': platform, 'filter[versionString]': version})
    draft = exact_record(api.items(f'/v1/apps/{APP_ID}/appStoreVersions?{version_filters}'), 'appStoreVersions',
                         lambda data: data.get('platform') == platform and data.get('versionString') == version)
    build_filters = urlencode({'filter[app]': APP_ID, 'filter[version]': build_number,
                               'filter[preReleaseVersion.version]': version})
    builds = []
    for candidate in api.items(f'/v1/builds?{build_filters}'):
        candidate_id = candidate.get('id', '')
        if candidate.get('type') != 'builds' or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', candidate_id):
            raise ValueError('Unexpected build resource in the App Store Connect response')
        train = api.request(f'/v1/builds/{candidate_id}/preReleaseVersion')['data']
        if (train.get('type') == 'preReleaseVersions' and train['attributes'].get('platform') == platform
                and train['attributes'].get('version') == version):
            builds.append(candidate)
    build = exact_record(builds, 'builds', lambda data: data.get('version') == build_number)
    if build['attributes'].get('processingState') != 'VALID' or build['attributes'].get('expired') is not False:
        raise ValueError('The exact uploaded build must be VALID and unexpired')

    relationship = f'/v1/appStoreVersions/{draft["id"]}/relationships/build'
    expected = {'type': 'builds', 'id': build['id']}
    attached = api.request(relationship)['data']
    state = draft['attributes'].get('appVersionState') or draft['attributes'].get('appStoreState')
    changed = attached != expected
    if changed and not inspect_only:
        if state not in {'PREPARE_FOR_SUBMISSION', 'READY_FOR_REVIEW'}:
            raise ValueError('The exact store version is no longer an editable review draft')
        api.request(relationship, {'data': expected}, method='PATCH')
        if api.request(relationship)['data'] != expected:
            raise ValueError('Apple did not retain the exact draft build attachment')
    receipt = {'app_id': APP_ID, 'platform': platform, 'version': version, 'build_number': build_number,
               'app_store_version_id': draft['id'], 'asc_build_id': build['id'],
               'observed_draft_state': state, 'changed': changed and not inspect_only}
    if inspect_only:
        receipt['exact_build_attached'] = not changed
        receipt.update(inspect_review(api, draft['id'], platform))
    return receipt


def inspect_review(api, draft_id, platform):
    localizations = []
    for locale in api.items(f'/v1/appStoreVersions/{draft_id}/appStoreVersionLocalizations'):
        metadata = locale['attributes']
        placements = api.items(f'/v1/appStoreVersionLocalizations/{locale["id"]}/placements')
        localizations.append({
            'locale': metadata.get('locale'),
            'description_present': bool(metadata.get('description')),
            'support_url_present': bool(metadata.get('supportUrl')),
            'screenshots': [
                {'group': item['attributes'].get('placementGroup'), 'state': item['attributes'].get('state')}
                for item in placements if item['attributes'].get('placementType') == 'APP_SCREENSHOT'
            ],
        })
    purchases = api.items(f'/v1/apps/{APP_ID}/inAppPurchasesV2')
    purchase_states = [
        {key: product['attributes'].get(key) for key in ('productId', 'inAppPurchaseType', 'state')}
        for product in purchases
        if product['attributes'].get('productId') in {'pleazyng_7_day_trial', 'pleazyng_lifetime_unlock'}
    ]
    submissions = api.items(f'/v1/apps/{APP_ID}/reviewSubmissions?' + urlencode({'filter[platform]': platform}))
    return {'localizations': localizations, 'purchases': purchase_states, 'review_submissions': [
        {key: submission['attributes'].get(key) for key in ('state', 'submittedDate')}
        for submission in submissions if submission['attributes'].get('platform') == platform
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True, choices=PLATFORMS)
    parser.add_argument('--version', required=True)
    parser.add_argument('--build-number', required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--inspect-only', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version) or not re.fullmatch(r'[1-9]\d*', args.build_number):
        parser.error('Expected a stable release version and positive numeric build')
    with tempfile.TemporaryDirectory(prefix='pleazyng-apple-review-') as directory:
        api = AppleProfileAPI(_apple_api_token(Path(directory)))
        receipt = prepare(api, args.platform, args.version, args.build_number, inspect_only=args.inspect_only)
    args.receipt.write_text(json.dumps(receipt, indent=2) + '\n')
    action = 'Inspected the review draft for' if args.inspect_only else 'Verified the attached App Store build for'
    print(f'{action} {args.platform} {args.version} ({args.build_number})')


if __name__ == '__main__':
    main()
