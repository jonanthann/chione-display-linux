#!/usr/bin/env python3
"""
chione_temp_display.py

Reads the real CPU temperature from Linux (via a hwmon driver, k10temp by
default for AMD Ryzen CPUs) and pushes it to the physical digital display on
the pump block of a Gamdias Chione E4 AIO liquid cooler, over raw USB HID,
entirely without Gamdias's Windows-only ZEUS CAST software and without Wine.

Background
----------
The Chione E4's display is normally driven by ZEUS CAST (Windows only),
which has no native Linux port and does not run reliably under Wine. This
script replaces it: the USB HID protocol below was reverse engineered by
capturing real traffic between ZEUS CAST and the device (via usbmon/tcpdump)
while running it under Wine, patched just enough to reach the point of
talking to the hardware.

Device identification
----------------------
Vendor ID:  0x1b80
Product ID: 0xb53c
(shows up in `lsusb` as "Afatech CHIONE Digital Display")

The hidraw node number (/dev/hidrawN) is NOT stable: it is assigned in USB
enumeration order and changes across reboots and replugs. The USB VID:PID is
stable, so by default this script auto-discovers the node by scanning
/sys/class/hidraw for the device matching 1b80:b53c (see find_hidraw_device).

Protocol (reverse engineered, digit display mode only)
-------------------------------------------------------
64-byte USB HID Interrupt OUT report, no Report ID, sent periodically to
keep the display alive and updated:

    byte 0-1   : 0x3a 0xb5     fixed header / magic bytes
    byte 2     : 0x01          command: "update display"
                                (0xff was observed once, at device startup,
                                as a distinct "init" variant; not required
                                for basic continuous use)
    byte 3     : 0x20          fixed in every capture (exact purpose not
                                confirmed, possibly a length/sub-command
                                field)
    byte 4     : tens digit    0-9 shows that digit; any other value
                                (e.g. 0x20) blanks the position (shown as a
                                dash on the display)
    byte 5     : units digit   same rule as byte 4
    byte 6     : decimal digit same rule as byte 4 (shown after the
                                decimal point)
    byte 7     : 0x01          fixed in every capture
    byte 8-11  : 01 01 00 00   fixed in every capture
    byte 12-15 : 13 00 00 00   fixed in every capture (varies slightly
                                between the init/update variants seen;
                                exact meaning not confirmed)
    byte 16-63 : 0x00          padding, always zero in every capture

Input report (device -> host)
-----------------------------
The device returns a 64-byte report after every OUT packet. Its contents are
NOT yet decoded: it may be a bare ACK, or it may carry telemetry (pump/fan
RPM, coolant temp) the way many other AIO HID protocols do. Run with --debug
to hexdump it and watch which bytes move when fan/pump speed changes; that is
the cheapest lead for decoding the unmapped fan-RPM feature. The read is
gated with select() and a timeout so a device that ever stops replying can
never hang the loop.

Only the temperature-digit display mode has been mapped so far. Other modes
the hardware may support (fan RPM, coolant temperature, custom images) have
not been captured or decoded.

Requirements
------------
- Python 3, standard library only (no external packages)
- A Linux kernel with hidraw support (default on virtually every distro)
- Read/write access to the device's /dev/hidrawN node (see udev/ in this
  repo for a rule that grants this without root)
- A CPU with a working hwmon temperature sensor

The CPU sensor is auto-detected too: without --hwmon, the known CPU
temperature drivers are tried in order (see HWMON_CANDIDATES), so the same
command works on AMD and Intel, on any distribution, without editing
anything. On distributions that do not autoload the driver (Debian with an
Intel CPU is the common case) load it once with `sudo modprobe coretemp`.

Usage
-----
    python3 chione_temp_display.py                 # auto-discover everything
    python3 chione_temp_display.py --hwmon coretemp  # pin a specific driver
    python3 chione_temp_display.py --list-hidraw     # list HID devices + exit
    python3 chione_temp_display.py --list-hwmon      # list temp sensors + exit
    python3 chione_temp_display.py --debug           # hexdump device replies

Runs until interrupted (Ctrl+C or SIGTERM), or use
systemd/chione-temp.service to run it automatically in the background. On any
exit the display is blanked ("--.-") so a stale reading is not mistaken for a
live one, and USB replug / suspend-resume are recovered automatically.
"""

