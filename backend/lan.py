"""LAN-access helpers: which addresses other devices on the local network can use
to reach this machine, and whether the backend has been told to listen for them.

By default the backend binds 127.0.0.1 and is unreachable from anywhere else. LAN
mode is opt-in (`OASIS_LAN=1`, `oasis-portal.sh --lan`, or a non-loopback `HOST`)
and changes nothing about *who is trusted*: a request that arrives over the
network is never local (see auth.is_local_request), so every LAN device still
needs the access code from settings -> remote access.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket


def _truthy(value: str | None) -> bool:
    return (value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def bind_host() -> str:
    """The interface the backend should listen on."""
    host = os.environ.get('HOST', '').strip()
    if host:
        return host
    return '0.0.0.0' if _truthy(os.environ.get('OASIS_LAN')) else '127.0.0.1'


def is_loopback_host(host: str) -> bool:
    host = (host or '').strip().strip('[]').lower()
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def lan_enabled() -> bool:
    """True when the backend is (meant to be) reachable from other devices."""
    return _truthy(os.environ.get('OASIS_LAN')) or not is_loopback_host(bind_host())


def lan_addresses() -> list[str]:
    """This machine's private IPv4 addresses, best guess first.

    The UDP "connect" trick finds the interface the OS would route out of without
    sending anything; getaddrinfo on the hostname adds the rest (Wi-Fi + Ethernet).
    Loopback, link-local and public addresses are dropped -- only an address a
    phone on the same router could actually dial is useful.
    """
    found: list[str] = []

    def add(ip: str) -> None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return
        if addr.version == 4 and addr.is_private and not addr.is_loopback \
                and not addr.is_link_local and ip not in found:
            found.append(ip)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('10.255.255.255', 1))
            add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return found


def lan_origin_regex(extra_hosts: list[str] | None = None) -> str | None:
    """CORS regex admitting a page served from *this machine's own* LAN addresses.

    Deliberately not "any private IP": that would let any web page hosted on any
    device in the house call the owner's backend with the owner's local trust.
    Only origins whose host is one of this machine's addresses (the web frontend
    the portal serves on :3000) qualify. None when there is nothing to allow.
    """
    hosts = list(lan_addresses()) + list(extra_hosts or [])
    try:
        name = socket.gethostname().strip().lower()
        if name:
            hosts.append(name)
            if not name.endswith('.local'):
                hosts.append(f'{name}.local')
    except OSError:
        pass
    hosts = sorted({h for h in hosts if h})
    if not hosts:
        return None
    alternation = '|'.join(re.escape(h) for h in hosts)
    return rf'^http://({alternation})(:\d{{1,5}})?$'


def describe_urls(port: int | str, frontend_port: int | str | None = 3000) -> list[tuple[str, str]]:
    """(label, url) pairs worth printing for a phone user."""
    out: list[tuple[str, str]] = []
    for ip in lan_addresses():
        out.append(('後端 API', f'http://{ip}:{port}'))
        if frontend_port:
            out.append(('網頁介面', f'http://{ip}:{frontend_port}'))
    return out
