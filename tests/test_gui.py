import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chione_gui as g

ROOT = g.HERE
FANS = [
    {"hwmon": "it8688", "file": "fan1_input", "label": "", "rpm": 1451,
     "gpu": False},
    {"hwmon": "amdgpu", "file": "fan1_input", "label": "", "rpm": None,
     "gpu": True},
]


class TestArgs(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(g.parse_chione_args('CHIONE_ARGS="--mode auto"\n'),
                         {"mode": "auto"})
        got = g.parse_chione_args(
            'CHIONE_ARGS="--mode=fan --cycle 7 --fahrenheit --alarm 85 '
            '--fan-hwmon it8688 --fan-input fan2_input"')
        self.assertEqual(got, {"mode": "fan", "cycle": 7.0, "fahrenheit": True,
                               "alarm": 85.0, "fan_hwmon": "it8688",
                               "fan_input": "fan2_input"})

    def test_bad_input_ignored(self):
        self.assertEqual(g.parse_chione_args(""), {})
        self.assertEqual(g.parse_chione_args('CHIONE_ARGS="--mode bogus"'), {})
        self.assertEqual(g.parse_chione_args('CHIONE_ARGS="--mode'), {})

    def test_initial_state_precedence(self):
        st = g.initial_state({"cycle": 9.0}, {"mode": "auto", "cycle": 3.0})
        self.assertEqual(st["mode"], "auto")
        self.assertEqual(st["cycle"], 9.0)
        self.assertFalse(st["fahrenheit"])
        self.assertIsNone(st["alarm"])


class TestFan(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(g.fan_settings(FANS, 0),
                         {"fan_hwmon": None, "fan_input": None})
        s = g.fan_settings(FANS, 2)
        self.assertEqual(s, {"fan_hwmon": "amdgpu", "fan_input": "fan1_input"})
        self.assertEqual(g.fan_index(FANS, s), 2)
        self.assertEqual(g.fan_index(FANS, {"fan_hwmon": "x"}), 0)

    def test_labels(self):
        labels = g.fan_labels(FANS)
        self.assertEqual(labels[0], "Automático")
        self.assertEqual(labels[1], "it8688 fan1 · 1451 RPM")
        self.assertIn("GPU", labels[2])

    def test_format(self):
        self.assertEqual(g.format_temp(48.5, False), "48.5 °C")
        self.assertEqual(g.format_temp(100, True), "212.0 °F")
        self.assertEqual(g.format_temp(None, False), "sem sensor")
        self.assertEqual(g.format_rpm(1451), "1451 RPM")


class TestStoreDebounce(unittest.TestCase):
    def test_store_saves_and_keeps_state_on_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "settings.json")
            store = g.Store(path, g.initial_state({}, {}))
            store.update(mode="fan")
            self.assertEqual(g.core.load_settings(path)["mode"], "fan")
            self.assertFalse(store.update(mode="fan"))
            with self.assertRaises(ValueError):
                store.update(cycle=500)
            self.assertEqual(store.state["cycle"],
                             g.initial_state({}, {})["cycle"])

    def test_store_writes_only_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            state = g.initial_state({}, {"mode": "auto"})
            store = g.Store(path, state, {})
            self.assertFalse(store.update(mode="auto"))
            self.assertFalse(os.path.exists(path))
            store.update(fahrenheit=True)
            self.assertEqual(g.core.load_settings(path), {"fahrenheit": True})
            store.update(alarm=90.0)
            self.assertEqual(g.core.load_settings(path),
                             {"fahrenheit": True, "alarm": 90.0})
            self.assertEqual(store.state["mode"], "auto")

    def test_store_keeps_loaded_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            loaded = {"cycle": 9.0}
            store = g.Store(path, g.initial_state(loaded, {}), loaded)
            store.update(mode="fan")
            self.assertEqual(g.core.load_settings(path),
                             {"cycle": 9.0, "mode": "fan"})

    def test_debounce(self):
        calls, pending = [], {}

        def schedule(ms, cb):
            pending["cb"] = cb
            pending["n"] = pending.get("n", 0) + 1
            return pending["n"]

        def cancel(h):
            if h == pending["n"]:
                pending.pop("cb")

        d = g.Debouncer(schedule, cancel, 300, lambda: calls.append(1))
        d.call()
        d.call()
        d.call()
        self.assertEqual(calls, [])
        pending["cb"]()
        self.assertEqual(calls, [1])
        d.call()
        d.flush()
        self.assertEqual(calls, [1, 1])


@unittest.skipUnless(os.name == "posix", "install.sh needs POSIX")
class TestInstallGui(unittest.TestCase):
    def run_sh(self, home, *args):
        env = {k: v for k, v in os.environ.items() if k != "XDG_DATA_HOME"}
        env["HOME"] = home
        return subprocess.run(["bash", os.path.join(ROOT, "install.sh"), *args],
                              env=env, capture_output=True, text=True,
                              check=False)

    def test_install_and_remove(self):
        with tempfile.TemporaryDirectory() as home:
            r = self.run_sh(home, "gui")
            self.assertEqual(r.returncode, 0, r.stderr)
            share = os.path.join(home, ".local", "share")
            desk = os.path.join(share, "applications",
                                f"{g.APP_ID}.desktop")
            icon = os.path.join(share, "icons", "hicolor", "scalable", "apps",
                                f"{g.APP_ID}.svg")
            with open(desk, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("Name=Chione Display", text)
            self.assertIn(f'"{os.path.join(ROOT, "chione_gui.py")}"', text)
            self.assertTrue(os.path.isfile(icon))
            r = self.run_sh(home, "gui", "--remove")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(desk))
            self.assertFalse(os.path.exists(icon))


if __name__ == "__main__":
    unittest.main()
