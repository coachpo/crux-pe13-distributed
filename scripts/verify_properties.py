#!/usr/bin/env python3
"""Verify admitted frontend identity against real buildinfo and image properties."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


SPARSE_MAGIC = b"\x3a\xff\x26\xed"
NATIVE_PARTITIONS = ("system", "system_ext", "system_dlkm", "product", "vendor", "odm", "vendor_dlkm", "odm_dlkm")
PE_BRANDING = {"org.pixelexperience.build_date": "20261008-0448",
               "org.pixelexperience.build_date_utc": "1791434880"}
PROVENANCE_LIMIT = ("Checks admitted frontend values and actual property bytes. Historical default-versus-override "
                    "selection and reproduction of the whole frontend environment are not established.")


class PropertyError(ValueError):
    pass


class MissingInspectionTool(PropertyError):
    pass


def read_regular(path):
    path = Path(path)
    if not stat.S_ISREG(path.stat().st_mode):
        raise PropertyError("Inspection requires a regular file: " + str(path))
    return path.read_bytes()


def property_source(label, data, partition=None):
    """Keep every assignment, including repeated keys and their source lines."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PropertyError("Properties are not UTF-8: " + label) from error
    assignments, other = [], []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator:
            other.append({"line": number, "text": line})
            continue
        assignments.append({"key": key.strip(), "value": value.strip(), "line": number, "source": label})
    return {"source": label, "partition": partition, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "assignments": assignments,
            "non_assignment_lines": other}


def expected_from_proposal(proposal):
    """Local adapter; production can use the compact schema directly."""
    validation = proposal["final_actual_property_validation"]
    return {"schema_version": 1, "required_buildinfo": validation["required_buildinfo"],
            "required_final_keys": validation["required_final_keys"], "branding_keys": dict(PE_BRANDING),
            "frontend_thumbprint": proposal["native_rematerialization"]["scalar_literals"]["build_thumbprint"]}


def validate_expected(expected):
    if not isinstance(expected, dict) or expected.get("schema_version") != 1:
        raise PropertyError("Unsupported expected identity schema")
    for field in ("required_buildinfo", "required_final_keys", "branding_keys"):
        values = expected.get(field)
        if not isinstance(values, dict) or not values or any(
                not isinstance(key, str) or not key or not isinstance(value, str) or not value.strip()
                for key, value in values.items()):
            raise PropertyError("Expected nonempty identity strings in " + field)
    thumbprint = expected.get("frontend_thumbprint")
    if not isinstance(thumbprint, str) or not thumbprint.strip():
        raise PropertyError("Expected exact frontend_thumbprint text bytes")
    buildinfo, final = expected["required_buildinfo"], expected["required_final_keys"]
    agreed = {}
    for field in ("required_buildinfo", "required_final_keys", "branding_keys"):
        for key, value in expected[field].items():
            if key in agreed and agreed[key] != value:
                raise PropertyError("Conflicting admitted values for " + key)
            agreed[key] = value
    required = ((buildinfo, "ro.build.version.incremental"), (buildinfo, "ro.build.date.utc"),
                (buildinfo, "ro.build.date"), (final, "ro.build.fingerprint"))
    if any(key not in mapping for mapping, key in required):
        raise PropertyError("Expected identity lacks native partition incremental/fingerprint/date values")
    # Partitions whose common build properties the frozen build rules deliberately skip
    # (for example /product on a device without a product partition) are declared here so the
    # namespace is required to be absent instead of required to be present.
    absent = expected.get("absent_partition_namespaces", {})
    if not isinstance(absent, dict) or any(
            partition not in NATIVE_PARTITIONS or not isinstance(declaration, dict)
            or not isinstance(declaration.get("reason"), str) or not declaration["reason"].strip()
            for partition, declaration in absent.items()):
        raise PropertyError("Expected a reasoned declared-absent namespace for a native partition")
    return expected


def check_values(sources, expected, errors, scope, required=True):
    for key, target in expected.items():
        observed = [entry for source in sources for entry in source["assignments"] if entry["key"] == key]
        if not observed:
            if required:
                errors.append({"scope": scope, "key": key, "reason": "missing required property", "expected": target})
            continue
        values = {entry["value"] for entry in observed}
        if len(values) > 1:
            errors.append({"scope": scope, "key": key, "reason": "contradictory required duplicate",
                           "values": sorted(values), "assignments": observed})
        for entry in observed:
            if not entry["value"]:
                errors.append({"scope": scope, "key": key, "reason": "blank required identity", "assignment": entry})
            elif entry["value"] != target:
                errors.append({"scope": scope, "key": key, "reason": "identity differs from admitted frontend",
                               "expected": target, "assignment": entry})


