#!/usr/bin/env python3
"""Dispatch dependency-ordered waves on standard GitHub Actions runners.

The scheduler archives source inputs on demand and compiles nothing locally.
Each wave consumes artifacts from successful upstream shards.
"""
import argparse
import copy
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


def validate_worker(state, sha, description):
    expected = state.get('worker_sha')
    if not expected:
        return
    accepted = state.get('accepted_worker_commits', [expected])
    if not isinstance(accepted, list) or expected not in accepted or any(
            not isinstance(item, str) or not re.fullmatch(r'[a-f0-9]{40}', item) for item in accepted):
        raise ValueError('Invalid explicitly accepted worker commit list')
    if sha not in accepted:
        raise ValueError(description + ' has a different worker commit')


def successful_task(state, identity):
    producer = state.get('task_runs', {}).get(identity)
    if not producer or producer.get('conclusion') != 'success':
        return None
    validate_worker(state, producer.get('headSha'), 'Task producer ' + identity)
    return producer


def frozen_manifest(task):
    recipe = task.get('preparation')
    return next(spec['sha256'] for spec in recipe['slice_files']
                if spec['path'] == recipe['manifest']) if recipe else None


def correction_manifest(task, correction, verify_proof=False):
    original = frozen_manifest(task)
    if not correction or not any(key in correction for key in
                                  ('original_manifest_sha256', 'variant_manifest_sha256', 'variant_provenance')):
        return original, None
    provenance = correction.get('variant_provenance', {})
    if not isinstance(provenance, dict) or not isinstance(provenance.get('proof', {}), dict):
        raise ValueError('Invalid variant manifest provenance: ' + task['id'])
    proof = provenance.get('proof', {})
    variant = correction.get('variant_manifest_sha256')
    if correction.get('approved') is not True or correction.get('original_manifest_sha256') != original \
            or not original or not isinstance(variant, str) or not re.fullmatch(r'[a-f0-9]{64}', variant) \
            or provenance.get('verified') is not True or not Path(proof.get('path', '')).is_absolute() \
            or not re.fullmatch(r'[a-f0-9]{64}', proof.get('sha256', '')):
        raise ValueError('Variant manifest lacks approved frozen original/provenance: ' + task['id'])
    if verify_proof:
        validate_frozen_file(proof)
    return variant, original


def record_successful_tasks(state, wave, run):
    """Retain successful shards even when another job makes the run fail."""
    known = {task['id']: task for task in wave['tasks']}
    changed = False
    for job in run.get('jobs', []):
        match = re.match(r'^compile \(([^, )]+)', job['name'])
        if not match or match[1] not in known or job.get('conclusion') != 'success':
            continue
        identity = match[1]
        validate_worker(state, run.get('headSha'), 'Successful run ' + str(run['run_id']))
        correction = run.get('input_corrections', {}).get(identity)
        manifest_sha, original_sha = correction_manifest(known[identity], correction)
        prior = successful_task(state, identity)
        if prior:
            if manifest_sha:
                if prior.get('manifest_sha256', manifest_sha) != manifest_sha:
                    raise ValueError('Task producer uses a different frozen manifest: ' + identity)
                if 'manifest_sha256' not in prior:
                    prior['manifest_sha256'] = manifest_sha
                    changed = True
            continue
        producer = {'run_id': run['run_id'], 'url': run['url'], 'headSha': run.get('headSha'),
                    'conclusion': 'success', 'wave': wave['id'], 'artifact': 'shard-' + identity}
        if manifest_sha:
            producer['manifest_sha256'] = manifest_sha
        if original_sha:
            producer.update(original_manifest_sha256=original_sha, variant_manifest_sha256=manifest_sha,
                            variant_provenance=copy.deepcopy(correction['variant_provenance']))
        for source, target in [('databaseId', 'job_id'), ('url', 'job_url')]:
            if source in job:
                producer[target] = job[source]
        if identity in run.get('input_corrections', {}):
            producer['input_correction'] = copy.deepcopy(run['input_corrections'][identity])
        state.setdefault('task_runs', {})[identity] = producer
        changed = True
    return changed


