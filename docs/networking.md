# Networking and first-boot access

## Port publishing is not service configuration

Compose publishes host ports toward the container/CHR network. It does **not**
enable RouterOS services, create VPN interfaces or peers, install certificates,
set credentials, or configure RouterOS firewall/NAT rules. A correct `compose
config` render proves configuration syntax and mappings, not guest reachability
or a working VPN. Host firewalls, Docker forwarding, upstream NAT and guest rules
must all be checked in an isolated environment before production use.

## Management bindings and bootstrap risk

`MANAGEMENT_BIND_IP` controls only these TCP host ports:

| Host port | Guest port | Service |
|---|---|---|
| 21 | 21 | FTP control |
| 2222 | 22 | SSH |
| 23 | 23 | Telnet |
| 80 | 80 | WebFig HTTP |
| 443 | 443 | WebFig HTTPS |
| 8291 | 8291 | Winbox |
| 8728 | 8728 | API |
| 8729 | 8729 | API-SSL |

The unset or empty default is `0.0.0.0` (all host IPv4 addresses), and
`.env.example` explicitly preserves it for existing users. This is a compatibility
default, **not a secure first-boot default**. Previously unspecified host bindings
could also expose IPv6 depending on Docker; these management mappings now specify
IPv4 explicitly. Do not assume IPv6 exposure or isolation without checking the host.

For a new installation, set this in `.env` **before starting the container**:

```dotenv
MANAGEMENT_BIND_IP=127.0.0.1
```

**Warning:** Remote connections directly to `<server-ip>:8291`, `:2222`, `:80`,
etc. will stop working with loopback binding. First establish SSH access to the
**Docker host**, not to RouterOS. From your workstation, open a tunnel:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18291:127.0.0.1:8291 \
  -L 127.0.0.1:12222:127.0.0.1:2222 \
  -L 127.0.0.1:18080:127.0.0.1:80 host-user@server-ip
```

Keep that connection open. Connect Winbox to `127.0.0.1:18291`, use
`ssh admin@127.0.0.1 -p 12222`, or open `http://127.0.0.1:18080` locally.
If the Docker host uses a nonstandard SSH port, add its `-p` option to the tunnel
command. Do not use RouterOS port 2222 as the tunnel's host SSH endpoint.

A fresh CHR may allow `admin` with no password. Never expose first boot to an
untrusted network while waiting to set a password. Set strong credentials, restrict
allowed management sources in RouterOS, and disable unused/insecure services
(such as Telnet and FTP). These actions are not automated by this Compose file.
The container remains privileged; loopback publishing does not sandbox it.
Docker-published traffic may bypass ordinary host INPUT/firewall rules, and
loopback binding is not a substitute for Docker-aware firewall controls or guest
access policy. Verify from another host before relying on isolation.

Changing `.env` alone does not change a running container. Plan a maintenance
window and preserve the guest disk before applying a Compose recreation; see
[upgrades and backups](upgrades.md). No deployment is performed by validation.

## VPN and custom mappings

These mappings are **not** restricted by `MANAGEMENT_BIND_IP`; they retain
Docker's unspecified-host binding behavior (normally all host addresses).

| Host and guest port | Transport | Meaning |
|---|---|---|
| 1194 | TCP and UDP | OpenVPN; existing TCP clients remain supported |
| 1701 | UDP | L2TP only; not a complete L2TP/IPsec setup |
| 13231 | UDP | WireGuard; configure the interface listen port and peers |
| 1723 | TCP | PPTP control only; see GRE limitation below |
| 1450 | TCP | Legacy custom mapping of unknown purpose, **not L2TP** |
| 9000–9100 | TCP | Custom services; no UDP range is published |

OpenVPN transport support depends on the RouterOS version and server
configuration. Publishing both transports does not make a TCP-only server listen
on UDP. Match client transport, server transport and listen port. A nondefault
port such as 4646 needs a matching Compose mapping; it is not included by default.
Custom TCP ports are usable only when the service is enabled/listening and the
entire firewall/NAT path permits access. A management service moved into the
custom range is **not** protected by `MANAGEMENT_BIND_IP`.

### L2TP/IPsec

UDP 1701 alone does not provide IPsec or encryption. IPsec commonly needs UDP
500 (IKE), UDP 4500 (NAT traversal), and, when not UDP-encapsulated, ESP (IP protocol
50). UDP 500/4500 are **not published** by this Compose file. ESP is an IP protocol,
not TCP/UDP port 50, and cannot be enabled with an ordinary Compose port mapping.
Even adding UDP 500/4500 does not prove ESP/NAT traversal or L2TP/IPsec works through
the Docker/guest network. Design and test the complete routing, NAT, firewall and
RouterOS IPsec policy path. Do not expose unprotected L2TP as a workaround.

### PPTP

TCP 1723 is only the control channel. PPTP data requires GRE (IP protocol 47),
not TCP/UDP port 47. Normal Compose port publishing does not forward GRE by adding
a port entry. NAT/connection tracking and routing must support it separately;
this Compose file does not guarantee working PPTP. Prefer a modern VPN rather
than PPTP for new deployments.

## Local and automatic validation

No Docker daemon, guest boot, image pull or publication is needed for these checks:

```sh
docker compose version
docker compose --env-file .env.example -f docker-compose.yml config --quiet
REQUIRE_COMPOSE=1 python3 -m unittest discover -s tests -v
for script in bin/*.sh bin/qemu-ifup bin/qemu-ifdown; do
  bash -n "$script"
done
```

The networking unittest executes real Compose `config --format json` via
`subprocess`, checking protocols, management binding scope/defaults, the example
environment, legacy/custom mappings and retained runtime settings. It ignores
local `.env` and Compose overrides to make these tests reproducible. It discovers
`docker compose` or `docker-compose`. To select a standalone executable:

```sh
COMPOSE_BINARY=/absolute/path/to/docker-compose REQUIRE_COMPOSE=1 \
  python3 -m unittest discover -s tests -v
```

Without Compose, local networking tests skip explicitly. `REQUIRE_COMPOSE=1` or an
unavailable explicit `COMPOSE_BINARY` makes that an error. The `Validate` GitHub
Actions workflow installs pinned Docker Compose, requires `docker compose version`
and a successful config render **before** unit tests, and sets `REQUIRE_COMPOSE=1`.
It runs on pull requests and pushes to `main` with `contents: read`, no publishing
credentials, no image publication and no deployment. The separate manual image
publishing workflow is unchanged. Shell syntax is checked with `bash -n`; existing
ShellCheck findings are not a new blocking gate.

These checks do not validate live packet forwarding, TCP/UDP listeners, VPN
handshakes, host firewall behavior or the existing healthcheck's accuracy.
