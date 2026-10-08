"""Find Tapo plugs on the local network.

Three probes, because none of them works everywhere:

  1. mDNS  -- plugs advertise ``_tapo._tcp.local.``; blocked by AP isolation
              and by some hotspots.
  2. UDP broadcast handshake -- the protocol the Tapo app itself uses; blocked
              by the same things mDNS is.
  3. TCP port sweep -- plugs always listen on 80 (older fw) or 443 (newer).
              Works whenever plain IP connectivity works, but says nothing
              about *which* device answered.

Run with:  python -m tapo_scheduler.discover
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Any

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from .config import Config
from .net import LocalNetwork, local_network

SERVICE_TYPE = "_tapo._tcp.local."
CANDIDATE_PORTS = (80, 443)


@dataclass
class Discovery:
    ip: str
    model: str
    nickname: str
    device_type: str
    device_id: str = ""
    error: str | None = None

    def __str__(self) -> str:
        line = f"{self.ip:<15} {self.model:<10} {self.device_type:<24} {self.nickname}"
        if self.error:
            line += f"  [unclassified: {self.error}]"
        return line


def _fmt_props(properties: dict[bytes, bytes] | None) -> dict[str, str]:
    return {
        k.decode(errors="replace"): (v or b"").decode(errors="replace")
        for k, v in (properties or {}).items()
    }


class _MdnsListener(ServiceListener):
    def __init__(self) -> None:
        self.found: list[tuple[str, str, int, dict[str, str]]] = []

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=3000)
        if not info:
            return
        for addr in info.parsed_scoped_addresses():
            self.found.append((name, addr, info.port, _fmt_props(info.properties)))

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def probe_mdns(timeout_s: float) -> list[tuple[str, str, int, dict[str, str]]]:
    zc = Zeroconf()
    listener = _MdnsListener()
    try:
        ServiceBrowser(zc, SERVICE_TYPE, listener)
        time.sleep(timeout_s)
    finally:
        zc.close()
    return listener.found


async def probe_broadcast(
    broadcast: str, timeout_s: float, email: str = "", password: str = ""
) -> list[Discovery]:
    """Ask the tapo library to do its own UDP broadcast discovery.

    The handshake is authenticated, so real credentials are required — with
    empty ones every device comes back as `Unauthorized { HASH_MISMATCH }`.

    Raises RuntimeError if the probe itself cannot run.
    """
    from tapo import ApiClient

    try:
        # timeout_s must be a whole number of seconds.
        client = ApiClient(email, password)
        results = await client.discover_devices(broadcast, int(timeout_s))
    except Exception as exc:  # noqa: BLE001 - probe must never kill the run
        raise RuntimeError(f"broadcast probe failed: {exc!r}") from exc

    return [_to_discovery(item) for item in results or []]


def _read(obj: Any, name: str) -> str:
    """Read a field that may be exposed as a method or as an attribute.

    The rust side declares the accessors twice over: `DiscoveryResultExt` types
    them as methods, while the concrete variants carry `ip` as a plain
    attribute. Either shape shows up depending on the result.
    """
    value = getattr(obj, name, None)
    if value is None:
        return "?"
    try:
        return str(value() if callable(value) else value)
    except Exception as exc:  # noqa: BLE001 - accessor blew up
        return f"?({exc!r})"


def _to_discovery(item: Any) -> Discovery:
    """Normalise a raw discovery result.

    The rust side hands back `MaybeDiscoveryResult`, whose `get()` raises for
    devices the library cannot classify.
    """
    if not hasattr(item, "device_info") and hasattr(item, "get"):
        try:
            item = item.get()
        except Exception as exc:  # noqa: BLE001 - unclassifiable device
            return Discovery(
                ip="?", model="?", nickname="?", device_type="?", error=str(exc)
            )

    discovery = Discovery(
        ip=_read(item, "ip"),
        model=_read(item, "model"),
        nickname=_read(item, "nickname"),
        device_type=_read(item, "device_type"),
        device_id=_read(item, "device_id"),
    )
    if "?" in (discovery.ip, discovery.model):
        discovery.error = f"unreadable fields on {type(item).__name__}"
    return discovery


async def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def probe_ports(net: LocalNetwork, timeout: float) -> list[tuple[str, list[int]]]:
    hosts = [str(h) for h in net.hosts()]

    async def check(host: str) -> tuple[str, list[int]]:
        results = await asyncio.gather(
            *(_port_open(host, port, timeout) for port in CANDIDATE_PORTS)
        )
        return host, [p for p, ok in zip(CANDIDATE_PORTS, results) if ok]

    found = await asyncio.gather(*(check(h) for h in hosts))
    return [(h, ports) for h, ports in sorted(found) if ports]


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=5.0, help="seconds per probe")
    parser.add_argument("--skip-mdns", action="store_true")
    parser.add_argument("--skip-broadcast", action="store_true")
    parser.add_argument("--skip-ports", action="store_true")
    args = parser.parse_args(argv)

    try:
        net = local_network()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"interface : {net.interface}")
    print(f"address   : {net.address}/{net.netmask}  (network {net.network})")
    print(f"broadcast : {net.broadcast}")
    print(f"hosts     : {len(net.hosts())}")
    print()

    mdns_found: list[tuple[str, str, int, dict[str, str]]] = []
    broadcast_found: list[Discovery] = []
    port_found: list[tuple[str, list[int]]] = []

    if not args.skip_mdns:
        print(f"[1/3] mDNS {SERVICE_TYPE} ...")
        mdns_found = probe_mdns(args.timeout)
        for name, addr, port, props in mdns_found:
            print(f"  {addr}:{port}  {name}")
            for key, value in sorted(props.items()):
                print(f"      {key} = {value}")
        if not mdns_found:
            print("  (nothing)")
    print()

    if not args.skip_broadcast:
        print(f"[2/3] UDP broadcast to {net.broadcast} ...")
        try:
            config = Config.load()
        except RuntimeError as exc:
            print(f"  skipped: {exc}")
        else:
            try:
                broadcast_found = await probe_broadcast(
                    str(net.broadcast), args.timeout, config.email, config.password
                )
                for item in broadcast_found:
                    print(f"  {item}")
                if not broadcast_found:
                    print("  (nothing)")
            except RuntimeError as exc:
                print(f"  {exc}")
    print()

    if not args.skip_ports:
        print(f"[3/3] TCP sweep {net.network} ports {CANDIDATE_PORTS} ...")
        port_found = await probe_ports(net, timeout=min(args.timeout, 1.0))
        for host, ports in port_found:
            print(f"  {host}  open: {ports}")
        if not port_found:
            print("  (nothing)")
    print()

    identified = [d for d in broadcast_found if d.error is None]
    rejected = [d for d in broadcast_found if d.error is not None]
    open_ips = {ip for ip, _ in port_found}
    named_ips = {d.ip for d in identified} | {d.ip for d in rejected}

    print("summary")
    print(f"  identified plugs : {len(identified)}")
    for item in identified:
        print(f"    {item.ip:<15} {item.model:<10} {item.nickname}  [{item.device_type}]")
    if rejected:
        # Something answered the Tapo handshake but we could not classify it.
        print(f"  answered, failed : {len(rejected)}")
        for item in rejected:
            print(f"    {item.ip}")
    # Ports answered but the tapo handshake did not: not a Tapo device
    # (router, NAS, printer), or a plug unreachable over UDP.
    unidentified = sorted(open_ips - named_ips)
    if unidentified:
        print(f"  port only        : {', '.join(unidentified)}")
    missing = sorted(named_ips - open_ips)
    if missing:
        print(f"  named, no port   : {', '.join(missing)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
