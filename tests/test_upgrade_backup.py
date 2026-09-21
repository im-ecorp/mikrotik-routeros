"""Run the documented Bash backup with stub Docker and real filesystem tools."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCKER_STUB = r'''import json
import os
from pathlib import Path
import sys

state_file = Path(os.environ["DOCKER_STATE"])
state = json.loads(state_file.read_text())
args = sys.argv[1:]
state["calls"].append(args)
output = ""
if args == ["compose", "ps", "-a", "-q", "routers"]:
    output = "fixture-container"
elif args == ["compose", "config"]:
    output = "services: {routers: {restart: unless-stopped}}"
elif args == ["compose", "stop", "routers"]:
    state["status"] = "exited"
elif args == ["update", "--restart=no", "fixture-container"]:
    state["policy"] = "no"
elif len(args) == 4 and args[0:2] == ["inspect", "-f"] and args[3] == "fixture-container":
    template = args[2]
    if template == "{{.State.Running}}":
        output = str(state["status"] == "running").lower()
    elif template == "{{.State.Status}}":
        output = state["status"]
    elif template.startswith("{{.HostConfig.RestartPolicy.Name}}"):
        output = state["policy"]
    elif template == "{{.Config.Image}} {{.Image}}":
        output = "routeros:fixture sha256:fixture"
    else:
        raise SystemExit("Unexpected inspect: " + template)
else:
    raise SystemExit("Unexpected Docker call: " + repr(args))
state_file.write_text(json.dumps(state))
print(output)
'''


class DocumentedBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="routeros-backup-test-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        for name in ("data", "shared"):
            (self.project / name).mkdir()
        (self.project / "data/chr.vdi").write_bytes(b"configured guest\x00\xff")
        (self.project / "shared/export.rsc").write_bytes(b"private export")
        (self.project / ".env").write_text("ROUTEROS_VERSION=fixture\n")
        bindir = self.root / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text("#!" + sys.executable + "\n" + DOCKER_STUB)
        docker.chmod(0o700)
        self.state_file = self.root / "docker.json"
        # The operator has halted the guest when the script checks its state.
        self.state_file.write_text(json.dumps({"status": "exited", "policy": "unless-stopped", "calls": []}))
        self.environment = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                                DOCKER_STATE=str(self.state_file))

    def run_backup(self):
        section = (ROOT / "docs/upgrades.md").read_text().split("## 1.", 1)[1].split("## 2.", 1)[0]
        block = re.search(r"```bash\n(.*?)\n```", section, re.DOTALL).group(1)
        return subprocess.run(["bash", "-c", block], cwd=self.project, env=self.environment,
                              input="halted\n", capture_output=True, text=True, timeout=10)

    def state(self):
        return json.loads(self.state_file.read_text())

    def backup(self):
        paths = list(self.root.glob("mikrotik-backup.*"))
        self.assertEqual(len(paths), 1)
        return paths[0]

    def test_restart_policy_saved_and_disabled_before_exit_check_and_stop(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.state()["calls"]
        disable = ["update", "--restart=no", "fixture-container"]
        self.assertIn(disable, calls, "Backup never disables the container restart policy")
        check = ["inspect", "-f", "{{.State.Status}}", "fixture-container"]
        stop = ["compose", "stop", "routers"]
        self.assertLess(calls.index(disable), calls.index(check))
        self.assertLess(calls.index(check), calls.index(stop))
        self.assertEqual(self.state()["policy"], "no", "Do not auto-restore during backup")
        self.assertEqual((self.backup() / "restart-policy.txt").read_text().strip(), "unless-stopped")

    def test_directory_symlinks_are_rejected(self):
        for name in ("data", "shared"):
            with self.subTest(source=name):
                source = self.project / name
                actual = self.project / (name + "-actual")
                source.rename(actual)
                source.symlink_to(actual, target_is_directory=True)
                try:
                    result = self.run_backup()
                    self.assertNotEqual(result.returncode, 0, "Directory symlink accepted as independent backup")
                    self.assertNotIn("Verified offline backup:", result.stdout)
                    self.assertEqual(self.state()["policy"], "no")
                finally:
                    source.unlink()
                    actual.rename(source)

    def test_nested_symlinks_are_rejected(self):
        for name in ("data", "shared"):
            for target in (self.project / "data/chr.vdi", self.project / "shared", self.root / "missing"):
                with self.subTest(source=name, target=target):
                    link = self.project / name / "alias"
                    link.symlink_to(target)
                    try:
                        result = self.run_backup()
                        self.assertNotEqual(result.returncode, 0, "Nested symlink accepted")
                        self.assertNotIn("Verified offline backup:", result.stdout)
                        self.assertEqual(self.state()["policy"], "no")
                    finally:
                        link.unlink()

    def test_successful_backup_has_independent_real_files(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = self.backup()
        self.assertIn("Verified offline backup:", result.stdout)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        for name in ("data/chr.vdi", "shared/export.rsc"):
            source, copied = self.project / name, backup / name
            original = source.read_bytes()
            self.assertFalse(copied.is_symlink())
            self.assertEqual(copied.read_bytes(), original)
            self.assertFalse(os.path.samefile(source, copied))
            source.write_bytes(b"later guest writes")
            self.assertEqual(copied.read_bytes(), original)

    def test_non_exited_container_is_not_stopped_or_copied(self):
        for status in ("running", "restarting", "paused"):
            with self.subTest(status=status):
                state = self.state()
                state.update(status=status, calls=[])
                self.state_file.write_text(json.dumps(state))
                result = self.run_backup()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(["compose", "stop", "routers"], self.state()["calls"])
                self.assertFalse(any(self.root.glob("mikrotik-backup.*/data")))
                self.assertEqual(self.state()["policy"], "no")


if __name__ == "__main__":
    unittest.main()
