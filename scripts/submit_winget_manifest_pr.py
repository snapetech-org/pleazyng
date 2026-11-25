#!/usr/bin/env python3
"""Submit verified Pleazyng manifests from a dedicated branch based on WinGet master."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode


UPSTREAM = 'microsoft/winget-pkgs'
FORK = 'snapetech-org/winget-pkgs'
PACKAGE = 'SnapeTech.Pleazyng'
MANIFEST_NAMES = (f'{PACKAGE}.yaml', f'{PACKAGE}.installer.yaml', f'{PACKAGE}.locale.en-US.yaml')


def api(repo, endpoint, *, payload=None, missing=False):
    command = ['gh', 'api', f'repos/{repo}/{endpoint}']
    if payload is not None:
        command += ['--method', 'POST', '--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    if result.returncode:
        if missing and 'HTTP 404' in result.stderr:
            return None
        raise RuntimeError(f'WinGet GitHub request for {repo}/{endpoint} failed: {result.stderr.strip()}')
    return json.loads(result.stdout)


def git_blob_sha(data):
    return hashlib.sha1(f'blob {len(data)}\0'.encode() + data, usedforsecurity=False).hexdigest()


def verify_only_manifest_changes(files, expected):
    observed = {item['filename']: item['sha'] for item in files if item['status'] == 'added'}
    if observed != expected or len(files) != len(expected):
        raise RuntimeError('The existing WinGet submission has different manifest changes.')


def submit(manifest_dir, version):
    if re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', version) is None:
        raise ValueError('WinGet submission requires a stable semantic version.')
    prefix = f'manifests/s/SnapeTech/Pleazyng/{version}'
    manifests = {}
    for name in MANIFEST_NAMES:
        path = manifest_dir / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'The verified release bundle omitted {name}.')
        data = path.read_bytes()
        contents = data.decode('utf-8')
        if (not re.search(rf'^PackageIdentifier: {re.escape(PACKAGE)}\s*$', contents, re.M)
                or not re.search(rf'^PackageVersion: {re.escape(version)}\s*$', contents, re.M)):
            raise ValueError(f'Unexpected package version or identifier in {name}.')
        manifests[f'{prefix}/{name}'] = (contents, git_blob_sha(data))
    expected = {path: digest for path, (_, digest) in manifests.items()}

    branch = f'pleazyng-v{version}'
    head = f'{FORK.split("/", 1)[0]}:{branch}'
    prior = api(UPSTREAM, 'pulls?' + urlencode({'state': 'all', 'base': 'master', 'head': head}))
    for pull in prior:
        if pull['state'] == 'open' or pull.get('merged_at'):
            verify_only_manifest_changes(api(UPSTREAM, f'pulls/{pull["number"]}/files?per_page=100'), expected)
            print(pull['html_url'])
            return
        raise RuntimeError(f'The existing WinGet PR was closed without merging: {pull["html_url"]}')

    base = api(UPSTREAM, 'git/ref/heads/master')['object']['sha']
    published = api(UPSTREAM, f'contents/{prefix}', missing=True)
    if published is not None:
        if {item['path']: item['sha'] for item in published if item['type'] == 'file'} != expected:
            raise RuntimeError('The catalogue already contains different manifests for this version.')
        print(f'{PACKAGE} {version} already exists in the WinGet catalogue.')
        return

    title = f'{PACKAGE} {version}'
    existing = api(FORK, f'git/ref/heads/{branch}', missing=True)
    if existing is None:
        base_tree = api(UPSTREAM, f'git/commits/{base}')['tree']['sha']
        tree = api(FORK, 'git/trees', payload={'base_tree': base_tree, 'tree': [
            {'path': path, 'type': 'blob', 'mode': '100644', 'content': contents}
            for path, (contents, _) in manifests.items()
        ]})['sha']
        commit = api(FORK, 'git/commits', payload={'message': title, 'tree': tree, 'parents': [base]})['sha']
        api(FORK, 'git/refs', payload={'ref': f'refs/heads/{branch}', 'sha': commit})
    else:
        commit = api(FORK, f'commits/{existing["object"]["sha"]}')
        verify_only_manifest_changes(commit['files'], expected)
        if len(commit['parents']) != 1:
            raise RuntimeError('The existing WinGet topic branch does not have one upstream parent.')
        parent = commit['parents'][0]['sha']
        ancestry = api(UPSTREAM, f'compare/{parent}...{base}?per_page=1')
        if ancestry['status'] not in ('identical', 'ahead'):
            raise RuntimeError('The WinGet topic branch is outside the upstream catalogue history.')

    pull = api(UPSTREAM, 'pulls', payload={
        'title': title, 'head': head, 'base': 'master',
        'body': (f'Adds the verified WinGet manifests for Pleazyng {version}.\n\n'
                 f'[Published release](https://github.com/snapetech-org/pleazyng/releases/tag/v{version})'),
    })
    print(pull['html_url'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest_dir', type=Path)
    parser.add_argument('version')
    args = parser.parse_args()
    submit(args.manifest_dir, args.version)