def validate_property_bytes(expected, buildinfo_data, members, frontend_thumbprint_data):
    """Validate bytes; members are (logical partition, member label, data) tuples."""
    validate_expected(expected)
    buildinfo = property_source("buildinfo", buildinfo_data)
    final = [property_source(label, data, partition) for partition, label, data in members]
    errors = []
    check_values([buildinfo], expected["required_buildinfo"], errors, "buildinfo")
    final_identity = {**expected["required_buildinfo"], **expected["required_final_keys"], **expected["branding_keys"]}
    check_values(final, final_identity, errors, "final properties")
    thumbprint = expected["frontend_thumbprint"]
    if frontend_thumbprint_data != thumbprint.encode("utf-8"):
        errors.append({"scope": "frontend thumbprint", "reason": "text bytes differ from admitted frontend",
                       "expected_utf8": thumbprint, "actual_hex": frontend_thumbprint_data.hex()})
    # This native property is emitted only by the OEM thumbprint branch.
    thumb = {"ro.build.thumbprint": thumbprint.rstrip("\n")}
    check_values([buildinfo], thumb, errors, "buildinfo optional thumbprint", required=False)
    check_values(final, thumb, errors, "final optional thumbprint", required=False)
    native_values = {"version.incremental": expected["required_buildinfo"]["ro.build.version.incremental"],
                     "fingerprint": expected["required_final_keys"]["ro.build.fingerprint"],
                     "date.utc": expected["required_buildinfo"]["ro.build.date.utc"],
                     "date": expected["required_buildinfo"]["ro.build.date"]}
    # A supplied native property member establishes that partition's contract;
    # namespaces observed elsewhere are checked as well. Absent optional
    # partitions do not become new requirements.
    member_partitions = {source["partition"] for source in final if source["partition"] in NATIVE_PARTITIONS}
    present = set(member_partitions)
    for source in final:
        for entry in source["assignments"]:
            match = re.match(r"^ro\.([^.]*)\.build\.", entry["key"])
            if match and match[1] in NATIVE_PARTITIONS:
                present.add(match[1])
    for partition in sorted(present):
        identity = {"ro." + partition + ".build." + key: value for key, value in native_values.items()}
        check_values(final, identity, errors, "partition " + partition + " observations", required=False)
        if partition in member_partitions:
            own_members = [source for source in final if source["partition"] == partition]
            if partition in expected.get("absent_partition_namespaces", {}):
                for key in identity:
                    observed = [entry for source in own_members for entry in source["assignments"]
                                if entry["key"] == key]
                    for entry in observed:
                        errors.append({"scope": "partition " + partition + " declared-absent namespace",
                                       "key": key, "reason": "declared-absent namespace property is present",
                                       "assignment": entry})
            else:
                check_values(own_members, identity, errors, "partition " + partition + " native members")
    return {"schema_version": 1, "status": "success" if not errors else "failed", "errors": errors,
            "buildinfo": buildinfo, "property_members": final, "native_partitions_checked": sorted(present),
            "frontend_thumbprint": {"size": len(frontend_thumbprint_data),
                                     "sha256": hashlib.sha256(frontend_thumbprint_data).hexdigest()},
            "provenance_qualification": PROVENANCE_LIMIT}


def resolve_tool(value, option):
    if not value:
        raise MissingInspectionTool("Explicit " + option + " tool is required")
    result = shutil.which(str(value))
    if not result:
        raise MissingInspectionTool("Inspection tool is unavailable: " + option + " " + str(value))
    return result


def debugfs_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def member_candidates(partition):
    candidates = [(partition, "/build.prop"), (partition, "/etc/build.prop"),
                  (partition, "/" + partition + "/build.prop"),
                  (partition, "/" + partition + "/etc/build.prop")]
    if partition == "system":
        for optional in NATIVE_PARTITIONS:
            if optional == "system":
                continue
            for prefix in ("/" + optional, "/system/" + optional):
                candidates.extend([(optional, prefix + "/build.prop"), (optional, prefix + "/etc/build.prop")])
    elif partition == "vendor":
        for optional in ("odm", "vendor_dlkm", "odm_dlkm"):
            for prefix in ("/" + optional, "/vendor/" + optional):
                candidates.extend([(optional, prefix + "/build.prop"), (optional, prefix + "/etc/build.prop")])
    return list(dict.fromkeys(candidates))


