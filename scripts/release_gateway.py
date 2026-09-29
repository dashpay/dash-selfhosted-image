#!/usr/bin/env python3
"""Permanent release discovery and protected Gateway-only GitHub transport.

No run/attempt allowlist and no credential on the Docker host. One invocation
per scheduler tick; all writes are bounded, journaled, and never blindly replayed.
"""
import argparse
import base64
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from image_contract import require, unique_object, validate_manifest
import release_runner as release

PLATFORM = 'dashpay/platform'
GROUP_ID = 6
MAX_BYTES = 8 * 1024 * 1024


def save(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class GitHub:
    def __init__(self):
        self.token = os.environ.get('GITHUB_MAC_CI_PAT', '')
        require(self.token.startswith('oc-sent-'), 'Protected Gateway credential unavailable')
        self.opener = urllib.request.build_opener(NoRedirect())

    def call(self, path, data=None, method='GET', missing_ok=False):
        require(not any(x in path for x in ('://', '\\', '..', '#'))
                and (path.startswith('repos/' + PLATFORM + '/')
                     or path.startswith('orgs/dashpay/actions/')), 'Unexpected API origin/path')
        req = urllib.request.Request('https://api.github.com/' + path,
            data=None if data is None else json.dumps(data).encode(), method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with self.opener.open(req, timeout=30) as response:
                raw = response.read(MAX_BYTES + 1)
            require(len(raw) <= MAX_BYTES, 'Oversized API response')
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if missing_ok and method == 'GET' and error.code == 404:
                return None
            raise RuntimeError('GitHub ' + method + ' HTTP ' + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError('GitHub transport unavailable; no mutation replay') from None


def pages(api, path, key):
    separator = '&' if '?' in path else '?'
    for page in range(1, 11):
        result = api.call(path + separator + f'per_page=100&page={page}')
        values = result[key]
        require(isinstance(values, list) and len(values) <= 100, 'Invalid API page')
        yield from values
        if len(values) < 100:
            return
    raise ValueError('Release discovery pagination limit; queued work not silently discarded')


def verify_group(api):
    prefix = f'orgs/dashpay/actions/runner-groups/{GROUP_ID}'
    group = api.call(prefix)
    repos = list(pages(api, prefix + '/repositories', 'repositories'))
    require(group['name'] == release.GROUP and group['visibility'] == 'selected'
            and not group['default'] and group['allows_public_repositories'] is True
            and {repo['full_name'] for repo in repos} == {PLATFORM}, 'Release group policy changed')
    return list(pages(api, prefix + '/runners', 'runners'))


def verify_run(api, run, excluded_runs=()):
    require(run['id'] not in excluded_runs, 'Explicitly excluded historical release')
    require(run.get('path') in release.WORKFLOWS and run.get('event') in ('release', 'workflow_dispatch')
            and run.get('repository', {}).get('full_name') == PLATFORM
            and run.get('head_repository', {}).get('full_name') == PLATFORM
            and re.fullmatch('[0-9a-f]{40}', run['head_sha']), 'Untrusted release workflow identity')
    ref = run['head_branch']
    require(isinstance(ref, str) and ref and len(ref) <= 255 and not any(x in ref for x in ('..', '~', '^', ':', '?', '#', '\\')),
            'Invalid release ref')
    encoded = urllib.parse.quote(ref, safe='')
    tag = api.call(f'repos/{PLATFORM}/git/ref/tags/{encoded}', missing_ok=True)
    if tag:
        obj = tag['object']
        for _ in range(3):
            if obj['type'] != 'tag':
                break
            require(re.fullmatch('[0-9a-f]{40}', obj['sha']), 'Invalid annotated tag')
            obj = api.call(f'repos/{PLATFORM}/git/tags/{obj["sha"]}')['object']
        require(obj['type'] == 'commit' and obj['sha'] == run['head_sha'], 'Release tag changed from workflow source')
        if run['event'] == 'release':
            published = api.call(f'repos/{PLATFORM}/releases/tags/{encoded}')
            require(not published['draft'] and published.get('published_at'), 'Release is not published')
        return
    require(run['event'] == 'workflow_dispatch', 'Release tag is missing')
    branch = api.call(f'repos/{PLATFORM}/branches/{encoded}')
    require(branch.get('protected') is True, 'Manual release workflow needs a protected branch or immutable tag')


def discover(api, config):
    jobs, ignored, seen = [], [], set()
    for workflow in release.WORKFLOWS:
        name = workflow.rsplit('/', 1)[1]
        for status in ('queued', 'in_progress'):
            path = f'repos/{PLATFORM}/actions/workflows/{name}/runs?status={status}'
            for summary in pages(api, path, 'workflow_runs'):
                if summary['id'] in seen or summary['id'] in config['excluded_runs']:
                    continue
                seen.add(summary['id'])
                run = api.call(f'repos/{PLATFORM}/actions/runs/{summary["id"]}')
                # Global/workflow listings can lag a completed run; exact state wins.
                if run['status'] == 'completed':
                    continue
                try:
                    verify_run(api, run, config['excluded_runs'])
                except ValueError as error:
                    ignored.append({'run': run['id'], 'reason': str(error)})
                    continue
                path = f'repos/{PLATFORM}/actions/runs/{run["id"]}/attempts/{run["run_attempt"]}/jobs'
                for job in pages(api, path, 'jobs'):
                    if job['status'] != 'queued' or not any(release.LABEL.fullmatch(label) for label in job.get('labels', [])):
                        continue
                    release.parse_job(run, job)
                    require(job.get('run_attempt', run['run_attempt']) == run['run_attempt'], 'Job attempt mismatch')
                    manifest_data = api.call(f'repos/{PLATFORM}/contents/.github/runner-requirements.json?ref={run["head_sha"]}')
                    require(manifest_data['encoding'] == 'base64' and manifest_data['size'] < 256 * 1024, 'Invalid manifest response')
                    manifest = json.loads(base64.b64decode(manifest_data['content'], validate=False))
                    validate_manifest(manifest)
                    jobs.append({'run': {key: run[key] for key in ('id', 'run_attempt', 'event', 'head_sha', 'head_branch', 'path', 'repository', 'head_repository')},
                                 'job': {key: job[key] for key in ('id', 'run_id', 'head_sha', 'status', 'labels', 'name', 'created_at')},
                                 'manifest': manifest})
    return sorted(jobs, key=lambda work: work['job']['id']), ignored


class Broker:
    def __init__(self, api, config, state, work, records):
        self.api, self.config, self.state = api, config, state
        self.work = {item['job']['id']: item for item in work}
        self.records = records
        self.run_ids = {item['run']['id'] for item in work} | {r['run_id'] for r in records.values()}
        self.job_ids = set(self.work) | {r['job_id'] for r in records.values()}

    def serve(self, req):
        require(set(req) == {'path', 'data', 'method'}, 'Invalid broker request')
        path, data, method = req['path'], req['data'], req['method']
        if method == 'GET':
            require(data is None, 'Unexpected read body')
            if re.fullmatch(r'orgs/dashpay/actions/runner-groups/6(?:/(?:repositories|runners))?(?:\?per_page=100&page=[1-9][0-9]*)?', path):
                return self.api.call(path)
            match = re.fullmatch(r'repos/dashpay/platform/actions/(runs|jobs)/([1-9][0-9]*)', path)
            require(match and int(match[2]) in (self.run_ids if match[1] == 'runs' else self.job_ids), 'Read outside current release work')
            return self.api.call(path)
        if method == 'POST':
            require(path == 'orgs/dashpay/actions/runners/generate-jitconfig'
                    and isinstance(data, dict) and set(data) == {'name', 'runner_group_id', 'labels', 'work_folder'}, 'Only one-job release registration is writable')
            match = release.NAME.fullmatch(data['name'])
            job_id = int(data['name'].split('-')[2]) if match else None
            require(job_id in self.work and data['runner_group_id'] == GROUP_ID and data['work_folder'] == '/work', 'JIT outside release scope')
            work = self.work[job_id]
            run = self.api.call(f'repos/{PLATFORM}/actions/runs/{work["run"]["id"]}')
            job = self.api.call(f'repos/{PLATFORM}/actions/jobs/{job_id}')
            verify_run(self.api, run, self.config['excluded_runs'])
            release.parse_job(run, job)
            require(run['head_sha'] == work['run']['head_sha'] and run['run_attempt'] == work['run']['run_attempt']
                    and len(data['labels']) == 4 and set(data['labels']) == set(job['labels']), 'Release changed before JIT registration')
            require(str(job_id) not in self.records, 'Existing JIT admission receipt; reconcile, never replay')
            record = {'name': data['name'], 'job_id': job_id, 'run_id': run['id'], 'head': run['head_sha'], 'attempt': run['run_attempt'], 'state': 'registering'}
            self.records[str(job_id)] = record
            save(self.state / 'registrations.json', self.records)
            result = self.api.call(path, data, 'POST')
            record.update(state='registered', runner_id=result['runner']['id'])
            save(self.state / 'registrations.json', self.records)
            return result
        require(method == 'DELETE' and re.fullmatch(r'orgs/dashpay/actions/runners/[1-9][0-9]*', path) and data is None, 'Mutation outside release scope')
        runner = self.api.call(path)
        record = next((r for r in self.records.values() if r['name'] == runner['name']), None)
        require(record and not runner['busy'] and (not record.get('runner_id') or record['runner_id'] == runner['id']), 'Registration cleanup identity mismatch')
        require(any(r['id'] == runner['id'] for r in verify_group(self.api)), 'Registration outside release group')
        return self.api.call(path, method='DELETE')


def host_pass(config, broker, work, plan=False):
    target = config['host']
    args = ['ssh', '-i', target['key'], '-p', str(target['port']), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', target['destination'],
            'sudo -n timeout 150 python3 ' + target['script'] + (' --plan' if plan else '')]
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    result = None
    try:
        process.stdin.write(json.dumps({'work': work}) + '\n')
        process.stdin.flush()
        # The constant remote timeout, SSH keepalive and scheduler invocation
        # timeout bound this blocking line protocol. Raw output is never echoed.
        for line in process.stdout:
            require(len(line) <= MAX_BYTES, 'Oversized host message')
            if line.startswith('RPC '):
                try:
                    response = {'result': broker.serve(json.loads(line[4:], object_pairs_hook=unique_object))}
                except Exception as error:
                    response = {'error': str(error) if isinstance(error, ValueError) else 'Protected API unavailable'}
                process.stdin.write(json.dumps(response) + '\n')
                process.stdin.flush()
            elif line.startswith('STATE '):
                result = json.loads(line[6:], object_pairs_hook=unique_object)
        require(process.wait(timeout=5) == 0 and result is not None, 'Release host reconciliation failed')
        return result
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdin.close()
        process.stdout.close()


def notifications(status, previous, now):
    """Durable transition/cooldown logic, independent of live notification transport."""
    alerts = list(status.get('blocked', []))
    active = {record['job_id'] for record in status.get('allocations', {}).values()}
    for job in status.get('queued', []):
        age = now - datetime.datetime.fromisoformat(job['created_at'].replace('Z', '+00:00')).timestamp()
        if job['id'] not in active and age >= 900:
            alerts.append({'job': job['id'], 'reason': 'release job queued over 15 minutes', 'age_minutes': int(age / 60)})
    key = hashlib.sha256(json.dumps([{k: v for k, v in item.items() if k != 'age_minutes'} for item in alerts], sort_keys=True).encode()).hexdigest()
    changed = key != previous.get('key') or now - previous.get('at', 0) >= 1800
    message = None
    if alerts and changed:
        message = 'Platform release allocator needs attention: ' + '; '.join(str(a.get('job', 'allocator')) + ': ' + a['reason'] for a in alerts[:8])
    elif not alerts and previous.get('active'):
        message = 'Platform release allocator recovered: no blocked or overdue queued jobs.'
    return message, {'key': key, 'at': now if message else previous.get('at', 0), 'active': bool(alerts)}


def notify(config, message):
    # Internal, creation-bound owner conversation only; never Slack/email/PR comments.
    result = subprocess.run(['openclaw', 'system', 'event', '--session-key', config['notify_session'], '--mode', 'now', '--text', message, '--json'],
                            capture_output=True, text=True, timeout=30)
    require(result.returncode == 0, 'Owner notification handoff failed')


def validate_config(config):
    require(set(config) == {'state_dir', 'host', 'candidate_lock', 'notify_session', 'excluded_runs'}, 'Unexpected Gateway config')
    require(config['state_dir'] == '/home/ubuntu/.openclaw/state/platform-release-pool'
            and config['candidate_lock'] == '/home/ubuntu/.openclaw/state/platform-candidate-pool/controller.lock', 'Unexpected allocator state/lock')
    require(re.fullmatch(r'agent:main:dashboard:[0-9a-f-]{36}', config['notify_session']), 'Expected private owner conversation')
    require(isinstance(config['excluded_runs'], list) and all(type(i) is int and i > 0 for i in config['excluded_runs']), 'Invalid historical exclusions')
    target = config['host']
    require(set(target) == {'destination', 'port', 'key', 'script'}, 'Unexpected SSH settings')
    require(re.fullmatch(r'[a-z_][a-z0-9_-]*@[a-zA-Z0-9.-]+', target['destination'])
            and type(target['port']) is int and 1 <= target['port'] <= 65535, 'Invalid SSH destination')
    for field, prefix in [('key', '/home/ubuntu/'), ('script', '/opt/dash-ci-release-pool/')]:
        require(target[field].startswith(prefix) and re.fullmatch(r'/[a-zA-Z0-9_./-]+', target[field])
                and '..' not in target[field], 'Invalid fixed SSH path')
    return config


def admission_lock(handle, timeout=40):
    # Avoid phase-lock starvation when the one-minute candidate and two-minute
    # release schedules consistently overlap. Never wait indefinitely.
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--config', required=True); parser.add_argument('--plan', action='store_true')
    args = parser.parse_args(); os.umask(0o077)
    config_path = Path(args.config)
    info = config_path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
            'Private operator-owned config required')
    config = validate_config(json.loads(config_path.read_text(), object_pairs_hook=unique_object))
    state = Path(config['state_dir']); state.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = int(time.time()); status = {'checked_at': now, 'queued': [], 'allocations': {}, 'blocked': [], 'ignored': []}
    with (state / 'controller.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'locked': True})); return
        # Serializes admission with the already-deployed candidate Gateway allocator.
        with Path(config['candidate_lock']).open('a') as candidate_lock:
            if not admission_lock(candidate_lock):
                print(json.dumps({'locked': True, 'reason': 'candidate admission active after bounded wait'})); return
            try:
                api = GitHub(); verify_group(api)
                records_path = state / 'registrations.json'
                records = json.loads(records_path.read_text()) if records_path.exists() else {}
                work, status['ignored'] = discover(api, config)
                status['queued'] = [item['job'] for item in work]
                result = host_pass(config, Broker(api, config, state, work, records), work, args.plan)
                status.update(result)
            except Exception as error:
                status['blocked'].append({'reason': str(error) if isinstance(error, (ValueError, RuntimeError)) else 'Allocator unavailable'})
            save(state / 'status.json', status)
    if not args.plan:
        prior_path = state / 'notification.json'
        prior = json.loads(prior_path.read_text()) if prior_path.exists() else {}
        message, receipt = notifications(status, prior, now)
        if message:
            save(state / 'notification-pending.json', {'at': now, 'message': message})
            notify(config, message + ' Inspect /home/ubuntu/.openclaw/state/platform-release-pool/status.json; preserve current workers and release publishing gates.')
        save(prior_path, receipt)
        (state / 'notification-pending.json').unlink(missing_ok=True)
    print(json.dumps(status))
    # A blocked queue is a reconciled, observable state, not a reason for the
    # scheduler to back off or auto-disable. Transport/config/notification
    # failures outside this state still fail the invocation visibly.
    return 0


if __name__ == '__main__':
    sys.exit(main())
