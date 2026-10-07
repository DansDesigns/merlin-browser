"""Connections made as Chrome makes them.

Sites that look for robots (Google's "unusual traffic" page, Cloudflare's
"Just a moment...") tell a browser from a program first by how it connects,
before any page is asked for: the TLS handshake (its ciphers, extensions and
their order, the GREASE values Chrome puts in) and HTTP/2's settings and header
order. Merlin Engine's connections had been Python's (OpenSSL, HTTP/1.1), known
for a program at once, whatever its headers said.

curl_cffi (libcurl-impersonate: C, BoringSSL as Chrome has it, no Rust) makes
them as Chrome does. This opener uses it behind urllib's own shape, .open(request,
timeout), so nothing that fetches changes: cookies are kept in Merlin's jar,
redirects are followed one step at a time so the cookies each sets are kept,
and failures come back as urllib's errors, so retries and error pages behave as
before. Without curl_cffi, or with a proxy in use, urllib is used as it was.
"""
from __future__ import annotations

import email.message
import io
import os
import queue
import urllib.error
import urllib.parse
import urllib.request

try:                                                       # optional: urllib otherwise
    from curl_cffi import requests as _curl
    AVAILABLE = True
except Exception:                                          # noqa: BLE001
    _curl = None
    AVAILABLE = False

IMPERSONATE = "chrome"                  # the newest Chrome curl_cffi knows
DOH_URL = "https://cloudflare-dns.com/dns-query"
MAX_REDIRECTS = 10
enabled = True                          # set from the "chrome_connections" setting

# headers curl_cffi gives as Chrome does: Merlin's own would contradict them,
# and a contradiction (a user agent not matching the handshake) is a robot's mark
_OWN = {"user-agent", "accept-encoding", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
        "host", "content-length", "connection"}


def usable() -> bool:
    """Whether connections can be made as Chrome's now."""
    if not (AVAILABLE and enabled):
        return False
    proxied = any(os.environ.get(name) for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                                                   "ALL_PROXY", "all_proxy"))
    return not proxied


class _Response(io.BytesIO):
    """A finished answer in the shape urllib gives one."""

    def __init__(self, body: bytes, url: str, status: int, headers: list):
        super().__init__(body)
        self.url = url
        self.status = self.code = status
        self.reason = ""
        message = email.message.Message()
        for name, value in headers:
            # curl has unpacked the body already: said again, it would be twice
            if name.lower() in ("content-encoding", "transfer-encoding", "content-length"):
                continue
            message[name] = value
        message["Content-Length"] = str(len(body))
        self.headers = self.msg = message

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status

    def info(self):
        return self.headers

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# Sessions are kept and lent out, so their connections are kept too: a page's
# stylesheets, scripts and images reuse the site's connection (HTTP/2, many
# requests on one), as Chrome does. A session a request had made and closed
# again cost a whole TLS handshake for every image on a page.
_POOL: "queue.LifoQueue" = queue.LifoQueue()
POOL_SIZE = 8


def _borrow():
    try:
        return _POOL.get_nowait()
    except queue.Empty:
        # one connection for the session, whichever thread borrows it: curl_cffi
        # keeps one a thread by default, and Merlin's image threads each live
        # for one picture. A session is lent to one thread at a time.
        try:
            return _curl.Session(impersonate=IMPERSONATE, use_thread_local_curl=False)
        except TypeError:                                  # an older curl_cffi
            return _curl.Session(impersonate=IMPERSONATE)


def _give_back(session, broken: bool = False) -> None:
    if broken or _POOL.qsize() >= POOL_SIZE:
        try:
            session.close()
        except Exception:                                  # noqa: BLE001
            pass
        return
    _POOL.put(session)


