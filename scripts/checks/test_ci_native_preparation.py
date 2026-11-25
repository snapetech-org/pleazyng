#!/usr/bin/env python3
"""Guard the prerequisite order that caused exact-head native CI failures."""
from pathlib import Path
import os
import re
import shlex
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/ci.yml'
APPLE_SUITE = ROOT / '.github/actions/apple-native-reliability/action.yml'


def job(text: str, name: str) -> str:
    match = re.search(rf'^  {re.escape(name)}:\n(.*?)(?=^  [\w-]+:|\Z)', text, re.M | re.S)
    if match is None:
        raise AssertionError(f'Missing CI job: {name}')
    return match.group(1)


def step(text: str, name: str) -> str:
    match = re.search(rf'^(?P<indent> +)- name: {re.escape(name)}\n.*?(?=^(?P=indent)- |\Z)', text, re.M | re.S)
    if match is None:
        raise AssertionError(f'Missing preparation step: {name}')
    return match.group(0)


class NativePreparationOrderTests(unittest.TestCase):
    def test_independent_ci_jobs_can_start_without_waiting_for_other_suites(self):
        workflow = WORKFLOW.read_text()
        for name in ('android-test', 'linux-native-test', 'server'):
            with self.subTest(job=name):
                self.assertNotRegex(job(workflow, name), r'(?m)^    needs:')
                self.assertNotIn('${{ needs.', job(workflow, name))

    def test_desktop_native_builds_use_the_current_application_target(self):
        workflow = WORKFLOW.read_text()
        for platform in ('linux', 'windows'):
            with self.subTest(platform=platform):
                cmake = (ROOT / f'{platform}/CMakeLists.txt').read_text()
                binary = re.search(r'set\(BINARY_NAME "([^"]+)"\)', cmake)
                self.assertIsNotNone(binary)
                build = step(job(workflow, f'{platform}-native-test'),
                             f'Build {platform.title()} native reliability tests')
                self.assertRegex(build, rf'(?m)^\s+{re.escape(binary[1])} [\\`]\s*$')

    def test_desktop_runtime_is_ready_before_flutter_and_cmake(self):
        text = WORKFLOW.read_text()
        for job_name, platform, architectures in (
            ('linux-native-test', 'linux', ['x64']),
            ('linux-packages', 'linux', ['x64']),
            ('windows-native-test', 'windows', ['x64', 'arm64']),
        ):
            with self.subTest(job=job_name):
                body = job(text, job_name)
                prepare = step(body, f'Build bundled MilkRust for {platform.title()}')
                # Runtime staging cannot depend on cache misses or optional flags.
                self.assertNotRegex(prepare, r'(?m)^        if:', msg=prepare)
                self.assertNotIn('continue-on-error', prepare)
                commands = re.findall(r'flutter build (?:linux|windows)\b|cmake -S (?:linux|windows)\b', body)
                self.assertTrue(commands, 'The job must configure the actual app/native targets')
                for command in commands:
                    self.assertLess(body.index(prepare), body.index(command))
                command = prepare.split('        run: ', 1)[1].strip()
                for arch in architectures:
                    args = shlex.split(command.replace('${{ matrix.arch }}', arch))
                    self.assertEqual(args[1], 'scripts/visualizer/build_runtime.py')
                    self.assertEqual(args[args.index('--platform') + 1], platform)
                    self.assertEqual(args[args.index('--architectures') + 1], arch)
                    self.assertEqual(args[args.index('--output') + 1], 'build/visualizer-native')
                if platform == 'windows':
                    self.assertIn('shell: pwsh', prepare)
                    self.assertIn('--architectures "${{ matrix.arch }}"', prepare)

    def test_apple_refresh_follows_pub_get_and_precedes_every_flutter_config(self):
        body = APPLE_SUITE.read_text()
        dependencies = step(body, 'Install locked Dart dependencies')
        self.assertIn('flutter pub get --enforce-lockfile --no-example', dependencies)
        for platform, slug in (('iOS', 'ios'), ('macOS', 'macos')):
            with self.subTest(platform=platform):
                refresh = step(body, f'Refresh pinned {platform} Swift packages')
                configure = step(body, f'Prepare {platform} Flutter build settings')
                self.assertIn(f"inputs.platform == '{platform}'", refresh)
                self.assertIn(f'run: scripts/refresh_apple_spm.sh {slug}', refresh)
                self.assertNotIn('--reset', refresh, 'Refresh must preserve the existing package cache')
                self.assertLess(body.index(dependencies), body.index(refresh))
                self.assertLess(body.index(refresh), body.index(configure))
                for match in re.finditer(rf'flutter build {slug}\b', body):
                    self.assertLess(body.index(refresh), match.start())

    def test_apple_platform_suites_are_independent_and_aggregate_gate_is_required(self):
        jobs = yaml.safe_load(WORKFLOW.read_text())['jobs']
        apple = jobs['apple-native-suite']
        self.assertEqual(apple['strategy']['fail-fast'], False)
        self.assertCountEqual([row['platform'] for row in apple['strategy']['matrix']['include']],
                              ['macOS', 'tvOS'])
        self.assertEqual(apple['name'], 'Apple native reliability (${{ matrix.platform }})')
        suite = next(s for s in apple['steps'] if s.get('uses') == './.github/actions/apple-native-reliability')
        self.assertEqual(suite['with']['platform'], '${{ matrix.platform }}')
        self.assertEqual(suite['timeout-minutes'], 110)
        ios = jobs['apple-native-ios-suite']
        self.assertCountEqual([row['platform'] for row in ios['strategy']['matrix']['include']], ['iOS'])
        self.assertEqual(ios['name'], 'Apple native reliability (${{ matrix.platform }})')
        ios_suite = next(s for s in ios['steps'] if s.get('uses') == './.github/actions/apple-native-reliability')
        self.assertEqual(ios_suite['with']['platform'], '${{ matrix.platform }}')
        self.assertIn("runner.environment == 'github-hosted'", ios_suite['with']['defer-older-iphone-smoke'])
        self.assertIn('compatibility-artifact-id', ios['outputs']['ios-compatibility-artifact-id'])
        compatibility = jobs['apple-native-ios-compatibility']
        self.assertEqual(compatibility['needs'], 'apple-native-ios-suite')
        self.assertEqual(compatibility['runs-on'], 'macos-26')
        self.assertIn('needs.apple-native-ios-suite.outputs.ios-compatibility-artifact-id', compatibility['if'])
        gate = jobs['apple-native-test']
        self.assertEqual(gate['name'], 'Apple native reliability')
        self.assertCountEqual(gate['needs'], ['apple-native-suite', 'apple-native-ios-suite', 'apple-native-ios-compatibility'])
        self.assertIn('always()', gate['if'])
        self.assertEqual(gate['steps'][0]['env']['SUITE_RESULT'], '${{ needs.apple-native-suite.result }}')
        self.assertEqual(gate['steps'][0]['env']['IOS_SUITE_RESULT'], '${{ needs.apple-native-ios-suite.result }}')
        self.assertIn('test "$IOS_COMPATIBILITY_RESULT" = success', gate['steps'][0]['run'])
        self.assertIn('test "$IOS_COMPATIBILITY_RESULT" = skipped', gate['steps'][0]['run'])
        self.assertIn('gha-{0}-apple-native-test-{1}', apple['runs-on'])

    def test_motion_dispatch_uses_only_the_hosted_iphone_suite(self):
        jobs = yaml.safe_load(WORKFLOW.read_text())['jobs']
        focused = jobs['ios-motion']
        self.assertIn("github.event_name == 'workflow_dispatch'", focused['if'])
        self.assertEqual(focused['runs-on'], 'xcode-27')
        self.assertEqual(focused['strategy']['matrix']['include'], [{'platform': 'iOS', 'slug': 'ios'}])
        self.assertEqual(focused['steps'], jobs['apple-native-suite']['steps'])
        for name, config in jobs.items():
            if name != 'ios-motion':
                self.assertIn("inputs.action != 'ios-motion'", config['if'], name)

    def test_motion_selector_names_an_existing_iphone_test(self):
        selector = re.search(r'-only-testing:RunnerTests/(\w+)/(\w+)', APPLE_SUITE.read_text())
        self.assertIsNotNone(selector)
        suite, method = selector.groups()
        source = '\n'.join(
            path.read_text()
            for path in (ROOT / 'ios/RunnerTests').glob('*.swift')
        )
        declaration = re.search(rf'^final class {suite}: XCTestCase \{{.*?^\}}', source, re.M | re.S)
        self.assertIsNotNone(declaration, suite)
        self.assertIn(f'func {method}()', declaration.group())

    def test_apple_native_job_installs_required_runtime_generators(self):
        body = job(WORKFLOW.read_text(), 'apple-native-suite')
        tools = step(body, 'Ensure CMake for the embedded Steam receiver')
        self.assertIn('command -v xcodegen >/dev/null || brew install xcodegen', tools)
        self.assertIn('command -v git-lfs >/dev/null || brew install git-lfs', tools)

    def test_private_apple_suites_verify_their_selected_sdk_before_allocating_scratch(self):
        prepare = yaml.safe_load(APPLE_SUITE.read_text())['runs']['steps'][0]['run']
        probes = """
            xcodebuild() { [[ "$*" == '-version' ]] && echo "Xcode $XCODE_VERSION"; }
            xcrun() { [[ "$*" == "--sdk $EXPECTED_SDK --show-sdk-version" ]] && echo "$SDK_VERSION"; }
            bash() { [[ "$1" == scripts/prepare_apple_jit_scratch.sh ]]; }
        """
        for platform, sdk, slug in [('macOS', 'macosx', 'macos'),
                                    ('tvOS', 'appletvsimulator', 'tvos')]:
            for runner, xcode, actual_sdk, ok in [
                ('self-hosted', '27.0', '27.0', platform != 'iOS'),
                ('self-hosted', '27.1', '27.1', platform == 'iOS'),
                ('self-hosted', '27.2', '27.0', False),
                ('self-hosted', '27.10', '27.2', platform == 'iOS'),
                ('self-hosted', 'unknown', '27.2', False),
                ('self-hosted', '26.5', '27.0', False),
                ('self-hosted', '27.0', '26.5', False),
                ('github-hosted', '26.5', '26.5', True),
            ]:
                with self.subTest(platform=platform, runner=runner, xcode=xcode, sdk=actual_sdk):
                    env = dict(RUNNER_ENVIRONMENT=runner, PLATFORM=platform, XCODE_VERSION=xcode,
                               EXPECTED_SDK=sdk, SDK_VERSION=actual_sdk, GITHUB_OUTPUT='/dev/null',
                               RUNNER_TEMP='/fixture', PUB_CACHE=f'/fixture/apple-native-pub-{slug}')
                    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', probes + prepare],
                                            env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, ok, result.stderr)

    def test_iphone_setup_checks_both_sdks_before_native_qualification(self):
        config = yaml.safe_load((ROOT / '.github/actions/setup-flutter-apple/action.yml').read_text())
        step = next(s for s in config['runs']['steps'] if s['name'] == 'Verify iPhone Duo SDK for device and simulator')
        self.assertEqual(step['if'], "inputs.require-duo-sdk == 'true'")
        probes = """
            xcrun() {
                case "$2" in
                    iphoneos) echo "$DEVICE_SDK" ;;
                    iphonesimulator) echo "$SIM_SDK" ;;
                    *) return 1 ;;
                esac
            }
        """
        for device, simulator, ok in [('27.1', '27.1', True), ('27.0', '27.1', False), ('27.1', '27.0', False)]:
            with self.subTest(device=device, simulator=simulator):
                env = dict(os.environ, RUNNER_ENVIRONMENT='github-hosted', DEVICE_SDK=device, SIM_SDK=simulator)
                result = subprocess.run(['bash', '-c', probes + step['run']], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, ok, result.stderr)

    def test_private_checkout_and_cleanup_preserve_scratch_ownership(self):
        steps = {s['name']: s for s in yaml.safe_load(APPLE_SUITE.read_text())['runs']['steps']}
        checkout = steps['Check out the exact source for this suite']['run']
        self.assertIn('if [[ "$RUNNER_ENVIRONMENT" == self-hosted ]]; then', checkout)
        self.assertIn('scripts/copy_apple_jit_sources.py "$GITHUB_WORKSPACE" "$SUITE_ROOT" "$GITHUB_SHA"', checkout)
        self.assertIn('ln -s "$APPLE_LOCAL_BUILD_DIR" "$SUITE_ROOT/build"', checkout)
        cleanup = steps['Clean Apple build scratch']['run']
        self.assertRegex(cleanup, r'if \[\[ "\$APPLE_HOSTED_SCRATCH" == 1 && [^\n]+\n\s+git worktree remove')
        self.assertIn('bash scripts/cleanup_apple_jit_scratch.sh "$APPLE_SCRATCH_JOB"', cleanup)

    def test_apple_simulator_evidence_is_retained_before_scratch_cleanup(self):
        action_steps = yaml.safe_load(APPLE_SUITE.read_text())['runs']['steps']
        steps = {s['name']: s for s in action_steps}
        cleanup = steps['Clean Apple build scratch']
        for name, condition, artifact, output in (
            ('Retain failed Apple native test results', 'failure()', 'apple-native-results',
             'steps.scratch.outputs.APPLE_RESULT_BUNDLE'),
            ('Retain Apple simulator ownership receipt', 'always()', 'apple-simulator',
             'steps.destination.outputs.APPLE_SIMULATOR_RECEIPT'),
        ):
            with self.subTest(name=name):
                upload = steps[name]
                self.assertIn(condition, upload['if'])
                self.assertTrue(upload['uses'].startswith('actions/upload-artifact@'))
                self.assertEqual(upload['with']['name'],
                                 artifact + '-${{ inputs.platform }}-${{ github.run_id }}-${{ github.run_attempt }}')
                self.assertIn('${{ ' + output + ' }}', upload['with']['path'].splitlines())
                self.assertLess(action_steps.index(upload), action_steps.index(cleanup))

    def test_flutter_shards_cover_each_test_once_with_deterministic_assignment(self):
        from ci.flutter_test_shard import partition
        files = ['test/slow_test.dart', 'test/other_test.dart', 'test/new_test.dart', 'test/fast_test.dart']
        groups = partition(files, {files[0]: 100}, 3)
        self.assertEqual([files[0]], groups[0])
        self.assertEqual(sorted(files), sorted(p for group in groups for p in group))
        self.assertEqual(groups, partition(list(reversed(files)), {files[0]: 100}, 3))

    def test_unit_job_is_the_only_full_application_suite(self):
        workflow = WORKFLOW.read_text()
        analysis = job(workflow, 'analyze')
        unit = job(workflow, 'test-extras') + job(workflow, 'reader-browser')
        shards = job(workflow, 'test-shards')
        self.assertNotRegex(analysis, r'flutter test\b|scripts/run_tests\.sh\b')
        self.assertIn('dart run scripts/checks/check_analyzer.dart', step(analysis, 'Analyze code'))
        self.assertEqual(workflow.count('scripts/ci/flutter_test_shard.py'), 1)
        self.assertIn('matrix.shard', step(shards, 'Run Flutter test shard'))
        self.assertIn('needs: [test-shards, test-extras, reader-browser]', job(workflow, 'test'))
        # These independent suites are intentional coverage, not duplicates.
        for name in (
            'Run real reader browser checks',
            'Run wakelock_plus VM tests',
            'Run wakelock_plus Chrome tests',
            'Run the vendored desktop preference store tests',
        ):
            self.assertTrue(step(unit, name))

    def test_required_runtime_receipts_are_not_bypassed(self):
        workflow = WORKFLOW.read_text()
        for name in ('linux-native-test', 'linux-packages', 'windows-native-test'):
            self.assertNotIn('PLEZY_VISUALIZER_REQUIRE_MILKRUST=OFF', job(workflow, name))
        cmake = (ROOT / 'native/visualizer/milkrust.cmake').read_text()
        self.assertIn('option(PLEZY_VISUALIZER_REQUIRE_MILKRUST "Require the bundled native visualization runtime" ON)', cmake)
        self.assertIn('Staged MilkRust runtime differs from the app source pin', cmake)
        self.assertIn('MilkRust runtime is missing:', cmake)


if __name__ == '__main__':
    unittest.main()
