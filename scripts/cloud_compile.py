#!/usr/bin/env python3
"""Run one admitted fresh Ninja shard from its accepted cold-input capsule."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import cloud_artifacts

worker_spec = importlib.util.spec_from_file_location("crux_worker", SCRIPT_DIR / "worker.py")
worker = importlib.util.module_from_spec(worker_spec)
worker_spec.loader.exec_module(worker)

CONTEXT_IDENTITY_FIELDS = (
    "execution_id", "worker_commit", "repository", "build_number", "source_root", "out_root",
    "pack_run", "shard_run", "graph_run", "index_run", "source_run",
    "source_manifest_sha256", "plan_sha256", "graph_index",
)
FRESH_BUILD_FIELDS = ("execution_id", "pack_run", "shard_run", "plan_sha256",
                      "source_manifest_sha256")
RUN_ID_FIELDS = ("execution_id", "pack_run", "shard_run", "graph_run", "index_run", "source_run")
SHORT_KERNEL_SOURCE_REVISION = "5e4950d2619f2e2119765b6b48c5b3be2efcaa9e"
SHORT_KERNEL_SOURCE_PROJECT = "kernel/xiaomi/crux-pe-cepheus"
SHORT_KERNEL_SOURCE_ALIAS = Path("/k")
SHORT_KERNEL_SOURCE_POLICY = {
    "project": SHORT_KERNEL_SOURCE_PROJECT,
    "revision": SHORT_KERNEL_SOURCE_REVISION,
    "alias": str(SHORT_KERNEL_SOURCE_ALIAS),
    "validated_probe_run": "41801581346",
}


class CompileError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _absolute(value, name):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise CompileError(name + " must be an absolute path")
    return Path(os.path.abspath(value))


def validate_context(context: dict, shard_id: str, environment=None,
                     check_runner=True) -> tuple[dict, dict]:
    if not isinstance(context, dict) or context.get("schema_version") != 1:
        raise CompileError("Unsupported fresh-build context schema")
    required = set(CONTEXT_IDENTITY_FIELDS) | {"waves", "tasks"}
    missing = sorted(required - set(context))
    if missing:
        raise CompileError("Fresh-build context is missing: " + ", ".join(missing))
    for key in RUN_ID_FIELDS:
        if not str(context[key]).isdecimal():
            raise CompileError("Fresh-build context has an invalid " + key)
    if not re.fullmatch(r"[a-f0-9]{40}", str(context["worker_commit"])):
        raise CompileError("Fresh-build context has an invalid worker_commit")
    if not isinstance(context["repository"], str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", context["repository"]):
        raise CompileError("Fresh-build context has an invalid repository")
    if not isinstance(context["build_number"], str) or not context["build_number"].strip():
        raise CompileError("Fresh-build context has no build_number")
    for key in ("source_manifest_sha256", "plan_sha256"):
        if not isinstance(context[key], str) or not re.fullmatch(r"[a-fA-F0-9]{64}", context[key]):
            raise CompileError("Fresh-build context has an invalid " + key)
    source_root = _absolute(context["source_root"], "source_root")
    out_root = _absolute(context["out_root"], "out_root")
    if source_root == out_root or source_root in out_root.parents or out_root in source_root.parents:
        raise CompileError("source_root and out_root must be separate")
    graph_index = context["graph_index"]
    if (not isinstance(graph_index, dict) or type(graph_index.get("bytes")) is not int
            or graph_index["bytes"] <= 0 or not isinstance(graph_index.get("sha256"), str)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", graph_index["sha256"])):
        raise CompileError("Fresh-build context has an invalid graph_index identity")

    tasks, waves = context["tasks"], context["waves"]
    if not isinstance(tasks, dict) or not isinstance(waves, list) or not waves:
        raise CompileError("Fresh-build context tasks/waves are invalid")
    wave_by_id = {}
    for wave_number, identifiers in enumerate(waves):
        if not isinstance(identifiers, list):
            raise CompileError("Fresh-build waves must be arrays of shard IDs")
        for identity in identifiers:
            if not isinstance(identity, str) or identity in wave_by_id:
                raise CompileError("Fresh-build waves have invalid or duplicate shard IDs")
            wave_by_id[identity] = wave_number
    if set(tasks) != set(wave_by_id):
        raise CompileError("Fresh-build task IDs differ from its wave inventory")
    for identity, task in tasks.items():
        if not isinstance(task, dict) or task.get("id") != identity or task.get("wave") != wave_by_id[identity]:
            raise CompileError("Fresh-build task record differs from its wave: " + identity)
        if (not isinstance(task.get("depends_on"), list)
                or any(dependency not in tasks for dependency in task["depends_on"])):
            raise CompileError("Fresh-build task has an invalid dependency list: " + identity)
        if any(wave_by_id[dependency] >= wave_by_id[identity] for dependency in task["depends_on"]):
            raise CompileError("Fresh-build dependency is not from an earlier wave: " + identity)
        if not isinstance(task.get("manifest_sha256"), str) or not re.fullmatch(
                r"[a-fA-F0-9]{64}", task["manifest_sha256"]):
            raise CompileError("Fresh-build task has an invalid manifest SHA: " + identity)
        if (not isinstance(task.get("archive"), str) or Path(task["archive"]).name != task["archive"]
                or not task["archive"].endswith(".tar.zst")):
            raise CompileError("Fresh-build task has an invalid archive name: " + identity)
        if type(task.get("archive_bytes")) is not int or task["archive_bytes"] <= 0:
            raise CompileError("Fresh-build task has an invalid archive size: " + identity)
        if not isinstance(task.get("archive_sha256"), str) or not re.fullmatch(
                r"[a-fA-F0-9]{64}", task["archive_sha256"]):
            raise CompileError("Fresh-build task has an invalid archive SHA: " + identity)
        extra_exports = task.get("extra_exports", [])
        if not isinstance(extra_exports, list) or any(not isinstance(path, str) for path in extra_exports):
            raise CompileError("Fresh-build task has invalid extra_exports: " + identity)
        if len(extra_exports) != len(set(extra_exports)):
            raise CompileError("Fresh-build task has duplicate extra_exports: " + identity)
        for path in extra_exports:
            _validate_out_path(path, out_root, "extra export")
        local_runtime_targets = task.get("local_runtime_targets", [])
        if not isinstance(local_runtime_targets, list) or any(
                not isinstance(path, str) for path in local_runtime_targets):
            raise CompileError("Fresh-build task has invalid local_runtime_targets: " + identity)
        if len(local_runtime_targets) != len(set(local_runtime_targets)):
            raise CompileError("Fresh-build task has duplicate local_runtime_targets: " + identity)
        for path in local_runtime_targets:
            _validate_out_path(path, out_root, "local runtime target")
        notice_sources = task.get("required_notice_sources", [])
        if not isinstance(notice_sources, list) or any(
                not isinstance(path, str) for path in notice_sources):
            raise CompileError("Fresh-build task has invalid required_notice_sources: " + identity)
        if len(notice_sources) != len(set(notice_sources)):
            raise CompileError("Fresh-build task has duplicate required_notice_sources: " + identity)
        for path in notice_sources:
            _validate_source_path(path, source_root, out_root, "required NOTICE source")
        runtime_inputs = task.get("runtime_inputs", [])
        if not isinstance(runtime_inputs, list):
            raise CompileError("Fresh-build task has invalid runtime_inputs: " + identity)
        seen_runtime_paths = set()
        for runtime in runtime_inputs:
            if not isinstance(runtime, dict) or set(runtime) != {"path", "producer"}:
                raise CompileError("Fresh-build task has invalid runtime input record: " + identity)
            path, producer = runtime["path"], runtime["producer"]
            _validate_out_path(path, out_root, "runtime input")
            producer_exports = tasks.get(producer, {}).get("extra_exports", []) if isinstance(producer, str) else []
            if (path in seen_runtime_paths or not isinstance(producer, str) or producer not in tasks
                    or producer not in task["depends_on"]
                    or not isinstance(producer_exports, list) or path not in producer_exports):
                raise CompileError("Fresh-build runtime input has an invalid producer binding: " + identity)
            seen_runtime_paths.add(path)
    if shard_id not in tasks:
        raise CompileError("Selected shard is absent from fresh-build context: " + shard_id)

    environment = os.environ if environment is None else environment
    expected_runner = {"execution_id": "GITHUB_RUN_ID", "worker_commit": "GITHUB_SHA",
                       "repository": "GITHUB_REPOSITORY"}
    if check_runner:
        for field, variable in expected_runner.items():
            if environment.get(variable) != str(context[field]):
                raise CompileError(f"Fresh-build context {field} differs from this Actions run")
    identity = {field: context[field] for field in FRESH_BUILD_FIELDS}
    return context, identity


def _validate_out_path(value, out_root: Path, label: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise CompileError(label + " path must be absolute")
    candidate = Path(os.path.abspath(value))
    if str(candidate) != value or candidate == out_root or out_root not in candidate.parents:
        raise CompileError(label + " path must be a normalized path inside OUT_DIR")
    return candidate


def _validate_source_path(value, source_root: Path, out_root: Path, label: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise CompileError(label + " path must be absolute")
    candidate = Path(os.path.abspath(value))
    if (str(candidate) != value or source_root not in candidate.parents
            or candidate == out_root or out_root in candidate.parents):
        raise CompileError(label + " path must be a normalized path inside source_root and outside OUT_DIR")
    return candidate


def _locate_capsule(directory: Path, archive_name: str) -> Path:
    matches = [path for path in directory.rglob(archive_name) if path.is_file() and not path.is_symlink()]
    if len(matches) != 1:
        raise CompileError(f"Expected one capsule archive {archive_name}, found {len(matches)}")
    return matches[0]


def _validate_restored_capsule(source_root: Path, out_root: Path, task: dict) -> tuple[Path, Path]:
    slice_dir = source_root / ".crux-task/graph"
    manifest_path = slice_dir / "manifest.json"
    bundle_path = slice_dir / "bundle.json"
    if manifest_path.is_symlink() or bundle_path.is_symlink() or not manifest_path.is_file() or not bundle_path.is_file():
        raise CompileError("Restored capsule does not contain its graph manifest and bundle")
    if sha256(manifest_path) != task["manifest_sha256"]:
        raise CompileError("Restored capsule manifest differs from the accepted slice identity")
    manifest = worker.read_json(manifest_path)
    if manifest.get("source_root") != str(source_root):
        raise CompileError("Restored shard manifest has a different source_root")
    bundle = worker.read_json(bundle_path)
    if (bundle.get("source_root") != str(source_root) or bundle.get("out_root") != str(out_root)
            or bundle.get("manifest_sha256") != task["manifest_sha256"]):
        raise CompileError("Restored capsule bundle differs from the fresh-build context")
    if bundle.get("allowed_generated_inputs") != []:
        raise CompileError("Cold capsule contains allowed_generated_inputs")
    for item in bundle.get("files", []):
        path = Path(item["path"])
        if not path.is_absolute():
            raise CompileError("Capsule bundle contains a non-absolute file path")
        absolute = Path(os.path.abspath(path))
        if absolute == out_root or out_root in absolute.parents:
            raise CompileError("Cold capsule contains an OUT_DIR file: " + str(absolute))
    return manifest_path, bundle_path


def _verify_required_notice_sources(task: dict, source_root: Path, out_root: Path):
    source_directory, output_directory = source_root.resolve(), out_root.resolve()
    for value in task.get("required_notice_sources", []):
        path = Path(value)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise CompileError("Required NOTICE source is not a restored file: " + value) from error
        # Source-relative license links are valid only with their bundled target;
        # never satisfy a missing capsule input from an OUT or old host file.
        if (source_directory not in resolved.parents
                or output_directory == resolved or output_directory in resolved.parents
                or not resolved.is_file()):
            raise CompileError("Required NOTICE source is not a restored source file: " + value)


def _read_fresh_dependency(dependency_id: str, dependency_dir: Path,
                           context: dict, fresh_identity: dict) -> dict:
    receipt_path = dependency_dir / "receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise CompileError("Fresh dependency has no worker receipt: " + dependency_id)
    receipt = worker.read_json(receipt_path)
    task = context["tasks"][dependency_id]
    expected = {"id": dependency_id, "run_id": str(context["execution_id"]),
                "worker_commit": context["worker_commit"],
                "manifest_sha256": task["manifest_sha256"],
                "source_root": context["source_root"], "out_root": context["out_root"],
                "status": "success", "fresh_build": fresh_identity}
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise CompileError(f"Dependency {dependency_id} receipt {key} differs from this fresh build")
    return receipt


def _verify_runtime_inputs(task: dict, dependency_receipts: dict,
                           dependency_directories: dict) -> list[dict]:
    admitted = []
    producer_metadata, producer_receipt_shas = {}, {}
    for runtime in task.get("runtime_inputs", []):
        producer = runtime["producer"]
        receipt = dependency_receipts.get(producer)
        if receipt is None:
            raise CompileError("Runtime input producer was not imported: " + producer)
        path = runtime["path"]
        if producer not in producer_metadata:
            additional_exports = receipt.get("additional_exports", [])
            output_specs = receipt.get("outputs", [])
            if not isinstance(additional_exports, list) or not isinstance(output_specs, list):
                raise CompileError("Runtime input producer receipt has invalid output metadata: " + producer)
            exports = {value for value in additional_exports if isinstance(value, str)}
            output_index = {}
            for spec in output_specs:
                if isinstance(spec, dict) and isinstance(spec.get("path"), str):
                    output_index.setdefault(spec["path"], []).append(spec)
            producer_metadata[producer] = exports, output_index
        additional_exports, output_index = producer_metadata[producer]
        if path not in additional_exports:
            raise CompileError("Runtime input is not declared as an additional producer export: " + path)
        matches = output_index.get(path, [])
        if len(matches) != 1:
            raise CompileError("Runtime input has no unique producer receipt output: " + path)
        spec = matches[0]
        if (spec.get("type") != "file" or type(spec.get("size")) is not int or spec["size"] <= 0
                or not isinstance(spec.get("sha256"), str)
                or not re.fullmatch(r"[a-fA-F0-9]{64}", spec["sha256"])):
            raise CompileError("Runtime input producer output is not a nonempty regular file: " + path)
        if producer not in producer_receipt_shas:
            receipt_path = dependency_directories[producer] / "receipt.json"
            producer_receipt_shas[producer] = sha256(receipt_path)
        admitted.append({**runtime, "producer_receipt_sha256": producer_receipt_shas[producer],
                         "size": spec["size"], "sha256": spec["sha256"]})
    return admitted


def _validate_diagnostic_parent(current: dict, parent: dict, shard_id: str,
                                environment: dict) -> tuple[dict, dict]:
    parent, parent_identity = validate_context(parent, shard_id, environment, check_runner=False)
    compare_fields = ("repository", "build_number", "source_root", "out_root",
                      "pack_run", "shard_run", "graph_run", "index_run", "source_run",
                      "source_manifest_sha256", "plan_sha256", "graph_index", "waves", "tasks")
    for field in compare_fields:
        if parent.get(field) != current.get(field):
            raise CompileError("Diagnostic parent differs from current lineage at " + field)
    if parent["execution_id"] == current["execution_id"]:
        raise CompileError("Diagnostic parent must be a distinct prior execution")
    return parent, parent_identity


def _diagnostic_process_limits(environment: dict) -> dict:
    def getconf(name):
        result = subprocess.run(["getconf", name], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
    values = [os.fsencode(key) + b"=" + os.fsencode(value) + b"\0"
              for key, value in environment.items()]
    return {
        "argv_bytes": sum(len(os.fsencode(arg)) + 1 for arg in sys.argv),
        "argv_max_string_bytes": max((len(os.fsencode(arg)) + 1 for arg in sys.argv), default=0),
        "environment_bytes": sum(map(len, values)),
        "environment_max_string_bytes": max((len(value) for value in values), default=0),
        "arg_max": getconf("ARG_MAX"),
        "page_size": getconf("PAGESIZE"),
        "stack_limit": subprocess.run(["bash", "-lc", "ulimit -s"], capture_output=True,
                                       text=True).stdout.strip(),
    }


def _validate_short_kernel_source(context: dict, shard_id: str, source_root: Path) -> Path:
    kernel_output = str(Path(context["out_root"]) / "target/product/crux/obj/KERNEL_OBJ")
    if context.get("assembly_exports", {}).get(kernel_output) != shard_id:
        raise CompileError("Short-source mode is restricted to the task owning the kernel output")
    projects = context.get("source_selection", {}).get("projects", [])
    pins = {project.get("path"): project.get("revision") for project in projects
            if isinstance(project, dict)}
    if pins.get(SHORT_KERNEL_SOURCE_PROJECT) != SHORT_KERNEL_SOURCE_REVISION:
        raise CompileError("Short-source mode requires the pinned kernel source revision")
    kernel_source = source_root / SHORT_KERNEL_SOURCE_PROJECT
    try:
        kernel_source.relative_to(source_root)
    except ValueError as error:
        raise CompileError("Pinned kernel source escapes source_root") from error
    if kernel_source.is_symlink() or not kernel_source.is_dir():
        raise CompileError("Pinned kernel source must be a regular directory in the restored capsule")
    return kernel_source


def _mount_short_source_alias(kernel_source: Path) -> dict:
    alias = SHORT_KERNEL_SOURCE_ALIAS
    if os.path.lexists(alias):
        raise CompileError("Short-source alias already exists: " + str(alias))
    subprocess.run(["sudo", "mkdir", "--", str(alias)], check=True)
    try:
        subprocess.run(["sudo", "mount", "--bind", str(kernel_source), str(alias)], check=True)
    except Exception:
        subprocess.run(["sudo", "rmdir", "--", str(alias)], check=False)
        raise
    return {"source": str(kernel_source), "alias": str(alias), "mount": "bind"}


def _unmount_short_source_alias():
    alias = SHORT_KERNEL_SOURCE_ALIAS
    subprocess.run(["sudo", "umount", "--", str(alias)], check=True)
    subprocess.run(["sudo", "rmdir", "--", str(alias)], check=True)


def compile_shard(context_path: Path, shard_id: str, output_dir: Path,
                  staging: Path, environment=None, diagnostic_context_path=None,
                  short_kernel_source=False) -> tuple[int, dict]:
    environment = dict(os.environ if environment is None else environment)
    context = worker.read_json(context_path)
    context, fresh_identity = validate_context(context, shard_id, environment)
    diagnostic_parent = diagnostic_identity = None
    if diagnostic_context_path is not None:
        diagnostic_parent, diagnostic_identity = _validate_diagnostic_parent(
            context, worker.read_json(diagnostic_context_path), shard_id, environment)
    kernel_output = str(Path(context["out_root"]) / "target/product/crux/obj/KERNEL_OBJ")
    owns_kernel_output = context.get("assembly_exports", {}).get(kernel_output) == shard_id
    alias_policy = context.get("kernel_source_alias")
    if alias_policy is not None and alias_policy != SHORT_KERNEL_SOURCE_POLICY:
        raise CompileError("Context kernel_source_alias policy is not the validated probe policy")
    if short_kernel_source and diagnostic_parent is None and alias_policy is None:
        raise CompileError("Production short-source mode requires the validated context policy")
    if short_kernel_source and not owns_kernel_output:
        raise CompileError("Short-source mode is restricted to the task owning the kernel output")
    alias_enabled = bool((alias_policy is not None and owns_kernel_output)
                         or (short_kernel_source and diagnostic_parent is not None))
    task = context["tasks"][shard_id]
    source_root = Path(context["source_root"])
    out_root = Path(context["out_root"])
    output_dir = Path(output_dir).resolve()
    staging = Path(staging).resolve()
    if (output_dir == staging or output_dir in staging.parents or staging in output_dir.parents):
        raise CompileError("Worker output and staging directories must be separate")
    if output_dir == source_root or source_root in output_dir.parents or output_dir == out_root or out_root in output_dir.parents:
        raise CompileError("Worker output directory must be outside source_root and out_root")
    if staging == source_root or source_root in staging.parents or staging == out_root or out_root in staging.parents:
        raise CompileError("Worker staging directory must be outside source_root and out_root")
    output_dir.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    receipt_path = output_dir / "receipt.json"
    if receipt_path.exists():
        raise CompileError("Worker receipt already exists; refusing to overwrite")
    receipt = {"schema_version": 1, "id": shard_id, "status": "failed",
               "fresh_build": fresh_identity}
    if diagnostic_parent is not None:
        receipt["diagnostic_only"] = True
        receipt["diagnostic_parent"] = {
            "execution_id": diagnostic_parent["execution_id"],
            "worker_commit": diagnostic_parent["worker_commit"],
            "fresh_build": diagnostic_identity,
        }
    diagnostic_process_limits = None
    source_alias_mounted = False
    runtime_source_alias = None
    admitted_runtime_inputs = []
    admitted_local_runtime_targets = task.get("local_runtime_targets", [])
    started = time.monotonic()
    try:
        if os.path.lexists(source_root):
            raise CompileError("source_root must be cold and nonexistent before capsule restore: " + str(source_root))
        if os.path.lexists(out_root):
            raise CompileError("out_root must be cold and nonexistent before capsule restore: " + str(out_root))
        if any(staging.iterdir()):
            raise CompileError("staging directory is not empty: " + str(staging))
        token = environment.get("GH_TOKEN") or environment.get("GITHUB_TOKEN")
        if not token:
            raise CompileError("GH_TOKEN or GITHUB_TOKEN is required for artifact restore")

        capsule_dir = staging / "capsule"
        capsule_dir.mkdir()
        artifact_name = "cloud-capsule-" + shard_id
        downloaded = cloud_artifacts.download_artifacts(
            str(context["pack_run"]), context["repository"], capsule_dir,
            token=token, name=artifact_name)
        if len(downloaded) != 1 or downloaded[0]["name"] != artifact_name:
            raise CompileError("Pack run did not return the exact admitted capsule artifact")
        archive = _locate_capsule(Path(downloaded[0]["destination"]), task["archive"])
        if archive.stat().st_size != task["archive_bytes"] or sha256(archive) != task["archive_sha256"]:
            raise CompileError("Capsule archive bytes/SHA differ from the accepted pack registry")
        subprocess.run(["sudo", "tar", "--zstd", "--no-same-owner", "-xpf", str(archive), "-C", "/"],
                       check=True)
        subprocess.run(["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(source_root)],
                       check=True)
        archive.unlink()
        shutil.rmtree(capsule_dir)
        manifest_path, bundle_path = _validate_restored_capsule(source_root, out_root, task)
        _verify_required_notice_sources(task, source_root, out_root)
        if alias_enabled:
            kernel_source = _validate_short_kernel_source(context, shard_id, source_root)
            alias_record = _mount_short_source_alias(kernel_source)
            source_alias_mounted = True
            runtime_source_alias = {
                **alias_record, "source_revision": SHORT_KERNEL_SOURCE_REVISION,
                "makeflags_addition": "srctree=/k",
                "validation_policy": alias_policy,
            }
            receipt["runtime_source_alias"] = runtime_source_alias

        dependency_dirs, bindings = [], []
        dependency_receipts, dependency_directories = {}, {}
        dependency_context = diagnostic_parent or context
        dependency_identity = diagnostic_identity or fresh_identity
        for dependency_id in task["depends_on"]:
            dependency_dir = staging / "dependencies" / dependency_id
            dependency_dir.mkdir(parents=True, exist_ok=False)
            dependency_artifact = "shard-" + dependency_id
            downloaded = cloud_artifacts.download_artifacts(
                str(dependency_context["execution_id"]), dependency_context["repository"], dependency_dir,
                token=token, name=dependency_artifact)
            if len(downloaded) != 1 or downloaded[0]["name"] != dependency_artifact:
                raise CompileError("Current build run did not return dependency artifact: " + dependency_id)
            dependency_directory = Path(downloaded[0]["destination"])
            dependency_receipt = _read_fresh_dependency(dependency_id, dependency_directory,
                                                        dependency_context, dependency_identity)
            dependency_receipts[dependency_id] = dependency_receipt
            dependency_directories[dependency_id] = dependency_directory
            dependency_dirs.append(dependency_directory)
            bindings.append({"id": dependency_id, "run_id": str(dependency_context["execution_id"]),
                             "artifact": dependency_artifact,
                             "expected_worker_commit": dependency_context["worker_commit"],
                             "expected_manifest_sha256": dependency_context["tasks"][dependency_id]["manifest_sha256"]})

        admitted_runtime_inputs = _verify_runtime_inputs(task, dependency_receipts, dependency_directories)

        # Keep artifact credentials in this wrapper only; worker.run_shard passes
        # its process environment to Ninja and native build tools.
        auth_names = ("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN",
                      "ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        saved_auth = {name: os.environ[name] for name in auth_names if name in os.environ}
        for name in auth_names:
            os.environ.pop(name, None)
        previous_makeflags = os.environ.get("MAKEFLAGS", "")
        makeflags_modified = diagnostic_parent is not None or alias_enabled
        if makeflags_modified:
            flags = previous_makeflags.split()
            if diagnostic_parent is not None and "--trace" not in flags:
                flags.append("--trace")
            if alias_enabled and "srctree=/k" not in flags:
                flags.append("srctree=/k")
            os.environ["MAKEFLAGS"] = " ".join(flags)
            if alias_enabled:
                runtime_source_alias["makeflags"] = os.environ["MAKEFLAGS"]
                receipt["runtime_source_alias"] = runtime_source_alias
        if diagnostic_parent is not None:
            diagnostic_process_limits = _diagnostic_process_limits(os.environ)
            receipt["diagnostic_process_limits"] = diagnostic_process_limits
        try:
            result = worker.run_shard(manifest_path, bundle_path, shard_id, output_dir,
                                      dependency_dirs, jobs=4, dependency_bindings=bindings,
                                      extra_exports=task.get("extra_exports", []),
                                      prebuild_targets=admitted_local_runtime_targets)
        finally:
            if makeflags_modified:
                if previous_makeflags:
                    os.environ["MAKEFLAGS"] = previous_makeflags
                else:
                    os.environ.pop("MAKEFLAGS", None)
            os.environ.update(saved_auth)
        receipt = result if isinstance(result, dict) else worker.read_json(receipt_path)
        if (receipt.get("id") != shard_id or receipt.get("run_id") != str(context["execution_id"])
                or receipt.get("worker_commit") != context["worker_commit"]
                or receipt.get("manifest_sha256") != task["manifest_sha256"]
                or receipt.get("source_root") != context["source_root"]
                or receipt.get("out_root") != context["out_root"]):
            raise CompileError("Native worker receipt differs from fresh-build shard identity")
        if (result.get("status") == "success"
                and sorted(receipt.get("additional_exports", [])) != sorted(task.get("extra_exports", []))):
            raise CompileError("Native worker receipt differs from admitted extra_exports")
        if (result.get("status") == "success"
                and receipt.get("prebuild_targets") != admitted_local_runtime_targets):
            raise CompileError("Native worker receipt differs from admitted local_runtime_targets")
        receipt["fresh_build"] = fresh_identity
        receipt["admitted_runtime_inputs"] = admitted_runtime_inputs
        receipt["admitted_local_runtime_targets"] = admitted_local_runtime_targets
        if diagnostic_parent is not None:
            receipt["diagnostic_only"] = True
            receipt["diagnostic_parent"] = {
                "execution_id": diagnostic_parent["execution_id"],
                "worker_commit": diagnostic_parent["worker_commit"],
                "fresh_build": diagnostic_identity,
            }
            receipt["diagnostic_process_limits"] = diagnostic_process_limits
        if alias_enabled:
            runtime_source_alias["makeflags"] = " ".join(flags)
            receipt["runtime_source_alias"] = runtime_source_alias
        receipt["status"] = result.get("status", "failed")
        if receipt["status"] != "success" and "error" not in receipt:
            receipt["error"] = "native shard compilation failed"
    except Exception as error:
        if receipt_path.is_file():
            try:
                receipt = worker.read_json(receipt_path)
            except Exception:
                pass
        receipt.update({"schema_version": 1, "id": shard_id, "status": "failed",
                       "fresh_build": fresh_identity, "error": str(error)})
        if diagnostic_parent is not None:
            receipt.update({"diagnostic_only": True, "diagnostic_parent": {
                "execution_id": diagnostic_parent["execution_id"],
                "worker_commit": diagnostic_parent["worker_commit"],
                "fresh_build": diagnostic_identity,
            }})
            if diagnostic_process_limits is not None:
                receipt["diagnostic_process_limits"] = diagnostic_process_limits
        if runtime_source_alias is not None:
            receipt["runtime_source_alias"] = runtime_source_alias
        receipt["admitted_runtime_inputs"] = admitted_runtime_inputs
        receipt["admitted_local_runtime_targets"] = admitted_local_runtime_targets
    finally:
        if source_alias_mounted:
            try:
                _unmount_short_source_alias()
            except Exception as error:
                receipt.update({"status": "failed", "error": "Could not clean short-source mount: " + str(error)})
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return (0 if receipt.get("status") == "success" else 1), receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--staging", required=True)
    parser.add_argument("--dependency-context")
    parser.add_argument("--short-kernel-source", action="store_true")
    args = parser.parse_args(argv)
    try:
        code, receipt = compile_shard(Path(args.context), args.id, Path(args.output),
                                      Path(args.staging), diagnostic_context_path=args.dependency_context,
                                      short_kernel_source=args.short_kernel_source)
    except (CompileError, OSError, ValueError, json.JSONDecodeError) as error:
        print("cloud_compile: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps({"id": args.id, "status": receipt["status"],
                      "execution_id": receipt["fresh_build"]["execution_id"],
                      "elapsed_seconds": receipt["elapsed_seconds"]}, sort_keys=True))
    if code and receipt.get("error"):
        print("cloud_compile: " + receipt["error"], file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
