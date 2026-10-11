"""
chione_core.py

Code shared by chione_temp_display.py (Linux) and
windows/chione_temp_display_win.py (Windows): the USB identity, the packet
format, the display digits, the --test pattern and the pure decisions of the
display loop (stale detection, rate-limited logging). Everything that touches
a platform (sysfs/hidraw, hidapi, LibreHardwareMonitor) stays in the scripts.

Standard library only. See the docstring of chione_temp_display.py for the
reverse-engineered packet layout.
"""

import math
import time

__version__ = "1.1.0"

# USB identity of the Chione display.
CHIONE_VID = 0x1B80
CHIONE_PID = 0xB53C

# How often to refresh the display, in seconds.
UPDATE_INTERVAL_SECONDS = 0.5
# Floor for --interval: the sensor updates about once a second, so anything
# faster only floods the USB link with identical writes.
MIN_INTERVAL_SECONDS = 0.1
# Upper bound for --interval (also keeps sleeps finite).
MAX_INTERVAL_SECONDS = 60.0

# How long to wait before retrying after the device disappears / isn't found.
RECONNECT_DELAY_SECONDS = 3.0

# If the sensor cannot be read for this long, blank the display instead of
# freezing the last good value on it (which would look live).
STALE_AFTER_SECONDS = 5.0

# Outside --debug, log the shown value at most this often (seconds).
LOG_EVERY_SECONDS = 60.0

# Any non-digit byte blanks a display position (renders as a dash).
BLANK = 0x20


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


def blank_packet():
    return make_update_packet(BLANK, BLANK, BLANK)


def check_interval(parser, interval):
    """Reject an --interval outside the allowed range via parser.error."""
    if not math.isfinite(interval) or not (
            MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS):
        parser.error(f"--interval must be between {MIN_INTERVAL_SECONDS} and "
                     f"{MAX_INTERVAL_SECONDS:.0f}")


def warn_once(warned, msg, file=None):
    """Print msg unless it was already reported since the last success."""
    if msg not in warned:
        print(msg, file=file, flush=True)
        warned.add(msg)


def run_test_pattern(write, sleep=None):
    """--test sequence: 88.8 then 12.3 for 2s each, then blank.

    `write` takes a 64-byte packet and may raise OSError.
    """
    sleep = sleep or time.sleep
    for value in (88.8, 12.3):
        write(make_update_packet(*temp_to_digits(value)))
        print(f"Showing {value}", flush=True)
        sleep(2)
    write(blank_packet())


def format_response(resp, prev_resp):
    """Lines to print for a --debug device reply (hexdump plus changed bytes)."""
    lines = [f"  resp: {resp.hex(' ')}"]
    if prev_resp is not None and len(prev_resp) == len(resp):
        changed = [i for i in range(len(resp)) if resp[i] != prev_resp[i]]
        if changed:
            lines.append(f"  changed offsets vs prev: {changed}")
    return lines


class DisplayState:
    """Pure decisions of the display loop: what to show, when to blank, when
    to log and when to look for the sensor again. Time is passed in, so the
    class never reads a clock or touches a device."""

    def __init__(self, interval, now):
        self.interval = interval
        self.last_temp = None
        self.last_good = now
        self.last_resolve = 0.0
        self.last_digits = None
        self.logged_digits = None
        self.last_log = None
        self.last_stale = None

    def reading(self, temp, now):
        """Record a successful sensor reading (None means no reading)."""
        if temp is not None:
            self.last_temp = temp
            self.last_good = now

    def is_stale(self, now):
        return now - self.last_good > max(STALE_AFTER_SECONDS,
                                          2 * self.interval)

    def idle(self, stale):
        """Never read yet and not stale: leave the display untouched."""
        return self.last_temp is None and not stale

    def digits(self, stale):
        if stale:
            return (BLANK, BLANK, BLANK)
        return temp_to_digits(self.last_temp)

    def resolve_due(self, now):
        """True at most once per reconnect delay (and marks it as done)."""
        if now - self.last_resolve >= RECONNECT_DELAY_SECONDS:
            self.last_resolve = now
            return True
        return False

    def log_due(self, digits, stale, now, debug):
        """Whether to log this iteration, updating the log bookkeeping.

        Outside --debug, log at most every LOG_EVERY_SECONDS, but at once on
        the first reading and on stale transitions. With --debug, log every
        change of the shown digits.
        """
        important = self.last_stale is None or stale != self.last_stale
        due = self.last_log is None or now - self.last_log >= LOG_EVERY_SECONDS
        log = (debug and digits != self.last_digits) or important or (
            due and not debug and digits != self.logged_digits)
        if log:
            self.last_log = now
            self.logged_digits = digits
        self.last_digits = digits
        self.last_stale = stale
        return log

    def shown_text(self, digits):
        return (f"CPU: {self.last_temp:.1f}C -> display: "
                f"{digits[0]}{digits[1]}.{digits[2]}")
