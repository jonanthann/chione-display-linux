#!/usr/bin/env python3
"""
chione_temp_display_win.py

Windows companion to chione_temp_display.py: shows the real CPU temperature
on the digital display of a Gamdias Chione E4 AIO liquid cooler, without
Gamdias's ZEUS CAST software.

The USB HID protocol is identical to the Linux version (see the docstring in
../chione_temp_display.py for the full reverse-engineered packet layout).
Only the two platform-specific pieces differ:

  1. Talking to the device. Windows has no /dev/hidraw, so this uses the
     `hidapi` package. Windows also expects a leading Report ID byte on HID
     writes, so the 64-byte report is sent as 65 bytes with a 0x00 prefix.

  2. Reading the CPU temperature. Windows has no hwmon equivalent, and CPU
     die temperature is not readable from user mode without a kernel driver.
     This script therefore reads it from LibreHardwareMonitor's built-in web
     server (free and open source), which does the privileged part.

Setup
-----
1. Install the HID library:

       pip install hidapi

2. Install LibreHardwareMonitor:
       https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases

   Run it, then enable:  Options -> Remote Web Server -> Run
   (default port 8085). Leave it running in the background; it must be
   running whenever this script runs. Running it as administrator is
   recommended so it can load its sensor driver.

   Verify it works by opening http://localhost:8085/data.json in a browser -
   you should see a wall of JSON.

3. Run this script:

       python chione_temp_display_win.py

   Optional: make it start with Windows by creating a shortcut to
   `pythonw chione_temp_display_win.py` in the folder that opens when you
   press Win+R and enter:  shell:startup
   (pythonw runs it without a console window.)

Usage
-----
    python chione_temp_display_win.py                  # normal run
    python chione_temp_display_win.py --list-sensors   # show sensors + exit
    python chione_temp_display_win.py --sensor "CPU Package"
    python chione_temp_display_win.py --debug          # hexdump replies

Press Ctrl+C to stop; the display is blanked ("--.-") on exit so a frozen
reading is not mistaken for a live one.
"""

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

# chione_core.py lives in the parent folder in the repo, or next to this
# script if it was copied alongside.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (os.path.dirname(_HERE), _HERE):
    if _path not in sys.path:
        # append, not insert: never shadow a stdlib module.
        sys.path.append(_path)

try:
    from chione_core import (  # noqa: F401 (re-exported for tests and callers)
        BLANK,
        CHIONE_PID,
        CHIONE_VID,
        LOG_EVERY_SECONDS,
        MAX_INTERVAL_SECONDS,
        MIN_INTERVAL_SECONDS,
        RECONNECT_DELAY_SECONDS,
        STALE_AFTER_SECONDS,
        UPDATE_INTERVAL_SECONDS,
        DisplayState,
        __version__,
        blank_packet,
        check_interval,
        format_response,
        make_update_packet,
        run_test_pattern,
        temp_to_digits,
        warn_once,
    )
except ImportError:
    sys.exit("chione_core.py not found. Copy it next to this script (it is in "
             "the repository root, one folder up).")

try:
    import hid
except ImportError:
    sys.exit("The 'hidapi' package is required on Windows.\n"
             "Install it with:  pip install hidapi")

# ---------------------------------------------------------------------------
# DEFAULTS (all overridable via command-line flags; see --help)
# ---------------------------------------------------------------------------

# LibreHardwareMonitor's web server (Options -> Remote Web Server -> Run).
LHM_URL = "http://localhost:8085/data.json"

# Sensor names to look for, in order of preference. The first temperature
# sensor whose name contains one of these (case-insensitive) wins.
# "Tctl/Tdie" is AMD Ryzen; "CPU Package" is Intel.
SENSOR_PREFERENCE = ["Tctl/Tdie", "CPU Package", "Core (Tctl", "CPU Total",
                     "Package", "CPU"]

# --log-file is rotated to PATH.1 when it is larger than this on startup.
MAX_LOG_BYTES = 1024 * 1024

# Message kept across successful writes so reconnects do not repeat it.
WRITING_MSG = "Writing to device: Chione E4 display"


def write_report(device, packet):
    """Send a 64-byte report. Windows HID writes need a leading Report ID."""
    # hidapi signals a failed write (e.g. the cooler was unplugged) by
    # returning -1, not by raising, so turn it into an exception here.
    if device.write(b"\x00" + packet) < 0:
        raise OSError("HID write failed")


