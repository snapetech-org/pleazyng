#!/usr/bin/env python3
"""Prepare one valid, already uploaded App Store build for review."""

import argparse
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlencode

from build_apple_store_archive import AppleProfileAPI, BUNDLE, _apple_api_token


APP_ID = '6826955231'
PLATFORMS = ('IOS', 'MAC_OS', 'TV_OS')
REQUIRED_SCREENSHOT_GROUPS = {
    'IOS': {'IPHONE_DYNAMIC_ISLAND_MEDIUM_PROFILE', 'IPAD_13_PROFILE'},
    'MAC_OS': {'MAC_PROFILE'},
    'TV_OS': {'TV_PROFILE'},
}


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
    contact_fields = ('contactFirstName', 'contactLastName', 'contactPhone', 'contactEmail')
    review_fields = (*contact_fields, 'demoAccountRequired', 'notes')
    query = urlencode({'include': 'appStoreReviewDetail', 'fields[appStoreVersions]': 'appStoreReviewDetail',
                       'fields[appStoreReviewDetails]': ','.join(review_fields)})
    version = api.request(f'/v1/appStoreVersions/{draft_id}?{query}')
    details = next((item['attributes'] for item in version.get('included', [])
                    if item['type'] == 'appStoreReviewDetails'), {})
    # Workflow receipts only record readiness. Keep contact information and notes in Apple.
    review_detail = {
        'contact_complete': all(bool(details.get(field)) for field in contact_fields),
        'demo_account_required': details.get('demoAccountRequired'),
        'notes_present': bool(details.get('notes')),
    }
    localizations = []
    for locale in api.items(f'/v1/appStoreVersions/{draft_id}/appStoreVersionLocalizations'):
        metadata = locale['attributes']
        placements = api.items(f'/v1/appStoreVersionLocalizations/{locale["id"]}/placements')
        localizations.append({
            'id': locale['id'],
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
    submissions = []
    for submission in api.items(f'/v1/apps/{APP_ID}/reviewSubmissions'):
        attributes = submission['attributes']
        if attributes.get('platform') not in {None, platform}:
            continue
        summary = {key: attributes.get(key) for key in ('platform', 'state', 'submittedDate')}
        if summary['state'] == 'READY_FOR_REVIEW':
            validated = exact_record([submission], 'reviewSubmissions', lambda _: True)
            items = api.items(f'/v1/reviewSubmissions/{validated["id"]}/items?include=appStoreVersion')
            summary['items'] = [review_item_summary(item, draft_id) for item in items]
        submissions.append(summary)
    return {'localizations': localizations, 'app_review_detail': review_detail,
            'purchases': purchase_states, 'review_submissions': submissions}


def review_item_summary(item, draft_id):
    links = {key: value.get('data') for key, value in item.get('relationships', {}).items() if isinstance(value, dict)}
    return {'type': item.get('type'), 'state': item.get('attributes', {}).get('state'),
            'relations': sorted(links),
            'related_resources': {key: link.get('type') for key, link in links.items() if isinstance(link, dict)},
            'exact_app_version': links.get('appStoreVersion') == {'type': 'appStoreVersions', 'id': draft_id}}


def add_version_for_review(api, platform, version, build_number):
    receipt = prepare(api, platform, version, build_number, inspect_only=True)
    if (not receipt['exact_build_attached']
            or receipt['observed_draft_state'] not in {'PREPARE_FOR_SUBMISSION', 'READY_FOR_REVIEW'}):
        raise ValueError('The exact attached build must be available for a new review draft')
    listing = [locale for locale in receipt['localizations'] if locale['locale'] == 'en-US']
    if (len(listing) != 1 or not listing[0]['description_present'] or not listing[0]['support_url_present']
            or not receipt['app_review_detail']['contact_complete']):
        raise ValueError('Review contact, description and support URL are required')
    present_groups = {item['group'] for item in listing[0]['screenshots'] if item['state'] == 'ACTIVE'}
    missing_groups = REQUIRED_SCREENSHOT_GROUPS[platform] - present_groups
    if missing_groups:
        raise ValueError(f'Review requires active screenshots for {", ".join(sorted(missing_groups))}')

    # Apple can leave a newly created draft's platform unset until its first item is added.
    filters = urlencode({'filter[state]': 'READY_FOR_REVIEW'})
    submissions = [item for item in api.items(f'/v1/apps/{APP_ID}/reviewSubmissions?{filters}')
                   if item['attributes'].get('platform') in {None, platform}]
    if not submissions:
        if receipt['observed_draft_state'] == 'READY_FOR_REVIEW':
            raise ValueError('The version already belongs to a review submission that could not be identified')
        submissions = [api.request('/v1/reviewSubmissions', {'data': {
            'type': 'reviewSubmissions', 'attributes': {'platform': platform},
            'relationships': {'app': {'data': {'type': 'apps', 'id': APP_ID}}},
        }})['data']]
    submission = exact_record(submissions, 'reviewSubmissions', lambda attrs:
                              attrs.get('platform') in {None, platform} and attrs['state'] == 'READY_FOR_REVIEW'
                              and attrs.get('submittedDate') is None)

    version_link = {'type': 'appStoreVersions', 'id': receipt['app_store_version_id']}
    items_path = f'/v1/reviewSubmissions/{submission["id"]}/items?include=appStoreVersion'
    items = api.items(items_path)
    version_links = [item.get('relationships', {}).get('appStoreVersion', {}).get('data') for item in items]
    if any(link and link != version_link for link in version_links):
        raise ValueError('The editable review submission already contains a different version')
    if version_link not in version_links:
        if not submission['attributes'].get('platform') and items:
            raise ValueError('A review submission with existing items has no assigned platform')
        if receipt['observed_draft_state'] == 'READY_FOR_REVIEW':
            raise ValueError('The version is already assigned to a different review submission')
        api.request('/v1/reviewSubmissionItems', {'data': {
            'type': 'reviewSubmissionItems',
            'relationships': {'reviewSubmission': {'data': {'type': submission['type'], 'id': submission['id']}},
                              'appStoreVersion': {'data': version_link}},
        }})
        items = api.items(items_path)
    matches = [entry for entry in items if entry.get('relationships', {}).get('appStoreVersion', {}).get('data')
               == version_link]
    item = exact_record(matches, 'reviewSubmissionItems', lambda attrs: attrs['state'] == 'READY_FOR_REVIEW')
    return {**receipt, 'review_submission_id': submission['id'], 'review_item_id': item['id'],
            'review_item_state': item['attributes']['state']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True, choices=PLATFORMS)
    parser.add_argument('--version', required=True)
    parser.add_argument('--build-number', required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--inspect-only', action='store_true')
    modes.add_argument('--add-for-review', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version) or not re.fullmatch(r'[1-9]\d*', args.build_number):
        parser.error('Expected a stable release version and positive numeric build')
    with tempfile.TemporaryDirectory(prefix='pleazyng-apple-review-') as directory:
        api = AppleProfileAPI(_apple_api_token(Path(directory)))
        if args.add_for_review:
            receipt = add_version_for_review(api, args.platform, args.version, args.build_number)
        else:
            receipt = prepare(api, args.platform, args.version, args.build_number, inspect_only=args.inspect_only)
    args.receipt.write_text(json.dumps(receipt, indent=2) + '\n')
    if args.add_for_review:
        action = 'Verified the app version in its draft review submission for'
    else:
        action = 'Inspected the review draft for' if args.inspect_only else 'Verified the attached App Store build for'
    print(f'{action} {args.platform} {args.version} ({args.build_number})')


if __name__ == '__main__':
    main()
