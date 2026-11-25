import hashlib
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stores import stage_apple_listing_screenshot as screenshot


class AppleListingScreenshotTests(unittest.TestCase):
    def test_provenance_selects_the_exact_ipad_capture_and_rejects_alpha(self):
        source = 'a' * 40
        workflow_source = 'b' * 40
        defines = ['SUPPORTER_MODE=iap', 'ENABLE_UPDATE_CHECK=false', f'GIT_COMMIT={source}',
                   'PLEAZYNG_DISCORD_APPLICATION_ID=']
        dimensions = (2064, 2752)
        filename = 'ipad-01-files-and-books.png'
        manifest = {'schema_version': 1, 'product': 'Pleazyng', 'product_version': '3.0.51',
                    'product_build': '210', 'source_tag': 'v3.0.51', 'source_sha': source,
                    'capture_tools_sha': workflow_source, 'platform': 'iOS and iPadOS',
                    'build_mode': 'App Store IAP', 'build_configuration': 'Debug Simulator', 'build_defines': defines}
        record = {'filename': filename, 'app_version': '3.0.51', 'app_build': '210', 'bundle_id': screenshot.BUNDLE,
                  'source_sha': source, 'source_tag': 'v3.0.51', 'capture_tools_sha': workflow_source,
                  'platform': 'iOS', 'build_configuration': 'Debug Simulator', 'build_defines': defines,
                  'capture_scene': ('Post-onboarding local Files and Books screen after the real trial CTA completed '
                                    'one isolated Xcode StoreKitTest LOCALTEST purchase.'),
                  'qualification': 'Exact product on an iPad Simulator', 'app_bundle_sha256': 'c' * 64,
                  'dimensions': dimensions}
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            args = (directory, 'IOS', '3.0.51', '210', source, '1', '1', workflow_source)
            for color_type in (2, 6):
                image = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + struct.pack('>IIBB', *dimensions, 8, color_type) + b'\0' * 7
                record['capture_sha256'] = hashlib.sha256(image).hexdigest()
                (directory / filename).write_bytes(image)
                (directory / 'capture-provenance.json').write_text(json.dumps({**manifest, 'captures': [record]}))
                if color_type == 2:
                    self.assertEqual(screenshot.read_capture(*args), (image, record['capture_sha256'], dimensions))
                else:
                    with self.assertRaisesRegex(ValueError, 'PNG format'):
                        screenshot.read_capture(*args)

    def test_asset_upload_refuses_untrusted_destinations(self):
        api = Mock()
        operation = {'url': 'https://blobstore.apple.com.example.invalid/asset', 'method': 'PUT',
                     'offset': 0, 'length': 3, 'requestHeaders': []}
        with self.assertRaisesRegex(ValueError, 'Unexpected Apple asset-upload operation'):
            screenshot.upload_parts(api, [operation], b'png')
        api.opener.open.assert_not_called()

    def test_asset_upload_uses_only_apple_operations_and_exact_bytes(self):
        api = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=SimpleNamespace(status=200))
        response.__exit__ = Mock(return_value=False)
        api.opener.open.return_value = response
        base = {'method': 'PUT', 'requestHeaders': []}
        screenshot.upload_parts(api, [
            {**base, 'url': 'https://store-030.blobstore.apple.com/asset', 'offset': 2, 'length': 1},
            {**base, 'url': 'https://northamerica-1.object-storage.apple.com/asset', 'offset': 0, 'length': 2},
        ], b'png')
        self.assertEqual([call.args[0].data for call in api.opener.open.call_args_list], [b'pn', b'g'])

    def test_existing_image_and_placement_are_reused(self):
        api = Mock()
        digest = 'a' * 64
        library = {'id': 'the-library', 'type': 'appAssetLibraries'}
        image = {'id': 'the-image', 'type': 'appAssetLibraryImages', 'attributes': {
            'referenceName': f'Pleazyng 3.0.51 Apple TV Open media {digest[:16]}',
            'fileName': 'pleazyng-3.0.51-tvos-open-media.png', 'fileSize': 3,
            'category': screenshot.CATEGORY, 'state': 'PREPARE_FOR_SUBMISSION', 'specId': 'the-spec',
        }}
        placement = {'id': 'the-placement', 'type': 'appAssetLibraryPlacements', 'attributes': {
            'placementGroup': 'TV_PROFILE', 'placementType': 'APP_SCREENSHOT', 'state': 'ACTIVE',
        }}
        api.items.side_effect = [[image], [placement]]
        api.request.side_effect = [{'data': library}, {'data': image}, {'data': placement}]
        inspection = {'exact_build_attached': True, 'observed_draft_state': 'PREPARE_FOR_SUBMISSION',
                      'localizations': [{'id': 'en-localization', 'locale': 'en-US', 'screenshots': []}]}
        with patch.object(screenshot, 'prepare', return_value=inspection), \
             patch.object(screenshot, 'screenshot_spec', return_value=({'the-spec'}, 10)):
            result = screenshot.stage(api, 'TV_OS', '3.0.51', '210', b'png', digest, (1920, 1080))
        self.assertEqual(result['placement_id'], 'the-placement')
        self.assertEqual(result['placement_state'], 'ACTIVE')
        self.assertTrue(all(len(call.args) == 1 for call in api.request.call_args_list))
