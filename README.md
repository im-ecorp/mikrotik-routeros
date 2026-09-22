# MikroTik RouterOS on Docker

<div align="center">

[![Docker Image](https://img.shields.io/docker/v/hossein3piol/mikrotik-routeros?label=Docker%20Hub&logo=docker)](https://hub.docker.com/r/hossein3piol/mikrotik-routeros)
[![Docker Pulls](https://img.shields.io/docker/pulls/hossein3piol/mikrotik-routeros)](https://hub.docker.com/r/hossein3piol/mikrotik-routeros)
[![GitHub Stars](https://img.shields.io/github/stars/im-ecorp/mikrotik-routeros?style=social)](https://github.com/im-ecorp/mikrotik-routeros)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**MikroTik RouterOS CHR inside Docker, with persistent disks, guest-aware health checks, and bounded clean shutdown.**

</div>

---

## Overview

This project packages MikroTik RouterOS (Cloud Hosted Router) inside a Docker container using QEMU for full x86_64 virtualization. It is designed for infrastructure engineers who need a reproducible, version-controlled MikroTik environment on any Linux server — with complete persistence, host file sharing, and safe port routing.

Because QEMU handles the x86_64 emulation layer, the Docker image itself is built for **multiple CPU architectures**. Whether your host is an AMD64 server, an ARM64 Raspberry Pi, or any other supported platform, you can pull and run the same image without any modifications.

---

## Features

| Feature | Description |
|---|---|
| **Multi-Architecture** | New CHR image releases target `amd64` and `arm64`; qualification differs by platform |
| **Persistent Storage** | Router configuration survives container rebuilds via a host-mounted virtual drive |
| **Host ↔ Guest File Sharing** | A local directory is exposed inside MikroTik's File Manager as a virtual FAT drive |
| **Safe SSH Access** | MikroTik SSH is remapped to port `2222` — your host SSH on port `22` is untouched |
| **Dynamic Port Range** | Ports `9000–9100` are pre-allocated for custom services — no Compose restarts needed |
| **Version Pinning** | Pin the container and factory seed version; upgrade existing guests inside RouterOS |
| **KVM Acceleration** | Hardware virtualization is enabled automatically when `/dev/kvm` is available, with graceful fallback to software emulation |
| **Shutdown Handling** | Requests QMP guest poweroff, verifies guest-origin shutdown, and reports forced fallback |

---

## Prerequisites

- Linux host with Docker Engine installed
- Docker Compose v2+
- KVM support recommended (`/dev/kvm` available) for acceptable performance

> **Note:** KVM is optional. If unavailable, QEMU falls back to software emulation automatically. Performance will be lower but the router will function correctly.

---

## Supported Architectures

| Architecture | New CHR image release status |
|---|---|
| `linux/amd64` | Qualified through Docker runtime integration before publication |
| `linux/arm64` | Cross-built; not runtime-qualified |

Legacy tags may include `arm/v7`, `arm/v6`, and `386`; these platforms are not
published by the new CHR image release workflow. Do not substitute a legacy image
and assume it contains the new health, shutdown, or disk migration code.

---

## Quick Start

### 1. Clone the repository

```sh
git clone https://github.com/im-ecorp/mikrotik-routeros.git
cd mikrotik-routeros
```

### 2. Configure environment variables

Copy the example and set your desired RouterOS version:

```sh
cp .env.example .env
# Edit .env: set ROUTEROS_VERSION, MANAGEMENT_BIND_IP and optionally TZ
```

**.env example (use a published CHR image release):**
```sh
ROUTEROS_VERSION=7.21.4
TZ=Asia/Tehran
# Recommended for a new setup; remote access requires an SSH tunnel:
MANAGEMENT_BIND_IP=127.0.0.1
```

If `.env` is omitted, the `7.21.4` tag and `Asia/Tehran` timezone are used by default.

> **Seed vs. pulled tag.** `ROUTEROS_VERSION` here selects an image tag that must
> already exist in the registry. The Dockerfile's `ARG ROUTEROS_VERSION` selects
> the CHR seed the *next* build will bake in, and is currently **7.21.5** (the
> current longTerm release; MikroTik no longer offers 7.21.4). Compose moves to
> `7.21.5` only after that seed is built, qualified and published.
Public image tags are CHR seed versions: `7.21.4` and `v7.21.4`.
Before using these aliases, confirm the successful [recovery manifest](docs/releases.md#bounded-exact-digest-recovery)
shows the runtime-capable image in both registries. The previous alias digest does
not contain `/routeros/bin/runtime.py`; this recovery leaves `latest` untouched; it is not the
Compose default. Pin the verified digest for an immutable deployment.
`MANAGEMENT_BIND_IP` defaults to `0.0.0.0` (all host IPv4 addresses) when unset or
empty. The checked-in `.env.example` preserves that compatibility default; change
it explicitly to `127.0.0.1` before first boot for local/tunneled management.

**First-boot warning:** A fresh CHR may permit `admin` without a password. Do not
expose management to an untrusted network while setting credentials. Loopback
binding prevents direct remote management access; establish an SSH tunnel through
the **Docker host**, not RouterOS. See [networking and the tunnel example](docs/networking.md).
VPN and custom mappings remain unrestricted by `MANAGEMENT_BIND_IP`.

### 3. Start the container

```sh
docker compose up -d
```

### 4. Connect to MikroTik

These addresses assume the compatibility/public binding. With `127.0.0.1`, use
the local endpoints in the [SSH tunnel example](docs/networking.md#management-bindings-and-bootstrap-risk).

| Method | Address |
|---|---|
| **Winbox** | `<server-ip>:8291` |
| **SSH** | `ssh admin@<server-ip> -p 2222` |
| **WebFig** | `http://<server-ip>:80` |
| **API** | `<server-ip>:8728` |

Default credentials on a fresh CHR may be `admin` / *(no password — set one immediately)*.
Port publication does not enable a RouterOS service or configure its firewall.

---

## Architecture

### How It Works

```
┌──────────────────────────────────────────────────────┐
│  Docker Container (Alpine Linux)                     │
│                                                      │
│   ┌────────────────────────────────────┐             │
│   │  QEMU (x86_64 system emulation)    │             │
│   │                                    │             │
│   │   MikroTik RouterOS CHR            │             │
│   │   ├── chr.vdi  (persistent disk)   │             │
│   │   └── FAT drive (./shared)         │             │
│   └────────────────┬───────────────────┘             │
│                    │ TAP / bridge (qemubr0)           │
└────────────────────┼─────────────────────────────────┘
                     │
              Docker bridge network
              172.24.0.0/16
```

QEMU always emulates an x86_64 guest. ARM64 uses software emulation; a successful ARM64 image build is not an ARM runtime qualification. See [runtime requirements](docs/runtime.md).

### Directory Structure

```
.
├── bin/
│   ├── entrypoint.sh          # Container entrypoint & QEMU launcher
│   ├── init-disk.py           # Stable disk selection & legacy migration
│   ├── runtime.py            # Network validation, health, QMP & supervision
│   ├── qemu-ifup              # TAP interface bring-up script
│   └── qemu-ifdown            # TAP interface teardown script
├── data/                      # Auto-created — stores chr.vdi (persistent)
├── shared/                    # Auto-created — shared with MikroTik File Manager
├── .github/
│   └── workflows/
│       ├── docker-image.yml   # Manual image publishing
│       └── validate.yml       # PR/main checks; no publication or deployment
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

### Volumes

| Host Path | Container Path | Purpose |
|---|---|---|
| `./data` | `/routeros/data` | Stores the persistent virtual hard drive (`chr.vdi`) |
| `./shared` | `/routeros/shared` | Exposed to MikroTik as a virtual FAT drive |

> **Important:** Preserve `./data` and back it up while the guest is stopped. A single legacy `chr-*.vdi` is copied to `chr.vdi` without changing the original; multiple legacy disks require explicit selection. See [safe upgrades and recovery](docs/upgrades.md). The provided mounts are bind mounts, so `docker compose down -v` does not remove these host directories, but it is not an upgrade or reset procedure.

---

## Port Reference

| Port | Protocol | Service |
|---|---|---|
| `21` | TCP | FTP |
| `22` → host `2222` | TCP | SSH (remapped) |
| `23` | TCP | Telnet |
| `80` | TCP | WebFig (HTTP) |
| `443` | TCP | WebFig (HTTPS) |
| `1194` | TCP/UDP | OpenVPN |
| `1450` | TCP | Legacy custom mapping; purpose unknown, not L2TP |
| `1701` | UDP | L2TP |
| `1723` | TCP | PPTP control only; also requires GRE |
| `8291` | TCP | Winbox |
| `8728` | TCP | RouterOS API |
| `8729` | TCP | RouterOS API-SSL |
| `13231` | UDP | WireGuard |
| `9000–9100` | TCP | Reserved for custom services |

Management TCP ports (`21`, `2222`, `23`, `80`, `443`, `8291`, `8728`, `8729`) use
`MANAGEMENT_BIND_IP`. VPN and custom ports do not. A service on `9000–9100` must be
configured to listen on TCP and allowed through the guest/host firewall and NAT;
moving management there bypasses the management binding restriction.

**VPN limitations:** UDP `1701` is L2TP, not a complete L2TP/IPsec setup. IPsec also
needs UDP `500`/`4500` (not published here) and potentially ESP (IP protocol 50).
PPTP requires GRE (IP protocol 47), not merely TCP `1723`. ESP and GRE are not
TCP/UDP ports and cannot be added as normal port mappings. Publishing a port does
not enable the service or prove a working VPN. See [networking scope and limitations](docs/networking.md).

---

## Configuration

### Container Updates and RouterOS Upgrades

`ROUTEROS_VERSION` selects the container image and its factory seed. Existing
routers always reuse `./data/chr.vdi`; changing a tag does **not** upgrade or
downgrade the guest OS. Upgrade RouterOS through its own package manager.

Before changing either layer, shut down the guest and create a verified offline
backup. Follow [safe upgrades, legacy-disk migration, and rollback](docs/upgrades.md)
for the complete procedure. These behaviors require an image built with the new
disk initializer; updating this checkout alone does not update a running image.

### Changing the Timezone

```sh
TZ=Europe/Berlin
```

### Stopping the Container

```sh
docker compose down
```

Your configuration is stored in `./data` for the next startup. For a clean offline
backup, shut down RouterOS inside the guest before stopping the service; see the
[backup procedure](docs/upgrades.md#1-make-an-offline-backup-before-any-change).

---

## CI/CD: Building and Publishing Images

A separate `.github/workflows/validate.yml` runs on pull requests and pushes to
`main` with `contents: read`. It installs Docker Compose, requires a successful
version/config check, runs `bash -n` and `python3 -m unittest discover -s tests -v`,
and never publishes or deploys. Networking tests render actual Compose JSON;
[local validation](docs/networking.md#local-and-automatic-validation) supports a
standalone `COMPOSE_BINARY` and needs no Docker daemon. Missing Compose may skip
locally, but is an error in CI. Existing ShellCheck findings are not a new gate.

`Runtime integration` builds the actual Dockerfile with its default seed on an
isolated hosted runner. The fresh guest version must match the image's nonempty
`io.mikrotik-routeros.seed.version` OCI label. It also tests DHCP, HTTP/SSH TCP
forwarding, UDP DNS, health transitions, guest-clean stop/start, and recreation
persistence. Its evidence is uploaded separately from authentication-bearing
disks. See [runtime validation](docs/runtime.md).

`Publish CHR image` is manual and main-only. Successful `Validate` and
`Runtime integration` main-push runs must cover the **exact source SHA**.
Public aliases are the tested CHR seed version and its `v` alias:

```text
hossein3piol/mikrotik-routeros:7.21.4
ghcr.io/im-ecorp/mikrotik-routeros:7.21.4
# Both registries also publish the v7.21.4 alias.
```

Publication creates no `sha-<commit>` tag. The source commit travels inside the
image as the standard OCI label, so the tag list stays readable:

```bash
docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  hossein3piol/mikrotik-routeros:7.21.4
```

Existing CHR aliases require the explicit `approve_version_overwrite=true`
boolean dispatch input. SHA tags are never overwritten. The default-seed publisher
does not write `latest`; no workflow creates new `-r` suffix tags. Internal wrapper metadata remains in
OCI labels, separate from the CHR seed version. Docker Hub uses
`DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`; GHCR uses the scoped `GITHUB_TOKEN`.
See [release gates, platform qualifications, and exact-digest recovery](docs/releases.md).

The separate manual `current-chr-matrix.yml` workflow covers the reviewed website
CHR versions in `config/chr-versions.json`. Seventeen are recorded; **fifteen
qualify**. `6.49.21` and `6.49.22` are listed under `runtimeBlocked` because the
guest does not accept the persisted admin password after its first restart, so
runtime qualification cannot complete — see
[RouterOS 6 and current-version qualification](docs/runtime.md). They keep their
reviewed checksums but are excluded from the matrix and from the `latest` gate.

Each checksum-verified seed is built for amd64/arm64; the exact final amd64 digest
must pass full Docker/CHR integration before version aliases move. Only after all
fifteen pass and both registries read back correctly may it promote `latest` to
**7.24.4**, with explicit overwrite approval. `7.25beta5` is a `development`
channel build and is never a promotion target: the manifest pins `latest` to
`7.24.4` and validation rejects any other value. ARM64 is build-only;
the Dockerfile/Compose default remains `7.21.4`. Workflow availability is not
proof of publication: require its successful aggregate report before deployment.

---

## Advanced: OpenVPN Configuration

<details>
<summary>Click to expand — step-by-step OpenVPN setup guide</summary>

### 1. IP Pool Setup

Add a private IP range for VPN clients under **IP → Pool**.
For example: `172.24.0.0/16`

![ip_pool](./media/1.png)

### 2. Generate Certificates

Create or import the required certificates under **System → Certificates**.

![certificate](./media/2.png)

### 3. Create an OpenVPN Profile

Configure the PPP Profile under **PPP → Profiles** with the appropriate local and remote address settings.

![profile](./media/3.png)

### 4. Add Client Secrets

Define user credentials under **PPP → Secrets**.

![client](./media/4.png)

### 5. Configure the OpenVPN Interface

Create an OpenVPN Server interface under **Interfaces → OpenVPN Server**.
The example below uses port `4646` instead of the default `1194`.
Add a matching TCP or UDP mapping to Compose for `4646` and match the server/client
transport; that port is not published by the default file. Publishing both
transports on `1194` does not enable both on the RouterOS server.

![interface](./media/5.png)

### 6. Configure NAT Masquerade

This step is critical for client internet access. Add a masquerade rule under **IP → Firewall → NAT**.

![firewall_nat](./media/6.png)

</details>

---

## Troubleshooting

**Container exits immediately**
- Verify KVM is available: `ls -la /dev/kvm`
- Check logs: `docker compose logs -f`
- If KVM is unavailable, the container will fall back to software emulation automatically

**Cannot connect via Winbox**
- Confirm the container is running: `docker compose ps`
- Health checks verify running QEMU plus fresh guest ARP, not Winbox itself.
- After the boot grace period, inspect guest service and firewall settings separately.

**MikroTik lost its configuration after restart or a tag change**
- Verify the same `./data` directory is mounted.
- Do not delete or replace any disks. Older images selected versioned filenames.
- If several legacy disks exist, startup intentionally refuses to guess. Follow
  [legacy-disk selection and recovery](docs/upgrades.md#2-resolve-multiple-legacy-disks-if-necessary).
- Existing `chr.vdi` takes priority over all legacy files. A preserved legacy file
  does not receive subsequent guest writes.

**SSH connection refused**
- MikroTik SSH is on port `2222`, not `22`
- Connect with: `ssh admin@<server-ip> -p 2222`

**Bridge already exists error on restart**
- The entrypoint checks for an existing bridge before creating one — this is handled automatically

---

## Acknowledgments

- [lordbasex](https://github.com/lordbasex) — original inspiration for the QEMU-in-Docker approach

---

## Support

If this project saves you time in your infrastructure work, consider supporting its development:

- **USDT (TRC20):** `TH1iDsFr2wjgpptghFBn6h7DVt88pp5WoH`

---

<div align="center">
  
## Star History

<a href="https://www.star-history.com/?repos=im-ecorp%2Fmikrotik-routeros&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=im-ecorp/mikrotik-routeros&type=date&theme=dark&legend=top-left&sealed_token=Vdw0SgJpSWqG3v_GFS98zFJiIt81d03meN97sfKguGlO-dAxoFF4oKSGwOvO6wStWgbwllKXbOxtqSn5_J8JpBsEeWVkg_FI8Vm4aaARPpVGY1h6GWhqPw" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=im-ecorp/mikrotik-routeros&type=date&legend=top-left&sealed_token=Vdw0SgJpSWqG3v_GFS98zFJiIt81d03meN97sfKguGlO-dAxoFF4oKSGwOvO6wStWgbwllKXbOxtqSn5_J8JpBsEeWVkg_FI8Vm4aaARPpVGY1h6GWhqPw" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=im-ecorp/mikrotik-routeros&type=date&legend=top-left&sealed_token=Vdw0SgJpSWqG3v_GFS98zFJiIt81d03meN97sfKguGlO-dAxoFF4oKSGwOvO6wStWgbwllKXbOxtqSn5_J8JpBsEeWVkg_FI8Vm4aaARPpVGY1h6GWhqPw" />
 </picture>
</a>

</div>
