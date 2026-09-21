# Persistent disks, upgrades, and recovery

## Container version is not guest version

`ROUTEROS_VERSION` in `.env` selects the **container image tag**. The image contains
QEMU, startup scripts, and a factory RouterOS seed. That seed is used only when
there is no existing disk. Once initialized, `/routeros/data/chr.vdi` contains the
installed RouterOS version, configuration, and guest state.

Changing the container tag does **not** upgrade or downgrade an existing RouterOS
installation. Use RouterOS's own package-upgrade procedure for that.

These instructions apply to images built with `bin/init-disk.py`. Older published
images still use versioned disk names. Updating this checkout alone does not
update the running image. Use a release known to contain this fix, or build and
tag an image from this checkout before following the container-update procedure.

## Disk selection on startup

| Data directory contents | Behavior |
| --- | --- |
| `chr.vdi` exists | Reuse it, regardless of container version or legacy files. |
| No `chr.vdi`, exactly one `chr-*.vdi` | Copy that disk to `chr.vdi`; retain the original unchanged. |
| No `chr.vdi`, multiple `chr-*.vdi` files | Stop with an error. Never choose by version, timestamp, or filename order. |
| Neither stable nor legacy disk exists | Copy the container's factory seed to `chr.vdi`. |
| Selected path is empty, a directory, or a symlink | Stop with an error; do not replace it with a factory disk. |

A migration copy needs free space for another full disk. It is a separate file,
not a hard link to the legacy disk: subsequent guest writes do not change the
preserved original. Preserved legacy disks are **point-in-time copies**, not
continuously updated backups.

New disks are private (`0600`). A temporary file on the data filesystem is copied
and flushed before it is published as `chr.vdi` using a no-overwrite hard link.
The filesystem must support hard links. Copy errors leave no active partial disk;
a forcibly killed initializer may leave an unused `.chr-init-*` file. Never use
that file as a recovery disk. Existing disks are checked for basic file validity,
not VDI integrity. Only one container may use a data directory, and all guest
writers must be stopped during migration, backup, or restore.

## 1. Make an offline backup before any change

Plan downtime and keep console/out-of-band access. These host commands assume
Linux, Bash, and execution from the Compose project directory.

1. Record `/system resource print` inside RouterOS, the current image reference,
   and the container configuration. Export the RouterOS configuration and save an
   encrypted RouterOS backup using your established procedure. Download both to
   separate storage; do not rely solely on files inside the virtual disk.
2. Run the block below **before** asking RouterOS to shut down. It records the
   current container's restart policy (including an `on-failure` retry limit),
   then disables it. At the prompt, use a separate RouterOS console to issue
   `/system shutdown` and confirm shutdown. Only then type `halted` on the host.
   The block requires the container state to be `exited` before Compose stop and
   copying. If it has not exited yet, wait and check; do not force-stop the guest.
   The current entrypoint's SIGTERM handler alone does **not** guarantee
   guest-clean shutdown.
3. The block creates a new private backup directory without overwriting a backup.
   It deliberately leaves the restart policy disabled, including on failure.
   `data` and optional `shared` must be real directories with no nested symlinks;
   aliases are rejected because `cp -a` would preserve them instead of making an
   independent backup. Do not change these trees between validation and copying:

```bash
set -eu
container_id=$(docker compose ps -a -q routers)
test -n "$container_id"
backup=$(mktemp -d "../mikrotik-backup.XXXXXXXX")
chmod 700 "$backup"
printf 'Backup/recovery records: %s\n' "$backup"
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}{{if .HostConfig.RestartPolicy.MaximumRetryCount}}:{{.HostConfig.RestartPolicy.MaximumRetryCount}}{{end}}' "$container_id" > "$backup/restart-policy.txt"
test -s "$backup/restart-policy.txt"
docker update --restart=no "$container_id"
test "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$container_id")" = no
printf 'Now issue /system shutdown in RouterOS; after shutdown type halted: '
read -r confirmation
test "$confirmation" = halted
test "$(docker inspect -f '{{.State.Status}}' "$container_id")" = exited
docker compose stop routers
test "$(docker inspect -f '{{.State.Status}}' "$container_id")" = exited
docker inspect -f '{{.Config.Image}} {{.Image}}' "$container_id" > "$backup/image.txt"
docker compose config > "$backup/compose.resolved.yml"
if test -f .env; then cp -p .env "$backup/env.saved"; fi
# Reject aliases before copying; all source trees must remain unchanged.
for directory in data shared; do
    if test "$directory" = shared && ! test -e "$directory" && ! test -L "$directory"; then
        continue
    fi
    if test -L "$directory" || ! test -d "$directory"; then
        printf 'Refusing non-directory or symlink source: %s\n' "$directory" >&2
        exit 1
    fi
    links=$(find -P "$directory" -type l -print)
    if test -n "$links"; then
        printf 'Refusing nested symlinks in: %s\n' "$directory" >&2
        exit 1
    fi
done
cp -a data "$backup/data"
if test -d shared; then cp -a shared "$backup/shared"; fi
diff -qr data "$backup/data"
if test -d shared; then diff -qr shared "$backup/shared"; fi
printf 'Verified offline backup: %s\n' "$backup"
```

Keep the resolved Compose file and backup private: they can contain sensitive
configuration. Copy the backup off-host. Do not restart until the comparison
succeeds. Retain the old container image for rollback.

