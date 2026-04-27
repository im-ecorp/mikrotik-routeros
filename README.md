# MikroTik RouterOS on Docker

<div align="center">

[![Docker Image](https://img.shields.io/docker/v/hossein3piol/mikrotik-routeros?label=Docker%20Hub&logo=docker)](https://hub.docker.com/r/hossein3piol/mikrotik-routeros)
[![Docker Pulls](https://img.shields.io/docker/pulls/hossein3piol/mikrotik-routeros)](https://hub.docker.com/r/hossein3piol/mikrotik-routeros)
[![GitHub Stars](https://img.shields.io/github/stars/im-ecorp/mikrotik-routeros?style=social)](https://github.com/im-ecorp/mikrotik-routeros)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**A production-ready, QEMU-powered environment for running MikroTik RouterOS CHR inside Docker — without sacrificing your host OS.**

</div>

---

## Overview

This project packages MikroTik RouterOS (Cloud Hosted Router) inside a Docker container using QEMU for full x86_64 virtualization. It is designed for infrastructure engineers who need a reproducible, version-controlled MikroTik environment on any Linux server — with complete persistence, host file sharing, and safe port routing.

---

## Features

| Feature | Description |
|---|---|
| **Persistent Storage** | Router configuration survives container rebuilds via a host-mounted virtual drive |
| **Host ↔ Guest File Sharing** | A local directory is exposed inside MikroTik's File Manager as a virtual FAT drive |
| **Safe SSH Access** | MikroTik SSH is remapped to port `2222` — your host SSH on port `22` is untouched |
| **Dynamic Port Range** | Ports `9000–9100` are pre-allocated for custom services — no Compose restarts needed |
| **Version Pinning** | Switch RouterOS versions via a single environment variable |
| **KVM Acceleration** | Hardware virtualization enabled by default when `/dev/kvm` is available |

---

## Prerequisites

- Linux host with Docker Engine installed
- Docker Compose v2+
- KVM support recommended (`/dev/kvm` available) for acceptable performance

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
# Edit .env and set ROUTEROS_VERSION (e.g. 7.21.4)
```

If `.env` is omitted, the `latest` tag is used by default.

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

Default credentials: `admin` / *(no password)*

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
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

### Volumes

| Host Path | Container Path | Purpose |
|---|---|---|
| `./data` | `/routeros/data` | Stores the persistent virtual hard drive (`chr.vdi`) |
| `./shared` | `/routeros/shared` | Exposed to MikroTik as a virtual FAT drive |

> **Important:** Never delete `./data` unless you intend to reset the router to factory defaults.

---

## Port Reference

| Port | Protocol | Service |
|---|---|---|
| `21` | TCP | FTP |
| `22` (→ host `2222`) | TCP | SSH |
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

> **Tip:** If you need to change the default port of a MikroTik service, assign it a value within the `9000–9100` range — it will be immediately reachable without modifying `docker-compose.yml`.

---

## Configuration

### Changing the RouterOS Version

Set `ROUTEROS_VERSION` in your `.env` file:

```sh
ROUTEROS_VERSION=7.21.4
```

Then rebuild:

```sh
docker compose down
docker compose up -d --build
```

### Changing the Timezone

Set `TZ` in your `.env` file:

```sh
TZ=Europe/Helsinki
```

### Stopping the Container

```sh
docker compose down
```

> Your configuration is safely stored in `./data` and will persist for the next startup.

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

## GitHub Actions: Building Your Own Image

A reusable workflow is included at `.github/workflows/docker-image.yml`. It builds and pushes a versioned image to both Docker Hub and GitHub Container Registry.

**To trigger it manually:**

1. Go to **Actions → Build and Push MikroTik Image**
2. Click **Run workflow**
3. Enter the RouterOS version (e.g. `7.21.4`)

The workflow produces two tags:
- `hossein3piol/mikrotik-routeros:v7.21.4`
- `hossein3piol/mikrotik-routeros:latest`

---

## Troubleshooting

**Container exits immediately**
- Verify KVM is available: `ls -la /dev/kvm`
- Check QEMU logs: `docker compose logs -f`

**Cannot connect via Winbox**
- Confirm the container is running: `docker compose ps`
- The healthcheck polls port `8291` — wait for `healthy` status

**MikroTik lost its configuration after restart**
- Ensure `./data` directory exists and is writable
- Never use `docker compose down -v` (removes volumes)

**SSH connection refused**
- MikroTik SSH is on port `2222`, not `22`
- Connect with: `ssh admin@<server-ip> -p 2222`

---

## Acknowledgments

- [lordbasex](https://github.com/lordbasex) — original inspiration for the QEMU-in-Docker approach

---

## Support

If this project saves you time in your infrastructure work, consider supporting its development:

- **USDT (TRC20):** `TH1iDsFr2wjgpptghFBn6h7DVt88pp5WoH`

---

<div align="center">

[![Stargazers over time](https://starchart.cc/im-ecorp/mikrotik-routeros.svg?variant=light)](https://starchart.cc/im-ecorp/mikrotik-routeros)

</div>
