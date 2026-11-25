import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stores import prepare_apple_review_draft as review


class PrepareAppleReviewDraftTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.relationship = '/v1/appStoreVersions/draft/relationships/build'
        self.exact = {'id': 'tv-build', 'type': 'builds'}
        self.wrong_platform = {'id': 'phone-build', 'type': 'builds'}
        self.relationships = {'data': {'id': 'previous-build', 'type': 'builds'}}
        self.draft = {'platform': 'TV_OS', 'versionString': '3.0.51',
                      'appVersionState': 'PREPARE_FOR_SUBMISSION'}
        self.build = {'version': '210', 'processingState': 'VALID', 'expired': False}
        self.api.items.side_effect = [
            [{'type': 'appStoreVersions', 'id': 'draft', 'attributes': self.draft}],
            [{**self.wrong_platform, 'attributes': self.build}, {**self.exact, 'attributes': self.build}],
        ]
        self.api.request.side_effect = self.request

    def request(self, path, body=None, *, method=None):
        if path == f'/v1/apps/{review.APP_ID}':
            return {'data': {'type': 'apps', 'id': review.APP_ID, 'attributes': {'bundleId': review.BUNDLE}}}
        if path in ('/v1/builds/tv-build/preReleaseVersion', '/v1/builds/phone-build/preReleaseVersion'):
            platform = 'TV_OS' if 'tv-build' in path else 'IOS'
            return {'data': {'type': 'preReleaseVersions', 'attributes': {'platform': platform, 'version': '3.0.51'}}}
        if path.startswith('/v1/appStoreVersions/draft?include=appStoreReviewDetail&'):
            return {'included': [{'type': 'appStoreReviewDetails', 'attributes': {
                'contactFirstName': 'Example', 'contactLastName': 'Reviewer', 'contactPhone': '5550100',
                'contactEmail': 'test@example.invalid', 'demoAccountRequired': False, 'notes': 'Private review notes',
            }}]}
        self.assertEqual(path, self.relationship)
        if method == 'PATCH':
            self.assertEqual(body, {'data': self.exact})
            self.relationships = body
        return self.relationships

    def test_attach_exact_platform_and_read_back(self):
        receipt = review.prepare(self.api, 'TV_OS', '3.0.51', '210')
        self.assertEqual(receipt['asc_build_id'], 'tv-build')
        self.assertTrue(receipt['changed'])
        self.api.request.assert_called_with(self.relationship)

    def test_preserve_in_review_draft(self):
        self.draft['appVersionState'] = 'WAITING_FOR_REVIEW'
        with self.assertRaisesRegex(ValueError, 'no longer an editable review draft'):
            review.prepare(self.api, 'TV_OS', '3.0.51', '210')
        self.assert_no_patch()

    def test_refuse_build_still_processing(self):
        self.build['processingState'] = 'PROCESSING'
        with self.assertRaisesRegex(ValueError, 'VALID and unexpired'):
            review.prepare(self.api, 'TV_OS', '3.0.51', '210')
        self.assert_no_patch()

    def test_already_attached_does_not_change_relationship(self):
        self.relationships = {'data': self.exact}
        self.assertFalse(review.prepare(self.api, 'TV_OS', '3.0.51', '210')['changed'])
        self.assert_no_patch()

    def test_inspect_reports_gaps_without_attaching_a_build(self):
        self.api.items.side_effect = [
            *tuple(self.api.items.side_effect),
            [{'id': 'en-id', 'attributes': {'locale': 'en-US', 'description': 'Media player'}}],
            [{'attributes': {'placementGroup': 'TV', 'placementType': 'APP_SCREENSHOT', 'state': 'APPROVED'}}],
            [{'attributes': {'productId': 'pleazyng_lifetime_unlock', 'state': 'READY_TO_SUBMIT'}}],
            [{'attributes': {'platform': 'TV_OS', 'state': 'READY_FOR_REVIEW', 'submittedDate': None}}],
        ]
        receipt = review.prepare(self.api, 'TV_OS', '3.0.51', '210', inspect_only=True)
        self.assertFalse(receipt['exact_build_attached'])
        self.assertFalse(receipt['changed'])
        self.assertEqual(receipt['localizations'][0]['screenshots'], [{'group': 'TV', 'state': 'APPROVED'}])
        self.assertEqual(receipt['purchases'][0]['state'], 'READY_TO_SUBMIT')
        self.assertEqual(receipt['review_submissions'][0]['state'], 'READY_FOR_REVIEW')
        self.assertEqual(receipt['app_review_detail'], {
            'contact_complete': True, 'demo_account_required': False, 'notes_present': True,
        })
        self.assertNotIn('test@example.invalid', str(receipt))
        self.assert_no_patch()

    def assert_no_patch(self):
        self.assertFalse(any(call.kwargs.get('method') == 'PATCH' for call in self.api.request.call_args_list))
