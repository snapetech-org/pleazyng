#!/usr/bin/env python3
"""Download the pinned matching WebKit helper packages for release repackaging."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.error
import urllib.request


def verify_hash(path, architecture, expected):
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise ValueError(f'WebKit checksum mismatch: {architecture}')


def fetch_helper(destination, architecture, source):
    urls = [source['url']]
    if snapshot := source.get('snapshot_url'):
        urls.append(snapshot)
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        partial = Path(scratch) / destination.name
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=60) as response, partial.open('wb') as stream:
                    shutil.copyfileobj(response, stream, 1024 * 1024)
            except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.IncompleteRead) as error:
                print(f'WebKit download failed for {architecture} at {url}: {error}', file=sys.stderr)
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                continue
            verify_hash(partial, architecture, source['sha256'])
            partial.replace(destination)
            return
    raise OSError(f'WebKit helper for {architecture} is unavailable from its configured Ubuntu archives')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    sources = json.loads(Path(__file__).with_name('webkit-runtime.json').read_text())
    for arch, source in sources.items():
        destination = args.output / f'webkit-{arch}.deb'
        if destination.exists():
            verify_hash(destination, arch, source['sha256'])
        else:
            fetch_helper(destination, arch, source)
        print(f'Verified WebKit helper package: {arch}')


if __name__ == '__main__':
    main()
