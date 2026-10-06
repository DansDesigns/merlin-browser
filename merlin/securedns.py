"""Secure DNS: site names looked up over HTTPS, as Brave does by default.

Windows' own DNS, or a router's, can hold an old address for a site after its
records change, or be read and altered on the way. Merlin Engine's connections
ask a DNS-over-HTTPS service instead: Cloudflare, then Google, reached by
address (1.1.1.1, 8.8.8.8), so asking needs no DNS of its own. Certificates are
still checked against the site's own name. Names on the local network
(localhost, addresses, names with no dot, .local) go to the system as before,
and so does everything if neither service can be reached.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request

# (address to ask, the JSON form of the answer): by address, so no lookup first
ENDPOINTS = ["https://1.1.1.1/dns-query", "https://8.8.8.8/resolve"]
TIMEOUT = 3.0
_original_getaddrinfo = socket.getaddrinfo
_cache: dict = {}                 # name -> (good until, [addresses])
_lock = threading.Lock()
_enabled = False


def _local(host: str) -> bool:
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".local", ".localhost", ".lan", ".internal", ".home.arpa")):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return "." not in host


def _ask(endpoint: str, host: str, kind: str):
    """One question to one service: the addresses, and how long they hold."""
    url = f"{endpoint}?name={urllib.parse.quote(host)}&type={kind}"
    request = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
        answer = json.loads(response.read(256 * 1024))
    wanted = 1 if kind == "A" else 28
    found = [(a.get("data", ""), int(a.get("TTL", 300))) for a in answer.get("Answer", []) or []
             if a.get("type") == wanted]
    return [ip for ip, _ttl in found], min([ttl for _ip, ttl in found] or [300])


def resolve(host: str) -> list:
    """The addresses of host from the secure services, IPv4 first; [] if
    they could not be asked or had none (the system is used then)."""
    host = host.lower().rstrip(".")
    now = time.monotonic()
    with _lock:
        known = _cache.get(host)
    if known and known[0] > now:
        return known[1]
    for endpoint in ENDPOINTS:
        try:
            v4, ttl4 = _ask(endpoint, host, "A")
            try:
                v6, ttl6 = _ask(endpoint, host, "AAAA")
            except Exception:                              # noqa: BLE001
                v6, ttl6 = [], ttl4
        except Exception:                                  # noqa: BLE001
            continue
        addresses = [ip for ip in v4 + v6 if ip]
        if addresses:
            hold = max(60, min(ttl4, ttl6 if v6 else ttl4, 3600))
            with _lock:
                _cache[host] = (now + hold, addresses)
            return addresses
    return []


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if isinstance(host, str) and host.lower().rstrip(".") in ("localhost", "localhost.localdomain"):
        # IPv4 first for localhost: Windows gives ::1 first, and a server
        # listening on 127.0.0.1 alone (as Python's do, Ponder's among them)
        # was reached only after the IPv6 attempt failed, two seconds a request
        found = _original_getaddrinfo(host, port, family, type, proto, flags)
        return sorted(found, key=lambda entry: entry[0] != socket.AF_INET)
    if not _enabled or not isinstance(host, str) or _local(host):
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    addresses = resolve(host)
    if not addresses:
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    results = []
    for ip in addresses:
        is_v6 = ":" in ip
        kind = socket.AF_INET6 if is_v6 else socket.AF_INET
        if family not in (0, socket.AF_UNSPEC, kind):
            continue
        try:
            number = int(port) if port is not None else 0
        except (TypeError, ValueError):
            number = socket.getservbyname(str(port))
        where = (ip, number, 0, 0) if is_v6 else (ip, number)
        for sock_type in ([type] if type else [socket.SOCK_STREAM]):
            results.append((kind, sock_type, proto or socket.IPPROTO_TCP, "", where))
    return results or _original_getaddrinfo(host, port, family, type, proto, flags)


def install(enabled: bool = True) -> None:
    """Merlin's own connections look names up securely from now on (or not)."""
    global _enabled
    _enabled = bool(enabled)
    socket.getaddrinfo = _getaddrinfo


def chromium_secure_dns(enabled: bool = True) -> bool:
    """Chromium tabs too, where Qt WebEngine can be told (Qt 6.7 and later)."""
    try:
        from PyQt6.QtWebEngineCore import QWebEngineGlobalSettings as settings

        mode = settings.DnsMode()
        mode.secureMode = (settings.SecureDnsMode.SecureWithFallback if enabled
                           else settings.SecureDnsMode.SystemOnly)
        if enabled:
            mode.serverTemplates = ["https://cloudflare-dns.com/dns-query"]
        return bool(settings.setDnsMode(mode))
    except Exception:                                      # noqa: BLE001
        return False
