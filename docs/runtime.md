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
The runtime does not modify host interfaces, routes, firewall rules, or IPAM
allocations. The opt-in integration harness separately installs scoped host
firewall guards on its disposable bridge, as described below.
The guest disk remains `/routeros/data/chr.vdi`; the existing initializer and
legacy-disk migration rules are unchanged.

For an **internal** Docker network without a default route, explicitly set
`ROUTEROS_GATEWAY` to that network's Docker IPAM gateway, discovered using
`docker network inspect`; do not guess an address. Internal networks do not
provide the published-port path required by this integration test. Ordinary
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

The harness uses unique container/network/bridge names, a normal IPv4 bridge
with `com.docker.network.bridge.enable_ip_masquerade=false` and IPv4 gateway mode
`nat`, ephemeral **loopback-only** HTTP/SSH/DNS published ports, and disposable
private bind mounts. `--internal` is deliberately absent: modern Docker internal
bridges inhibit the port mappings needed to exercise the real forwarding path.
Gateway mode `isolated` also requires `--internal` and is not a substitute.

Disabling masquerade alone is **not isolation**. Before starting a guest, the
harness installs comment-tagged host `iptables` guards in `DOCKER-USER` that
drop new forwarded traffic entering or leaving only its uniquely named bridge.
A similarly scoped `INPUT` guard blocks guest-initiated host connections.
Established/related replies remain eligible for Docker's normal rules so host
loopback TCP/UDP forwarding can work. Scoped `ip6tables` INPUT/FORWARD guards
block IPv6 on this IPv4-only lab bridge. It requires root or passwordless
`sudo -n`, iptables/ip6tables, and Docker's iptables backend with an active
`FORWARD` jump to `DOCKER-USER`; unsupported backends fail before guest launch.
It never flushes chains, changes global policies, or prunes unrelated resources.
Only exact owned rules are deleted, after owned container/network removal; failed
resource removal retains the isolation guards. Removal errors fail the run.
The 840-second execution deadline is followed by bounded cleanup operations.

Isolation is checked on first boot, restart and recreation: guest pings toward
off-subnet TEST-NET address `198.51.100.1` and the host bridge gateway must return
zero replies **and increment the corresponding scoped DROP counter**. Mere
timeouts do not count as evidence, and no public service is required. The same
run must pass loopback HTTP, SSH and UDP DNS checks with these guards installed.
This is disposable test isolation, not a security boundary against the
privileged container or a malicious host administrator.

It verifies the actual RouterOS DHCP lease equals Docker's assigned address;
real HTTP success and SSH banners traverse Docker TCP forwarding; and an actual
UDP DNS query returns the configured `chr-integration.invalid` A record
`192.0.2.42` with the matching transaction ID. This UDP test is explicitly **not a
VPN handshake test**. It disables/re-enables the guest DHCP client to check ARP
health independently. Console mutations wait for a unique, anchored completion
tag, not a potentially stale/redrawn prompt. The test reads back the DHCP
`disabled` flag on both sides and keeps the console connected until recovery;
success additionally requires a bound lease matching Docker's assigned address.
It then pauses/resumes the guest through QMP to require Docker
healthy → unhealthy → healthy transitions, then checks password, identity,
version, DHCP and TCP/UDP persistence through a stop/start and removal/recreation
using the same stable disk. It requires guest-origin clean-shutdown evidence at
every stop and makes hash-verified cold copies before subsequent disk reuse.

Passwords are generated in memory and passed only through stdin to a private
UNIX-socket proxy. Neither raw serial text nor passwords are saved. Sanitized
`report.json` records image ID/labels, network topology, guest values, checks,
shutdown events, cold-copy hashes, cleanup results and script hash. Allowlisted
network diagnostics include `NetworkSettings.Ports`,
`HostConfig.PortBindings`, and endpoint IP/gateway/network ID, including on
failure; container environment and raw logs are not copied into the report.
DHCP transition diagnostics contain only phase, disabled flag, allowlisted status
and validated IPv4 address (null when unbound), or an exception type. Health
failures additionally retain container running/exit status, Docker health state,
failing streak and health-log exit codes (never log output), allowlisted QMP
`query-status`, and the exit code of a fresh production health probe. Diagnostics
do not convert failures to passes or extend the 160-second recovery deadline.

A successful initial lease does not prove unicast renewal works: BusyBox's `-I`
sets both reply source and DHCP server identifier, but does not assign an address
or intercept packets addressed to it. With the gateway identifier, renewal can
be addressed to the actual Docker host bridge rather than the container DHCP
socket. Merely substituting an unassigned dummy identifier does not provide a
reachable unicast server either. Broadcast reacquisition and T1 unicast renewal
are different paths (RFC 2131 sections 4.3.2 and 4.4.5). A hosted recovery timeout
without post-enable guest state is insufficient evidence to blame this path;
retain the diagnostics and distinguish it from console-command completion.

