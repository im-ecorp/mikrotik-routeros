#!/usr/bin/env python3
"""Private QMP control and credential-free layer-2 guest readiness."""
import json
import socket
import time
import argparse
import ipaddress
import signal
import os
from pathlib import Path
import struct
import subprocess
import sys


def arp_probe(mac, target):
    # RFC 5227 probe: no IP address is claimed or taken from Docker IPAM.
    return (b'\xff' * 6 + mac + b'\x08\x06'
            + struct.pack('!HHBBH', 1, 0x800, 6, 4, 1)
            + mac + b'\x00' * 4 + b'\x00' * 6 + socket.inet_aton(target))


def is_guest_reply(packet, mac, target):
    return (len(packet) >= 42 and packet[:6] == mac
            and packet[12:22] == b'\x08\x06' + struct.pack('!HHBBH', 1, 0x800, 6, 4, 2)
            and packet[28:32] == socket.inet_aton(target)
            and packet[32:38] == mac and packet[38:42] == b'\x00' * 4)


def guest_responds(interface, target, timeout=2):
    mac = bytes.fromhex(Path('/sys/class/net', interface, 'address').read_text().strip().replace(':', ''))
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806)) as raw:
        raw.bind((interface, 0))
        raw.settimeout(timeout)
        raw.send(arp_probe(mac, target))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw.settimeout(max(.01, deadline - time.monotonic()))
            if is_guest_reply(raw.recv(2048), mac, target):
                return True
    return False


def health(run_dir):
    if (run_dir / 'stopping').exists():
        return False
    with QMP(run_dir / 'qmp.sock', timeout=2) as qmp:
        if qmp.execute('query-status').get('status') != 'running':
            return False
    state = json.loads((run_dir / 'network.json').read_text())
    return guest_responds('qemubr0', state['guest_ip'])


def network_plan(links, addrs, routes, gateway):
    interfaces = [link for link in links if link['ifname'] != 'lo']
    if (len(interfaces) != 1 or interfaces[0]['ifname'] != 'eth0'
            or interfaces[0].get('linkinfo', {}).get('info_kind') != 'veth'):
        raise ValueError('Only a private single-eth0 Docker bridge veth namespace is supported; host/macvlan/multi-network modes are refused')
    addresses = [a for interface in addrs if interface['ifname'] == 'eth0'
                 for a in interface.get('addr_info', []) if a['family'] == 'inet']
    if len(addresses) != 1:
        raise ValueError('Exactly one IPv4 address on eth0 is required')
    address = ipaddress.IPv4Interface((addresses[0]['local'], addresses[0]['prefixlen']))
    defaults = [route for route in routes if route.get('dst') == 'default']
    if defaults and (len(defaults) != 1 or defaults[0].get('dev') != 'eth0'):
        raise ValueError('Unsupported default route topology')
    gateway = gateway or (defaults[0].get('gateway') if defaults else None)
    if not gateway:
        raise ValueError('No default gateway; internal Docker networks require ROUTEROS_GATEWAY set to their Docker IPAM gateway')
    gateway = ipaddress.IPv4Address(gateway)
    if gateway not in address.network or gateway in (address.ip, address.network.network_address, address.network.broadcast_address):
        raise ValueError('Invalid Docker IPv4 gateway')
    return {'interface': 'eth0', 'guest_ip': str(address.ip), 'gateway': str(gateway),
            'netmask': str(address.network.netmask), 'prefixlen': address.network.prefixlen}


def prepare(run_dir):
    # Refuse host namespaces before any network mutation. No heuristic can make
    # privileged containers safe against a malicious operator: bridge mode only.
    if not Path('/.dockerenv').exists():
        raise ValueError('This entrypoint requires a Docker container')
    def ip_read(*args):
        return json.loads(subprocess.check_output(['ip', '-json', *args], text=True))
    plan = network_plan(ip_read('-details', 'link'), ip_read('addr'), ip_read('route'),
                        os.environ.get('ROUTEROS_GATEWAY'))
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    (run_dir / 'network.json').write_text(json.dumps(plan))
    (run_dir / 'dhcpd.conf').write_text(
        f"start {plan['guest_ip']}\nend {plan['guest_ip']}\nmaxleases 1\ninterface qemubr0\n"
        f"option subnet {plan['netmask']}\noption router {plan['gateway']}\n"
        "option dns 8.8.8.8 8.8.4.4\nlease_file /run/routeros/udhcpd.leases\n")


def guest_shutdown(events):
    return any(event.get('event') == 'SHUTDOWN' and event.get('data', {}).get('guest') is True
               and event.get('data', {}).get('reason') == 'guest-shutdown' for event in events)


