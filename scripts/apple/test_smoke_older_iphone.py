import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import smoke_older_iphone as smoke


class OlderIPhoneSmokeTests(unittest.TestCase):
    def test_retries_only_the_observed_install_promise_error_on_a_fresh_device(self):
        error = subprocess.CalledProcessError(
            2, ['simctl', 'install'],
            stderr='domain=IXErrorDomain, code=2\nFailed to create IXPlaceholder.\nFailed to create promise.',
        )
        with patch.object(smoke, 'sim', side_effect=['first', '', '', '', 'second', '', '', 'app: 42', '']) as sim, \
             patch.object(smoke.subprocess, 'run', side_effect=[error, None]) as install, \
             patch.object(smoke, 'report_free_space'), patch.object(smoke.time, 'sleep'), \
             patch.object(smoke.os, 'kill') as alive:
            smoke.install_and_launch(Path('/products/Runner.app'), 'app', 'iOS-26-5', 'iPhone')
        self.assertEqual([call.args[0][3] for call in install.call_args_list], ['first', 'second'])
        self.assertEqual([call.args[1] for call in sim.call_args_list if call.args[0] == 'delete'],
                         ['first', 'second'])
        self.assertEqual(alive.call_count, 15)

    def test_other_install_failures_are_not_retried(self):
        error = subprocess.CalledProcessError(2, ['simctl', 'install'], stderr='Missing executable')
        with patch.object(smoke, 'sim', side_effect=['first', '', '', '']) as sim, \
             patch.object(smoke.subprocess, 'run', side_effect=error) as install, \
             patch.object(smoke, 'report_free_space'), self.assertRaises(subprocess.CalledProcessError):
            smoke.install_and_launch(Path('/products/Runner.app'), 'app', 'iOS-26-5', 'iPhone')
        install.assert_called_once()
        self.assertEqual(sim.call_args.args, ('delete', 'first'))

    def test_disk_cleanup_preserves_the_compatibility_test_app(self):
        with tempfile.TemporaryDirectory() as temp:
            build = Path(temp) / 'job' / 'DerivedData' / 'Build'
            intermediate = build / 'Intermediates.noindex'
            intermediate.mkdir(parents=True)
            products = build / 'Products' / 'Debug-iphonesimulator'
            app = products / 'Runner.app'
            app.mkdir(parents=True)
            with patch.dict(os.environ, {'RUNNER_TEMP': temp}):
                smoke.discard_hosted_intermediates(products)
            self.assertTrue(app.is_dir())
            self.assertFalse(intermediate.exists())


if __name__ == '__main__':
    unittest.main()
