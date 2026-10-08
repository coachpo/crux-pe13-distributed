#!/usr/bin/env python3
"""Dispatch dependency-ordered waves on standard GitHub Actions runners.

The scheduler archives source inputs on demand and compiles nothing locally.
Each wave consumes only successful upstream run artifacts.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_plan import assets_for, digest


def gh(*arguments, json_output=False):
    result = subprocess.run(['gh', *map(str, arguments)], check=True,
                            text=True, stdout=subprocess.PIPE)
    return json.loads(result.stdout) if json_output else result.stdout.strip()


def say(message):
    print(message, flush=True)


def save(path, state):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(state, indent=2) + '\n')
    temporary.replace(path)


def validate_frozen_file(spec):
    if digest(spec['path']) != spec['sha256']:
        raise ValueError('Frozen input changed: ' + spec['path'])


def pin_worker(repo, ref, tag, state, state_path):
    if not state.get('worker_sha'):
        state['worker_sha'] = gh('api', f'repos/{repo}/commits/{ref}', '--jq', '.sha')
        if not re.fullmatch(r'[a-f0-9]{40}', state['worker_sha']):
            raise ValueError('Could not resolve the exact worker commit')
    state.setdefault('worker_ref', 'worker-' + tag + '-' + state['worker_sha'][:12])
    endpoint = f"repos/{repo}/git/ref/tags/{state['worker_ref']}"
    try:
        actual = gh('api', endpoint, '--jq', '.object.sha')
    except subprocess.CalledProcessError:
        actual = gh('api', f'repos/{repo}/git/refs', '--method', 'POST',
                    '-f', "ref=refs/tags/" + state['worker_ref'],
                    '-f', 'sha=' + state['worker_sha'], '--jq', '.object.sha')
    if actual != state['worker_sha']:
        raise ValueError('Worker tag points to a different commit: ' + state['worker_ref'])
    save(state_path, state)
    return state['worker_ref']


def prepare_task(plan, task, state, state_path, repo, tag):
    recipe = task.get('preparation')
    if not recipe:
        return task
    inputs = state.setdefault('inputs', {})
    existing = inputs.get(task['id'])
    if existing and existing.get('uploaded'):
        return {key: value for key, value in task.items() if key != 'preparation'} | {
            'capsule_assets': [part['name'] for part in existing['parts']]}
    preparation = plan['preparation']
    for spec in recipe['slice_files']:
        validate_frozen_file(spec)
    archive = Path(recipe['archive'])
    metadata_path = archive.with_name(archive.name.removesuffix('.tar.zst') + '.json')
    accepted_archive = False
    if archive.is_file() and metadata_path.is_file():
        if existing and existing.get('metadata_sha256'):
            if digest(metadata_path) != existing['metadata_sha256']:
                raise ValueError('Prepared capsule metadata changed during resume: ' + task['id'])
            accepted_archive = True
        elif recipe.get('existing_metadata'):
            validate_frozen_file(recipe['existing_metadata'])
            accepted_archive = True
    if not accepted_archive:
        command = [*preparation['pack_command'], 'pack', '--manifest', recipe['manifest'],
                   '--source-root', plan['source_root'], '--out-root', preparation['out_root'],
                   '--output', str(archive)]
        if preparation.get('shared_manifest'):
            validate_frozen_file(preparation['shared_manifest'])
            command += ['--shared-manifest', preparation['shared_manifest']['path']]
        if preparation.get('digest_cache'):
            command += ['--digest-cache', preparation['digest_cache']]
        log_path = state_path.parent / (task['id'] + '-pack.log')
        say('Preparing cold source capsule: ' + task['id'])
        with log_path.open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            inputs[task['id']] = {'status': 'pack_failed', 'log': str(log_path),
                                  'exit_code': result.returncode}
            save(state_path, state)
            raise RuntimeError('Cold input audit/packing failed: ' + task['id'] + '; see ' + str(log_path))
    metadata = json.loads(metadata_path.read_text())
    expected_manifest = next(spec['sha256'] for spec in recipe['slice_files']
                             if spec['path'] == recipe['manifest'])
    if metadata['manifest_sha256'] != expected_manifest or metadata['source_root'] != plan['source_root'] \
            or metadata['out_root'] != preparation['out_root']:
        raise ValueError('Cold capsule source/graph identity differs from frozen plan: ' + task['id'])
    runtime = json.loads(Path(recipe['manifest']).read_text()).get('runtime_dir', '.crux-task/graph')
    bundled = {spec['path']: spec for spec in metadata['files']}
    for spec in recipe['slice_files']:
        path = str(Path(plan['source_root']) / runtime / Path(spec['path']).relative_to(Path(recipe['manifest']).parent))
        if bundled.get(path, {}).get('sha256') != spec['sha256']:
            raise ValueError('Cold capsule has a different frozen slice file: ' + path)
    if preparation.get('shared_manifest'):
        shared = preparation['shared_manifest']
        validate_frozen_file(shared)
        if not any(layer['manifest_sha256'] == shared['sha256'] for layer in metadata.get('shared_layers', [])):
            raise ValueError('Cold capsule has a different frozen shared source layer: ' + task['id'])
    frozen_inputs = state.setdefault('frozen_inputs', {})
    current_inputs = {}
    for spec in metadata['files']:
        if 'task-graph' in spec.get('reasons', []):
            continue  # Each shard intentionally restores its own graph at this runtime path.
        keys = ['type', 'mode']
        if spec['type'] == 'file':
            keys += ['size', 'sha256']
        elif spec['type'] == 'symlink':
            keys += ['target']
        identity = {key: spec[key] for key in keys}
        if spec['path'] in frozen_inputs and frozen_inputs[spec['path']] != identity:
            raise ValueError('Input differs from an earlier cold capsule: ' + spec['path'])
        current_inputs[spec['path']] = identity
    archive_spec = metadata['archive']
    if archive.name != archive_spec['name'] or archive.stat().st_size != archive_spec['size'] \
            or digest(archive) != archive_spec['sha256']:
        raise ValueError('Cold capsule does not match its transfer receipt: ' + task['id'])
    parts = assets_for(archive)
    part_specs = [{'name': part.name, 'path': str(part), 'size': part.stat().st_size,
                   'sha256': archive_spec['sha256'] if part == archive else digest(part)} for part in parts]
    record = {'status': 'ready', 'archive_sha256': archive_spec['sha256'],
              'metadata_sha256': digest(metadata_path), 'parts': part_specs}
    if existing and existing.get('archive_sha256') and existing['archive_sha256'] != record['archive_sha256']:
        raise ValueError('Prepared capsule changed during resume: ' + task['id'])
    frozen_inputs.update(current_inputs)
    inputs[task['id']] = record
    save(state_path, state)
    for part in part_specs:
        say('Uploading cold source capsule: ' + part['name'])
        gh('release', 'upload', tag, part['path'], '--repo', repo, '--clobber')
    record['uploaded'] = True
    save(state_path, state)
    return {key: value for key, value in task.items() if key != 'preparation'} | {
        'capsule_assets': [part['name'] for part in part_specs]}


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
            if candidate['databaseId'] not in old \
                    and candidate['displayTitle'] == f'PE13 {tag} wave {wave}':
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
    parser.add_argument('--prepare-only', action='store_true', help='prepare the next unfinished wave without dispatch')
    parser.add_argument('--stop-after-wave', type=int, help='stop after this wave completes')
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
    if state.get('workflow_ref', args.ref) != args.ref:
        raise ValueError('Resume state belongs to a different worker revision')
    if plan.get('schema_version') == 2 and state['runs'] and not state.get('worker_sha'):
        raise ValueError('Lazy resume state does not identify its worker commit')
    state['workflow_ref'] = args.ref
    if plan.get('preparation'):
        validate_frozen_file(plan['preparation']['graph_plan'])
    for spec in plan.get('release_inputs', []):
        validate_frozen_file(spec)
        uploads = state.setdefault('release_inputs', {})
        if uploads.get(Path(spec['path']).name) != spec['sha256']:
            gh('release', 'upload', args.tag, spec['path'], '--repo', args.repo, '--clobber')
            uploads[Path(spec['path']).name] = spec['sha256']
            save(state_path, state)
    for wave in plan['waves']:
        wave_id = str(wave['id'])
        existing = state['runs'].get(wave_id)
        if existing and state.get('worker_sha'):
            if not existing.get('headSha'):
                existing['headSha'] = gh('run', 'view', existing['run_id'], '--repo', args.repo,
                                         '--json', 'headSha', json_output=True)['headSha']
                save(state_path, state)
            if existing['headSha'] != state['worker_sha']:
                raise ValueError('Saved run has a different worker commit: ' + existing['url'])
        if existing and existing.get('conclusion') == 'success':
            if args.stop_after_wave is not None and wave['id'] >= args.stop_after_wave:
                return
            continue
        if existing and not existing.get('conclusion'):
            say(f"Resuming wave {wave_id}: {existing['url']}")
            result = wait(args.repo, existing['run_id'])
            existing.update(conclusion=result['conclusion'], jobs=result['jobs'])
            save(state_path, state)
            if result['conclusion'] != 'success':
                raise RuntimeError(f"Wave {wave_id} failed: {result['url']}")
            if args.stop_after_wave is not None and wave['id'] >= args.stop_after_wave:
                return
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
            prepared = prepare_task(plan, task, state, state_path, args.repo, args.tag)
            tasks.append({**prepared, 'dependencies': dependencies})
        control = {'include': tasks}
        if not 1 <= len(tasks) <= 256:
            raise ValueError('Each wave must contain 1–256 tasks')
        control_path = state_path.parent / f'wave-{wave_id}.json'
        control_path.write_text(json.dumps(control, indent=2) + '\n')
        gh('release', 'upload', args.tag, control_path, '--repo', args.repo, '--clobber')
        state.setdefault('waves', {})[wave_id] = {'status': 'ready', 'control': str(control_path)}
        save(state_path, state)
        if args.prepare_only:
            say('Next wave inputs and control are ready: ' + wave_id)
            return
        worker_ref = pin_worker(args.repo, args.ref, args.tag, state, state_path)
        run = dispatch(args.repo, args.tag, wave_id, worker_ref)
        if existing:
            state.setdefault('failed_runs', {}).setdefault(wave_id, []).append(existing)
        state['runs'][wave_id] = {'run_id': run['databaseId'], 'url': run['url'], 'headSha': run['headSha']}
        save(state_path, state)
        if run['headSha'] != state['worker_sha']:
            state['runs'][wave_id]['conclusion'] = 'worker_identity_mismatch'
            save(state_path, state)
            raise ValueError('Dispatched run has a different worker commit: ' + run['url'])
        say(f"Wave {wave_id}: {run['url']}")
        result = wait(args.repo, run['databaseId'])
        state['runs'][wave_id].update(conclusion=result['conclusion'], jobs=result['jobs'])
        save(state_path, state)
        if result['conclusion'] != 'success':
            raise RuntimeError(f"Wave {wave_id} failed: {result['url']}")
        if args.stop_after_wave is not None and wave['id'] >= args.stop_after_wave:
            return
    state['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save(state_path, state)
    say('All planned remote waves completed successfully.')


if __name__ == '__main__':
    main()
