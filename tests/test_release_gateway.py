import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release_gateway as gateway
import release_gateway_host as host

HEAD = 'a' * 40
MANIFEST = json.loads((Path(__file__).parent / 'fixtures/legacy-e49e8bc/manifest.json').read_text())


def pair(run_id=100, attempt=2, job_id=200):
    run = {'id': run_id, 'run_attempt': attempt, 'event': 'release', 'head_sha': HEAD, 'head_branch': 'v4.2.0-beta.7',
           'path': '.github/workflows/release.yml', 'repository': {'full_name': gateway.PLATFORM},
           'head_repository': {'full_name': gateway.PLATFORM}, 'status': 'queued'}
    job = {'id': job_id, 'run_id': run_id, 'head_sha': HEAD, 'run_attempt': attempt, 'status': 'queued', 'name': 'Build NPM packages',
           'created_at': '2026-09-29T12:00:00Z', 'labels': ['self-hosted', 'Linux', 'X64', f'platform-release-{run_id}-{attempt}-npm']}
    return run, job


class FakeAPI:
    def __init__(self, run=None, job=None):
        self.run, self.job = (run, job) if run else pair()
        self.writes = []
        self.tag = {'object': {'type': 'commit', 'sha': HEAD}}
        self.group = {'name': gateway.release.GROUP, 'visibility': 'selected', 'default': False, 'allows_public_repositories': True}
        self.fail_post = False
    def call(self, path, data=None, method='GET', missing_ok=False):
        if method != 'GET':
            self.writes.append((path, data, method))
            if self.fail_post:
                raise RuntimeError('Ambiguous network write')
            return {'runner': {'id': 300}, 'encoded_jit_config': 'DUMMY-NOT-A-CREDENTIAL'}
        if '/git/ref/tags/' in path:return self.tag
        if '/releases/tags/' in path:return {'draft': False, 'published_at': '2026-09-29T00:00:00Z'}
        if '/branches/' in path:return {'protected': True}
        if '/actions/workflows/release.yml/runs?' in path:return {'workflow_runs': [self.run]}
        if '/actions/workflows/release-kotlin-sdk.yml/runs?' in path:return {'workflow_runs': []}
        if '/attempts/' in path:return {'jobs': [self.job]}
        if '/contents/' in path:return {'encoding': 'base64', 'size': len(json.dumps(MANIFEST)), 'content': base64.b64encode(json.dumps(MANIFEST).encode()).decode()}
        if '/actions/runs/' in path:return self.run
        if '/actions/jobs/' in path:return self.job
        if '/runner-groups/6/repositories' in path:return {'repositories': [{'full_name': gateway.PLATFORM}]}
        if '/runner-groups/6/runners' in path:return {'runners': []}
        if '/runner-groups/6' in path:return self.group
        raise AssertionError(path)