import argparse
import glob
import math
import os
import select
import signal
import sys
import time

__version__ = "1.1.0"

# ---------------------------------------------------------------------------
# DEFAULTS (all overridable via command-line flags; see --help)
# ---------------------------------------------------------------------------

# USB identity of the Chione display. Stable across reboots, unlike the
# /dev/hidrawN number, so the node is discovered by this ID at startup.
CHIONE_VID = "1B80"
CHIONE_PID = "B53C"

# hidraw device node. None -> auto-discover by CHIONE_VID:CHIONE_PID.
# Override with --device /dev/hidrawN only for unusual setups.
HIDRAW_DEVICE = None

# hwmon driver name that exposes your CPU temperature. None -> try each of
# HWMON_CANDIDATES in order, so one command works on both AMD and Intel.
# Override with --hwmon NAME to pin a specific driver.
HWMON_DRIVER_NAME = None

# CPU temperature drivers tried, in order, when --hwmon is not given:
#   "k10temp"  -> AMD Ryzen / most modern AMD desktop CPUs
#   "coretemp" -> Intel CPUs
#   "zenpower" -> out-of-tree k10temp replacement some AMD users install
# Motherboard/GPU/NVMe sensors are deliberately absent: they are not the CPU.
HWMON_CANDIDATES = ("k10temp", "coretemp", "zenpower")

# Which temp*_input file under that hwmon device to read. temp1_input is
# usually the main CPU die/Tctl sensor on k10temp, but this can vary by
# motherboard/chipset. Cross check with `sensors` (lm_sensors) if readings
# look wrong.
HWMON_TEMP_FILE = "temp1_input"

# How often to refresh the display, in seconds.
UPDATE_INTERVAL_SECONDS = 0.5
# Floor for --interval: the sensor updates about once a second, so anything
# faster only floods the USB link with identical writes.
MIN_INTERVAL_SECONDS = 0.1

# How long to wait before retrying after the device disappears / isn't found.
RECONNECT_DELAY_SECONDS = 3.0

# If the CPU sensor cannot be read for this long, blank the display instead
# of freezing the last good value on it (which would look live).
STALE_AFTER_SECONDS = 5.0

# Exit code for permanent misconfiguration (no matching hwmon sensor). A
# missing device permission is retried instead, since at boot it is usually
# transient (see main). The systemd unit uses RestartPreventExitStatus=2 to stop
# retrying a hopeless start instead of looping forever.
EXIT_CONFIG_ERROR = 2

# Outside --debug, log the shown value at most this often (seconds).
LOG_EVERY_SECONDS = 60.0

# Upper bound for --interval (also keeps sleeps finite).
MAX_INTERVAL_SECONDS = 60.0
# How long to wait at startup for the sensor to appear (e.g. coretemp loaded
# late) before giving up, and how often to check meanwhile.
SENSOR_WAIT_SECONDS = 60.0
SENSOR_POLL_SECONDS = 2.0

# Any non-digit byte blanks a display position (renders as a dash).
BLANK = 0x20

# ---------------------------------------------------------------------------

# Set by SIGINT/SIGTERM handlers; checked by the loops for a clean shutdown.
_stop = False


def _request_stop(signum, frame):
    global _stop
    _stop = True


def _sleep_interruptible(seconds):
    """Sleep up to `seconds`, but return early if a stop signal arrives."""
    deadline = time.monotonic() + seconds
    while not _stop:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))


