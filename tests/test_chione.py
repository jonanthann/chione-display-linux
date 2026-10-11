import contextlib, errno, glob as _glob, io, os, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chione_temp_display as m

BLANK = m.BLANK


class FakeGlob:
    """Redireciona /sys/class/hwmon para um diretorio falso."""
    def __init__(self, root):
        self.root = root
    def glob(self, pat):
        return _glob.glob(pat.replace("/sys/class/hwmon", self.root))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "hwmon")
        os.mkdir(self.root)
        p = mock.patch.object(m, "glob", FakeGlob(self.root))
        p.start(); self.addCleanup(p.stop)
        m._stop = False
        self.addCleanup(setattr, m, "_stop", False)

    def mk(self, n, name, temp="45000\n", fname="temp1_input"):
        d = os.path.join(self.root, f"hwmon{n}")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "name"), "w") as f:
            f.write(name + "\n")
        if temp is not None:
            with open(os.path.join(d, fname), "w") as f:
                f.write(temp)
        return os.path.join(d, fname)


class TestResolve(Base):
    def test_match(self):
        self.mk(1, "nvme"); p = self.mk(2, "k10temp")
        self.assertEqual(m.find_hwmon_temp_path("k10temp", "temp1_input"), p)

    def test_absent(self):
        self.mk(1, "nvme")
        self.assertIsNone(m.find_hwmon_temp_path("k10temp", "temp1_input"))
        self.assertEqual(m.resolve_hwmon_temp_path(None, "temp1_input"), (None, None))

    def test_missing_temp_file(self):
        self.mk(1, "k10temp", temp=None)
        self.assertIsNone(m.find_hwmon_temp_path("k10temp", "temp1_input"))

    def test_candidate_order(self):
        self.mk(1, "coretemp"); p = self.mk(2, "k10temp")
        self.assertEqual(m.resolve_hwmon_temp_path(None, "temp1_input"), (p, "k10temp"))

    def test_explicit_driver_only(self):
        self.mk(1, "k10temp"); p = self.mk(2, "coretemp")
        self.assertEqual(m.resolve_hwmon_temp_path("coretemp", "temp1_input"), (p, "coretemp"))
        self.assertEqual(m.resolve_hwmon_temp_path("zenpower", "temp1_input"), (None, None))

    def test_renumbered(self):
        old = self.mk(3, "k10temp")
        self.assertTrue(m.find_hwmon_temp_path("k10temp", "temp1_input").endswith("hwmon3/temp1_input"))
        os.remove(old); os.remove(os.path.join(self.root, "hwmon3", "name")); os.rmdir(os.path.join(self.root, "hwmon3"))
        new = self.mk(5, "k10temp")
        self.assertEqual(m.find_hwmon_temp_path("k10temp", "temp1_input"), new)

    def test_name_unreadable_dir_skipped(self):
        os.makedirs(os.path.join(self.root, "hwmon0"))  # sem arquivo name
        p = self.mk(1, "k10temp")
        self.assertEqual(m.find_hwmon_temp_path("k10temp", "temp1_input"), p)

    def test_read_temp_ok(self):
        self.assertEqual(m.read_temp_celsius(self.mk(1, "k10temp", "45500\n")), 45.5)
        self.assertEqual(m.read_temp_celsius(self.mk(2, "k10temp", "-5000")), -5.0)

    def test_read_temp_garbage(self):
        for bad in ("", "abc", "45.5", "\n"):
            p = self.mk(1, "k10temp", bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                m.read_temp_celsius(p)

    def test_read_temp_unreadable(self):
        with self.assertRaises(OSError):
            m.read_temp_celsius(os.path.join(self.root, "nada"))
        with self.assertRaises(OSError):  # diretorio no lugar do arquivo
            m.read_temp_celsius(self.root)

    def test_name_with_invalid_utf8(self):
        d = os.path.join(self.root, "hwmon1"); os.makedirs(d)
        with open(os.path.join(d, "name"), "wb") as f:
            f.write(b"\xff\xfe\n")
        p = self.mk(2, "k10temp")
        self.assertEqual(m.find_hwmon_temp_path("k10temp", "temp1_input"), p)


class TestFormat(unittest.TestCase):
    def d(self, t): return m.temp_to_digits(t)

    def test_normal(self):
        self.assertEqual(self.d(45.5), (4, 5, 5))
        self.assertEqual(self.d(7.0), (0, 7, 0))
        self.assertEqual(self.d(12.34), (1, 2, 3))

    def test_zero_negative(self):
        self.assertEqual(self.d(0), (0, 0, 0))
        self.assertEqual(self.d(-0.0), (0, 0, 0))
        self.assertEqual(self.d(-10), (0, 0, 0))
        self.assertEqual(self.d(-273.15), (0, 0, 0))

    def test_upper_limit(self):
        self.assertEqual(self.d(99.9), (9, 9, 9))
        self.assertEqual(self.d(99.94), (9, 9, 9))
        self.assertEqual(self.d(99.95), (9, 9, 9))
        self.assertEqual(self.d(100), (9, 9, 9))
        self.assertEqual(self.d(150.7), (9, 9, 9))

    def test_absurd(self):
        self.assertEqual(self.d(1e12), (9, 9, 9))
        self.assertEqual(self.d(float("inf")), (9, 9, 9))
        self.assertEqual(self.d(-float("inf")), (0, 0, 0))

    def test_rounding_carry(self):
        self.assertEqual(self.d(44.96), (4, 5, 0))
        self.assertEqual(self.d(9.96), (1, 0, 0))
        self.assertEqual(self.d(29.99), (3, 0, 0))

    def test_digits_always_valid(self):
        for i in range(-50, 1200):
            t = i / 10 + 0.04
            a, b, c = self.d(t)
            for x in (a, b, c):
                self.assertTrue(0 <= x <= 9, (t, a, b, c))

    def test_packet(self):
        p = m.make_update_packet(4, 5, 6)
        self.assertEqual(len(p), 64)
        self.assertEqual(p[:8], bytes([0x3A, 0xB5, 1, 0x20, 4, 5, 6, 1]))
        self.assertEqual(p[16:], bytes(48))
        self.assertEqual(m.make_update_packet(BLANK, BLANK, BLANK)[4:7], b"   ")


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class RunBase(Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.dev = os.path.join(self.tmp.name, "dev")
        open(self.dev, "wb").close()
        self.sleeps = []
        self.on_sleep = lambda: None
        def fake_sleep(s):
            self.sleeps.append(s)
            self.clock.t += s
            self.on_sleep()
        for p in (mock.patch.object(m.time, "monotonic", self.clock),
                  mock.patch.object(m, "_sleep_interruptible", fake_sleep),
                  mock.patch.object(m, "_read_response", lambda fd: None)):
            p.start(); self.addCleanup(p.stop)

    def packets(self):
        data = open(self.dev, "rb").read()
        self.assertEqual(len(data) % 64, 0)
        return [data[i:i + 64] for i in range(0, len(data), 64)]

    def shown(self):
        return [tuple(p[4:7]) for p in self.packets()]

    def stop_after(self, secs):
        end = self.clock.t + secs
        def cb():
            if self.clock.t >= end:
                m._stop = True
        self.on_sleep = cb


class TestRun(RunBase):
    def test_normal_and_blank_on_exit(self):
        p = self.mk(1, "k10temp", "45500")
        self.stop_after(1.0)
        sensor = {"path": p, "name": "k10temp"}
        self.assertEqual(m.run(self.dev, sensor, None, "temp1_input", 0.5, False), "stop")
        s = self.shown()
        self.assertEqual(s[0], (4, 5, 5))
        self.assertEqual(s[-1], (BLANK,) * 3)

    def test_stale_blanks_after_5s(self):
        p = self.mk(1, "k10temp", "45500")
        sensor = {"path": p, "name": "k10temp"}
        def cb():
            if self.clock.t >= 1002.0 and os.path.exists(p):
                os.remove(p)   # sensor some
            if self.clock.t >= 1012: m._stop = True
        self.on_sleep = cb
        m.run(self.dev, sensor, "k10temp", "temp1_input", 0.5, False)
        s = self.shown()[:-1]  # sem o apagar final
        self.assertIn((4, 5, 5), s)
        self.assertEqual(s[-1], (BLANK,) * 3)
        # nao apaga antes de 5 s sem leitura boa (ultima boa em ~1002)
        first_blank = s.index((BLANK,) * 3)
        n_good_after = first_blank  # pacotes antes do blank
        self.assertGreaterEqual(n_good_after * 0.5, 5.0 - 0.5 - 2.5)

    def test_stale_threshold_not_before_5s(self):
        p = self.mk(1, "k10temp", "45500")
        sensor = {"path": p, "name": "k10temp"}
        state = {}
        def cb():
            if "gone" not in state:
                os.remove(p); state["gone"] = self.clock.t
            if self.clock.t - state["gone"] >= 4.5:
                m._stop = True
        self.on_sleep = cb
        m.run(self.dev, sensor, "k10temp", "temp1_input", 0.5, False)
        # leitura boa so no 1o ciclo; <5 s depois nada de blank (exceto o final)
        self.assertNotIn((BLANK,) * 3, self.shown()[:-1])

    def test_never_readable_blank_after_stale(self):
        sensor = {"path": os.path.join(self.root, "nope"), "name": "x"}
        self.stop_after(8)
        m.run(self.dev, sensor, None, "temp1_input", 0.5, False)
        s = self.shown()
        self.assertTrue(s)  # nada de crash; ao menos o blank
        self.assertTrue(all(x == (BLANK,) * 3 for x in s))

    def test_garbage_reading_uses_last_good_then_stale(self):
        p = self.mk(1, "k10temp", "50000")
        sensor = {"path": p, "name": "k10temp"}
        def cb():
            if self.clock.t >= 1001 and open(p).read() == "50000":
                open(p, "w").write("lixo")
            if self.clock.t >= 1003: m._stop = True
        self.on_sleep = cb
        m.run(self.dev, sensor, None, "temp1_input", 0.5, False)
        s = self.shown()[:-1]
        self.assertTrue(all(x == (5, 0, 0) for x in s), s)

    def test_reresolve_when_stale_updates_sensor(self):
        old = self.mk(3, "k10temp", "40000")
        sensor = {"path": old, "name": "k10temp"}
        state = {}
        def cb():
            if self.clock.t >= 1001 and "moved" not in state:
                os.remove(old); os.remove(os.path.join(self.root, "hwmon3", "name"))
                os.rmdir(os.path.join(self.root, "hwmon3"))
                state["new"] = self.mk(5, "k10temp", "61500")
                state["moved"] = True
            if self.clock.t >= 1015: m._stop = True
        self.on_sleep = cb
        m.run(self.dev, sensor, None, "temp1_input", 0.5, False)
        self.assertEqual(sensor["path"], state["new"])
        self.assertIn((6, 1, 5), self.shown())

    def test_reresolve_rate_limited_3s(self):
        sensor = {"path": os.path.join(self.root, "nope"), "name": "x"}
        calls = []
        def fake_resolve(h, t):
            calls.append(self.clock.t); return None, None
        self.stop_after(20)
        with mock.patch.object(m, "resolve_hwmon_temp_path", fake_resolve):
            m.run(self.dev, sensor, None, "temp1_input", 0.5, False)
        self.assertGreaterEqual(len(calls), 2)
        for a, b in zip(calls, calls[1:]):
            self.assertGreaterEqual(b - a, m.RECONNECT_DELAY_SECONDS - 1e-9)
        # primeira so depois de stale (>5 s)
        self.assertGreaterEqual(calls[0] - 1000.0, 5.0)

    def test_write_failure_returns_lost(self):
        p = self.mk(1, "k10temp", "45000")
        sensor = {"path": p, "name": "k10temp"}
        real = os.write
        def bad(fd, b): raise OSError(errno.EIO, "Input/output error")
        with mock.patch.object(m.os, "write", bad):
            self.assertEqual(m.run(self.dev, sensor, None, "temp1_input", 0.5, False), "lost")

    def test_write_failure_closes_fd(self):
        p = self.mk(1, "k10temp", "45000")
        sensor = {"path": p, "name": "k10temp"}
        closed = []
        real_close = os.close
        def c(fd): closed.append(fd); real_close(fd)
        with mock.patch.object(m.os, "write", side_effect=OSError(errno.EIO, "x")), \
             mock.patch.object(m.os, "close", c):
            m.run(self.dev, sensor, None, "temp1_input", 0.5, False)
        self.assertEqual(len(closed), 1)

    def test_main_reconnects_after_lost(self):
        p = self.mk(1, "k10temp", "45000")
        results = ["lost", OSError(errno.EIO, "io"), "lost", "stop"]
        calls = []
        def fake_run(*a):
            calls.append(a)
            r = results[len(calls) - 1]
            if isinstance(r, Exception): raise r
            return r
        with mock.patch.object(m, "run", fake_run), \
             mock.patch.object(m, "find_hidraw_device", lambda v, p: "/dev/hidraw9"), \
             mock.patch.object(m.signal, "signal"):
            rc = m.main(["--device", self.dev])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 4)

    def test_main_permission_error_retries(self):
        self.mk(1, "k10temp", "45000")
        results = [PermissionError(errno.EACCES, "x"), "stop"]
        calls = []
        def fake_run(*a):
            calls.append(1); r = results[len(calls) - 1]
            if isinstance(r, Exception): raise r
            return r
        with mock.patch.object(m, "run", fake_run), mock.patch.object(m.signal, "signal"):
            self.assertEqual(m.main(["--device", self.dev]), 0)
        self.assertEqual(len(calls), 2)


class TestStartupWait(RunBase):
    def main(self, *extra):
        with mock.patch.object(m.signal, "signal"), \
             mock.patch.object(m, "run", lambda *a: "stop"):
            return m.main(list(extra) + ["--device", self.dev])

    def test_exit_2_after_60s(self):
        self.mk(1, "nvme")
        self.assertEqual(self.main(), m.EXIT_CONFIG_ERROR)
        self.assertEqual(m.EXIT_CONFIG_ERROR, 2)
        self.assertAlmostEqual(sum(self.sleeps), 60.0, delta=m.SENSOR_POLL_SECONDS)
        self.assertGreaterEqual(sum(self.sleeps), 60.0)

    def test_sensor_appears_midway(self):
        def cb():
            if self.clock.t >= 1010 and not glob_has():
                self.mk(2, "k10temp")
        glob_has = lambda: bool(m.find_hwmon_temp_path("k10temp", "temp1_input"))
        self.on_sleep = cb
        self.assertEqual(self.main(), 0)
        self.assertLess(sum(self.sleeps), 20)

    def test_sensor_present_no_wait(self):
        self.mk(1, "k10temp")
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.sleeps, [])

    def test_stop_during_wait_returns_0(self):
        def cb(): m._stop = True
        self.on_sleep = cb
        self.assertEqual(self.main(), 0)

    def test_pinned_hwmon_missing_exit_2(self):
        self.mk(1, "k10temp")
        self.assertEqual(self.main("--hwmon", "coretemp"), 2)

    def test_wait_uses_constants(self):
        self.mk(1, "nvme")
        with mock.patch.object(m, "SENSOR_WAIT_SECONDS", 6.0):
            self.assertEqual(self.main(), 2)
        self.assertLessEqual(sum(self.sleeps), 6.0 + m.SENSOR_POLL_SECONDS)


