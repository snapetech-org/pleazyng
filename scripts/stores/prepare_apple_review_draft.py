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
PURCHASES = {'pleazyng_7_day_trial', 'pleazyng_lifetime_unlock'}
REVIEWING_OR_RELEASED = {
    'WAITING_FOR_REVIEW', 'IN_REVIEW', 'ACCEPTED', 'PENDING_APPLE_RELEASE',
    'READY_FOR_DISTRIBUTION', 'READY_FOR_SALE', 'PROCESSING_FOR_DISTRIBUTION',
}
EDITABLE = {'PREPARE_FOR_SUBMISSION', 'READY_FOR_REVIEW'}


class AppleStoreAPI(AppleProfileAPI):
    allowed_api_prefixes = ('/v1/', '/v2/')


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


def version_records(api, platform, version):
    filters = urlencode({'filter[platform]': platform, 'filter[versionString]': version})
    return api.items(f'/v1/apps/{APP_ID}/appStoreVersions?{filters}')


def exact_version(records, platform, version):
    return exact_record(records, 'appStoreVersions',
                        lambda attrs: attrs.get('platform') == platform and attrs.get('versionString') == version)


def version_state(draft):
    return draft['attributes'].get('appVersionState') or draft['attributes'].get('appStoreState')


def prepare(api, platform, version, build_number, *, inspect_only=False, previous_draft=None):
    if previous_draft is not None and (
        inspect_only or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', previous_draft)
        or tuple(map(int, previous_draft.split('.'))) >= tuple(map(int, version.split('.')))
    ):
        raise ValueError('The previous draft must name an earlier stable version and request an attachment')

    app = api.request(f'/v1/apps/{APP_ID}').get('data', {})
    if (app.get('type') != 'apps' or app.get('id') != APP_ID
            or app.get('attributes', {}).get('bundleId') != BUNDLE):
        raise ValueError('App Store Connect returned an unexpected app')

    drafts = version_records(api, platform, version)
    replace_draft = not drafts and previous_draft is not None
    if replace_draft:
        draft = exact_version(version_records(api, platform, previous_draft), platform, previous_draft)
        if version_state(draft) != 'PREPARE_FOR_SUBMISSION':
            raise ValueError('Only a previous draft in Prepare for Submission can be updated')
    else:
        draft = exact_version(drafts, platform, version)
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

    expected = {'type': 'builds', 'id': build['id']}
    draft_path = f'/v1/appStoreVersions/{draft["id"]}'
    relationship = f'{draft_path}/relationships/build'
    if replace_draft:
        api.request(draft_path, {'data': {
            'type': 'appStoreVersions', 'id': draft['id'], 'attributes': {'versionString': version},
            'relationships': {'build': {'data': expected}},
        }}, method='PATCH')
        updated = exact_version([api.request(draft_path)['data']], platform, version)
        if updated['id'] != draft['id']:
            raise ValueError('Apple did not retain the updated version on the same draft')
        draft = updated
    attached = api.request(relationship)['data']
    if replace_draft and attached != expected:
        raise ValueError('Apple did not retain the exact build on the updated draft')
    state = version_state(draft)
    changed = attached != expected
    if changed and not inspect_only:
        if state not in EDITABLE:
            raise ValueError('The exact store version is no longer an editable review draft')
        api.request(relationship, {'data': expected}, method='PATCH')
        if api.request(relationship)['data'] != expected:
            raise ValueError('Apple did not retain the exact draft build attachment')
    receipt = {'app_id': APP_ID, 'platform': platform, 'version': version, 'build_number': build_number,
               'app_store_version_id': draft['id'], 'asc_build_id': build['id'],
               'observed_draft_state': state, 'release_type': draft['attributes'].get('releaseType'), 'changed': (changed or replace_draft) and not inspect_only}
    if replace_draft:
        receipt['previous_draft_version'] = previous_draft
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
        if product['attributes'].get('productId') in PURCHASES
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


def app_version_link(receipt):
    return {'type': 'appStoreVersions', 'id': receipt['app_store_version_id']}


def draft_review_items(api, submission_id, *, purchases=False):
    related = 'appStoreVersion,inAppPurchaseVersion' if purchases else 'appStoreVersion'
    return api.items(f'/v1/reviewSubmissions/{submission_id}/items?' + urlencode({'include': related}))


