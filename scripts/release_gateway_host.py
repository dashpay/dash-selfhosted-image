#!/usr/bin/env python3
"""Credential-free host transport for the permanent release Gateway allocator."""
import argparse
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import urllib.error
import uuid

from image_contract import require, validate_manifest
import release_runner as release

CONFIG = Path('/etc/dash-ci-releases/gateway.json')
MAX_BYTES = 1024 * 1024


def read_config():
    require(os.geteuid() == 0, 'Root-owned release operator only')
    info = CONFIG.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o077 == 0,
            'Private root-owned release config required')
    config = json.loads(CONFIG.read_text())
    require(set(config) == {'runner_group_id', 'state_dir', 'images', 'max_runners', 'cpus_per_runner',
                           'binaryen_cores', 'memory_gib_per_runner', 'min_free_gib', 'max_age_seconds'}, 'Unexpected host configuration')
    require(config['runner_group_id'] == 6 and config['state_dir'] == '/var/lib/dash-ci-releases', 'Unexpected release group/state')
    require(config['max_runners'] == 1 and 0 < config['cpus_per_runner'] <= 20
            and 0 < config['memory_gib_per_runner'] <= 32 and config['min_free_gib'] >= 100
            and 0 < config['max_age_seconds'] <= 14400, 'Release resource bounds exceeded')
    release.binaryen_budget(config, config['cpus_per_runner'])
    require(set(config['images']) == {'npm', 'kotlin'}, 'Pin both release image kinds')
    for image in config['images'].values():
        require(release.re.fullmatch(r'(?:dashpay/dash-selfhosted-image@)?sha256:[0-9a-f]{64}', image), 'Immutable operator image required')
    state = Path(config['state_dir'])
    info = state.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o077 == 0, 'Private root-owned release journal required')
    return config


class PipeAPI:
    def call(self, path, data=None, method=None):
        sys.__stdout__.write('RPC ' + json.dumps({'path': path, 'data': data, 'method': method or ('POST' if data is not None else 'GET')}) + '\n')
        sys.__stdout__.flush()
        line = sys.stdin.readline(8 * MAX_BYTES + 1)
        require(line and len(line) <= 8 * MAX_BYTES and line.endswith('\n'), 'Invalid Gateway transport response')
        reply = json.loads(line)
        if 'error' in reply:
            raise urllib.error.URLError('Protected Gateway operation unavailable')
        require(set(reply) == {'result'}, 'Invalid Gateway envelope')
        return reply['result']


class OfflineAPI:
    def call(self, *args, **kwargs):
        raise urllib.error.URLError('Offline cleanup; registration tombstone retained')


def verify_contract(config, work):
    manifest = validate_manifest(work['manifest'])
    kind, _ = release.parse_job(work['run'], work['job'])
    name = 'release-contract-' + uuid.uuid4().hex
    args = ['docker', 'run', '--rm', '--name', name, '--pull', 'never', '--network', 'none', '--read-only',
            '--user', '1001:1001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true',
            '--cpus', '1', '--memory', '1g', '--pids-limit', '128', '-i', '--entrypoint', 'ci-image-contract',
            config['images'][kind], 'verify', '/dev/stdin']
    try:
        result = subprocess.run(args, input=json.dumps(manifest), text=True, capture_output=True, timeout=45)
        require(result.returncode == 0, 'Pinned release image does not satisfy this exact source manifest')
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)


def candidate_active():
    for label in ('org.dash.ci.candidate-pool=1', 'org.dash.ci.candidate=1'):
        if subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'label=' + label], text=True).strip():
            return True
    return False


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--plan', action='store_true'); parser.add_argument('--offline', action='store_true')
    args = parser.parse_args(); os.umask(0o077); config = read_config(); state = Path(config['state_dir'])
    with (state / 'controller.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('STATE ' + json.dumps({'locked': True, 'blocked': []})); return
        request = {'work': []} if args.offline else json.loads(sys.stdin.readline(MAX_BYTES + 1))
        require(set(request) == {'work'} and isinstance(request['work'], list) and len(request['work']) <= 20,
                'Bounded release work orders required')
        work, blocked = request['work'], []
        for item in work:
            require(set(item) == {'run', 'job', 'manifest'}, 'Invalid release work order')
            release.parse_job(item['run'], item['job'])
            validate_manifest(item['manifest'])
        def queued(api):
            if work and candidate_active():
                blocked.append({'reason': 'Release slot waits for the active candidate worker'})
                return
            for item in work:
                try:
                    verify_contract(config, item)
                    yield item['run'], item['job']
                except ValueError as error:
                    blocked.append({'job': item['job']['id'], 'reason': str(error)})
        release.queued_jobs = queued
        # Keep the protocol separate from allocator diagnostics (which may include
        # exception text). No raw worker logs or JIT configs go to stdout.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            release.reconcile(OfflineAPI() if args.offline else PipeAPI(), config,
                              apply=not args.plan, drain=args.offline)
        path = state / 'journal.json'
        journal = json.loads(path.read_text()) if path.exists() else {'allocations': {}}
        for record in journal['allocations'].values():
            if record['phase'] != 'running':
                blocked.append({'job': record['job_id'], 'reason': 'Release worker admission incomplete; inspect allocation receipt'})
        # A production allocator catches launch exceptions; surface an idle-slot
        # launch failure explicitly instead of returning a misleading healthy tick.
        if not args.plan and work and not journal['allocations'] and not blocked:
            blocked.append({'reason': 'Release work remains queued without a worker; inspect admission journal'})
        print('STATE ' + json.dumps({'allocations': journal['allocations'], 'blocked': blocked,
                                    'plan': args.plan, 'planned_jobs': [w['job']['id'] for w in work] if args.plan else []}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('STATE ' + json.dumps({'blocked': [{'reason': str(error) if isinstance(error, ValueError) else 'Host reconciliation unavailable'}]}), flush=True)
        sys.exit(1)
