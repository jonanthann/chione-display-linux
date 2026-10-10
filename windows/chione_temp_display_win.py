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
import sys
import time
import urllib.error
import urllib.request

try:
    import hid
except ImportError:
    sys.exit("The 'hidapi' package is required on Windows.\n"
             "Install it with:  pip install hidapi")

# ---------------------------------------------------------------------------
# DEFAULTS (all overridable via command-line flags; see --help)
# ---------------------------------------------------------------------------

CHIONE_VID = 0x1B80
CHIONE_PID = 0xB53C

# LibreHardwareMonitor's web server (Options -> Remote Web Server -> Run).
LHM_URL = "http://localhost:8085/data.json"

# Sensor names to look for, in order of preference. The first temperature
# sensor whose name contains one of these (case-insensitive) wins.
# "Tctl/Tdie" is AMD Ryzen; "CPU Package" is Intel.
SENSOR_PREFERENCE = ["Tctl/Tdie", "CPU Package", "Core (Tctl", "CPU Total",
                     "Package", "CPU"]

UPDATE_INTERVAL_SECONDS = 0.5
# Floor for --interval: the sensor updates about once a second, so anything
# faster only floods the USB link with identical writes.
MIN_INTERVAL_SECONDS = 0.1
RECONNECT_DELAY_SECONDS = 3.0

# If the sensor cannot be read for this long, blank the display instead of
# freezing the last good value on it (which would look live).
STALE_AFTER_SECONDS = 5.0

# Any non-digit byte blanks a display position (renders as a dash).
BLANK = 0x20


# ---------------------------------------------------------------------------
# Protocol (identical to the Linux version)
# ---------------------------------------------------------------------------

def temp_to_digits(temp_c):
    """Clamp to the display's two-digit-plus-decimal range and split into
    (tens, units, tenths)."""
    t = max(0.0, min(99.9, round(temp_c, 1)))
    whole = int(t)
    tenths = int(round((t - whole) * 10))
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

def open_display():
    """Open the Chione display, or return None if it isn't present."""
    if not hid.enumerate(CHIONE_VID, CHIONE_PID):
        return None
    device = hid.device()
    try:
        device.open(CHIONE_VID, CHIONE_PID)
    except OSError as exc:
        # Present but not openable yet (still enumerating, or held
        # exclusively by ZEUS CAST): treat as absent and retry later.
        print(f"Could not open the display ({exc}).", file=sys.stderr,
              flush=True)
        return None
    device.set_nonblocking(True)
    return device


def run(device, url, sensor_path, sensor_name, wanted, interval, debug):
    """Drive the display until the device is lost or the user stops it."""
    last_digits = None
    last_temp = None
    last_good = time.monotonic()
    prev_resp = None
    last_resolve = 0.0

    while True:
        try:
            temp = read_sensor(url, sensor_path, sensor_name)
            if temp is not None:
                last_temp = temp
                last_good = time.monotonic()
        except (urllib.error.URLError, OSError, ValueError,
                http.client.HTTPException):
            # LibreHardwareMonitor restarted or is briefly unreachable:
            # keep showing the last good value rather than dying.
            pass

        stale = (time.monotonic() - last_good
                 > max(STALE_AFTER_SECONDS, 2 * interval))
        if last_temp is None and not stale:
            time.sleep(interval)
            continue

        if stale:
            # LibreHardwareMonitor has been gone for a while (or the sensor
            # vanished): blank rather than show a frozen value as live.
            digits = (BLANK, BLANK, BLANK)
            # After an LHM restart the sensor may be renamed or reordered, so
            # try to find it again (at most once per reconnect delay).
            if time.monotonic() - last_resolve >= RECONNECT_DELAY_SECONDS:
                last_resolve = time.monotonic()
                try:
                    found = pick_cpu_sensor(
                        list(walk_temperature_sensors(fetch_lhm_tree(url))),
                        wanted)
                except (urllib.error.URLError, OSError, ValueError,
                        http.client.HTTPException):
                    found = None
                if found and found != (sensor_path, sensor_name):
                    sensor_path, sensor_name = found
                    print(f"Sensor changed, now reading: {sensor_name} "
                          f"[{' / '.join(sensor_path[:-1])}]", flush=True)
        else:
            digits = temp_to_digits(last_temp)
        try:
            write_report(device, make_update_packet(*digits))
            resp = device.read(64)
        except (OSError, ValueError) as exc:
            print(f"Device I/O failed ({exc}); will reconnect.", flush=True)
            return "lost"

        if debug and resp:
            resp = bytes(resp)
            print(f"  resp: {resp.hex(' ')}", flush=True)
            if prev_resp is not None and len(prev_resp) == len(resp):
                changed = [i for i in range(len(resp)) if resp[i] != prev_resp[i]]
                if changed:
                    print(f"  changed offsets vs prev: {changed}", flush=True)
            prev_resp = resp

        if digits != last_digits:
            if stale:
                print(f"Cannot read '{sensor_name}' from {url}; display "
                      "blanked.", file=sys.stderr, flush=True)
            else:
                print(f"CPU: {last_temp:.1f}C -> display: "
                      f"{digits[0]}{digits[1]}.{digits[2]}", flush=True)
            last_digits = digits

        time.sleep(interval)


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
    args = p.parse_args(argv)
    if args.interval < MIN_INTERVAL_SECONDS:
        p.error(f"--interval must be at least {MIN_INTERVAL_SECONDS}")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

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
    if not args.sensor and not _is_cpu_path(sensor_path):
        print("Warning: no CPU sensor was identified; using "
              f"{sensor_name!r} [{' / '.join(sensor_path[:-1])}]. Check "
              "with --list-sensors and pass --sensor if this is wrong.",
              file=sys.stderr)
    print(f"Reading temperature from: {sensor_name} "
          f"[{' / '.join(sensor_path[:-1])}]", flush=True)
    print("Ctrl+C to stop.", flush=True)

    device = None
    try:
        while True:
            if device is None:
                device = open_display()
                if device is None:
                    print(f"Chione display ({CHIONE_VID:04X}:{CHIONE_PID:04X}) "
                          "not found - is it plugged in? Retrying...",
                          flush=True)
                    time.sleep(RECONNECT_DELAY_SECONDS)
                    continue
                print("Writing to device: Chione E4 display", flush=True)

            run(device, args.url, sensor_path, sensor_name, args.sensor,
                args.interval, args.debug)

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
                write_report(device, make_update_packet(BLANK, BLANK, BLANK))
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
