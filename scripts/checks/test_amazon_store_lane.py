"""Execute the Fastlane DSL with a local upload stub; never contact a store."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import release_all_platforms as release
from stores import submit_amazon_release as delivery

RUBY = shutil.which('ruby')
HARNESS = '''
require 'json'
$lanes = {}
def default_platform(*); end
def platform(*); yield; end
def desc(*); end
def lane(name, &block); $lanes[name] = block; end
def upload_to_amazon_appstore(**options); puts JSON.generate(options); end
load ARGV.fetch(0)
$lanes.fetch(:publish_amazon_appstore).call
'''


@unittest.skipUnless(RUBY, 'Ruby is required for the Fastlane DSL test')
class AmazonStoreLaneTest(unittest.TestCase):
    def test_store_upload_is_after_signed_apk_emulator_qualification(self):
        workflow = (ROOT / '.github/workflows/publish-app-stores.yml').read_text()
        amazon_job = workflow.split('  publish-amazon-appstore:', 1)[1].split('  publish-apple-app-store:', 1)[0]
        emulator_gate = amazon_job.index('Install Android TV emulator image')
        qualification = amazon_job.index('Boot Fire TV emulator and run Amazon APK qualification')
        submission = amazon_job.index("Submit the qualified release")
        self.assertLess(qualification, submission)
        upload = amazon_job.index('Upload the APK without submitting it for review')
        self.assertLess(emulator_gate, qualification)
        self.assertLess(qualification, upload)
        self.assertIn('scripts/qa/test_firetv_emulator.py', amazon_job)
        self.assertIn('if: always()', amazon_job)

    def run_lane(self, app_id):
        environment = {
            'PATH': os.environ.get('PATH', ''),
            'AMAZON_APK_PATH': '/fixture/pleazyng-amazon-firetv.apk',
            'AMAZON_CLIENT_ID': 'fixture-client',
            'AMAZON_CLIENT_SECRET': 'fixture-not-a-real-secret',
        }
        if app_id is not None:
            environment['AMAZON_APPSTORE_APP_ID'] = app_id
        return subprocess.run([RUBY, '-e', HARNESS, str(ROOT / 'fastlane/Fastfile')],
                              capture_output=True, text=True, env=environment)

    def test_upload_targets_amazon_record_and_preserves_console_listing(self):
        app_id = 'amzn1.devportal.mobileapp.' + 'a' * 32
        result = self.run_lane(app_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        options = json.loads(result.stdout)
        self.assertEqual(options['package_name'], app_id)
        for key in ('skip_upload_metadata', 'skip_upload_changelogs', 'skip_upload_images',
                    'skip_upload_screenshots', 'changes_not_sent_for_review'):
            self.assertIs(options[key], True)
        self.assertEqual(options['overwrite_upload_mode'], 'reuse')

    def test_android_package_name_is_rejected_before_upload(self):
        result = self.run_lane('org.snapetech.pleazyng')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('Amazon console App ID', result.stderr)

    def test_missing_record_id_is_not_guessed(self):
        result = self.run_lane(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('AMAZON_APPSTORE_APP_ID', result.stderr)


class AutomaticAmazonDeliveryTest(unittest.TestCase):
    def test_github_only_release_wakes_automatic_delivery(self):
        settings = {"AMAZON_APPSTORE_BOOTSTRAP_COMPLETE": "true"}
        with patch.object(release, "configured_repo_variables", return_value=settings), \
             patch.object(release, "command") as command, \
             patch.object(release, "publish_configured_channels") as other_channels:
            result = release.post_release_actions(
                "v3.0.52", "3.0.52", release_sha="a" * 40,
                include_webos_release=True, publish_channels=False, notify_discord=False,
            )
        other_channels.assert_not_called()
        self.assertEqual(result["amazon_appstore_automation"], [
            {"tag": "v3.0.52", "workflow": "deliver-amazon-appstore.yml", "state": "dispatched"},
        ])
        self.assertIn("release_tag=v3.0.52", command.call_args_list[0].args[0])
        self.assertFalse(result["channel_failures"])

    def test_large_apk_preserves_targeting_before_committing(self):
        old = {"id": "previous", "versionCode": 3209}
        new = {"id": "new", "versionCode": 3210}
        targets = {"amazonDevices": [{"id": "device", "status": "SUPPORTED"}]}
        apk = Mock()
        apk.stat.return_value.st_size = 910_794_926
        store = Mock()
        store.call.side_effect = [
            (None, None), ({"id": "edit"}, "edit-created"), ([old], None),
            (targets, "old-target"), ({"fileId": "file"}, None), (new, None),
            (old, "old-apk"), (None, None), ({}, "new-target"), (targets, None),
            (targets, None), ([new], None), ({"listings": {}}, None), ({"id": "edit"}, "before-validation"),
            ({"id": "edit"}, "validated"), ({"id": "edit"}, None),
        ]
        receipt = {}
        self.assertEqual(delivery.submit(store, apk, 3210, "v3.0.51", receipt), "submitted_for_review")
        calls = store.call.call_args_list
        self.assertEqual(calls[4].args[1:], ("POST", "/edit/apks/large/upload"))
        self.assertEqual(calls[5].kwargs["payload"], {"fileId": "file"})
        self.assertEqual(calls[7].args[1:], ("DELETE", "/edit/apks/previous"))
        self.assertEqual(calls[9].kwargs, {"payload": targets, "etag": "new-target"})
        self.assertEqual(calls[-1].args[1:], ("POST", "/edit/commit"))
        self.assertEqual(calls[-1].kwargs["etag"], "validated")
        self.assertEqual(receipt["previous_version_code"], 3209)

    def test_open_operator_edit_is_never_modified(self):
        store = Mock()
        store.call.return_value = ({"id": "operator-edit"}, "operator")
        with self.assertRaisesRegex(RuntimeError, "has an open edit"):
            delivery.submit(store, Mock(), 3210, "v3.0.51", {})
        store.call.assert_called_once_with("read open edit", "GET")

    def test_version_already_current_or_newer_clears_only_the_new_draft(self):
        store = Mock()
        store.call.side_effect = [
            (None, None), ({"id": "ours"}, "created"),
            ([{"id": "live-apk", "versionCode": 3211}], None),
            ({"id": "ours"}, "latest"), (None, None),
        ]
        self.assertEqual(delivery.submit(store, Mock(), 3210, "v3.0.51", {}), "already_current_or_newer")
        store.call.assert_called_with("remove task draft", "DELETE", "/ours", etag="latest")

    def test_only_missing_required_notes_are_filled(self):
        target = {"language": "en_US", "title": "Pleazyng", "recentChanges": ""}
        store = Mock()
        store.call.side_effect = [
            ({"listings": {"en_US": target, "fr_FR": {"recentChanges": "Existing notes"}}}, None),
            (target.copy(), "listing"), (None, None),
        ]
        delivery.fill_required_release_notes(store, "/edit", "v3.0.51")
        store.call.assert_called_with(
            "set required release notes", "PUT", "/edit/listings/en_US", etag="listing",
            payload={**target, "recentChanges": "Pleazyng 3.0.51 for Fire TV. "
                     "Full release notes are available in the Pleazyng project on GitHub."},
        )

    def test_ambiguous_commit_preserves_the_edit_for_reconciliation(self):
        apk = Mock()
        apk.stat.return_value.st_size = 50_000
        old = {"id": "previous", "versionCode": 3209}
        new = {"id": "previous", "versionCode": 3210}
        store = Mock()
        store.call.side_effect = [
            (None, None), ({"id": "edit"}, "created"), ([old], None),
            (old, "old-apk"), (new, None), ([new], None), ({"listings": {}}, None),
            ({"id": "edit"}, "before-validation"), ({"id": "edit"}, "validated"),
            TimeoutError("commit response lost"),
        ]
        receipt = {}
        with self.assertRaises(TimeoutError):
            delivery.submit(store, apk, 3210, "v3.0.51", receipt)
        self.assertTrue(receipt["commit_attempted"])
        self.assertEqual(store.call.call_args.args[1:], ("POST", "/edit/commit"))


if __name__ == '__main__':
    unittest.main()
