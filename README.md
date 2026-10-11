# Chione E4 Temperature Display

[![CI](https://github.com/jonanthann/chione-display-linux/actions/workflows/ci.yml/badge.svg)](https://github.com/jonanthann/chione-display-linux/actions/workflows/ci.yml)

Drive the physical digital display on a Gamdias Chione E4 (and possibly
other Chione-family) AIO liquid cooler with **real CPU temperature**,
without Gamdias's ZEUS CAST software.

There are two scripts, one per platform, speaking the same reverse-engineered
USB HID protocol:

| Script | Platform | Temperature source | Dependencies |
|--------|----------|--------------------|--------------|
| [`chione_temp_display.py`](chione_temp_display.py) | Linux | `hwmon` (`k10temp` / `coretemp` / `zenpower`, auto-detected) | none, standard library only |
| [`windows/chione_temp_display_win.py`](windows/chione_temp_display_win.py) | Windows | LibreHardwareMonitor web server | `hidapi`, LibreHardwareMonitor |

The update packet is byte-for-byte identical on both; only how the script
reaches the device and where it gets the temperature from differ. The code
they share (packet building, display state, self-test pattern, interval
check) lives in [`chione_core.py`](chione_core.py) at the repository root,
and both scripts need it: it must sit next to the Linux script, and next to
the Windows script or in its parent folder.

```
chione_core.py                    shared code (both platforms)
chione_temp_display.py            Linux script
windows/chione_temp_display_win.py   Windows script
install.sh  systemd/  udev/       Linux setup
tests/                            unit tests
```

## Why this exists

The Chione E4's pump-block display is only ever driven by ZEUS CAST, a
Windows-only .NET/WPF application. There is no official Linux client, and
ZEUS CAST does not run reliably under Wine: among other issues, its
device-detection code makes a WMI query with a `WHERE` clause, which Wine's
WMI implementation cannot execute, and its hardware-sensor SDK requires a
real Windows kernel driver that Wine has no way to provide.

This project bypasses all of that. The USB HID protocol the display speaks
was reverse engineered by capturing real traffic between ZEUS CAST (patched
just enough to get past the Wine-specific failures above) and the cooler,
using `usbmon`/`tcpdump`. The result is a small, dependency-free Python
script that talks to the display directly, no Windows or Wine involved at
runtime.

Once the protocol was known, porting it back to Windows was almost free, so
the repository also ships a Windows script for people who want the display
working without leaving ZEUS CAST running.

## What it does

- Reads the real CPU temperature (on Linux from the `hwmon` subsystem; on
  Windows from LibreHardwareMonitor, since Windows exposes no equivalent)
- **Auto-detects the CPU sensor** on both platforms: on Linux it tries the
  known CPU drivers in order (`k10temp`, `coretemp`, `zenpower`) instead of
  assuming AMD, so the same command works on any distribution and either
  vendor
- Writes it to the cooler's display over raw USB HID, roughly twice a second
- **Auto-discovers** the cooler by its USB ID (`1b80:b53c`), so there is
  nothing to edit and it survives the `/dev/hidrawN` number changing across
  reboots and replugs
- On Windows: picks the sensor by name, preferring `Tctl`/`Tdie` on AMD and
  `CPU Package` on Intel, and warns if it has to fall back to a non-CPU
  sensor
- **Self-recovers** from USB replug and suspend/resume, and blanks the
  display (`--.-`) on exit and when the sensor stops responding, so a
  frozen reading isn't mistaken for a live one. On Linux it also looks for
  the hwmon sensor again if it disappears or is renumbered (driver reload),
  and on Windows it keeps a re-resolved sensor across reconnects (without
  `--sensor` it never swaps a CPU sensor for a non-CPU one). Outside
  `--debug` the shown value is logged at most once every 60 s (the first
  reading and blank/live transitions are logged at once), and each device
  error warning is printed once until a write succeeds
- On Linux: needs no Wine, no proprietary SDK, and no root access after a
  one-time udev rule is installed

## Hardware this was built for / tested on

- **Gamdias Chione E4-360** AIO liquid cooler
- USB ID `1b80:b53c`, shown as "Afatech CHIONE Digital Display" by `lsusb`
  and as "GAMDIAS Technology Co., Ltd. CHIONE Digital Display" under
  `/sys/class/hidraw`
- **AMD Ryzen 7 5700X** on a **Gigabyte A520M AORUS ELITE**

### Operating systems

The same machine in every case, so the rows differ by operating system
alone, not by hardware:

| OS | Script | Temperature source | Status |
|----|--------|--------------------|--------|
| Bazzite (Fedora Atomic) | `chione_temp_display.py` | `k10temp` (Tctl) | where the protocol was reverse engineered, and in daily use |
| Debian 13 (trixie), kernel 6.12 | `chione_temp_display.py` | `k10temp` (Tctl) | verified end to end: udev rule, driver, systemd user service |
| Windows 11 | `windows/chione_temp_display_win.py` | LibreHardwareMonitor (`Tctl/Tdie`) | verified with `hidapi` |

The Linux script is the same file on Bazzite and Debian, with no
per-distribution changes and no conditionals: the differences between those
two distributions turned out to sit entirely in setup, not in code. Anything
else with `hidraw` support should work; systemd is needed only for the
optional background service.

Debian was checked on Python 3.13 in a Wayland session, which is the case
the default `uaccess` udev rule is written for. A machine with no graphical
login needs the group-based rule instead, as described in
[Setup (Linux)](#1-grant-non-root-access-to-the-device-recommended).

Other Chione-family coolers (M4, P3, P5, etc.) may or may not share the same
protocol; this has not been tested against them.

## Current limitations

- The display only has two modes, CPU (temperature) and FAN (RPM). There is
  no brightness, color, GPU or other mode to drive.
- The cooler does not report its own fan speed. The RPM shown comes from the
  motherboard (the `CPU_FAN` header), read through `hwmon` on Linux and
  LibreHardwareMonitor on Windows.
- On Linux, many Gigabyte boards (including the A520M AORUS ELITE) need the
  out-of-tree `it87` driver
  ([frankcrawford/it87](https://github.com/frankcrawford/it87), installable
  through DKMS) before `fanN_input` shows up in `hwmon`.
- Without a fan source, `--mode auto` stays on `cpu`.

## Protocol notes

The full reverse-engineered packet layout is documented in the docstring at
the top of [`chione_temp_display.py`](chione_temp_display.py).

The layout started from the `usbmon` capture described above. It was later
confirmed in full by static analysis of ZEUS CAST 3.3.1.4 (decompiling the
.NET assemblies, without running it). That showed the packet carries the
display digits, the decimal point, the unit (°C or °F), the CPU icon, the
mode (CPU or FAN) and a flash flag used for the temperature alarm. Byte 12
is a checksum, the sum of bytes 0 to 11 modulo 256, so the script now
calculates it for every packet instead of sending a fixed value.

## Setup (Linux)

Nothing here is distribution-specific: the same steps work on Debian,
Ubuntu, Fedora, Bazzite, Arch and anything else with `hidraw` and systemd.
Debian/Ubuntu users have one extra thing to know, in
[Notes for Debian and Ubuntu](#notes-for-debian-and-ubuntu) below.

### The short way

[`install.sh`](install.sh) does the whole setup and can also just look at
your machine and tell you what's missing:

```bash
./install.sh check       # inspect everything, change nothing
./install.sh print-rule  # show the udev rule it would install
./install.sh all         # install the udev rule + the systemd user service
./install.sh service -- --hwmon coretemp   # service only, saving script options
./install.sh remove      # undo both (keeps ~/.config/chione-temp.conf)
```

Every line `check` prints is a verdict on one piece of the setup, so a
machine where everything already works looks like this:

```console
$ ./install.sh check

System
  + Debian GNU/Linux 13 (trixie)  kernel 6.12.105+deb13-amd64
  + systemd is running

Python
  + Python 3.13.5 at /usr/bin/python3
  + driver script: /home/you/chione-display-linux/chione_temp_display.py

Cooler (USB 1b80:b53c)
  + found at /dev/hidraw9
  + read/write access granted

CPU temperature sensor
  + k10temp temp1_input = 41.4C

Device access method
  + udev rule installed at /etc/udev/rules.d/70-chione.rules
  + active logind session with a seat: the default 'uaccess' rule applies

Service
  + unit installed at /home/you/.config/systemd/user/chione-temp.service
  + service is running

Ready. Run it with: python3 /home/you/chione-display-linux/chione_temp_display.py
```

Anything not yet in place is reported the same way, saying which step fixes
it, which is why running `check` first is usually faster than reading the
manual steps below.

`check` and `print-rule` are safe to run first and need no privileges; only
the udev step needs root, and it asks through `sudo`, falling back to
`pkexec` when there is no terminal to prompt on. If you prefer doing it by
hand, the same three steps are below.

### 1. Grant non-root access to the device (recommended)

```bash
sudo cp udev/70-chione.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
```

Then replug the cooler (or reboot) so the access rule is applied. A rule
cannot change a device node that is already open, which is why the replug
matters. Without this, the script keeps waiting for device access (printing
how to install the rule) until the rule is installed, or you can run it
(and the service below) as root.

The rule grants access via a logind ACL (`uaccess`) to the user with the
active graphical session. That covers the normal desktop case. It does *not*
cover a machine nobody logs into graphically, because `uaccess` only grants
the ACL while a session is active; for that, use the group-based variant:

```bash
./install.sh udev --group plugdev
```

Both alternatives are documented in the rule file itself.

### 2. Run it

```bash
python3 chione_temp_display.py                    # auto-detects everything
python3 chione_temp_display.py --hwmon coretemp   # pin a driver, if needed
python3 chione_temp_display.py --mode auto        # alternate CPU temperature and fan RPM
```

Run it from the cloned repository. If you copy the script elsewhere (manual
install), copy `chione_core.py` into the same folder, otherwise it fails to
start with an import error.

The script finds both the cooler (by USB ID) and the CPU sensor (by driver
name) automatically, with nothing to edit. Your CPU temperature should
appear on the display within a couple of seconds, updating live, and also
print in the terminal (only when the shown value changes) for confirmation.

To check the wiring without any sensor, run the self-test: it shows `88.8`,
then `12.3` (2 s each), blanks the display and exits (exit code `1` if the
device cannot be opened or written to):

```bash
python3 chione_temp_display.py --test
```

If something isn't found, list what the machine actually has:

```bash
python3 chione_temp_display.py --list-hidraw   # HID devices + USB IDs
python3 chione_temp_display.py --list-hwmon    # temperature and fan sensors
```

### 3. (Optional) Run it automatically on login

```bash
./install.sh service
```

This generates the unit with this checkout's real path, so it works wherever
you cloned the repo. If no default CPU sensor exists yet, it prints a
warning first (the service would wait about 60 s and then exit with code 2).
To pass options to the script, put them after `--`:

```bash
./install.sh service -- --hwmon coretemp --interval 1
./install.sh service -- --mode auto --alarm 90
```

They are saved as `CHIONE_ARGS` in `~/.config/chione-temp.conf`, which the
unit reads through `EnvironmentFile` (the file is optional). Arguments with
spaces, quotes, backslashes or empty values are refused. Running
`./install.sh service` without `--` keeps the existing conf unchanged, and
`./install.sh remove` does not delete it (remove it by hand if unwanted).
If a `--hwmon` is saved, the installer checks for that driver instead of the
default CPU sensors.

To change the options later, edit the conf (a single line such as
`CHIONE_ARGS="--hwmon coretemp"`) and restart the service:

```bash
systemctl --user restart chione-temp
```

By hand instead:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/chione-temp.service ~/.config/systemd/user/
# The shipped unit assumes the repo is at ~/chione-display-linux. If you
# cloned it elsewhere, edit the ExecStart path in the copied unit.
# Optional script options: CHIONE_ARGS="--hwmon NAME" in
# ~/.config/chione-temp.conf
systemctl --user daemon-reload
systemctl --user enable --now chione-temp.service
```

Check that it is running with:

```bash
systemctl --user status chione-temp.service
journalctl --user -u chione-temp.service -f
```

To keep it running with no one logged in, enable lingering. Pair it with
the group-based udev rule, since `uaccess` grants nothing without an active
session:

```bash
sudo loginctl enable-linger "$USER"
sudo groupadd -f plugdev && sudo usermod -aG plugdev "$USER"
./install.sh udev --group plugdev
```

Log out and back in so the new group membership takes effect.

## Notes for Debian and Ubuntu

The driver itself needs no changes. Only two things differ from a Fedora-
family setup:

**Nothing to install beyond Python.** Debian's `python3` package is enough;
the script imports only the standard library. `usbutils` (for `lsusb`) and
`lm-sensors` (for `sensors`) are useful for diagnosis but never required:

```bash
sudo apt install python3          # already present on a desktop install
sudo apt install usbutils lm-sensors   # optional, diagnosis only
```

**Intel CPUs may need the sensor driver loaded.** `k10temp` (AMD) autoloads
on Debian, but `coretemp` sometimes does not, and then no CPU sensor exists
for the script to find (at startup the script waits up to 60 s for it, then exits with code 2). Check with `--list-hwmon`; if nothing is marked
`<- default`:

```bash
sudo modprobe coretemp
echo coretemp | sudo tee /etc/modules-load.d/coretemp.conf   # persist it
```

The `plugdev` group used by the group-based udev rule already exists on
Debian and Ubuntu, and desktop users are normally members (check with `id`).
On Fedora/Bazzite/Arch it does not exist and has to be created first, which
is why `uaccess` is the default.

## Setup (Windows)

Windows has no `hwmon`, and CPU die temperature can't be read from user mode
without a kernel driver. Rather than shipping one, the Windows script reads
the value from
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor),
which is free, open source, and already does the privileged part.

### 1. Install the prerequisites

```powershell
pip install hidapi
```

Then download and run
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases)
and enable **Options -> Remote Web Server -> Run** (default port 8085).
Running it as administrator is recommended so it can load its sensor driver,
and it has to stay running whenever the script runs.

Check that it works by opening <http://localhost:8085/data.json> in a
browser: you should see a wall of JSON.

### 2. Get the files

Either clone the repository and run from it (the script finds
`chione_core.py` in the parent folder), or download just
`windows\chione_temp_display_win.py` and `chione_core.py` and put them in
the same folder. The script looks for the core in its own folder and in the
parent one. If it finds neither, it exits with code `1` asking you to copy
`chione_core.py` next to it.

### 3. Run it

```powershell
python windows\chione_temp_display_win.py
python windows\chione_temp_display_win.py --test   # 88.8, then 12.3, then blank
```

The script finds the cooler by its USB ID and picks the CPU sensor on its
own. If the auto-detection picks the wrong one, see what is available with
`--list-sensors` and pass the one you want with `--sensor`.

### 4. (Optional) Start it automatically at login

Press <kbd>Win</kbd>+<kbd>R</kbd>, enter `shell:startup`, and create a
shortcut in the folder that opens, pointing at:

```
pythonw C:\path\to\chione-display-linux\windows\chione_temp_display_win.py --wait -1
```

The shortcut keeps working as long as `chione_core.py` is next to the script
or in its parent folder (the cloned repository layout, or both files copied
into one folder). `pythonw` runs it without a console window, and `--wait -1` makes it wait
for LibreHardwareMonitor instead of giving up when it happens to start first.

`pythonw` has no console, so its output is lost. To keep a log, add
`--log-file`:

```
pythonw C:\path\to\chione-display-linux\windows\chione_temp_display_win.py --wait -1 --log-file C:\path\to\chione.log
```

Output (stdout and stderr) is appended to that file. If it is larger than
1 MB at startup it is moved to `chione.log.1` (replacing any older one)
and a new file is started. If the log file cannot be opened the script exits
with code `2`.

## Configuration

Everything is a command-line flag with a sensible default, nothing in either
script needs editing.

### Linux

| Flag | Default | Purpose |
|------|---------|---------|
| `--device PATH` | auto | hidraw node; override auto-discovery for unusual setups |
| `--hwmon NAME` | auto | pin the hwmon driver; auto tries `k10temp`, `coretemp`, `zenpower` |
| `--temp-file NAME` | `temp1_input` | which `temp*_input` file to read |
| `--interval SECONDS` | `0.5` | how often to refresh the display (between `0.1` and `60`) |
| `--mode cpu\|fan\|auto` | `cpu` | show CPU temperature, fan RPM, or both alternating |
| `--cycle SECONDS` | `5` | with `--mode auto`, seconds on each mode (between `1` and `60`) |
| `--fahrenheit` | off | show Fahrenheit in cpu mode |
| `--alarm CELSIUS` | off | flash the display when the CPU temperature reaches this value |
| `--fan-hwmon DRIVER` | auto | hwmon driver for the fan RPM; auto takes the first CPU-labelled fan of a motherboard driver, ignoring GPUs |
| `--fan-input FILE` | auto | which `fan*_input` file to read, e.g. `fan2_input` |
| `--debug` | off | hexdump the device's reply |
| `--list-hidraw` | off | list all HID devices with their USB IDs, then exit |
| `--list-hwmon` | off | list every hwmon temperature and fan sensor with live values, then exit |
| `--test` | off | show `88.8`, then `12.3` (2 s each), blank and exit; no sensor needed |
| `--version` | off | print the version and exit |

Not sure which `--hwmon` to use? `--list-hwmon` prints every sensor with its
driver, label and current reading, marking the one auto-detection would
accept:

```console
$ python3 chione_temp_display.py --list-hwmon
nvme      temp1_input   51.9C   Composite
amdgpu    temp1_input   58.0C   edge
k10temp   temp1_input   53.5C   Tctl     <- default
k10temp   temp3_input   42.2C   Tccd1
```

### Windows

| Flag | Default | Purpose |
|------|---------|---------|
| `--url URL` | `http://localhost:8085/data.json` | LibreHardwareMonitor's JSON endpoint |
| `--sensor NAME` | auto | sensor to display; override the CPU auto-detection |
| `--interval SECONDS` | `0.5` | how often to refresh the display (between `0.1` and `60`) |
| `--wait SECONDS` | `0` | wait for LibreHardwareMonitor to come up; `-1` waits forever; other negative values are rejected |
| `--mode cpu\|fan\|auto` | `cpu` | show CPU temperature, fan RPM, or both alternating |
| `--cycle SECONDS` | `5` | with `--mode auto`, seconds on each mode (between `1` and `60`) |
| `--fahrenheit` | off | show Fahrenheit in cpu mode |
| `--alarm CELSIUS` | off | flash the display when the CPU temperature reaches this value |
| `--fan-sensor NAME` | auto | fan sensor to display; auto takes the first fan whose name starts with `CPU` |
| `--debug` | off | hexdump the device's reply |
| `--list-sensors` | off | list every temperature and fan sensor LibreHardwareMonitor reports, then exit |
| `--test` | off | show `88.8`, then `12.3` (2 s each), blank and exit; no LibreHardwareMonitor needed |
| `--log-file PATH` | off | append stdout and stderr to PATH (for `pythonw`); rotated to `PATH.1` above 1 MB |
| `--version` | off | print the version and exit |

### Exit codes

| Script | Code | Meaning |
|--------|------|---------|
| Linux | `0` | stopped normally (Ctrl+C or SIGTERM), `--test`, `--version` or a `--list-*` flag |
| Linux | `1` | `--test` could not open or write to the device |
| Linux | `2` | no CPU hwmon sensor found after waiting up to 60 s at startup (also argparse errors); the systemd unit's `RestartPreventExitStatus=2` uses it to stop restarting |
| Windows | `0` | stopped normally, or `--test` / `--version` |
| Windows | `2` | LibreHardwareMonitor unreachable after `--wait`, or no sensor found (also argparse errors, or a `--log-file` that cannot be opened) |
| Windows | `1` | `--list-sensors` found no sensors or could not connect, `--test` could not find, open or write to the display, `chione_core.py` not found next to the script or in its parent folder, or `hidapi` not installed |

A missing or inaccessible cooler does not exit: both scripts keep retrying.

## Requirements

### Linux

- Python 3, standard library only (no external packages needed)
- `chione_core.py` in the same folder as `chione_temp_display.py`
- `hidraw` support (default on virtually every distribution)
- A CPU with a working `hwmon` temperature sensor (`k10temp`, `coretemp` or
  `zenpower`; on Debian with an Intel CPU you may need `modprobe coretemp`)
- systemd, only for the optional background service

### Windows

- Python 3 and the [`hidapi`](https://pypi.org/project/hidapi/) package
- `chione_core.py` next to the script or in its parent folder
- LibreHardwareMonitor running with its web server enabled

## Other Chione models, and reporting one

Only the E4-360 has been tested, so the table above is the honest limit of
what is known, not a claim about the rest of the family. Finding out where
another model stands costs two commands and no risk, since both only read:

```bash
lsusb                                          # what USB ID does the cooler show?
python3 chione_temp_display.py --list-hidraw   # look for 00001B80:0000B53C
```

If the ID matches, the driver should simply run. If it differs, the device
is still worth a look: point the script at it with `--device /dev/hidrawN`
and add `--debug` to hexdump what it replies, which is what the decoding
work started from in the first place.

Opening an issue with the model name, the `--list-hidraw` line for the
cooler and, when relevant, the `--debug` output is enough to tell whether it
is the same protocol, a variant, or something unrelated. Reports that a
model works unchanged are as valuable as reports that it doesn't: together
they are what turns the tested-hardware table into something a buyer can
rely on before ordering.

## Development

Unit tests live in `tests/`: `test_core.py` covers the shared
`chione_core.py`, the others cover each platform script. Run them from the repository root:

```bash
python3 -m unittest discover -s tests
```

Lint with [ruff](https://docs.astral.sh/ruff/) (configured in `ruff.toml`,
targeting Python 3.9) and [shellcheck](https://www.shellcheck.net/):

```bash
pip install ruff==0.17.0
ruff check .
shellcheck -S warning install.sh
```

GitHub Actions ([`ci.yml`](.github/workflows/ci.yml)) runs on every push and
pull request to `main`: the tests on Ubuntu and Windows with Python 3.9 and
3.13, plus ruff and shellcheck.

## Disclaimer

This is an unofficial, reverse-engineered protocol implementation, not
affiliated with or endorsed by Gamdias. It writes directly to a USB HID
device; while this has caused no issues during testing (the worst observed
failure mode is the display showing a blank digit/dash), use your own
judgement, as with anything that talks to hardware outside the
manufacturer's own software.

## License

MIT, see [LICENSE](LICENSE).
