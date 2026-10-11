#!/usr/bin/env python3
"""GNOME (GTK4 + libadwaita) settings window for the Chione display.

Every change is written to the live settings file (chione_core.settings_path)
that the chione-temp user service reloads within a second. The logic below
the "UI" marker is plain Python so it can be tested without GTK.
"""
import glob
import os
import shlex
import subprocess
import sys
import threading

import chione_core as core
import chione_temp_display as drv

APP_ID = "io.github.jonanthann.ChioneDisplay"
SERVICE = "chione-temp"
UNIT_PATH = os.path.join("~", ".config", "systemd", "user", SERVICE + ".service")
CONF_PATH = os.path.join("~", ".config", "chione-temp.conf")
DEFAULT_ALARM = 90.0
DEBOUNCE_MS = 300
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- logic


def parse_chione_args(text):
    """Settings implied by the CHIONE_ARGS="..." line of the service conf."""
    value = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("CHIONE_ARGS="):
            value = line.split("=", 1)[1].strip().strip("\"'")
    try:
        words = shlex.split(value)
    except ValueError:
        return {}
    out = {}
    keys = {"--mode": "mode", "--cycle": "cycle", "--alarm": "alarm",
            "--fan-hwmon": "fan_hwmon", "--fan-input": "fan_input"}
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--fahrenheit":
            out["fahrenheit"] = True
        else:
            name, eq, val = w.partition("=")
            if name in keys:
                if not eq and i + 1 < len(words):
                    i += 1
                    val = words[i]
                elif not eq:
                    break
                out[keys[name]] = val
        i += 1
    for key in ("cycle", "alarm"):
        if key in out:
            try:
                out[key] = float(out[key])
            except ValueError:
                del out[key]
    try:
        return core.validate_settings(out)
    except ValueError:
        return {}


def read_conf_args(path=None):
    try:
        with open(os.path.expanduser(path or CONF_PATH), encoding="utf-8") as f:
            return parse_chione_args(f.read())
    except OSError:
        return {}


def initial_state(settings, conf):
    """Full state: defaults, then CHIONE_ARGS, then settings.json on top."""
    state = {"mode": "cpu", "cycle": float(core.DEFAULT_CYCLE_SECONDS),
             "fahrenheit": False, "alarm": None,
             "fan_hwmon": None, "fan_input": None}
    state.update(conf)
    state.update(settings)
    return state


def scan_fans():
    """Every fan input as dicts: hwmon, file, label, rpm (None if unreadable),
    gpu (driver is a GPU one)."""
    fans = []
    for hwmon_dir in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = drv._read_text(os.path.join(hwmon_dir, "name"))
        if not name:
            continue
        for path in drv._fan_files(hwmon_dir):
            try:
                rpm = drv.read_fan_rpm(path)
            except (OSError, ValueError):
                rpm = None
            fans.append({"hwmon": name, "file": os.path.basename(path),
                         "label": drv._read_text(path.replace("_input", "_label")),
                         "rpm": rpm, "gpu": name in drv.GPU_HWMON_DRIVERS})
    return fans


def fan_choice_label(fan):
    rpm = "n/d" if fan["rpm"] is None else f"{fan['rpm']} RPM"
    text = f"{fan['hwmon']} {fan['file'].replace('_input', '')} · {rpm}"
    if fan["gpu"]:
        text += " (GPU)"
    return text


def fan_labels(fans):
    return ["Automático"] + [fan_choice_label(f) for f in fans]


def fan_index(fans, state):
    """Combo index for the saved fan (0 = Automático, also if not found)."""
    for i, f in enumerate(fans, 1):
        if (f["hwmon"] == state.get("fan_hwmon")
                and f["file"] == state.get("fan_input")):
            return i
    return 0


def fan_settings(fans, index):
    if index <= 0 or index > len(fans):
        return {"fan_hwmon": None, "fan_input": None}
    return {"fan_hwmon": fans[index - 1]["hwmon"],
            "fan_input": fans[index - 1]["file"]}


def format_temp(celsius, fahrenheit):
    if celsius is None:
        return "sem sensor"
    if fahrenheit:
        return f"{celsius * 9 / 5 + 32:.1f} °F"
    return f"{celsius:.1f} °C"