def find_hidraw_device(vid, pid):
    """Return /dev/hidrawN for the HID device matching vid:pid, or None.

    Mirrors find_hwmon_temp_path: scans sysfs and matches on stable identity
    (the HID_ID line, e.g. HID_ID=0003:00001B80:0000B53C) instead of trusting
    an unstable device-node number.
    """
    vid, pid = int(vid, 16), int(pid, 16)
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            with open(uevent) as f:
                content = f.read()
        except OSError:
            continue
        for line in content.splitlines():
            if not line.startswith("HID_ID="):
                continue
            # Compare the parsed fields, not substrings: "1B80" must match the
            # vendor field only, never a fragment of some other device's ID.
            try:
                _bus, dev_vid, dev_pid = line.split("=", 1)[1].split(":")
                match = int(dev_vid, 16) == vid and int(dev_pid, 16) == pid
            except ValueError:
                continue
            if match:
                name = os.path.basename(os.path.dirname(os.path.dirname(uevent)))
                return "/dev/" + name
    return None


def list_hidraw():
    """Print every hidraw node with its USB ID and name (for --list-hidraw)."""
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        name = os.path.basename(os.path.dirname(os.path.dirname(uevent)))
        hid_id = hid_name = ""
        try:
            with open(uevent) as f:
                for line in f:
                    if line.startswith("HID_ID="):
                        hid_id = line.strip().split("=", 1)[1]
                    elif line.startswith("HID_NAME="):
                        hid_name = line.strip().split("=", 1)[1]
        except OSError:
            continue
        print(f"/dev/{name}\t{hid_id}\t{hid_name}")


def find_hwmon_temp_path(driver_name, temp_file):
    for hwmon_dir in glob.glob("/sys/class/hwmon/hwmon*"):
        name_path = os.path.join(hwmon_dir, "name")
        try:
            with open(name_path) as f:
                name = f.read().strip()
        except OSError:
            continue
        if name == driver_name:
            candidate = os.path.join(hwmon_dir, temp_file)
            if os.path.exists(candidate):
                return candidate
    return None


