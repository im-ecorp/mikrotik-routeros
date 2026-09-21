"""Runtime protocol tests; real local sockets, no Docker privileges."""
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]

def load_runtime():
    path = ROOT / 'bin/runtime.py'
    if not path.exists():
        raise AssertionError('Runtime QMP/health implementation missing')
    spec = importlib.util.spec_from_file_location('runtime', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def load_integration():
    spec = importlib.util.spec_from_file_location('integration', ROOT / 'tests/docker-integration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SerialCompletionTests(unittest.TestCase):
    def test_command_completion_rejects_stale_prompt_and_echo(self):
        import os
        import re
        from types import SimpleNamespace
        module = load_integration()
        console = module.Serial.__new__(module.Serial)
        console.buffer = '[admin@MikroTik] > '
        console.timeout = .03
        console.capture = False
        console.transcript = ''
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, 'rb', buffering=0) as output:
            console.proc = SimpleNamespace(stdout=output)
            sent = []
            def send(command):
                sent.append(command)
                os.write(write_fd, ('[admin@MikroTik] > ' + command + '\r\n').encode())
            console.send = send
            try:
                with self.assertRaisesRegex(RuntimeError, 'Serial timeout'):
                    console.command_done('/ip dhcp-client enable [find interface=ether1]')
                # A real output line for this invocation, not its echo, completes it.
                def acknowledge(command):
                    tag = re.search(r':put "([A-Za-z0-9_]+)"', command).group(1)
                    os.write(write_fd, (tag + '\r\n[admin@MikroTik] > ').encode())
                console.send = acknowledge
                console.command_done('/ip dhcp-client enable [find interface=ether1]')
            finally:
                os.close(write_fd)


class RecoveryDiagnosticsTests(unittest.TestCase):
    def test_dhcp_diagnostic_preserves_documented_transitional_status(self):
        from unittest.mock import Mock
        module = load_integration()
        lab = module.Lab.__new__(module.Lab)
        lab.report = {}
        for status in ('searching...', 'requesting...', 'rebinding...', 'SECRET'):
            console = Mock(timeout=150)
            console.value.side_effect = ['false', status]
            record = lab.dhcp_diagnostics(console, 'reenabled')
            self.assertEqual(record['status'], status if status != 'SECRET' else 'unknown')
            self.assertNotIn('SECRET', json.dumps(record))
            self.assertEqual(console.timeout, 150)

    def test_dhcp_recovery_records_both_sides_before_logout(self):
        from unittest.mock import Mock, MagicMock
        module = load_integration()
        for recovered in (True, False):
            with self.subTest(recovered=recovered), tempfile.TemporaryDirectory() as directory:
                lab = module.Lab('test', Path(directory) / 'evidence')
                lab.network = 'test-net'
                lab.inspect = Mock(return_value={'NetworkSettings': {'Networks': {
                    lab.network: {'IPAddress': '172.18.0.2'}}}})
                console = Mock()
                # Disable snapshot, enable snapshot, final recovery snapshot.
                console.value.side_effect = ['true', 'stopped', 'false', 'searching',
                    'false', 'bound' if recovered else 'searching'] + (['172.18.0.2/16'] if recovered else [])
                lab.console = MagicMock()
                lab.console.return_value.__enter__.return_value = console
                def health(expected):
                    self.assertFalse(console.send.called)
                    if expected == 'healthy' and not recovered:
                        raise RuntimeError('Timed out: Docker health healthy')
                lab.health = Mock(side_effect=health)
                if recovered:
                    lab.address_health_transition()
                    self.assertTrue(lab.report['checks'][-1]['arp_health_tracks_guest_address'])
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                        lab.address_health_transition()
                samples = lab.report['dhcp_diagnostics']
                self.assertEqual([s['phase'] for s in samples], ['disabled', 'reenabled', 'recovery'])
                self.assertEqual(samples[1]['disabled'], False)
                self.assertEqual(samples[2]['status'], 'bound' if recovered else 'searching')
                console.send.assert_called_once_with('/quit')

    def test_health_timeout_keeps_allowlisted_state_and_probe_results(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        module = load_integration()
        with tempfile.TemporaryDirectory() as directory:
            lab = module.Lab('test', Path(directory) / 'evidence')
            lab.wait = Mock(side_effect=RuntimeError('Timed out: Docker health healthy'))
            lab.inspect = Mock(return_value={'State': {'Running': True, 'ExitCode': 0,
                'Health': {'Status': 'unhealthy', 'FailingStreak': 3,
                           'Log': [{'ExitCode': 1, 'Output': 'SECRET'}]}},
                'Config': {'Env': ['SECRET']}})
            lab.docker = Mock(side_effect=[
                SimpleNamespace(returncode=0, stdout='{"status":"running","secret":"SECRET"}'),
                SimpleNamespace(returncode=1, stdout='SECRET')])
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                lab.health('healthy')
            record = lab.report['health_diagnostics'][-1]
            self.assertEqual(record['expected'], 'healthy')
            self.assertEqual(record['qmp_status'], 'running')
            self.assertEqual(record['probe_exit_code'], 1)
            self.assertEqual(record['health_exit_codes'], [1])
            self.assertNotIn('SECRET', json.dumps(record))


class RuntimeTests(unittest.TestCase):
    def test_qmp_ignores_async_events_and_correlates_reply(self):
        runtime = load_runtime()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'qmp.sock')
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(1)
            errors = []
            def peer():
                try:
                    conn, _ = server.accept()
                    with conn, conn.makefile('rwb') as stream:
                        stream.write(b'{"QMP":{"version":{}}}\r\n'); stream.flush()
                        for expected in ('qmp_capabilities', 'query-status'):
                            request = json.loads(stream.readline())
                            self.assertEqual(request['execute'], expected)
                            stream.write(b'{"event":"RESET"}\r\n')
                            stream.write((json.dumps({'return': {'status': 'running'}, 'id': request['id']})+'\r\n').encode());stream.flush()
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=peer);thread.start()
            try:
                with runtime.QMP(path, timeout=2) as qmp:
                    self.assertEqual(qmp.execute('query-status')['status'], 'running')
            finally:
                thread.join(3);server.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_health_requires_fresh_guest_arp_and_running_qemu(self):
        runtime = load_runtime()
        mac = bytes.fromhex('020000000001')
        guest = bytes.fromhex('020000000002')
        packet = runtime.arp_probe(mac, '172.25.0.2')
        self.assertEqual(packet[:6], b'\xff' * 6)
        self.assertEqual(packet[28:32], b'\x00' * 4)
        # An actual ARP reply must identify the leased guest, not another host.
        import struct
        reply = mac + guest + b'\x08\x06' + struct.pack('!HHBBH', 1, 0x800, 6, 4, 2) + guest + socket.inet_aton('172.25.0.2') + mac + b'\x00' * 4
        self.assertTrue(runtime.is_guest_reply(reply, mac, '172.25.0.2'))
        self.assertFalse(runtime.is_guest_reply(reply, mac, '172.25.0.3'))
        self.assertFalse(runtime.is_guest_reply(packet, mac, '172.25.0.2'))
        self.assertFalse(runtime.is_guest_reply(b'truncated', mac, '172.25.0.2'))

    def test_network_rejects_host_topology_before_changes(self):
        runtime = load_runtime()
        links = [{'ifname': 'lo'}, {'ifname': 'eth0', 'linkinfo': {'info_kind': 'ether'}}]
        with self.assertRaisesRegex(ValueError, 'veth'):
            runtime.network_plan(links, [], [], None)

    def test_internal_bridge_requires_explicit_gateway(self):
        runtime = load_runtime()
        links = [{'ifname': 'lo'}, {'ifname': 'eth0', 'link_index': 9, 'ifindex': 2, 'linkinfo': {'info_kind': 'veth'}}]
        addrs = [{'ifname':'eth0','addr_info':[{'family':'inet','local':'172.28.0.2','prefixlen':24}]}]
        with self.assertRaisesRegex(ValueError, 'gateway'):
            runtime.network_plan(links, addrs, [], None)
        plan = runtime.network_plan(links, addrs, [], '172.28.0.1')
        self.assertEqual(plan['guest_ip'], '172.28.0.2')
        self.assertEqual(plan['gateway'], '172.28.0.1')

    def test_health_rejects_stopping_without_contacting_guest(self):
        runtime = load_runtime()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'stopping').touch()
            self.assertFalse(runtime.health(root))

    def test_network_rejects_ambiguous_routes_and_ipam_gateway(self):
        runtime = load_runtime()
        links = [{'ifname': 'eth0', 'linkinfo': {'info_kind': 'veth'}}]
        addrs = [{'ifname':'eth0','addr_info':[{'family':'inet','local':'172.28.0.2','prefixlen':24}]}]
        for gateway in ('172.29.0.1', '172.28.0.2', '172.28.0.0', '172.28.0.255'):
            with self.subTest(gateway=gateway), self.assertRaises(ValueError):
                runtime.network_plan(links, addrs, [], gateway)
        with self.assertRaises(ValueError):
            runtime.network_plan(links, addrs, [{'dst':'default','dev':'other','gateway':'172.28.0.1'}], None)
        with self.assertRaises(ValueError):
            runtime.network_plan(links + [{'ifname':'eth1'}], addrs, [], '172.28.0.1')

    def test_shutdown_requires_guest_shutdown_event_not_powerdown(self):
        runtime = load_runtime()
        self.assertFalse(runtime.guest_shutdown([{'event':'POWERDOWN'}]))
        self.assertFalse(runtime.guest_shutdown([{'event':'SHUTDOWN','data':{'guest':False,'reason':'host-qmp-quit'}}]))
        self.assertTrue(runtime.guest_shutdown([{'event':'SHUTDOWN','data':{'guest':True,'reason':'guest-shutdown'}}]))

if __name__ == '__main__':
    unittest.main()
