#!/usr/bin/env python3
"""Opt-in real CHR ARP and ACPI proof without Docker or host network changes."""
import argparse
import importlib.util
import json
from pathlib import Path
import secrets
import shutil
import socket
import struct
import sys
import tempfile
import time


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[1]
SMOKE = load('chr_smoke', ROOT / 'tests/chr-smoke.py')
RUNTIME = load('chr_runtime', ROOT / 'bin/runtime.py')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--seed', type=Path, action='append', required=True)
    parser.add_argument('--scratch-parent', type=Path, required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error('--run required')
    lab = Path(tempfile.mkdtemp(prefix='qmp-arp-', dir=args.scratch_parent))
    report = {'status': 'running', 'vms': [], 'seeds': [], 'proofs': []}
    print('LAB='+str(lab), flush=True)
    try:
        for index, seed in enumerate(args.seed):
            seed = seed.resolve()
            digest = SMOKE.sha256(seed)
            record = {'path': str(seed), 'before': digest}
            report['seeds'].append(record)
            disk = lab / f'chr-{index}.vdi'
            shutil.copyfile(seed, disk)
            SMOKE.require(SMOKE.sha256(disk) == digest, 'Disposable seed copy differs')
            vm = SMOKE.VM(disk, lab, f'guest-{index}', 'qemu-system-x86_64', 180, report)
            nic = vm.command.index('-nic'); del vm.command[nic:nic+2]
            net = lab / f'net-{index}.sock'
            run = lab / f'run-{index}'; run.mkdir()
            vm.command += ['-qmp', f'unix:{run}/qmp.sock,server=on,wait=off',
                           '-netdev', f'stream,id=n,server=on,addr.type=unix,addr.path={net}',
                           '-device', 'e1000,netdev=n']
            with vm:
                deadline = time.monotonic() + 10
                while not net.exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                with socket.socket(socket.AF_UNIX) as network:
                    network.settimeout(5); network.connect(str(net))
                    vm.login('ChrLab!' + secrets.token_urlsafe(24), True)
                    vm.capture = False
                    version = vm.value('PROOF_VERSION', '/system resource get version')
                    vm.command_done('/ip address add address=192.0.2.2/24 interface=ether1')
                    mac = bytes.fromhex('020000000001')
                    frame = RUNTIME.arp_probe(mac, '192.0.2.2')
                    network.sendall(struct.pack('!I', len(frame)) + frame)
                    buffer = b''; found = False; deadline = time.monotonic() + 15
                    packets = []
                    network.settimeout(1)
                    while time.monotonic() < deadline and not found:
                        try:
                            buffer += network.recv(65536)
                        except socket.timeout:
                            network.sendall(struct.pack('!I', len(frame)) + frame)
                            continue
                        while len(buffer) >= 4:
                            length = struct.unpack('!I', buffer[:4])[0]
                            if len(buffer) < length + 4:
                                break
                            packet, buffer = buffer[4:4+length], buffer[4+length:]
                            if packet[12:14] == b'\x08\x06':
                                packets.append(packet.hex())
                            found = found or RUNTIME.is_guest_reply(packet, mac, '192.0.2.2')
                    report['arp_packets'] = packets
                    SMOKE.require(found, 'RouterOS did not answer zero-source ARP probe')
                shutdown = RUNTIME.stop_guest(vm.proc, run, 45)
                vm.record['clean_shutdown'] = shutdown['clean']
                SMOKE.require(shutdown['clean'], 'ACPI did not result in guest-clean shutdown')
                report['proofs'].append({'version': version, 'zero_source_arp_reply': found, 'shutdown': shutdown})
            record['after'] = SMOKE.sha256(seed)
            SMOKE.require(record['after'] == digest, 'Pristine seed changed')
        # A stopped CPU cannot consume ACPI. Prove actual forced fallback is bounded.
        fallback_run = lab / 'fallback'; fallback_run.mkdir()
        import subprocess
        proc = subprocess.Popen(['qemu-system-x86_64', '-machine', 'pc,accel=tcg', '-m', '64',
                                 '-S', '-nic', 'none', '-display', 'none', '-serial', 'none',
                                 '-monitor', 'none', '-qmp', f'unix:{fallback_run}/qmp.sock,server=on,wait=off'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic()+10
            while not (fallback_run/'qmp.sock').exists() and time.monotonic()<deadline:
                time.sleep(.1)
            started = time.monotonic()
            fallback = RUNTIME.stop_guest(proc, fallback_run, 1)
            SMOKE.require(fallback['forced'] and not fallback['clean'] and proc.poll() is not None,
                          'Paused guest did not use explicit forced fallback')
            report['fallback'] = {**fallback, 'seconds': round(time.monotonic()-started, 2)}
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait(timeout=10)
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        for record in report['seeds']:
            record['after'] = SMOKE.sha256(Path(record['path']))
            if record['before'] != record['after']:
                report['status'] = 'failed'
        (lab / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