Docker stop/start must recreate the private network namespace: the entrypoint
requires fresh single-`eth0` topology, then creates `qemubr0`. It intentionally
does not delete or reuse an unexpected existing bridge; such topology fails
closed, and the restart checks must pass on the actual runner.
Separate
`*-shutdown.json` files preserve the exact supervisor evidence. Guest disks and
backups are deleted after successful resource cleanup and must never be uploaded
as CI artifacts. Failed cleanup is reported as failure; inspect and remove only
the uniquely named test resources before discarding any retained private files.

### RouterOS 6 and current-version qualification

Do not infer the login flow from the major version. Vendor-checksummed CHR
6.49.22, 7.24.4 and 7.25beta5 all exercised the existing forced fresh-password
flow in isolated QEMU TCG probes. No blank-password fallback or authentication
bypass is needed for those seeds. The same identity, HTTP/SSH service and static
DNS configuration commands read back correctly on all three. Disabled DHCP on
6.49.22 can return an empty status; the serial value parser accepts an anchored
empty result instead of timing out. Diagnostics classify that status as unknown,
not bound, and still require the disabled flag and subsequent bound address.

These are **partial offline results**, not release qualification. All three
answered the production zero-source ARP frame and produced guest-origin ACPI
shutdown events with QEMU exit zero. In restricted user-network tests, 7.24.4 and
7.25beta5 retained password, identity and service/DNS configuration across cold
restart. The 6.49.22 probe did **not** retain password/identity across cold restart,
despite clean shutdown; its cause remains unresolved. Loopback protocol attempts
under restricted QEMU user networking also timed out (including DNS on 7.24.4).
Those failures must not be treated as passes or used to weaken hosted protocol,
health, persistence or shutdown assertions. No runtime workaround is justified
by these probes alone; the actual per-version Docker matrix remains required.

A local environment without Docker can run protocol/unit tests and the offline
QEMU proof but must report Docker integration as **not run**, not passed.

### What `authentication/serial_timeout` looked like in run 35656139663

The first hosted run in which every variant reached runtime split cleanly in two:

| | `duration_seconds` | shutdowns recorded | checks |
| --- | --- | --- | --- |
| 7.23.5, 7.24.3, 7.24.4, 7.25beta5 — passed | 133.46, 136.69, 137.38, 138.12 | 3 | 21 |
| 6.49.21, 6.49.22, 7.24.2 — failed | 243.81, 246.05, 247.74 | **1** | 11–14 |

Every failure finished the first phase: health `healthy`, fresh guest with the
expected `guest_version`, DHCP `bound` at the Docker address, the seed-version
check, and the full disabled → reenabled → recovery DHCP cycle. Each then
recorded a **clean** `restart` shutdown (`guest_shutdown: true`, `qemu_returncode`
0) and stopped. The extra ~110 seconds before exit is a serial `expect` elapsing,
not work being done.

So the guest does not come back usable from the *first* restart, and the failure
surfaces as a timeout rather than a persistence error. That is consistent with the
6.49.22 cold-restart observation above: if the admin password is not retained, the
harness sends the stored password, RouterOS simply re-prompts `Login:`, and none
of `software license? [Y/n]`, `new password>` or the prompt ever appear. The
`Unexpected password reset on persisted guest` guard only fires when `new password>`
is actually seen, so this path cannot reach it.

A second full run (35667609143) separated the two causes. 7.24.3 passed once then
failed, and 7.24.2 stopped at 14 checks then 19, so those are timing under parallel
TCG guests and are addressed by `max-parallel: 1`. 6.49.21 and 6.49.22 stopped at
the **identical** point in every attempt.

### 6.49.21 and 6.49.22 are runtime-blocked

Comparing the `checks` arrays pins the failure exactly. Both complete checks 1–11
— health, fresh guest at the expected `guest_version`, seed match, HTTP/SSH/UDP DNS,
isolation, the full DHCP disable/re-enable cycle, and the `before-restart` cold
backup — then stop. A passing version's next two entries are `{"health": "healthy"}`
and `{"fresh": false, ...}`: the re-login to the **persisted** guest after restart.

So the guest does not accept the previously set admin password once it returns. The
harness sends the stored password, RouterOS re-prompts `Login:`, none of the awaited
patterns appear, and the read times out. The `Unexpected password reset on persisted
guest` guard cannot fire, because that requires actually seeing `new password>`.

This is guest-side, not harness-side: the same unchanged harness passes 6.49.17
through 6.49.20 and every 7.x version, and it reproduced identically across runs
35621045214, 35656139663 and 35667609143. It matches the independent offline probe
above, which found 6.49.22 losing password and identity across a cold restart.

Both are therefore listed in `runtimeBlocked` in `config/chr-versions.json`: kept on
record as reviewed versions, excluded from runtime qualification and from the
`latest` gate. They are **not** deleted, and no restart or authentication gate was
weakened to accommodate them. Remove them from that list to re-include them if a
future RouterOS release fixes the behaviour.

Treat the classification as `authentication/serial_timeout` only; do not record a
persistence claim without a transcript that shows the prompt.
