"""
The one-line installers in install/, which users run straight from GitHub's
raw URLs. Nothing here runs them or reaches a server.

Run from the repository root:  python -m unittest discover -s tests
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install"
RAW = "https://raw.githubusercontent.com/japherwocky/pdatum/main/install/"
# What users copy: jobwolverine redirects each of these to RAW + the same name.
SHORT = "https://pdatum.pearachute.com/"


def read(name):
    # Bytes, not text mode: text mode would hide the line endings under test.
    return (INSTALL / name).read_bytes().decode("utf-8")


def code_lines(script, comment):
    return [line for line in script.splitlines() if not line.lstrip().startswith(comment)]


class Installers(unittest.TestCase):
    def test_shell_installer_has_unix_line_endings(self):
        # sh reads a \r as part of each command.
        self.assertNotIn("\r", read("install.sh"))

    def test_cmd_installer_has_windows_line_endings(self):
        text = read("install.cmd")
        self.assertIn("\r\n", text)
        self.assertNotIn("\n", text.replace("\r\n", ""))

    def test_cmd_installer_hands_over_to_the_powershell_one(self):
        # Straight to the raw file, not via the redirect: one less hop that
        # could be down.
        self.assertIn(f"irm '{RAW}install.ps1' | iex", read("install.cmd"))

    def test_powershell_installer_never_exits(self):
        # It runs under `irm | iex`, in the caller's own session: `exit` would
        # close their terminal window.
        for line in code_lines(read("install.ps1"), "#"):
            self.assertFalse(line.strip().startswith("exit"), line)

    def test_powershell_installer_leaves_a_clean_exit_code(self):
        # `native | Select-Object -First` stops the pipe early and leaves
        # $LASTEXITCODE at -1 in the caller's window after a good install.
        for line in code_lines(read("install.ps1"), "#"):
            self.assertNotIn("Select-Object -First", line)

    def test_powershell_installer_runs_on_windows_powershell_5(self):
        # 5.1 is what every Windows ships with, and it has no && or ??.
        for line in code_lines(read("install.ps1"), "#"):
            self.assertNotRegex(line, r"&&|\?\?", line)

    def test_each_installer_installs_the_package_pyproject_names(self):
        name = re.search(r'^name = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
        for script in ("install.sh", "install.ps1"):
            text = read(script)
            self.assertIn(f"uv tool install --quiet --upgrade {name}\n", text.replace("\r\n", "\n"))
            self.assertIn(f"pipx install --quiet --force {name}", text)

    def test_each_installer_documents_its_own_url(self):
        # The one-liner in each script's header is the one users copy.
        for script in ("install.sh", "install.ps1", "install.cmd"):
            self.assertIn(SHORT + script, read(script))

    def test_readme_one_liners_name_installers_that_exist(self):
        named = re.findall(re.escape(SHORT) + r"(install[\w.]+)", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(sorted(set(named)), ["install.cmd", "install.ps1", "install.sh"])
        for name in named:
            self.assertTrue((INSTALL / name).is_file(), name)

    @unittest.skipUnless(shutil.which("sh"), "no sh here")
    def test_shell_installer_parses(self):
        result = subprocess.run(["sh", "-n", str(INSTALL / "install.sh")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