def dependency_producer(state, dependency):
    identity = dependency['shard']
    producer = successful_task(state, identity)
    if not producer:
        if identity in state.get('task_runs', {}):
            raise ValueError('Unsuccessful task dependency: ' + identity)
        producer = state['runs'][str(dependency['wave'])]
        if producer.get('conclusion') != 'success' or (
                'task_ids' in producer and identity not in producer['task_ids']):
            raise ValueError('Unsuccessful dependency: ' + str(dependency))
        validate_worker(state, producer.get('headSha'), 'Dependency run ' + str(producer['run_id']))
    result = {'id': identity, 'run_id': producer['run_id'], 'artifact': 'shard-' + identity}
    if producer.get('headSha'):
        result['expected_worker_commit'] = producer['headSha']
    if producer.get('manifest_sha256'):
        result['expected_manifest_sha256'] = producer['manifest_sha256']
    return result


def require_wave_producers(state, wave, run):
    # Older full-wave records, including the accepted bootstrap import, can
    # still supply their whole wave. A subset retry must retain every producer.
    if 'task_ids' in run:
        missing = [task['id'] for task in wave['tasks'] if not successful_task(state, task['id'])]
        if missing:
            raise ValueError('Successful wave attempt is missing producer bindings: ' + ', '.join(missing))


def ready_subset(state, wave):
    subset = state.get('ready_subsets', {}).get(str(wave['id']))
    if subset is None:
        return None
    ids = subset.get('task_ids', [])
    known = {task['id'] for task in wave['tasks']}
    if subset.get('approved') is not True or not isinstance(ids, list) or not ids \
            or any(not isinstance(identity, str) or identity not in known for identity in ids) \
            or len(set(ids)) != len(ids):
        raise ValueError('Invalid explicitly approved ready subset for wave ' + str(wave['id']))
    return set(ids)


def waiting_for_inputs(state, state_path, wave):
    state.setdefault('waves', {}).setdefault(str(wave['id']), {})['status'] = 'waiting_for_inputs'
    save(state_path, state)
    say('Wave ' + str(wave['id']) + ' successful subset retained; waiting for remaining inputs.')


def apply_input_correction(state, task, original_task=None, verify_proof=True):
    correction = state.get('input_corrections', {}).get(task['id'])
    if not correction:
        return task
    if correction.get('approved') is not True:
        raise ValueError('Input correction has no explicit approval: ' + task['id'])
    correction_manifest(original_task or task, correction, verify_proof=verify_proof)
    for field, actual in [('expected_worker_sha', state.get('worker_sha')),
                          ('expected_worker_ref', state.get('worker_ref'))]:
        if field in correction and correction[field] != actual:
            raise ValueError('Input correction expects a different worker: ' + task['id'])
    result = dict(task)
    if 'source_overlays' in correction:
        if not isinstance(correction['source_overlays'], list):
            raise ValueError('Approved source overlays must be an array')
        result['source_overlays'] = [*task.get('source_overlays', []), *correction['source_overlays']]
    if 'source_archives' in correction:
        if not isinstance(correction['source_archives'], dict):
            raise ValueError('Approved source archive correction must be an object')
        result['source_archives'] = {**task.get('source_archives', {}), **correction['source_archives']}
    if 'supplemental_dependencies' in correction:
        supplemental = correction['supplemental_dependencies']
        if not isinstance(supplemental, list):
            raise ValueError('Approved supplemental dependencies must be an array')
        if not state.get('worker_sha'):
            raise ValueError('Supplemental dependencies require a fixed current worker commit')
        dependencies = list(task.get('dependencies', []))
        bound = {dep.get('id', dep['artifact'].removeprefix('shard-')): dep for dep in dependencies}
        fields = {'id', 'run_id', 'artifact', 'expected_worker_commit', 'expected_manifest_sha256'}
        for dependency in supplemental:
            if not isinstance(dependency, dict) or set(dependency) != fields \
                    or not isinstance(dependency.get('id'), str) \
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', dependency['id']) \
                    or not str(dependency['run_id']).isdigit() or int(dependency['run_id']) <= 0 \
                    or dependency['artifact'] != 'shard-' + dependency['id'] \
                    or not re.fullmatch(r'[a-f0-9]{40}', str(dependency['expected_worker_commit'])) \
                    or not re.fullmatch(r'[a-f0-9]{64}', str(dependency['expected_manifest_sha256'])):
                raise ValueError('Invalid approved supplemental dependency descriptor')
            identity = dependency['id']
            producer = successful_task(state, identity)
            if not producer or str(producer['run_id']) != str(dependency['run_id']) \
                    or producer.get('artifact') != dependency['artifact'] \
                    or producer.get('headSha') != dependency['expected_worker_commit'] \
                    or producer.get('manifest_sha256') != dependency['expected_manifest_sha256']:
                raise ValueError('Supplemental dependency differs from its successful binding: ' + identity)
            previous = bound.get(identity)
            if previous:
                if any(str(previous.get(key)) != str(dependency[key]) for key in fields):
                    raise ValueError('Conflicting supplemental dependency: ' + identity)
                continue
            dependencies.append(dict(dependency))
            bound[identity] = dependency
        result['dependencies'] = dependencies
    if 'graph_bundle_overlay' in correction:
        record = state.get('inputs', {}).get(task['id'], {})
        primary = correction.get('primary_input', {})
        if record.get('graph_bundle_overlay') != correction['graph_bundle_overlay'] \
                or record.get('primary_manifest_sha256') != primary.get('manifest_sha256') \
                or record.get('manifest_sha256') != correction.get('variant_manifest_sha256') \
                or record.get('metadata_sha256') != primary.get('metadata', {}).get('sha256') \
                or record.get('uploaded') is not True \
                or task.get('capsule_assets') != [part['name'] for part in record.get('parts', [])]:
            raise ValueError('Graph bundle overlay has not been prepared against this primary input: ' + task['id'])
        layer = {'assets': [part['name'] for part in correction['graph_bundle_overlay']['parts']]}
        result['source_overlays'] = [*result.get('source_overlays', []), layer]
    return result


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


