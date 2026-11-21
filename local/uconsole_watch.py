#!/usr/bin/env python3
"""Reconnecting U-Boot console daemon for the Crux phone.

Survives USB re-enumeration (reset / reboot / fastboot round-trips): if
the CDC-ACM port goes away it closes the old handle and opens the next
/dev/cu.usbmodem* that appears.

Commands are read from /tmp/crux-m0/cmd.txt (a CR is appended), all
console output is appended to /tmp/crux-m0/console.log.
The host-only token @menu-redraw sends two raw Ctrl-N bytes, without CR,
to a verified waiting U-Boot menu so a late console can see its choices.

Usage:
    tools/edl/.venv/bin/python crux-pe13-distributed/local/uconsole_watch.py

Start it before triggering a reboot so the menu output is captured
immediately.
"""
import glob
import os
import threading
import time

import serial

PORT_GLOB = '/dev/cu.usbmodem*'
CMD = '/tmp/crux-m0/cmd.txt'
LOG = '/tmp/crux-m0/console.log'
MENU_REDRAW = '@menu-redraw'
MENU_FEATURE = b'[watch] feature menu-redraw-v1\n'

s = None
log = open(LOG, 'ab', buffering=0)
lock = threading.Lock()


def port_present():
    return bool(glob.glob(PORT_GLOB))


def reopen(reason, expected):
    """Drop the current handle and wait for a fresh ACM port.

    macOS can keep an old fd alive after the USB device is re-enumerated:
    reads then return EOF forever and writes fail quietly, so neither side
    notices on its own.  Callers must therefore also treat a disappearing
    /dev node and a failed write as "reopen"."
    """
    global s
    with lock:
        # Read and write failures can race on the same old handle. Only its
        # first recovery may replace it; a delayed failure must leave the new
        # live connection open so its initial console output is preserved.
        if s is not expected:
            return
        try:
            if expected is not None:
                expected.close()
        except Exception:
            pass
        s = None
        log.write(f'[watch] reopening ({reason})\n'.encode())
        while s is None:
            s = try_open()
            if s is None:
                time.sleep(0.5)
        log.write(b'[watch] port reopened\n' + MENU_FEATURE)


def try_open():
    for p in sorted(glob.glob(PORT_GLOB)):
        try:
            ser = serial.Serial(port=p, baudrate=115200, timeout=0.2, write_timeout=1.0)
            ser.dtr = True
            ser.rts = True
            return ser
        except Exception:
            pass
    return None


while s is None:
    s = try_open()
    if s is None:
        time.sleep(0.5)
log.write(b'[watch] port open\n' + MENU_FEATURE)


def reader():
    global s
    while True:
        current = s
        if current is None:
            time.sleep(0.2)
            continue
        try:
            d = current.read(4096)
            if d:
                log.write(d)
        except Exception as e:
            log.write(f'\n[watch] read error {e}\n'.encode())
            time.sleep(0.5)
            reopen('read error', current)


threading.Thread(target=reader, daemon=True).start()

last = 0
if os.path.exists(CMD):
    last = os.path.getmtime(CMD)

pending = None

while True:
    time.sleep(0.2)
    # macOS leaves stale handles behind across USB re-enumeration
    current = s
    if current is not None and not port_present():
        reopen('port disappeared', current)
    try:
        mt = os.path.getmtime(CMD) if os.path.exists(CMD) else 0
    except OSError:
        continue
    if mt != last:
        last = mt
        try:
            cmd = open(CMD).read().strip()
        except OSError:
            continue
        if cmd:
            pending = cmd
    if pending:
        current = s
        if current is None:
            continue
        try:
            # No CR: Enter would boot the highlighted entry. The first
            # Ctrl-N stops the countdown and the second redraws via Down.
            payload = b'\x0e\x0e' if pending == MENU_REDRAW else (pending + '\r').encode()
            current.write(payload)
            current.flush()
            log.write(f'\n[watch] >>> {pending}\n'.encode())
            pending = None
        except Exception as e:
            log.write(f'[watch] write failed ({e})\n'.encode())
            time.sleep(0.5)
            reopen('write failed', current)
