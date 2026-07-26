# MikroTik RouterOS on Docker

<div align="center">

[![Docker Image](https://img.shields.io/docker/v/hossein3piol/mikrotik-routeros?label=Docker%20Hub&logo=docker)](https://hub.docker.com/r/hossein3piol/mikrotik-routeros)
[![Docker Pulls](https://img.shields.io/docker/pulls/hossein3piol/mikrotik-routeros)](https://hub.docker.com/r/hossein3piol/mikrotik-routeros)
[![GitHub Stars](https://img.shields.io/github/stars/im-ecorp/mikrotik-routeros?style=social)](https://github.com/im-ecorp/mikrotik-routeros)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**A production-ready, QEMU-powered environment for running MikroTik RouterOS CHR inside Docker — on any architecture, without sacrificing your host OS.**

</div>

---

## Overview

This project packages MikroTik RouterOS (Cloud Hosted Router) inside a Docker container using QEMU for full x86_64 virtualization. It is designed for infrastructure engineers who need a reproducible, version-controlled MikroTik environment on any Linux server — with complete persistence, host file sharing, and safe port routing.

Because QEMU handles the x86_64 emulation layer, the Docker image itself is built for **multiple CPU architectures**. Whether your host is an AMD64 server, an ARM64 Raspberry Pi, or any other supported platform, you can pull and run the same image without any modifications.

---

## Features

| Feature | Description |
|---|---|
| **Multi-Architecture** | Pre-built for `amd64`, `arm64`, `arm/v7`, `arm/v6`, and `386` |
| **Persistent Storage** | Router configuration survives container rebuilds via a host-mounted virtual drive |
| **Host ↔ Guest File Sharing** | A local directory is exposed inside MikroTik's File Manager as a virtual FAT drive |
| **Safe SSH Access** | MikroTik SSH is remapped to port `2222` — your host SSH on port `22` is untouched |
| **Dynamic Port Range** | Ports `9000–9100` are pre-allocated for custom services — no Compose restarts needed |
| **Version Pinning** | Switch RouterOS versions via a single environment variable |
| **KVM Acceleration** | Hardware virtualization is enabled automatically when `/dev/kvm` is available, with graceful fallback to software emulation |
| **Graceful Shutdown** | SIGTERM is caught and forwarded to QEMU — MikroTik shuts down cleanly on `docker compose down` |

---

## Prerequisites

- Linux host with Docker Engine installed
- Docker Compose v2+
- KVM support recommended (`/dev/kvm` available) for acceptable performance

> **Note:** KVM is optional. If unavailable, QEMU falls back to software emulation automatically. Performance will be lower but the router will function correctly.

---

## Supported Architectures

| Architecture | Tag |
|---|---|
| x86-64 | `linux/amd64` |
| ARM 64-bit | `linux/arm64` |
| ARM 32-bit v7 | `linux/arm/v7` |
| ARM 32-bit v6 | `linux/arm/v6` |
| x86 32-bit | `linux/386` |

Docker will automatically pull the correct variant for your host platform.

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
# Edit .env and set ROUTEROS_VERSION and optionally TZ
```

**.env example:**
```sh
ROUTEROS_VERSION=7.22.2
TZ=Asia/Tehran
```

If `.env` is omitted, the `latest` tag and `Asia/Tehran` timezone are used by default.

### 3. Start the container

```sh
docker compose up -d
```

### 4. Connect to MikroTik

| Method | Address |
|---|---|
| **Winbox** | `<server-ip>:8291` |
| **SSH** | `ssh admin@<server-ip> -p 2222` |
| **WebFig** | `http://<server-ip>:80` |
| **API** | `<server-ip>:8728` |

Default credentials: `admin` / *(no password — set one immediately)*

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

Regardless of the host CPU architecture, QEMU always emulates an x86_64 machine for MikroTik — this is why the image runs identically on ARM and AMD64 hosts.

### Directory Structure

```
.
├── bin/
│   ├── entrypoint.sh          # Container entrypoint & QEMU launcher
│   ├── generate-dhcpd-conf.py # Dynamic DHCP config generator
│   ├── qemu-ifup              # TAP interface bring-up script
│   └── qemu-ifdown            # TAP interface teardown script
├── data/                      # Auto-created — stores chr.vdi (persistent)
├── shared/                    # Auto-created — shared with MikroTik File Manager
├── .github/
│   └── workflows/
│       └── docker-image.yml   # CI/CD pipeline for building & publishing images
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

### Volumes

| Host Path | Container Path | Purpose |
|---|---|---|
| `./data` | `/routeros/data` | Stores the persistent virtual hard drive (`chr.vdi`) |
| `./shared` | `/routeros/shared` | Exposed to MikroTik as a virtual FAT drive |

> **Important:** Never delete `./data` unless you intend to reset the router to factory defaults. Never run `docker compose down -v`.

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
| `1701` | UDP | L2TP |
| `1723` | TCP | PPTP |
| `8291` | TCP | Winbox |
| `8728` | TCP | RouterOS API |
| `8729` | TCP | RouterOS API-SSL |
| `13231` | UDP | WireGuard |
| `9000–9100` | TCP | Reserved for custom services |

> **Tip:** If you need to reassign a default MikroTik service port, use any port in the `9000–9100` range — it will be immediately reachable without modifying `docker-compose.yml`.

---

## Configuration

### Changing the RouterOS Version

Update `ROUTEROS_VERSION` in your `.env` file, then recreate the container:

```sh
docker compose down
docker compose up -d
```

> The existing `chr.vdi` in `./data` will be reused. To start fresh with the new version's default config, delete `./data/chr-*.vdi` before starting.

### Changing the Timezone

```sh
TZ=Europe/Berlin
```

### Stopping the Container

```sh
docker compose down
```

Your configuration is safely stored in `./data` and will persist for the next startup.

---

## CI/CD: Building and Publishing Images

A GitHub Actions workflow is included at `.github/workflows/docker-image.yml`. It builds a multi-architecture image and pushes it to both Docker Hub and GitHub Container Registry (GHCR).

### Required Secrets

Configure these under **Settings → Secrets and variables → Actions**:

| Secret | Description |
|---|---|
| `DOCKERHUB_USERNAME` | Your Docker Hub username |
| `DOCKERHUB_TOKEN` | Docker Hub access token |
| `CR_PAT` | GitHub Personal Access Token with `write:packages` scope |

### Triggering a Build

1. Go to **Actions → Build and Push MikroTik Image**
2. Click **Run workflow**
3. Fill in the inputs:

| Input | Description |
|---|---|
| `version` | RouterOS version to build (e.g. `7.22.2`) |
| `tag_latest` | Check to also push the `latest` tag for this version |

### Published Tags

Each build produces the following tags (both with and without the `v` prefix for maximum compatibility):

```
hossein3piol/mikrotik-routeros:7.22.2
hossein3piol/mikrotik-routeros:v7.22.2
ghcr.io/im-ecorp/mikrotik-routeros:7.22.2
ghcr.io/im-ecorp/mikrotik-routeros:v7.22.2

# If "tag_latest" was checked:
hossein3piol/mikrotik-routeros:latest
ghcr.io/im-ecorp/mikrotik-routeros:latest
```

### Building Manually on Your Server

```sh
docker build \
  --build-arg ROUTEROS_VERSION=7.22.2 \
  -t hossein3piol/mikrotik-routeros:v7.22.2 \
  -t hossein3piol/mikrotik-routeros:7.22.2 \
  .

docker push hossein3piol/mikrotik-routeros:v7.22.2
docker push hossein3piol/mikrotik-routeros:7.22.2
```

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
- The healthcheck polls port `8291` — wait until status shows `healthy`

**MikroTik lost its configuration after restart**
- Ensure `./data` directory exists and is writable by the container
- Never use `docker compose down -v`

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
