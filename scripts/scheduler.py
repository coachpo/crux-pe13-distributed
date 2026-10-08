#!/usr/bin/env python3
"""Dispatch dependency-ordered waves on standard GitHub Actions runners.

Planning and input archival happen before this command. The scheduler compiles
nothing locally. Each wave consumes only successful upstream run artifacts.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import time


def gh(*arguments, json_output=False):
    result = subprocess.run(['gh', *map(str, arguments)], check=True,
                            text=True, stdout=subprocess.PIPE)
    return json.loads(result.stdout) if json_output else result.stdout.strip()


def say(message):
    print(message, flush=True)


def dispatch(repo, tag, wave, ref):
    old = {item['databaseId'] for item in gh(
        'run', 'list', '--repo', repo, '--workflow', 'wave.yml', '--limit', '100',
        '--json', 'databaseId', json_output=True)}
    gh('workflow', 'run', 'wave.yml', '--repo', repo, '--ref', ref,
       '-f', f'tag={tag}', '-f', f'wave={wave}')
    for _ in range(60):
        candidates = gh('run', 'list', '--repo', repo, '--workflow', 'wave.yml',
                        '--limit', '100', '--json',
                        'databaseId,displayTitle,headSha,url,status', json_output=True)
        for candidate in candidates:
            if candidate['databaseId'] not in old and tag in candidate['displayTitle'] \
                    and str(wave) in candidate['displayTitle']:
                return candidate
        time.sleep(5)
    raise RuntimeError('Dispatched workflow run did not appear within five minutes')


def wait(repo, run_id):
    previous = None
    while True:
        result = gh('run', 'view', run_id, '--repo', repo, '--json',
                    'status,conclusion,url,jobs', json_output=True)
        state = [(j['name'], j['status'], j['conclusion']) for j in result['jobs']]
        if state != previous:
            say(json.dumps({'run_id': run_id, 'jobs': state}, ensure_ascii=False))
            previous = state
        if result['status'] == 'completed':
            return result
        time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--ref', default='main')
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    plan_bytes = Path(args.plan).read_bytes()
    plan = json.loads(plan_bytes)
    plan_digest = hashlib.sha256(plan_bytes).hexdigest()
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        'repo': args.repo, 'tag': args.tag, 'plan_sha256': plan_digest,
        'runs': {}, 'started_at':
        datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if state['repo'] != args.repo or state['tag'] != args.tag:
        raise ValueError('Resume state belongs to a different repository or input release')
    if state['plan_sha256'] != plan_digest:
        raise ValueError('Resume state belongs to a different task plan')
    for wave in plan['waves']:
        wave_id = str(wave['id'])
        existing = state['runs'].get(wave_id)
        if existing and existing.get('conclusion') == 'success':
            continue
        if existing and not existing.get('conclusion'):
            say(f"Resuming wave {wave_id}: {existing['url']}")
            result = wait(args.repo, existing['run_id'])
            existing.update(conclusion=result['conclusion'], jobs=result['jobs'])
            state_path.write_text(json.dumps(state, indent=2) + '\n')
            if result['conclusion'] != 'success':
                raise RuntimeError(f"Wave {wave_id} failed: {result['url']}")
            continue
        tasks = []
        for task in wave['tasks']:
            dependencies = []
            for dep in task.get('dependencies', []):
                upstream = state['runs'][str(dep['wave'])]
                if upstream.get('conclusion') != 'success':
                    raise ValueError(f'Unsuccessful dependency: {dep}')
                dependencies.append({'run_id': upstream['run_id'],
                                     'artifact': 'shard-' + dep['shard']})
            tasks.append({**task, 'dependencies': dependencies})
        control = {'include': tasks}
        if not 1 <= len(tasks) <= 256:
            raise ValueError('Each wave must contain 1–256 tasks')
        control_path = state_path.parent / f'wave-{wave_id}.json'
        control_path.write_text(json.dumps(control, indent=2) + '\n')
        gh('release', 'upload', args.tag, control_path, '--repo', args.repo, '--clobber')
        run = dispatch(args.repo, args.tag, wave_id, args.ref)
        state['runs'][wave_id] = {'run_id': run['databaseId'], 'url': run['url']}
        state_path.write_text(json.dumps(state, indent=2) + '\n')
        say(f"Wave {wave_id}: {run['url']}")
        result = wait(args.repo, run['databaseId'])
        state['runs'][wave_id].update(conclusion=result['conclusion'], jobs=result['jobs'])
        state_path.write_text(json.dumps(state, indent=2) + '\n')
        if result['conclusion'] != 'success':
            raise RuntimeError(f"Wave {wave_id} failed: {result['url']}")
    state['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    state_path.write_text(json.dumps(state, indent=2) + '\n')
    say('All planned remote waves completed successfully.')


if __name__ == '__main__':
    main()
