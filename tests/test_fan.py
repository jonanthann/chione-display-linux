import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_chione as tc
import test_chione_win as tw

import chione_core as c

m = tc.m
w = tw.w
B = c.BLANK


def hexs(pkt):
    return pkt[:13].hex(" ")


class TestProtocol(unittest.TestCase):
    def test_init_packet(self):
        p = c.init_packet()
        self.assertEqual(len(p), 64)
        self.assertEqual(hexs(p), "3a b5 ff 20 20 20 20 00 01 01 00 00 70")

    def test_zero_and_blank_checksums(self):
        self.assertEqual(c.make_update_packet(0, 0, 0)[12], 0x13)
        self.assertEqual(c.blank_packet()[12], 0x73)
        self.assertEqual(c.make_update_packet(4, 5, 6)[12],
                         sum(c.make_update_packet(4, 5, 6)[:12]) & 0xFF)

    def test_fan_examples(self):
        self.assertEqual(hexs(c.make_fan_packet(c.rpm_to_digits(1450))),
                         "3a b5 01 01 04 05 00 00 00 00 01 00 fb")
        self.assertEqual(hexs(c.make_fan_packet(c.rpm_to_digits(850))),
                         "3a b5 01 20 08 05 00 00 00 00 01 00 1e")

    def test_rpm_clamp(self):
        self.assertEqual(c.rpm_to_digits(-5), (B, 0, 0, 0))
        self.assertEqual(c.rpm_to_digits(99999), (9, 9, 9, 9))

    def test_hundreds(self):
        self.assertEqual(c.temp_display_digits(51.2), (B, 5, 1, 2))
        self.assertEqual(c.temp_display_digits(100.0), (1, 0, 0, 0))
        self.assertEqual(c.temp_display_digits(999.9), (9, 9, 9, 9))
        self.assertEqual(c.temp_display_digits(5000), (9, 9, 9, 9))

    def test_fahrenheit(self):
        self.assertEqual(c.temp_display_digits(50.0, True), (1, 2, 2, 0))
        st = c.DisplayState(0.5, 0.0, "cpu", True)
        st.reading(50.0, 0.0)
        pkt = st.packet(False)
        self.assertEqual((pkt[3:7], pkt[8]), (bytes([1, 2, 2, 0]), 0))
        self.assertEqual(st.shown_text(st.digits(False)),
                         "CPU: 50.0C -> display: 122.0F")

    def test_alarm(self):
        st = c.DisplayState(0.5, 0.0, "cpu", False, 80.0)
        st.reading(79.9, 0.0)
        self.assertEqual(st.packet(False)[11], 0)
        st.reading(80.0, 0.0)
        pkt = st.packet(False)
        self.assertEqual(pkt[11], 1)
        self.assertEqual(pkt[12], sum(pkt[:12]) & 0xFF)

    def test_texts(self):
        st = c.DisplayState(0.5, 0.0)
        st.reading(51.2, 0.0)
        self.assertEqual(st.shown_text(st.digits(False)),
                         "CPU: 51.2C -> display: 51.2")
        fs = c.DisplayState(0.5, 0.0, "fan")
        fs.reading(1450, 0.0)
        self.assertEqual(fs.shown_text(fs.digits(False)),
                         "FAN: 1450 RPM -> display: 1450")
        self.assertEqual(fs.packet(True)[10], 1)

    def test_auto_alternates(self):
        seq = [c.pick_mode("auto", t, 100.0, 5.0, True)
               for t in (100, 104.9, 105, 109.9, 110)]
        self.assertEqual(seq, ["cpu", "cpu", "fan", "fan", "cpu"])
        self.assertEqual(c.pick_mode("auto", 105, 100.0, 5.0, False), "cpu")
        self.assertEqual(c.pick_mode("fan", 100, 100.0, 5.0, False), "fan")

    def test_parse_init_response(self):
        resp = bytes([0x3A, 0xB5, 0xFF, 1, 7, 1, 0, 1]) + bytes(56)
        self.assertEqual(c.parse_init_response(resp),
                         {"firmware": "1.7", "unit": "C", "mode": "FAN"})
        self.assertIsNone(c.parse_init_response(None))

    def test_cycle_range(self):
        for v in ("0.5", "61", "nan"):
            with self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                m.parse_args(["--cycle", v])
        self.assertEqual(m.parse_args(["--cycle", "1"]).cycle, 1.0)


class TestLinuxFan(tc.Base):
    def fan(self, n, name, rpm, label=None, idx=1):
        p = self.mk(n, name, f"{rpm}\n", f"fan{idx}_input")
        if label:
            with open(p.replace("_input", "_label"), "w") as f:
                f.write(label + "\n")
        return p

    def test_prefers_cpu_label_and_ignores_gpu(self):
        self.fan(1, "amdgpu", 2000, "CPU_FAN")
        self.fan(2, "nct6775", 900, "SYS_FAN1", 1)
        want = self.fan(2, "nct6775", 1450, "CPU_FAN", 2)
        self.assertEqual(m.resolve_hwmon_fan(), (want, "nct6775"))

    def test_first_live_without_label(self):
        self.fan(1, "it87", 0, None, 1)
        want = self.fan(1, "it87", 700, None, 2)
        self.assertEqual(m.resolve_hwmon_fan(), (want, "it87"))
        self.assertEqual(m.resolve_hwmon_fan("it87", "fan1_input")[0],
                         want.replace("fan2", "fan1"))

    def test_explicit_gpu_and_none(self):
        g = self.fan(1, "amdgpu", 2000)
        self.assertEqual(m.resolve_hwmon_fan(), (None, None))
        self.assertEqual(m.resolve_hwmon_fan("amdgpu"), (g, "amdgpu"))

    def test_list_marks_default_fan(self):
        self.mk(1, "k10temp")
        self.fan(2, "nct6775", 1450, "CPU_FAN")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.list_hwmon()
        self.assertIn("nct6775\tfan1_input\t1450RPM\tCPU_FAN <- default fan",
                      out.getvalue())


