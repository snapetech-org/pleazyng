"""Launch the same iOS simulator application on a pre-Duo runtime."""
import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import time
from pathlib import Path


def sim(*args, timeout=120):
    return subprocess.check_output(['xcrun', 'simctl', *args], text=True, timeout=timeout).strip()


def older_iphone(inventory):
    for runtime in sorted(inventory['runtimes'], key=lambda r: tuple(int(n) for n in r['version'].split('.')),
                          reverse=True):
        if not runtime.get('isAvailable') or '.iOS-' not in runtime['identifier'] or not runtime['version'].startswith('26.'):
            continue
        models = [d for d in runtime.get('supportedDeviceTypes', []) if d.get('productFamily') == 'iPhone'
                  or d.get('identifier', '').startswith('com.apple.CoreSimulator.SimDeviceType.iPhone-')]
        if not models:
            models = [{'identifier': d['deviceTypeIdentifier']} for d in inventory.get('devices', {}).get(runtime['identifier'], [])
                      if d.get('isAvailable') and '.iPhone-' in d.get('deviceTypeIdentifier', '')]
        if models:
            return runtime['identifier'], models[0]['identifier']
    return None


def discard_hosted_intermediates(products):
    build = products.resolve().parent.parent
    runner_temp = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    if build.name != 'Build' or build.parent.name != 'DerivedData' or not build.is_relative_to(runner_temp):
        raise ValueError('The iOS compatibility smoke only discards hosted job build intermediates')
    intermediates = build / 'Intermediates.noindex'
    if intermediates.is_symlink():
        raise ValueError('Refusing to remove redirected Xcode build intermediates')
    if intermediates.is_dir():
        shutil.rmtree(intermediates)


def report_free_space(phase):
    available = shutil.disk_usage(Path.home()).free / 1024**3
    print(f'iOS compatibility smoke {phase}: {available:.1f} GiB available', flush=True)


def install_and_launch(app, bundle, runtime, model):
    for attempt in (1, 2):
        udid = sim('create', f'Pleazyng-compat-{os.getpid()}-{attempt}', model, runtime)
        try:
            print(f'Booting the job-owned iOS compatibility simulator (attempt {attempt})', flush=True)
            sim('boot', udid)
            sim('bootstatus', udid, '-b', timeout=900)
            report_free_space('before installing the tested app')
            try:
                subprocess.run(['xcrun', 'simctl', 'install', udid, str(app)],
                               capture_output=True, text=True, timeout=300, check=True)
            except subprocess.CalledProcessError as error:
                print(error.stderr, flush=True)
                report_free_space('after the simulator install failed')
                if (attempt == 1 and re.search(r'domain=IXErrorDomain, code=2\b', error.stderr)
                        and 'Failed to create IXPlaceholder' in error.stderr
                        and 'Failed to create promise' in error.stderr):
                    print('Retrying the failed installation promise on a fresh simulator.', flush=True)
                    continue
                raise
            print(f'Installed {bundle}; launching it on the compatibility simulator.', flush=True)
            launch = sim('launch', udid, bundle)
            pid = int(launch.rsplit(':', 1)[1].strip())
            # A launch request can succeed before a delayed engine or dyld crash.
            for _ in range(15):
                time.sleep(1)
                os.kill(pid, 0)
            print(f'{bundle} remained running on {model}, {runtime} ({pid})')
            return
        finally:
            sim('delete', udid)


def main(products, *, discard_intermediates=False, require_installed_runtime=False):
    apps = [path for path in products.glob('*.app') if (path / 'Frameworks/App.framework').is_dir()]
    if len(apps) != 1:
        raise ValueError(f'Expected one built Flutter simulator app, found {apps}')
    info = plistlib.loads((apps[0] / 'Info.plist').read_bytes())
    if discard_intermediates:
        discard_hosted_intermediates(products)
    report_free_space('before selecting the legacy runtime')
    target = older_iphone(json.loads(sim('list', '-j')))
    if target is None and require_installed_runtime:
        raise RuntimeError('The compatibility runner must provide an installed iOS 26 phone runtime')
    if target is None:
        subprocess.run(['xcodebuild', '-downloadPlatform', 'iOS', '-buildVersion', '26.5'], check=True, timeout=900)
        target = older_iphone(json.loads(sim('list', '-j')))
    if target is None:
        raise RuntimeError('Unable to install an iOS 26 phone runtime for compatibility testing')
    runtime, model = target
    install_and_launch(apps[0], info['CFBundleIdentifier'], runtime, model)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('products', type=Path)
    parser.add_argument('--discard-intermediates', action='store_true')
    parser.add_argument('--require-installed-runtime', action='store_true')
    options = parser.parse_args()
    main(options.products, discard_intermediates=options.discard_intermediates,
         require_installed_runtime=options.require_installed_runtime)