# ---------------------------------------------------------------------------
# Temperature source: LibreHardwareMonitor's JSON web server
# ---------------------------------------------------------------------------

def fetch_lhm_tree(url, timeout=2.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def parse_value(text):
    """Parse LHM's formatted value, e.g. '45,0 °C' or '45.0 °C' -> 45.0.

    LHM formats numbers with the machine's locale, so a Brazilian/European
    Windows yields a comma decimal separator.
    """
    if not text:
        return None
    cleaned = []
    for ch in text:
        if ch.isdigit() or ch in ".,-":
            cleaned.append("." if ch == "," else ch)
        elif cleaned:
            break  # stop at the unit suffix
    try:
        return float("".join(cleaned))
    except ValueError:
        return None


def walk_temperature_sensors(node, path=()):
    """Yield (full_path, name, celsius) for every temperature sensor.

    LHM's JSON is a tree of {"Text": ..., "Value": ..., "Children": [...]}.
    Temperature sensors are leaves whose Value ends in an actual degree unit,
    grouped under a "Temperatures" node.
    """
    text = node.get("Text") or ""
    children = node.get("Children") or []
    here = path + (text,) if text else path

    if not children:
        value = node.get("Value") or ""
        is_f = "°F" in value
        if "°C" in value or is_f or value.strip().endswith("C"):
            celsius = parse_value(value)
            if celsius is not None:
                if is_f:
                    celsius = (celsius - 32) * 5 / 9
                yield here, text, celsius
        return

    for child in children:
        yield from walk_temperature_sensors(child, here)


def _is_cpu_path(path):
    return any("cpu" in part.lower() or "ryzen" in part.lower()
               or "intel" in part.lower() for part in path)


def pick_cpu_sensor(sensors, wanted=None):
    """Choose which temperature sensor to display.

    `sensors` is a list of (path, name, celsius). With --sensor, match that
    name; otherwise use SENSOR_PREFERENCE, restricted to sensors that live
    under a CPU node. If none does, fall back to all sensors (so a
    motherboard/GPU probe may be picked; main() warns in that case).
    """
    if wanted:
        low = wanted.lower()
        for path, name, _ in sensors:
            if low in name.lower() or low in "/".join(path).lower():
                return path, name
        return None

    cpu_sensors = [(p, n) for p, n, _ in sensors
                   if _is_cpu_path(p)]
    pool = cpu_sensors or [(p, n) for p, n, _ in sensors]

    for preference in SENSOR_PREFERENCE:
        low = preference.lower()
        for path, name in pool:
            if low in name.lower():
                return path, name
    return pool[0] if pool else None


def read_sensor(url, path, name):
    """Re-read one sensor's current value by its path+name."""
    for spath, sname, celsius in walk_temperature_sensors(fetch_lhm_tree(url)):
        if spath == path and sname == name:
            return celsius
    return None


def list_sensors(url):
    try:
        tree = fetch_lhm_tree(url)
    except (urllib.error.URLError, OSError, ValueError,
                http.client.HTTPException) as exc:
        print(f"Could not reach LibreHardwareMonitor at {url}: {exc}",
              file=sys.stderr)
        return 1
    found = False
    for path, name, celsius in walk_temperature_sensors(tree):
        print(f"{celsius:6.1f} C   {name}   [{' / '.join(path[:-1])}]")
        found = True
    if not found:
        print("No temperature sensors found. Is LibreHardwareMonitor running "
              "as administrator?", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------

def open_display(warned=None):
    """Open the Chione display, or return None if it isn't present."""
    if not hid.enumerate(CHIONE_VID, CHIONE_PID):
        return None
    device = hid.device()
    try:
        device.open(CHIONE_VID, CHIONE_PID)
    except OSError as exc:
        # Present but not openable yet (still enumerating, or held
        # exclusively by ZEUS CAST): treat as absent and retry later.
        msg = f"Could not open the display ({exc})."
        if warned is None:
            print(msg, file=sys.stderr, flush=True)
        else:
            warn_once(warned, msg, sys.stderr)
        return None
    device.set_nonblocking(True)
    return device


def run(device, url, sensor, wanted, interval, debug, warned=None):
    """Drive the display until the device is lost or the user stops it.

    `sensor` is a mutable dict ("path", "name") so a sensor re-resolved here
    survives reconnects. `warned` is a set of error messages already
    reported; it is cleared after a successful write.
    """
    if warned is None:
        warned = set()
    state = DisplayState(interval, time.monotonic())
    prev_resp = None

    while True:
        try:
            state.reading(read_sensor(url, sensor["path"], sensor["name"]),
                          time.monotonic())
        except (urllib.error.URLError, OSError, ValueError,
                http.client.HTTPException):
            # LibreHardwareMonitor restarted or is briefly unreachable:
            # keep showing the last good value rather than dying.
            pass

        stale = state.is_stale(time.monotonic())
        if state.idle(stale):
            time.sleep(interval)
            continue

        digits = state.digits(stale)
        # If LibreHardwareMonitor has been gone for a while (or the sensor
        # vanished) the display is blanked, not frozen. After an LHM restart
        # the sensor may be renamed or reordered, so look for it again (at
        # most once per reconnect delay).
        if stale and state.resolve_due(time.monotonic()):
            try:
                sensors = list(walk_temperature_sensors(fetch_lhm_tree(url)))
                found = pick_cpu_sensor(sensors, wanted)
                # Without --sensor, a CPU sensor must not be replaced by
                # the non-CPU fallback (it would stick to it).
                if (found and not wanted and _is_cpu_path(sensor["path"])
                        and not _is_cpu_path(found[0])):
                    found = None
            except (urllib.error.URLError, OSError, ValueError,
                    http.client.HTTPException):
                found = None
            if found and found != (sensor["path"], sensor["name"]):
                sensor["path"], sensor["name"] = found
                print(f"Sensor changed, now reading: {sensor['name']} "
                      f"[{' / '.join(sensor['path'][:-1])}]", flush=True)
        try:
            write_report(device, make_update_packet(*digits))
            resp = device.read(64)
        except (OSError, ValueError) as exc:
            warn_once(warned, f"Device I/O failed ({exc}); will reconnect.")
            return "lost"
        # A successful write re-arms warnings (WRITING_MSG is kept so
        # reconnects do not repeat it).
        warned.intersection_update({WRITING_MSG})

        if debug and resp:
            resp = bytes(resp)
            for line in format_response(resp, prev_resp):
                print(line, flush=True)
            prev_resp = resp

        if state.log_due(digits, stale, time.monotonic(), debug):
            if stale:
                print(f"Cannot read '{sensor['name']}' from {url}; display "
                      "blanked.", file=sys.stderr, flush=True)
            else:
                print(state.shown_text(digits), flush=True)

        time.sleep(interval)


def run_test():
    """Self-test: 88.8 then 12.3 for 2s each, then blank. Returns an exit code."""
    device = open_display()
    if device is None:
        print("Chione display not found or could not be opened.",
              file=sys.stderr)
        return 1
    try:
        run_test_pattern(lambda pkt: write_report(device, pkt))
    except OSError as exc:
        print(f"Cannot write to the display: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            device.close()
        except OSError:
            pass
    return 0


def redirect_output(path):
    """Send stdout and stderr to `path` (append, line-buffered).

    For pythonw, which has no console (sys.stdout/stderr are None). A file
    over MAX_LOG_BYTES is rotated to path.1 first so it cannot grow forever.
    """
    try:
        if os.path.getsize(path) > MAX_LOG_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass  # missing file is the normal first run
    log = open(path, "a", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115 (kept open for the process lifetime)
    sys.stdout = log
    sys.stderr = log


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Show real CPU temperature on a Gamdias Chione E4 "
                    "cooler's display on Windows (no ZEUS CAST).")
    p.add_argument("--url", default=LHM_URL,
                   help="LibreHardwareMonitor JSON URL (default: %(default)s)")
    p.add_argument("--sensor", default=None,
                   help="sensor name to display (default: auto-detect the "
                        "CPU package/Tctl sensor)")
    p.add_argument("--interval", type=float, default=UPDATE_INTERVAL_SECONDS,
                   help="seconds between updates (default: %(default)s)")
    p.add_argument("--debug", action="store_true",
                   help="hexdump the device's response report (for RE work)")
    p.add_argument("--list-sensors", action="store_true",
                   help="list every temperature sensor, then exit")
    p.add_argument("--wait", type=float, default=0.0, metavar="SECONDS",
                   help="wait this long for LibreHardwareMonitor to come up "
                        "instead of failing immediately; use -1 to wait "
                        "forever (recommended when starting at login)")
    p.add_argument("--test", action="store_true",
                   help="show 88.8 then 12.3 for 2s each, blank and exit "
                        "(no sensor needed)")
    p.add_argument("--log-file", default=None, metavar="PATH",
                   help="append stdout and stderr to this file (for pythonw, "
                        "which has no console); rotated to PATH.1 above 1 MB")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    check_interval(p, args.interval)
    if args.wait < 0 and args.wait != -1:
        p.error("--wait must be -1 (forever) or >= 0")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.log_file:
        try:
            redirect_output(args.log_file)
        except OSError as exc:
            print(f"Cannot open log file {args.log_file}: {exc}",
                  file=sys.stderr)
            return 2

    if args.test:
        return run_test()

    if args.list_sensors:
        return list_sensors(args.url)

    # Resolve which sensor to show, once, at startup. When this runs at login
    # LibreHardwareMonitor may still be starting, so wait for it instead of
    # giving up (--wait 0 restores fail-fast for interactive runs).
    sensors = None
    warned = False
    # --wait  <0 waits forever, 0 fails immediately, N waits up to N seconds.
    deadline = None if args.wait < 0 else time.monotonic() + args.wait
    try:
        while True:
            try:
                sensors = list(walk_temperature_sensors(fetch_lhm_tree(args.url)))
                break
            except (urllib.error.URLError, OSError, ValueError,
                    http.client.HTTPException) as exc:
                if deadline is not None and time.monotonic() >= deadline:
                    print(f"Could not reach LibreHardwareMonitor at {args.url}: "
                          f"{exc}", file=sys.stderr)
                    print("Start LibreHardwareMonitor and enable "
                          "Options -> Remote Web Server -> Run.", file=sys.stderr)
                    return 2
                if not warned:
                    print(f"Waiting for LibreHardwareMonitor at {args.url}...",
                          flush=True)
                    warned = True
                time.sleep(RECONNECT_DELAY_SECONDS)
    except KeyboardInterrupt:
        return 0

    chosen = pick_cpu_sensor(sensors, args.sensor)
    if not chosen:
        if args.sensor:
            print(f"No temperature sensor matches --sensor "
                  f"{args.sensor!r}.", file=sys.stderr)
            print("Note that AMD CPUs report 'Core (Tctl/Tdie)' while Intel "
                  "reports 'CPU Package'.", file=sys.stderr)
            cpu_names = [n for p, n, _ in sensors
                         if _is_cpu_path(p)]
            if cpu_names:
                print("CPU sensors on this machine: "
                      + ", ".join(repr(n) for n in cpu_names), file=sys.stderr)
            print("Or just run without --sensor to auto-detect.",
                  file=sys.stderr)
        else:
            print("No suitable CPU temperature sensor found. See what is "
                  "available with --list-sensors.", file=sys.stderr)
        return 2
    sensor_path, sensor_name = chosen
    sensor = {"path": sensor_path, "name": sensor_name}
    if not args.sensor and not _is_cpu_path(sensor_path):
        print("Warning: no CPU sensor was identified; using "
              f"{sensor_name!r} [{' / '.join(sensor_path[:-1])}]. Check "
              "with --list-sensors and pass --sensor if this is wrong.",
              file=sys.stderr)
    print(f"Reading temperature from: {sensor_name} "
          f"[{' / '.join(sensor_path[:-1])}]", flush=True)
    print("Ctrl+C to stop.", flush=True)

    device = None
    warned = set()
    try:
        while True:
            if device is None:
                device = open_display(warned)
                if device is None:
                    warn_once(warned,
                               f"Chione display ({CHIONE_VID:04X}:"
                               f"{CHIONE_PID:04X}) not found - is it plugged "
                               "in? Retrying...")
                    time.sleep(RECONNECT_DELAY_SECONDS)
                    continue
                warn_once(warned, WRITING_MSG)

            run(device, args.url, sensor, args.sensor,
                args.interval, args.debug, warned)

            # run() only returns when the device was lost; drop it and retry.
            try:
                device.close()
            except OSError:
                pass
            device = None
            time.sleep(RECONNECT_DELAY_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        if device is not None:
            # Blank the display so a frozen value isn't mistaken for a live one.
            try:
                write_report(device, blank_packet())
            except OSError:
                pass
            try:
                device.close()
            except OSError:
                pass

    print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
