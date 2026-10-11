# Chione E4 Temperature Display

[![CI](https://github.com/jonanthann/chione-display-linux/actions/workflows/ci.yml/badge.svg)](https://github.com/jonanthann/chione-display-linux/actions/workflows/ci.yml)

Shows the real CPU temperature (or fan RPM) on the display of a Gamdias Chione
E4 AIO liquid cooler. It does not need Gamdias's ZEUS CAST or Wine. The USB
HID protocol was reverse engineered from the traffic between ZEUS CAST and the
cooler, so this is an unofficial driver. The script finds the cooler by its USB
ID (`1b80:b53c`). Only the E4-360 has been tested.

It comes as a Linux script (standard library only) and a Windows script. On
Linux you can also change the settings from a GNOME app or a browser panel.

## Features

- CPU temperature from `hwmon`, with the sensor auto-detected (`k10temp`, `coretemp` or `zenpower`)
- Three modes: CPU temperature, fan RPM, or both alternating
- Celsius or Fahrenheit
- Alarm: the display flashes above a temperature you choose
- Live settings: edit `settings.json` and the running service applies it within 1 s
- Recovers by itself after a USB replug or suspend, and blanks the display on exit
- GNOME app and browser panel for the settings and the service
- Windows script that reads the temperature from LibreHardwareMonitor
- `.deb` package for Debian and Ubuntu

Fan RPM comes from the motherboard (`CPU_FAN` header), not from the cooler. Many
Gigabyte boards need the out-of-tree
[`it87`](https://github.com/frankcrawford/it87) driver for it to show up. Without
a fan source, `--mode auto` stays on CPU.

## How to use

### Install the .deb (Debian, Ubuntu)

Download `chione-display_<version>_all.deb` from the
[Releases](https://github.com/jonanthann/chione-display-linux/releases) page:

```bash
sudo apt install ./chione-display_<version>_all.deb
systemctl --user enable --now chione-temp
```

Replug the cooler once if it was already connected. To remove it:

```bash
systemctl --user disable --now chione-temp
sudo apt remove chione-display
```

### From source (any Linux with systemd)

Clone the repository, then use `install.sh`:

```bash
./install.sh check     # inspect the machine, change nothing
./install.sh all       # udev rule + systemd user service
./install.sh udev      # only the udev rule (needs sudo)
./install.sh service   # only the user service
./install.sh gui       # GNOME app launcher and icon
```

Replug the cooler after installing the udev rule. To pass options to the
service, put them after `--`: `./install.sh service -- --mode auto --alarm 90`.

To uninstall, `./install.sh remove` takes out the service, the udev rule and
the GNOME launcher. `./install.sh gui --remove` removes only the launcher and
icon.

To run without the service:

```bash
python3 chione_temp_display.py                 # auto-detects everything
python3 chione_temp_display.py --mode auto     # alternate CPU and fan
python3 chione_temp_display.py --test          # show 88.8, then 12.3, then blank
```

If the cooler or sensor is not found, `--list-hidraw` and `--list-hwmon` show
what the machine has. Keep `chione_core.py` in the same folder as the script.

### GNOME app

Open "Chione Display" from the app menu. It needs `python3-gi`, `gir1.2-gtk-4.0`
and `gir1.2-adw-1` (installed with the .deb). From source, run
`./install.sh gui` first. The window shows live readings and lets you change
mode, unit, alarm and fan source, start or stop the service, test the display and
read the log. The interface text is in Portuguese.

### Web panel

For desktops without GNOME:

```bash
chione-web                          # from the .deb
./chione_web.py                     # from a clone
chione-web --port 9000 --no-browser
```

Options: `--port` (default 8765), `--no-browser`, `--settings PATH`. The panel
listens on `127.0.0.1` only and needs a random token, which is part of the URL
printed in the terminal. Open that URL if the browser does not start by itself.
The page text is in Portuguese.

Neither panel talks to the cooler. They only edit `settings.json`, and the
`chione-temp` service applies it.

### Settings file

The service watches `~/.config/chione-display/settings.json`
(`$XDG_CONFIG_HOME` is respected). Every key is optional, and a key in the file
wins over the command line:

| Key | Values | Same as |
|-----|--------|---------|
| `mode` | `"cpu"`, `"fan"`, `"auto"` | `--mode` |
| `cycle` | seconds, 1 to 60 | `--cycle` |
| `fahrenheit` | `true` or `false` | `--fahrenheit` |
| `alarm` | Celsius, or `null` for off | `--alarm` |
| `fan_hwmon` | driver name, or `null` for auto | `--fan-hwmon` |
| `fan_input` | e.g. `"fan2_input"`, or `null` for auto | `--fan-input` |

Invalid JSON or values are ignored (the last good settings stay) and logged.

### Main command-line options (Linux)

| Option | Default | Purpose |
|--------|---------|---------|
| `--mode cpu\|fan\|auto` | `cpu` | what to show |
| `--cycle SECONDS` | `5` | seconds per mode in `auto` |
| `--fahrenheit` | off | show Fahrenheit |
| `--alarm CELSIUS` | off | flash at or above this temperature |
| `--hwmon NAME` | auto | pin the CPU sensor driver |
| `--fan-hwmon`, `--fan-input` | auto | pick the fan sensor |
| `--interval SECONDS` | `0.5` | refresh rate |
| `--settings PATH`, `--no-settings` | default path | use another settings file, or none |
| `--test` | off | self-test pattern, then exit |

### Windows

Windows has no `hwmon`, so the script reads the temperature from
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor).

1. `pip install hidapi`
2. Run LibreHardwareMonitor (as administrator) and enable
   Options > Remote Web Server > Run (port 8085).
3. Run `python windows\chione_temp_display_win.py`

Use `--test` to check the display, `--list-sensors` and `--sensor` to pick a
sensor, and `--wait -1` to wait for LibreHardwareMonitor at startup. The
settings file is not supported on Windows. Keep `chione_core.py` next to the
script or in its parent folder.

## License

MIT, see [LICENSE](LICENSE).

This is an unofficial project, not affiliated with or endorsed by Gamdias. It
writes directly to a USB HID device, so use it at your own risk.
