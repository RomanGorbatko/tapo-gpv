"""Local network helpers: figure out which subnet we are on."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalNetwork:
    interface: str
    address: ipaddress.IPv4Address
    netmask: ipaddress.IPv4Address

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.address}/{self.netmask}", strict=False)

    @property
    def broadcast(self) -> ipaddress.IPv4Address:
        return self.network.broadcast_address

    def hosts(self) -> list[ipaddress.IPv4Address]:
        """Every usable host on the subnet, ourselves excluded."""
        return [h for h in self.network.hosts() if h != self.address]


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout


def _default_interface() -> str:
    """Interface carrying the default route, e.g. 'en0'."""
    match = re.search(r"^\s*interface:\s*(\S+)", _run("route", "-n", "get", "default"), re.MULTILINE)
    if not match:
        raise RuntimeError("no default route; connect to Wi-Fi first")
    return match.group(1)


def local_network() -> LocalNetwork:
    interface = _default_interface()
    address = _run("ipconfig", "getifaddr", interface).strip()
    netmask = _run("ipconfig", "getoption", interface, "subnet_mask").strip()
    if not address or not netmask:
        raise RuntimeError(f"could not read address/netmask for {interface}")
    return LocalNetwork(
        interface=interface,
        address=ipaddress.IPv4Address(address),
        netmask=ipaddress.IPv4Address(netmask),
    )


def local_ip_towards(host: str = "8.8.8.8", port: int = 53) -> str:
    """Local IP the kernel would use to reach `host`. No packets are sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, port))
        return sock.getsockname()[0]
