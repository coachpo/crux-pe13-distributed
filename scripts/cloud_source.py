#!/usr/bin/env python3
"""Fetch pinned source projects and package complete source layers for hosted runners."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

import cloud_release_snapshot


SCHEMA_VERSION = 1
MAX_PART_BYTES = 1_900_000_000  # Strictly below the 2 GiB release-asset limit.
CHUNK_BYTES = 1024 * 1024
ARCHIVE_ATTEMPTS = 3
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
LAYER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SourceError(ValueError):
    """Invalid source input or failed materialization."""


def _relative(value, label):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SourceError(f"{label} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() in {"", "."}:
        raise SourceError(f"{label} must stay beneath the source root: {value!r}")
    return path


def _safe_destination(root, relative, create_parent=False):
    """Resolve a lexical source-tree path without traversing an existing symlink."""
    relative = _relative(relative, "destination")
    root = Path(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise SourceError(f"destination parent is a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise SourceError(f"destination parent is not a directory: {current}")
        if create_parent:
            current.mkdir(exist_ok=True)
    result = root.joinpath(*relative.parts)
    if result.is_symlink():
        return result
    return result


def _validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise SourceError("unsupported source manifest schema_version")
    source_root = manifest.get("source_root")
    if (not isinstance(source_root, str) or not source_root.startswith("/")
            or ".." in PurePosixPath(source_root).parts):
        raise SourceError("source_root must be an absolute mount path")
    project_list = manifest.get("projects")
    if not isinstance(project_list, list) or not project_list:
        raise SourceError("projects must be a non-empty list")
    projects, seen = {}, set()
    for item in project_list:
        if not isinstance(item, dict):
            raise SourceError("each project must be an object")
        path = _relative(item.get("path"), "project path").as_posix()
        revision = item.get("revision")
        if not isinstance(revision, str) or not revision.strip():
            raise SourceError(f"project {path}: revision is required")
        archive_revision = item.get("archive_revision")
        if archive_revision is not None and (not isinstance(archive_revision, str) or not archive_revision.strip()):
            raise SourceError(f"project {path}: archive_revision must be a non-empty string")
        patches = item.get("patches", [])
        if not isinstance(patches, list):
            raise SourceError(f"project {path}: patches must be a list")
        for patch in patches:
            if (not isinstance(patch, dict) or not isinstance(patch.get("path"), str)
                    or not patch["path"] or not isinstance(patch.get("sha256"), str)
                    or not re.fullmatch(r"[a-fA-F0-9]{64}", patch["sha256"])):
                raise SourceError(f"project {path}: each patch requires a path and SHA-256")
        expected_tree = item.get("expected_tree")
        if patches and (not isinstance(expected_tree, str)
                        or not re.fullmatch(r"[a-fA-F0-9]{40,64}", expected_tree)):
            raise SourceError(f"project {path}: patches require a full expected_tree object id")
        if expected_tree is not None and (not isinstance(expected_tree, str)
                                          or not re.fullmatch(r"[a-fA-F0-9]{40,64}", expected_tree)):
            raise SourceError(f"project {path}: expected_tree must be a full Git tree object id")
        url = item.get("url")
        archive_url = item.get("archive_url")
        if not isinstance(url, str) and not isinstance(archive_url, str):
            raise SourceError(f"project {path}: url or archive_url is required")
        if archive_url is not None and not isinstance(archive_url, str):
            raise SourceError(f"project {path}: archive_url must be a string")
        if path in seen:
            raise SourceError(f"duplicate project path: {path}")
        seen.add(path)
        project = dict(item)
        project["path"], project["revision"] = path, revision
        archive_type = project.get("archive_type", "tar.gz")
        if path in cloud_release_snapshot.PROJECT_REPOSITORIES and archive_type != cloud_release_snapshot.ARCHIVE_TYPE:
            raise SourceError(f"project {path}: a locked private Release snapshot is required")
        if archive_type not in {"tar.gz", "git-bundle", cloud_release_snapshot.ARCHIVE_TYPE}:
            raise SourceError(f"project {path}: unsupported archive_type")
        project["archive_url"] = resolve_archive_url(project)
        projects[path] = project

    layout = manifest.get("layout", {})
    if isinstance(layout, list):
        normalized = {"copyfiles": [], "linkfiles": []}
        for entry in layout:
            if not isinstance(entry, dict):
                raise SourceError("layout entries must be objects")
            kind = entry.get("type")
            if kind not in {"copy", "link"}:
                raise SourceError(f"unsupported layout entry type: {kind!r}")
            normalized["copyfiles" if kind == "copy" else "linkfiles"].append(entry)
        layout = normalized
    if not isinstance(layout, dict):
        raise SourceError("layout must be an object or a list of typed entries")
    for key in ("copyfiles", "linkfiles"):
        entries = layout.get(key, [])
        if not isinstance(entries, list):
            raise SourceError(f"layout.{key} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise SourceError(f"layout.{key} entries must be objects")
            source = entry.get("source", entry.get("src"))
            destination = entry.get("destination", entry.get("dest"))
            _relative(source, f"layout.{key} source")
            _relative(destination, f"layout.{key} destination")
            if not any(source == project or source.startswith(project + "/")
                       for project in projects):
                raise SourceError(f"layout source is outside all projects: {source}")

    layer_list = manifest.get("layers")
    if not isinstance(layer_list, list) or not layer_list:
        raise SourceError("layers must be a non-empty list")
    layers, layer_ids = {}, set()
    for layer in layer_list:
        if not isinstance(layer, dict):
            raise SourceError("each layer must be an object")
        layer_id = layer.get("id")
        if not isinstance(layer_id, str) or not LAYER_ID_RE.fullmatch(layer_id):
            raise SourceError(f"invalid layer id: {layer_id!r}")
        if layer_id in layer_ids:
            raise SourceError(f"duplicate layer id: {layer_id}")
        layer_ids.add(layer_id)
        assigned = layer.get("projects")
        if not isinstance(assigned, list) or not assigned:
            raise SourceError(f"layer {layer_id}: projects must be a non-empty list")
        paths = []
        for path in assigned:
            normalized = _relative(path, f"layer {layer_id} project").as_posix()
            if normalized not in projects:
                raise SourceError(f"layer {layer_id}: unknown project {normalized}")
            if normalized in paths:
                raise SourceError(f"layer {layer_id}: duplicate project {normalized}")
            paths.append(normalized)
        layers[layer_id] = {"id": layer_id, "projects": paths}
    assignments = [path for layer in layers.values() for path in layer["projects"]]
    if len(assignments) != len(set(assignments)) or set(assignments) != set(projects):
        raise SourceError("layers must assign every project exactly once")
    return source_root, projects, layout, layers


def _sanitized_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _network_error_detail(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code} {error.reason}"
    if isinstance(error, urllib.error.URLError):
        return f"{type(error.reason).__name__}: {error.reason}"
    return f"{type(error).__name__}: {error}"


def _open_archive_url(project, url, timeout):
    for attempt in range(1, ARCHIVE_ATTEMPTS + 1):
        try:
            return urllib.request.urlopen(url, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP_STATUS or attempt == ARCHIVE_ATTEMPTS:
                if error.fp is not None:
                    error.close()
                raise
            delay = 2 ** (attempt - 1)
            print(f"retrying {project['path']} after HTTP {error.code} "
                  f"({attempt + 1}/{ARCHIVE_ATTEMPTS}) in {delay}s", flush=True)
            if error.fp is not None:
                error.close()
            time.sleep(delay)


def resolve_archive_url(project):
    """Return the archive endpoint for the reviewed source provider."""
    if project.get("archive_type") == cloud_release_snapshot.ARCHIVE_TYPE:
        try:
            return cloud_release_snapshot.asset_url(cloud_release_snapshot.validate(project))
        except cloud_release_snapshot.SnapshotError as error:
            raise SourceError(str(error)) from None
    direct = project.get("archive_url")
    if project.get("archive_type") == "git-bundle" and not direct:
        direct = project.get("url")
    if direct:
        parsed = urllib.parse.urlsplit(direct)
        if parsed.scheme != "https" or not parsed.netloc:
            raise SourceError(f"project {project.get('path')}: archive_url must use HTTPS")
        return direct
    url = project.get("url")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SourceError(f"project {project.get('path')}: url must use HTTPS")
    if "+archive/" in parsed.path or "/-/archive/" in parsed.path or parsed.path.endswith((".tar.gz", ".tgz")):
        return url
    revision = urllib.parse.quote(project.get("archive_revision", project["revision"]), safe="")
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if host == "github.com" or host == "www.github.com":
        parts = path.strip("/").split("/")
        if len(parts) != 2:
            raise SourceError(f"project {project.get('path')}: GitHub url must name owner/repository")
        return f"https://codeload.github.com/{parts[0]}/{parts[1]}/tar.gz/{revision}"
    if host.endswith("googlesource.com"):
        return f"https://{parsed.netloc}{path}/+archive/{revision}.tar.gz"
    if host in {"gitlab.com", "www.gitlab.com"}:
        name = path.rsplit("/", 1)[-1]
        return f"https://{parsed.netloc}{path}/-/archive/{revision}/{name}-{revision}.tar.gz"
    raise SourceError(f"project {project.get('path')}: supply archive_url for this host")


def _member_path(name, strip_components):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\x00" in name:
        raise SourceError(f"unsafe archive member path: {name!r}")
    parts = path.parts[strip_components:]
    if not parts:
        return None
    return PurePosixPath(*parts)


def _archive_strip_components(project):
    if "strip_components" in project:
        value = project["strip_components"]
        if not isinstance(value, int) or value < 0:
            raise SourceError(f"project {project['path']}: strip_components must be non-negative")
        return value
    parsed = urllib.parse.urlsplit(project["archive_url"])
    if parsed.hostname in {"codeload.github.com"} or "/-/archive/" in parsed.path:
        return 1
    return 0


def _ensure_project_symlink(member_path, target):
    if not target or "\x00" in target or PurePosixPath(target).is_absolute():
        raise SourceError(f"unsafe absolute/empty archive symlink target: {member_path} -> {target!r}")
    combined = PurePosixPath(member_path).parent.joinpath(PurePosixPath(target))
    depth = 0
    for part in combined.parts:
        if part == "..":
            depth -= 1
        elif part not in {"", "."}:
            depth += 1
        if depth < 0:
            raise SourceError(f"archive symlink escapes source root: {member_path} -> {target}")


def _extract_tar_stream(archive, project, destination, strip_components):
    project_rel = PurePosixPath(project["path"])
    destination.mkdir(parents=True, exist_ok=True)
    directory_modes = {}
    seen = set()
    for member in archive:
        relative = _member_path(member.name, strip_components)
        if relative is None:
            continue
        relative_text = relative.as_posix()
        if relative_text in seen:
            raise SourceError(f"duplicate archive member: {member.name}")
        seen.add(relative_text)
        mode = stat.S_IMODE(member.mode)
        if member.isdir():
            target = _safe_destination(destination, relative_text, create_parent=True)
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise SourceError(f"archive directory conflicts with existing entry: {target}")
            target.mkdir(exist_ok=True)
            directory_modes[relative_text] = mode
        elif member.isfile():
            target = _safe_destination(destination, relative_text, create_parent=True)
            if target.exists() or target.is_symlink():
                raise SourceError(f"duplicate archive destination: {target}")
            source = archive.extractfile(member)
            if source is None:
                raise SourceError(f"archive file has no data: {member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, CHUNK_BYTES)
            target.chmod(mode)
        elif member.issym():
            target = _safe_destination(destination, relative_text, create_parent=True)
            _ensure_project_symlink(project_rel / relative, member.linkname)
            if target.exists() or target.is_symlink():
                raise SourceError(f"duplicate archive destination: {target}")
            target.symlink_to(member.linkname)
        else:
            raise SourceError(f"unsupported archive entry type: {member.name}")
    return directory_modes


def _stream_git_archive(repository, commit, project, destination, env=None):
    process = subprocess.Popen(["git", "--git-dir", str(repository), "archive", "--format=tar", commit],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            directory_modes = _extract_tar_stream(archive, project, destination, 0)
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        result = process.wait()
    except Exception as error:
        if process.poll() is None:
            process.kill()
        process.wait()
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        if isinstance(error, (EOFError, tarfile.TarError)) and process.returncode:
            raise SourceError(f"project {project['path']} git archive failed "
                              f"({process.returncode}): {stderr[-2000:]}") from error
        raise
    finally:
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
    if result:
        raise SourceError(f"project {project['path']} git archive failed ({result}): {stderr[-2000:]}")
    return directory_modes


def _extract_git_bundle(project, destination, timeout):
    url = project["archive_url"]
    try:
        with tempfile.TemporaryDirectory(prefix="cloud-source-bundle-") as temporary:
            bundle_path = Path(temporary) / "project.bundle"
            with _open_archive_url(project, url, timeout) as response, bundle_path.open("wb") as output:
                shutil.copyfileobj(response, output, CHUNK_BYTES)
            repository = Path(temporary) / "repository.git"
            cloned = subprocess.run(["git", "clone", "--bare", str(bundle_path), str(repository)],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if cloned.returncode:
                raise SourceError(f"project {project['path']} git bundle clone failed: {cloned.stderr[-2000:]}")
            resolved = subprocess.run(["git", "--git-dir", str(repository), "rev-parse", "--verify",
                                       project["revision"] + "^{commit}"],
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if resolved.returncode:
                raise SourceError(f"project {project['path']} revision is absent from bundle: {project['revision']}")
            commit = resolved.stdout.strip()
            directory_modes = _stream_git_archive(repository, commit, project, destination)
            if re.fullmatch(r"[a-fA-F0-9]{40,64}", project["revision"]) and commit.lower() != project["revision"].lower():
                raise SourceError(f"project {project['path']} bundle revision resolved to unexpected commit {commit}")
            return {"path": project["path"], "revision": project["revision"],
                    "archive_revision": project.get("archive_revision", project["revision"]),
                    "resolved_commit": commit,
                    "revision_status": ("resolved-and-matched-pin" if re.fullmatch(r"[a-fA-F0-9]{40,64}", project["revision"])
                                        else "resolved-reference"),
                    "archive_url": _sanitized_url(url),
                    "status": "fetched-bundle-and-archived"}, directory_modes
    except SourceError:
        raise
    except (OSError, subprocess.SubprocessError, tarfile.TarError, urllib.error.URLError) as error:
        raise SourceError(f"project {project['path']} git-bundle fetch failed from {_sanitized_url(url)}: "
                          f"{_network_error_detail(error)}") from error


def _extract_release_tar(path, project, destination):
    with tarfile.open(path, mode="r|gz") as archive:
        if archive.pax_headers.get("comment") != project["revision"]:
            raise SourceError(f"project {project['path']}: private git archive commit differs from its pin")
        return _extract_tar_stream(archive, project, destination, 0)


def _extract_release_snapshot(project, destination, timeout, token):
    try:
        with tempfile.TemporaryDirectory(prefix="cloud-source-release-") as temporary:
            archive_path = Path(temporary) / "project.tar.gz"
            lock = cloud_release_snapshot.download(project, archive_path, token, timeout)
            directory_modes = _extract_release_tar(archive_path, project, destination)
    except cloud_release_snapshot.SnapshotError as error:
        raise SourceError(f"project {project['path']}: {error}") from None
    except (OSError, EOFError, tarfile.TarError, zlib.error) as error:
        raise SourceError(f"project {project['path']}: invalid private git archive ({type(error).__name__})") from None
    return {"path": project["path"], "revision": project["revision"],
            "resolved_commit": project["revision"],
            "revision_status": "git-archive-commit-matched-pin",
            "archive_url": project["archive_url"],
            "source_method": cloud_release_snapshot.ARCHIVE_TYPE,
            "release_snapshot": lock, "status": "fetched-and-extracted"}, directory_modes


def _extract_archive(project, destination, timeout, snapshot_token=None):
    if project.get("archive_type") == cloud_release_snapshot.ARCHIVE_TYPE:
        return _extract_release_snapshot(project, destination, timeout, snapshot_token)
    if project.get("archive_type", "tar.gz") == "git-bundle":
        return _extract_git_bundle(project, destination, timeout)
    url = project["archive_url"]
    try:
        response = _open_archive_url(project, url, timeout)
        with response, tarfile.open(fileobj=response, mode="r|gz") as archive:
            strip_components = _archive_strip_components(project)
            directory_modes = _extract_tar_stream(archive, project, destination, strip_components)
    except SourceError:
        raise
    except (OSError, EOFError, tarfile.TarError, urllib.error.URLError, zlib.error, http.client.IncompleteRead) as error:
        project_url = project.get("url", "")
        host = urllib.parse.urlsplit(project_url).hostname or ""
        if host == "googlesource.com" or host.endswith(".googlesource.com"):
            shutil.rmtree(destination)
            return _extract_gitiles_archive(project, destination, error, timeout)
        safe_url = _sanitized_url(url)
        raise SourceError(f"project {project['path']} fetch/extract failed from {safe_url}: "
                          f"{_network_error_detail(error)}") from error
    return {"path": project["path"], "revision": project["revision"],
            "archive_revision": project.get("archive_revision", project["revision"]),
            "revision_status": "pinned-archive-url", "archive_url": _sanitized_url(url),
            "status": "fetched-and-extracted"}, directory_modes


def _extract_gitiles_archive(project, destination, archive_error, timeout):
    """Recover a failed Gitiles archive through a depth-one fetch of its exact pin."""
    source_url = project["url"]
    archive_revision = project.get("archive_revision", project["revision"])
    with tempfile.TemporaryDirectory(prefix="cloud-source-gitiles-") as temporary:
        temporary = Path(temporary)
        repository = temporary / "repository.git"
        template = temporary / "template"
        template.mkdir()
        env = os.environ.copy()
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TEMPLATE_DIR": str(template), "GIT_TERMINAL_PROMPT": "0"})
        try:
            initialized = subprocess.run(["git", "init", "--bare", "--quiet", str(repository)],
                                         text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         env=env, timeout=timeout)
        except subprocess.SubprocessError as error:
            raise SourceError(f"project {project['path']} Gitiles fallback init failed: {error}") from error
        if initialized.returncode:
            raise SourceError(f"project {project['path']} Gitiles fallback init failed: "
                              f"{initialized.stderr[-2000:]}")
        try:
            fetched = subprocess.run(["git", "-c", "http.version=HTTP/1.1", "--git-dir", str(repository),
                                      "fetch", "--depth=1", "--no-tags", source_url, archive_revision],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        except subprocess.SubprocessError as error:
            raise SourceError(f"project {project['path']} Gitiles fallback fetch failed from "
                              f"{_sanitized_url(source_url)} at {archive_revision}: {error}") from error
        if fetched.returncode:
            raise SourceError(f"project {project['path']} Gitiles fallback fetch failed from "
                              f"{_sanitized_url(source_url)} at {archive_revision}: {fetched.stderr[-3000:]}")
        try:
            resolved = subprocess.run(["git", "--git-dir", str(repository), "rev-parse", "--verify",
                                       archive_revision + "^{commit}"], text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout)
        except subprocess.SubprocessError as error:
            raise SourceError(f"project {project['path']} Gitiles fallback revision resolution failed: {error}") from error
        if resolved.returncode:
            raise SourceError(f"project {project['path']} Gitiles fallback could not resolve "
                              f"{archive_revision}: {resolved.stderr[-2000:]}")
        commit = resolved.stdout.strip()
        if re.fullmatch(r"[a-fA-F0-9]{40,64}", archive_revision) and commit.lower() != archive_revision.lower():
            raise SourceError(f"project {project['path']} Gitiles fallback resolved unexpected commit {commit}")
        directory_modes = _stream_git_archive(repository, commit, project, destination, env)
    archive_failure = {"archive_url": _sanitized_url(project["archive_url"]),
                       "type": type(archive_error).__name__,
                       "message": _network_error_detail(archive_error)}
    receipt = {"path": project["path"], "revision": project["revision"],
               "archive_revision": archive_revision, "resolved_commit": commit,
               "revision_status": "fetched-git-and-archived",
               "archive_url": _sanitized_url(project["archive_url"]),
               "source_url": _sanitized_url(source_url),
               "source_method": "git-fetch-archive", "archive_failure": archive_failure,
               "status": "fetched-git-and-archived"}
    return receipt, directory_modes


def _git_tree_id(worktree):
    """Compute Git's tree object for a tree without adding Git metadata to it."""
    with tempfile.TemporaryDirectory(prefix="cloud-source-tree-") as temporary:
        temporary = Path(temporary)
        git_dir = temporary / "git"
        template = temporary / "template"
        template.mkdir()
        env = os.environ.copy()
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TEMPLATE_DIR": str(template)})
        initialized = subprocess.run(["git", "init", "--bare", "--quiet", str(git_dir)],
                                     text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        if initialized.returncode:
            raise SourceError(f"temporary Git index initialization failed: {initialized.stderr[-2000:]}")
        env.update({"GIT_DIR": str(git_dir), "GIT_INDEX_FILE": str(temporary / "index"),
                    "GIT_WORK_TREE": str(Path(worktree).resolve())})
        added = subprocess.run(["git", "-c", "core.filemode=true", "-c", "core.autocrlf=false",
                                "add", "-f", "-A", "--", "."], cwd=worktree, env=env,
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if added.returncode:
            raise SourceError(f"temporary Git index staging failed: {added.stderr[-2000:]}")
        tree = subprocess.run(["git", "write-tree"], cwd=worktree, env=env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if tree.returncode:
            raise SourceError(f"temporary Git tree write failed: {tree.stderr[-2000:]}")
        return tree.stdout.strip()


def _apply_project_patches(project, project_root):
    patch_records = []
    for patch in project.get("patches", []):
        path = Path(patch["path"])
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        try:
            actual_hash = _digest(path)
        except OSError as error:
            raise SourceError(f"project {project['path']} patch is unavailable: {patch['path']}: {error}") from error
        expected_hash = patch["sha256"].lower()
        if actual_hash != expected_hash:
            raise SourceError(f"project {project['path']} patch SHA-256 mismatch for {patch['path']}: "
                              f"expected {expected_hash}, got {actual_hash}")
        applied = subprocess.run(["git", "apply", "--binary", "--", str(path)], cwd=project_root,
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if applied.returncode:
            details = applied.stderr.strip() or applied.stdout.strip()
            raise SourceError(f"project {project['path']} patch failed ({patch['path']}): {details[-3000:]}")
        patch_records.append({"path": patch["path"], "sha256": actual_hash, "status": "applied"})

    expected_tree = project.get("expected_tree")
    reconstructed_tree = _git_tree_id(project_root) if expected_tree is not None else None
    if expected_tree is not None and reconstructed_tree.lower() != expected_tree.lower():
        raise SourceError(f"project {project['path']} reconstructed Git tree mismatch: "
                          f"expected {expected_tree}, got {reconstructed_tree}")
    return patch_records, reconstructed_tree


def _merge_tree(source, destination, relative_root, directory_modes, archive_directory_modes=None):
    source = Path(source)
    archive_directory_modes = archive_directory_modes or {}
    for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
        current = Path(current)
        dirs.sort()
        files.sort()
        for name in list(dirs):
            child = current / name
            rel = child.relative_to(source)
            tree_rel = PurePosixPath(relative_root, *rel.parts).as_posix()
            target = _safe_destination(destination, tree_rel, create_parent=True)
            if child.is_symlink():
                dirs.remove(name)
                _replace_entry(target)
                target.symlink_to(os.readlink(child))
            else:
                if target.is_symlink() or (target.exists() and not target.is_dir()):
                    _replace_entry(target)
                target.mkdir(exist_ok=True)
                directory_modes[tree_rel] = archive_directory_modes.get(
                    rel.as_posix(), stat.S_IMODE(child.lstat().st_mode))
        for name in files:
            child = current / name
            rel = child.relative_to(source)
            tree_rel = PurePosixPath(relative_root, *rel.parts).as_posix()
            target = _safe_destination(destination, tree_rel, create_parent=True)
            _replace_entry(target)
            if child.is_symlink():
                target.symlink_to(os.readlink(child))
            else:
                shutil.copyfile(child, target)
                target.chmod(stat.S_IMODE(child.lstat().st_mode))


def _replace_entry(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _apply_layout(root, layout, kind, included_projects, all_projects):
    for entry in layout.get(kind, []):
        source = entry.get("source", entry.get("src"))
        destination = entry.get("destination", entry.get("dest"))
        owners = [project for project in all_projects
                  if source == project or source.startswith(project + "/")]
        if not owners or max(owners, key=len) not in included_projects:
            continue
        source_path = _safe_destination(root, source)
        if kind == "copyfiles":
            valid_source = not source_path.is_symlink() and source_path.is_file()
        else:
            # Repo layouts include dangling links for inactive platform families.
            # Preserve the link itself; inventing its target changes source discovery.
            valid_source = True
        if not valid_source:
            expected = "regular file" if kind == "copyfiles" else "file, directory, or symlink"
            raise SourceError(f"layout {kind} source is missing or not a {expected}: {source}")
        target = _safe_destination(root, destination, create_parent=True)
        _replace_entry(target)
        if kind == "copyfiles":
            shutil.copyfile(source_path, target)
            target.chmod(stat.S_IMODE(source_path.stat().st_mode))
        else:
            target.symlink_to(os.path.relpath(source_path, target.parent))


def _tree_entries(root):
    entries = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(current)
        dirs.sort()
        files.sort()
        for name in list(dirs):
            path = current / name
            relative = path.relative_to(root).as_posix()
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode):
                dirs.remove(name)
                entries.append({"path": relative, "type": "symlink", "mode": stat.S_IMODE(details.st_mode),
                                "target": os.readlink(path)})
            else:
                entries.append({"path": relative, "type": "directory", "mode": stat.S_IMODE(details.st_mode)})
        for name in files:
            path = current / name
            relative = path.relative_to(root).as_posix()
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode):
                entries.append({"path": relative, "type": "symlink", "mode": stat.S_IMODE(details.st_mode),
                                "target": os.readlink(path)})
            elif stat.S_ISREG(details.st_mode):
                entries.append({"path": relative, "type": "file", "mode": stat.S_IMODE(details.st_mode),
                                "size": details.st_size, "sha256": _digest(path)})
            else:
                raise SourceError(f"unsupported materialized source entry: {path}")
    return sorted(entries, key=lambda item: item["path"])


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_failure(path, layer_id, error, stage, project=None):
    failure = {"schema_version": SCHEMA_VERSION, "layer_id": layer_id,
               "stage": stage, "error": {"type": type(error).__name__, "message": str(error)}}
    if project is not None:
        failure["project"] = {"path": project["path"], "revision": project["revision"],
                              "archive_revision": project.get("archive_revision", project["revision"]),
                              "expected_tree": project.get("expected_tree"),
                              "patches": project.get("patches", []),
                              "archive_url": _sanitized_url(project["archive_url"])}
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(failure, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    except OSError as write_error:
        print(f"cloud_source: could not write failure receipt {path}: {write_error}", file=sys.stderr)


def _canonical_digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def _split_archive(archive, parts_dir):
    archive_size = archive.stat().st_size
    if parts_dir.exists():
        if not parts_dir.is_dir() or parts_dir.is_symlink():
            raise SourceError(f"parts destination is not a directory: {parts_dir}")
        for old in parts_dir.iterdir():
            if old.is_dir() and not old.is_symlink():
                raise SourceError(f"unexpected directory in parts destination: {old}")
            old.unlink()
    else:
        parts_dir.mkdir(parents=True)
    result = []
    with archive.open("rb") as source:
        number = 0
        while True:
            remaining = MAX_PART_BYTES
            part_digest = hashlib.sha256()
            size = 0
            name = f"part-{number:05d}"
            part = parts_dir / name
            with part.open("xb") as output:
                while remaining:
                    data = source.read(min(CHUNK_BYTES, remaining))
                    if not data:
                        break
                    output.write(data)
                    part_digest.update(data)
                    size += len(data)
                    remaining -= len(data)
            if not size:
                part.unlink()
                break
            result.append({"name": name, "size": size, "sha256": part_digest.hexdigest()})
            number += 1
    if sum(part["size"] for part in result) != archive_size:
        raise SourceError("split part sizes do not match SquashFS image size")
    return result


def build(manifest_path, layer_id, output_dir, timeout=300, *, snapshot_token=None):
    if timeout <= 0:
        raise SourceError("timeout must be greater than zero")
    manifest_path = Path(manifest_path)
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as error:
        raise SourceError(f"cannot read source manifest: {error}") from error
    source_root, projects, layout, layers = _validate_manifest(manifest)
    if layer_id not in layers:
        raise SourceError(f"unknown layer id: {layer_id}")
    if (not snapshot_token and any(projects[path].get("archive_type") == cloud_release_snapshot.ARCHIVE_TYPE
                                   for path in layers[layer_id]["projects"])):
        raise SourceError(f"missing {cloud_release_snapshot.TOKEN_ENV} for private snapshot layer {layer_id}")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    parts_dir = output / "layer.sqfs.parts"
    receipt_path = output / "layer.json"
    failure_path = output / "failure.json"
    archive_path = output / "layer.sqfs"
    if parts_dir.exists() and not parts_dir.is_dir():
        raise SourceError(f"parts destination is not a directory: {parts_dir}")
    if failure_path.is_symlink() or failure_path.is_file():
        failure_path.unlink()
    elif failure_path.exists():
        raise SourceError(f"failure receipt path is not a file: {failure_path}")
    if not shutil.which("mksquashfs"):
        error = SourceError("mksquashfs is required to create a source layer")
        _write_failure(failure_path, layer_id, error, "toolchain")
        raise error

    project_receipts, entries = [], None
    with tempfile.TemporaryDirectory(prefix=f"cloud-source-{layer_id}-", dir=output) as temporary:
        temporary = Path(temporary)
        overlay, staging = temporary / "root", temporary / "projects"
        overlay.mkdir()
        staging.mkdir()
        directory_modes = {}
        assigned = sorted(layers[layer_id]["projects"], key=lambda path: (path.count("/"), path))
        for index, project_path in enumerate(assigned, 1):
            project = projects[project_path]
            destination = staging / ("project-" + project_path.replace("/", "_"))
            destination.mkdir()
            print(f"layer {layer_id}: fetching project {index}/{len(assigned)} {project_path} "
                  f"at {project['revision']} from {_sanitized_url(project['archive_url'])}", flush=True)
            try:
                status, archive_directory_modes = _extract_archive(project, destination, timeout, snapshot_token)
                patch_records, reconstructed_tree = _apply_project_patches(project, destination)
                if project.get("patches"):
                    status["patches"] = patch_records
                    status["expected_tree"] = project["expected_tree"]
                    status["reconstructed_tree"] = reconstructed_tree
                    status["revision_status"] = "patched-tree-matched-local-revision"
                elif reconstructed_tree is not None:
                    status["expected_tree"] = project["expected_tree"]
                    status["reconstructed_tree"] = reconstructed_tree
                _merge_tree(destination, overlay, project_path, directory_modes, archive_directory_modes)
                project_receipts.append(status)
                print(f"layer {layer_id}: completed project {project_path} ({status['status']})", flush=True)
            except Exception as error:
                _write_failure(failure_path, layer_id, error, "source", project)
                raise
            finally:
                shutil.rmtree(destination)
        try:
            _apply_layout(overlay, layout, "copyfiles", assigned, projects)
            _apply_layout(overlay, layout, "linkfiles", assigned, projects)
            for relative, mode in sorted(directory_modes.items(), key=lambda item: item[0].count("/"), reverse=True):
                path = overlay / relative
                if path.is_dir() and not path.is_symlink():
                    path.chmod(mode)
            entries = _tree_entries(overlay)
            content_digest = _canonical_digest(entries)
            temp_archive = temporary / "layer.sqfs"
            completed = subprocess.run(["mksquashfs", str(overlay), str(temp_archive), "-noappend", "-all-root"],
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if completed.returncode:
                details = completed.stderr.strip() or completed.stdout.strip()
                raise SourceError(f"mksquashfs failed ({completed.returncode}): {details[-4000:]}")
            os.replace(temp_archive, archive_path)
        except Exception as error:
            _write_failure(failure_path, layer_id, error, "layout-or-package")
            raise
    try:
        parts = _split_archive(archive_path, parts_dir)
        archive_path.unlink()
    except Exception as error:
        _write_failure(failure_path, layer_id, error, "split")
        if isinstance(error, OSError):
            raise SourceError(f"could not split SquashFS image: {error}") from error
        raise
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_root": source_root,
        "source_scope": manifest.get("source_scope"),
        "layer_id": layer_id,
        "projects": project_receipts,
        "content": {"entry_count": len(entries), "sha256": content_digest, "entries": entries},
        "squashfs": {"compression": "mksquashfs-default", "part_limit_bytes": MAX_PART_BYTES,
                     "parts_directory": parts_dir.name, "parts": parts,
                     "size": sum(part["size"] for part in parts)},
    }
    temporary_receipt = receipt_path.with_suffix(".json.tmp")
    temporary_receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    os.replace(temporary_receipt, receipt_path)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build one pinned source layer")
    build_parser.add_argument("--manifest", required=True)
    build_parser.add_argument("--layer", required=True)
    build_parser.add_argument("--output", required=True)
    build_parser.add_argument("--timeout", type=int, default=300,
                              help="per-source archive request timeout in seconds")
    args = parser.parse_args(argv)
    try:
        # Remove the scoped credential before any git or squashfs subprocess.
        snapshot_token = os.environ.pop(cloud_release_snapshot.TOKEN_ENV, None)
        receipt = build(args.manifest, args.layer, args.output, args.timeout,
                        snapshot_token=snapshot_token)
    except (SourceError, OSError) as error:
        print(f"cloud_source: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"layer": receipt["layer_id"], "entries": receipt["content"]["entry_count"],
                      "content_sha256": receipt["content"]["sha256"],
                      "parts": receipt["squashfs"]["parts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