def checked_source_inputs(state, metadata):
    frozen = state.get('frozen_inputs', {})
    current = {}
    for spec in metadata['files']:
        if 'task-graph' in spec.get('reasons', []):
            continue
        keys = ['type', 'mode']
        if spec['type'] == 'file':
            keys += ['size', 'sha256']
        elif spec['type'] == 'symlink':
            keys += ['target']
        identity = {key: spec[key] for key in keys}
        if spec['path'] in frozen and frozen[spec['path']] != identity:
            raise ValueError('Input differs from an earlier cold capsule: ' + spec['path'])
        current[spec['path']] = identity
    return current


def verified_parts(archive, parts, description):
    if not isinstance(parts, list) or not parts or sum(part['size'] for part in parts) != archive['size']:
        raise ValueError(description + ' has invalid asset parts')
    names = [archive['name']] if len(parts) == 1 else [
        archive['name'] + f'.part-{index:04d}' for index in range(len(parts))]
    if [part['name'] for part in parts] != names or (len(parts) == 1 and parts[0]['sha256'] != archive['sha256']):
        raise ValueError(description + ' has different asset names/identity')
    for part in parts:
        if part.get('uploaded') is not True or part.get('api_size') != part['size'] \
                or part.get('api_digest') != 'sha256:' + part['sha256'] \
                or not re.fullmatch(r'[a-f0-9]{64}', part['sha256']):
            raise ValueError(description + ' asset is not API verified: ' + part['name'])


