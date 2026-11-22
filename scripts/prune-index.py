#!/usr/bin/env python3
"""Derive a closure-equivalent graph index that fits GitHub release assets.

The frozen `index.sqlite` of the 2026101104 release graph holds 1,749,432 edges
(8.1 GB of evaluated Ninja command text) because `graph.py index` records the
whole generated graph. Only the closure of the dispatch targets is ever read by
`graph.py shard`, so the hosted preparation input keeps exactly those edges and
every metadata row they need.

The pruned database remains a normal index: `graph.py closure` on it must return
the same edge set, and `graph.py shard` must reproduce the frozen slice plan.
This script records both the identity of its input and its output so a reviewer
can repeat the derivation. It never edits the source database.

MacOS `/home` is a firmlink and recorded paths are verbatim absolute paths, so
nothing here resolves symlinks or normalizes recorded paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph  # noqa: E402  (the maintained index reader)


TABLES = ("meta", "files", "statements", "side_output_dirs", "edges", "outputs")
# `edges.id` and `outputs.edge` identify the retained closure; every other table
# is small (statements 0.10 GB, outputs 0.36 GB) or per-file metadata. A fresh
# index carries no `side_output_dirs`; ownership augmentation adds it, so the
# copy list is derived from the source schema instead of assumed.


class PruneError(ValueError):
    pass


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def schema(database):
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
    finally:
        connection.close()
    tables = [name for kind, name, _ in rows if kind == "table" and not name.startswith("sqlite_")]
    if not tables or "edges" not in tables or "outputs" not in tables:
        raise PruneError("Source database is not a graph index: " + str(database))
    return tables, [(name, sql) for _, name, sql in rows]


def create_output(database, definitions):
    if Path(database).exists():
        raise PruneError("Output database already exists: " + str(database))
    connection = sqlite3.connect(database)
    try:
        for name, statement in definitions:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def batches(values, size=20000):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def project_metadata(current, edge_ids):
    """Keep producer-bound contracts meaningful in the retained edge domain."""
    keep = set(edge_ids)
    ownership = current.meta.get('ownership_profile')
    runtime = current.meta.get('runtime_adaptations')
    paths = set()
    if ownership:
        for key in ('side_output_dirs', 'group_export_dirs', 'not_applicable_side_effects'):
            paths.update(item['producer'] for item in ownership.get(key, []))
        paths.update(path for group in ownership.get('groups', []) for path in group)
    if runtime:
        paths.update(item['producer'] for item in runtime.get('command_adaptations', []))
    owners = current.producers(paths)
    updates = {}

    def retain_group(members):
        members = set(members)
        selected = members & keep
        if selected and selected != members:
            raise PruneError('Closure cuts an atomic action group: ' + str(sorted(members)))
        return bool(selected)

    if 'atomic_groups' in current.meta:
        updates['atomic_groups'] = [group for group in current.meta['atomic_groups'] if retain_group(group)]
    if ownership:
        projected = dict(ownership)
        for key in ('side_output_dirs', 'group_export_dirs', 'not_applicable_side_effects'):
            if key in ownership:
                projected[key] = [item for item in ownership[key] if owners.get(item['producer']) in keep]
        if 'groups' in ownership:
            projected['groups'] = [group for group in ownership['groups']
                                   if retain_group(owners[path] for path in group)]
        updates['ownership_profile'] = projected
    if runtime:
        # A rule adaptation's producer is a witness for its lexical rule, not
        # its only consumer. Re-anchor that witness if another consumer survives.
        rule_witnesses = {}
        for batch in graph.chunks(edge_ids):
            marks = ','.join('?' for _ in batch)
            rows = current.db.execute(
                'SELECT statements.ref,edges.id,edges.outputs FROM statements '
                'JOIN edges ON edges.id=statements.id WHERE edges.id IN (' + marks + ') ORDER BY edges.id', batch)
            for row in rows:
                rule_witnesses.setdefault(row['ref'], json.loads(row['outputs'])[0])
        adaptations = []
        for item in runtime.get('command_adaptations', []):
            owner = owners[item['producer']]
            if owner in keep:
                adaptations.append(item)
            elif item.get('scope') != 'edge':
                rule = current.db.execute('SELECT ref FROM statements WHERE id=?', (owner,)).fetchone()['ref']
                if rule in rule_witnesses:
                    adaptations.append({**item, 'producer': rule_witnesses[rule]})
        updates['runtime_adaptations'] = {**runtime, 'command_adaptations': adaptations}
    return updates


def prune(source, closure_json, output, record, targets):
    if not Path(source).is_file():
        raise PruneError("Missing source index: " + str(source))
    source_stat = Path(source).stat()
    tables, definitions = schema(source)
    current = graph.Graph(source)
    try:
        expected = current.closure(targets)
        edge_ids = expected['edge_ids']
        if closure_json:
            payload = json.loads(Path(closure_json).read_text())
            if payload.get('edge_count') != len(payload['edge_ids']):
                raise PruneError('Closure record is internally inconsistent')
            if payload['edge_ids'] != edge_ids:
                raise PruneError('Closure record differs from the source graph closure')
        metadata = project_metadata(current, edge_ids)
    finally:
        current.db.close()
    if not edge_ids:
        raise PruneError("Closure is empty; refusing to write an empty index")

    create_output(output, definitions)
    target_db = sqlite3.connect(output)
    try:
        # `main` is the pruned output; the frozen index is attached read only so
        # the source database cannot be written even by accident.
        target_db.execute("ATTACH DATABASE ? AS src", ("file:" + str(source) + "?mode=ro",))
        for table in tables:
            if table in ("edges", "outputs", "side_output_dirs"):
                continue
            target_db.execute(f"INSERT INTO main.{table} SELECT * FROM src.{table}")
        target_db.execute("CREATE TEMP TABLE keep(id INTEGER PRIMARY KEY)")
        for batch in batches(edge_ids):
            target_db.executemany("INSERT INTO keep VALUES (?)", [(value,) for value in batch])
        retained_tables = [('edges', 'id'), ('outputs', 'edge')]
        if 'side_output_dirs' in tables:
            retained_tables.append(('side_output_dirs', 'edge'))
        for table, column in retained_tables:
            target_db.execute(
                f"INSERT INTO main.{table} SELECT * FROM src.{table} "
                f"WHERE {column} IN (SELECT id FROM keep)")
        target_db.executemany('UPDATE meta SET value=? WHERE key=?',
                              [(json.dumps(value), key) for key, value in metadata.items()])
        target_db.commit()
        counts = {table: target_db.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0]
                  for table in tables}
        source_counts = {
            table: target_db.execute(f"SELECT count(*) FROM src.{table}").fetchone()[0]
            for table in tables}
    finally:
        target_db.close()

    # The pruned database must answer the same closure for the same targets.
    reduced = graph.Graph(output)
    try:
        check = reduced.closure(targets)
    finally:
        reduced.db.close()
    if check != expected:
        raise PruneError("Pruned index closure differs from the recorded closure")
    for table in tables:
        if table in ("edges", "outputs", "side_output_dirs"):
            continue
        if counts[table] != source_counts[table]:
            raise PruneError("Pruned index lost rows in " + table)
    if counts["edges"] != len(edge_ids):
        raise PruneError("Pruned index edge count differs from the closure")

    record_data = {
        "schema_version": 1,
        "kind": "closure-pruned-graph-index",
        "targets": sorted(targets),
        "source": {"path": str(Path(source).resolve()), "bytes": source_stat.st_size,
                   "sha256": digest(source)},
        "output": {"path": str(Path(output).resolve()), "bytes": Path(output).stat().st_size,
                   "sha256": digest(output)},
        "closure_edge_count": len(edge_ids),
        "source_row_counts": source_counts,
        "output_row_counts": counts,
        "graph_script_sha256": digest(Path(__file__).resolve().parent / "graph.py"),
        "created_at_UTC": datetime.now(timezone.utc).isoformat(),
    }
    Path(record).write_text(json.dumps(record_data, indent=2, sort_keys=True) + "\n")
    return record_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Frozen source index (read only)")
    parser.add_argument("--target", action="append", required=True,
                        help="Dispatch target; the union of their closures is retained")
    parser.add_argument("--output", required=True)
    parser.add_argument("--record", required=True)
    parser.add_argument("--closure", help="Existing `graph.py closure` JSON to reuse")
    args = parser.parse_args()
    try:
        record = prune(args.database, args.closure, args.output, args.record, args.target)
    except (PruneError, OSError, sqlite3.Error, KeyError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"output": record["output"], "closure_edge_count": record["closure_edge_count"],
                      "source_row_counts": record["source_row_counts"],
                      "output_row_counts": record["output_row_counts"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
