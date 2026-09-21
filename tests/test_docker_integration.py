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