class ChromeOpener:
    """urllib's opener, its connections made as Chrome makes them."""

    def __init__(self, jar=None, lenient: str = "", secure_dns: bool | None = None):
        self.jar = jar
        self.lenient = lenient
        self.secure_dns = secure_dns

    def _doh(self, host: str):
        try:
            from .. import securedns
        except Exception:                                  # noqa: BLE001
            return None
        wanted = securedns._enabled if self.secure_dns is None else self.secure_dns
        return DOH_URL if wanted and not securedns._local(host) else None

    def open(self, request, data=None, timeout: float = 30):
        if isinstance(request, str):
            request = urllib.request.Request(request, data=data)
        method = request.get_method()
        body = request.data
        url = request.full_url
        headers = {k: v for k, v in request.header_items() if k.lower() not in _OWN}
        identity = identity_headers()            # this computer's system, not a Mac's
        session = _borrow()
        broken = False
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                ask = urllib.request.Request(url, data=body, method=method, headers=headers)
                if self.jar is not None:
                    self.jar.add_cookie_header(ask)
                sent = {k: v for k, v in ask.header_items() if k.lower() not in _OWN}
                sent.update(identity)
                sent.update(_not_a_navigation(sent))
                host = urllib.parse.urlsplit(url).hostname or ""
                try:
                    answer = session.request(method, url, data=body, headers=sent, timeout=timeout,
                                             allow_redirects=False,
                                             verify=False if self.lenient else _trust_bundle(),
                                             doh_url=self._doh(host), discard_cookies=True)
                except Exception as exc:                   # noqa: BLE001
                    broken = True                          # its connection may be in any state
                    reason = _reason(exc)
                    if "CERTIFICATE_VERIFY_FAILED" in reason:
                        reason = _diagnosis(url) or reason
                    raise urllib.error.URLError(reason) from exc
                pairs = list(answer.headers.multi_items()) if hasattr(answer.headers, "multi_items") \
                    else list(answer.headers.items())
                response = _Response(answer.content or b"", url, answer.status_code, pairs)
                if self.jar is not None:
                    self.jar.extract_cookies(response, ask)
                location = response.headers.get("Location")
                if answer.status_code in (301, 302, 303, 307, 308) and location:
                    url = urllib.parse.urljoin(url, location)
                    if answer.status_code in (301, 302, 303) and method not in ("GET", "HEAD"):
                        method, body = "GET", None
                        headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
                    continue
                if answer.status_code >= 400:
                    raise urllib.error.HTTPError(url, answer.status_code, answer.reason or "",
                                                 response.headers, io.BytesIO(answer.content or b""))
                return response
            raise urllib.error.URLError("too many redirects")
        finally:
            _give_back(session, broken)


def _not_a_navigation(sent: dict) -> dict:
    """Headers curl_cffi adds as for a page the user opened, taken off a
    request that is not one: Chrome sends Sec-Fetch-User only for a page the
    user asked for, and Upgrade-Insecure-Requests only for a navigation. An
    image or a frame's page sent with them is something Chrome never sends,
    which sites that look for robots look for. (None takes off a header curl
    would otherwise add.)"""
    found = {k.lower(): v for k, v in sent.items()}
    removed = {}
    dest = found.get("sec-fetch-dest", "document").lower()
    mode = found.get("sec-fetch-mode", "navigate").lower()
    if dest != "document" and "sec-fetch-user" not in found:
        removed["Sec-Fetch-User"] = None
    if mode != "navigate" and "upgrade-insecure-requests" not in found:
        removed["Upgrade-Insecure-Requests"] = None
    return removed


# Chrome's own words for the system it runs on, as its user agent gives them
# (frozen by Chrome: every Linux is "X11; Linux x86_64", every Windows 10 or
# 11 "Windows NT 10.0; Win64; x64"), and as its sec-ch-ua-platform does.
PLATFORMS = {
    "windows": ("Windows NT 10.0; Win64; x64", "Windows"),
    "linux": ("X11; Linux x86_64", "Linux"),
    "mac": ("Macintosh; Intel Mac OS X 10_15_7", "macOS"),
}


def this_platform() -> tuple:
    """(user agent's system part, sec-ch-ua-platform's name) for this computer.

    curl_cffi's Chrome is a Mac's: sent as it is, a Windows or Linux computer
    said it was a Mac, which nothing else about it agreed with (its fonts, its
    screen, what Chromium's own tabs said)."""
    import sys

    if sys.platform.startswith("win"):
        return PLATFORMS["windows"]
    if sys.platform == "darwin":
        return PLATFORMS["mac"]
    return PLATFORMS["linux"]


_LEARNT: dict = {}


