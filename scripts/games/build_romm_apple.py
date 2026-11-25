#!/usr/bin/env python3
"""Build the host and pinned software cores consumed by the local Apple pods."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'native/games/romm'
FRAMEWORKS = ('PlezyRetro', 'gambatte_libretro', 'mgba_libretro', 'fceumm_libretro')
SPECS = {
    'ios': [('iphoneos', 'arm64'), ('iphonesimulator', 'arm64;x86_64')],
    'tvos': [('appletvos', 'arm64'), ('appletvsimulator', 'arm64;x86_64')],
    'macos': [('macosx', 'arm64;x86_64')],
}


def framework_names(platform):
    return FRAMEWORKS + (('PlezyRetroWorker',) if platform == 'ios' else ())


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def tree_digest(directory):
    """Include framework symlink destinations and executable permissions."""
    checksum = hashlib.sha256()
    for path in sorted(directory.rglob('*')):
        checksum.update(path.relative_to(directory).as_posix().encode() + b'\0')
        checksum.update(str(path.lstat().st_mode & 0o777).encode() + b'\0')
        if path.is_symlink():
            checksum.update(b'link\0' + os.readlink(path).encode())
        elif path.is_file():
            checksum.update(b'file\0')
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    checksum.update(chunk)
        else:
            checksum.update(b'dir\0')
    return checksum.hexdigest()


def cache_valid(destination, fingerprint, platform=None):
    try:
        receipt = json.loads((destination / 'build-receipt.json').read_text())
        if receipt['sourceSha256'] != fingerprint:
            return False
        expected = {name + '.xcframework' for name in framework_names(platform)} | {'notices'}
        if platform == 'macos':
            expected.add('helpers')
        if receipt['artifacts'].keys() != expected:
            return False
        return all((destination / name).is_dir()
                   and tree_digest(destination / name) == checksum
                   for name, checksum in receipt['artifacts'].items())
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def source_fingerprint(platform):
    inputs = [Path(__file__), ROOT / 'scripts/games/package_romm_cores.py',
              PACKAGE / 'cores/CMakeLists.txt']
    inputs.extend(path for path in sorted(PACKAGE.glob('*')) if path.is_file())
    inputs.extend(sorted((ROOT / 'shared/process').glob('*.h')))
    inputs.extend(ROOT / 'scripts/games' / name for name in
                  ('install_macos_worker.sh', 'install_macos_romm_cores.py'))
    inputs.extend(sorted((PACKAGE / 'apple').glob('*.podspec')))
    inputs.extend(path for path in sorted((PACKAGE / 'apple/extension').rglob('*')) if path.is_file())
    checksum = hashlib.sha256(platform.encode())
    for path in inputs:
        checksum.update(path.relative_to(ROOT).as_posix().encode() + b'\0')
        checksum.update(path.read_bytes())
    for command in (['xcodebuild', '-version'], ['cmake', '--version']):
        checksum.update(subprocess.check_output(command))
    return checksum.hexdigest()


def find_framework(directory, name, sdk):
    # Xcode also creates .framework directories containing only .tbd stubs in
    # EagerLinkingTBDs. Package the actual executable, including versioned macOS
    # framework symlinks, rather than treating those temporary stubs as slices.
    candidates = [path for path in directory.rglob(name + '.framework')
                  if path.parent.name.startswith('Release') and (path / name).is_file()]
    if len(candidates) != 1:
        raise RuntimeError(f'Expected one built {sdk} slice for {name}, found {len(candidates)}')
    return candidates[0]


def _notice_inventory(directory):
    """Hash shared notice files, excluding the SDK-specific library receipt."""
    inventory = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise RuntimeError(f'Unexpected symlink in Apple notices: {path}')
        if not path.is_file() or path.relative_to(directory).as_posix() == 'romm-cores.json':
            continue
        relative = path.relative_to(directory).as_posix()
        inventory[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return inventory


def _notice_relative_file(root, value):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or '..' in path.parts or '\\' in value
            or path.as_posix() != value):
        raise RuntimeError(f'Invalid Apple notice path: {value!r}')
    file = root.joinpath(*path.parts)
    if not file.is_file():
        raise RuntimeError(f'Missing Apple notice referenced by manifest: {file}')
    return file


def stage_ios_notices(sources, destination):
    """Keep both SDK receipts while installing shared source and licenses once."""
    expected_sdks = {sdk for sdk, _ in SPECS['ios']}
    if set(sources) != expected_sdks:
        raise RuntimeError(f'Expected iOS notices for {sorted(expected_sdks)}')

    manifests = {}
    common_inventory = None
    reference_rows = None
    for sdk in sorted(expected_sdks):
        source = sources[sdk]
        manifest_path = source / 'romm-cores.json'
        if not manifest_path.is_file():
            raise RuntimeError(f'Missing corresponding source and core receipt for {sdk}')
        manifest = json.loads(manifest_path.read_text())
        rows = manifest.get('cores')
        if (manifest.get('schemaVersion') != 1 or manifest.get('platform') != 'iOS'
                or not isinstance(rows, list) or not rows):
            raise RuntimeError(f'Invalid iOS source receipt for {sdk}')
        row_contract = []
        for row in rows:
            archive_path = _notice_relative_file(source, row['sourceArchive'])
            if hashlib.sha256(archive_path.read_bytes()).hexdigest() != row.get('sourceSha256'):
                raise RuntimeError(f'Apple corresponding source digest differs for {sdk}')
            licenses = row.get('licenses')
            if not isinstance(licenses, list) or not licenses:
                raise RuntimeError(f'Apple license notices are missing for {row.get("core")}')
            for license_path in licenses:
                _notice_relative_file(source, license_path)
            row_contract.append((row.get('core'), row.get('commit'), row['sourceArchive'],
                                 row['sourceSha256'], tuple(licenses)))
        if len({row[0] for row in row_contract}) != len(row_contract):
            raise RuntimeError(f'Duplicate core receipt entry for {sdk}')
        if reference_rows is None:
            reference_rows = row_contract
        elif row_contract != reference_rows:
            raise RuntimeError(f'iOS source and license receipts differ for {sdk}')
        inventory = _notice_inventory(source)
        if common_inventory is None:
            common_inventory = inventory
        elif inventory != common_inventory:
            raise RuntimeError(f'iOS shared notice resources differ for {sdk}')
        manifests[sdk] = manifest

    destination.mkdir(parents=True, exist_ok=True)
    shared = destination / 'shared'
    reference = sources['iphoneos']

    def skip_sdk_manifest(directory, names):
        if Path(directory) == reference:
            return {'romm-cores.json'} & set(names)
        return set()

    shutil.copytree(reference, shared, ignore=skip_sdk_manifest)
    for sdk, manifest in manifests.items():
        for row in manifest['cores']:
            row['sourceArchive'] = (PurePosixPath('..') / 'shared' / row['sourceArchive']).as_posix()
            row['licenses'] = [
                (PurePosixPath('..') / 'shared' / path).as_posix()
                for path in row['licenses']
            ]
        sdk_directory = destination / sdk
        sdk_directory.mkdir()
        (sdk_directory / 'romm-cores.json').write_text(json.dumps(manifest, indent=2) + '\n')


def build(platform):
    specs = SPECS[platform]
    destination = PACKAGE / 'apple/Frameworks' / platform
    fingerprint = source_fingerprint(platform)
    if cache_valid(destination, fingerprint, platform):
        return
    work = ROOT / 'build/romm' / platform
    frameworks = {name: [] for name in framework_names(platform)}
    notices = {}
    for sdk, arch in specs:
        out = work / sdk
        command = ['cmake', '-S', PACKAGE, '-B', out, '-G', 'Xcode',
                   '-DPLEZY_ROMM_BUILD_CORES=ON',
                   '-DCMAKE_OSX_SYSROOT=' + sdk, '-DCMAKE_OSX_ARCHITECTURES=' + arch,
                   '-DCMAKE_OSX_DEPLOYMENT_TARGET=' + {'macos': '13.0', 'ios': '15.5', 'tvos': '15.0'}[platform],
                   '-DCMAKE_XCODE_ATTRIBUTE_CODE_SIGNING_ALLOWED=NO',
                   '-DCMAKE_XCODE_ATTRIBUTE_ONLY_ACTIVE_ARCH=NO',
                   '-DCMAKE_XCODE_ATTRIBUTE_DEBUG_INFORMATION_FORMAT=dwarf-with-dsym',
                   '-DFETCHCONTENT_BASE_DIR=' + str(out / 'dependencies')]
        if platform != 'macos':
            command += ['-DCMAKE_SYSTEM_NAME=' + ('iOS' if platform == 'ios' else 'tvOS')]
        if platform == 'ios':
            command += ['-DPLEZY_ROMM_APPLE_EXTENSION=ON']
        run(*command)
        run('cmake', '--build', out, '--config', 'Release', '--parallel', str(os.cpu_count() or 2))
        for name in framework_names(platform):
            frameworks[name].append(find_framework(out, name, sdk))
        notices[sdk] = out / 'cores/notices'
        if not (notices[sdk] / 'romm-cores.json').is_file():
            raise RuntimeError(f'Missing corresponding source and core receipt for {sdk}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Install the entire framework/source set together. A failed slice or
    # xcodebuild leaves the previous complete set available for diagnosis.
    with tempfile.TemporaryDirectory(prefix='plezy-retro-', dir=destination.parent) as temporary:
        prepared = Path(temporary) / platform
        prepared.mkdir()
        for name, slices in frameworks.items():
            args = ['xcodebuild', '-create-xcframework']
            for framework in slices:
                args += ['-framework', framework]
            run(*args, '-output', prepared / (name + '.xcframework'))
        if platform == 'macos':
            helpers = [path for path in work.rglob('plezy_retro_worker')
                       if path.is_file() and path.parent.name == 'Release']
            if len(helpers) != 1:
                raise RuntimeError('Expected one universal macOS emulator worker')
            (prepared / 'helpers').mkdir()
            shutil.copy2(helpers[0], prepared / 'helpers/plezy_retro_worker')
        if platform == 'ios':
            stage_ios_notices(notices, prepared / 'notices')
        else:
            for sdk, source in notices.items():
                shutil.copytree(source, prepared / 'notices' / sdk)
        receipt = {'sourceSha256': fingerprint, 'qualified': False,
                   'artifacts': {path.name: tree_digest(path) for path in prepared.iterdir()}}
        (prepared / 'build-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        backup = Path(temporary) / 'previous'
        if destination.exists():
            destination.rename(backup)
        try:
            prepared.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('platform', choices=SPECS)
    build(parser.parse_args().platform)
