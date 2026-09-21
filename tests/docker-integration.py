#!/usr/bin/env python3
"""Opt-in disposable Docker bridge integration. Prebuilt image; no downloads.

Run only on an isolated Docker host (for example a GitHub-hosted runner).
Passwords and raw serial exchanges never enter logs, argv, or evidence.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time

SPEC = importlib.util.spec_from_file_location('chr_smoke', Path(__file__).with_name('chr-smoke.py'))
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)
require = SMOKE.require


def dns_query(ident, name):
    return struct.pack('!6H', ident, 0x0100, 1, 0, 0, 0) + b''.join(
        bytes([len(label)]) + label.encode('ascii') for label in name.split('.')) + b'\x00\x00\x01\x00\x01'


def dns_answer(packet, ident, expected):
    def skip_name(offset):
        for _ in range(128):
            length = packet[offset]
            if length & 0xc0 == 0xc0:
                return offset + 2
            offset += 1
            if not length:
                return offset
            if length > 63:
                raise ValueError('Invalid DNS name')
            offset += length
        raise ValueError('DNS name too long')
    try:
        rid, flags, questions, answers, _, _ = struct.unpack('!6H', packet[:12])
        if rid != ident or not flags & 0x8000 or flags & 0x020f or questions != 1:
            return False
        offset = skip_name(12) + 4
        for _ in range(answers):
            offset = skip_name(offset)
            kind, cls, _, length = struct.unpack('!HHIH', packet[offset:offset+10])
            offset += 10
            if kind == cls == 1 and length == 4 and packet[offset:offset+4] == socket.inet_aton(expected):
                return True
            offset += length
    except (IndexError, ValueError, struct.error):
        pass
    return False


# Raw bridge between subprocess pipes and the container's private UNIX console.
# This is code, not a shell string; passwords are exclusively stdin bytes.
SERIAL_PROXY = '''import socket,sys,select,os
s=socket.socket(socket.AF_UNIX);s.connect('/run/routeros/serial.sock')
while True:
 ready,_,_=select.select([s,sys.stdin.buffer],[],[])
 if s in ready:
  data=s.recv(65536)
  if not data:break
  sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
 if sys.stdin.buffer in ready:
  data=os.read(0,65536)
  if not data:break
  s.sendall(data)
'''


class Serial(SMOKE.VM):
    def __init__(self, container):
        self.buffer = ''
        self.timeout = 150
        self.capture = False
        self.transcript = ''
        self.record = {}
        self.proc = subprocess.Popen(['docker', 'exec', '-i', container, 'python3', '-c', SERIAL_PROXY],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.send('')

    def __enter__(self):
        return self

    def login(self, password, fresh):
        super().login(password, fresh)
        self.capture = False
        self.transcript = ''

    def __exit__(self, *_):
        # Do not log any serial text (including command echoes).
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill(); self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except BrokenPipeError:
                pass
        self.buffer = self.transcript = ''


class Lab:
    def __init__(self, image, output):
        self.image = image
        self.output = output
        self.name = 'chr-it-' + secrets.token_hex(6)
        self.network = self.name + '-net'
        # Keep authentication-bearing disks outside the uploadable evidence tree.
        self.root = Path(tempfile.mkdtemp(prefix=self.name+'-', dir=output.parent))
        self.data = self.root / 'data'; self.data.mkdir()
        self.shared = self.root / 'shared'; self.shared.mkdir()
        self.report = {'status': 'running', 'name': self.name, 'image': image, 'checks': [],
                       'isolation': 'Unique IPv4 Docker bridge without masquerade; loopback published ports; host firewall guards scoped to the unique bridge; disposable bind data',
                       'limitations': ['UDP proves configured DNS request/response, not a VPN handshake',
                                       'No in-guest package upgrade is performed'], 'shutdowns': []}
        self.password = 'ChrLab!' + secrets.token_urlsafe(24)
        self.marker = self.name
        self.container_created = self.network_created = False
        self.bridge = self.name[-12:]
        self.firewall_rules = []

    def firewall(self, family, *args, check=True):
        command = (['sudo', '-n'] if os.geteuid() != 0 else [])
        result = subprocess.run(command + [family, '--wait', '5', *args],
                                capture_output=True, text=True, timeout=15)
        if check and result.returncode:
            raise RuntimeError('Scoped firewall operation failed: ' + family + ' ' + args[0])
        return result

    def setup_network(self):
        self.docker('network', 'create', '--driver', 'bridge',
                    '--opt', 'com.docker.network.bridge.enable_ip_masquerade=false',
                    '--opt', 'com.docker.network.bridge.gateway_mode_ipv4=nat',
                    '--opt', 'com.docker.network.bridge.name='+self.bridge,
                    '--label', 'routeros.integration='+self.name, self.network)
        self.network_created = True
        net = json.loads(self.docker('network', 'inspect', self.network).stdout)[0]
        require(not net['Internal'] and not net.get('EnableIPv6') and net['Driver'] == 'bridge',
                'Expected IPv4 non-internal bridge for Docker port publishing')
        require(net['Options'].get('com.docker.network.bridge.enable_ip_masquerade') == 'false'
                and net['Options'].get('com.docker.network.bridge.name') == self.bridge,
                'Bridge isolation options differ')
        self.gateway = net['IPAM']['Config'][0]['Gateway']
        self.report['network'] = {'internal': net['Internal'], 'ipam': net['IPAM'],
                                  'bridge': self.bridge, 'options': net['Options']}
        # Refuse unsupported firewall backends before starting any guest. Never
        # flush chains, alter policies, or change rules belonging to Docker.
        self.firewall('iptables', '-C', 'FORWARD', '-j', 'DOCKER-USER')
        fresh = ['-m', 'conntrack', '!', '--ctstate', 'ESTABLISHED,RELATED']
        specs = [('iptables', 'DOCKER-USER', ['-i', self.bridge] + fresh),
                 ('iptables', 'DOCKER-USER', ['-o', self.bridge] + fresh),
                 ('iptables', 'INPUT', ['-i', self.bridge] + fresh),
                 ('ip6tables', 'FORWARD', ['-i', self.bridge]),
                 ('ip6tables', 'FORWARD', ['-o', self.bridge]),
                 ('ip6tables', 'INPUT', ['-i', self.bridge])]
        for family, chain, rule in specs:
            rule += ['-m', 'comment', '--comment', self.name, '-j', 'DROP']
            # Remember exact rule before insertion, including timeout/interrupt.
            self.firewall_rules.append((family, chain, rule))
            self.firewall(family, '-I', chain, '1', *rule)
            self.firewall(family, '-C', chain, *rule)

    def docker(self, *args, timeout=60, check=True):
        result = subprocess.run(['docker', *map(str, args)], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            # Avoid dumping arbitrary daemon/container output into authentication logs.
            raise RuntimeError('Docker operation failed: ' + ' '.join(map(str, args[:2])) + f' (exit {result.returncode})')
        return result

    def inspect(self):
        return json.loads(self.docker('inspect', self.name).stdout)[0]

    def wait(self, predicate, label, timeout=160):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(1)
        raise RuntimeError('Timed out: ' + label)

    def health(self, expected):
        self.wait(lambda: self.inspect()['State'].get('Health', {}).get('Status') == expected,
                  'Docker health ' + expected, timeout=160 if expected == 'healthy' else 40)
        self.report['checks'].append({'health': expected})

    def create(self):
        self.docker('run', '-d', '--name', self.name, '--label', 'routeros.integration='+self.name,
                    '--privileged', '--network', self.network, '--restart', 'no',
                    '--stop-timeout', '65', '--env', 'ROUTEROS_GATEWAY='+self.gateway,
                    '--health-cmd', 'python3 /routeros/bin/runtime.py health',
                    '--health-interval', '3s', '--health-timeout', '6s',
                    '--health-start-period', '0s', '--health-retries', '2',
                    '--publish', '127.0.0.1::80/tcp', '--publish', '127.0.0.1::22/tcp',
                    '--publish', '127.0.0.1::53/udp',
                    '--mount', f'type=bind,src={self.data},dst=/routeros/data',
                    '--mount', f'type=bind,src={self.shared},dst=/routeros/shared', self.image)
        self.container_created = True

    def console(self):
        self.wait(lambda: self.docker('exec', self.name, 'test', '-S', '/run/routeros/serial.sock', check=False).returncode == 0,
                  'serial socket')
        return Serial(self.name)

    def configure(self, fresh):
        with self.console() as console:
            console.login(self.password, fresh)
            if fresh:
                console.command_done(f'/system identity set name={self.marker}')
                console.command_done('/ip service set [find name=www] disabled=no')
                console.command_done('/ip service set [find name=ssh] disabled=no')
                console.command_done('/ip dns set allow-remote-requests=yes')
                console.command_done('/ip dns static add name=chr-integration.invalid address=192.0.2.42')
            identity = console.value('IT_IDENTITY', '/system identity get name')
            version = console.value('IT_VERSION', '/system resource get version')
            require(identity == self.marker, 'Persisted identity differs')
            self.health('healthy')
            guest_ip = self.inspect()['NetworkSettings']['Networks'][self.network]['IPAddress']
            address = console.value('IT_ADDRESS', '/ip dhcp-client get [find interface=ether1] address')
            status = console.value('IT_DHCP', '/ip dhcp-client get [find interface=ether1] status')
            require(status == 'bound' and address.split('/')[0] == guest_ip, 'Guest did not obtain Docker IP via DHCP')
            self.report['checks'].append({'fresh': fresh, 'identity': identity, 'guest_version': version,
                                          'dhcp': status, 'guest_address': address, 'docker_address': guest_ip})
            # Explicit logout means later console connections cannot inherit authentication.
            console.send('/quit')
        return version

    def drop_count(self, chain):
        result = self.firewall('iptables', '-L', chain, '-n', '-v', '-x')
        rows = [line.split() for line in result.stdout.splitlines()
                if self.name in line and 'DROP' in line]
        rows = [row for row in rows if len(row) > 6 and row[5] == self.bridge]
        require(len(rows) == 1 and rows[0][0].isdigit(), 'Missing scoped firewall counter')
        return int(rows[0][0])

    def verify_isolation(self):
        # An unreachable destination is not isolation evidence. Count packets
        # actually dropped by our bridge-specific guard; no public service is
        # contacted. TEST-NET is outside the Docker subnet and uses its default
        # route. Also prove new connections to the host bridge are blocked.
        probes = []
        with self.console() as console:
            console.login(self.password, False)
            for target, chain in [('198.51.100.1', 'DOCKER-USER'), (self.gateway, 'INPUT')]:
                before = self.drop_count(chain)
                received = console.value('IT_ISOLATION', f'/ping address={target} count=3 interval=200ms')
                after = self.drop_count(chain)
                require(received == '0' and after - before >= 3,
                        'Isolation requires zero replies and a counted firewall drop')
                probes.append({'destination': target, 'chain': chain, 'received': 0,
                               'dropped_packets': after - before})
            console.send('/quit')
        self.report['checks'].append({'external_egress_blocked': True,
                                      'guest_initiated_host_access_blocked': True, 'probes': probes})

    def network_diagnostics(self):
        # Explicit allowlist: never serialize Config.Env or raw container logs.
        info = self.inspect()
        network = info.get('NetworkSettings', {})
        diagnostics = {'Ports': network.get('Ports'),
                       'PortBindings': info.get('HostConfig', {}).get('PortBindings'),
                       'Networks': {name: {key: values.get(key) for key in
                                    ('IPAddress', 'Gateway', 'NetworkID')}
                                    for name, values in network.get('Networks', {}).items()}}
        self.report['network_diagnostics'] = diagnostics
        return diagnostics

    def protocols(self):
        ports = self.network_diagnostics()['Ports'] or {}
        def port(key):
            bindings = ports.get(key)
            require(bool(bindings), 'Missing published port: ' + key)
            require(len(bindings) == 1 and bindings[0]['HostIp'] == '127.0.0.1',
                    'Port is not exclusively loopback bound: ' + key)
            require(str(bindings[0].get('HostPort', '')).isdigit(), 'Missing host port: ' + key)
            return int(bindings[0]['HostPort'])
        with socket.create_connection(('127.0.0.1', port('80/tcp')), timeout=5) as conn:
            conn.sendall(b'GET / HTTP/1.0\r\nHost: localhost\r\n\r\n')
            response = conn.recv(4096)
            require(response.startswith(b'HTTP/1.') and b'200' in response.split(b'\r\n')[0], 'Guest HTTP response not successful')
            http = response.split(b'\r\n')[0].decode('ascii')
        with socket.create_connection(('127.0.0.1', port('22/tcp')), timeout=5) as conn:
            banner = conn.recv(1024)
            require(banner.startswith(b'SSH-2.0-'), 'Guest SSH banner missing')
        ident = secrets.randbelow(65536)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as conn:
            conn.settimeout(5)
            conn.connect(('127.0.0.1', port('53/udp')))
            conn.send(dns_query(ident, 'chr-integration.invalid'))
            answer = conn.recv(4096)
            require(dns_answer(answer, ident, '192.0.2.42'), 'Guest UDP DNS record differs')
        self.report['checks'].append({'http': http, 'ssh_banner': banner.decode('ascii').strip(),
                                      'udp_dns': {'name': 'chr-integration.invalid', 'address': '192.0.2.42', 'transaction_verified': True}})

    def stop(self, label):
        self.docker('stop', '--time', '65', self.name, timeout=80)
        state = self.inspect()['State']
        require(not state['Running'], 'Container still running after stop')
        target = self.output / (label + '-shutdown.json')
        self.docker('cp', self.name+':/run/routeros/shutdown.json', target)
        evidence = json.loads(target.read_text())
        require(evidence.get('clean') and evidence.get('guest_shutdown') and not evidence.get('forced')
                and evidence.get('qemu_returncode') == 0 and state['ExitCode'] == 0,
                'Guest shutdown was not clean; see shutdown evidence')
        require(any(e.get('event') == 'SHUTDOWN' and e.get('data') == {'guest': True, 'reason': 'guest-shutdown'}
                    for e in evidence['events']), 'Missing guest-origin shutdown event')
        self.report['shutdowns'].append({'label': label, **evidence})

    def backup(self, label):
        require(not self.inspect()['State']['Running'], 'Cold copy requires a stopped guest')
        source = self.data / 'chr.vdi'
        require(source.is_file() and not source.is_symlink(), 'Stable disk missing')
        dest = self.root / (label + '-verified.vdi')
        verification = self.root / (label + '-readback.vdi')
        # Guest disks are root-owned mode 0600; docker cp yields runner-owned
        # cold copies without weakening disk permissions or requiring host sudo.
        self.docker('cp', self.name+':/routeros/data/chr.vdi', dest)
        self.docker('cp', self.name+':/routeros/data/chr.vdi', verification)
        digest = SMOKE.sha256(verification)
        require(digest == SMOKE.sha256(dest), 'Cold disk backup differs from independent readback')
        verification.unlink()
        self.report['checks'].append({'cold_backup': label, 'sha256': digest})
        return digest

    def run(self):
        self.docker('info', '--format', '{{.OSType}}', timeout=20)
        image_info = json.loads(self.docker('image', 'inspect', self.image).stdout)[0]
        self.report['image_id'] = image_info['Id']
        self.report['image_labels'] = image_info['Config'].get('Labels') or {}
        seed_version = self.report['image_labels'].get('io.mikrotik-routeros.seed.version')
        require(isinstance(seed_version, str) and seed_version.strip(),
                'Image seed version label is missing or empty')
        self.setup_network()
        self.create()
        initial_version = self.configure(True)
        # RouterOS appends its channel (for example " (stable)") to the version.
        require(initial_version.split()[:1] == [seed_version],
                'Fresh guest version differs from image seed version label')
        self.report['checks'].append({'seed_version': seed_version,
                                      'fresh_guest_matches_seed': True})
        self.protocols()
        self.verify_isolation()
        # QMP still says running: loss of the DHCP address must fail ARP health.
        with self.console() as console:
            console.login(self.password, False)
            console.command_done('/ip dhcp-client disable [find interface=ether1]')
            self.health('unhealthy')
            console.command_done('/ip dhcp-client enable [find interface=ether1]')
            console.send('/quit')
        self.health('healthy')
        self.report['checks'].append({'arp_health_tracks_guest_address': True})
        # Exercise loss of guest execution, not an irrelevant container service.
        self.docker('exec', self.name, 'python3', '/routeros/bin/runtime.py', 'qmp', 'stop')
        self.health('unhealthy')
        self.docker('exec', self.name, 'python3', '/routeros/bin/runtime.py', 'qmp', 'cont')
        self.health('healthy')
        self.stop('restart')
        self.backup('before-restart')
        self.docker('start', self.name)
        require(self.configure(False) == initial_version, 'Restart changed guest version')
        self.protocols()
        self.verify_isolation()
        self.stop('recreate')
        self.backup('before-recreate')
        self.docker('rm', self.name); self.container_created = False
        self.create()
        require(self.configure(False) == initial_version, 'Recreation changed guest version')
        self.protocols()
        self.verify_isolation()
        self.stop('final')
        self.backup('final')
        self.report['status'] = 'passed'

    def cleanup(self):
        # Owned names only; never global prune. Disks remain private until removal.
        errors = []
        if self.container_created:
            result = self.docker('rm', '-f', self.name, timeout=30, check=False)
            if result.returncode:
                errors.append('container removal failed')
        if self.network_created:
            result = self.docker('network', 'rm', self.network, timeout=30, check=False)
            if result.returncode:
                errors.append('network removal failed')
        # Keep isolation intact if any guest or network failed to disappear.
        if not errors:
            for family, chain, rule in reversed(self.firewall_rules):
                try:
                    exists = self.firewall(family, '-C', chain, *rule, check=False)
                    if exists.returncode == 0:
                        self.firewall(family, '-D', chain, *rule)
                    else:
                        require(exists.returncode == 1, 'Cannot inspect firewall rule')
                    require(self.firewall(family, '-C', chain, *rule, check=False).returncode == 1,
                            'Scoped firewall rule still present')
                except Exception:
                    errors.append('scoped firewall cleanup failed: '+family+' '+chain)
        self.report['cleanup_errors'] = errors
        if errors:
            self.report['status'] = 'failed'
        else:
            # Never upload guest disks containing disposable authentication state.
            shutil.rmtree(self.root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Acknowledge privileged disposable Docker execution')
    parser.add_argument('--image', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error('--run is required; use only an isolated Docker host')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.output_dir, 0o700)
    lab = Lab(args.image, args.output_dir.resolve())
    def interrupted(*_):
        raise RuntimeError('Integration exceeded 840 seconds or was interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    signal.alarm(840)
    started = time.monotonic()
    try:
        lab.run()
    except Exception as error:
        lab.report['status'] = 'failed'
        lab.report['error'] = f'{type(error).__name__}: {error}'.replace(lab.password, '[REDACTED]')
        if lab.container_created:
            try:
                lab.network_diagnostics()
            except Exception as diagnostic_error:
                lab.report['network_diagnostics_error'] = type(diagnostic_error).__name__
    finally:
        signal.alarm(0)
        try:
            lab.cleanup()
        except Exception as error:
            lab.report['status'] = 'failed'
            lab.report['cleanup_errors'] = [type(error).__name__]
        lab.report['duration_seconds'] = round(time.monotonic() - started, 2)
        lab.report['harness_sha256'] = SMOKE.sha256(Path(__file__))
        (args.output_dir / 'report.json').write_text(json.dumps(lab.report, indent=2) + '\n')
    print(json.dumps({'status': lab.report['status'], 'report': str(args.output_dir / 'report.json'),
                      'error': lab.report.get('error')}))
    return 0 if lab.report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
