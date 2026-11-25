import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

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

    def previous_draft(self, state='PREPARE_FOR_SUBMISSION'):
        record = {'type': 'appStoreVersions', 'id': 'draft', 'attributes': {
            'platform': 'TV_OS', 'versionString': '3.0.50', 'appVersionState': state,
        }}
        self.api.items.side_effect = [[], [record], [{**self.exact, 'attributes': self.build}]]

    def test_update_previous_draft_and_attach_build_in_one_request(self):
        self.previous_draft()
        draft_path = '/v1/appStoreVersions/draft'

        def request(path, body=None, *, method=None):
            if path != draft_path:
                return self.request(path, body, method=method)
            if method == 'PATCH':
                self.assertEqual(body, {'data': {
                    'type': 'appStoreVersions', 'id': 'draft', 'attributes': {'versionString': '3.0.51'},
                    'relationships': {'build': {'data': self.exact}},
                }})
                self.relationships = {'data': self.exact}
            return {'data': {'type': 'appStoreVersions', 'id': 'draft', 'attributes': self.draft}}

        self.api.request.side_effect = request
        receipt = review.prepare(self.api, 'TV_OS', '3.0.51', '210', previous_draft='3.0.50')
        self.assertEqual(receipt['app_store_version_id'], 'draft')
        self.assertEqual(receipt['previous_draft_version'], '3.0.50')
        self.assertTrue(receipt['changed'])
        patches = [call for call in self.api.request.call_args_list if call.kwargs.get('method') == 'PATCH']
        self.assertEqual([call.args[0] for call in patches], [draft_path])

    def test_previous_draft_in_review_is_preserved(self):
        self.previous_draft(state='WAITING_FOR_REVIEW')
        with self.assertRaisesRegex(ValueError, 'Prepare for Submission'):
            review.prepare(self.api, 'TV_OS', '3.0.51', '210', previous_draft='3.0.50')
        self.assert_no_patch()

    def test_previous_draft_is_preserved_until_new_build_is_valid(self):
        self.previous_draft()
        self.build['processingState'] = 'PROCESSING'
        with self.assertRaisesRegex(ValueError, 'VALID and unexpired'):
            review.prepare(self.api, 'TV_OS', '3.0.51', '210', previous_draft='3.0.50')
        self.assert_no_patch()

    def test_existing_target_draft_takes_priority(self):
        self.relationships = {'data': self.exact}
        receipt = review.prepare(self.api, 'TV_OS', '3.0.51', '210', previous_draft='3.0.50')
        self.assertFalse(receipt['changed'])
        self.assertNotIn('previous_draft_version', receipt)
        self.assert_no_patch()

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
            [{'type': 'reviewSubmissions', 'id': 'review-id',
              'attributes': {'platform': 'TV_OS', 'state': 'READY_FOR_REVIEW', 'submittedDate': None}}],
            [{'type': 'reviewSubmissionItems', 'attributes': {'state': 'READY_FOR_REVIEW'},
              'relationships': {'appStoreVersion': {'data': {'type': 'appStoreVersions', 'id': 'draft'}}}}],
        ]
        receipt = review.prepare(self.api, 'TV_OS', '3.0.51', '210', inspect_only=True)
        self.assertFalse(receipt['exact_build_attached'])
        self.assertFalse(receipt['changed'])
        self.assertEqual(receipt['localizations'][0]['screenshots'], [{'group': 'TV', 'state': 'APPROVED'}])
        self.assertEqual(receipt['purchases'][0]['state'], 'READY_TO_SUBMIT')
        self.assertEqual(receipt['review_submissions'][0]['state'], 'READY_FOR_REVIEW')
        self.assertTrue(receipt['review_submissions'][0]['items'][0]['exact_app_version'])
        self.assertEqual(receipt['app_review_detail'], {
            'contact_complete': True, 'demo_account_required': False, 'notes_present': True,
        })
        self.assertNotIn('test@example.invalid', str(receipt))
        self.assert_no_patch()

    def assert_no_patch(self):
        self.assertFalse(any(call.kwargs.get('method') == 'PATCH' for call in self.api.request.call_args_list))


class AddVersionForReviewTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.version_link = {'type': 'appStoreVersions', 'id': 'tv-version'}
        self.item = {'type': 'reviewSubmissionItems', 'id': 'review-item',
                     'attributes': {'state': 'READY_FOR_REVIEW'},
                     'relationships': {'appStoreVersion': {'data': self.version_link}}}
        self.submission = {'type': 'reviewSubmissions', 'id': 'review-id',
                           'attributes': {'platform': 'TV_OS', 'state': 'READY_FOR_REVIEW', 'submittedDate': None}}
        self.listing = {'locale': 'en-US', 'description_present': True, 'support_url_present': True,
                        'screenshots': [{'group': 'TV_PROFILE', 'state': 'ACTIVE'}]}
        self.receipt = {'exact_build_attached': True, 'observed_draft_state': 'PREPARE_FOR_SUBMISSION',
                        'app_store_version_id': 'tv-version', 'localizations': [self.listing],
                        'app_review_detail': {'contact_complete': True}}

    def test_add_exact_version_to_editable_review_without_submitting(self):
        self.api.items.side_effect = [[], [], [self.item]]
        self.api.request.side_effect = [{'data': self.submission}, {'data': self.item}]
        with patch.object(review, 'prepare', return_value=self.receipt):
            receipt = review.add_version_for_review(self.api, 'TV_OS', '3.0.51', '210')
        self.assertEqual(receipt['review_item_state'], 'READY_FOR_REVIEW')
        creation, addition = self.api.request.call_args_list
        self.assertEqual(creation.args[0], '/v1/reviewSubmissions')
        self.assertEqual(addition.args[0], '/v1/reviewSubmissionItems')
        self.assertEqual(addition.args[1]['data']['relationships']['appStoreVersion']['data'], self.version_link)

    def test_existing_exact_review_item_is_reused(self):
        self.receipt['observed_draft_state'] = 'READY_FOR_REVIEW'
        self.api.items.side_effect = [[self.submission], [self.item]]
        with patch.object(review, 'prepare', return_value=self.receipt):
            receipt = review.add_version_for_review(self.api, 'TV_OS', '3.0.51', '210')
        self.assertEqual(receipt['review_item_id'], 'review-item')
        self.api.request.assert_not_called()

    def test_existing_empty_draft_without_platform_is_reused(self):
        self.submission['attributes']['platform'] = None
        self.api.items.side_effect = [[self.submission], [], [self.item]]
        self.api.request.return_value = {'data': self.item}
        with patch.object(review, 'prepare', return_value=self.receipt):
            receipt = review.add_version_for_review(self.api, 'TV_OS', '3.0.51', '210')
        self.assertEqual(receipt['review_submission_id'], 'review-id')
        self.assertEqual(self.api.request.call_args.args[0], '/v1/reviewSubmissionItems')
        self.api.request.assert_called_once()

    def test_stop_before_creating_a_draft_when_screenshot_missing(self):
        self.listing['screenshots'] = []
        with patch.object(review, 'prepare', return_value=self.receipt):
            with self.assertRaisesRegex(ValueError, 'requires active screenshots'):
                review.add_version_for_review(self.api, 'TV_OS', '3.0.51', '210')
        self.api.items.assert_not_called()
        self.api.request.assert_not_called()


    def test_occupied_unassigned_draft_is_left_alone(self):
        foreign = {**self.submission, 'id': 'foreign', 'attributes': {
            **self.submission['attributes'], 'platform': None,
        }}
        self.api.items.side_effect = [[foreign], [{'type': 'reviewSubmissionItems'}], [], [self.item]]
        self.api.request.side_effect = [{'data': self.submission}, {'data': self.item}]
        with patch.object(review, 'prepare', return_value=self.receipt):
            result = review.add_version_for_review(self.api, 'TV_OS', '3.0.51', '210')
        self.assertEqual(result['review_submission_id'], 'review-id')
        self.assertEqual(self.api.request.call_args_list[0].args[0], '/v1/reviewSubmissions')


class SubmitAppleReviewTests(unittest.TestCase):
    def test_submits_the_exact_version_and_first_purchases_together(self):
        api = Mock()
        purchases = [{'type': 'inAppPurchaseVersions', 'id': name} for name in ('trial', 'unlock')]
        receipt = {'exact_build_attached': True, 'observed_draft_state': 'PREPARE_FOR_SUBMISSION',
                   'release_type': 'AFTER_APPROVAL', 'app_store_version_id': 'ios-version',
                   'app_review_detail': {'notes_present': True}}
        expected = {'type': 'appStoreVersions', 'id': 'ios-version'}
        app_item = {'attributes': {'state': 'READY_FOR_REVIEW'},
                    'relationships': {'appStoreVersion': {'data': expected}}}
        purchase_items = [{'attributes': {'state': 'READY_FOR_REVIEW'},
                           'relationships': {'inAppPurchaseVersion': {'data': link}}} for link in purchases]
        api.items.side_effect = [[app_item], [app_item, *purchase_items]]
        api.request.side_effect = [{}, {}, {}, {'data': {'type': 'reviewSubmissions', 'id': 'review-id',
            'attributes': {'platform': 'IOS', 'state': 'WAITING_FOR_REVIEW', 'submittedDate': 'date'}}}]
        with patch.object(review, 'prepare', return_value=receipt), \
             patch.object(review, 'add_version_for_review', return_value={**receipt, 'review_submission_id': 'review-id'}), \
             patch.object(review, 'first_purchase_versions', return_value=purchases) as products:
            result = review.submit_for_review(api, 'IOS', '3.0.51', '210', first_purchases=True)
        products.assert_called_once_with(api, first_purchases=True)
        self.assertEqual(result['submitted_purchase_count'], 2)
        self.assertEqual(result['verified_review_state'], 'WAITING_FOR_REVIEW')
        self.assertEqual(api.request.call_args_list[2].args[1]['data']['attributes'], {'submitted': True})

    def test_in_review_version_is_observed_without_additional_mutations(self):
        receipt = {'exact_build_attached': True, 'observed_draft_state': 'IN_REVIEW'}
        api = Mock()
        with patch.object(review, 'prepare', return_value=receipt):
            self.assertEqual(review.submit_for_review(api, 'IOS', '3.0.51', '210')['submission_outcome'],
                             'already_submitted')
        api.request.assert_not_called()