def select_review_draft(api, platform, version_link, version_state):
    filters = urlencode({'filter[state]': 'READY_FOR_REVIEW'})
    existing = api.items(f'/v1/apps/{APP_ID}/reviewSubmissions?{filters}')
    eligible = []
    for entry in existing:
        if entry['attributes'].get('platform') not in {None, platform}:
            continue
        submission = exact_record([entry], 'reviewSubmissions', lambda attrs:
                                  attrs['state'] == 'READY_FOR_REVIEW' and attrs.get('submittedDate') is None)
        items = draft_review_items(api, submission['id'])
        versions = [item.get('relationships', {}).get('appStoreVersion', {}).get('data') for item in items]
        if version_link in versions:
            return submission, items, versions
        if not items:
            eligible.append((submission, items, versions))
    if version_state == 'READY_FOR_REVIEW':
        raise ValueError('The version already belongs to a review submission that could not be identified')
    if len(eligible) == 1:
        return eligible[0]
    if len(eligible) > 1:
        raise ValueError('Multiple editable Apple review submissions could accept this version')
    submission = api.request('/v1/reviewSubmissions', {'data': {
        'type': 'reviewSubmissions', 'attributes': {'platform': platform},
        'relationships': {'app': {'data': {'type': 'apps', 'id': APP_ID}}},
    }})['data']
    submission = exact_record([submission], 'reviewSubmissions', lambda attrs:
                              attrs.get('platform') in {None, platform} and attrs['state'] == 'READY_FOR_REVIEW'
                              and attrs.get('submittedDate') is None)
    items = draft_review_items(api, submission['id'])
    if items:
        raise ValueError('Apple returned a new review submission that already contains items')
    return submission, [], []


def add_version_for_review(api, platform, version, build_number, *, receipt=None):
    if receipt is None:
        receipt = prepare(api, platform, version, build_number, inspect_only=True)
    if not receipt['exact_build_attached'] or receipt['observed_draft_state'] not in EDITABLE:
        raise ValueError('The exact attached build must be available for a new review draft')
    listing = [locale for locale in receipt['localizations'] if locale['locale'] == 'en-US']
    if (len(listing) != 1 or not listing[0]['description_present'] or not listing[0]['support_url_present']
            or not receipt['app_review_detail']['contact_complete']):
        raise ValueError('Review contact, description and support URL are required')
    present_groups = {item['group'] for item in listing[0]['screenshots'] if item['state'] == 'ACTIVE'}
    missing_groups = REQUIRED_SCREENSHOT_GROUPS[platform] - present_groups
    if missing_groups:
        raise ValueError(f'Review requires active screenshots for {", ".join(sorted(missing_groups))}')

    version_link = app_version_link(receipt)
    submission, items, version_links = select_review_draft(api, platform, version_link, receipt['observed_draft_state'])
    if any(link and link != version_link for link in version_links):
        raise ValueError('The editable review submission already contains a different app version')
    if version_link not in version_links:
        api.request('/v1/reviewSubmissionItems', {'data': {
            'type': 'reviewSubmissionItems',
            'relationships': {'reviewSubmission': {'data': {'type': submission['type'], 'id': submission['id']}},
                              'appStoreVersion': {'data': version_link}},
        }})
        items = draft_review_items(api, submission['id'])
    matches = [entry for entry in items if entry.get('relationships', {}).get('appStoreVersion', {}).get('data')
               == version_link]
    item = exact_record(matches, 'reviewSubmissionItems', lambda attrs: attrs['state'] == 'READY_FOR_REVIEW')
    return {**receipt, 'review_submission_id': submission['id'], 'review_item_id': item['id'],
            'review_item_state': item['attributes']['state']}


def first_purchase_versions(api, *, first_purchases):
    products = api.items(f'/v1/apps/{APP_ID}/inAppPurchasesV2')
    links = []
    for product_id in sorted(PURCHASES):
        product = exact_record(products, 'inAppPurchases', lambda attrs:
                               attrs.get('productId') == product_id
                               and attrs.get('inAppPurchaseType') == 'NON_CONSUMABLE')
        state = product['attributes']['state']
        if state == 'APPROVED':
            continue
        if not first_purchases or state != 'READY_TO_SUBMIT':
            raise ValueError(f'Apple review is waiting for {product_id} ({state})')
        versions = api.items(f'/v2/inAppPurchases/{product["id"]}/versions')
        version = exact_record(versions, 'inAppPurchaseVersions', lambda attrs:
                               attrs.get('state') in {'PREPARE_FOR_SUBMISSION', 'READY_FOR_REVIEW'})
        links.append({'type': version['type'], 'id': version['id']})
    return links