def graph_bundle_overlay(plan, task, correction, base, primary, variant):
    descriptor = correction['graph_bundle_overlay']
    spec = descriptor['envelope']
    validate_frozen_file(spec)
    envelope = json.loads(Path(spec['path']).read_text())
    runtime = Path(base['slice_path'])
    repo = Path(__file__).resolve().parents[1]
    def local_path(value):
        path = Path(value)
        return (path if path.is_absolute() else repo / path).resolve()
    baseline = envelope['baseline']
    expected = {'archive_name': base['archive']['name'], 'archive_size': base['archive']['size'],
                'archive_sha256': primary['archive_sha256'], 'metadata_sha256': primary['metadata']['sha256'],
                'manifest_sha256': base['manifest_sha256']}
    inline = {key: value for key, value in base.items() if key not in {'archive', 'digest_cache'}}
    inline_bytes = (json.dumps(inline, indent=2, sort_keys=True) + '\n').encode()
    baseline_bundle = hashlib.sha256(inline_bytes).hexdigest()
    if envelope.get('schema_version') != 1 or envelope.get('kind') != 'graph-bundle-overlay' \
            or envelope['source_root'] != plan['source_root'] or envelope['slice_path'] != str(runtime) \
            or any(baseline.get(key) != value for key, value in expected.items()) \
            or local_path(baseline['metadata_path']) != local_path(primary['metadata']['path']) \
            or baseline['bundle_sha256'] != baseline_bundle:
        raise ValueError('Graph overlay baseline differs from the immutable primary input: ' + task['id'])
    final = envelope['final']
    final_path = local_path(final['metadata_path'])
    final_bytes = final_path.read_bytes()
    final_sha = hashlib.sha256(final_bytes).hexdigest()
    metadata = json.loads(final_bytes)
    if final_sha != final['metadata_sha256'] or final_sha != final['bundle_sha256'] \
            or final['manifest_sha256'] != variant or metadata['manifest_sha256'] != variant \
            or metadata.get('schema_version') != 1 or metadata['source_root'] != base['source_root'] \
            or metadata['out_root'] != base['out_root'] or metadata['slice_path'] != str(runtime):
        raise ValueError('Graph overlay final bundle/manifest identity differs: ' + task['id'])
    def split(metadata):
        graph, sources = {}, {}
        for item in metadata['files']:
            path = Path(item['path'])
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('Invalid graph overlay input path: ' + str(path))
            within = path.is_relative_to(runtime)
            if 'task-graph' in item.get('reasons', []) and not within:
                raise ValueError('Task graph input lies outside its runtime graph: ' + str(path))
            group = graph if within else sources
            if item['path'] in group or path == runtime / 'bundle.json':
                raise ValueError('Duplicate or self-referencing graph bundle input: ' + str(path))
            if within and item['type'] != 'file':
                raise ValueError('Graph overlay runtime inputs must be regular files')
            group[item['path']] = item
        return graph, sources
    old_graph, old_sources = split(base)
    new_graph, new_sources = split(metadata)
    source_proof = envelope['source_identity']
    if source_proof.get('all_required_sources_equal_baseline') is not True \
            or source_proof.get('baseline_archive_verified') is not True \
            or source_proof.get('baseline_inline_bundle_verified') is not True \
            or source_proof.get('source_gate_errors') != [] or source_proof.get('allowed_generated_inputs') != [] \
            or type(source_proof.get('baseline_source_specs')) is not int \
            or source_proof['baseline_source_specs'] != len(old_sources) \
            or type(source_proof.get('required_source_specs')) is not int \
            or not 0 <= source_proof['required_source_specs'] <= len(old_sources):
        raise ValueError('Graph overlay source proof did not verify the baseline subset: ' + task['id'])
    if new_sources != old_sources or type(envelope['base_source_specs_preserved']) is not int \
            or envelope['base_source_specs_preserved'] != len(old_sources) \
            or envelope['new_source_spec_count'] != 0 or source_proof['new_source_paths'] \
            or source_proof['changed_source_paths']:
        raise ValueError('Graph overlay changes non-graph source specifications: ' + task['id'])
    if set(old_graph) - set(new_graph):
        raise ValueError('Graph overlay cannot remove existing runtime graph files')
    def graph_identity(item):
        return {key: item[key] for key in ('type', 'mode', 'size', 'sha256')}
    changed = {path for path, item in new_graph.items()
               if path not in old_graph or graph_identity(item) != graph_identity(old_graph[path])}
    bundle_path = str(runtime / 'bundle.json')
    members = {item['path']: item for item in envelope['changed_members']}
    if len(members) != len(envelope['changed_members']) or set(members) != changed | {bundle_path} \
            or new_graph.get(str(runtime / 'manifest.json'), {}).get('sha256') != variant:
        raise ValueError('Graph overlay changed members differ from the final graph specifications')
    for path, member in members.items():
        if not Path(path).is_relative_to(runtime) or member['type'] != 'file':
            raise ValueError('Graph overlay member lies outside its runtime graph')
        if path == bundle_path:
            old_sha, new_sha, size, mode = baseline_bundle, final_sha, len(final_bytes), 0o644
        else:
            item = new_graph[path]
            old_sha = old_graph.get(path, {}).get('sha256')
            new_sha, size, mode = item['sha256'], item['size'], item['mode']
        if member['baseline_sha256'] != old_sha or member['final_sha256'] != new_sha \
                or member['size'] != size or member['mode'] != mode:
            raise ValueError('Graph overlay member identity differs: ' + path)
    verified_parts(envelope['archive'], descriptor['parts'], 'Graph overlay')
    return metadata, {'path': str(final_path), 'sha256': final_sha}


