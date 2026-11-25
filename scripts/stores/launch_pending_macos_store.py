#!/usr/bin/env python3
"""Provision the approved private signing runner for queued automatic Mac App Store work."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from launch_queued_apple_jit_jobs import MAC_STORE_WORKFLOW, REPOSITORY, get_jobs, get_run, is_apple_job


SIGNED_UPLOAD = re.compile(r'macos-app-store:signed-upload:(v[0-9]+\.[0-9]+\.[0-9]+)')
LAUNCHER = Path(__file__).resolve().parents[1] / 'launch_queued_apple_jit_jobs.py'


def get(*args):
    return json.loads(subprocess.check_output(['gh', 'api', *args], text=True, timeout=45))


def next_job():
    variables = get(f'repos/{REPOSITORY}/actions/variables?per_page=100')['variables']
    enabled = any(item['name'] == 'APPLE_APP_STORE_AUTOMATIC_RELEASES' and item['value'] == 'true'
                  for item in variables)
    if not enabled:
        return None
    runs = get(f'repos/{REPOSITORY}/actions/workflows/macos-app-store-signed.yml/runs'
               '?branch=main&event=workflow_dispatch&per_page=50')['workflow_runs']
    for record in reversed(runs):
        match = SIGNED_UPLOAD.fullmatch(record['display_title'])
        if (not match or record['status'] == 'completed'
                or record.get('actor', {}).get('login') != 'github-actions[bot]'):
            continue
        run_id = str(record['id'])
        run = get_run(run_id)
        if run['path'] != MAC_STORE_WORKFLOW:
            continue
        if any(job['status'] == 'queued' and is_apple_job(job, run_id, run) for job in get_jobs(run_id)):
            return run_id
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', required=True, type=Path)
    args = parser.parse_args()
    run = next_job()
    if not run:
        return 0
    target = os.environ.get('APPLE_JIT_SSH_TARGET', 'keith@100.64.0.3')
    if subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', target, 'true'],
                      check=False, timeout=15).returncode:
        print(f'Mac App Store signing host unavailable. Run {run} remains queued.')
        return 0
    return subprocess.run([sys.executable, str(LAUNCHER), run, '--evidence-dir', str(args.evidence_dir)],
                          check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