def format_rpm(rpm):
    return "sem sensor" if rpm is None else f"{rpm} RPM"


def read_live(state):
    """(cpu_celsius or None, fan_rpm or None) using the configured fan."""
    cpu = rpm = None
    path, _ = drv.resolve_hwmon_temp_path(None, drv.HWMON_TEMP_FILE)
    if path:
        try:
            cpu = drv.read_temp_celsius(path)
        except (OSError, ValueError):
            pass
    fpath, _ = drv.resolve_hwmon_fan(state.get("fan_hwmon"),
                                     state.get("fan_input"))
    if fpath:
        try:
            rpm = drv.read_fan_rpm(fpath)
        except (OSError, ValueError):
            pass
    return cpu, rpm


class Debouncer:
    """Run fn once, `delay_ms` after the last call. schedule(ms, cb) returns
    a handle and cancel(handle) drops it (GLib timeouts in the UI)."""

    def __init__(self, schedule, cancel, delay_ms, fn):
        self._schedule, self._cancel = schedule, cancel
        self.delay_ms, self.fn = delay_ms, fn
        self._handle = None

    def call(self):
        if self._handle is not None:
            self._cancel(self._handle)
        self._handle = self._schedule(self.delay_ms, self._fire)

    def _fire(self):
        self._handle = None
        self.fn()
        return False

    def flush(self):
        if self._handle is not None:
            self._cancel(self._handle)
            self._fire()


class Store:
    """Holds the displayed state plus the overrides (only the keys the user
    changed). Only the overrides are written, so CHIONE_ARGS keeps applying
    to everything else."""

    def __init__(self, path, state, overrides=None):
        self.path, self.state = path, dict(state)
        self.overrides = dict(overrides or {})

    def update(self, **changes):
        """Save with the changes applied; on failure keep the old state and
        raise (ValueError or OSError). Returns False when nothing changed
        (no write)."""
        if all(self.state.get(k) == v for k, v in changes.items()):
            return False
        over = dict(self.overrides)
        over.update(changes)
        core.save_settings(self.path, over)
        self.overrides = over
        self.state = dict(self.state, **changes)
        return True


def systemctl(*args):
    """Return (ok, stdout) of `systemctl --user ARGS`."""
    try:
        r = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                           text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return r.returncode == 0, r.stdout.strip()


def service_installed():
    # `cat` finds units in ~/.config (install.sh) and in /usr/lib (the .deb).
    return systemctl("cat", SERVICE)[0] or os.path.exists(
        os.path.expanduser(UNIT_PATH))


def service_hint():
    """How to install the service, depending on how the app was installed."""
    if HERE.startswith("/usr/share/chione-display"):
        return "systemctl --user enable --now " + SERVICE
    return "./install.sh service"


def service_active():
    return systemctl("is-active", SERVICE)[1] == "active"


def journal_tail(lines=30):
    try:
        r = subprocess.run(["journalctl", "--user", "-u", SERVICE, "-n",
                            str(lines), "--no-pager"], capture_output=True,
                           text=True, timeout=20, check=False)
        return (r.stdout or r.stderr).strip() or "(sem registros)"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Não foi possível ler o log: {exc}"


def run_display_test():
    """Stop the service, run the driver --test, restart if it was active.
    Returns an error message or None."""
    was_active = service_active()
    if was_active:
        systemctl("stop", SERVICE)
    try:
        r = subprocess.run([sys.executable,
                            os.path.join(HERE, "chione_temp_display.py"),
                            "--test"], capture_output=True, text=True,
                           timeout=60, check=False)
        err = None if r.returncode == 0 else (
            r.stderr.strip() or "o teste falhou")
    except (OSError, subprocess.SubprocessError) as exc:
        err = str(exc)
    finally:
        if was_active:
            systemctl("start", SERVICE)
    return err

# ------------------------------------------------------------------- UI

