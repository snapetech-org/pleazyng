from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stores import stage_apple_tv_screenshot as screenshot


class AppleTvScreenshotTests(unittest.TestCase):
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
            'placementGroup': 'tv-group', 'placementType': 'APP_SCREENSHOT', 'state': 'ACTIVE',
        }}
        api.items.side_effect = [[image], [placement]]
        api.request.side_effect = [{'data': library}, {'data': image}, {'data': placement}]
        inspection = {'exact_build_attached': True, 'observed_draft_state': 'PREPARE_FOR_SUBMISSION',
                      'localizations': [{'id': 'en-localization', 'locale': 'en-US', 'screenshots': []}]}
        with patch.object(screenshot, 'prepare', return_value=inspection), \
             patch.object(screenshot, 'screenshot_spec', return_value=('tv-group', {'the-spec'}, 10)):
            result = screenshot.stage(api, '3.0.51', '210', b'png', digest)
        self.assertEqual(result['placement_id'], 'the-placement')
        self.assertEqual(result['placement_state'], 'ACTIVE')
        self.assertTrue(all(len(call.args) == 1 for call in api.request.call_args_list))
