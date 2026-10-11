import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chione_web as w

FANS = [{"hwmon": "it8688", "file": "fan1_input", "label": "", "rpm": 1451,
         "gpu": False}]


def patched():
    return mock.patch.multiple(
        w.g, read_conf_args=lambda *a: {"mode": "auto"},
        scan_fans=lambda: FANS, read_live=lambda s: (45.0, 1200),
        service_installed=lambda: True, service_active=lambda: True,
        service_hint=lambda: "./install.sh service")


class TestLogic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")

    def test_state_payload(self):
        with patched():
            data = w.api_state(self.path)
        self.assertEqual(data["state"]["mode"], "auto")
        self.assertEqual(data["fans"], FANS)
        self.assertEqual(data["live"], {"cpu": 45.0, "rpm": 1200})
        self.assertEqual(data["service"], {"installed": True, "active": True,
                                           "hint": "./install.sh service"})
        self.assertIsNone(data["settings_error"])

    def test_bad_file_reported(self):
        with open(self.path, "w") as f:
            f.write("{nope")
        with patched():
            data = w.api_state(self.path)
        self.assertIsNotNone(data["settings_error"])
        self.assertEqual(data["state"]["mode"], "auto")

    def test_update_writes_only_overrides(self):
        with patched():
            w.api_update(self.path, {"fahrenheit": True})
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"fahrenheit": True})

    def test_update_validation_error(self):
        with patched():
            with self.assertRaises(ValueError):
                w.api_update(self.path, {"mode": "bogus"})
            with self.assertRaises(ValueError):
                w.api_update(self.path, {"evil": 1})
        self.assertFalse(os.path.exists(self.path))

    def test_host_and_token(self):
        self.assertTrue(w.host_allowed("127.0.0.1:8765", 8765))
        self.assertTrue(w.host_allowed("localhost:8765", 8765))
        self.assertFalse(w.host_allowed("evil.com:8765", 8765))
        self.assertFalse(w.host_allowed("127.0.0.1", 8765))
        self.assertTrue(w.token_ok("abc", "abc"))
        self.assertFalse(w.token_ok("abc", ""))
        self.assertFalse(w.token_ok("abc", "abd"))

    def test_service_action(self):
        with mock.patch.object(w.g, "systemctl",
                               return_value=(True, "")) as sc, \
                mock.patch.object(w.g, "service_active", return_value=True):
            self.assertEqual(w.api_service("start"), {"active": True})
            sc.assert_called_with("enable", "--now", w.g.SERVICE)
            w.api_service("stop")
            sc.assert_called_with("disable", "--now", w.g.SERVICE)
        with self.assertRaises(ValueError):
            w.api_service("restart")


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")
        self.srv = w.make_server(self.path, 0, "tok")
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        p = patched()
        p.start()
        self.addCleanup(p.stop)

    def req(self, path, body=None, token="tok", host=None, ctype="application/json"):
        headers = {}
        if token:
            headers["X-Chione-Token"] = token
        if host:
            headers["Host"] = host
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = ctype
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   data=data, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_round_trip(self):
        code, page = self.req("/?t=tok", token=None)
        self.assertEqual(code, 200)
        self.assertIn('const TOKEN="tok"', page)
        self.assertNotIn("—", page)
        code, body = self.req("/api/settings", {"cycle": 7})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["state"]["cycle"], 7)
        code, body = self.req("/api/state")
        self.assertEqual(json.loads(body)["state"]["cycle"], 7)

    def test_token_required(self):
        self.assertEqual(self.req("/", token=None)[0], 403)
        self.assertEqual(self.req("/?t=bad", token=None)[0], 403)
        self.assertEqual(self.req("/api/state", token=None)[0], 403)
        self.assertEqual(self.req("/api/log", token="x")[0], 403)
        self.assertEqual(self.req("/api/settings", {"cycle": 7}, token=None)[0], 403)
        self.assertEqual(self.req("/api/settings", {"cycle": 7}, token="x")[0], 403)
        self.assertFalse(os.path.exists(self.path))

    def test_cookie_allows_reload(self):
        url = f"http://127.0.0.1:{self.port}/"
        r = urllib.request.Request(url + "?t=tok")
        with urllib.request.urlopen(r, timeout=5) as resp:
            cookie = resp.headers["Set-Cookie"]
        self.assertIn(f"chione_{self.port}=tok", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        r = urllib.request.Request(url, headers={
            "Cookie": f"chione_{self.port}=tok"})
        with urllib.request.urlopen(r, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        r = urllib.request.Request(url, headers={
            "Cookie": f"chione_{self.port}=bad"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r, timeout=5)
        self.assertEqual(cm.exception.code, 403)
        # the cookie alone never opens the API
        self.assertEqual(self.req("/api/state", token=None)[0], 403)

    def test_launcher_file(self):
        path = w.write_launcher("http://127.0.0.1:1/?t=abc")
        self.addCleanup(w.remove_quiet, path)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as f:
            self.assertIn("location.replace", f.read())

    def test_bad_host(self):
        self.assertEqual(self.req("/api/state", host="evil.com")[0], 403)
        self.assertEqual(self.req("/api/settings", {"cycle": 7},
                                  host="evil.com")[0], 403)

    def test_validation_400_and_content_type(self):
        code, body = self.req("/api/settings", {"mode": "bogus"})
        self.assertEqual(code, 400)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.req("/api/settings", {"cycle": 7},
                                  ctype="text/plain")[0], 415)

    def test_body_cap(self):
        code, _ = self.req("/api/settings", {"x": "a" * (w.MAX_BODY + 1)})
        self.assertEqual(code, 413)


if __name__ == "__main__":
    unittest.main()


class TestSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")

    def test_invalid_file_not_overwritten(self):
        with open(self.path, "w") as f:
            f.write("{nope")
        with patched(), self.assertRaises(FileExistsError):
            w.api_update(self.path, {"cycle": 5})
        with open(self.path) as f:
            self.assertEqual(f.read(), "{nope")

    def test_invalid_file_http_409(self):
        with open(self.path, "w") as f:
            f.write("{nope")
        srv = w.make_server(self.path, 0, "tok")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        r = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/api/settings",
            data=b'{"cycle": 5}', headers={
                "Content-Type": "application/json", "X-Chione-Token": "tok"})
        with patched(), self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r, timeout=5)
        self.assertEqual(cm.exception.code, 409)

    def test_concurrent_updates_keep_all(self):
        keys = [{"cycle": 7.0}, {"fahrenheit": True}, {"mode": "fan"},
                {"alarm": 80.0}] * 5
        errors = []

        def run(ch):
            try:
                w.api_update(self.path, ch)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        with patched():
            ts = [threading.Thread(target=run, args=(c,)) for c in keys]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(errors, [])
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"cycle": 7.0, "fahrenheit": True,
                                            "mode": "fan", "alarm": 80.0})
