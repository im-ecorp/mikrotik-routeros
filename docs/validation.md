# Isolated CHR disk validation

`tests/chr-smoke.py` is a **manual opt-in** integration test, not a unittest-discovered test. It uses the Python standard library and `qemu-system-x86_64`; no pexpect, Docker, KVM, root privileges, network namespace, or guest networking is required.

## Run

Obtain pristine MikroTik vendor VDI images for **7.21.4** and **7.22.2** separately. The script does not download anything. Never supply a production disk as a seed, even though seeds are only read. Allow scratch space for copies of the images.

```sh
python3 tests/chr-smoke.py --run \
  --old-seed /absolute/path/chr-7.21.4.vdi \
  --new-seed /absolute/path/chr-7.22.2.vdi \
  --scratch-parent /absolute/path/existing-scratch \
  --fresh-new --timeout 180
```

The script always creates a new private `chr-smoke-*` directory. There is no option to boot an existing data directory. Every QEMU disk is a disposable copy beneath that new directory. Both supplied seed hashes are checked again at the end. QEMU uses TCG, `-nic none`, `-display none`, `-monitor none`, and `-serial stdio` connected to subprocess pipes. It creates no TCP listener and changes no host networking.

The test:

1. Copies the old seed to `legacy-data/chr-7.21.4.vdi`, boots it, logs in as blank-password admin, and handles the first-login password change using a disposable random password held only in process memory.
2. Reads the guest version, sets and reads a harmless unique system identity, and issues `/system shutdown` with confirmation. It waits for QEMU to exit successfully.
3. Invokes the repository's actual `bin/init-disk.py` to migrate the legacy image to `chr.vdi`. The legacy image and newly published stable disk must have identical SHA-256 hashes.
4. Invokes that same initializer with the different 7.22.2 seed. The existing stable disk must remain byte-identical before boot.
5. Boots the stable disk and authenticates with the persisted password. Guest version must still be 7.21.4, and the identity must match. After clean shutdown, the retained legacy file must still have its original post-shutdown hash.
6. With `--fresh-new`, initializes a separate empty data directory from the new seed, boots it, verifies 7.22.2, and shuts it down cleanly.

`report.json` contains actual commands, hashes, guest values, exit codes, and cleanup status. Separate serial logs begin **after authentication**; neither sent passwords nor the authentication exchange are recorded. Evidence and disposable disks remain in scratch for inspection; passwords are not saved for later access. VM context cleanup terminates, then kills if necessary, and waits for only its own child on failure or interruption. Forced cleanup is recorded and is not a successful clean-shutdown result. SIGKILL or host failure cannot be handled by Python cleanup.

## Observed real run

QEMU: `QEMU emulator version 10.0.13 (Debian 1:10.0.13+ds-0+deb13u1)`.

Executed from `/root/mygit/mikrotik-routeros`:

```sh
python3 tests/chr-smoke.py --run \
  --old-seed /home/hermeswebui/.hermes/cache/scratch/routeros-chr-lab-cj_ipae2/chr-7.21.4.vdi \
  --new-seed /home/hermeswebui/.hermes/cache/scratch/routeros-chr-lab-cj_ipae2/chr-7.22.2.vdi \
  --scratch-parent /home/hermeswebui/.hermes/cache/scratch/routeros-chr-lab-cj_ipae2 \
  --fresh-new --timeout 90
```

Result: **passed**, command exit code **0**.

Evidence directory:
`/home/hermeswebui/.hermes/cache/scratch/routeros-chr-lab-cj_ipae2/chr-smoke-7yobuntp/`

| Boot | Observed guest version | Identity | Shutdown / QEMU exit |
| --- | --- | --- | --- |
| Legacy versioned disk | `7.21.4 (long-term)` | `chr-smoke-79e435217e40` | Clean / 0 |
| Stable disk after selecting 7.22.2 seed | `7.21.4 (long-term)` | `chr-smoke-79e435217e40` | Clean / 0 |
| Fresh disk initialized from 7.22.2 | `7.22.2 (stable)` | Not modified | Clean / 0 |

Legacy post-shutdown hash, stable post-migration hash, and legacy hash after stable boot were all:
`a2a152fe55541c6cf7575bdb9283cfcb513a52753a8e3fd793c6d1803b57a01e`.

Both input VDI hashes were unchanged:

- 7.21.4: `29f30c0cc78aa9a626eaf0ff6c5ee75cbc19f2120fdf0f880cb3dd5d7ce3c96c`
- 7.22.2: `6e604eba656cf25921e23e720eca3d7fef8ad1cd0518407b8bf2ebeb8e867d56`

Tested script SHA-256: `d85847163eae6e1ad6575958220ce308214fcdd6a2bff75e51ec5682b1fbf399`.
Tested initializer SHA-256: `605f0c5768d787fcac8b20624bfc5aea77864c7967762ebaf03426a38ed466dc`.
These hashes record local provenance, not authenticated vendor checksums.

All three QEMU children were reaped. A separate `/proc` check found no remaining QEMU process. Running without `--run` was also verified to exit with code 2 without creating a lab directory.

Two earlier development attempts timed out during authentication because the console redraw left a stale password prompt in the parser buffer. They were recorded as failures, and their QEMU children were terminated and reaped. The parser now waits for the shell prompt immediately after password confirmation. Both the initial corrected run (`chr-smoke-qpczz1nj`) and the final script run above passed. Failed reports remain in `chr-smoke-7h5b455j` and `chr-smoke-g7o694ax` under the same scratch parent; initial boot/login probes are also disposable scratch artifacts.

## Boundaries

This validates real guest bootability and persisted-disk selection, not an in-guest RouterOS package upgrade. Selecting a different image seed is intentionally **not** an upgrade of an existing disk. Docker entrypoint behavior, Compose networking, KVM, license activation, production workloads, and package downgrade/upgrade paths are outside this test. No production disk or host network configuration was changed. Scratch evidence is local and subject to scratch retention policy; preserve selected sanitized reports elsewhere if long-term evidence is needed.
