#!/usr/bin/env python3
"""Place a verified Open media screenshot in its matching App Store draft."""

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
import tempfile
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from build_apple_store_archive import AppleProfileAPI, BUNDLE, _apple_api_token
from stores.prepare_apple_review_draft import APP_ID, exact_record, prepare


SCREENSHOT_TYPE = 'APP_SCREENSHOT'
CATEGORY = 'APP_SCREENSHOTS_AND_PREVIEWS'
MAX_IMAGE_BYTES = 16 * 1024 * 1024
OPEN_MEDIA_SCENE = 'Post-onboarding local Files and Books screen'


@dataclass(frozen=True)
class Listing:
    name: str
    device: str
    filename: str
    catalog_platform: str
    group: str
    sizes: frozenset[tuple[int, int]]


LISTINGS = {
    'IOS': Listing('iPad', 'ipad', 'ipad-01-files-and-books.png', 'IPAD_APP_STORE', 'IPAD_13_PROFILE',
                   frozenset({(2064, 2752), (2752, 2064), (2048, 2732), (2732, 2048)})),
    'MAC_OS': Listing('Mac', 'macos', 'macos-01-files-and-books.png', 'MAC_APP_STORE', 'MAC_PROFILE',
                      frozenset({(1280, 800), (1440, 900), (2560, 1600), (2880, 1800)})),
    'TV_OS': Listing('Apple TV', 'tvos', 'tvos-01-files-and-books-localtest.png', 'TV_APP_STORE', 'TV_PROFILE',
                     frozenset({(1920, 1080)})),
}