class ReleaseGatewayTests(unittest.TestCase):
    def test_overlapping_schedules_wait_for_candidate_then_remain_bounded(self):
        with patch.object(gateway.fcntl, 'flock', side_effect=[BlockingIOError, None]), patch.object(gateway.time, 'sleep'):
            self.assertTrue(gateway.admission_lock(object()))
        with patch.object(gateway.fcntl, 'flock', side_effect=BlockingIOError), patch.object(gateway.time, 'sleep'):
            self.assertFalse(gateway.admission_lock(object(), timeout=0))

    def test_future_runs_and_attempts_discovered_without_edits(self):
        for run_id, attempt, job_id in [(100, 1, 200), (100, 2, 201), (999, 7, 777)]:
            api = FakeAPI(*pair(run_id, attempt, job_id))
            jobs, ignored = gateway.discover(api, {'excluded_runs': []})
            self.assertEqual([w['job']['id'] for w in jobs], [job_id])
            self.assertFalse(ignored)
            self.assertFalse(api.writes)
    def test_old_attempt_label_rejected(self):
        run, job = pair(); job['labels'][-1] = 'platform-release-100-1-npm'
        with self.assertRaises(ValueError):gateway.discover(FakeAPI(run, job), {'excluded_runs': []})
    def test_explicit_legacy_exclusion(self):
        self.assertEqual(gateway.discover(FakeAPI(), {'excluded_runs': [100]}), ([], []))
    def test_untrusted_ref_or_repo_rejected(self):
        for key, value in [('event', 'pull_request'), ('path', '.github/workflows/evil.yml'), ('head_repository', {'full_name': 'evil/platform'})]:
            api = FakeAPI(); api.run[key] = value
            with self.assertRaises(ValueError):gateway.verify_run(api, api.run)
        api = FakeAPI(); api.tag['object']['sha'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'tag changed'):gateway.verify_run(api, api.run)
    def test_unprotected_manual_branch_rejected(self):
        api = FakeAPI(); api.tag = None; api.run['event'] = 'workflow_dispatch'
        original = api.call
        api.call = lambda path, **kw: {'protected': False} if '/branches/' in path else original(path, **kw)
        with self.assertRaisesRegex(ValueError, 'protected branch'):gateway.verify_run(api, api.run)
    def test_group_cannot_expand(self):
        api = FakeAPI(); api.group['visibility'] = 'all'
        with self.assertRaises(ValueError):gateway.verify_group(api)
    def broker(self, folder, api):
        work = [{'run': api.run, 'job': api.job, 'manifest': MANIFEST}]
        return gateway.Broker(api, {'excluded_runs': []}, Path(folder), work, {})
    def request(self):
        return {'path': 'orgs/dashpay/actions/runners/generate-jitconfig', 'method': 'POST',
                'data': {'name': 'platform-release-200-abcdefabcdef', 'runner_group_id': 6, 'labels': pair()[1]['labels'], 'work_folder': '/work'}}
    def test_mutations_strictly_scoped(self):
        for key, value in [('runner_group_id', 4), ('work_folder', '/runner'), ('labels', ['npm-build']), ('name', 'platform-release-201-abcdefabcdef')]:
            with tempfile.TemporaryDirectory() as folder:
                api = FakeAPI(); req = self.request(); req['data'][key] = value
                with self.assertRaises(ValueError):self.broker(folder, api).serve(req)
                self.assertFalse(api.writes)
    def test_ambiguous_registration_never_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            api = FakeAPI(); api.fail_post = True; broker = self.broker(folder, api)
            with self.assertRaises(RuntimeError):broker.serve(self.request())
            with self.assertRaisesRegex(ValueError, 'never replay'):broker.serve(self.request())
            self.assertEqual(len(api.writes), 1)
            self.assertEqual(json.loads((Path(folder) / 'registrations.json').read_text())['200']['state'], 'registering')
    def test_latest_attempt_rechecked_before_registration(self):
        with tempfile.TemporaryDirectory() as folder:
            api = FakeAPI(); broker = self.broker(folder, api); broker.work = copy.deepcopy(broker.work)
            api.run['run_attempt'] = 3
            with self.assertRaises(ValueError):broker.serve(self.request())
            self.assertFalse(api.writes)
    def test_arbitrary_api_egress_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            api = FakeAPI(); broker = self.broker(folder, api)
            for path, method in [('https://evil.test', 'GET'), ('repos/dashpay/platform/releases', 'POST'), ('orgs/dashpay/actions/runner-groups/4', 'GET')]:
                with self.assertRaises(ValueError):broker.serve({'path': path, 'method': method, 'data': None})
            self.assertFalse(api.writes)
    def test_queue_alert_cooldown_recovery_and_worker_distinction(self):
        now = 1790684700 # 12:25 UTC
        job = pair()[1]
        status = {'queued': [job], 'allocations': {}, 'blocked': []}
        message, prior = gateway.notifications(status, {}, now)
        self.assertIn('queued over 15 minutes', message)
        self.assertIsNone(gateway.notifications(status, prior, now + 60)[0])
        status['allocations'] = {'worker': {'job_id': job['id']}}
        self.assertIn('recovered', gateway.notifications(status, prior, now + 60)[0])
        self.assertIn('attention', gateway.notifications({'blocked': [{'reason': 'host unavailable'}]}, {}, now)[0])
    def test_manifest_failure_prevents_allocation(self):
        with patch.object(host.subprocess, 'run', return_value=Mock(returncode=1)) as proc:
            with self.assertRaisesRegex(ValueError, 'exact source manifest'):
                host.verify_contract({'images': {'npm': 'sha256:' + 'f' * 64}}, {'run': pair()[0], 'job': pair()[1], 'manifest': MANIFEST})
        self.assertEqual(proc.call_count, 2) # bounded contract probe plus exact-name cleanup
    def test_candidate_worker_blocks_shared_release_slot(self):
        with patch.object(host.subprocess, 'check_output', return_value='platform-pr-6000-npm-1-12345678'):
            self.assertTrue(host.candidate_active())



class WatchdogTests(unittest.TestCase):
    def test_independent_missing_disabled_stale_and_recovery(self):
        from release_watchdog import diagnose, RELEASE_JOB, CANDIDATE_JOB
        jobs=[{'id':i,'enabled':True,'state':{'consecutiveErrors':0}} for i in (RELEASE_JOB,CANDIDATE_JOB)]
        self.assertFalse(diagnose(jobs, {'checked_at':1000}, 1001))
        self.assertIn('10 minutes', diagnose(jobs, {'checked_at':1000}, 1601)[0]['reason'])
        self.assertTrue(diagnose(jobs, {'checked_at':1000,'plan':True}, 1001))
        jobs[0]['enabled']=False
        self.assertIn('disabled', diagnose(jobs, {'checked_at':1000}, 1001)[0]['reason'])
        jobs[0]['enabled']=True; jobs[1]['state']['consecutiveErrors']=2
        self.assertIn('repeated', diagnose(jobs, {'checked_at':1000}, 1001)[0]['reason'])
        jobs[1]['state']['consecutiveErrors']=0
        self.assertFalse(diagnose(jobs, {'checked_at':1000}, 1001))

class DeploymentBundleTests(unittest.TestCase):
    def test_staged_bundle_validates_manifest_without_checkout_dependencies(self):
        import shutil, subprocess
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            staged = Path(folder)
            for relative in (root / 'deploy/release-gateway.files').read_text().splitlines():
                destination = staged / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / relative, destination)
            (staged / 'manifest.json').write_text(json.dumps(MANIFEST))
            command = "import sys,json;sys.path.insert(0,'scripts');import release_gateway,release_gateway_host,release_watchdog;release_gateway.validate_manifest(json.load(open('manifest.json')))"
            result = subprocess.run([sys.executable, '-I', '-c', command], cwd=staged, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':unittest.main()
