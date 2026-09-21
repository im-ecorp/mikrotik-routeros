"""Filesystem regression tests; no Docker or RouterOS installation required."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "init-disk.py"


class DiskInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="routeros-disk-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.seed = self.root / "chr-7.22.2.vdi"
        # Deliberately synthetic bytes: these tests do not validate VDI format.
        self.seed.write_bytes(b"synthetic factory disk\x00\xff")

    def run_init(self, seed=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.data), str(seed or self.seed)],
            capture_output=True, text=True, timeout=10,
        )

    def test_existing_disk_survives_seed_change_or_missing_seed(self):
        disk = self.data / "chr.vdi"
        disk.write_bytes(b"configured guest")
        inode = disk.stat().st_ino
        for seed in (self.seed, self.root / "missing.vdi"):
            with self.subTest(seed=seed):
                result = self.run_init(seed)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(disk.read_bytes(), b"configured guest")
                self.assertEqual(disk.stat().st_ino, inode)

    def test_single_legacy_disk_is_copied_without_modifying_original(self):
        legacy = self.data / "chr-7.21.4.vdi"
        legacy.write_bytes(b"legacy configuration")
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        disk = self.data / "chr.vdi"
        self.assertEqual(disk.read_bytes(), b"legacy configuration")
        self.assertNotEqual(disk.stat().st_ino, legacy.stat().st_ino)
        disk.write_bytes(b"new guest writes")
        self.assertEqual(legacy.read_bytes(), b"legacy configuration")

    def test_multiple_legacy_disks_require_manual_selection(self):
        for name in ("chr-7.21.4.vdi", self.seed.name):
            (self.data / name).write_bytes(name.encode())
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        result = self.run_init()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Multiple legacy disks", result.stderr)
        self.assertIn("chr.vdi", result.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_invalid_disk_paths_fail_closed(self):
        for name in ("chr.vdi", "chr-7.21.4.vdi"):
            for kind in ("empty", "directory", "symlink", "dangling"):
                with self.subTest(name=name, kind=kind):
                    self.data = self.root / (name + "-" + kind)
                    self.data.mkdir()
                    path = self.data / name
                    if kind == "empty":
                        path.touch()
                    elif kind == "directory":
                        path.mkdir()
                    else:
                        path.symlink_to(self.seed if kind == "symlink" else self.root / "absent")
                    result = self.run_init()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("non-empty regular file", result.stderr)
                    self.assertEqual(list(self.data.iterdir()), [path])

    def test_entrypoint_uses_stable_disk_across_image_versions(self):
        # Exercise the actual entrypoint initialization, before any network setup.
        prefix = (ROOT / "bin" / "entrypoint.sh").read_text().split("QEMU_BRIDGE=", 1)[0]
        prefix = prefix.replace("/routeros", str(self.root))
        (self.root / "bin").mkdir()
        (self.root / "bin" / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
        environment = dict(os.environ, ROUTEROS_IMAGE=self.seed.name)
        command = prefix + '\nprintf "%s\\n" "$IMAGE_FILE"\n'
        for version in (self.seed.name, "chr-unavailable-version.vdi"):
            environment["ROUTEROS_IMAGE"] = version
            result = subprocess.run(["bash", "-c", command], env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(self.data / "chr.vdi"))
            disk = self.data / "chr.vdi"
            if version == self.seed.name:
                disk.write_bytes(b"configuration after initial boot")
            self.assertEqual(disk.read_bytes(), b"configuration after initial boot")

    def test_existing_stable_disk_wins_over_multiple_legacy_disks(self):
        (self.data / "chr.vdi").write_bytes(b"active configuration")
        for name in ("chr-old.vdi", "chr-new.vdi"):
            (self.data / name).write_bytes(b"historical configuration")
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.data / "chr.vdi").read_bytes(), b"active configuration")

    def test_missing_seed_leaves_no_disk(self):
        result = self.run_init(self.root / "missing.vdi")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_failed_copy_never_publishes_partial_disk(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location("init_disk", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def interrupted_copy(source, destination):
            destination.write(b"partial")
            raise OSError("simulated disk full")

        with patch.object(module.shutil, "copyfileobj", side_effect=interrupted_copy):
            with self.assertRaisesRegex(OSError, "simulated disk full"):
                module.initialize_disk(self.data, self.seed)
        self.assertEqual(list(self.data.iterdir()), [])
        self.assertEqual(self.seed.read_bytes(), b"synthetic factory disk\x00\xff")
        self.assertEqual(self.run_init().returncode, 0)

    def test_publication_race_never_overwrites_existing_disk(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location("init_disk", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_link = os.link

        def competing_publisher(source, target):
            target.write_bytes(b"competing disk")
            real_link(source, target)

        with patch.object(module.os, "link", side_effect=competing_publisher):
            with self.assertRaises(FileExistsError):
                module.initialize_disk(self.data, self.seed)
        self.assertEqual((self.data / "chr.vdi").read_bytes(), b"competing disk")
        self.assertEqual(list(self.data.iterdir()), [self.data / "chr.vdi"])

    def test_first_boot_creates_stable_disk(self):
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.data / "chr.vdi"))
        self.assertEqual((self.data / "chr.vdi").read_bytes(), self.seed.read_bytes())
        self.assertFalse((self.data / self.seed.name).exists())
        self.assertEqual((self.data / "chr.vdi").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