class TestLinuxRunFan(tc.RunBase):
    def test_fan_mode_shows_rpm_and_sends_init(self):
        t = self.mk(1, "k10temp", "45000")
        f = self.mk(2, "nct6775", "1450\n", "fan1_input")
        self.stop_after(1.0)
        m.run(self.dev, {"path": t, "name": "k10temp"}, None, "temp1_input",
              0.5, False, None, "fan", False, None, 5.0,
              {"path": f, "name": "nct6775"})
        pk = self.packets()
        self.assertEqual(pk[0][:13].hex(" "),
                         "3a b5 01 01 04 05 00 00 00 00 01 00 fb")
        self.assertEqual(self.packets(True)[0][2], 0xFF)
        self.assertEqual(pk[-1][10], 1)  # blank on exit keeps FAN mode

    def test_auto_alternates(self):
        t = self.mk(1, "k10temp", "45000")
        f = self.mk(2, "nct6775", "1450\n", "fan1_input")
        self.stop_after(11.0)
        m.run(self.dev, {"path": t, "name": "k10temp"}, None, "temp1_input",
              1.0, False, None, "auto", False, None, 5.0,
              {"path": f, "name": "nct6775"})
        modes = [p[10] for p in self.packets()[:-1]]
        self.assertEqual(modes, [0] * 5 + [1] * 5 + [0])

    def test_auto_stays_on_cpu_while_fan_is_stale(self):
        t = self.mk(1, "k10temp", "45000")
        f = self.mk(2, "nct6775", "1450\n", "fan1_input")
        def cb():
            if self.clock.t >= 1002.0 and os.path.exists(f):
                os.remove(f)
            if self.clock.t >= 1030.0:
                m._stop = True
        self.on_sleep = cb
        m.run(self.dev, {"path": t, "name": "k10temp"}, None, "temp1_input",
              1.0, False, None, "auto", False, None, 5.0,
              {"path": f, "name": "nct6775"}, None, "fan1_input")
        modes = [p[10] for p in self.packets()[:-1]]
        # Fan mode only before the fan died (first 10 s at most).
        self.assertNotIn(1, modes[12:])

    def test_auto_without_fan_picks_one_up_later(self):
        t = self.mk(1, "k10temp", "45000")
        state = {}
        def cb():
            if self.clock.t >= 1003.0 and "f" not in state:
                state["f"] = self.mk(2, "nct6775", "1450\n", "fan1_input")
            if self.clock.t >= 1040.0:
                m._stop = True
        self.on_sleep = cb
        fan = {"path": None, "name": None}
        m.run(self.dev, {"path": t, "name": "k10temp"}, None, "temp1_input",
              1.0, False, None, "auto", False, None, 5.0, fan, None,
              "fan1_input")
        self.assertEqual(fan["path"], state["f"])
        self.assertIn(1, [p[10] for p in self.packets()[:-1]])

    def test_no_fan_message_has_no_none(self):
        t = self.mk(1, "k10temp", "45000")
        self.stop_after(12.0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            m.run(self.dev, {"path": t, "name": "k10temp"}, None,
                  "temp1_input", 1.0, False, None, "fan", False, None, 5.0,
                  {"path": None, "name": None}, None, "fan1_input")
        self.assertIn("no fan sensor found", err.getvalue())
        self.assertNotIn("None", err.getvalue())


LHM_TREE = {"Text": "Sensor", "Children": [{"Text": "PC", "Children": [
    {"Text": "Mainboard", "Children": [{"Text": "Fans", "Children": [
        {"Text": "System Fan #1", "Type": "Fan", "Value": "900 RPM"},
        {"Text": "CPU Fan", "Type": "Fan", "Value": "1.450 RPM"}]}]},
    {"Text": "NVIDIA GPU", "Children": [{"Text": "Fans", "Children": [
        {"Text": "GPU Fan", "Value": "1200 RPM"}]}]},
    {"Text": "CPU", "Children": [{"Text": "Temperatures", "Children": [
        {"Text": "Tctl", "Type": "Temperature",
         "Value": "45,0 °C"}]}]}]}]}


class TestWinFan(unittest.TestCase):
    def test_walk_and_pick(self):
        fans = list(w.walk_fan_sensors(LHM_TREE))
        self.assertEqual([f[1] for f in fans],
                         ["System Fan #1", "CPU Fan", "GPU Fan"])
        self.assertEqual(w.pick_fan_sensor(fans)[1], "CPU Fan")
        self.assertEqual(w.pick_fan_sensor(fans, "gpu")[1], "GPU Fan")
        self.assertEqual(w.pick_fan_sensor(fans[:1])[1], "System Fan #1")
        self.assertIsNone(w.pick_fan_sensor([], None))
        self.assertEqual(len(list(w.walk_temperature_sensors(LHM_TREE))), 1)


if __name__ == "__main__":
    unittest.main()
