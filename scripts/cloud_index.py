#!/usr/bin/env python3
"""Validate a hosted Soong/Kati graph artifact and build its fresh SQLite index."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import sqlite3
import sys
import time


TARGETS = ("bootimage", "recoveryimage", "systemimage", "vendorimage")
GRAPH_FILES = (
    "soong/build.ninja",
    "build-aosp_crux.ninja",
    "build-aosp_crux-package.ninja",
    "combined-aosp_crux.ninja",
)
SCRIPT_DIR = Path(__file__).resolve().parent
GRAPH_PARSER = SCRIPT_DIR / "graph.py"
spec = importlib.util.spec_from_file_location("crux_graph_parser", GRAPH_PARSER)
graph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(graph)


class IndexError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_graph_inputs(graph_evidence: Path, manifest_path: Path,
                      build_number: str) -> tuple[Path, str, str]:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("source_root"), str):
        raise IndexError("Source manifest has no source_root")
    source_root = manifest["source_root"]
    if not source_root.startswith("/"):
        raise IndexError("Source manifest source_root must be absolute")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    receipt_path = graph_evidence / "graph/graph-receipt.json"
    if not receipt_path.is_file():
        raise IndexError("Missing hosted graph receipt: " + str(receipt_path))
    receipt = json.loads(receipt_path.read_text())
    if not isinstance(receipt, dict) or receipt.get("status") != "success":
        raise IndexError("Hosted graph receipt is not successful")
    if receipt.get("build_number") != build_number:
        raise IndexError("Hosted graph build number differs from the requested build")
    manifest_receipt = receipt.get("source_manifest")
    if not isinstance(manifest_receipt, dict) or manifest_receipt.get("sha256") != manifest_sha:
        raise IndexError("Hosted graph was generated from a different source manifest")
    if receipt.get("source_root") != source_root:
        raise IndexError("Hosted graph source_root differs from the current source manifest")
    out_value = receipt.get("out_root")
    if not isinstance(out_value, str) or not Path(out_value).is_absolute():
        raise IndexError("Hosted graph receipt has an invalid OUT_DIR")
    out_root = Path(out_value)
    if not out_root.is_dir():
        raise IndexError("Restored hosted graph OUT_DIR is missing: " + str(out_root))
    listed = receipt.get("graph_files")
    if not isinstance(listed, list):
        raise IndexError("Hosted graph receipt has no graph file inventory")
    by_relative = {}
    for item in listed:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise IndexError("Hosted graph receipt contains an invalid file record")
        path = Path(item["path"])
        try:
            relative = path.relative_to(out_root).as_posix()
        except ValueError:
            raise IndexError("Graph receipt file is outside OUT_DIR: " + str(path)) from None
        if relative in by_relative:
            raise IndexError("Duplicate graph file record: " + relative)
        by_relative[relative] = item
    for relative in GRAPH_FILES:
        item = by_relative.get(relative)
        if item is None:
            raise IndexError("Hosted graph receipt is missing " + relative)
        path = out_root / PurePosixPath(relative)
        if path.is_symlink() or not path.is_file():
            raise IndexError("Restored graph file is missing or unsafe: " + str(path))
        if not isinstance(item.get("size"), int) or path.stat().st_size != item["size"]:
            raise IndexError("Restored graph file size differs from its receipt: " + relative)
        expected_hash = item.get("sha256")
        if not isinstance(expected_hash, str) or sha256(path) != expected_hash:
            raise IndexError("Restored graph file SHA-256 differs from its receipt: " + relative)
    return out_root, source_root, manifest_sha


def database_counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        counts = {}
        for table in ("files", "statements", "edges", "outputs"):
            counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return counts


def run_index(graph_evidence: Path, manifest_path: Path, build_number: str,
              graph_run: str, output: Path) -> tuple[int, dict]:
    output.mkdir(parents=True, exist_ok=True)
    receipt_path = output / "index-receipt.json"
    result_path = output / "index-result.json"
    database = output / "index.sqlite"
    if receipt_path.exists() or result_path.exists():
        raise IndexError("Index receipt/result already exists; refusing to overwrite")
    receipt = {"schema_version": 1, "status": "running", "graph_run": graph_run,
               "build_number": build_number, "output": str(output.resolve()),
               "parser_sha256": sha256(GRAPH_PARSER), "database": database.name}
    start = time.monotonic()
    try:
        out_root, source_root, manifest_sha = read_graph_inputs(
            graph_evidence, manifest_path, build_number)
        receipt.update({"manifest_sha256": manifest_sha, "source_root": source_root,
                        "graph_receipt_sha256": sha256(graph_evidence / "graph/graph-receipt.json")})
        stale_database_files = [database, Path(str(database) + "-wal"),
                                Path(str(database) + "-shm"), Path(str(database) + "-journal")]
        stale = next((path for path in stale_database_files if path.exists()), None)
        if stale is not None:
            raise IndexError("Stale SQLite index exists; refusing to reuse it: " + str(stale))
        combined = out_root / "combined-aosp_crux.ninja"
        result = graph.index_graph(combined, source_root, database)
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        counts = database_counts(database)
        receipt.update({"database_bytes": database.stat().st_size,
                        "database_sha256": sha256(database), "row_counts": counts})
        if not all(counts[name] > 0 for name in ("files", "statements", "edges", "outputs")):
            raise IndexError("Fresh SQLite index has empty required tables: " + json.dumps(counts, sort_keys=True))
        indexed = graph.Graph(database)
        try:
            target_producers = indexed.producers(TARGETS, directory_fallback=False)
        finally:
            indexed.db.close()
        receipt["target_producers"] = target_producers
        missing = sorted(set(TARGETS) - set(target_producers))
        if missing:
            raise IndexError("Fresh SQLite index has no producer for targets: " + ", ".join(missing))
        receipt.update({"status": "success", "elapsed_seconds": round(time.monotonic() - start, 3)})
        return 0, receipt
    except Exception as error:
        receipt.update({"status": "failed", "error": str(error),
                        "elapsed_seconds": round(time.monotonic() - start, 3)})
        if database.is_file():
            receipt["partial_database_bytes"] = database.stat().st_size
            receipt["partial_database_sha256"] = sha256(database)
        return 1, receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-evidence", required=True)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--build-number", required=True)
    parser.add_argument("--graph-run", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if not args.build_number or any(char.isspace() for char in args.build_number):
        parser.error("--build-number must be non-empty and contain no whitespace")
    if not args.graph_run.isdecimal():
        parser.error("--graph-run must be a numeric Actions run ID")

    output = Path(args.output).resolve()
    try:
        code, receipt = run_index(Path(args.graph_evidence), Path(args.source_manifest),
                                  args.build_number, args.graph_run, output)
        receipt_path = output / "index-receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except (IndexError, OSError, json.JSONDecodeError) as error:
        print("cloud_index: " + str(error), file=sys.stderr)
        return 1
    if code:
        print("cloud_index: " + receipt.get("error", "indexing failed"), file=sys.stderr)
        return code
    print(json.dumps({"status": "success", "graph_run": args.graph_run,
                      "database_bytes": receipt["database_bytes"],
                      "row_counts": receipt["row_counts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