def submit_for_review(api, platform, version, build_number, *, first_purchases=False):
    receipt = prepare(api, platform, version, build_number, inspect_only=True)
    if not receipt['exact_build_attached']:
        raise ValueError('Attach the exact processed Apple build before submitting')
    state = receipt['observed_draft_state']
    if state in REVIEWING_OR_RELEASED:
        return {**receipt, 'submission_outcome': 'already_submitted', 'verified_review_state': state}
    if state not in EDITABLE:
        raise ValueError(f'Apple version is not an editable review draft ({state})')
    if not receipt['app_review_detail']['notes_present']:
        raise ValueError('App Review notes are required for this listing')
    purchases = first_purchase_versions(api, first_purchases=first_purchases)
    draft_path = f'/v1/appStoreVersions/{receipt["app_store_version_id"]}'
    if receipt['release_type'] != 'AFTER_APPROVAL':
        api.request(draft_path, {'data': {
            **app_version_link(receipt), 'attributes': {'releaseType': 'AFTER_APPROVAL'},
        }}, method='PATCH')
        updated = exact_version([api.request(draft_path)['data']], platform, version)
        if updated['attributes'].get('releaseType') != 'AFTER_APPROVAL':
            raise ValueError('Apple did not retain automatic release after approval')
        receipt['release_type'] = 'AFTER_APPROVAL'
    receipt = add_version_for_review(api, platform, version, build_number, receipt=receipt)
    submission_id = receipt['review_submission_id']
    submission = {'type': 'reviewSubmissions', 'id': submission_id}
    version_link = app_version_link(receipt)
    allowed = [version_link, *purchases]

    def submitted_items():
        items = draft_review_items(api, submission_id, purchases=True)
        links = []
        for item in items:
            relations = item.get('relationships', {})
            link = next((relations.get(key, {}).get('data') for key in ('appStoreVersion', 'inAppPurchaseVersion')
                         if relations.get(key, {}).get('data')), None)
            if link not in allowed or item['attributes']['state'] != 'READY_FOR_REVIEW':
                raise ValueError('The Apple submission contains an unexpected or non-ready review item')
            links.append(link)
        return links

    existing = submitted_items()
    for purchase in purchases:
        if purchase in existing:
            continue
        api.request('/v1/reviewSubmissionItems', {'data': {
            'type': 'reviewSubmissionItems',
            'relationships': {'reviewSubmission': {'data': submission}, 'inAppPurchaseVersion': {'data': purchase}},
        }})
    final = submitted_items()
    if len(final) != len(allowed) or any(link not in final for link in allowed):
        raise ValueError('Apple did not retain the exact app version and requested purchase items')
    path = f'/v1/reviewSubmissions/{submission_id}'
    api.request(path, {'data': {**submission, 'attributes': {'submitted': True}}}, method='PATCH')
    acknowledged = exact_record([api.request(path)['data']], 'reviewSubmissions', lambda attrs:
                                attrs.get('platform') in {None, platform} and
                                (attrs.get('submittedDate') is not None or attrs['state'] != 'READY_FOR_REVIEW'))
    return {**receipt, 'submission_outcome': 'submitted_for_review',
            'verified_review_state': acknowledged['attributes']['state'], 'submitted_purchase_count': len(purchases)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True, choices=PLATFORMS)
    parser.add_argument('--version', required=True)
    parser.add_argument('--build-number', required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--previous-draft', help='Earlier editable draft to update if the target version does not exist')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--inspect-only', action='store_true')
    modes.add_argument('--add-for-review', action='store_true')
    modes.add_argument('--submit-for-review', action='store_true')
    parser.add_argument('--first-purchases', action='store_true',
                        help='Submit the two existing non-consumables with the first iOS release')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version) or not re.fullmatch(r'[1-9]\d*', args.build_number):
        parser.error('Expected a stable release version and positive numeric build')
    if args.previous_draft and (args.inspect_only or args.add_for_review or args.submit_for_review):
        parser.error('Updating a previous draft requires attachment mode')
    if args.first_purchases and (not args.submit_for_review or args.platform != 'IOS'):
        parser.error('First purchases require submitting the iOS app version')
    with tempfile.TemporaryDirectory(prefix='pleazyng-apple-review-') as directory:
        api = AppleStoreAPI(_apple_api_token(Path(directory)))
        if args.submit_for_review:
            receipt = submit_for_review(api, args.platform, args.version, args.build_number,
                                        first_purchases=args.first_purchases)
        elif args.add_for_review:
            receipt = add_version_for_review(api, args.platform, args.version, args.build_number)
        else:
            receipt = prepare(api, args.platform, args.version, args.build_number,
                              inspect_only=args.inspect_only, previous_draft=args.previous_draft)
    args.receipt.write_text(json.dumps(receipt, indent=2) + '\n')
    if args.submit_for_review:
        action = f'Apple App Review {receipt["submission_outcome"]} for'
    elif args.add_for_review:
        action = 'Verified the app version in its draft review submission for'
    else:
        action = 'Inspected the review draft for' if args.inspect_only else 'Verified the attached App Store build for'
    print(f'{action} {args.platform} {args.version} ({args.build_number})')


if __name__ == '__main__':
    main()