def prepare_primary_variant(plan, task, correction, state, state_path):
    variant, original = correction_manifest(task, correction, verify_proof=True)
    if not original:
        raise ValueError('Primary input requires an approved manifest variant: ' + task['id'])
    for spec in task['preparation']['slice_files']:
        validate_frozen_file(spec)
    primary = correction['primary_input']
    spec = primary['metadata']
    if not Path(spec['path']).is_absolute():
        raise ValueError('Primary input metadata path must be absolute')
    metadata_bytes = Path(spec['path']).read_bytes()
    if hashlib.sha256(metadata_bytes).hexdigest() != spec['sha256']:
        raise ValueError('Approved primary input metadata changed: ' + task['id'])
    metadata = json.loads(metadata_bytes)
    if metadata.get('schema_version') != 1 or metadata['source_root'] != plan['source_root'] \
            or metadata['out_root'] != plan['preparation']['out_root'] \
            or metadata['manifest_sha256'] != primary['manifest_sha256'] \
            or primary['archive_sha256'] != metadata['archive']['sha256']:
        raise ValueError('Approved primary input has a different manifest/source/archive identity: ' + task['id'])
    manifest = json.loads(Path(task['preparation']['manifest']).read_text())
    runtime = Path(plan['source_root']) / manifest.get('runtime_dir', '.crux-task/graph')
    bundled = {item['path']: item for item in metadata['files']}
    if metadata.get('slice_path') != str(runtime) or bundled.get(str(runtime / 'manifest.json'), {}).get('sha256') != primary['manifest_sha256']:
        raise ValueError('Approved primary input omits its variant manifest: ' + task['id'])
    archive = metadata['archive']
    parts = primary['parts']
    verified_parts(archive, parts, 'Approved primary input')
    effective = metadata
    effective_spec = None
    if 'graph_bundle_overlay' in correction:
        effective, effective_spec = graph_bundle_overlay(plan, task, correction, metadata, primary, variant)
    elif primary['manifest_sha256'] != variant:
        raise ValueError('Approved primary input has a different final manifest identity: ' + task['id'])
    current = checked_source_inputs(state, effective)
    record = {'status': 'ready', 'uploaded': True, 'primary_variant': True,
              'archive_sha256': primary['archive_sha256'], 'metadata': spec['path'],
              'metadata_sha256': spec['sha256'], 'manifest_sha256': variant,
              'original_manifest_sha256': original, 'variant_provenance': copy.deepcopy(correction['variant_provenance']),
              'parts': copy.deepcopy(parts)}
    if effective_spec:
        record.update(primary_manifest_sha256=primary['manifest_sha256'], effective_metadata=effective_spec,
                      effective_bundle_sha256=effective_spec['sha256'],
                      graph_bundle_overlay=copy.deepcopy(correction['graph_bundle_overlay']))
    frozen = state.setdefault('frozen_inputs', {})
    changed = any(path not in frozen for path in current)
    frozen.update(current)
    inputs = state.setdefault('inputs', {})
    if inputs.get(task['id']) != record or changed:
        inputs[task['id']] = record
        save(state_path, state)
    return {key: value for key, value in task.items() if key != 'preparation'} | {
        'capsule_assets': [part['name'] for part in parts]}