def read_capture(directory, platform, version, build, source, run, attempt, workflow_source):
    listing = LISTINGS[platform]
    data = json.loads((directory / 'capture-provenance.json').read_text())
    if platform == 'TV_OS':
        size, expected_digest, expected_bytes = read_tv_provenance(
            data, listing.filename, version, build, source, run, attempt, workflow_source)
    else:
        size, expected_digest = read_native_provenance(
            data, platform, listing.filename, version, build, source, workflow_source)
        expected_bytes = None

    with (directory / listing.filename).open('rb') as stream:
        image = stream.read(MAX_IMAGE_BYTES + 1)
    digest = hashlib.sha256(image).hexdigest()
    if (not 33 <= len(image) <= MAX_IMAGE_BYTES or (expected_bytes is not None and len(image) != expected_bytes)
            or digest != expected_digest or size not in listing.sizes
            or image[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
            or struct.unpack('>IIBB', image[16:26]) != (*size, 8, 2)):
        raise ValueError(f'Unexpected {listing.name} screenshot digest, PNG format, or dimensions')
    return image, digest, size


def read_tv_provenance(data, filename, version, build, source, run, attempt, workflow_source):
    product = data['product']
    capture = data['capture']
    lineage = product['source_lineage']
    record = data['screenshots'][filename]
    if ((product['version'], product['build'], product['bundle_id'], product['source_sha'])
            != (version, build, BUNDLE, source)
            or lineage['workflow_source_sha'] != workflow_source
            or (capture['run_id'], capture['run_attempt'], capture['platform']) != (run, attempt, 'tvos')):
        raise ValueError('Screenshot capture does not match the exact published tvOS release')
    return tuple(record['dimensions']), record['sha256'], record['size_bytes']


def read_native_provenance(data, platform, filename, version, build, source, workflow_source):
    manifest_platform, capture_platform, configuration = {
        'IOS': ('iOS and iPadOS', 'iOS', 'Debug Simulator'),
        'MAC_OS': ('macOS', 'macOS', 'Release Archive'),
    }[platform]
    source_tag = data['source_tag']
    defines = ['SUPPORTER_MODE=iap', 'ENABLE_UPDATE_CHECK=false', f'GIT_COMMIT={source}',
               'PLEAZYNG_DISCORD_APPLICATION_ID=']
    if (data['schema_version'] != 1 or data['product'] != 'Pleazyng'
            or data['platform'] != manifest_platform or data['build_mode'] != 'App Store IAP'
            or data['build_configuration'] != configuration or data['build_defines'] != defines
            or source_tag not in {f'v{version}', f'v{version}-candidate.{source}'}
            or (data['product_version'], data['product_build'], data['source_sha'], data['capture_tools_sha'])
            != (version, build, source, workflow_source)):
        raise ValueError(f'Screenshot manifest does not match the published {manifest_platform} release')
    matches = [capture for capture in data['captures'] if capture['filename'] == filename]
    if len(matches) != 1:
        raise ValueError('Expected one Open media screenshot in the verified capture manifest')
    record = matches[0]
    if ((record['app_version'], record['app_build'], record['bundle_id'], record['source_sha'], record['source_tag'])
            != (version, build, BUNDLE, source, source_tag)
            or record['capture_tools_sha'] != workflow_source or record['platform'] != capture_platform
            or record['build_configuration'] != configuration or record['build_defines'] != defines
            or not record['capture_scene'].startswith((f'{OPEN_MEDIA_SCENE}.', f'{OPEN_MEDIA_SCENE} after '))
            or not record['qualification'] or not re.fullmatch(r'[0-9a-f]{64}', record['app_bundle_sha256'])):
        raise ValueError(f'Unexpected {manifest_platform} screenshot provenance')
    return tuple(record['dimensions']), record['capture_sha256']


def screenshot_spec(api, listing, size, size_bytes):
    data = exact_record(api.items('/v1/appAssetLibraryRefData'), 'appAssetLibraryRefData', lambda _: True)
    catalog = data['attributes']
    types = [entry for entry in catalog['placementTypes'] if entry['placementTypeId'] == SCREENSHOT_TYPE]
    if len(types) != 1 or CATEGORY not in types[0]['acceptsAssetCategories']:
        raise ValueError('Apple does not accept screenshot images in the expected asset category')
    limits = {
        group: limit['maxCount']
        for feature in catalog['features'] if feature['featureId'] == 'APP_STORE_VERSIONS'
        for policy in feature['placementPolicies'] if policy['placementType'] == SCREENSHOT_TYPE
        for limit in policy['groupLimits'] for group in limit['groupIds']
    }
    groups = {group['placementProfileGroupId'] for group in catalog['placementProfileGroups']
              if group['platform'] == listing.catalog_platform}
    width, height = size
    specs = set()
    for spec in catalog['imageSpecs']:
        dimensions = spec.get('dimensions') or {}
        if (dimensions.get('minWidth', width + 1) <= width <= dimensions.get('maxWidth', width - 1)
                and dimensions.get('minHeight', height + 1) <= height <= dimensions.get('maxHeight', height - 1)
                and 'image/png' in spec['mimeTypes'] and size_bytes <= spec['maxFileSize']):
            specs.add(spec['specId'])
    accepted_specs = {spec for mapping in types[0]['specMappings'] if mapping['placementGroupId'] == listing.group
                      for spec in mapping['specs']} & specs
    if listing.group not in groups or not limits.get(listing.group) or not accepted_specs:
        raise ValueError(f'Apple does not accept the {size[0]}×{size[1]} PNG for {listing.group}')
    return accepted_specs, limits[listing.group]


def upload_parts(api, operations, image):
    offset = 0
    for operation in sorted(operations, key=lambda item: item['offset']):
        url = urlsplit(operation['url'])
        length = operation['length']
        headers = {item['name']: item['value'] for item in operation['requestHeaders']}
        if (operation['method'] != 'PUT' or url.scheme != 'https' or url.username or url.password
                or not re.fullmatch(r'(?:[a-z0-9-]+\.)+(?:blobstore|object-storage)\.apple\.com', url.netloc)
                or operation['offset'] != offset or not 0 < length <= len(image) - offset
                or {'authorization', 'cookie', 'proxy-authorization'} & {name.lower() for name in headers}):
            # Apple storage URLs and header values are temporary credentials. Keep those out of logs.
            raise ValueError('Unexpected Apple asset-upload operation '
                             f'(method {operation["method"]}, scheme {url.scheme}, host {url.hostname}, '
                             f'offset {operation["offset"]}, length {length}, expected offset {offset}, '
                             f'image bytes {len(image)}, header names {sorted(headers)})')
        request = Request(operation['url'], data=image[offset:offset + length], headers=headers, method='PUT')
        try:
            with api.opener.open(request, timeout=60) as response:
                if response.status not in (200, 201, 202, 204):
                    raise ValueError('Apple asset storage did not accept the screenshot part')
        except OSError:
            raise RuntimeError('Apple asset storage could not accept the screenshot upload') from None
        offset += length
    if offset != len(image):
        raise ValueError('Apple upload operations did not cover the complete screenshot')


def wait_for_asset(api, path, *, accepted, pending):
    for _ in range(24):
        item = api.request(path)['data']
        state = item['attributes']['state']
        if state in accepted:
            return item
        if state not in pending:
            raise RuntimeError(f'Apple cannot use the screenshot in state {state}')
        time.sleep(5)
    raise TimeoutError('Apple is still processing the exact screenshot')


def stage(api, platform, version, build, image, digest, size):
    listing = LISTINGS[platform]
    inspection = prepare(api, platform, version, build, inspect_only=True)
    if not inspection['exact_build_attached'] or inspection['observed_draft_state'] != 'PREPARE_FOR_SUBMISSION':
        raise ValueError(f'The exact attached build must be an editable {listing.name} review draft')
    localizations = [locale for locale in inspection['localizations'] if locale['locale'] == 'en-US']
    if len(localizations) != 1:
        raise ValueError('Expected one en-US App Store version localization')
    locale_id = localizations[0]['id']
    library = api.request(f'/v1/apps/{APP_ID}/assetLibrary')['data']
    if library.get('type') != 'appAssetLibraries':
        raise ValueError('App Store Connect returned an unexpected asset library')
    group = listing.group
    accepted_specs, limit = screenshot_spec(api, listing, size, len(image))

    reference = f'Pleazyng {version} {listing.name} Open media {digest[:16]}'
    filter_query = urlencode({'filter[referenceName]': reference})
    assets = api.items(f'/v1/appAssetLibraries/{library["id"]}/images?{filter_query}')
    expected_name = f'pleazyng-{version}-{listing.device}-open-media.png'
    if assets:
        asset = exact_record(assets, 'appAssetLibraryImages', lambda meta:
                             meta['referenceName'] == reference and meta['fileName'] == expected_name
                             and meta['fileSize'] == len(image) and meta['category'] == CATEGORY)
    else:
        if sum(item['group'] == group for item in localizations[0]['screenshots']) >= limit:
            raise ValueError(f'The {listing.name} screenshot group has reached its placement limit')
        asset = api.request('/v1/appAssetLibraryImages', {'data': {
            'type': 'appAssetLibraryImages',
            'attributes': {'fileName': expected_name, 'fileSize': len(image), 'category': CATEGORY,
                           'referenceName': reference},
            'relationships': {'assetLibrary': {'data': {'type': library['type'], 'id': library['id']}}},
        }})['data']
    image_path = f'/v1/appAssetLibraryImages/{asset["id"]}'
    if asset['attributes']['state'] == 'AWAITING_UPLOAD':
        upload_parts(api, asset['attributes']['uploadOperations'], image)
        api.request(image_path, {'data': {'type': 'appAssetLibraryImages', 'id': asset['id'],
                                          'attributes': {'uploaded': True}}}, method='PATCH')
    asset = wait_for_asset(api, image_path, accepted={'PREPARE_FOR_SUBMISSION', 'APPROVED'},
                           pending={'UPLOAD_COMPLETE', 'PROCESSING'})
    if asset['attributes']['specId'] not in accepted_specs:
        raise ValueError('Apple processed the image against an incompatible screenshot specification')

    placement_query = urlencode({'filter[appStoreVersionLocalization]': locale_id,
                                 'filter[placementType]': SCREENSHOT_TYPE, 'filter[placementGroup]': group})
    placements = api.items(f'{image_path}/placements?{placement_query}')
    if placements:
        placement = exact_record(placements, 'appAssetLibraryPlacements', lambda attrs:
                                 attrs['placementGroup'] == group and attrs['placementType'] == SCREENSHOT_TYPE)
    else:
        if sum(item['group'] == group for item in localizations[0]['screenshots']) >= limit:
            raise ValueError(f'The {listing.name} screenshot group has reached its placement limit')
        placement = api.request('/v1/appAssetLibraryPlacements', {'data': {
            'type': 'appAssetLibraryPlacements',
            'attributes': {'placementType': SCREENSHOT_TYPE, 'placementGroup': group},
            'relationships': {
                'image': {'data': {'type': 'appAssetLibraryImages', 'id': asset['id']}},
                'appStoreVersionLocalization': {'data': {'type': 'appStoreVersionLocalizations', 'id': locale_id}},
            },
        }})['data']
    placement = wait_for_asset(api, f'/v1/appAssetLibraryPlacements/{placement["id"]}',
                               accepted={'ACTIVE'}, pending={'ASSET_PROCESSING'})
    return {'version': version, 'build': build, 'platform': platform, 'locale': 'en-US',
            'capture_sha256': digest, 'placement_group': group, 'placement_id': placement['id'],
            'image_id': asset['id'], 'placement_state': placement['attributes']['state']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=LISTINGS, required=True)
    for field in ('version', 'build', 'source', 'capture-run', 'capture-attempt', 'capture-workflow-source'):
        parser.add_argument(f'--{field}', required=True)
    parser.add_argument('--capture-dir', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    image, digest, size = read_capture(args.capture_dir, args.platform, args.version, args.build, args.source,
                                       args.capture_run, args.capture_attempt, args.capture_workflow_source)
    with tempfile.TemporaryDirectory(prefix='pleazyng-apple-screenshots-') as directory:
        api = AppleProfileAPI(_apple_api_token(Path(directory)))
        receipt = stage(api, args.platform, args.version, args.build, image, digest, size)
    args.receipt.write_text(json.dumps(receipt, indent=2) + '\n')
    print(f'{LISTINGS[args.platform].name} Open media screenshot is active on the {args.version} draft')


if __name__ == '__main__':
    main()