CSS = """
@define-color accent_color #ffffff;
@define-color accent_bg_color #ffffff;
@define-color accent_fg_color #000000;
@define-color window_bg_color #000000;
@define-color window_fg_color #ffffff;
@define-color view_bg_color #000000;
@define-color headerbar_bg_color #000000;
@define-color headerbar_fg_color #ffffff;
@define-color headerbar_border_color transparent;
@define-color card_bg_color #0b0b0b;
@define-color card_fg_color #ffffff;
@define-color popover_bg_color #141414;
@define-color popover_fg_color #ffffff;
window, .background { background: #000; color: #fff; }
headerbar { background: #000; box-shadow: none; border: none; }
banner > revealer > widget { background: rgba(255,255,255,.1); color: #fff;
  border: 1px solid rgba(255,255,255,.3); border-radius: 28px; margin: 8px 16px; }
preferencespage > scrolledwindow > viewport > clamp { margin: 0 4px; }
preferencesgroup label.heading {
  font-size: 12px; font-weight: 600; letter-spacing: 2.2px; color: #a3a3a3; }
list.boxed-list, .boxed-list {
  background: linear-gradient(160deg, #141414, #050505);
  border-radius: 32px; border: 1px solid rgba(255,255,255,.14);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.28),
              inset 0 0 28px rgba(255,255,255,.06),
              0 0 32px rgba(255,255,255,.07); }
.boxed-list > row { background: transparent; color: #fff; padding: 2px 8px;
  border-color: rgba(255,255,255,.14); }
.boxed-list > row:first-child { border-top-left-radius: 32px; border-top-right-radius: 32px; }
.boxed-list > row:last-child { border-bottom-left-radius: 32px; border-bottom-right-radius: 32px; }
.boxed-list > row:disabled { opacity: .55; }
row .subtitle, row label.subtitle { color: #a3a3a3; }
.hero { background: linear-gradient(160deg, #141414, #050505);
  border-radius: 40px; border: 1px solid rgba(255,255,255,.14);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.28),
              inset 0 0 28px rgba(255,255,255,.06),
              0 0 32px rgba(255,255,255,.07);
  padding: 28px 20px 24px 20px; }
.hero-circle { background: #fff; color: #000; border-radius: 50%;
  min-width: 220px; min-height: 220px;
  box-shadow: 0 0 60px rgba(255,255,255,.25); }
.hero-circle .cap { font-size: 11px; font-weight: 700; letter-spacing: 2px;
  color: #444; }
.hero-circle .big { font-size: 38px; font-weight: 700; color: #000; }
.hero-bar { padding: 10px 20px; border-radius: 999px;
  background: linear-gradient(180deg, rgba(0,0,0,.5), rgba(0,0,0,.66));
  border: 1px solid rgba(255,255,255,.4);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.5),
              inset 0 0 24px rgba(255,255,255,.08); }
.hero-bar .cap { font-size: 12px; letter-spacing: 2px; color: #eee; }
.hero-bar .rpm { font-size: 18px; font-weight: 600; }
.hero-title { font-size: 44px; font-weight: 600; letter-spacing: -2px; }
.hero-badge { border: 1px solid #fff; border-radius: 999px; padding: 6px 12px; }
.hero-badge label { font-size: 9px; letter-spacing: 2px; }
.hero-badge .star { font-size: 14px; letter-spacing: 0; }
entry, spinbutton, dropdown > button, .boxed-list spinbutton {
  background: rgba(255,255,255,.08); color: #fff;
  border: 1px solid rgba(255,255,255,.25); border-radius: 999px;
  box-shadow: none; }
row.spin { padding-right: 12px; }
row.spin spinbutton { background: transparent; border: none; min-width: 0;
  box-shadow: none; }
row.spin spinbutton > text { background: transparent; min-width: 24px;
  margin-right: 6px; }
row.spin spinbutton > button { min-width: 28px; min-height: 28px;
  padding: 0; margin: 0 2px; }
headerbar button { background: transparent; border-color: transparent; }
headerbar button:hover { background: rgba(255,255,255,.14); }
button { border-radius: 999px; background: rgba(255,255,255,.08); color: #fff;
  border: 1px solid rgba(255,255,255,.25); box-shadow: none; padding: 6px 16px; }
button:hover { background: rgba(255,255,255,.16); }
button.pill-solid { background: #fff; color: #000; border-color: #fff;
  font-weight: 600; }
button.pill-solid:hover { background: #d9d9d9; }
button.pill-outline { background: transparent; color: #fff; border-color: #fff; }
button.pill-outline:hover { background: rgba(255,255,255,.14); }
button:disabled { opacity: .5; }
windowcontrols { border-spacing: 8px; }
windowcontrols > button { min-width: 0; min-height: 0; padding: 0;
  margin: 0; border: none; background: none; box-shadow: none; }
windowcontrols > button:hover { background: none; }
windowcontrols > button > image { padding: 1px; margin: 0; border-radius: 50%;
  box-shadow: none; -gtk-icon-size: 12px; color: transparent; }
windowcontrols > button:hover > image { color: rgba(0,0,0,.6); }
windowcontrols > button.close > image { background: #ff5f57; }
windowcontrols > button.minimize > image { background: #febc2e; }
windowcontrols > button.maximize > image { background: #28c840; }
windowcontrols > button:backdrop > image { background: #3a3a3a; }
switch { background: transparent; border: 1px solid #fff; border-radius: 999px;
  box-shadow: none; }
switch:checked { background: #fff; }
switch slider { background: #fff; border-radius: 50%; box-shadow: none;
  border: none; }
switch:checked slider { background: #000; }
:focus-visible { outline: 2px solid #fff; outline-offset: 3px; }
toast { background: rgba(255,255,255,.18); color: #fff; border-radius: 999px;
  border: 1px solid rgba(255,255,255,.4); }
"""



