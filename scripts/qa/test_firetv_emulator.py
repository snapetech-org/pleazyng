#!/usr/bin/env python3
"""Install and smoke-test the signed Amazon APK in an Android TV emulator."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import time
import zipfile

PACKAGE = 'org.snapetech.pleazyng'
TV_KEYS = ('19', '20', '21', '22')  # DPAD up/down/left/right


def run(command: list[str], *, timeout: int = 60, check: bool = True) -> str:
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def adb(serial: str, *args: str, timeout: int = 60, check: bool = True) -> str:
    return run(['adb', '-s', serial, *args], timeout=timeout, check=check)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apk', required=True, type=Path)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--boot-timeout', type=int, default=300)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = args.output_dir / 'qualification.json'
    log_path = args.output_dir / 'emulator-logcat.txt'
    screenshot_path = args.output_dir / 'firetv-home.png'
    result: dict[str, object] = {
        'schema_version': 1,
        'test': 'signed Amazon Fire TV APK install, launch, and remote-input smoke test',
        'apk': args.apk.name,
        'apk_sha256': sha256(args.apk),
        'package': PACKAGE,
        'status': 'failed',
        'checks': {},
    }
    try:
        with zipfile.ZipFile(args.apk) as archive:
            names = set(archive.namelist())
        if not any(name.startswith('lib/x86_64/') and name.endswith('/libapp.so') for name in names):
            raise RuntimeError('The signed Amazon APK has no x86_64 Flutter AOT runtime for the Fire TV emulator.')

        adb(args.serial, 'wait-for-device', timeout=args.boot_timeout)
        deadline = time.monotonic() + args.boot_timeout
        while time.monotonic() < deadline:
            if adb(args.serial, 'shell', 'getprop', 'sys.boot_completed', check=False) == '1':
                break
            time.sleep(2)
        else:
            raise RuntimeError('Fire TV emulator did not finish booting.')

        abi = adb(args.serial, 'shell', 'getprop', 'ro.product.cpu.abi')
        if abi != 'x86_64':
            raise RuntimeError(f'Expected the x86_64 Android TV emulator, got {abi!r}.')
        fingerprint = adb(args.serial, 'shell', 'getprop', 'ro.build.fingerprint')
        sdk = adb(args.serial, 'shell', 'getprop', 'ro.build.version.sdk')
        adb(args.serial, 'logcat', '-c')
        install = adb(args.serial, 'install', '-r', '--no-streaming', str(args.apk), timeout=180)
        if 'Success' not in install:
            raise RuntimeError(f'APK installation did not report success: {install}')
        result['checks']['install'] = 'passed'

        leanback_intent = ('-a', 'android.intent.action.MAIN',
                           '-c', 'android.intent.category.LEANBACK_LAUNCHER')
        # TV launchers resolve launcher filters without MATCH_DEFAULT_ONLY.
        resolved = adb(args.serial, 'shell', 'cmd', 'package', 'resolve-activity',
                       '--brief', '--query-flags', '0', *leanback_intent, '-p', PACKAGE).splitlines()
        component = resolved[-1] if resolved else ''
        if not re.fullmatch(re.escape(PACKAGE) + r'/[A-Za-z0-9_.$]+', component):
            raise RuntimeError('The installed APK has no enabled Leanback launcher activity.')
        launch = adb(args.serial, 'shell', 'am', 'start', '-W', *leanback_intent,
                     '-n', component, timeout=90)
        if 'Status: ok' not in launch:
            raise RuntimeError(f'Leanback launch intent failed: {launch}')
        result['checks']['leanback_launch'] = 'passed'

        deadline = time.monotonic() + 90
        pid = ''
        while time.monotonic() < deadline:
            pid = adb(args.serial, 'shell', 'pidof', PACKAGE, check=False)
            activities = adb(args.serial, 'shell', 'dumpsys', 'activity', 'activities', check=False)
            if pid and any(PACKAGE in line and ('mResumedActivity' in line or 'topResumedActivity' in line)
                           for line in activities.splitlines()):
                break
            time.sleep(2)
        else:
            raise RuntimeError('Pleazyng did not stay alive as the resumed activity after launch.')
        result['checks']['foreground_process'] = 'passed'

        for key in TV_KEYS:
            adb(args.serial, 'shell', 'input', 'keyevent', key)
            time.sleep(0.25)
        activities = adb(args.serial, 'shell', 'dumpsys', 'activity', 'activities', check=False)
        if (not adb(args.serial, 'shell', 'pidof', PACKAGE, check=False)
                or not any(PACKAGE in line and ('mResumedActivity' in line or 'topResumedActivity' in line)
                           for line in activities.splitlines())):
            raise RuntimeError('Pleazyng exited while receiving Fire TV remote navigation input.')
        result['checks']['remote_dpad_navigation'] = 'passed'
        with screenshot_path.open('wb') as stream:
            subprocess.run(['adb', '-s', args.serial, 'exec-out', 'screencap', '-p'],
                           check=True, stdout=stream, timeout=30)
        if screenshot_path.stat().st_size < 1000:
            raise RuntimeError('Emulator screenshot is empty or truncated.')
        with screenshot_path.open('rb') as stream:
            header = stream.read(24)
        if header[:8] != b'\x89PNG\r\n\x1a\n' or struct.unpack('>II', header[16:24]) != (1920, 1080):
            raise RuntimeError('Fire TV emulator screenshot is not a valid 1920x1080 PNG.')
        result['checks']['screenshot'] = screenshot_path.name
        log = adb(args.serial, 'logcat', '-d', '--pid', pid.split()[0], '-t', '3000', check=False)
        log_path.write_text(log + '\n')
        if re.search(r'FATAL EXCEPTION|Fatal signal \d+|Process: ' + re.escape(PACKAGE), log):
            raise RuntimeError('Emulator logcat contains a fatal app crash; see emulator-logcat.txt.')
        result.update({'status': 'passed', 'emulator': {
            'abi': abi, 'sdk': sdk, 'fingerprint': fingerprint, 'serial': args.serial,
        }})
        return 0
    finally:
        if not log_path.exists():
            try:
                log_path.write_text(adb(args.serial, 'logcat', '-d', '-t', '3000', check=False) + '\n')
            except (OSError, subprocess.SubprocessError, RuntimeError):
                log_path.write_text('Emulator logcat unavailable.\n')
        receipt_path.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    raise SystemExit(main())
