#!/usr/bin/env python3
"""Drive the cold wave chain from GitHub Actions instead of a build host.

The local scheduler read a plan and a state file, packed capsules on demand and
dispatched one workflow run per wave. This controller does the same work with only
GitHub as its state:

  * `inputs` makes sure the run's release carries every capsule and the source
    archive profile the plan needs, copying anything missing from the frozen
    releases and verifying the registry digest of every copy,
  * `wave` turns one wave of the plan into the control file the wave workflow
    downloads, resolving each task dependency to the producer run that actually
    holds the artifact, dispatches the wave, waits for it, verifies each successful
    task's artifact against the GitHub artifact digest and records the producer in
    the state,
  * tasks already recorded as successful in the state are reused and never
    recompiled, which is what makes a rerun after a single failed shard cheap.

Every capsule digest, artifact digest and slice manifest digest comes from the
frozen registry, so a reused producer is only accepted when its recorded manifest
is the frozen one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

WAVE_WORKFLOW = 'wave.yml'


def gh(*arguments, json_output=False):
    result = subprocess.run(['gh', *map(str, arguments)], check=True, text=True,
                            stdout=subprocess.PIPE)
    return json.loads(result.stdout) if json_output else result.stdout.strip()


def say(message):
    print(message, flush=True)


def load(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 22), b''):
            result.update(block)
    return result.hexdigest()


def release_assets(repository, tag):
    payload = gh('api', 'repos/%s/releases/tags/%s' % (repository, tag), json_output=True)
    return {asset['name']: asset for asset in payload['assets']}


def task_table(plan):
    return {task['id']: task for wave in plan['waves'] for task in wave['tasks']}


def wave_table(plan):
    return {wave['id']: wave for wave in plan['waves']}


def frozen_capsule(registry, task_id):
    for row in registry['capsules']:
        if row['id'] == task_id:
            return row
    raise SystemExit('Registry has no capsule for ' + task_id)


def command_inputs(args):
    plan = load(args.plan)
    registry = load(args.registry)
    view = subprocess.run(['gh', 'release', 'view', args.release, '--repo', plan['repository'],
                           '--json', 'tagName'], capture_output=True, text=True)
    if view.returncode != 0:
        gh('release', 'create', args.release, '--repo', plan['repository'], '--prerelease',
           '--title', 'Crux PE13 cold inputs ' + plan['build_number'],
           '--notes', 'Capsules consolidated by the controller from the frozen releases.')
    present = release_assets(plan['repository'], args.release)
    wanted = {}
    for wave in plan['waves']:
        for task in wave['tasks']:
            row = frozen_capsule(registry, task['id'])
            wanted[row['asset']] = row
    profile = registry['source_archives']
    wanted[profile['asset']] = {'asset': profile['asset'], 'release_tag': profile['release_tag'],
                                'sha256': profile['sha256'], 'bytes': profile['bytes']}
    copied = []
    staging = Path(args.staging)
    staging.mkdir(parents=True, exist_ok=True)
    for name, row in sorted(wanted.items()):
        if name in present:
            recorded = present[name]['digest'].removeprefix('sha256:')
            if recorded != row['sha256']:
                raise SystemExit('Release holds a different %s: %s' % (name, recorded))
            continue
        source = release_assets(plan['repository'], row.get('release_tag') or plan['input_tag'])
        if name not in source:
            raise SystemExit('Frozen source release has no ' + name)
        directory = staging / 'copy'
        directory.mkdir(parents=True, exist_ok=True)
        gh('release', 'download', row.get('release_tag') or plan['input_tag'], '--repo',
           plan['repository'], '--pattern', name, '--dir', str(directory))
        path = directory / name
        actual = digest(path)
        if actual != row['sha256']:
            raise SystemExit('Copied capsule digest mismatch: ' + name)
        gh('release', 'upload', args.release, str(path), '--repo', plan['repository'],
           '--clobber')
        upload = release_assets(plan['repository'], args.release)[name]
        if upload['digest'].removeprefix('sha256:') != row['sha256']:
            raise SystemExit('Uploaded capsule digest mismatch: ' + name)
        path.unlink()
        copied.append(name)
    summary = {'schema_version': 1, 'release': args.release, 'assets': len(wanted),
               'copied': copied, 'verified': sorted(name for name in wanted if name in present)}
    save(args.summary, summary)
    print(json.dumps(summary, indent=2))


def builder(plan, state, reuse):
    """Return the frozen dependency binding of one producer task."""
    tasks = task_table(plan)

    def binding(task_id):
        producer = state.get('tasks', {}).get(task_id) or (reuse or {}).get('tasks', {}).get(task_id)
        if not producer or producer.get('conclusion') != 'success':
            raise SystemExit('No successful producer recorded for ' + task_id)
        task = tasks[task_id]
        return {'id': task_id, 'run_id': producer['run_id'], 'artifact': producer.get('artifact')
                or 'shard-' + task_id,
                'expected_worker_commit': producer['headSha'],
                'expected_manifest_sha256': producer.get('manifest_sha256')}

    return tasks, binding


def command_wave(args):
    plan = load(args.plan)
    registry = load(args.registry)
    state = load(args.state, {'schema_version': 1, 'tasks': {}})
    reuse = load(args.reuse, {'schema_version': 1, 'tasks': {}}) or {'schema_version': 1, 'tasks': {}}
    tasks, binding = builder(plan, state, reuse)
    wave = wave_table(plan)[args.wave]
    selected = []
    for task in wave['tasks']:
        recorded = state.get('tasks', {}).get(task['id']) or reuse.get('tasks', {}).get(task['id'])
        if recorded and recorded.get('conclusion') == 'success' and task['id'] not in args.force.split():
            if recorded.get('manifest_sha256') and task.get('manifest_sha256') \
                    and recorded['manifest_sha256'] != task['manifest_sha256']:
                raise SystemExit('Reused producer manifest differs from the frozen plan: ' + task['id'])
            say('Reusing %s from run %s' % (task['id'], recorded['run_id']))
            continue
        row = {'id': task['id'],
               'dependencies': [binding(dependency) for dependency in task.get('dependencies', [])],
               'capsule_assets': task['capsule_assets'],
               'source_archives': plan['source_archives']}
        for key in ('shared_capsules', 'source_overlays'):
            if task.get(key):
                row[key] = task[key]
        selected.append(row)
    control = {'include': selected}
    Path(args.control).parent.mkdir(parents=True, exist_ok=True)
    Path(args.control).write_text(json.dumps(control, indent=2) + '\n')
    if not selected:
        say('Every task of wave %s is already recorded; nothing to dispatch' % args.wave)
        save(args.state_output, state)
        Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary).write_text(json.dumps({'wave': args.wave, 'dispatched': None,
                                                  'reused': [task['id'] for task in wave['tasks']]},
                                                 indent=2) + '\n')
        return
    gh('release', 'upload', args.release, args.control, '--repo', plan['repository'], '--clobber')
    started = time.time()
    gh('workflow', 'run', WAVE_WORKFLOW, '--repo', plan['repository'], '--ref', args.ref,
       '-f', 'tag=' + args.release, '-f', 'wave=' + str(args.wave))
    run = None
    while time.time() - started < 600:
        for candidate in gh('run', 'list', '--repo', plan['repository'], '--workflow', WAVE_WORKFLOW,
                            '--limit', '20', '--json',
                            'databaseId,event,createdAt,headSha,status,conclusion', json_output=True):
            created = time.strptime(candidate['createdAt'], '%Y-%m-%dT%H:%M:%SZ')
            # A dispatch returns no run id, so the newest dispatch started after the
            # request is the one that belongs to this wave.
            if candidate['event'] == 'workflow_dispatch' and time.mktime(created) >= started - 300:
                run = candidate
                break
        if run:
            break
        time.sleep(10)
    if not run:
        raise SystemExit('Dispatched wave run was not found')
    say('Wave %s: https://github.com/%s/actions/runs/%s' % (args.wave, plan['repository'],
                                                            run['databaseId']))
    while True:
        view = gh('run', 'view', str(run['databaseId']), '--repo', plan['repository'], '--json',
                  'status,conclusion,url,headSha,jobs', json_output=True)
        if view['status'] == 'completed':
            break
        time.sleep(30)
    if view['conclusion'] != 'success':
        raise SystemExit('Wave %s failed: %s' % (args.wave, view['url']))
    artifacts = {row['name']: row for row in gh(
        'api', 'repos/%s/actions/runs/%s/artifacts' % (plan['repository'], run['databaseId']),
        json_output=True)['artifacts']}
    recorded_ids = []
    for task in selected:
        name = 'shard-' + task['id']
        artifact = artifacts.get(name)
        if not artifact or artifact['expired']:
            raise SystemExit('Wave run did not publish ' + name)
        manifest = None
        for candidate in wave['tasks']:
            if candidate['id'] == task['id']:
                manifest = candidate.get('manifest_sha256')
        state.setdefault('tasks', {})[task['id']] = {
            'run_id': run['databaseId'], 'url': view['url'], 'headSha': view['headSha'],
            'conclusion': 'success', 'artifact': name, 'wave': args.wave,
            'digest': artifact['digest'], 'manifest_sha256': manifest,
        }
        recorded_ids.append(task['id'])
    state['plan_sha256'] = hashlib.sha256(Path(args.plan).read_bytes()).hexdigest()
    save(args.state_output, state)
    summary = {'wave': args.wave, 'run_id': run['databaseId'], 'run_url': view['url'],
               'head_sha': view['headSha'], 'dispatched': recorded_ids,
               'reused': [task['id'] for task in wave['tasks'] if task['id'] not in recorded_ids]}
    Path(args.summary).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary).write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


def command_summary(args):
    plan = load(args.plan)
    state = load(args.state, {'schema_version': 1, 'tasks': {}})
    reuse = load(args.reuse, {'schema_version': 1, 'tasks': {}}) or {'schema_version': 1, 'tasks': {}}
    merged = {**reuse.get('tasks', {}), **state.get('tasks', {})}
    missing = [task['id'] for wave in plan['waves'] for task in wave['tasks']
               if task['id'] not in merged]
    if missing:
        raise SystemExit('Tasks without a recorded producer: ' + ' '.join(sorted(missing)))
    terminal = merged[plan['terminal']['task']]
    kernel = merged[plan['kernel']['task']]
    summary = {'schema_version': 1, 'tasks': len(merged), 'missing': [],
               'terminal': {'task': plan['terminal']['task'], 'run_id': terminal['run_id'],
                            'artifact': terminal.get('artifact', 'shard-' + plan['terminal']['task']),
                            'head_sha': terminal['headSha']},
               'kernel': {'task': plan['kernel']['task'], 'run_id': kernel['run_id'],
                          'artifact': kernel.get('artifact', 'shard-' + plan['kernel']['task']),
                          'head_sha': kernel['headSha']},
               'worker_commits': sorted({row['headSha'] for row in merged.values()})}
    Path(args.summary).write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)

    inputs = commands.add_parser('inputs')
    inputs.add_argument('--plan', required=True)
    inputs.add_argument('--registry', required=True)
    inputs.add_argument('--release', required=True)
    inputs.add_argument('--staging', default='/tmp/controller-inputs')
    inputs.add_argument('--summary', required=True)
    inputs.set_defaults(function=command_inputs)

    wave = commands.add_parser('wave')
    wave.add_argument('--plan', required=True)
    wave.add_argument('--registry', required=True)
    wave.add_argument('--release', required=True)
    wave.add_argument('--wave', type=int, required=True)
    wave.add_argument('--ref', default='main')
    wave.add_argument('--state', required=True)
    wave.add_argument('--reuse')
    wave.add_argument('--force', default='', help='Space separated shard ids to recompile')
    wave.add_argument('--state-output', required=True)
    wave.add_argument('--control', required=True)
    wave.add_argument('--summary', required=True)
    wave.set_defaults(function=command_wave)

    summary = commands.add_parser('summary')
    summary.add_argument('--plan', required=True)
    summary.add_argument('--state', required=True)
    summary.add_argument('--reuse')
    summary.add_argument('--summary', required=True)
    summary.set_defaults(function=command_summary)

    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.release):
        parser.error('Release tag contains unsupported characters')
    args.function(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
