# Chione E4 Temperature Display

Drive the physical digital display on a Gamdias Chione E4 (and possibly
other Chione-family) AIO liquid cooler with **real CPU temperature**,
without Gamdias's ZEUS CAST software.

There are two scripts, one per platform, speaking the same reverse-engineered
USB HID protocol:

| Script | Platform | Temperature source | Dependencies |
|--------|----------|--------------------|--------------|
| [`chione_temp_display.py`](chione_temp_display.py) | Linux | `hwmon` (`k10temp` / `coretemp`, auto-detected) | none, standard library only |
| [`windows/chione_temp_display_win.py`](windows/chione_temp_display_win.py) | Windows | LibreHardwareMonitor web server | `hidapi`, LibreHardwareMonitor |

The update packet is byte-for-byte identical on both; only how the script
reaches the device and where it gets the temperature from differ.

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
  `CPU Package` on Intel, and ignoring motherboard, GPU and SSD probes
- **Self-recovers** from USB replug and suspend/resume, and blanks the
  display (`--.-`) on exit so a frozen reading isn't mistaken for a live one
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

- Only the digit-display "temperature" mode has been reverse engineered.
  Other modes the hardware may support (fan RPM, coolant temperature, custom
  images) are not implemented here.
- The display format is fixed at `TT.T` (two whole digits plus one decimal),
  clamped between 0.0 and 99.9 °C.

## Protocol notes

The full reverse-engineered packet layout is documented in the docstring at
the top of [`chione_temp_display.py`](chione_temp_display.py).

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
./install.sh remove      # undo both
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
  + k10temp temp3_input = 41.0C

Device access method
  + udev rule installed at /etc/udev/rules.d/70-chione.rules
  + active logind session: the default 'uaccess' rule applies

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
matters. Without this, you will need to run the script (and the service
below) with `sudo`.

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
```

The script finds both the cooler (by USB ID) and the CPU sensor (by driver
name) automatically, with nothing to edit. Your CPU temperature should
appear on the display within a couple of seconds, updating live, and also
print in the terminal (only when the shown value changes) for confirmation.

If something isn't found, list what the machine actually has:

```bash
python3 chione_temp_display.py --list-hidraw   # HID devices + USB IDs
python3 chione_temp_display.py --list-hwmon    # temperature sensors
```

### 3. (Optional) Run it automatically on login

```bash
./install.sh service
```

This generates the unit with this checkout's real path, so it works wherever
you cloned the repo. By hand instead:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/chione-temp.service ~/.config/systemd/user/
# The shipped unit assumes the repo is at ~/chione-display-linux. If you
# cloned it elsewhere, edit the ExecStart path in the copied unit.
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
./install.sh udev --group plugdev
```

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
for the script to find. Check with `--list-hwmon`; if nothing is marked
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

### 2. Run it

```powershell
python windows\chione_temp_display_win.py
```

The script finds the cooler by its USB ID and picks the CPU sensor on its
own. If the auto-detection picks the wrong one, see what is available with
`--list-sensors` and pass the one you want with `--sensor`.

### 3. (Optional) Start it automatically at login

Press <kbd>Win</kbd>+<kbd>R</kbd>, enter `shell:startup`, and create a
shortcut in the folder that opens, pointing at:

```
pythonw C:\path\to\chione-display-linux\windows\chione_temp_display_win.py --wait -1
```

`pythonw` runs it without a console window, and `--wait -1` makes it wait
for LibreHardwareMonitor instead of giving up when it happens to start first.

## Configuration

Everything is a command-line flag with a sensible default — nothing in either
script needs editing.

### Linux

| Flag | Default | Purpose |
|------|---------|---------|
| `--device PATH` | auto | hidraw node; override auto-discovery for unusual setups |
| `--hwmon NAME` | auto | pin the hwmon driver; auto tries `k10temp`, `coretemp`, `zenpower` |
| `--temp-file NAME` | `temp1_input` | which `temp*_input` file to read |
| `--interval SECONDS` | `0.5` | how often to refresh the display |
| `--debug` | off | hexdump the device's reply (a lead for decoding fan RPM) |
| `--list-hidraw` | — | list all HID devices with their USB IDs, then exit |
| `--list-hwmon` | — | list every hwmon temperature sensor with live values, then exit |

Not sure which `--hwmon` to use? `--list-hwmon` prints every sensor with its
driver, label and current reading, marking the ones auto-detection would
accept:

```console
$ python3 chione_temp_display.py --list-hwmon
nvme      temp1_input   51.9C   Composite
amdgpu    temp1_input   58.0C   edge
k10temp   temp1_input   53.5C   Tctl     <- default
k10temp   temp3_input   42.2C   Tccd1    <- default
```

### Windows

| Flag | Default | Purpose |
|------|---------|---------|
| `--url URL` | `http://localhost:8085/data.json` | LibreHardwareMonitor's JSON endpoint |
| `--sensor NAME` | auto | sensor to display; override the CPU auto-detection |
| `--interval SECONDS` | `0.5` | how often to refresh the display |
| `--wait SECONDS` | `0` | wait for LibreHardwareMonitor to come up; `-1` waits forever |
| `--debug` | off | hexdump the device's reply (a lead for decoding fan RPM) |
| `--list-sensors` | — | list every temperature sensor LibreHardwareMonitor reports, then exit |

## Requirements

### Linux

- Python 3, standard library only (no external packages needed)
- `hidraw` support (default on virtually every distribution)
- A CPU with a working `hwmon` temperature sensor (`k10temp`, `coretemp` or
  `zenpower`; on Debian with an Intel CPU you may need `modprobe coretemp`)
- systemd, only for the optional background service

### Windows

- Python 3 and the [`hidapi`](https://pypi.org/project/hidapi/) package
- LibreHardwareMonitor running with its web server enabled

## Other Chione models, and reporting one

Only the E4-360 has been tested, so the table above is the honest limit of
what is known, not a claim about the rest of the family. Finding out where
another model stands costs two commands and no risk, since both only read:

```bash
lsusb                                          # what USB ID does the cooler show?
python3 chione_temp_display.py --list-hidraw   # does it appear as 1b80:b53c?
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

## Disclaimer

This is an unofficial, reverse-engineered protocol implementation, not
affiliated with or endorsed by Gamdias. It writes directly to a USB HID
device; while this has caused no issues during testing (the worst observed
failure mode is the display showing a blank digit/dash), use your own
judgement, as with anything that talks to hardware outside the
manufacturer's own software.

## License

MIT, see [LICENSE](LICENSE).
