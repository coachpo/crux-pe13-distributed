#!/usr/bin/env python3
"""Serial host verification of an existing OTA; reads only public certificates."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import zipfile


def certificate_archive(certificate, output):
    pem = certificate.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" not in pem or b"PRIVATE KEY-----" in pem:
        raise ValueError(f"Expected only public X.509 PEM certificate: {certificate}")
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("releasecert.x509.pem", pem)


def corrupt_member_copy(original, output):
    with zipfile.ZipFile(original) as archive:
        member = next(item for item in archive.infolist() if not item.is_dir())
    with original.open("rb") as stream:
        stream.seek(member.header_offset)
        header = stream.read(30)
        if len(header) != 30 or header[:4] != b"PK\x03\x04":
            raise ValueError("Invalid ZIP member local header")
        # Change signed metadata while keeping entry data, sizes and CRCs intact.
        offset = member.header_offset + 10
        stream.seek(-6, 2)
        footer = stream.read(6)
        if footer[2:4] != b"\xff\xff":
            raise ValueError("OTA is missing a Recovery whole-file signature footer")
        comment_length = int.from_bytes(footer[4:6], "little")
        signed_length = original.stat().st_size - comment_length - 2
        if offset >= signed_length:
            raise ValueError("Selected mutation is outside the signed bytes")
    shutil.copyfile(original, output)
    with output.open("r+b") as stream:
        stream.seek(offset)
        before = stream.read(1)[0]
        after = before ^ 1
        stream.seek(offset)
        stream.write(bytes([after]))
    return {"member": member.filename, "field": "local DOS modification time",
            "offset": offset, "before": before,
            "after": after, "signed_length": signed_length}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", type=Path, required=True)
    parser.add_argument("--ota", type=Path, required=True)
    parser.add_argument("--release-cert", type=Path, required=True)
    parser.add_argument("--wrong-cert", type=Path, required=True)
    parser.add_argument("--otacerts", type=Path,
                        help="Optional actual Recovery otacerts.zip; must also accept the OTA")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New output directory; retains raw results and damaged OTA copy")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    correct = args.output_dir / "release-public-otacerts.zip"
    wrong = args.output_dir / "wrong-public-otacerts.zip"
    certificate_archive(args.release_cert, correct)
    certificate_archive(args.wrong_cert, wrong)

    cases = [("release-public-certificate", args.ota, correct, 0),
             ("wrong-public-certificate", args.ota, wrong, 1)]
    if args.otacerts:
        cases.insert(1, ("actual-recovery-otacerts", args.ota, args.otacerts, 0))
    records = []

    def verify(name, package, certificates, expected):
        command = [str(args.tool), "--package", str(package), "--otacerts", str(certificates)]
        result = subprocess.run(command, capture_output=True, text=True)
        (args.output_dir / f"{name}.stdout.txt").write_text(result.stdout)
        (args.output_dir / f"{name}.stderr.txt").write_text(result.stderr)
        record = {"case": name, "command": command, "expected_exit": expected,
                  "actual_exit": result.returncode, "passed": result.returncode == expected}
        records.append(record)
        print(json.dumps(record), flush=True)
        return record["passed"]

    for case in cases:
        verify(*case)
    if all(record["passed"] for record in records):
        damaged = args.output_dir / "ota-single-byte-damaged.zip"
        mutation = corrupt_member_copy(args.ota, damaged)
        (args.output_dir / "mutation.json").write_text(json.dumps(mutation, indent=2) + "\n")
        verify("single-byte-damage", damaged, correct, 1)
    summary = {"passed": all(record["passed"] for record in records), "cases": records,
               "scope": "Recovery verify_file and certificate ZIP loader on host; no installer gate",
               "input_ota": str(args.ota), "release_certificate": str(args.release_cert),
               "actual_recovery_otacerts": str(args.otacerts) if args.otacerts else None}
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
