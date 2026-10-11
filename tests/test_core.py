import argparse
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chione_core as c


class CoreTests(unittest.TestCase):
    def test_digits(self):
        self.assertEqual(c.temp_to_digits(45.04), (4, 5, 0))
        self.assertEqual(c.temp_to_digits(99.96), (9, 9, 9))
        self.assertEqual(c.temp_to_digits(-5), (0, 0, 0))
        self.assertEqual(c.temp_to_digits(12.3), (1, 2, 3))

    def test_packet(self):
        pkt = c.make_update_packet(1, 2, 3)
        self.assertEqual(len(pkt), 64)
        self.assertEqual(pkt[:7], bytes([0x3A, 0xB5, 0x01, 0x20, 1, 2, 3]))
        self.assertEqual(c.blank_packet()[4:7], bytes([c.BLANK] * 3))

    def test_test_pattern(self):
        sent, slept = [], []
        with contextlib.redirect_stdout(io.StringIO()) as out:
            c.run_test_pattern(sent.append, slept.append)
        self.assertEqual(sent, [c.make_update_packet(8, 8, 8),
                                c.make_update_packet(1, 2, 3),
                                c.blank_packet()])
        self.assertEqual(slept, [2, 2])
        self.assertIn("Showing 88.8", out.getvalue())

    def test_check_interval(self):
        p = argparse.ArgumentParser()
        for bad in (0.0, 61.0, float("nan"), float("inf")):
            with contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                c.check_interval(p, bad)
        c.check_interval(p, 0.5)

    def test_warn_once(self):
        warned = set()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            c.warn_once(warned, "x")
            c.warn_once(warned, "x")
        self.assertEqual(out.getvalue(), "x\n")

    def test_stale_and_idle(self):
        st = c.DisplayState(0.5, 0.0)
        self.assertTrue(st.idle(st.is_stale(1.0)))
        self.assertTrue(st.is_stale(c.STALE_AFTER_SECONDS + 0.1))
        st.reading(50.0, 1.0)
        self.assertFalse(st.is_stale(2.0))
        self.assertEqual(st.digits(False), (c.BLANK, 5, 0, 0))
        self.assertEqual(st.digits(True), (c.BLANK,) * 4)
        st.reading(None, 3.0)
        self.assertEqual(st.last_good, 1.0)

    def test_stale_threshold_scales_with_interval(self):
        st = c.DisplayState(60.0, 0.0)
        self.assertFalse(st.is_stale(100.0))
        self.assertTrue(st.is_stale(121.0))

    def test_log_due(self):
        st = c.DisplayState(0.5, 0.0)
        d = (4, 5, 0)
        self.assertTrue(st.log_due(d, False, 0.0, False))
        self.assertFalse(st.log_due(d, False, 1.0, False))
        self.assertFalse(st.log_due((4, 6, 0), False, 30.0, False))
        self.assertTrue(st.log_due((4, 6, 0), False, c.LOG_EVERY_SECONDS, False))
        # Stale transition logs at once.
        self.assertTrue(st.log_due((c.BLANK,) * 3, True, 61.0, False))
        # Debug logs every change.
        self.assertTrue(st.log_due((1, 1, 2), False, 63.0, True))

    def test_resolve_due(self):
        st = c.DisplayState(0.5, 0.0)
        self.assertTrue(st.resolve_due(10.0))
        self.assertFalse(st.resolve_due(11.0))
        self.assertTrue(st.resolve_due(10.0 + c.RECONNECT_DELAY_SECONDS))

    def test_format_response(self):
        self.assertEqual(c.format_response(b"\x01\x02", None),
                         ["  resp: 01 02"])
        self.assertEqual(c.format_response(b"\x01\x03", b"\x01\x02")[1],
                         "  changed offsets vs prev: [1]")


if __name__ == "__main__":
    unittest.main()