def extract_image_members(images, temporary, debugfs, simg2img=None):
    """Use native file-image inspection tools, never writable device access."""
    debugfs = resolve_tool(debugfs, "--debugfs")
    extracted, inspections = [], []
    for partition, value in images.items():
        if partition not in NATIVE_PARTITIONS:
            raise PropertyError("Unsupported native property partition: " + partition)
        image = Path(value)
        if not stat.S_ISREG(image.stat().st_mode):
            raise PropertyError("Image inspection requires a regular file: " + str(image))
        with image.open("rb") as stream:
            sparse = stream.read(4) == SPARSE_MAGIC
        raw = image
        inspection = {"partition": partition, "image": str(image), "image_size": image.stat().st_size,
                      "format": "android-sparse" if sparse else "raw", "members": [], "absent_or_nonregular": []}
        if sparse:
            converter = resolve_tool(simg2img, "--simg2img")
            raw = Path(temporary) / (partition + "-raw.img")
            converted = subprocess.run([converter, str(image), str(raw)], capture_output=True, text=True)
            if converted.returncode != 0 or not raw.is_file():
                raise PropertyError("simg2img failed for " + str(image) + ": " + converted.stderr.strip())
            inspection["simg2img"] = converter
        for index, (logical, member) in enumerate(member_candidates(partition)):
            inspected = subprocess.run([debugfs, "-R", "stat " + debugfs_quote(member), str(raw)],
                                       capture_output=True, text=True)
            # debugfs can return zero for a missing path or a damaged image.
            if inspected.returncode != 0 or not re.search(r"\bType:\s+regular\b", inspected.stdout):
                inspection["absent_or_nonregular"].append({"member": member, "diagnostic": inspected.stderr.strip()})
                continue
            destination = Path(temporary) / (partition + "-member-" + str(index) + ".prop")
            request = "dump -p " + debugfs_quote(member) + " " + debugfs_quote(destination)
            dumped = subprocess.run([debugfs, "-R", request, str(raw)], capture_output=True, text=True)
            if dumped.returncode != 0 or not destination.is_file():
                raise PropertyError("debugfs did not extract property bytes: " + str(image) + ":" + member
                                    + ": " + dumped.stderr.strip())
            data = read_regular(destination)
            extracted.append((logical, str(image) + ":" + member, data))
            inspection["members"].append({"partition": logical, "member": member, "size": len(data),
                                           "sha256": hashlib.sha256(data).hexdigest()})
        if not any(member["partition"] == partition for member in inspection["members"]):
            raise PropertyError("No regular native property file extracted from " + str(image))
        inspections.append(inspection)
    return extracted, inspections


def verify_images(expected, buildinfo_path, thumbprint_path, images, debugfs, simg2img=None):
    validate_expected(expected)
    if "system" not in images or "vendor" not in images:
        raise PropertyError("Actual final system and vendor image files are required")
    buildinfo = read_regular(buildinfo_path)
    thumbprint = read_regular(thumbprint_path)
    with tempfile.TemporaryDirectory(prefix="crux-property-inspection-") as temporary:
        members, inspections = extract_image_members(images, temporary, debugfs, simg2img)
        report = validate_property_bytes(expected, buildinfo, members, thumbprint)
    report["image_inspection"] = inspections
    report["buildinfo"]["source"] = str(buildinfo_path)
    report["frontend_thumbprint"]["source"] = str(thumbprint_path)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--expected", help="Compact admitted identity JSON (schema 1)")
    inputs.add_argument("--proposal", help="Local supersession-proposal adapter")
    parser.add_argument("--buildinfo", required=True, help="Actual verified native buildinfo.prop file")
    parser.add_argument("--frontend-thumbprint", required=True, help="Actual verified frontend thumbprint text")
    parser.add_argument("--image", action="append", required=True, metavar="PARTITION=FILE")
    parser.add_argument("--debugfs", required=True, help="Explicit standard e2fsprogs inspection executable")
    parser.add_argument("--simg2img", help="Explicit standard libsparse converter for sparse images")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output)
    try:
        path = args.expected or args.proposal
        config = json.loads(read_regular(path))
        expected = config if args.expected else expected_from_proposal(config)
        validate_expected(expected)
        images = {}
        for value in args.image:
            partition, separator, filename = value.partition("=")
            if not separator or not filename or partition in images:
                raise PropertyError("Each image must be one unique PARTITION=FILE: " + value)
            images[partition] = filename
        report = verify_images(expected, args.buildinfo, args.frontend_thumbprint, images,
                               args.debugfs, args.simg2img)
        report["expected_identity"] = {"source": str(path), "sha256": hashlib.sha256(read_regular(path)).hexdigest(),
                                       "mode": "compact" if args.expected else "proposal adapter"}
        code = 0 if report["status"] == "success" else 1
    except (PropertyError, OSError, KeyError, json.JSONDecodeError) as error:
        blocked = isinstance(error, MissingInspectionTool)
        report = {"schema_version": 1, "status": "blocked" if blocked else "failed",
                  "errors": [{"reason": str(error)}], "provenance_qualification": PROVENANCE_LIMIT}
        code = 2 if blocked else 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "error_count": len(report["errors"]), "report": str(output)}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
