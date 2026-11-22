#!/usr/bin/env python3
"""Stage a Crux PE Recovery/ROM FIT in U-Boot RAM and use its watchdog guard.

Use the existing uconsole_watch.py process and /tmp/crux-m0 bridge. Never starts
a watcher, flashes a partition, saves the environment, or patches kernel RAM.
--resume-shell requires a fresh U-Boot shell; otherwise the exact Crux ADB/ABL
target is rebooted to U-Boot. --bounded-diagnostic disables kernel watchdog
takeover for this boot and requires a captured stopped menu after the timeout.
It starts an independent ADB observer before handoff and retains its samples.
Normal mode records ADB availability without claiming a successful boot from
USB enumeration alone. Recovery may intentionally keep ADB disabled.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid
import zlib


SERIAL = os.environ.get("ANDROID_SERIAL", "")
# Qualcomm U-Boot exposes this phone's chip ID rather than ABL's serial.
UBOOT_SERIAL = "1499001831"
ADDRESS = "0xC0000000"
CMD = Path("/tmp/crux-m0/cmd.txt")
LOG = Path("/tmp/crux-m0/console.log")
# The deployed Boot/Recovery menu: bootmenu_3 (key 4) drops to the shell; key 5 resets.
SHELL_ENTRY, SHELL_KEY = "bootmenu_3", "4"
GUARD = "if wdt dev watchdog@17c10000; then if wdt start ${kernel_watchdog_ms}; then bootm go; fi; fi"
DIAGNOSTIC = "watchdog_v2.enable=0 msm_poweroff.download_mode=0"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
BOOT_ERROR = re.compile(r"FDT_ERR_|ERROR:|Bad (?:Linux|Data|Magic)|Hash.*(?:fail|mismatch)|allocation error|Unknown command", re.I)
ADB_IDENTITY_COMMAND = "cat /proc/version; cat /proc/cmdline; cat /sys/devices/system/cpu/online; getprop ro.product.device; getprop ro.build.fingerprint; getprop sys.boot_completed; getprop ro.twrp.version; pidof recovery || test $? -eq 1"
ADB_OBSERVER_TIMEOUT = 3
ADB_OBSERVER_INTERVAL = .5
ADB_OBSERVER_LIFETIME = 80
ADB_OBSERVER_JOIN = 4


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def clean(text):
    return ANSI.sub("", text).replace("\r", "")


def read_log(offset):
    with LOG.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("latin1")


def fit_components(data, mode):
    """Read inline FIT properties and independently verify default image hashes."""
    require(len(data) >= 40, "Truncated FIT header")
    header = struct.unpack_from(">10I", data)
    magic, total, off_struct, off_strings = header[:4]
    names_size, body_size = header[8:]
    require(magic == 0xD00DFEED and total == len(data), "Expected a complete inline FIT")
    require(off_struct + body_size <= total and off_strings + names_size <= total,
            "FIT blocks exceed file size")
    names = data[off_strings:off_strings + names_size]
    position, end = off_struct, off_struct + body_size
    stack, nodes = [], {}
    while position < end:
        require(position + 4 <= end, "Truncated FIT token")
        token = struct.unpack_from(">I", data, position)[0]
        position += 4
        if token == 1:
            stop = data.find(b"\0", position, end)
            require(stop >= position, "Unterminated FIT node")
            stack.append(data[position:stop].decode())
            path = "/".join(stack)
            require(path not in nodes, "Duplicate FIT node")
            nodes[path] = {}
            position = (stop + 4) & ~3
        elif token == 2:
            require(stack, "Unbalanced FIT node")
            stack.pop()
        elif token == 3:
            require(stack and position + 8 <= end, "Truncated FIT property")
            size, name_offset = struct.unpack_from(">II", data, position)
            position += 8
            require(name_offset < len(names) and position + size <= end,
                    "FIT property exceeds bounds")
            stop = names.find(b"\0", name_offset)
            require(stop >= name_offset, "Unterminated FIT property name")
            name = names[name_offset:stop].decode()
            require(name not in nodes["/".join(stack)], "Duplicate FIT property")
            nodes["/".join(stack)][name] = data[position:position + size]
            position = (position + size + 3) & ~3
        elif token == 4:
            continue
        elif token == 9:
            require(not stack, "Unbalanced FIT structure")
            break
        else:
            raise RuntimeError(f"Invalid FIT token {token}")
    else:
        raise RuntimeError("Missing FIT end token")

    def string(value):
        require(value.endswith(b"\0") and b"\0" not in value[:-1], "Invalid FIT string")
        return value[:-1].decode()

    configuration = string(nodes["/configurations"]["default"])
    config = nodes[f"/configurations/{configuration}"]
    require("kernel" in config and "fdt" in config, "FIT requires kernel and FDT")
    require(mode != "recovery" or "ramdisk" in config, "Recovery FIT requires ramdisk")
    result = {}
    for role in ("kernel", "fdt", "ramdisk"):
        if role not in config:
            continue
        name = string(config[role])
        path = f"/images/{name}"
        props = nodes[path]
        require("data" in props and props["data"], f"Missing inline data: {name}")
        digest = hashlib.sha256(props["data"]).digest()
        hashes = [p for key, p in nodes.items()
                  if key.startswith(path + "/") and key.count("/") == path.count("/") + 1
                  and p.get("algo") == b"sha256\0"]
        require(hashes and all(p.get("value") == digest for p in hashes),
                f"Missing or wrong SHA-256 hash: {name}")
        result[role] = {"image": name, "sha256": digest.hex(), "bytes": len(props["data"])}
    return result


class Session:
    def __init__(self, out):
        self.out = out
        self.host_commands = []
        self.uboot_commands = []
        self.offset = LOG.stat().st_size
        self.start_offset = self.offset
        self.command_nonce = uuid.uuid4().hex[:12]
        self.command_counter = 0

    def save(self):
        (self.out / "host-commands.json").write_text(json.dumps(self.host_commands, indent=2) + "\n")
        (self.out / "uboot-commands.json").write_text(json.dumps(self.uboot_commands, indent=2) + "\n")
        (self.out / "experiment-console.txt").write_bytes(read_log(self.start_offset).encode("latin1"))

    def run(self, argv, timeout=15, required=True):
        require(0 < timeout <= 60, "Host command timeout must be at most 60 seconds")
        record = {"argv": [str(a) for a in argv], "timeout_s": timeout}
        try:
            completed = subprocess.run(record["argv"], capture_output=True, text=True, timeout=timeout)
            record.update(exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
        except subprocess.TimeoutExpired as error:
            record.update(timed_out=True, stdout=(error.stdout or b"").decode() if isinstance(error.stdout, bytes) else error.stdout,
                          stderr=(error.stderr or b"").decode() if isinstance(error.stderr, bytes) else error.stderr)
            raise
        finally:
            self.host_commands.append(record)
            self.save()
        if required:
            require(completed.returncode == 0, f"Command failed: {record}")
        return completed

    def send(self, command, timeout=15, prompt=True):
        record = {"command": command, "timeout_s": timeout, "requires_prompt": prompt}
        self.command_counter += 1
        marker = f"CRUX_CMD_{self.command_nonce}_{self.command_counter}"
        wire_command = command + f"; echo {marker}:$?" if prompt else command
        record["wire_command"] = wire_command
        CMD.write_text(wire_command)
        deadline = time.monotonic() + timeout
        raw, echoed, completion = "", False, None
        try:
            while time.monotonic() < deadline:
                time.sleep(.15)
                raw = read_log(self.offset)
                text = clean(re.sub(r"(?m)^\[watch\][^\n]*\n", "", raw))
                echo_match = re.search(r"(?m)^(?:=> )?" + re.escape(wire_command) + r"$", text)
                echoed = bool(echo_match)
                completion = re.search(r"(?m)^" + re.escape(marker) + r":([0-9]+)$", text) if prompt else None
                sent_disconnecting_command = not prompt and f"[watch] >>> {wire_command}\n" in raw
                # An old prompt can drain after the current command echo. The
                # unique, device-executed marker is the transaction boundary.
                if (echoed and (not prompt or (completion and re.search(r"=>\s*$", text)))) or sent_disconnecting_command:
                    break
            # Gadget switches/reset can disconnect before their echo drains.
            # Callers separately require the exact new transport or watchdog
            # handoff evidence before treating these commands as executed.
            require(echoed or (not prompt and f"[watch] >>> {wire_command}\n" in raw),
                    f"No command transmission for {command!r}: {raw}")
            require(not prompt or (completion and re.search(r"=>\s*$", text)), f"No completion marker/prompt after {command!r}: {raw}")
            require(not prompt or completion[1] == "0", f"U-Boot command failed: {command!r}: {raw}")
            # Console refresh output may finish draining before this command's
            # echo. Its errors belong to the earlier transaction; keep them in
            # the receipt but validate and return only this command's output.
            command_text = text[echo_match.start():] if echo_match else text
            require(not BOOT_ERROR.search(command_text), f"U-Boot rejected {command!r}: {raw}")
            return command_text
        finally:
            self.offset += len(raw)
            record["console"] = raw
            self.uboot_commands.append(record)
            self.save()

    def recover(self, offset, timeout=60, require_menu=False, *, strict=False):
        """Recover a shell; strict mode never sends an unobserved menu shortcut."""
        deadline = time.monotonic() + timeout
        menu_seen, key_sent, redraw_sent = False, False, False
        menu_evidence, waiting_countdown, menu_key = None, None, None
        reopen_count, last_reopen_time, prompt_refresh_sent = 0, None, False
        while time.monotonic() < deadline:
            raw = read_log(offset)
            text = clean(raw)
            port_lines = (r"\[watch\] port (?:reopened|open)\n" if strict and require_menu
                          else r"\[watch\] port reopened\n")
            port_events = list(re.finditer(port_lines, raw))
            reconnected = bool(port_events)
            if reconnected:
                count = len(port_events)
                if count != reopen_count:
                    reopen_count, last_reopen_time = count, time.monotonic()
                # The menu positions successive rows using ANSI escapes
                # without line feeds; preserve the boundary between labels.
                after_reconnect = ANSI.sub(" ", raw[port_events[-1].end():]).replace("\r", "")
                full_menu = ("*** U-Boot Boot Menu ***" in after_reconnect
                             and f"{SHELL_KEY}. Drop to shell" in after_reconnect)
                countdowns = [int(value) for value in re.findall(
                    r"Hit any key to stop autoboot:\s*(\d+)\b", after_reconnect)]
                high_countdowns = [value for value in countdowns if 86000 <= value <= 86400]
                if strict:
                    if not require_menu:
                        require(not high_countdowns and "*** U-Boot Boot Menu ***" not in after_reconnect,
                                "Unexpected reboot to menu while returning from fastboot")
                    elif high_countdowns or "*** U-Boot Boot Menu ***" in after_reconnect:
                        # The USB reopen can lose the first menu rows. Exit is
                        # a separately rendered, non-booting choice.
                        keys = set(re.findall(r"\b([1-9a-z])\.\s+Drop to shell\b", after_reconnect))
                        if not keys and re.search(r"\b0\.\s+Exit\b", after_reconnect):
                            keys.add("0")
                        require(len(keys) <= 1, "Ambiguous live shell shortcut")
                        if keys:
                            menu_seen, menu_evidence = True, "live_shell_choice"
                            menu_key = keys.pop()
                            if high_countdowns:
                                waiting_countdown = high_countdowns[-1]
                        elif (len(high_countdowns) >= 2 and not redraw_sent and not key_sent
                              and after_reconnect.startswith("[watch] feature menu-redraw-v1\n")):
                            # Confirm the fresh waiting menu and the running
                            # watcher's raw-navigation support before asking
                            # it to redraw; never send an assumed shortcut.
                            CMD.write_text("@menu-redraw")
                            redraw_sent = True
                elif full_menu:
                    menu_seen, menu_evidence = True, "full_menu"
                elif high_countdowns and not menu_seen:
                    menu_seen, menu_evidence = True, "waiting_countdown"
                    waiting_countdown = high_countdowns[-1]
            if menu_seen and not key_sent:
                CMD.write_text(menu_key if strict else SHELL_KEY)
                key_sent = True
            # The fastboot command restores the existing U-Boot shell. A USB
            # reopen can miss its prompt; the shell key yields a fresh shell prompt or
            # selects Drop to shell if a menu appeared while output was lost.
            if (reconnected and not strict and not require_menu and not key_sent
                    and not re.search(r"=>\s*$", text)
                    and time.monotonic() - last_reopen_time >= 1):
                CMD.write_text(SHELL_KEY)
                key_sent, prompt_refresh_sent = True, True
            if reconnected and (menu_seen or not require_menu) and re.search(r"=>\s*$", text):
                self.offset = offset + len(raw)
                return {"menu_observed": menu_seen, "menu_evidence": menu_evidence,
                        "menu_key": menu_key,
                        "menu_redraw_sent": redraw_sent,
                        "waiting_countdown": waiting_countdown,
                        "prompt_refresh_sent": prompt_refresh_sent,
                        "shell_observed": True, "console": raw}
            time.sleep(.2)
        raise RuntimeError("No verified U-Boot recovery" + (" through waiting menu" if require_menu else ""))


def wait_fastboot(session, executable, expected=None):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        output = session.run([executable, "devices"], timeout=5).stdout
        serials = [p[0] for line in output.splitlines()
                   if len(p := line.split()) >= 2 and p[1] == "fastboot"]
        ready = expected in serials if expected else len(serials) == 1
        if ready:
            return expected or serials[0]
        time.sleep(1)
    raise RuntimeError("Expected fastboot transport did not appear")


def stage_fastboot_command(session, strict_console):
    command = f"run stop_acm; fastboot -l {ADDRESS} usb 0; run serial_gadget"
    if strict_console:
        # The first prompt can be lost while the watcher reopens USB. Wait
        # in this same U-Boot command instead of guessing a menu keystroke.
        session.send("sleep 0")
        command += "; sleep 2"
    return command


def protocol_continue(serial, out):
    """SDKs without continue use only that standard command on the exact interface."""
    import usb.core
    import usb.util

    matches = []
    for device in usb.core.find(find_all=True):
        try:
            if usb.util.get_string(device, device.iSerialNumber) != serial:
                continue
            for interface in device.get_active_configuration():
                if (interface.bInterfaceClass, interface.bInterfaceSubClass, interface.bInterfaceProtocol) == (255, 66, 3):
                    matches.append((device, interface))
        except usb.core.USBError:
            continue
    require(len(matches) == 1, "Exact staged fastboot USB interface not found")
    device, interface = matches[0]
    record = {"serial": serial, "interface": interface.bInterfaceNumber, "command": "continue"}
    try:
        usb.util.claim_interface(device, interface.bInterfaceNumber)
        outgoing = usb.util.find_descriptor(interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        incoming = usb.util.find_descriptor(interface, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        require(outgoing is not None and incoming is not None, "Fastboot endpoints missing")
        require(outgoing.write(b"continue", timeout=3000) == 8, "Incomplete continue command")
        response = bytes(incoming.read(64, timeout=3000))
        record["response"] = response.decode("ascii", "replace")
        require(response.startswith(b"OKAY"), f"Fastboot continue failed: {response!r}")
        return record
    finally:
        (out / "continue-protocol.json").write_text(json.dumps(record, indent=2) + "\n")
        usb.util.dispose_resources(device)


def verify_environment(session, expected_version):
    version = session.send("version")
    require("U-Boot " in version and expected_version in version, "Unexpected deployed U-Boot version")
    output = session.send("printenv fastboot_addr_r bootdelay bootcmd menucmd boot_go kernel_watchdog_ms " + SHELL_ENTRY)
    actual = dict(re.findall(r"(?m)^([a-z0-9_]+)=(.*)$", output))
    for key, value in {"bootdelay": "0", "bootcmd": "run menucmd", "menucmd": "bootmenu 86400",
                       "boot_go": GUARD, "kernel_watchdog_ms": "30000",
                       SHELL_ENTRY: "Drop to shell=setenv bootretry -1"}.items():
        require(actual.get(key) == value, f"Unexpected deployed {key}: {actual.get(key)!r}")
    # Qualcomm may replace this default with an allocated high-memory buffer.
    # The test uses fastboot -l explicitly and verifies that buffer with CRC32.
    return {"version": version, "environment": actual}


def guarded_handoff(session, command="run boot_go"):
    """Require fresh, ordered watchdog-start and kernel-start console witnesses."""
    offset = LOG.stat().st_size
    session.send(command, prompt=False)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        launch = clean(read_log(offset))
        require(not BOOT_ERROR.search(launch) and not re.search(
            r"watchdog.*(?:failed|not supported)", launch, re.I),
            f"Kernel handoff rejected: {launch}")
        started = re.search(r"WDT:\s+Started watchdog@17c10000.*\(30s timeout\)", launch)
        kernel = launch.find("Starting kernel ...")
        if kernel >= 0:
            require(started and started.end() < kernel, "No watchdog-start success readback before kernel handoff")
            return offset
        time.sleep(.2)
    raise RuntimeError("No guarded kernel handoff observed")


def preparation_commands(components, bounded, bootargs_added=()):
    commands = ["setenv fdt_high", "setenv initrd_high", "setenv bootargs",
                f"bootm start {ADDRESS}", "bootm loados"]
    if "ramdisk" in components:
        commands.append("bootm ramdisk")
    additions = list(bootargs_added) + (DIAGNOSTIC.split() if bounded else [])
    if additions:
        commands += ["fdt get value ba /chosen bootargs", f'setenv ba "${{ba}} {" ".join(additions)}"',
                     'setenv bootargs "${ba}"']
    commands += ["bootm prep", "fdt print /chosen bootargs", "fdt print /memory"]
    if "ramdisk" in components:
        commands += ["fdt print /chosen linux,initrd-start", "fdt print /chosen linux,initrd-end"]
    return commands


def adb_identity(session, executable):
    try:
        listing = session.run([executable, "devices", "-l"], timeout=5, required=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "error": str(error)}
    if listing.returncode:
        return {"available": False, "error": listing.stdout + listing.stderr}
    devices = listing.stdout
    if not re.search(r"^" + SERIAL + r"\s+(?:device|recovery)\b", devices, re.M):
        return None
    # A normal ROM has no Recovery process; its absent PID must not turn an
    # otherwise successful identity read into a failed shell command.
    try:
        result = session.run([executable, "-s", SERIAL, "shell", ADB_IDENTITY_COMMAND], timeout=5, required=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": True, "identity_collected": False, "error": str(error)}
    (session.out / "adb-identity.txt").write_text(result.stdout + result.stderr)
    return {"available": True, "identity_exit_code": result.returncode,
            "identity_collected": result.returncode == 0}


class BoundedAdbObserver:
    """Observe ADB independently of the console Session and its output files."""

    def __init__(self, out, executable):
        self.out, self.executable = out, executable
        self.ready = threading.Event()
        self.handoff = threading.Event()
        self.stopping = threading.Event()
        self.launched = None
        self.deadline = None
        self.error = None
        self.final = None
        self.summary = {"serial": SERIAL, "available": None, "observed_states": [],
                        "listing_samples": 0, "successful_listing_samples": 0,
                        "command_errors": 0, "unauthorized_observed": False,
                        "first_seen_elapsed_s": None, "first_available_elapsed_s": None,
                        "last_listing_elapsed_s": None,
                        "identity_collected": False, "first_identity_elapsed_s": None,
                        "samples_file": "adb-observer-samples.jsonl",
                        "command_timeout_s": ADB_OBSERVER_TIMEOUT,
                        "maximum_lifetime_s": ADB_OBSERVER_LIFETIME}
        self.thread = threading.Thread(target=self.observe, name="crux-bounded-adb", daemon=True)

    def elapsed(self):
        return None if self.launched is None else time.monotonic() - self.launched

    def command(self, argv, kind):
        timeout = min(ADB_OBSERVER_TIMEOUT, max(.001, self.deadline - time.monotonic()))
        record = {"kind": kind, "argv": argv, "serial": SERIAL,
                  "phase": "before_handoff" if self.launched is None else "after_handoff",
                  "started_elapsed_s": self.elapsed(), "timeout_s": timeout}
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            record.update(exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            record["error"] = f"{type(error).__name__}: {error}"
            record["timed_out"] = isinstance(error, subprocess.TimeoutExpired)
        record["finished_elapsed_s"] = self.elapsed()
        return record

    def listing(self, handle, post_handoff):
        record = self.command([self.executable, "devices", "-l"], "devices")
        state = None
        if record.get("exit_code") == 0:
            for line in record["stdout"].splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[0] == SERIAL:
                    state = fields[1]
                    break
        valid = record.get("exit_code") == 0
        record.update(target_state=(state or "absent") if valid else "unknown",
                      target_available=state in ("device", "recovery") if valid else None)
        handle.write(json.dumps(record) + "\n")
        handle.flush()
        if post_handoff:
            self.summary["listing_samples"] += 1
            self.summary["last_listing_elapsed_s"] = record["finished_elapsed_s"]
            if record.get("exit_code") == 0:
                self.summary["successful_listing_samples"] += 1
                if record["target_state"] not in self.summary["observed_states"]:
                    self.summary["observed_states"].append(record["target_state"])
                if state and self.summary["first_seen_elapsed_s"] is None:
                    self.summary["first_seen_elapsed_s"] = record["finished_elapsed_s"]
                self.summary["unauthorized_observed"] |= state == "unauthorized"
                if record["target_available"]:
                    self.summary["available"] = True
                    if self.summary["first_available_elapsed_s"] is None:
                        self.summary["first_available_elapsed_s"] = record["finished_elapsed_s"]
            else:
                self.summary["command_errors"] += 1
        return record

    def identity(self, handle):
        record = self.command([self.executable, "-s", SERIAL, "shell", ADB_IDENTITY_COMMAND], "identity")
        handle.write(json.dumps(record) + "\n")
        handle.flush()
        if record.get("exit_code") == 0:
            identity_file = "adb-observer-identity.txt"
            (self.out / identity_file).write_text(record["stdout"] + record["stderr"])
            self.summary["identity_collected"] = True
            self.summary["first_identity_elapsed_s"] = record["finished_elapsed_s"]
            self.summary["identity_file"] = identity_file
            lines = record["stdout"].splitlines()
            if len(lines) >= 7:
                self.summary["short_identity"] = dict(zip(
                    ("kernel", "online_cpus", "product", "fingerprint", "boot_completed", "twrp_version"),
                    (lines[0], *lines[2:7])))
                if len(lines) > 7:
                    self.summary["short_identity"]["recovery_pid"] = lines[7]
        else:
            self.summary["command_errors"] += 1

    def observe(self):
        deadline = self.deadline = time.monotonic() + ADB_OBSERVER_LIFETIME
        try:
            with (self.out / self.summary["samples_file"]).open("x") as handle:
                first = self.listing(handle, post_handoff=False)
                require(first.get("exit_code") == 0,
                        "ADB observer initial listing failed: " + str(first))
                self.ready.set()
                self.handoff.wait(max(0, deadline - time.monotonic()))
                while self.launched is not None and not self.stopping.is_set() and time.monotonic() < deadline:
                    record = self.listing(handle, post_handoff=True)
                    if (record["target_available"] and not self.summary["identity_collected"]
                            and not self.stopping.is_set() and time.monotonic() < deadline):
                        self.identity(handle)
                    self.stopping.wait(ADB_OBSERVER_INTERVAL)
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
        finally:
            self.ready.set()

    def start(self):
        self.thread.start()
        if not self.ready.wait(ADB_OBSERVER_TIMEOUT + 1) or self.error or not self.thread.is_alive():
            self.stop("startup_failed")
            raise RuntimeError("ADB observer did not start: " + str(self.error or "readiness timeout"))

    def begin_handoff(self, launched):
        require(self.ready.is_set() and self.thread.is_alive() and not self.error,
                "ADB observer is not running before kernel handoff")
        self.launched = launched
        self.handoff.set()

    def stop(self, reason):
        if self.final is not None:
            return self.final
        self.stopping.set()
        self.handoff.set()
        if self.thread.ident is not None:
            self.thread.join(ADB_OBSERVER_JOIN)
        stopped = not self.thread.is_alive()
        if self.summary["available"] is not True and self.summary["successful_listing_samples"]:
            self.summary["available"] = False
        status = ("transport_observed" if self.summary["available"] is True else
                  "transport_not_observed" if self.summary["available"] is False else
                  "no_successful_post_handoff_sample")
        self.final = dict(self.summary, observation_status=status, observer_stopped=stopped,
                          stop_reason=reason, observer_error=self.error,
                          stopped_elapsed_s=self.elapsed())
        (self.out / "adb-observer-summary.json").write_text(json.dumps(self.final, indent=2) + "\n")
        return self.final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fit", type=Path, required=True)
    parser.add_argument("--fit-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True, help="New output directory")
    parser.add_argument("--mode", choices=("recovery", "rom"), required=True)
    parser.add_argument("--resume-shell", action="store_true")
    parser.add_argument("--strict-console", action="store_true",
                        help="Require fresh live menu/prompt; never refresh with a guessed key")
    parser.add_argument("--bounded-diagnostic", action="store_true")
    parser.add_argument("--bootarg", action="append", default=[], help="One additional RAM-only diagnostic boot argument")
    parser.add_argument("--dry-run", action="store_true", help="Validate input and print plan; no bridge/device access")
    sdk = Path.home() / "Library/Android/sdk/platform-tools"
    parser.add_argument("--adb", default=shutil.which("adb") or str(sdk / "adb"))
    parser.add_argument("--fastboot", default=shutil.which("fastboot") or str(sdk / "fastboot"))
    parser.add_argument("--expected-uboot-version", default="2026.10-rc5-00618-g5ba2e98a115a")
    parser.add_argument("--watcher-pid", type=int, default=66661, help="Existing watcher; never started by this script")
    args = parser.parse_args()
    if not SERIAL:
        parser.error("set ANDROID_SERIAL to the phone's adb serial")
    require(all(re.fullmatch(r"[a-zA-Z0-9_.=-]+", token) for token in args.bootarg), "Invalid added boot argument")
    require(re.fullmatch(r"[0-9a-fA-F]{64}", args.fit_sha256), "Invalid FIT SHA-256")
    args.fit = args.fit.resolve()
    data = args.fit.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    require(digest == args.fit_sha256.lower(), "FIT file SHA-256 differs from supplied digest")
    components = fit_components(data, args.mode)
    plan = {"fit": str(args.fit), "fit_sha256": digest, "fit_bytes": len(data), "mode": args.mode,
            "serial": SERIAL, "components": components, "bounded_diagnostic": args.bounded_diagnostic,
            "partition_writes": False, "kernel_ram_patches": False,
            "bootargs_added": args.bootarg,
            "preparation": preparation_commands(components, args.bounded_diagnostic, args.bootarg), "handoff": "run boot_go"}
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    args.out.mkdir(parents=True, exist_ok=False)
    initial_adb = {"available": None, "observation_status": "not_started"} if args.bounded_diagnostic else {"available": False}
    result = dict(plan, status="preflight", hardware_boot_verified=False, adb=initial_adb)
    session = None
    observer = None
    try:
        require(CMD.is_file() and LOG.is_file(), "Existing /tmp/crux-m0 watcher bridge is missing")
        require(args.watcher_pid > 0, "Invalid watcher PID")
        try:
            os.kill(args.watcher_pid, 0)
            result["watcher_process_check"] = "present"
        except PermissionError:
            # macOS sandboxing can forbid even signal 0; command echoes below
            # still require a live bridge without starting a second reader.
            result["watcher_process_check"] = "unavailable_under_host_permissions"
        result["watcher_pid"] = args.watcher_pid
        session = Session(args.out)
        if not args.resume_shell:
            devices = session.run([args.adb, "devices"], timeout=5).stdout
            if re.search(r"^" + SERIAL + r"\s+(?:device|recovery)\b", devices, re.M):
                session.run([args.adb, "-s", SERIAL, "reboot", "bootloader"])
            wait_fastboot(session, args.fastboot, SERIAL)
            product = session.run([args.fastboot, "-s", SERIAL, "getvar", "product"])
            require(re.search(r"product:\s*crux\b", product.stdout + product.stderr), "ABL target is not Crux")
            offset = LOG.stat().st_size
            session.run([args.fastboot, "-s", SERIAL, "reboot"])
            result["reboot_to_uboot"] = session.recover(offset, require_menu=True, strict=args.strict_console)
        result["deployed_uboot"] = verify_environment(session, args.expected_uboot_version)
        stage = stage_fastboot_command(session, args.strict_console)
        session.send(f"setenv ui_ram_fastboot '{stage}'")
        offset = LOG.stat().st_size
        session.send("run ui_ram_fastboot", prompt=False)
        transport = wait_fastboot(session, args.fastboot, UBOOT_SERIAL)
        result["uboot_fastboot_serial"] = transport
        product = session.run([args.fastboot, "-s", transport, "getvar", "product"])
        require(re.search(r"product:\s*xiaomi-crux\b", product.stdout + product.stderr), "U-Boot fastboot target is not Crux")
        session.run([args.fastboot, "-s", transport, "stage", args.fit], timeout=60)
        continued = session.run([args.fastboot, "-s", transport, "continue"], required=False)
        if continued.returncode:
            require(re.search(r"(?:unknown|unrecognized|unsupported|usage:).*", continued.stdout + continued.stderr, re.I),
                    "Continue failed for a reason other than missing SDK command; do not resend")
            result["continue_protocol"] = protocol_continue(transport, args.out)
        result["fastboot_to_shell"] = session.recover(offset, timeout=30, strict=args.strict_console)
        expected_crc = zlib.crc32(data) & 0xFFFFFFFF
        output = session.send(f"crc32 {ADDRESS} 0x{len(data):x}", timeout=20)
        require(re.search(r"==>\s*" + f"{expected_crc:08x}" + r"\b", output, re.I), "FIT RAM CRC differs")
        result["fit_crc_verified"] = True
        for command in plan["preparation"]:
            output = session.send(command)
            if command.startswith("bootm start"):
                for component in components.values():
                    require(component["sha256"] in output, f"Missing component hash readback: {component}")
                require(output.count("sha256+ OK") >= len(components), "Not all component hashes verified by U-Boot")
                result["component_hashes_verified"] = True
            if command == "fdt print /chosen bootargs":
                match = re.search(r'bootargs\s*=\s*"([^"]*)"', output)
                require(match, "No final DT bootargs readback")
                bootargs = match.group(1)
                result["prepared_bootargs"] = bootargs
                for token in args.bootarg:
                    require(token in bootargs.split(), f"Missing added RAM bootarg {token}")
                require(f"androidboot.serialno={SERIAL}" in bootargs.split(),
                        "Prepared FIT DT does not name the exact Crux target")
                for token in DIAGNOSTIC.split():
                    require((token in bootargs.split()) == args.bounded_diagnostic,
                            f"Unexpected diagnostic bootarg {token}")
        if args.bounded_diagnostic:
            observer = BoundedAdbObserver(args.out, args.adb)
            observer.start()
        launched = time.monotonic()
        if observer:
            observer.begin_handoff(launched)
        launch_offset = guarded_handoff(session)
        result["watchdog_start_verified"] = True
        result["kernel_handoff_observed"] = True
        if args.bounded_diagnostic:
            recovery = session.recover(launch_offset, timeout=60, require_menu=True, strict=args.strict_console)
            recovered = time.monotonic()
            result["adb"] = observer.stop("menu_recovered")
            require(result["adb"]["observer_stopped"], "ADB observer did not stop within its join deadline")
            result["watchdog_recovery"] = dict(recovery, elapsed_s=recovered - launched)
            result["deployed_uboot_after_reset"] = verify_environment(session, args.expected_uboot_version)
            result["status"] = "bounded_diagnostic_recovered_to_menu_and_shell"
            result["checkpoint_captured"] = False
        else:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                identity = adb_identity(session, args.adb)
                if identity:
                    result["adb"] = dict(identity, elapsed_s=time.monotonic() - launched)
                    break
                time.sleep(1)
            result["status"] = "kernel_handoff_adb_observed" if result["adb"]["available"] else "kernel_handoff_adb_unavailable_boot_unverified"
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        print(result["error"])
        return 1
    finally:
        if observer:
            result["adb"] = observer.stop("launcher_failed" if result["status"] == "failed" else "launcher_finished")
        if session:
            session.save()
        (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
