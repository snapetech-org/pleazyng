import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stores import deliver_apple_releases as apple
from stores import launch_pending_macos_store as runner


class AppleDeliveryTests(unittest.TestCase):
    release = {'tag_name': 'v3.0.52', 'created_at': '2026-11-25T15:00:00Z'}

    def test_reuse_the_signed_core_ios_asset(self):
        inventory = Mock()
        inventory.find.return_value = ('missing', None)
        with patch.object(apple, 'dispatch', return_value='dispatched') as dispatch:
            self.assertEqual(apple.processing_run(inventory, 'IOS', 'v3.0.52',
                                                  {apple.ASSETS['IOS']}), ('dispatched', None))
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args, (apple.STORE,))
        self.assertNotIn('apple_source_run', dispatch.call_args.kwargs)

    def test_reuse_existing_focused_tvos_signing(self):
        inventory = Mock()
        inventory.find.side_effect = [('missing', None), ('succeeded', 10)]
        with patch.object(apple, 'dispatch', return_value='dispatched') as dispatch:
            self.assertEqual(apple.processing_run(inventory, 'TV_OS', 'v3.0.52', set()),
                             ('dispatched', None))
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.kwargs['apple_source_run'], 10)

    def test_wait_during_previous_review_without_signing(self):
        current = {'attributes': {'versionString': '3.0.51', 'appVersionState': 'IN_REVIEW'}}
        with patch.object(apple, 'platform_versions', return_value=[current]), \
             patch.object(apple, 'processing_run') as process:
            receipt = apple.reconcile_platform(Mock(), 'IOS', self.release, set(), allow_bootstrap=True)
        process.assert_not_called()
        self.assertEqual(receipt['state'], 'waiting_for_previous_store_version')

    def test_run_review_after_exact_processing(self):
        current = {'attributes': {'versionString': '3.0.51', 'appVersionState': 'READY_FOR_DISTRIBUTION'}}
        with patch.object(apple, 'platform_versions', return_value=[current]), \
             patch.object(apple, 'processing_run', return_value=('succeeded', 99)), \
             patch.object(apple, 'ensure_stage', return_value=('dispatched', None)) as stage, \
             patch.dict(os.environ, {'GITHUB_SHA': 'a' * 40}):
            receipt = apple.reconcile_platform(Mock(), 'IOS', self.release, set(), allow_bootstrap=False)
        self.assertEqual(receipt['processing_run'], 99)
        self.assertEqual(stage.call_args.kwargs['automatic_update'], 'true')
        self.assertEqual(stage.call_args.kwargs['submit_for_review'], 'true')

    def test_mac_queue_only_launches_the_automatic_approved_signing_run(self):
        queued = {'status': 'queued', 'display_title': 'macos-app-store:signed-upload:v3.0.52'}
        manual = {**queued, 'id': 1, 'actor': {'login': 'owner'}}
        automatic = {**queued, 'id': 2, 'actor': {'login': 'github-actions[bot]'}}
        variables = {'variables': [{'name': 'APPLE_APP_STORE_AUTOMATIC_RELEASES', 'value': 'true'}]}
        with patch.object(runner, 'get', side_effect=[variables, {'workflow_runs': [automatic, manual]}]), \
             patch.object(runner, 'get_run', return_value={'path': runner.MAC_STORE_WORKFLOW}) as get_run, \
             patch.object(runner, 'get_jobs', return_value=[{'status': 'queued'}]), \
             patch.object(runner, 'is_apple_job', return_value=True):
            self.assertEqual(runner.next_job(), '2')
        get_run.assert_called_once_with('2')
