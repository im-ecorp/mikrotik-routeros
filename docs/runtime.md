# Runtime, health, and isolated integration

## Supported networking

Use one Docker **bridge** network with one IPv4 address on `eth0`. The runtime
validates the topology before changing interfaces or initializing the disk.
Physical interfaces, host networking on ordinary hosts, macvlan, multiple attached
networks, missing IPv4, and ambiguous routes are rejected. Never use `network_mode:
host`, share another container's network namespace, or run this privileged image
on an untrusted/production host for testing. Topology checks are not a security
boundary against a privileged operator or nested container namespaces.

`eth0` is enslaved to `qemubr0`, its Docker-assigned IPv4 address is removed from
the container, and the private DHCP server leases that exact address to RouterOS
`ether1`. Docker's existing published-port forwarding therefore reaches the guest.
No host interfaces, routes, firewall rules, or IPAM allocations are modified.
The guest disk remains `/routeros/data/chr.vdi`; the existing initializer and
legacy-disk migration rules are unchanged.

For an **internal** Docker network without a default route, explicitly set
`ROUTEROS_GATEWAY` to that network's Docker IPAM gateway. The integration harness
discovers it using `docker network inspect`; do not guess an address. Ordinary
bridge networks derive it from the container's default route. The DHCP server
uses this gateway address as its server source, without assigning another IPv4
address to the container. The guest still receives the Docker subnet and gateway.

## What healthy means

Run `python3 /routeros/bin/runtime.py health` inside the container. Success requires:

1. No shutdown-in-progress marker.
2. QMP reports the guest CPU state as `running`.
3. A **fresh layer-2 ARP reply** for the leased guest IPv4 address, addressed to
   the bridge MAC in response to a zero-source (`0.0.0.0`) ARP probe.

The probe uses a private raw socket on `qemubr0`, not `localhost:8291`. There is no
container IPv4 after the handoff, so the former localhost healthcheck could never
reach Winbox. The new check does not reserve a hidden Docker IPAM address, install
a source route, need RouterOS credentials, occupy the serial console, or depend
on a management service being enabled. It requires `CAP_NET_RAW` (included by the
current privileged runtime). ARP-disabled or deliberately isolated guest interfaces
will report unhealthy. It proves responsive guest networking, **not** application,
VPN, Internet, or management-service readiness. Monitor those separately.

Compose allows 120 seconds of boot grace, then checks every 30 seconds with a
10-second timeout and three failures before unhealthy. Docker health status alone
does not trigger restart under `restart: unless-stopped`.

## Guest-clean shutdown

Python supervises QEMU and DHCP as PID 1 and reaps both. SIGTERM/SIGINT marks the
container as stopping, sends QMP `system_powerdown`, and waits up to
`ROUTEROS_SHUTDOWN_TIMEOUT` seconds (default 45; allowed 1–120). A QMP command reply
or `POWERDOWN` event is **not** shutdown evidence. Clean shutdown requires the
QMP `SHUTDOWN` event with `guest: true` and `reason: guest-shutdown`, followed by
QEMU exiting with status zero. The result is written to
`/run/routeros/shutdown.json`; runtime logs distinguish `guest-clean` from failure.
QMP and serial are private UNIX sockets in mode-0700 `/run/routeros`; authentication
and console text no longer enter Docker logs. Access requires container privileges.

After the deadline the supervisor explicitly warns of disk-corruption risk, sends
QEMU SIGTERM, waits five seconds, then uses SIGKILL if necessary. Forced termination
is never reported clean and results in a nonzero supervisor exit. Allow additional
QMP, process-exit and DHCP cleanup time: the Compose default is `stop_grace_period:
65s`. If increasing the guest timeout, increase Docker's stop timeout to at least
that timeout plus 20 seconds. `docker kill`/host failure bypasses guest shutdown.

Real isolated QEMU TCG tests exercise RouterOS ACPI rather than assuming support:
`tests/qemu-runtime-smoke.py` sends the production zero-source ARP frame over a
UNIX QEMU netdev and invokes the actual shutdown function. It verifies the reply,
guest-origin shutdown event, zero exit, pristine-seed hashes, and an intentionally
paused VM's bounded forced fallback. This does not replace Docker integration.

## Disposable Docker integration

Only run on an isolated Docker host, such as a GitHub-hosted Ubuntu runner. Build
or obtain the image first; the harness never downloads seeds or other images:

```sh
python3 tests/docker-integration.py --run \
  --image routeros-integration:SOURCE_SHA \
  --output-dir "$RUNNER_TEMP/routeros-evidence"
```

The harness uses unique container/network names, a new internal bridge, ephemeral
**loopback-only** HTTP/SSH/DNS published ports, and disposable private bind mounts.
It requires Docker privileges but never prunes unrelated resources. An 840-second
overall deadline plus bounded cleanup keeps execution within 15 minutes.

It verifies the actual RouterOS DHCP lease equals Docker's assigned address;
real HTTP success and SSH banners traverse Docker TCP forwarding; and an actual
UDP DNS query returns the configured `chr-integration.invalid` A record
`192.0.2.42` with the matching transaction ID. This UDP test is explicitly **not a
VPN handshake test**. It disables/re-enables the guest DHCP client to check ARP
health independently, then pauses/resumes the guest through QMP to require Docker
healthy → unhealthy → healthy transitions, then checks password, identity,
version, DHCP and TCP/UDP persistence through a stop/start and removal/recreation
using the same stable disk. It requires guest-origin clean-shutdown evidence at
every stop and makes hash-verified cold copies before subsequent disk reuse.

Passwords are generated in memory and passed only through stdin to a private
UNIX-socket proxy. Neither raw serial text nor passwords are saved. Sanitized
`report.json` records image ID/labels, network topology, guest values, checks,
shutdown events, cold-copy hashes, cleanup results and script hash. Separate
`*-shutdown.json` files preserve the exact supervisor evidence. Guest disks and
backups are deleted after successful resource cleanup and must never be uploaded
as CI artifacts. Failed cleanup is reported as failure; inspect and remove only
the uniquely named test resources before discarding any retained private files.

A local environment without Docker can run protocol/unit tests and the offline
QEMU proof but must report Docker integration as **not run**, not passed.
