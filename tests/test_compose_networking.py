"""Validate rendered Compose networking without starting a Docker daemon.

Use COMPOSE_BINARY=/path/to/docker-compose for a standalone Compose binary.
Set REQUIRE_COMPOSE=1 to turn missing Compose into an error (as CI does).
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANAGEMENT_PORTS = {21: 21, 2222: 22, 23: 23, 80: 80, 443: 443,
                    8291: 8291, 8728: 8728, 8729: 8729}


class ComposeNetworkingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        explicit = os.environ.get("COMPOSE_BINARY")
        candidates = [[explicit]] if explicit else [
            ["docker", "compose"], ["docker-compose"]
        ]
        for command in candidates:
            if not shutil.which(command[0]):
                continue
            result = subprocess.run(command + ["version"], capture_output=True,
                                    text=True, timeout=30)
            if result.returncode == 0:
                cls.compose = command
                return
        message = "Docker Compose is required; install it or set COMPOSE_BINARY"
        if explicit or os.environ.get("REQUIRE_COMPOSE") == "1":
            raise RuntimeError(message)
        raise unittest.SkipTest(message)

    def render(self, bind_ip=None, env_file=None):
        # Do not inherit a developer's project, override files, or local .env.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("COMPOSE_") and key not in
               {"MANAGEMENT_BIND_IP", "ROUTEROS_VERSION", "TZ"}}
        if bind_ip is not None:
            env["MANAGEMENT_BIND_IP"] = bind_ip
        result = subprocess.run(
            self.compose + ["--project-name", "networking-tests", "--env-file",
                            str(env_file or os.devnull), "-f",
                            str(ROOT / "docker-compose.yml"), "config",
                            "--format", "json"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def ports(config):
        return {(int(port["published"]), port["target"], port["protocol"]):
                port.get("host_ip", "0.0.0.0")
                for port in config["services"]["routers"]["ports"]}

    def test_default_image_pins_runtime_capable_wrapper_release(self):
        self.assertEqual(self.render()["services"]["routers"]["image"],
                         "hossein3piol/mikrotik-routeros:7.21.4-r1.0.0")

    def test_management_bind_ip_is_scoped_and_defaults_to_all_ipv4(self):
        for bind_ip in (None, "", "127.0.0.1", "192.0.2.10"):
            with self.subTest(bind_ip=bind_ip):
                ports = self.ports(self.render(bind_ip))
                for published, target in MANAGEMENT_PORTS.items():
                    self.assertEqual(ports[published, target, "tcp"],
                                     bind_ip or "0.0.0.0")
                for (published, target, protocol), host_ip in ports.items():
                    if published not in MANAGEMENT_PORTS:
                        self.assertEqual(host_ip, "0.0.0.0")

    def test_example_image_pins_runtime_capable_wrapper_release(self):
        example = ROOT / ".env.example"
        self.assertIn("ROUTEROS_VERSION=7.21.4-r1.0.0", example.read_text().splitlines())
        self.assertEqual(self.render(env_file=example)["services"]["routers"]["image"],
                         "hossein3piol/mikrotik-routeros:7.21.4-r1.0.0")

    def test_example_explicitly_preserves_public_management_default(self):
        example = ROOT / ".env.example"
        self.assertIn("MANAGEMENT_BIND_IP=0.0.0.0", example.read_text().splitlines())
        ports = self.ports(self.render(env_file=example))
        for published, target in MANAGEMENT_PORTS.items():
            self.assertEqual(ports[published, target, "tcp"], "0.0.0.0")

    def test_legacy_custom_ports_and_pptp_control_are_preserved(self):
        ports = self.ports(self.render("127.0.0.1"))
        for port in (1450, 1723, *range(9000, 9101)):
            with self.subTest(port=port):
                self.assertEqual(ports[port, port, "tcp"], "0.0.0.0")
        expected = {(published, target, "tcp")
                    for published, target in MANAGEMENT_PORTS.items()}
        expected.update((port, port, "tcp")
                        for port in (1194, 1450, 1723, *range(9000, 9101)))
        expected.update((port, port, "udp") for port in (1194, 1701, 13231))
        self.assertEqual(set(ports), expected)
        self.assertEqual(len(ports), len(self.render()["services"]["routers"]["ports"]))

    def test_existing_runtime_contract_is_unchanged(self):
        config = self.render()
        self.assertEqual(set(config["services"]), {"routers"})
        router = config["services"]["routers"]
        self.assertTrue(router["privileged"])
        self.assertEqual(router["healthcheck"], {
            "test": ["CMD", "python3", "/routeros/bin/runtime.py", "health"],
            "interval": "30s", "timeout": "10s", "start_period": "2m0s", "retries": 3,
        })
        self.assertEqual(router['stop_grace_period'], '1m5s')

    def test_openvpn_preserves_tcp_and_adds_udp(self):
        ports = self.ports(self.render())
        protocols = {protocol for published, target, protocol in ports
                     if published == target == 1194}
        self.assertEqual(protocols, {"tcp", "udp"})

    def test_wireguard_and_l2tp_are_udp_only(self):
        ports = self.ports(self.render())
        for port in (13231, 1701):
            with self.subTest(port=port):
                self.assertIn((port, port, "udp"), ports)
                self.assertNotIn((port, port, "tcp"), ports)


if __name__ == "__main__":
    unittest.main()
