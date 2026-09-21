#!/usr/bin/env python3
"""Manual, offline CHR persistence smoke test; never part of unittest discovery.

Requires two separately obtained, pristine vendor VDI seed images and QEMU.
Boots only copies in a newly created private scratch directory. No downloads.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time


ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[ -/]*[@-~]|[78])")
PROMPT = r"\[admin@[^\]\r\n]+\] > ?"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class VM:
    def __init__(self, disk, lab, label, qemu, timeout, report):
        require(disk.is_relative_to(lab), "Refusing to boot a disk outside this new lab")
        self.buffer = ""
        self.timeout = timeout
        self.capture = False
        self.transcript = ""
        self.record = {"label": label, "disk": str(disk), "clean_shutdown": False}
        self.command = [qemu, "-machine", "pc,accel=tcg", "-m", "256", "-smp", "1",
                        "-nic", "none", "-display", "none", "-monitor", "none",
                        "-serial", "stdio", "-no-reboot", "-drive",
                        f"file={disk},format=vdi,if=ide"]
        self.record["command"] = self.command
        self.log = lab / f"{label}-serial.txt"
        self.proc = None
        report["vms"].append(self.record)

    def __enter__(self):
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.record["pid"] = self.proc.pid
        return self

    def __exit__(self, *_):
        try:
            if self.proc.poll() is None:
                self.record["forced_cleanup"] = True
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=10)
            self.record["returncode"] = self.proc.returncode
            self.record["exited"] = self.proc.poll() is not None
        finally:
            self.proc.stdin.close()
            self.proc.stdout.close()
            self.log.write_text("Authentication exchange intentionally not recorded.\n"
                                + ANSI.sub("", self.transcript), encoding="utf-8")
            self.record["serial_log"] = str(self.log)

    def send(self, text):
        self.proc.stdin.write((text + "\r").encode())
        self.proc.stdin.flush()

    def expect(self, patterns, stage):
        deadline = time.monotonic() + self.timeout
        while True:
            cleaned = ANSI.sub("", self.buffer).replace("\r", "")
            for index, pattern in enumerate(patterns):
                match = re.search(pattern, cleaned, re.MULTILINE)
                if match:
                    self.buffer = cleaned[match.end():]
                    return index, match
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Serial timeout at {stage}; authentication is not logged")
            ready, _, _ = select.select([self.proc.stdout], [], [], min(1, remaining))
            if ready:
                data = os.read(self.proc.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError(f"QEMU serial closed at {stage}")
                text = data.decode("utf-8", errors="replace")
                self.buffer += text
                if self.capture:
                    self.transcript += text

    def login(self, password, fresh):
        self.expect([r"Login: ?"], "login")
        self.send("admin")
        self.expect([r"Password: ?"], "password")
        self.send("" if fresh else password)
        while True:
            index, _ = self.expect([r"software license\? \[Y/n\]: ?",
                                    r"new password> ?", PROMPT], "login completion")
            if index == 0:
                self.send("n")
            elif index == 1:
                require(fresh, "Unexpected password reset on persisted guest")
                self.send(password)
                self.expect([r"repeat new password> ?"], "repeat fresh password")
                self.send(password)
                self.record["fresh_password_set"] = True
                self.expect([PROMPT], "fresh password completion")
                break
            else:
                break
        if fresh:
            require(self.record.get("fresh_password_set"), "Fresh-password flow not exercised")
        self.buffer = ""
        self.capture = True
        self.record["login"] = "success"

    def command_done(self, command):
        self.send(command)
        self.expect([PROMPT], "command completion")

    def value(self, tag, expression):
        self.send(f':put ("{tag}=" . [{expression}])')
        _, match = self.expect([rf"^{re.escape(tag)}=([^\n]*)\n"], tag)
        value = match.group(1).strip()
        self.expect([PROMPT], f"{tag} prompt")
        return value

    def shutdown(self):
        self.send("/system shutdown")
        self.expect([r"\[y/N\]: ?"], "shutdown confirmation")
        self.send("y")
        # Drain serial while waiting; pipe backpressure must not block shutdown.
        deadline = time.monotonic() + self.timeout
        while self.proc.poll() is None and time.monotonic() < deadline:
            if select.select([self.proc.stdout], [], [], 0.2)[0]:
                data = os.read(self.proc.stdout.fileno(), 65536)
                self.transcript += data.decode("utf-8", errors="replace")
        require(self.proc.poll() is not None, "QEMU did not exit after guest shutdown")
        require(self.proc.returncode == 0, "QEMU failed during shutdown")
        self.record["clean_shutdown"] = True


def initialize(initializer, data_dir, seed, lab, report):
    command = [sys.executable, str(initializer), str(data_dir), str(seed)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    record = {"command": command, "returncode": result.returncode,
              "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    report["initializers"].append(record)
    require(result.returncode == 0, "Real bin/init-disk.py failed; see report")
    target = data_dir / "chr.vdi"
    require(result.stdout.strip() == str(target), "Initializer returned unexpected disk")
    require(target.is_relative_to(lab) and target.is_file() and not target.is_symlink(),
            "Initializer did not create a regular lab disk")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Required explicit manual opt-in")
    parser.add_argument("--old-seed", type=Path, required=True, help="Pristine 7.21.4 VDI (read only)")
    parser.add_argument("--new-seed", type=Path, required=True, help="Pristine 7.22.2 VDI (read only)")
    parser.add_argument("--scratch-parent", type=Path, required=True,
                        help="Existing scratch directory; a NEW private subdirectory is created")
    parser.add_argument("--fresh-new", action="store_true", help="Also initialize and boot fresh 7.22.2")
    parser.add_argument("--timeout", type=int, default=180, help="Per serial stage timeout in seconds")
    args = parser.parse_args()
    if not args.run:
        parser.error("Refusing to run without --run")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    seeds = [args.old_seed.resolve(), args.new_seed.resolve()]
    if any(not p.is_file() or p.stat().st_size == 0 for p in seeds):
        parser.error("Both seeds must be existing nonempty regular files")
    parent = args.scratch_parent.resolve()
    if not parent.is_dir() or "," in str(parent):
        parser.error("Scratch parent must exist and must not contain commas")
    qemu = shutil.which("qemu-system-x86_64")
    if not qemu:
        parser.error("qemu-system-x86_64 is required")
    initializer = Path(__file__).resolve().parents[1] / "bin" / "init-disk.py"
    lab = Path(tempfile.mkdtemp(prefix="chr-smoke-", dir=parent))
    report = {"status": "running", "lab": str(lab), "vms": [], "initializers": [],
              "isolation": "TCG, -nic none, no monitor, serial subprocess pipes; no host network changes",
              "limitations": ["No Docker/KVM or production integration tested",
                              "No package upgrade performed: seed selection is not a guest upgrade",
                              "Seed hashes are local provenance, not authenticated vendor checksums"],
              "qemu_version": subprocess.check_output([qemu, "--version"], text=True).splitlines()[0],
              "initializer_sha256": sha256(initializer),
              "script_sha256": sha256(Path(__file__).resolve())}
    report_path = lab / "report.json"
    print(f"LAB={lab}", flush=True)
    password = "ChrLab!" + secrets.token_urlsafe(24)
    marker = "chr-smoke-" + secrets.token_hex(6)
    report["identity_marker"] = marker
    old_handler = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    exit_code = 1
    try:
        report["seeds"] = [{"path": str(p), "sha256_before": sha256(p)} for p in seeds]
        require(report["seeds"][0]["sha256_before"] != report["seeds"][1]["sha256_before"],
                "Old and new seeds must contain different images")
        data_dir = lab / "legacy-data"
        data_dir.mkdir()
        legacy = data_dir / "chr-7.21.4.vdi"
        shutil.copyfile(seeds[0], legacy)
        require(sha256(legacy) == report["seeds"][0]["sha256_before"], "Seed copy differs")
        with VM(legacy, lab, "legacy-7.21.4", qemu, args.timeout, report) as vm:
            vm.login(password, fresh=True)
            version = vm.value("CHR_SMOKE_VERSION", "/system resource get version")
            require(version.split()[0] == "7.21.4", "Legacy guest version was not 7.21.4")
            vm.record["guest_version"] = version
            vm.command_done(f"/system identity set name={marker}")
            identity = vm.value("CHR_SMOKE_IDENTITY", "/system identity get name")
            require(identity == marker, "Guest identity marker was not set")
            vm.record["guest_identity"] = identity
            vm.shutdown()
        legacy_hash = sha256(legacy)
        report["legacy_sha256_after_shutdown"] = legacy_hash
        stable = initialize(initializer, data_dir, seeds[0], lab, report)
        report["stable_sha256_after_migration"] = sha256(stable)
        require(sha256(legacy) == legacy_hash == sha256(stable), "Migration did not preserve bytes")
        # Exercise stable-disk precedence with a genuinely different seed.
        stable_again = initialize(initializer, data_dir, seeds[1], lab, report)
        require(stable_again == stable and sha256(stable) == legacy_hash,
                "Changing seed changed existing stable disk")
        report["stable_unchanged_after_new_seed"] = True
        with VM(stable, lab, "stable-with-7.22.2-seed", qemu, args.timeout, report) as vm:
            vm.login(password, fresh=False)
            version = vm.value("CHR_SMOKE_VERSION", "/system resource get version")
            identity = vm.value("CHR_SMOKE_IDENTITY", "/system identity get name")
            vm.record.update(guest_version=version, guest_identity=identity)
            require(version.split()[0] == "7.21.4", "New seed changed persisted guest version")
            require(identity == marker, "Persisted identity was lost")
            vm.shutdown()
        report["legacy_sha256_after_stable_boot"] = sha256(legacy)
        require(sha256(legacy) == legacy_hash, "Legacy image changed during stable boot")
        if args.fresh_new:
            fresh = initialize(initializer, lab / "fresh-data", seeds[1], lab, report)
            require(sha256(fresh) == report["seeds"][1]["sha256_before"], "Fresh seed copy differs")
            with VM(fresh, lab, "fresh-7.22.2", qemu, args.timeout, report) as vm:
                vm.login(password, fresh=True)
                version = vm.value("CHR_SMOKE_VERSION", "/system resource get version")
                vm.record["guest_version"] = version
                require(version.split()[0] == "7.22.2", "Fresh guest version was not 7.22.2")
                vm.shutdown()
        require(all(v.get("exited") and v.get("clean_shutdown") for v in report["vms"]),
                "Not all spawned VMs shut down cleanly")
        report["status"] = "passed"
        exit_code = 0
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}".replace(password, "[REDACTED]")
    finally:
        signal.signal(signal.SIGTERM, old_handler)
        for seed in report.get("seeds", []):
            try:
                seed["sha256_after"] = sha256(Path(seed["path"]))
                if seed["sha256_after"] != seed["sha256_before"]:
                    report["status"] = "failed"
                    report["seed_integrity_error"] = "Input seed changed"
                    exit_code = 1
            except OSError:
                report["status"] = "failed"
                report["seed_integrity_error"] = "Input seed unavailable for final check"
                exit_code = 1
        report["all_spawned_qemu_exited"] = all(v.get("exited", False) for v in report["vms"])
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"RESULT={report['status']} REPORT={report_path}", flush=True)
    if report.get("error"):
        print(report["error"], file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