Keep `restart-policy.txt` from the **first** attempt. After any failure, leave
restart disabled while investigating; do not automatically restore it in an exit
trap or rerun the whole block and replace the original policy record with `no`.
An incomplete backup is not a recovery copy. Keep all other writers and external
orchestrators stopped throughout backup and restore.

Once backup or rollback/restore is complete and verified, resume the **same**
container explicitly with `docker compose start routers`, then restore its saved
policy with `docker update --restart="$(cat "$backup/restart-policy.txt")" "$container_id"`.
Use the recorded backup path and container ID if opening a new shell. If abandoning
an unsuccessful backup, restore the policy only after deciding it is safe to
resume the untouched original disk and explicitly starting that container.
For an update or rollback that **recreates** the container, verify the Compose
`restart:` setting matches the recorded policy before `docker compose up`; the
new container takes its policy from Compose, not from the old container's
`docker update`. Do not run `up`, `start`, or restore a restart policy while
backup/restore work is still in progress.

## 2. Resolve multiple legacy disks, if necessary

With the container stopped and the backup verified, identify the disk containing
your intended configuration. Do not assume the newest filename or modification
time is correct. If unsure, inspect copies in an isolated lab, not on the live
network.

Set `source` below to the exact chosen file. This host-side operation stages a
copy, verifies its bytes, and publishes it without replacing `data/chr.vdi`.
Do not execute it against a running router. Python 3 is required on the host.

```bash
source=./data/chr-7.21.4.vdi  # Replace with the disk you actually selected.
python3 - "$source" <<'PY'
import filecmp
import os
from pathlib import Path
import shutil
import sys
import tempfile

source = Path(sys.argv[1])
target = Path("data/chr.vdi")
if target.exists() or target.is_symlink():
    raise SystemExit("chr.vdi already exists; refusing to replace it")
if source.is_symlink() or not source.is_file() or source.stat().st_size == 0:
    raise SystemExit("Source must be a non-empty regular file")
fd, staging = tempfile.mkstemp(prefix=".chr-manual-", dir="data")
try:
    with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
        shutil.copyfileobj(input_file, output)
        output.flush()
        os.fsync(output.fileno())
    if not filecmp.cmp(source, staging, shallow=False):
        raise SystemExit("Copy verification failed; original retained")
    os.link(staging, target)
finally:
    os.unlink(staging)
print("Selected", source, "as", target, "without changing the original")
PY
```

## 3. Update the container independently

After the offline backup and any manual disk selection:

1. Set `.env` to a pinned container tag known to include this migration code.
2. Pull and recreate the service:

   ```bash
   docker compose pull routers
   docker compose up -d --no-deps --force-recreate routers
   docker compose logs --tail=100 routers
   ```

   For a locally built image, omit `pull` and use the corresponding local tag.
3. Confirm access via Winbox or SSH. Check identity, interfaces, routes, firewall,
   VPNs, and `/system resource print`. The guest version should be unchanged.
4. Verify `data/chr.vdi` exists. Keep legacy files and the offline backup until
   recovery has been tested. Do not treat container `healthy` status as proof of
   configuration integrity.

## 4. Upgrade RouterOS inside the guest

Take another verified offline backup before changing the guest. Start it again,
review MikroTik's release notes and CHR upgrade/licensing requirements, and use
RouterOS **System > Packages > Check For Updates** or the official manual-package
procedure for your desired release/channel. An update can reboot the router and
interrupt traffic. Do not replace `chr.vdi` with a fresh vendor image to upgrade.

After reboot, verify the installed version and routing/VPN behavior. Container
tags now describe the bundled factory seed, not necessarily the running guest.

References: [RouterOS upgrades](https://help.mikrotik.com/docs/spaces/ROS/pages/328142/Upgrading+and+installation),
[CHR](https://help.mikrotik.com/docs/spaces/ROS/pages/18350234/Cloud+Hosted+Router+CHR).

## Rollback and recovery

- **Container-only rollback:** choose an earlier image that also supports
  `chr.vdi`; keep the disk unchanged. A rollback to legacy code will ignore
  `chr.vdi` and can boot an outdated legacy disk or a fresh seed. Do not use the
  ordinary tag-switch procedure for that case.
- **Guest rollback:** changing the image tag does not undo a RouterOS upgrade.
  Stop the service and restore the verified pre-upgrade disk, or follow the
  vendor's supported downgrade procedure. A disk restore discards guest changes
  since that backup.
- **Restore safely:** with all writers stopped, archive the entire current `data`
  directory under a unique name rather than deleting it. Copy the verified backup
  into a new `data` directory and compare it before starting. Use the matching
  container image. If recovering into this implementation from multiple legacy
  files, repeat the explicit selection step. If returning to legacy code, use
  the original versioned disk names and matching image from the backup.
- **Intentional fresh installation:** use a separate empty data directory and a
  separate isolated deployment. Preserve the old directory. Never delete disks
  or run wildcard removal commands as an upgrade step.

## Local regression tests

```bash
python3 -m unittest discover -s tests -v
bash -n bin/entrypoint.sh
```

Tests exercise synthetic disk bytes on a real filesystem, including the
entrypoint's initialization section. They do not boot QEMU or prove VDI integrity,
RouterOS upgrade success, or power-loss recovery. Validate those in an isolated
CHR lab before deploying to a production router.