def list_hwmon():
    """Print every hwmon temperature sensor with its driver name and label.

    The Linux counterpart of the Windows script's --list-sensors: it answers
    "which --hwmon should I pass?" directly, instead of making the user
    assemble a shell loop over sysfs.
    """
    for hwmon_dir in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(os.path.join(hwmon_dir, "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        for temp_input in sorted(glob.glob(os.path.join(hwmon_dir, "temp*_input"))):
            temp_file = os.path.basename(temp_input)
            label = ""
            try:
                with open(temp_input.replace("_input", "_label")) as f:
                    label = f.read().strip()
            except OSError:
                pass
            try:
                value = f"{read_temp_celsius(temp_input):.1f}C"
            except (OSError, ValueError):
                value = "n/a"
            marker = (" <- default" if name in HWMON_CANDIDATES
                      and temp_file == HWMON_TEMP_FILE else "")
            print(f"{name}\t{temp_file}\t{value}\t{label}{marker}")


def resolve_hwmon_temp_path(driver_name, temp_file):
    """Return (path, driver_name) for the CPU sensor, or (None, None).

    With an explicit driver name only that one is tried; without one, each of
    HWMON_CANDIDATES is tried in order. Mirrors how the device itself is
    found by identity rather than by a hardcoded path.
    """
    candidates = [driver_name] if driver_name else list(HWMON_CANDIDATES)
    for name in candidates:
        path = find_hwmon_temp_path(name, temp_file)
        if path:
            return path, name
    return None, None


def read_temp_celsius(path):
    with open(path) as f:
        milli_c = int(f.read().strip())
    return milli_c / 1000.0


def temp_to_digits(temp_c):
    """Clamp to the display's two-digit-plus-decimal range and split into
    (tens, units, tenths)."""
    t = max(0.0, min(99.9, round(temp_c, 1)))
    whole = int(t)
    tenths = round((t - whole) * 10)
    if tenths == 10:
        whole += 1
        tenths = 0
    return whole // 10, whole % 10, tenths


def make_update_packet(tens, units, tenths):
    pkt = bytearray(64)
    pkt[0:4] = bytes([0x3A, 0xB5, 0x01, 0x20])
    pkt[4] = tens
    pkt[5] = units
    pkt[6] = tenths
    pkt[7] = 0x01
    pkt[8:12] = bytes([0x01, 0x01, 0x00, 0x00])
    pkt[12:16] = bytes([0x13, 0x00, 0x00, 0x00])
    return bytes(pkt)


def _read_response(fd):
    """Non-blocking read of the device's reply, or None if none is ready.

    Gated with select() so a device that stops replying can never hang the
    loop (the reply is unused for display; it's only interesting for --debug).
    """
    r, _, _ = select.select([fd], [], [], 0.2)
    if not r:
        return None
    try:
        return os.read(fd, 64)
    except BlockingIOError:
        return None


def run(device_path, sensor, hwmon, temp_file, interval, debug, warned=None):
    """Drive the display until the device is lost or a stop is requested.

    Returns "stop" on a clean shutdown request, or "lost" if the device
    disappeared (so the caller can wait and reconnect). Blanks the display on
    the way out either way. `sensor` is a mutable dict ("path", "name") so a
    sensor re-resolved here survives reconnects. `warned` is a set of error
    messages already reported; it is cleared after a successful write.
    """
    if warned is None:
        warned = set()
    fd = os.open(device_path, os.O_RDWR)
    try:
        os.set_blocking(fd, False)
    except BaseException:
        os.close(fd)
        raise
    msg = f"Writing to device: {device_path}"
    if msg not in warned:
        print(msg, flush=True)
        warned.add(msg)

    last_digits = None
    logged_digits = None
    last_log = None
    last_stale = None
    last_temp = None
    last_good = time.monotonic()
    prev_resp = None
    last_resolve = 0.0
    try:
        while not _stop:
            try:
                last_temp = read_temp_celsius(sensor["path"])
                last_good = time.monotonic()
            except (OSError, ValueError):
                # Transient sensor hiccup: reuse the last good reading rather
                # than crashing. With none yet, show nothing until stale.
                pass

            stale = (time.monotonic() - last_good
                     > max(STALE_AFTER_SECONDS, 2 * interval))
            if stale:
                # The sensor has been gone for a while (driver unloaded,
                # hwmon renumbered): blank rather than show a frozen value.
                digits = (BLANK, BLANK, BLANK)
                # hwmon may have been renumbered (driver reloaded): look for
                # the sensor again, at most once per reconnect delay.
                if time.monotonic() - last_resolve >= RECONNECT_DELAY_SECONDS:
                    last_resolve = time.monotonic()
                    new_path, new_name = resolve_hwmon_temp_path(hwmon, temp_file)
                    if new_path and new_path != sensor["path"]:
                        sensor["path"], sensor["name"] = new_path, new_name
                        print(f"Sensor changed, now reading: {new_path} "
                              f"({new_name})", flush=True)
            elif last_temp is None:
                # Never read yet and not stale: leave the display untouched.
                _sleep_interruptible(interval)
                continue
            else:
                digits = temp_to_digits(last_temp)
            try:
                os.write(fd, make_update_packet(*digits))
                resp = _read_response(fd)
            except OSError as exc:
                msg = f"Device I/O failed ({exc}); will reconnect."
                if msg not in warned:
                    print(msg, flush=True)
                    warned.add(msg)
                return "lost"
            # A successful write re-arms warnings (the "Writing to device"
            # line is kept in the set so reconnects do not repeat it).
            warned.intersection_update({f"Writing to device: {device_path}"})

            if debug and resp is not None:
                print(f"  resp: {resp.hex(' ')}", flush=True)
                if prev_resp is not None and len(prev_resp) == len(resp):
                    changed = [i for i in range(len(resp)) if resp[i] != prev_resp[i]]
                    if changed:
                        print(f"  changed offsets vs prev: {changed}", flush=True)
                prev_resp = resp

            # Outside --debug, log at most every LOG_EVERY_SECONDS, but at
            # once on first reading and on stale transitions. Flush so
            # `systemctl status` isn't block-buffered.
            now = time.monotonic()
            important = last_stale is None or stale != last_stale
            due = last_log is None or now - last_log >= LOG_EVERY_SECONDS
            if (debug and digits != last_digits) or important or (
                    due and not debug and digits != logged_digits):
                if stale:
                    print(f"Cannot read {sensor['path']}; display blanked.",
                          file=sys.stderr, flush=True)
                else:
                    print(f"CPU: {last_temp:.1f}C -> display: "
                          f"{digits[0]}{digits[1]}.{digits[2]}", flush=True)
                last_log = now
                logged_digits = digits
            last_digits = digits
            last_stale = stale

            _sleep_interruptible(interval)
    finally:
        # Blank the display so a frozen value isn't mistaken for a live one.
        try:
            os.write(fd, make_update_packet(BLANK, BLANK, BLANK))
        except OSError:
            pass
        os.close(fd)

    return "stop"


def run_test(device_path):
    """Self-test: 88.8 then 12.3 for 2s each, then blank. Returns an exit code."""
    try:
        fd = os.open(device_path, os.O_RDWR)
    except OSError as exc:
        print(f"Cannot open {device_path}: {exc}", file=sys.stderr)
        return 1
    try:
        for value in (88.8, 12.3):
            os.write(fd, make_update_packet(*temp_to_digits(value)))
            print(f"Showing {value}", flush=True)
            time.sleep(2)
        os.write(fd, make_update_packet(BLANK, BLANK, BLANK))
    except OSError as exc:
        print(f"Cannot write to {device_path}: {exc}", file=sys.stderr)
        return 1
    finally:
        os.close(fd)
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Show real CPU temperature on a Gamdias Chione E4 "
                    "cooler's display, over raw USB HID (no ZEUS CAST/Wine).")
    p.add_argument("--device", default=HIDRAW_DEVICE,
                   help=f"hidraw node (default: auto-discover by USB ID "
                        f"{CHIONE_VID}:{CHIONE_PID})")
    p.add_argument("--hwmon", default=HWMON_DRIVER_NAME,
                   help="hwmon driver name to pin (default: auto-detect, "
                        "trying " + ", ".join(HWMON_CANDIDATES) + ")")
    p.add_argument("--temp-file", default=HWMON_TEMP_FILE,
                   help="temp*_input file to read (default: %(default)s)")
    p.add_argument("--interval", type=float, default=UPDATE_INTERVAL_SECONDS,
                   help="seconds between updates (default: %(default)s)")
    p.add_argument("--debug", action="store_true",
                   help="hexdump the device's response report (for RE work)")
    p.add_argument("--list-hidraw", action="store_true",
                   help="list all hidraw devices with USB IDs, then exit")
    p.add_argument("--list-hwmon", action="store_true",
                   help="list all hwmon temperature sensors, then exit")
    p.add_argument("--test", action="store_true",
                   help="show 88.8 then 12.3 for 2s each, blank and exit "
                        "(no sensor needed)")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    if not math.isfinite(args.interval) or not (
            MIN_INTERVAL_SECONDS <= args.interval <= MAX_INTERVAL_SECONDS):
        p.error(f"--interval must be between {MIN_INTERVAL_SECONDS} and "
                f"{MAX_INTERVAL_SECONDS:.0f}")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.list_hidraw:
        list_hidraw()
        return 0

    if args.list_hwmon:
        list_hwmon()
        return 0

    if args.test:
        device_path = args.device or find_hidraw_device(CHIONE_VID, CHIONE_PID)
        if not device_path:
            print(f"Chione display ({CHIONE_VID}:{CHIONE_PID}) not found.",
                  file=sys.stderr)
            return 1
        return run_test(device_path)

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    temp_path, hwmon_name = resolve_hwmon_temp_path(args.hwmon, args.temp_file)
    if not temp_path:
        # The driver may simply load late at boot: wait before giving up.
        print(f"Waiting up to {SENSOR_WAIT_SECONDS:.0f}s for a CPU hwmon "
              "sensor...", flush=True)
        deadline = time.monotonic() + SENSOR_WAIT_SECONDS
        while not _stop and time.monotonic() < deadline:
            _sleep_interruptible(SENSOR_POLL_SECONDS)
            temp_path, hwmon_name = resolve_hwmon_temp_path(
                args.hwmon, args.temp_file)
            if temp_path:
                break
    if _stop:
        return 0
    if not temp_path:
        tried = args.hwmon or ", ".join(HWMON_CANDIDATES)
        print(f"Could not find a CPU hwmon sensor exposing "
              f"'{args.temp_file}' (tried: {tried}).", file=sys.stderr)
        print("Available sensors on this machine:", file=sys.stderr)
        list_hwmon()
        print("Pass the driver name you want with --hwmon NAME. If the list "
              "is empty or has no CPU entry, the driver may not be loaded: "
              "try 'sudo modprobe coretemp' (Intel) or 'sudo modprobe "
              "k10temp' (AMD).", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    print(f"Reading temperature from: {temp_path} ({hwmon_name})", flush=True)
    print("Ctrl+C to stop.", flush=True)

    sensor = {"path": temp_path, "name": hwmon_name}
    perm_warned = False
    absent_warned = False
    warned = set()
    while not _stop:
        device_path = args.device or find_hidraw_device(CHIONE_VID, CHIONE_PID)
        if not device_path:
            # Print once per absence so an unplugged cooler does not flood
            # the journal; reset when the device shows up again.
            if not absent_warned:
                print(f"Chione display ({CHIONE_VID}:{CHIONE_PID}) not found - "
                      "is it plugged in? Retrying...", flush=True)
                absent_warned = True
            _sleep_interruptible(RECONNECT_DELAY_SECONDS)
            continue
        absent_warned = False

        try:
            result = run(device_path, sensor, args.hwmon, args.temp_file,
                         args.interval, args.debug, warned)
        except PermissionError:
            # Transient at boot: when this starts as a user service, the
            # logind uaccess ACL may not be applied to the node yet, so the
            # open() briefly fails with EACCES and lands shortly after. Keep
            # retrying instead of giving up (the old behavior killed the
            # service permanently on every boot), but explain it once in case
            # the udev rule is genuinely missing.
            if not perm_warned:
                print(f"Permission denied opening {device_path}; waiting for "
                      "device access to become available...",
                      file=sys.stderr, flush=True)
                print("If this persists, install the udev rule:", file=sys.stderr)
                print("  sudo cp udev/70-chione.rules /etc/udev/rules.d/",
                      file=sys.stderr)
                print("  sudo udevadm control --reload-rules && "
                      "sudo udevadm trigger --subsystem-match=hidraw",
                      file=sys.stderr)
                print("then replug the cooler or reboot (or run as root).",
                      file=sys.stderr, flush=True)
                perm_warned = True
            _sleep_interruptible(RECONNECT_DELAY_SECONDS)
            continue
        except OSError as exc:
            # Node vanished or is not ready (ENODEV, ENXIO, EIO, EBUSY on
            # replug/resume); rediscover and retry.
            msg = f"Cannot open device ({exc}); will retry."
            if msg not in warned:
                print(msg, flush=True)
                warned.add(msg)
            result = "lost"

        if result == "stop":
            break
        _sleep_interruptible(RECONNECT_DELAY_SECONDS)

    print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
