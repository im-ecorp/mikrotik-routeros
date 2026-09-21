"""Focused serial regressions; pipes exercise the real parser, no guest required."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

SPEC = importlib.util.spec_from_file_location('chr_smoke_serial', Path(__file__).with_name('chr-smoke.py'))
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class SerialValueTests(unittest.TestCase):
    def test_command_echo_is_not_an_empty_guest_value(self):
        vm = SMOKE.VM.__new__(SMOKE.VM)
        vm.buffer = ''
        vm.capture = False
        vm.transcript = ''
        vm.timeout = .03
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, 'rb', buffering=0) as output:
            vm.proc = SimpleNamespace(stdout=output)
            vm.send = lambda command: os.write(write_fd, (command + '\r\n[admin@MikroTik] > ').encode())
            try:
                with self.assertRaisesRegex(RuntimeError, 'Serial timeout'):
                    vm.value('EMPTY', '/system identity get name')
            finally:
                os.close(write_fd)

    def test_empty_guest_value_is_not_a_serial_timeout(self):
        vm = SMOKE.VM.__new__(SMOKE.VM)
        vm.buffer = ''
        vm.capture = False
        vm.transcript = ''
        vm.timeout = .03
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, 'rb', buffering=0) as output:
            vm.proc = SimpleNamespace(stdout=output)
            def send(command):
                os.write(write_fd, (command + '\r\nEMPTY=\r\n[admin@MikroTik] > ').encode())
            vm.send = send
            try:
                self.assertEqual(vm.value('EMPTY', '/ip dhcp-client get [find interface=ether1] status'), '')
            finally:
                os.close(write_fd)


if __name__ == '__main__':
    unittest.main()
