import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_chione_win import ROOT, w

import chione_temp_display as m


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def packets(path):
    with open(path, "rb") as f:
        data = f.read()
    return [data[i:i + 64] for i in range(0, len(data), 64)]


@unittest.skipUnless(os.name == "posix", "Linux driver tests need POSIX")
class TestLinuxSelfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dev = os.path.join(self.tmp.name, "hidraw")
        open(self.dev, "wb").close()
        p = mock.patch.object(m.time, "sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def test_writes_three_packets_and_returns_0(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = m.run_test(self.dev)
        self.assertEqual(rc, 0)
        expected = [
            m.make_update_packet(*m.temp_to_digits(88.8)),
            m.make_update_packet(*m.temp_to_digits(12.3)),
            m.make_update_packet(m.BLANK, m.BLANK, m.BLANK),
        ]
        self.assertEqual(packets(self.dev), expected)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertIn("88.8", out.getvalue())
        self.assertIn("12.3", out.getvalue())

    def test_missing_device_returns_1(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = m.run_test(os.path.join(self.tmp.name, "nao-existe"))
        self.assertEqual(rc, 1)
        self.assertIn("Cannot open", err.getvalue())
        self.sleep.assert_not_called()

    def test_write_oserror_returns_1_and_closes_fd(self):
        err = io.StringIO()
        with mock.patch.object(m.os, "write", side_effect=OSError(5, "io")), \
                mock.patch.object(m.os, "close") as close, \
                contextlib.redirect_stderr(err):
            rc = m.run_test(self.dev)
        self.assertEqual(rc, 1)
        self.assertIn("Cannot write", err.getvalue())
        close.assert_called_once()

    def test_main_test_flag_with_device(self):
        with quiet():
            rc = m.main(["--test", "--device", self.dev])
        self.assertEqual(rc, 0)
        self.assertEqual(len(packets(self.dev)), 3)

    def test_main_test_flag_no_device_found(self):
        with mock.patch.object(m, "find_hidraw_device", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(["--test"]), 1)


class TestVersion(unittest.TestCase):
    def check(self, mod):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as cm:
            mod.parse_args(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(mod.__version__, out.getvalue() + err.getvalue())

    def test_linux(self):
        self.check(m)

    def test_windows(self):
        self.check(w)

    def test_versions_match(self):
        self.assertEqual(m.__version__, w.__version__)


class FakeHid:
    def __init__(self, fail=False):
        self.fail = fail
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        return -1 if self.fail else len(data)

    def close(self):
        self.closed = True


class TestWinSelfTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(w.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_packets_and_rc_0(self):
        dev = FakeHid()
        with mock.patch.object(w, "open_display", return_value=dev), quiet():
            rc = w.main(["--test"])
        self.assertEqual(rc, 0)
        exp = [
            b"\x00" + w.make_update_packet(*w.temp_to_digits(88.8)),
            b"\x00" + w.make_update_packet(*w.temp_to_digits(12.3)),
            b"\x00" + w.make_update_packet(w.BLANK, w.BLANK, w.BLANK),
        ]
        self.assertEqual(dev.writes, exp)
        self.assertTrue(dev.closed)

    def test_no_device_rc_1(self):
        with mock.patch.object(w, "open_display", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(w.run_test(), 1)

    def test_write_failure_rc_1_and_closes(self):
        dev = FakeHid(fail=True)
        with mock.patch.object(w, "open_display", return_value=dev), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(w.run_test(), 1)
        self.assertTrue(dev.closed)


class TestWinLogFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "chione.log")
        self.saved = (sys.stdout, sys.stderr)
        self.addCleanup(self.restore)

    def restore(self):
        for s in (sys.stdout, sys.stderr):
            if s not in self.saved and s is not None:
                s.close()
        sys.stdout, sys.stderr = self.saved

    def test_test_mode_writes_to_file(self):
        dev = FakeHid()
        with mock.patch.object(w, "open_display", return_value=dev), \
                mock.patch.object(w.time, "sleep"):
            rc = w.main(["--test", "--log-file", self.path])
        self.restore()
        self.assertEqual(rc, 0)
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("Showing 88.8", f.read())

    def test_big_file_rotated(self):
        with open(self.path, "wb") as f:
            f.write(b"x" * (w.MAX_LOG_BYTES + 1))
        w.redirect_output(self.path)
        print("novo")
        self.restore()
        self.assertEqual(os.path.getsize(self.path + ".1"), w.MAX_LOG_BYTES + 1)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "novo\n")

    def test_file_at_limit_not_rotated(self):
        with open(self.path, "wb") as f:
            f.write(b"x" * w.MAX_LOG_BYTES)
        w.redirect_output(self.path)
        self.restore()
        self.assertFalse(os.path.exists(self.path + ".1"))
        self.assertEqual(os.path.getsize(self.path), w.MAX_LOG_BYTES)

    def test_invalid_path_exits_2(self):
        bad = os.path.join(self.tmp.name, "falta", "x.log")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = w.main(["--log-file", bad])
        self.assertEqual(rc, 2)
        self.assertIn("Cannot open log file", err.getvalue())
        self.assertIs(sys.stdout, self.saved[0])


HAVE_BASH = shutil.which("bash") is not None and os.name == "posix"
HAVE_SYSTEMD = os.path.isdir("/run/systemd/system")


@unittest.skipUnless(HAVE_BASH and HAVE_SYSTEMD,
                     "install.sh service precisa de bash e /run/systemd/system")
class TestInstallService(unittest.TestCase):
    def setUp(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("service recusa rodar como root")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.bin = os.path.join(self.tmp.name, "bin")
        os.mkdir(self.home)
        os.mkdir(self.bin)
        self.calls = os.path.join(self.tmp.name, "calls.txt")
        fake = os.path.join(self.bin, "systemctl")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> "{self.calls}"\nexit 0\n')
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        self.conf = os.path.join(self.home, ".config", "chione-temp.conf")
        self.unit = os.path.join(
            self.home, ".config", "systemd", "user", "chione-temp.service")

    def run_install(self, *args):
        env = dict(os.environ)
        env["HOME"] = self.home
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["USER"] = "tester"
        return subprocess.run(
            ["bash", os.path.join(ROOT, "install.sh"), *args],
            check=False, env=env, capture_output=True, text=True, timeout=60)

    def test_args_saved_and_unit_references_them(self):
        r = self.run_install("service", "--", "--hwmon", "k10temp",
                             "--interval", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.conf) as f:
            self.assertEqual(
                f.read(), 'CHIONE_ARGS="--hwmon k10temp --interval 1"\n')
        with open(self.unit) as f:
            unit = f.read()
        self.assertIn("EnvironmentFile=-%h/.config/chione-temp.conf", unit)
        self.assertIn("$CHIONE_ARGS", unit)
        with open(self.calls) as f:
            self.assertIn("--user", f.read())

    def test_arg_with_space_refused(self):
        r = self.run_install("service", "--", "--hwmon", "a b")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.conf))
        self.assertFalse(os.path.exists(self.unit))

    def test_empty_and_quoted_args_refused(self):
        for bad in ("", 'a"b', "a'b", "a\\b"):
            with self.subTest(bad=bad):
                r = self.run_install("service", "--", bad)
                self.assertNotEqual(r.returncode, 0)
                self.assertFalse(os.path.exists(self.conf))

    def test_without_dashdash_conf_untouched(self):
        os.makedirs(os.path.dirname(self.conf))
        with open(self.conf, "w") as f:
            f.write('CHIONE_ARGS="--hwmon coretemp"\n')
        r = self.run_install("service")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.conf) as f:
            self.assertEqual(f.read(), 'CHIONE_ARGS="--hwmon coretemp"\n')

    def test_unexpected_first_arg_refused(self):
        r = self.run_install("service", "--hwmon", "k10temp")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.conf))


if __name__ == "__main__":
    unittest.main()
