#!/usr/bin/env python3

import argparse
import ipaddress
import json
import socket
import subprocess
import sys

from typing import List, Iterable

DEFAULT_ROUTE = 'default'
DEFAULT_DNS_IPS = ('8.8.8.8', '8.8.4.4')

DHCP_CONF_TEMPLATE = """
start {host_addr}
end   {host_addr}
# avoid dhcpd complaining that we have
# too many addresses
maxleases 1
interface {dhcp_intf}
option dns      {dns}
option router   {gateway}
option subnet   {subnet}
option hostname {hostname}
"""

def run_ip_command(args: list) -> list:
    """Runs an `ip -json` command and returns parsed JSON."""
    cmd = ['ip', '-json'] + args
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Command '{' '.join(cmd)}' failed (exit {proc.returncode}): {stderr.decode().strip()}"
            )
        return json.loads(stdout)

def default_route(routes: list) -> dict:
    """Returns the host's default route entry."""
    for route in routes:
        if route.get('dst') == DEFAULT_ROUTE:
            return route
    raise ValueError('No default route found in routing table.')

def addr_of(addrs: list, dev: str) -> ipaddress.IPv4Interface:
    """Finds and returns the IPv4 address of the given interface."""
    for addr in addrs:
        if addr.get('ifname') != dev:
            continue
        for info in addr.get('addr_info', []):
            if info.get('family') == 'inet':
                return ipaddress.IPv4Interface((info['local'], info['prefixlen']))
    raise ValueError(f"Interface '{dev}' not found or has no IPv4 address.")

def generate_conf(intf_name: str, dns: Iterable[str]) -> str:
    """Generates a udhcpd config for the given bridge interface."""
    routes = run_ip_command(['route'])
    addrs = run_ip_command(['addr'])

    droute = default_route(routes)
    host_addr = addr_of(addrs, droute['dev'])

    gateway = droute.get('gateway')
    if not gateway:
        raise ValueError('Default route has no gateway.')

    return DHCP_CONF_TEMPLATE.format(
        dhcp_intf=intf_name,
        dns=' '.join(dns),
        gateway=gateway,
        host_addr=host_addr.ip,
        hostname=socket.gethostname(),
        subnet=host_addr.network.netmask,
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Generate a udhcpd configuration for the QEMU bridge interface.'
    )
    parser.add_argument('intf_name', help='Bridge interface name (e.g. qemubr0)')
    parser.add_argument('dns_ips', nargs='*', help='Optional DNS server IPs')
    args = parser.parse_args()

    dns_ips = args.dns_ips if args.dns_ips else DEFAULT_DNS_IPS

    try:
        print(generate_conf(args.intf_name, dns_ips))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
