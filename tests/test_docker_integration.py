"""Pure protocol checks for the opt-in Docker integration harness."""
import importlib.util
import json
from pathlib import Path
import socket
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


def harness():
    path = Path(__file__).with_name('docker-integration.py')
    if not path.exists():
        raise AssertionError('Docker integration harness missing')
    spec = importlib.util.spec_from_file_location('integration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IntegrationProtocolTests(unittest.TestCase):
    def test_dns_checks_transaction_and_actual_record(self):
        module = harness()
        query = module.dns_query(1234, 'chr-integration.invalid')
        self.assertEqual(struct.unpack('!6H', query[:12]), (1234, 256, 1, 0, 0, 0))
        response = struct.pack('!6H', 1234, 0x8180, 1, 1, 0, 0) + query[12:] + b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + socket.inet_aton('192.0.2.42')
        self.assertTrue(module.dns_answer(response, 1234, '192.0.2.42'))
        self.assertFalse(module.dns_answer(response, 4321, '192.0.2.42'))
        self.assertFalse(module.dns_answer(response, 1234, '192.0.2.43'))
        self.assertFalse(module.dns_answer(query, 1234, '192.0.2.42'))
        self.assertFalse(module.dns_answer(b'bad', 1234, '192.0.2.42'))

class IntegrationNetworkTests(unittest.TestCase):
    def test_normal_bridge_installs_scoped_guards_before_container(self):
        module = harness()
        with tempfile.TemporaryDirectory() as tmp:
            lab = module.Lab('integration:test', Path(tmp) / 'evidence')
            net = {'Id': 'abc123', 'Internal': False, 'EnableIPv6': False, 'Driver': 'bridge',
                   'Options': {'com.docker.network.bridge.name': lab.name[-12:],
                               'com.docker.network.bridge.enable_ip_masquerade': 'false'},
                   'IPAM': {'Config': [{'Gateway': '172.18.0.1', 'Subnet': '172.18.0.0/16'}]}}
            lab.docker = Mock(return_value=SimpleNamespace(stdout=json.dumps([net]), returncode=0))
            lab.firewall = Mock(return_value=SimpleNamespace(stdout='', returncode=0))
            lab.setup_network()
            create_args = lab.docker.call_args_list[0].args
            self.assertNotIn('--internal', create_args)
            self.assertIn('com.docker.network.bridge.enable_ip_masquerade=false', create_args)
            self.assertFalse(lab.report['network']['internal'])
            self.assertEqual(len(lab.firewall_rules), 6)
            for family, chain, rule in lab.firewall_rules:
                self.assertIn(lab.bridge, rule)
                self.assertIn(lab.name, rule)
                self.assertEqual(rule[-2:], ['-j', 'DROP'])
            lab.container_created = True
            lab.docker = Mock(return_value=SimpleNamespace(returncode=0))
            deleted = set()
            def firewall(family, operation, chain, *rule, **kwargs):
                key = (family, chain, rule)
                if operation == '-D':
                    deleted.add(key)
                return SimpleNamespace(returncode=int(operation == '-C' and key in deleted))
            lab.firewall.side_effect = firewall
            lab.cleanup()
            self.assertEqual(lab.report['cleanup_errors'], [])
            deletes = [call.args for call in lab.firewall.call_args_list if call.args[1] == '-D']
            self.assertEqual(len(deletes), 6)
            self.assertTrue(all(call.args[1] not in ('-F', '-X', '-P') for call in lab.firewall.call_args_list))

    def test_cleanup_retains_guards_when_container_removal_fails(self):
        module = harness()
        with tempfile.TemporaryDirectory() as tmp:
            lab = module.Lab('integration:test', Path(tmp) / 'evidence')
            lab.container_created = True
            lab.firewall_rules = [('iptables', 'DOCKER-USER', ['-i', lab.bridge, '-j', 'DROP'])]
            lab.docker = Mock(return_value=SimpleNamespace(returncode=1))
            lab.firewall = Mock()
            lab.cleanup()
            lab.firewall.assert_not_called()
            self.assertEqual(lab.report['status'], 'failed')
            self.assertTrue(lab.root.exists())

    def test_counter_selects_owned_ingress_rule_only(self):
        module = harness()
        with tempfile.TemporaryDirectory() as tmp:
            lab = module.Lab('integration:test', Path(tmp) / 'evidence')
            lab.firewall = Mock(return_value=SimpleNamespace(stdout=(
                f'3 252 DROP all -- {lab.bridge} * 0.0.0.0/0 0.0.0.0/0 /* {lab.name} */\n'
                f'0 0 DROP all -- * {lab.bridge} 0.0.0.0/0 0.0.0.0/0 /* {lab.name} */\n'
                '90 9000 DROP all -- other * 0.0.0.0/0 0.0.0.0/0 /* unrelated */\n')))
            self.assertEqual(lab.drop_count('DOCKER-USER'), 3)

    def test_isolation_requires_counted_drop_not_merely_ping_timeout(self):
        module = harness()
        for counters, accepted in [([0, 3, 0, 3], True), ([0, 0], False)]:
            with self.subTest(counters=counters), tempfile.TemporaryDirectory() as tmp:
                lab = module.Lab('integration:test', Path(tmp) / 'evidence')
                lab.gateway = '172.18.0.1'
                console = Mock()
                console.value.return_value = '0'
                from unittest.mock import MagicMock
                lab.console = MagicMock()
                lab.console.return_value.__enter__.return_value = console
                lab.drop_count = Mock(side_effect=counters)
                if accepted:
                    lab.verify_isolation()
                    self.assertTrue(lab.report['checks'][-1]['external_egress_blocked'])
                else:
                    with self.assertRaisesRegex(RuntimeError, 'counted firewall drop'):
                        lab.verify_isolation()

    def test_missing_published_ports_is_explicit_failure_with_diagnostics(self):
        module = harness()
        with tempfile.TemporaryDirectory() as tmp:
            lab = module.Lab('integration:test', Path(tmp) / 'evidence')
            lab.inspect = Mock(return_value={
                'NetworkSettings': {'Ports': {}, 'Networks': {}},
                'HostConfig': {'PortBindings': {'80/tcp': [{'HostIp': '127.0.0.1', 'HostPort': ''}]}},
                'Config': {'Env': ['PASSWORD=never-report-this']}})
            with self.assertRaisesRegex(RuntimeError, 'Missing published port: 80/tcp'):
                lab.protocols()
            self.assertEqual(lab.report['network_diagnostics']['Ports'], {})
            self.assertIn('80/tcp', lab.report['network_diagnostics']['PortBindings'])
            self.assertNotIn('never-report-this', json.dumps(lab.report))


class IntegrationSeedTests(unittest.TestCase):
    def test_runtime_workflow_qualifies_dockerfile_default_seed(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    '.github/workflows/runtime-integration.yml').read_text()
        self.assertIn('docker build ', workflow)
        self.assertNotIn('ROUTEROS_VERSION=', workflow)

    def test_fresh_guest_must_match_nonempty_image_seed_label(self):
        module = harness()

        class ProtocolsReached(AssertionError):
            pass

        cases = [
            ({'io.mikrotik-routeros.seed.version': '7.21.4'}, '7.21.4 (stable)', True),
            ({'io.mikrotik-routeros.seed.version': '7.22.2'}, '7.22.2 (stable)', True),
            ({'io.mikrotik-routeros.seed.version': '7.21.4'}, '7.22.2 (stable)', False),
            ({'io.mikrotik-routeros.seed.version': '7.21.4'}, '7.21.40 (stable)', False),
            ({'io.mikrotik-routeros.seed.version': '7.21.4'}, '', False),
            ({'io.mikrotik-routeros.seed.version': ''}, '7.21.4 (stable)', False),
            ({'io.mikrotik-routeros.seed.version': '   '}, '7.21.4 (stable)', False),
            ({}, '7.21.4 (stable)', False),
            (None, '7.21.4 (stable)', False),
        ]
        for labels, version, accepted in cases:
            with self.subTest(labels=labels, version=version), tempfile.TemporaryDirectory() as tmp:
                lab = module.Lab('integration:test', Path(tmp) / 'evidence')

                def docker(*args, **kwargs):
                    if args[:2] == ('image', 'inspect'):
                        result = [{'Id': 'sha256:test', 'Config': {'Labels': labels}}]
                    elif args[:2] == ('network', 'inspect'):
                        result = [{'Internal': True, 'Driver': 'bridge',
                                   'IPAM': {'Config': [{'Gateway': '192.0.2.1'}]}}]
                    else:
                        result = []
                    return SimpleNamespace(stdout=json.dumps(result), returncode=0)

                lab.docker = Mock(side_effect=docker)
                lab.setup_network = Mock()
                lab.create = Mock()
                lab.configure = Mock(return_value=version)
                lab.protocols = Mock(side_effect=ProtocolsReached)
                if accepted:
                    with self.assertRaises(ProtocolsReached):
                        lab.run()
                    lab.configure.assert_called_once_with(True)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'seed'):
                        lab.run()
                    lab.protocols.assert_not_called()


if __name__ == '__main__':
    unittest.main()
