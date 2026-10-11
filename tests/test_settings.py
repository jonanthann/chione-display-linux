import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_chione as tc

import chione_core as c

m = tc.m


class TestValidate(unittest.TestCase):
    def test_normalizes(self):
        self.assertEqual(c.validate_settings({}), {})
        got = c.validate_settings({"mode": "auto", "cycle": 5, "alarm": 90,
                                   "fahrenheit": True, "fan_input": "",
                                   "other": 1})
        self.assertEqual(got, {"mode": "auto", "cycle": 5.0, "alarm": 90.0,
                               "fahrenheit": True, "fan_input": None})
        self.assertEqual(c.validate_settings({"alarm": None}), {"alarm": None})

    def test_rejects(self):
        for bad in ([], {"mode": "x"}, {"cycle": 0}, {"cycle": 61},
                    {"cycle": "5"}, {"cycle": True}, {"fahrenheit": 1},
                    {"alarm": "90"}, {"alarm": float("inf")},
                    {"alarm": True}, {"fan_hwmon": 3}):
            with self.assertRaises(ValueError, msg=bad):
                c.validate_settings(bad)

    def test_path(self):
        self.assertEqual(c.settings_path({"XDG_CONFIG_HOME": "/x"}, "posix"),
                         "/x/chione-display/settings.json")
        self.assertTrue(c.settings_path({}, "posix").endswith(
            ".config/chione-display/settings.json"))
        self.assertIn("chione-display", c.settings_path({"APPDATA": "A"}, "nt"))


class Tmp(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sub", "settings.json")


class TestFiles(Tmp):
    def test_save_load_atomic(self):
        c.save_settings(self.path, {"mode": "fan", "cycle": 3})
        self.assertEqual(c.load_settings(self.path),
                         {"mode": "fan", "cycle": 3.0})
        self.assertEqual(os.listdir(os.path.dirname(self.path)),
                         ["settings.json"])
        with self.assertRaises(ValueError):
            c.save_settings(self.path, {"mode": "bad"})
        self.assertEqual(c.load_settings(self.path)["mode"], "fan")
        self.assertEqual(os.listdir(os.path.dirname(self.path)),
                         ["settings.json"])

    def test_load_missing_and_bad(self):
        self.assertEqual(c.load_settings(self.path), {})
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write('{"mode": ')
        with self.assertRaises(ValueError):
            c.load_settings(self.path)

    def test_watcher_throttle_and_reload(self):
        t = [0.0]
        w = c.SettingsWatcher(self.path, clock=lambda: t[0])
        self.assertIsNone(w.poll())
        c.save_settings(self.path, {"mode": "fan"})
        self.assertIsNone(w.poll())  # throttled, under 1 s
        t[0] = 1.0
        self.assertEqual(w.poll(), {"mode": "fan"})
        t[0] = 2.0
        self.assertIsNone(w.poll())  # unchanged
        with open(self.path, "w") as f:
            f.write("{oops")
        t[0] = 3.0
        with self.assertRaises(ValueError):
            w.poll()
        t[0] = 4.0
        self.assertIsNone(w.poll())  # same bad content, not raised again
        self.assertEqual(w.current, {"mode": "fan"})
        os.remove(self.path)
        t[0] = 5.0
        self.assertIsNone(w.poll())
        self.assertEqual(w.current, {"mode": "fan"})


class TestLive(tc.RunBase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp.name, "sub", "settings.json")

    def run_it(self, watcher, mode="cpu", fan=None, **kw):
        t = self.mk(1, "k10temp", "45000")
        sensor = {"path": t, "name": "k10temp"}
        return m.run(self.dev, sensor, None, "temp1_input", 1.0, False, None,
                     mode, False, None, 5.0, fan, None, None, watcher, **kw)

    def test_precedence_over_cli(self):
        c.save_settings(self.path, {"fahrenheit": True})
        w = c.SettingsWatcher(self.path)
        w.poll()
        self.stop_after(1.0)
        self.run_it(w)
        pk = self.packets()[0]
        self.assertEqual((pk[3:7], pk[8]), (bytes([1, 1, 3, 0]), 0))

    def test_live_change_and_bad_json(self):
        c.save_settings(self.path, {"fahrenheit": True})
        w = c.SettingsWatcher(self.path)
        w.poll()
        step = {}

        def cb():
            t = self.clock.t
            if t >= 1003 and "a" not in step:
                step["a"] = 1
                c.save_settings(self.path, {"fahrenheit": False})
            if t >= 1006 and "b" not in step:
                step["b"] = 1
                with open(self.path, "w") as f:
                    f.write("{half")
            if t >= 1010:
                m._stop = True
        self.on_sleep = cb
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.run_it(w)
        pk = self.packets()[:-1]
        self.assertEqual(pk[0][8], 0)
        self.assertEqual(pk[-1][8], 1)  # back to Celsius, kept after bad json
        self.assertEqual(out.getvalue().count("Settings changed:"), 1)
        self.assertEqual(err.getvalue().count("Ignoring settings file"), 1)

    def test_fan_input_change_reresolves(self):
        a = self.mk(2, "nct6775", "1450\n", "fan1_input")
        b = self.mk(2, "nct6775", "900\n", "fan2_input")
        c.save_settings(self.path, {"mode": "fan", "fan_input": "fan1_input"})
        w = c.SettingsWatcher(self.path)
        w.poll()
        fan = {"path": a, "name": "nct6775"}

        def cb():
            if self.clock.t >= 1003 and "x" not in fan:
                fan["x"] = 1
                c.save_settings(self.path, {"mode": "fan",
                                            "fan_input": "fan2_input"})
            if self.clock.t >= 1007:
                m._stop = True
        self.on_sleep = cb
        with contextlib.redirect_stdout(io.StringIO()):
            self.run_it(w, "cpu", fan)
        self.assertEqual(fan["path"], b)

    def test_mode_to_auto_finds_fan(self):
        f = self.mk(2, "nct6775", "1450\n", "fan1_input")
        w = c.SettingsWatcher(self.path)
        fan = {"path": None, "name": None}

        def cb():
            if self.clock.t >= 1002 and not os.path.exists(self.path):
                c.save_settings(self.path, {"mode": "auto"})
            if self.clock.t >= 1008:
                m._stop = True
        self.on_sleep = cb
        with contextlib.redirect_stdout(io.StringIO()):
            self.run_it(w, "cpu", fan)
        self.assertEqual(fan["path"], f)

    def test_unreadable_does_not_crash(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkdir(self.path)  # a directory: open() raises OSError
        w = c.SettingsWatcher(self.path)
        self.stop_after(3.0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_it(w), "stop")


if __name__ == "__main__":
    unittest.main()
