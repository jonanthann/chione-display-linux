import contextlib, importlib.util, io, os, sys, unittest, urllib.error
from types import ModuleType
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_win():
    # hidapi so existe no Windows: stub de `hid` antes do import
    with mock.patch.dict(sys.modules, {"hid": ModuleType("hid")}):
        spec = importlib.util.spec_from_file_location(
            "chione_win", os.path.join(ROOT, "windows", "chione_temp_display_win.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


w = load_win()


class Stop(Exception):
    pass


class TestWinArgs(unittest.TestCase):
    def parse(self, *a):
        with contextlib.redirect_stderr(io.StringIO()):
            return w.parse_args(list(a))

    def test_interval_rejected(self):
        for v in ("nan", "inf", "-inf", "0.05", "61"):
            with self.subTest(v=v):
                with self.assertRaises(SystemExit) as cm:
                    self.parse("--interval", v)
                self.assertEqual(cm.exception.code, 2)

    def test_interval_accepted(self):
        for v in ("0.1", "60"):
            self.assertEqual(self.parse("--interval", v).interval, float(v))

    def test_wait_negative_other_than_minus_one_rejected(self):
        for v in ("-2", "-0.5"):
            with self.subTest(v=v):
                with self.assertRaises(SystemExit) as cm:
                    self.parse("--wait", v)
                self.assertEqual(cm.exception.code, 2)

    def test_wait_valid(self):
        for v in ("-1", "0", "30"):
            self.assertEqual(self.parse("--wait", v).wait, float(v))


class TestWinRun(unittest.TestCase):
    CPU = ("PC", "AMD Ryzen 7", "Temperatures", "Core (Tctl/Tdie)")
    BOARD = ("PC", "Mainboard", "Temperatures", "System")

    def setUp(self):
        self.t = [1000.0]
        def sleep(s):
            self.t[0] += s
            if self.t[0] > 1030:
                raise Stop
        def boom(*a): raise urllib.error.URLError("down")
        for p in (mock.patch.object(w.time, "monotonic", lambda: self.t[0]),
                  mock.patch.object(w.time, "sleep", sleep),
                  mock.patch.object(w, "read_sensor", boom),
                  mock.patch.object(w, "write_report", lambda d, p: None),
                  mock.patch.object(w, "fetch_lhm_tree", lambda u: None)):
            p.start(); self.addCleanup(p.stop)
        self.dev = mock.Mock(); self.dev.read.return_value = []

    def run_with(self, sensor, found_list, wanted=None):
        out = io.StringIO()
        with mock.patch.object(w, "walk_temperature_sensors",
                               lambda tree: iter(found_list)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(Stop):
                w.run(self.dev, "u", sensor, wanted, 0.5, False)

    def test_cpu_sensor_not_replaced_by_non_cpu_fallback(self):
        s = {"path": self.CPU, "name": self.CPU[-1]}
        self.run_with(s, [(self.BOARD, "System", 40.0)])
        self.assertEqual(s["path"], self.CPU)

    def test_cpu_sensor_replaced_by_other_cpu_sensor(self):
        new = ("PC", "AMD Ryzen 7", "Temperatures", "Core Max")
        s = {"path": self.CPU, "name": self.CPU[-1]}
        self.run_with(s, [(new, "Core Max", 50.0)])
        self.assertEqual(s["path"], new)

    def test_non_cpu_sensor_can_move_to_non_cpu(self):
        s = {"path": self.BOARD, "name": "System"}
        other = ("PC", "GPU", "Temperatures", "Core")
        self.run_with(s, [(other, "Core", 50.0)])
        self.assertEqual(s["path"], other)

    def test_explicit_sensor_may_pick_non_cpu(self):
        s = {"path": self.CPU, "name": self.CPU[-1]}
        self.run_with(s, [(self.BOARD, "System", 40.0)], wanted="System")
        self.assertEqual(s["path"], self.BOARD)


if __name__ == "__main__":
    unittest.main()
