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

# --mode auto: seconds on each mode, and the allowed range.
DEFAULT_CYCLE_SECONDS = 5.0
MIN_CYCLE_SECONDS = 1.0
MAX_CYCLE_SECONDS = 60.0

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


def _split_digits(value, blank_leading):
    """Split 0..9999 into 4 digits; blank_leading zeros of the highest
    position become BLANK (the app only blanks the first digit)."""
    n = max(0, min(9999, int(value)))
    d = (n // 1000, n // 100 % 10, n // 10 % 10, n % 10)
    if blank_leading and d[0] == 0:
        d = (BLANK,) + d[1:]
    return d


def temp_display_digits(temp_c, fahrenheit=False):
    """Four digits (hundreds, tens, units, tenths) for a temperature.

    The hundreds position is BLANK below 100 and the range is 0..999.9, like
    ZEUS CAST. With fahrenheit the value is converted first (1.8*t+32).
    """
    t = temp_c * 1.8 + 32 if fahrenheit else temp_c
    # Negative values clamp to 00.0 (same as the official app).
    if not math.isfinite(t):
        t = 999.9 if t > 0 else 0.0
    return _split_digits(round(t * 10), True)


def rpm_to_digits(rpm):
    """Four digits for a fan speed (clamped to 0..9999, thousands BLANK
    below 1000)."""
    if not math.isfinite(rpm):
        rpm = 9999 if rpm > 0 else 0
    return _split_digits(round(rpm), True)


def _finish(pkt):
    pkt[12] = sum(pkt[0:12]) & 0xFF
    return bytes(pkt)


def make_packet(digits, dot=True, celsius=True, icon=True, fan=False,
                flash=False):
    """Build the 64-byte update packet (see the protocol in
    chione_temp_display.py). The checksum is computed."""
    pkt = bytearray(64)
    pkt[0:3] = bytes([0x3A, 0xB5, 0x01])
    pkt[3:7] = bytes(digits)
    pkt[7] = 1 if dot else 0
    pkt[8] = 1 if celsius else 0
    pkt[9] = 1 if icon else 0
    pkt[10] = 1 if fan else 0
    pkt[11] = 1 if flash else 0
    return _finish(pkt)


def make_update_packet(tens, units, tenths, hundreds=BLANK, **kwargs):
    """CPU temperature packet (dot on, Celsius, CPU icon unless overridden)."""
    return make_packet((hundreds, tens, units, tenths), **kwargs)


def make_fan_packet(digits):
    """FAN mode packet: no dot, no unit, no CPU icon, mode byte 1."""
    return make_packet(digits, dot=False, celsius=False, icon=False, fan=True)


def blank_packet(fan=False):
    if fan:
        return make_fan_packet((BLANK,) * 4)
    return make_update_packet(BLANK, BLANK, BLANK)


def init_packet():
    """The "get" packet ZEUS CAST sends once at startup (DeviceID 0xFF)."""
    pkt = bytearray(64)
    pkt[0:13] = bytes([0x3A, 0xB5, 0xFF, BLANK, BLANK, BLANK, BLANK,
                       0x00, 0x01, 0x01, 0x00, 0x00, 0x00])
    return _finish(pkt)


def parse_init_response(resp):
    """Decode the reply to init_packet(), or None if it is too short.

    Wire bytes: 3-4 firmware (high, low), 5 saved unit (1 = Celsius),
    6 = in CPU mode, 7 = in FAN mode.
    """
    if resp is None or len(resp) < 8:
        return None
    return {"firmware": f"{resp[3]}.{resp[4]}",
            "unit": "C" if resp[5] == 1 else "F",
            "mode": "FAN" if resp[7] == 1 else "CPU" if resp[6] == 1 else "?"}


def do_init(write, read, debug):
    """Send the init packet and read the reply (never required to succeed).

    `write` may raise OSError (propagated); `read` returns bytes or None.
    With debug, prints the firmware version and the saved mode/unit.
    """
    write(init_packet())
    info = parse_init_response(read())
    if not debug:
        return
    if info is None:
        print("  init: no reply from the device", flush=True)
    else:
        print(f"  init: firmware {info['firmware']}, saved mode "
              f"{info['mode']}, saved unit {info['unit']}", flush=True)


def pick_mode(mode, now, start, cycle, fan_available):
    """Mode to show now: "cpu" or "fan". auto alternates every `cycle`
    seconds, and stays on cpu when there is no fan source."""
    if mode == "auto":
        if not fan_available:
            return "cpu"
        return "fan" if int((now - start) // cycle) % 2 else "cpu"
    return mode


def check_interval(parser, interval):
    """Reject an --interval outside the allowed range via parser.error."""
    if not math.isfinite(interval) or not (
            MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS):
        parser.error(f"--interval must be between {MIN_INTERVAL_SECONDS} and "
                     f"{MAX_INTERVAL_SECONDS:.0f}")


def check_cycle(parser, cycle):
    """Reject a --cycle outside MIN_CYCLE_SECONDS..MAX_CYCLE_SECONDS."""
    if not math.isfinite(cycle) or not (
            MIN_CYCLE_SECONDS <= cycle <= MAX_CYCLE_SECONDS):
        parser.error(f"--cycle must be between {MIN_CYCLE_SECONDS:.0f} and "
                     f"{MAX_CYCLE_SECONDS:.0f}")


def check_alarm(parser, alarm):
    """Reject a non-finite --alarm."""
    if alarm is not None and not math.isfinite(alarm):
        parser.error("--alarm must be a finite temperature in Celsius")


def add_display_args(p):
    """The display options shared by both scripts."""
    p.add_argument("--mode", choices=("cpu", "fan", "auto"), default="cpu",
                   help="what to show: CPU temperature, fan RPM, or both "
                        "alternating (default: %(default)s)")
    p.add_argument("--cycle", type=float, default=DEFAULT_CYCLE_SECONDS,
                   help="with --mode auto, seconds per mode "
                        "(default: %(default)s)")
    p.add_argument("--fahrenheit", action="store_true",
                   help="show Fahrenheit in cpu mode")
    p.add_argument("--alarm", type=float, default=None, metavar="CELSIUS",
                   help="flash the display when the CPU temperature reaches "
                        "this value (default: off)")


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

    def __init__(self, interval, now, kind="cpu", fahrenheit=False,
                 alarm=None):
        self.interval = interval
        self.kind = kind  # "cpu" (temperature in C) or "fan" (RPM)
        self.fahrenheit = fahrenheit
        self.alarm = alarm
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
            return (BLANK,) * 4
        if self.kind == "fan":
            return rpm_to_digits(self.last_temp)
        return temp_display_digits(self.last_temp, self.fahrenheit)

    def packet(self, stale):
        """The packet for the current state (blank when stale)."""
        fan = self.kind == "fan"
        if stale:
            return blank_packet(fan)
        digits = self.digits(False)
        if fan:
            return make_fan_packet(digits)
        flash = self.alarm is not None and self.last_temp >= self.alarm
        return make_packet(digits, celsius=not self.fahrenheit, flash=flash)

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
        text = "".join(str(d) for d in digits if d != BLANK)
        if self.kind == "fan":
            return f"FAN: {self.last_temp:.0f} RPM -> display: {text}"
        unit = "F" if self.fahrenheit else ""
        return (f"CPU: {self.last_temp:.1f}C -> display: "
                f"{text[:-1]}.{text[-1]}{unit}")