class TestLogThrottle(RunBase):
    def run_logged(self, secs, debug, interval=1.0, changing=True):
        p = self.mk(1, "k10temp", "30000")
        sensor = {"path": p, "name": "k10temp"}
        n = {"i": 0}
        def cb():
            if changing:
                n["i"] += 1
                with open(p, "w") as f:
                    f.write(str(30000 + 1000 * (n["i"] % 60)))
            if self.clock.t >= end:
                m._stop = True
        end = self.clock.t + secs
        self.on_sleep = cb
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.run(self.dev, sensor, None, "temp1_input", interval, debug)
        return out.getvalue(), err.getvalue(), p

    def cpu_lines(self, text):
        return [l for l in text.splitlines() if l.startswith("CPU:")]

    def test_no_debug_limits_to_one_per_period(self):
        out, _, _ = self.run_logged(130, False)
        n = len(self.cpu_lines(out))
        # primeira leitura + uma a cada 60 s (t=60, t=120)
        self.assertEqual(n, 3)

    def test_first_reading_logged_immediately(self):
        out, _, _ = self.run_logged(5, False)
        self.assertEqual(len(self.cpu_lines(out)), 1)

    def test_unchanged_value_not_logged_again(self):
        out, _, _ = self.run_logged(130, False, changing=False)
        self.assertEqual(len(self.cpu_lines(out)), 1)

    def test_value_settles_inside_window_logged_when_due(self):
        p = self.mk(1, "k10temp", "30000")
        sensor = {"path": p, "name": "k10temp"}
        t0 = self.clock.t
        def cb():
            t = self.clock.t - t0
            # muda dentro da janela (t=10) e estabiliza; vence em t=60
            if t >= 10:
                with open(p, "w") as f: f.write("41000")
            if t >= 130:
                m._stop = True
        self.on_sleep = cb
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.run(self.dev, sensor, None, "temp1_input", 1.0, False)
        cpu = self.cpu_lines(out.getvalue())
        # primeira (30.0), depois 41.0 uma vez ao vencer a janela, sem repetir
        self.assertEqual(len(cpu), 2)
        self.assertIn("30.0C", cpu[0])
        self.assertIn("41.0C", cpu[1])

    def test_debug_logs_every_change(self):
        out, _, _ = self.run_logged(10, True)
        # um valor novo por ciclo: todas as leituras mudam, todas logam
        self.assertGreaterEqual(len(self.cpu_lines(out)), 10)

    def test_stale_transitions_log_immediately(self):
        p = self.mk(1, "k10temp", "45000")
        sensor = {"path": p, "name": "k10temp"}
        t0 = self.clock.t
        def cb():
            t = self.clock.t - t0
            if 2 <= t < 3 and os.path.exists(p):
                os.remove(p)
            if t >= 10 and not os.path.exists(p):
                with open(p, "w") as f:
                    f.write("50000")
            if t >= 12:
                m._stop = True
        self.on_sleep = cb
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.run(self.dev, sensor, "k10temp", "temp1_input", 0.5, False)
        # tudo bem antes de 60 s: stale e volta ao normal logam na hora
        self.assertIn("display blanked", err.getvalue())
        cpu = self.cpu_lines(out.getvalue())
        self.assertEqual(len(cpu), 2)
        self.assertIn("50.0C", cpu[-1])