def prepare_task(plan, task, state, state_path, repo, tag):
    recipe = task.get('preparation')
    if not recipe:
        return task
    correction = state.get('input_corrections', {}).get(task['id'], {})
    if 'primary_input' in correction:
        return prepare_primary_variant(plan, task, correction, state, state_path)
    inputs = state.setdefault('inputs', {})
    existing = inputs.get(task['id'])
    if existing and existing.get('uploaded'):
        if existing.get('primary_variant') or existing.get('manifest_sha256', frozen_manifest(task)) != frozen_manifest(task):
            raise ValueError('Uploaded manifest variant has no approved primary input correction: ' + task['id'])
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
    current_inputs = checked_source_inputs(state, metadata)
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
    state.setdefault('frozen_inputs', {}).update(current_inputs)
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
    if state.get('worker_sha'):
        validate_worker(state, state['worker_sha'], 'Current worker')
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
        selected = ready_subset(state, wave)
        existing = state['runs'].get(wave_id)
        history = [*state.get('failed_runs', {}).get(wave_id, []),
                   *state.get('subset_runs', {}).get(wave_id, [])]
        for prior in [*history, *([existing] if existing else [])]:
            changed = False
            if state.get('worker_sha'):
                if not prior.get('headSha'):
                    prior['headSha'] = gh('run', 'view', prior['run_id'], '--repo', args.repo,
                                          '--json', 'headSha', json_output=True)['headSha']
                    changed = True
                validate_worker(state, prior['headSha'], 'Saved run ' + prior['url'])
            if prior.get('conclusion'):
                changed = record_successful_tasks(state, wave, prior) or changed
            if changed:
                save(state_path, state)
        if existing and existing.get('conclusion') == 'success':
            if not existing.get('ready_subset') or all(successful_task(state, task['id']) for task in wave['tasks']):
                require_wave_producers(state, wave, existing)
                if args.stop_after_wave is not None and wave['id'] >= args.stop_after_wave:
                    return
                continue
        if existing and not existing.get('conclusion'):
            say(f"Resuming wave {wave_id}: {existing['url']}")
            result = wait(args.repo, existing['run_id'])
            existing.update(conclusion=result['conclusion'], jobs=result['jobs'])
            record_successful_tasks(state, wave, existing)
            save(state_path, state)
            if result['conclusion'] != 'success':
                raise RuntimeError(f"Wave {wave_id} failed: {result['url']}")
            if existing.get('ready_subset') and not all(successful_task(state, task['id']) for task in wave['tasks']):
                waiting_for_inputs(state, state_path, wave)
                return
            require_wave_producers(state, wave, existing)
            if args.stop_after_wave is not None and wave['id'] >= args.stop_after_wave:
                return
            continue
        tasks = []
        for task in wave['tasks']:
            if selected is not None and task['id'] not in selected:
                continue
            if successful_task(state, task['id']):
                continue
            dependencies = [dependency_producer(state, dep) for dep in task.get('dependencies', [])]
            prepared = prepare_task(plan, task, state, state_path, args.repo, args.tag)
            correction = state.get('input_corrections', {}).get(task['id'], {})
            prepared = apply_input_correction(state, {**prepared, 'dependencies': dependencies}, task,
                                              verify_proof='primary_input' not in correction)
            tasks.append(prepared)
        if not tasks and wave['tasks']:
            if not all(successful_task(state, task['id']) for task in wave['tasks']):
                waiting_for_inputs(state, state_path, wave)
                return
            state.setdefault('waves', {}).setdefault(wave_id, {})['status'] = 'success'
            save(state_path, state)
            if args.stop_after_wave is not None and wave['id'] >= args.stop_after_wave:
                return
            continue
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
            history_key = 'subset_runs' if existing.get('conclusion') == 'success' else 'failed_runs'
            state.setdefault(history_key, {}).setdefault(wave_id, []).append(existing)
        state['runs'][wave_id] = {'run_id': run['databaseId'], 'url': run['url'], 'headSha': run['headSha'],
                                  'task_ids': [task['id'] for task in tasks]}
        if selected is not None:
            state['runs'][wave_id]['ready_subset'] = copy.deepcopy(state['ready_subsets'][wave_id])
        corrections = state.get('input_corrections', {})
        used = {task['id']: copy.deepcopy(corrections[task['id']]) for task in tasks if task['id'] in corrections}
        if used:
            state['runs'][wave_id]['input_corrections'] = used
        save(state_path, state)
        if run['headSha'] != state['worker_sha']:
            state['runs'][wave_id]['conclusion'] = 'worker_identity_mismatch'
            save(state_path, state)
            raise ValueError('Dispatched run has a different worker commit: ' + run['url'])
        say(f"Wave {wave_id}: {run['url']}")
        result = wait(args.repo, run['databaseId'])
        state['runs'][wave_id].update(conclusion=result['conclusion'], jobs=result['jobs'])
        record_successful_tasks(state, wave, state['runs'][wave_id])
        save(state_path, state)
        if result['conclusion'] != 'success':
            raise RuntimeError(f"Wave {wave_id} failed: {result['url']}")
        if selected is not None and not all(successful_task(state, task['id']) for task in wave['tasks']):
            waiting_for_inputs(state, state_path, wave)
            return
        require_wave_producers(state, wave, state['runs'][wave_id])
        if args.stop_after_wave is not None and wave['id'] >= args.stop_after_wave:
            return
    state['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save(state_path, state)
    say('All planned remote waves completed successfully.')


if __name__ == '__main__':
    main()
