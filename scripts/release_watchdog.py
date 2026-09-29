#!/usr/bin/env python3
"""Independent local watchdog: no GitHub credential or allocation authority."""
import argparse
import json
import pathlib
import subprocess
import time

from release_gateway import notifications, notify, save, validate_config

RELEASE_JOB = '76ed857a-9ad0-4f03-b37c-57480e1fa357'
CANDIDATE_JOB = 'eddd686d-2332-4d0e-92cd-ba2980e8bde0'


def diagnose(jobs, status, now):
    indexed = {job['id']: job for job in jobs}
    issues = []
    for job_id, label in ((RELEASE_JOB, 'release'), (CANDIDATE_JOB, 'candidate')):
        job = indexed.get(job_id)
        if not job or not job.get('enabled'):
            issues.append({'reason': label + ' allocator schedule disabled or missing'})
        elif job.get('state', {}).get('consecutiveErrors', 0) >= 2:
            issues.append({'reason': label + ' allocator has repeated execution failures'})
    if not status or status.get('plan') or now - status.get('checked_at', 0) >= 600:
        issues.append({'reason': 'release allocator has no live receipt within 10 minutes'})
    if status:
        issues.extend(status.get('blocked', []))
    return issues


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = validate_config(json.loads(pathlib.Path(args.config).read_text()))
    state = pathlib.Path(config['state_dir']); now = int(time.time())
    try:
        result = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Scheduler status unavailable')
        inventory = json.loads(result.stdout)
        if inventory.get('hasMore'):
            raise RuntimeError('Scheduler inventory incomplete')
        status_path = state / 'status.json'
        status = json.loads(status_path.read_text()) if status_path.exists() else None
        issues = diagnose(inventory['jobs'], status, now)
    except Exception:
        issues = [{'reason': 'independent watchdog could not read allocator/scheduler state'}]
    prior_path = state / 'watchdog-notification.json'
    prior = json.loads(prior_path.read_text()) if prior_path.exists() else {}
    message, receipt = notifications({'blocked': issues}, prior, now)
    save(state / 'watchdog-status.json', {'checked_at': now, 'issues': issues})
    if message:
        save(state / 'watchdog-pending.json', {'at': now, 'message': message})
        notify(config, 'Independent CI watchdog: ' + message + ' Inspect permanent allocator status; do not interrupt running workers.')
    save(prior_path, receipt)
    (state / 'watchdog-pending.json').unlink(missing_ok=True)
    print(json.dumps({'checked_at': now, 'issues': issues, 'notification_handoff': bool(message)}))


if __name__ == '__main__':main()