def _learn() -> dict:
    """What curl_cffi's Chrome sends (its user agent and brands), learnt once
    by asking a listener here what arrived."""
    if _LEARNT or not usable():
        return _LEARNT
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    got = []

    def answer():
        try:
            connection, _ = listener.accept()
            connection.settimeout(3)
            data = connection.recv(65536).decode("latin-1")
            got.append(data)
            connection.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            connection.close()
        except Exception:                                  # noqa: BLE001
            pass
    worker = threading.Thread(target=answer, daemon=True)
    worker.start()
    try:
        session = _curl.Session(impersonate=IMPERSONATE)
        session.get(f"http://127.0.0.1:{listener.getsockname()[1]}/", timeout=3)
        session.close()
    except Exception:                                      # noqa: BLE001
        pass
    worker.join(3)
    listener.close()
    for line in (got[0] if got else "").split("\r\n"):
        name, _, value = line.partition(":")
        if name.lower() in ("user-agent", "sec-ch-ua"):
            _LEARNT[name.lower()] = value.strip()
    return _LEARNT


def user_agent() -> str:
    """The user agent these connections send, this computer's system in it:
    curl_cffi's Chrome version (which its handshake is) with this computer's
    system, as Chrome itself would say it. The page's scripts are told the
    same: a header saying one browser and navigator.userAgent another is a
    robot's mark."""
    import re

    agent = _learn().get("user-agent", "")
    if not agent:
        return ""
    system, _name = this_platform()
    return re.sub(r"\([^)]*\)", f"({system})", agent, count=1)


def brands() -> list:
    """The brands sec-ch-ua sends, in its order, for navigator.userAgentData."""
    import re

    return [{"brand": name, "version": version}
            for name, version in re.findall(r'"([^"]*)";v="([^"]*)"', _learn().get("sec-ch-ua", ""))]


def identity_headers() -> dict:
    """The headers saying which browser on which system, as this computer's."""
    agent = user_agent()
    if not agent:
        return {}
    return {"User-Agent": agent, "sec-ch-ua-platform": f'"{this_platform()[1]}"'}


_BUNDLES: dict = {}


def _trust_bundle():
    """The authorities Python trusts, as a file for curl: the system's own
    store (Windows' certificate store there), and SSL_CERT_FILE. curl_cffi had
    trusted only its own list, so an authority added to the system (a router's,
    an organisation's) was trusted by Merlin before and would not have been."""
    import hashlib
    import ssl
    import tempfile

    key = os.environ.get("SSL_CERT_FILE", "") + "|" + os.environ.get("SSL_CERT_DIR", "")
    found = _BUNDLES.get(key)
    if found and os.path.exists(found):
        return found
    try:
        certificates = ssl.create_default_context().get_ca_certs(binary_form=True)
    except Exception:                                      # noqa: BLE001
        certificates = []
    if not certificates:
        return True                                        # curl's own list, then
    text = "".join(ssl.DER_cert_to_PEM_cert(certificate) for certificate in certificates)
    path = os.path.join(tempfile.gettempdir(),
                        f"merlin-trust-{hashlib.sha256(text.encode()).hexdigest()[:16]}.pem")
    if not os.path.exists(path):
        with open(path, "w", encoding="ascii") as handle:
            handle.write(text)
    _BUNDLES[key] = path
    return path


def _diagnosis(url: str) -> str:
    """What is wrong with a site's certificate, in Python's own words: curl's
    TLS names only the first fault it meets (a test certificate both self-signed
    and not yet valid is "self signed" to it), where Merlin offers a dates-only
    allowance for a clock behind. One handshake more, only when one failed."""
    import socket
    import ssl

    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return ""
    try:
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=5) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=parts.hostname):
                return ""
    except ssl.SSLCertVerificationError as error:
        problem = error.verify_message or str(error)
        return f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: {problem} (diagnosed)"
    except Exception:                                      # noqa: BLE001
        return ""


def _reason(exc) -> str:
    """A failure in the words Merlin's retry looks for (refused, timed out...)."""
    text = str(exc)
    lowered = text.lower()
    if "could not resolve" in lowered:
        return f"getaddrinfo failed: {text}"
    if "timed out" in lowered or "timeout" in lowered:
        return f"timed out: {text}"
    if "connection refused" in lowered or "failed to connect" in lowered:
        return f"connection refused: {text}"
    if "certificate" in lowered:
        # in Python's own words, as Merlin reads a certificate's problem (a
        # clock behind, "not yet valid", is offered a dates-only allowance):
        # curl names the problems as OpenSSL does, so only the frame differs
        import re

        found = re.search(r"(?:SSL certificate problem:|verify result:)\s*([^\n(]+)", text, re.I)
        problem = (found.group(1) if found else "unable to verify the certificate").strip().rstrip(".")
        return f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: {problem} (curl)"
    return text