class TestIntervalArg(unittest.TestCase):
    def parse(self, v):
        with contextlib.redirect_stderr(io.StringIO()):
            return m.parse_args(["--interval", v])

    def test_rejected(self):
        for v in ("nan", "inf", "-inf", "0.05", "61", "-1"):
            with self.subTest(v=v):
                with self.assertRaises(SystemExit) as cm:
                    self.parse(v)
                self.assertEqual(cm.exception.code, 2)

    def test_accepted_limits(self):
        for v in ("0.1", "60", "1"):
            with self.subTest(v=v):
                self.assertEqual(self.parse(v).interval, float(v))


class TestWarnDedup(RunBase):
    def test_cannot_open_warned_once_then_rearmed(self):
        self.mk(1, "k10temp", "45000")
        real_open, real_write = os.open, os.write
        st = {"opens": 0, "writes": 0}
        def fake_open(path, *a, **k):
            if path != self.dev:
                return real_open(path, *a, **k)
            st["opens"] += 1
            if st["opens"] >= 5:
                m._stop = True
            if st["opens"] == 3:
                return real_open(path, *a, **k)
            raise OSError(errno.ENODEV, "No such device")
        # abertura 3 funciona; a 2a escrita falha (lost); depois volta a falhar
        def fake_write(fd, data):
            st["writes"] += 1
            if st["writes"] == 2:
                raise OSError(errno.EIO, "io")
            return real_write(fd, data)
        out = io.StringIO()
        with mock.patch.object(m.signal, "signal"), \
             mock.patch.object(m.os, "open", fake_open), \
             mock.patch.object(m.os, "write", fake_write), \
             contextlib.redirect_stdout(out):
            m.main(["--device", self.dev, "--interval", "1"])
        n = out.getvalue().count("Cannot open device")
        # aberturas 1,2 falham (1 aviso); 3 ok escreve e re-arma; 4 volta a falhar,
        # 5 ja pede parada
        # 4 falha de novo apos sucesso: aviso reaparece
        self.assertEqual(n, 2)

    def test_repeated_open_failure_single_line(self):
        self.mk(1, "k10temp", "45000")
        st = {"n": 0}
        real_open = os.open
        def fake_open(path, *a, **k):
            if path != self.dev:
                return real_open(path, *a, **k)
            st["n"] += 1
            if st["n"] >= 4:
                m._stop = True
            raise OSError(errno.ENODEV, "No such device")
        out = io.StringIO()
        with mock.patch.object(m.signal, "signal"), \
             mock.patch.object(m.os, "open", fake_open), \
             contextlib.redirect_stdout(out):
            m.main(["--device", self.dev])
        self.assertEqual(out.getvalue().count("Cannot open device"), 1)

    def test_io_failed_deduped_and_rearmed_by_success_in_run(self):
        p = self.mk(1, "k10temp", "45000")
        sensor = {"path": p, "name": "k10temp"}
        warned = set()
        real_write = os.write
        def bad(fd, data): raise OSError(errno.EIO, "io")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with mock.patch.object(m.os, "write", bad):
                for _ in range(3):
                    self.assertEqual(m.run(self.dev, sensor, None, "temp1_input", 0.5, False, warned), "lost")
            self.assertEqual(out.getvalue().count("Device I/O failed"), 1)
            m.run(self.dev, sensor, None, "temp1_input", 0.5, False, warned) if False else None
            self.stop_after(1.0)
            self.assertEqual(m.run(self.dev, sensor, None, "temp1_input", 0.5, False, warned), "stop")
            self.assertFalse(any(w.startswith("Device I/O") for w in warned))
            m._stop = False
            with mock.patch.object(m.os, "write", bad):
                m.run(self.dev, sensor, None, "temp1_input", 0.5, False, warned)
        self.assertEqual(out.getvalue().count("Device I/O failed"), 2)


if __name__ == "__main__":
    unittest.main()