def stop_guest(proc, run_dir, timeout):
    (run_dir / 'stopping').touch()
    record = {'requested': 'system_powerdown', 'guest_shutdown': False, 'forced': False, 'events': []}
    try:
        with QMP(run_dir / 'qmp.sock', timeout=3) as qmp:
            qmp.execute('system_powerdown')
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if guest_shutdown(qmp.events):
                    break
                qmp.sock.settimeout(max(.01, deadline - time.monotonic()))
                try:
                    qmp.receive()
                except (EOFError, OSError):
                    break
            record['events'] = qmp.events
            record['guest_shutdown'] = guest_shutdown(qmp.events)
    except (OSError, EOFError, ValueError, RuntimeError):
        record['qmp_error'] = True
    if record['guest_shutdown']:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        record['forced'] = True
        print('WARNING: guest clean shutdown deadline exceeded; forcing QEMU termination (disk corruption risk)', flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    record['qemu_returncode'] = proc.returncode
    record['clean'] = record['guest_shutdown'] and not record['forced'] and proc.returncode == 0
    (run_dir / 'shutdown.json').write_text(json.dumps(record, indent=2) + '\n')
    print('RouterOS shutdown: ' + ('guest-clean' if record['clean'] else 'NOT guest-clean'), flush=True)
    return record


def supervise(run_dir, command):
    timeout = int(os.environ.get('ROUTEROS_SHUTDOWN_TIMEOUT', '45'))
    if not 1 <= timeout <= 120:
        raise ValueError('ROUTEROS_SHUTDOWN_TIMEOUT must be 1..120 seconds')
    for name in ('stopping', 'qmp.sock', 'serial.sock', 'shutdown.json'):
        (run_dir / name).unlink(missing_ok=True)
    stopping = False
    def request_stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    state = json.loads((run_dir / 'network.json').read_text())
    dhcp = subprocess.Popen(['udhcpd', '-I', state['gateway'], '-f', str(run_dir / 'dhcpd.conf')])
    proc = None
    try:
        proc = subprocess.Popen(command)
        while proc.poll() is None and dhcp.poll() is None and not stopping:
            time.sleep(.1)
        if proc.poll() is None:
            record = stop_guest(proc, run_dir, timeout)
            return 0 if record['clean'] and dhcp.poll() is None else 1
        return proc.returncode
    finally:
        if proc is not None and proc.poll() is None:
            stop_guest(proc, run_dir, timeout)
        if dhcp.poll() is None:
            dhcp.terminate()
            try:
                dhcp.wait(timeout=3)
            except subprocess.TimeoutExpired:
                dhcp.kill(); dhcp.wait(timeout=3)


class QMP:
    def __init__(self, path, timeout=3):
        self.path = str(path)
        self.timeout = timeout
        self.counter = 0
        self.events = []

    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
            self.stream = self.sock.makefile('rwb')
            if 'QMP' not in self.receive():
                raise RuntimeError('Invalid QMP greeting')
            self.execute('qmp_capabilities')
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        if hasattr(self, 'stream'):
            self.stream.close()
        self.sock.close()

    def receive(self):
        line = self.stream.readline(1024 * 1024)
        if not line:
            raise EOFError('QMP closed')
        message = json.loads(line)
        if 'event' in message:
            self.events.append(message)
        return message

    def execute(self, command):
        self.counter += 1
        ident = self.counter
        self.stream.write((json.dumps({'execute': command, 'id': ident})+'\n').encode())
        self.stream.flush()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.01, deadline - time.monotonic()))
            message = self.receive()
            if message.get('id') == ident:
                if 'error' in message:
                    raise RuntimeError('QMP command rejected: ' + command)
                return message['return']
        raise TimeoutError('QMP reply timeout')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'health', 'supervise', 'qmp'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    directory = Path('/run/routeros')
    try:
        if args.action == 'prepare':
            prepare(directory)
        elif args.action == 'health':
            sys.exit(0 if health(directory) else 1)
        elif args.action == 'qmp':
            if args.command not in (['stop'], ['cont'], ['query-status']):
                parser.error('Only stop, cont and query-status are supported for diagnostics')
            with QMP(directory / 'qmp.sock') as client:
                print(json.dumps(client.execute(args.command[0])))
        else:
            sys.exit(supervise(directory, args.command))
    except (OSError, ValueError, RuntimeError, EOFError) as error:
        if args.action != 'health':
            print(f'Runtime error: {error}', file=sys.stderr)
        sys.exit(1)