def run_gui():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, GLib, Gtk
    except (ImportError, ValueError) as exc:
        print(f"GTK4/libadwaita indisponível ({exc}).\n"
              "No Debian: sudo apt install python3-gi gir1.2-gtk-4.0 "
              "gir1.2-adw-1", file=sys.stderr)
        return 1

    MODES = ("cpu", "fan", "auto")

    class Window(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Chione Display",
                             default_width=520, default_height=780)
            self.path = core.settings_path()
            try:
                settings = core.load_settings(self.path)
                load_err = None
            except (ValueError, OSError) as exc:
                settings, load_err = {}, str(exc)
            self.store = Store(self.path,
                               initial_state(settings, read_conf_args()),
                               settings)
            st = self.store.state
            self.fans = []
            self.closed = False
            self.busy_live = False
            self.loading = True

            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_DARK)
            provider = Gtk.CssProvider()
            provider.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(
                self.get_display(), provider,
                Gtk.STYLE_PROVIDER_PRIORITY_USER)

            self.toasts = Adw.ToastOverlay()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.append(Adw.HeaderBar())
            self.banner = Adw.Banner(
                title="Serviço não instalado. Rode " + service_hint(),
                revealed=not service_installed())
            box.append(self.banner)
            page = Adw.PreferencesPage()
            scroll = Gtk.ScrolledWindow(vexpand=True)
            scroll.set_child(page)
            box.append(scroll)
            self.toasts.set_child(box)
            self.set_content(self.toasts)

            # Live readings (hero card)
            g = Adw.PreferencesGroup()
            V, H = Gtk.Orientation.VERTICAL, Gtk.Orientation.HORIZONTAL
            C = Gtk.Align.CENTER
            hero = Gtk.Box(orientation=V, spacing=20, css_classes=["hero"])
            circle = Gtk.Box(orientation=V, halign=C, valign=C,
                             css_classes=["hero-circle"])
            inner = Gtk.Box(orientation=V, spacing=4, halign=C, valign=C,
                            vexpand=True)
            inner.append(Gtk.Label(label="TEMPERATURA DA CPU",
                                   css_classes=["cap"]))
            self.cpu_lbl = Gtk.Label(css_classes=["big"])
            inner.append(self.cpu_lbl)
            circle.append(inner)
            hero.append(circle)
            bar = Gtk.Box(orientation=H, spacing=12, css_classes=["hero-bar"])
            bar.append(Gtk.Label(label="FAN", css_classes=["cap"]))
            self.fan_lbl = Gtk.Label(css_classes=["rpm"], hexpand=True,
                                     xalign=1)
            bar.append(self.fan_lbl)
            hero.append(bar)
            foot = Gtk.Box(orientation=H, spacing=12, margin_top=8)
            foot.append(Gtk.Label(label="Chione", xalign=0, hexpand=True,
                                  css_classes=["hero-title"]))
            badge = Gtk.Box(orientation=H, spacing=8, valign=Gtk.Align.END,
                            css_classes=["hero-badge"])
            badge.append(Gtk.Label(label="\u2500 \u2726",
                                   css_classes=["star"]))
            badge.append(Gtk.Label(label="CHIONE\nDISPLAY"))
            foot.append(badge)
            hero.append(foot)
            g.add(hero)
            page.add(g)

            # Display
            g = Adw.PreferencesGroup(title="EXIBIÇÃO")
            self.mode_row = Adw.ComboRow(
                title="Modo", model=Gtk.StringList.new(
                    ["CPU", "Fan", "Alternar"]))
            self.mode_row.set_selected(MODES.index(st["mode"]))
            self.mode_row.connect("notify::selected", self.on_mode)
            self.cycle_row = Adw.SpinRow.new_with_range(
                core.MIN_CYCLE_SECONDS, core.MAX_CYCLE_SECONDS, 1)
            self.cycle_row.set_title("Trocar a cada (s)")
            self.cycle_row.set_value(st["cycle"])
            self.cycle_row.connect("notify::value", self.on_spin)
            self.unit_row = Adw.ComboRow(
                title="Unidade", model=Gtk.StringList.new(["°C", "°F"]))
            self.unit_row.set_selected(1 if st["fahrenheit"] else 0)
            self.unit_row.connect("notify::selected", self.on_unit)
            for r in (self.mode_row, self.cycle_row, self.unit_row):
                g.add(r)
            page.add(g)

            # Alarm
            g = Adw.PreferencesGroup(title="ALARME")
            self.alarm_sw = Adw.SwitchRow(title="Piscar acima de")
            self.alarm_sw.set_active(st["alarm"] is not None)
            self.alarm_sw.connect("notify::active", self.on_spin)
            self.alarm_row = Adw.SpinRow.new_with_range(30, 120, 1)
            self.alarm_row.set_title("Limite (°C)")
            self.alarm_row.set_value(st["alarm"] or DEFAULT_ALARM)
            self.alarm_row.connect("notify::value", self.on_spin)
            g.add(self.alarm_sw)
            g.add(self.alarm_row)
            page.add(g)

            # Fan
            g = Adw.PreferencesGroup(title="FAN")
            self.fan_row = Adw.ComboRow(
                title="Fonte do fan",
                model=Gtk.StringList.new(fan_labels([])))
            self.fan_row.connect("notify::selected", self.on_fan)
            g.add(self.fan_row)
            page.add(g)

            # Service
            g = Adw.PreferencesGroup(title="SERVIÇO")
            self.svc_sw = Adw.SwitchRow(title="Serviço ativo")
            self.svc_sw.connect("notify::active", self.on_service)
            g.add(self.svc_sw)
            self.test_btn = Gtk.Button(label="Testar display",
                                       valign=Gtk.Align.CENTER,
                                       css_classes=["pill-solid"])
            self.test_btn.connect("clicked", self.on_test)
            row = Adw.ActionRow(title="Testar display",
                                subtitle="Para o serviço e roda o padrão de teste")
            row.add_suffix(self.test_btn)
            g.add(row)
            log_btn = Gtk.Button(label="Ver log", valign=Gtk.Align.CENTER,
                                 css_classes=["pill-outline"])
            log_btn.connect("clicked", self.on_log)
            row = Adw.ActionRow(title="Log do serviço")
            row.add_suffix(log_btn)
            g.add(row)
            page.add(g)

            self.debounce = Debouncer(
                GLib.timeout_add, GLib.source_remove, DEBOUNCE_MS, self.apply)
            self.loading = False
            self.sync_sensitivity()
            self.refresh_service()
            self.load_fans()
            self.tick()
            GLib.timeout_add(1000, self.tick)
            GLib.timeout_add_seconds(3, self.refresh_service)
            if load_err:
                self.toast(f"settings.json inválido, usando padrões: {load_err}")

        def toast(self, text):
            self.toasts.add_toast(Adw.Toast(title=text, timeout=2))

        def sync_sensitivity(self):
            self.cycle_row.set_sensitive(self.mode_row.get_selected() == 2)
            self.alarm_row.set_sensitive(self.alarm_sw.get_active())

        def save(self, **changes):
            try:
                changed = self.store.update(**changes)
            except (ValueError, OSError) as exc:
                self.toast(f"Erro ao salvar: {exc}")
                return
            if changed:
                self.toast("Aplicado")

        def apply(self):
            self.save(
                cycle=float(self.cycle_row.get_value()),
                alarm=(float(self.alarm_row.get_value())
                       if self.alarm_sw.get_active() else None))

        def on_spin(self, *_):
            self.sync_sensitivity()
            if not self.loading:
                self.debounce.call()

        def on_mode(self, *_):
            self.sync_sensitivity()
            if not self.loading:
                self.save(mode=MODES[self.mode_row.get_selected()])

        def on_unit(self, *_):
            if not self.loading:
                self.save(fahrenheit=self.unit_row.get_selected() == 1)

        def on_fan(self, *_):
            if not self.loading:
                self.save(**fan_settings(self.fans,
                                         self.fan_row.get_selected()))

        def load_fans(self):
            def work():
                fans = scan_fans()
                GLib.idle_add(done, fans)

            def done(fans):
                if self.closed:
                    return False
                self.fans = fans
                self.loading = True
                self.fan_row.set_model(Gtk.StringList.new(fan_labels(fans)))
                self.fan_row.set_selected(fan_index(fans, self.store.state))
                self.loading = False
                return False

            threading.Thread(target=work, daemon=True).start()

        def tick(self):
            # One read at a time: skip this tick if the last is still running.
            if self.busy_live:
                return True
            self.busy_live = True
            state = dict(self.store.state)

            def work():
                GLib.idle_add(done, state, *read_live(state))

            def done(state, cpu, rpm):
                self.busy_live = False
                if not self.closed:
                    self.cpu_lbl.set_text(
                        format_temp(cpu, state["fahrenheit"]))
                    self.fan_lbl.set_text(format_rpm(rpm))
                return False

            threading.Thread(target=work, daemon=True).start()
            return True

        def refresh_service(self, *_):
            def work():
                GLib.idle_add(done, service_installed(), service_active())

            def done(installed, active):
                if self.closed:
                    return False
                self.banner.set_revealed(not installed)
                self.loading = True
                self.svc_sw.set_active(active)
                self.svc_sw.set_subtitle("ativo" if active else "parado")
                self.loading = False
                return False

            threading.Thread(target=work, daemon=True).start()
            return True

        def on_service(self, *_):
            if self.loading:
                return
            want = self.svc_sw.get_active()

            def work():
                ok, _out = systemctl(
                    "enable" if want else "disable", "--now", SERVICE)
                GLib.idle_add(done, ok)

            def done(ok):
                if not self.closed:
                    if not ok:
                        self.toast("Não foi possível alterar o serviço")
                    self.refresh_service()
                return False

            threading.Thread(target=work, daemon=True).start()

        def on_test(self, *_):
            self.test_btn.set_sensitive(False)
            self.toast("Testando o display...")

            def work():
                err = run_display_test()
                GLib.idle_add(done, err)

            def done(err):
                if self.closed:
                    return False
                self.test_btn.set_sensitive(True)
                self.toast(f"Teste falhou: {err}" if err else "Teste concluído")
                self.refresh_service()
                return False

            threading.Thread(target=work, daemon=True).start()

        def on_log(self, *_):
            dlg = Adw.Window(transient_for=self, modal=True, title="Log",
                             default_width=640, default_height=420)
            view = Gtk.TextView(editable=False, monospace=True,
                                left_margin=8, top_margin=8)
            view.get_buffer().set_text(journal_tail())
            sc = Gtk.ScrolledWindow(vexpand=True)
            sc.set_child(view)
            tb = Adw.ToolbarView()
            tb.add_top_bar(Adw.HeaderBar())
            tb.set_content(sc)
            dlg.set_content(tb)
            dlg.present()

        def do_close_request(self):
            self.debounce.flush()
            self.closed = True
            return False

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID)

        def do_activate(self):
            win = self.props.active_window or Window(self)
            win.present()

    return App().run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(run_gui())
