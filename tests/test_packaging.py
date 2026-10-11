import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chione_core


@unittest.skipUnless(os.name == "posix" and shutil.which("dpkg-deb"),
                     "needs POSIX and dpkg-deb")
class DebBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        env = dict(os.environ, CHIONE_DIST=cls.tmp)
        subprocess.run(["bash", os.path.join(ROOT, "packaging", "build-deb.sh")],
                       check=True, env=env, capture_output=True)
        cls.deb = os.path.join(
            cls.tmp, f"chione-display_{chione_core.__version__}_all.deb")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def dpkg(self, *args):
        return subprocess.run(["dpkg-deb", *args, self.deb], check=True,
                              capture_output=True, text=True).stdout

    def test_control_fields(self):
        info = self.dpkg("-f")
        self.assertIn("Package: chione-display\n", info)
        self.assertIn(f"Version: {chione_core.__version__}\n", info)
        self.assertIn("Architecture: all\n", info)
        self.assertIn("Depends: python3", info)

    def test_paths_and_modes(self):
        rows = {}
        for line in self.dpkg("-c").splitlines():
            parts = line.split()
            rows[parts[5].lstrip(".")] = parts[0]
        for path in (
            "/usr/share/chione-display/chione_core.py",
            "/usr/share/chione-display/chione_temp_display.py",
            "/usr/share/chione-display/chione_gui.py",
            "/usr/share/chione-display/chione_web.py",
            "/usr/lib/udev/rules.d/70-chione.rules",
            "/usr/lib/systemd/user/chione-temp.service",
            "/usr/share/applications/io.github.jonanthann.ChioneDisplay.desktop",
            "/usr/share/doc/chione-display/copyright",
        ):
            self.assertIn(path, rows)
        self.assertEqual(rows["/usr/bin/chione-display"], "-rwxr-xr-x")
        self.assertEqual(rows["/usr/bin/chione-display-gui"], "-rwxr-xr-x")
        self.assertEqual(rows["/usr/bin/chione-web"], "-rwxr-xr-x")
        self.assertFalse([p for p in rows if "tests" in p or "windows" in p])


if __name__ == "__main__":
    unittest.main()
