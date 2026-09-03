"""Adblock Plus / EasyList style filter engine + QtWebEngine interceptor.

Supports the practical subset of the syntax:
    ||domain.tld^path      domain-anchored blocking
    |https://exact         start anchor
    /regex/                raw regular expression rules
    @@...                  exception (allow) rules
    $script,third-party,domain=a.com|~b.com     options
    example.com##.selector cosmetic (element hiding) rules
    #@#                    cosmetic exceptions

Matching uses a token index: every rule is filed under its longest literal
token, and only rules whose token appears in the URL are regex-tested.
"""
from __future__ import annotations

import os
import re
import threading
import time
import urllib.request
from collections import defaultdict
from typing import Iterable

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtWebEngineCore import QWebEngineUrlRequestInfo, QWebEngineUrlRequestInterceptor

from . import settings as cfg

# ---------------------------------------------------------------- resource types
RT = QWebEngineUrlRequestInfo.ResourceType

RESOURCE_TYPE_NAMES = {
    RT.ResourceTypeMainFrame: "document",
    RT.ResourceTypeSubFrame: "subdocument",
    RT.ResourceTypeStylesheet: "stylesheet",
    RT.ResourceTypeScript: "script",
    RT.ResourceTypeImage: "image",
    RT.ResourceTypeFontResource: "font",
    RT.ResourceTypeSubResource: "other",
    RT.ResourceTypeObject: "object",
    RT.ResourceTypeMedia: "media",
    RT.ResourceTypeWorker: "script",
    RT.ResourceTypeSharedWorker: "script",
    RT.ResourceTypePrefetch: "other",
    RT.ResourceTypeFavicon: "image",
    RT.ResourceTypeXhr: "xmlhttprequest",
    RT.ResourceTypePing: "ping",
    RT.ResourceTypeServiceWorker: "script",
    RT.ResourceTypeCspReport: "other",
    RT.ResourceTypePluginResource: "object",
    RT.ResourceTypeUnknown: "other",
}

KNOWN_TYPES = {
    "document", "subdocument", "stylesheet", "script", "image", "font",
    "media", "object", "xmlhttprequest", "ping", "websocket", "other",
}

TOKEN_RE = re.compile(r"[a-zA-Z0-9%]{3,}")
_NEVER_MATCHES = re.compile(r"(?!)")
PLAIN_HOST_RE = re.compile(r"^[a-z0-9.-]+$", re.IGNORECASE)
_BAD_TOKENS = {"http", "https", "www", "com", "net", "org", "html"}

# A tiny built-in list so the browser blocks something on first run, before any
# list has been downloaded.
BUILTIN_RULES = """
||doubleclick.net^
||googlesyndication.com^
||googleadservices.com^
||google-analytics.com^
||googletagmanager.com^
||googletagservices.com^
||adservice.google.com^
||scorecardresearch.com^
||quantserve.com^
||criteo.com^
||criteo.net^
||taboola.com^
||outbrain.com^
||adnxs.com^
||rubiconproject.com^
||pubmatic.com^
||openx.net^
||casalemedia.com^
||moatads.com^
||adsrvr.org^
||amazon-adsystem.com^
||facebook.net^$third-party
||connect.facebook.net^
||hotjar.com^
||mixpanel.com^
||segment.io^
||branch.io^
||bugsnag.com^
||newrelic.com^
||nr-data.net^
||sentry.io^$third-party
||chartbeat.com^
||parsely.com^
||onesignal.com^
||pushcrew.com^
||zqtk.net^
||adform.net^
||teads.tv^
||smartadserver.com^
||sharethis.com^
||addthis.com^
||yieldmo.com^
||media.net^$third-party
||advertising.com^
||serving-sys.com^
||2mdn.net^
##.adsbygoogle
##div[id^="google_ads_iframe"]
##iframe[src*="doubleclick.net"]
""".strip()


def _anchor_regex(pattern: str) -> str:
    """Translate an Adblock pattern into a Python regex string."""
    out = []
    i = 0
    n = len(pattern)
    if pattern.startswith("||"):
        out.append(r"^[a-z][a-z0-9+.-]*://(?:[^/?#]*\.)?")
        i = 2
    elif pattern.startswith("|"):
        out.append("^")
        i = 1
    while i < n:
        ch = pattern[i]
        if ch == "*":
            out.append(".*")
        elif ch == "^":
            out.append(r"(?:[^\w.%-]|$)")
        elif ch == "|" and i == n - 1:
            out.append("$")
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _best_token(pattern: str) -> str:
    candidates = [
        t.lower() for t in TOKEN_RE.findall(pattern) if t.lower() not in _BAD_TOKENS
    ]
    if not candidates:
        return ""
    return max(candidates, key=len)


class Rule:
    """One filter.

    The regex is compiled on first use, not at load time. A full EasyList plus
    EasyPrivacy plus Fanboy set is around 150,000 rules, and compiling every
    one of them cost about 34 seconds of blocked start-up. The overwhelming
    majority never match anything in a given session, so they are never
    compiled at all now.
    """

    __slots__ = (
        "_regex", "pattern", "is_raw_regex", "exception", "types",
        "excluded_types", "third_party", "domains", "excluded_domains",
        "token", "host", "raw",
    )

    def __init__(self, raw: str):
        self.raw = raw
        self.exception = False
        self.types: set[str] = set()
        self.excluded_types: set[str] = set()
        self.third_party = None      # True / False / None
        self.domains: set[str] = set()
        self.excluded_domains: set[str] = set()
        self._regex = None
        self.pattern = ""
        self.is_raw_regex = False
        self.token = ""
        self.host = ""               # set for plain ||domain^ rules

    @property
    def regex(self):
        if self._regex is None:
            try:
                source = (self.pattern if self.is_raw_regex
                          else _anchor_regex(self.pattern))
                self._regex = re.compile(source, re.IGNORECASE)
            except re.error:
                self._regex = _NEVER_MATCHES
        return self._regex

    # ------------------------------------------------------------------
    def options_allow(self, rtype: str, is_third_party: bool,
                      first_party_host: str) -> bool:
        if self.types and rtype not in self.types:
            return False
        if rtype in self.excluded_types:
            return False
        if self.third_party is not None and self.third_party != is_third_party:
            return False
        if self.domains or self.excluded_domains:
            if self.excluded_domains and _host_in(first_party_host,
                                                  self.excluded_domains):
                return False
            if self.domains and not _host_in(first_party_host, self.domains):
                return False
        return True

    def matches(self, url: str, rtype: str, is_third_party: bool,
                first_party_host: str) -> bool:
        if self.types and rtype not in self.types:
            return False
        if rtype in self.excluded_types:
            return False
        if self.third_party is not None and self.third_party != is_third_party:
            return False
        if self.domains or self.excluded_domains:
            host = first_party_host
            if self.excluded_domains and _host_in(host, self.excluded_domains):
                return False
            if self.domains and not _host_in(host, self.domains):
                return False
        return bool(self.regex.search(url))


def _host_in(host: str, domains: Iterable[str]) -> bool:
    host = (host or "").lower()
    for d in domains:
        if host == d or host.endswith("." + d):
            return True
    return False


def parse_filter(line: str) -> Rule | None:
    line = line.strip()
    if not line or line[0] in "!#[" and not line.startswith("##"):
        # '#' alone starts a comment in some lists; '##' is cosmetic (handled
        # by the caller) so it never reaches here.
        if not line.startswith("##"):
            return None
    if line.startswith("##") or "##" in line or "#@#" in line or "#?#" in line:
        return None

    rule = Rule(line)
    body = line
    if body.startswith("@@"):
        rule.exception = True
        body = body[2:]

    # options
    if "$" in body:
        head, _, opts = body.rpartition("$")
        # a '$' inside a regex rule is not an option separator
        if not (head.startswith("/") and head.endswith("/")):
            body = head
            for opt in opts.split(","):
                opt = opt.strip().lower()
                if not opt:
                    continue
                negate = opt.startswith("~")
                name = opt.lstrip("~")
                if name.startswith("domain="):
                    for d in name[7:].split("|"):
                        d = d.strip().lower()
                        if not d:
                            continue
                        if d.startswith("~"):
                            rule.excluded_domains.add(d[1:])
                        else:
                            rule.domains.add(d)
                elif name == "third-party":
                    rule.third_party = not negate
                elif name in ("first-party", "~third-party"):
                    rule.third_party = False
                elif name in KNOWN_TYPES:
                    (rule.excluded_types if negate else rule.types).add(name)
                elif name in ("xhr", "fetch"):
                    (rule.excluded_types if negate else rule.types).add("xmlhttprequest")
                elif name in ("popup", "elemhide", "generichide", "genericblock",
                              "csp", "redirect", "important", "match-case",
                              "badfilter", "removeparam", "empty", "mp4",
                              "inline-script", "inline-font", "all", "webrtc",
                              "cname", "denyallow", "method", "to", "from",
                              "header", "strict1p", "strict3p", "ipaddress"):
                    if name == "badfilter":
                        return None       # not supported; drop the rule
                    continue
                else:
                    continue

    if not body:
        return None

    if body.startswith("/") and body.endswith("/") and len(body) > 2:
        rule.pattern = body[1:-1]
        rule.is_raw_regex = True
        rule.token = ""
    else:
        rule.pattern = body
        rule.token = _best_token(body)
        # ||example.com^ with nothing else in it needs no regex at all: it is
        # a hostname suffix test, which is a dict lookup.
        if body.startswith("||") and body.endswith("^"):
            middle = body[2:-1]
            if PLAIN_HOST_RE.match(middle) and ".." not in middle:
                rule.host = middle.lower()
    return rule


class FilterEngine:
    """Holds compiled rules and answers should_block()."""

    def __init__(self):
        self.block_index: dict[str, list[Rule]] = defaultdict(list)
        self.block_generic: list[Rule] = []
        self.block_hosts: dict[str, list[Rule]] = defaultdict(list)
        self.allow_index: dict[str, list[Rule]] = defaultdict(list)
        self.allow_generic: list[Rule] = []
        self.allow_hosts: dict[str, list[Rule]] = defaultdict(list)
        self.cosmetic_generic: list[str] = []
        self.cosmetic_specific: dict[str, list[str]] = defaultdict(list)
        self.cosmetic_exceptions: dict[str, set[str]] = defaultdict(set)
        self.rule_count = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------- loading
    def add_rule(self, rule: Rule) -> None:
        if rule.host:
            hosts = self.allow_hosts if rule.exception else self.block_hosts
            hosts[rule.host].append(rule)
            self.rule_count += 1
            return
        target_index = self.allow_index if rule.exception else self.block_index
        target_generic = self.allow_generic if rule.exception else self.block_generic
        if rule.token:
            target_index[rule.token].append(rule)
        else:
            target_generic.append(rule)
        self.rule_count += 1

    def add_cosmetic(self, line: str) -> None:
        for sep, exception in (("#@#", True), ("##", False)):
            if sep in line:
                domains, _, selector = line.partition(sep)
                selector = selector.strip()
                if not selector or selector.startswith("+js") or ":" in selector[:1]:
                    return
                # skip procedural cosmetic filters we cannot evaluate
                if any(p in selector for p in (":has-text", ":xpath", ":matches-css",
                                               ":upward", ":remove(", ":style(")):
                    return
                domains = domains.strip()
                if exception:
                    for d in filter(None, domains.split(",")):
                        self.cosmetic_exceptions[d.strip().lower()].add(selector)
                elif domains:
                    for d in filter(None, domains.split(",")):
                        d = d.strip().lower()
                        if d.startswith("~"):
                            continue
                        self.cosmetic_specific[d].append(selector)
                else:
                    self.cosmetic_generic.append(selector)
                return

    def load_text(self, text: str) -> int:
        added = 0
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("!") or line.startswith("[Adblock"):
                continue
            if "##" in line or "#@#" in line:
                self.add_cosmetic(line)
                added += 1
                continue
            if "#?#" in line or "#$#" in line or "#%#" in line:
                continue
            rule = parse_filter(line)
            if rule is not None:
                self.add_rule(rule)
                added += 1
        return added

    def clear(self) -> None:
        self.__init__()

    # ------------------------------------------------------------ matching
    def _candidates(self, url: str, index, generic):
        seen_tokens = set(t.lower() for t in TOKEN_RE.findall(url))
        for token in seen_tokens:
            bucket = index.get(token)
            if bucket:
                yield from bucket
        yield from generic

    @staticmethod
    def _host_suffixes(host: str):
        """example.cdn.co.uk -> itself, then cdn.co.uk, then co.uk, then uk."""
        host = (host or "").lower()
        while host:
            yield host
            dot = host.find(".")
            if dot < 0:
                return
            host = host[dot + 1:]

    def _host_match(self, hosts: dict, host: str, rtype: str,
                    is_third_party: bool, first_party_host: str) -> bool:
        if not hosts:
            return False
        for suffix in self._host_suffixes(host):
            bucket = hosts.get(suffix)
            if not bucket:
                continue
            for rule in bucket:
                if rule.options_allow(rtype, is_third_party, first_party_host):
                    return True
        return False

    def should_block(self, url: str, rtype: str, is_third_party: bool,
                     first_party_host: str, host: str = "") -> bool:
        if not host:
            host = _host_of(url)

        blocked = self._host_match(self.block_hosts, host, rtype,
                                   is_third_party, first_party_host)
        if not blocked:
            for rule in self._candidates(url, self.block_index,
                                         self.block_generic):
                if rule.matches(url, rtype, is_third_party, first_party_host):
                    blocked = True
                    break
        if not blocked:
            return False

        if self._host_match(self.allow_hosts, host, rtype, is_third_party,
                            first_party_host):
            return False
        for allow in self._candidates(url, self.allow_index,
                                      self.allow_generic):
            if allow.matches(url, rtype, is_third_party, first_party_host):
                return False
        return True

    def cosmetic_css(self, host: str, include_generic: bool = True) -> str:
        host = (host or "").lower()
        selectors: list[str] = []
        excluded: set[str] = set()
        parts = host.split(".")
        for i in range(len(parts)):
            suffix = ".".join(parts[i:])
            selectors.extend(self.cosmetic_specific.get(suffix, ()))
            excluded |= self.cosmetic_exceptions.get(suffix, set())
        if include_generic:
            # cap generic selectors: huge stylesheets slow every page load
            selectors.extend(self.cosmetic_generic[:4000])
        selectors = [s for s in dict.fromkeys(selectors) if s not in excluded]
        if not selectors:
            return ""
        return ",\n".join(selectors) + "{display:none !important;}"


class FilterLoader(QObject):
    """Loads cached lists from disk and refreshes them over the network.

    Loading happens on a worker thread and produces a brand new FilterEngine,
    which is then swapped in on the UI thread. Parsing 150,000 rules takes a
    couple of seconds; doing it before the window is shown made start-up look
    like a hang, and mutating the live engine in place would race with the
    interceptor reading it.
    """

    loaded = pyqtSignal(int)
    status = pyqtSignal(str)
    engine_ready = pyqtSignal(object)

    def __init__(self, engine: FilterEngine, settings: cfg.Settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings

    def _cache_path(self, url: str) -> str:
        name = re.sub(r"[^a-zA-Z0-9._-]", "_", url)[-120:]
        return os.path.join(cfg.FILTER_DIR, name)

    def build_engine(self) -> "FilterEngine":
        """Parse every cached list into a fresh engine. Safe off-thread."""
        engine = FilterEngine()
        engine.load_text(BUILTIN_RULES)
        for url in self.settings.get("filter_lists", []):
            path = self._cache_path(url)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        engine.load_text(fh.read())
                except OSError:
                    pass
        return engine

    def load_cached(self) -> None:
        """Synchronous load, kept for tests and the command line."""
        engine = self.build_engine()
        self.engine = engine
        self.engine_ready.emit(engine)
        self.loaded.emit(engine.rule_count)

    def load_cached_async(self) -> None:
        threading.Thread(target=self._load_cached, daemon=True).start()

    def _load_cached(self) -> None:
        try:
            engine = self.build_engine()
        except Exception:                                # noqa: BLE001
            return
        self.engine = engine
        self.engine_ready.emit(engine)
        self.loaded.emit(engine.rule_count)

    def cache_age_hours(self) -> float:
        """Age of the freshest cached list, or a large number if there is none."""
        newest = 0.0
        for url in self.settings.get("filter_lists", []):
            path = self._cache_path(url)
            if os.path.exists(path):
                newest = max(newest, os.path.getmtime(path))
        if not newest:
            return 1e6
        return (time.time() - newest) / 3600.0

    def refresh_if_stale(self, max_age_hours: float = 12.0) -> None:
        """Only go to the network when the cache has actually aged out."""
        if self.cache_age_hours() < max_age_hours:
            return
        self.refresh_async()

    def refresh_async(self) -> None:
        thread = threading.Thread(target=self._refresh, daemon=True)
        thread.start()

    def _refresh(self) -> None:
        cfg.ensure_dirs()
        ok = 0
        for url in self.settings.get("filter_lists", []):
            try:
                self.status.emit(f"Downloading {url}")
                req = urllib.request.Request(
                    url, headers={"User-Agent": "MerlinBrowser/1.1 (+filters)"}
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read().decode("utf-8", "replace")
                if len(data) > 200:
                    with open(self._cache_path(url), "w", encoding="utf-8") as fh:
                        fh.write(data)
                    ok += 1
            except Exception as exc:                 # noqa: BLE001 - network is best effort
                self.status.emit(f"Failed {url}: {exc}")
        if ok:
            self.status.emit(f"Updated {ok} filter list(s)")
            self._load_cached()
        else:
            # nothing came down, so the cache is unchanged and re-parsing a
            # quarter of a million rules would burn CPU for no reason
            self.status.emit("Filter lists unchanged")


class RequestInterceptor(QWebEngineUrlRequestInterceptor):
    """Blocks ads/trackers, upgrades to HTTPS, sets privacy headers."""

    blocked = pyqtSignal(str, str)   # first-party host, blocked url

    def __init__(self, engine: FilterEngine, settings: cfg.Settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self.counts: dict[str, int] = defaultdict(int)

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:  # noqa: N802
        url = info.requestUrl()
        scheme = url.scheme()
        if scheme in ("data", "blob", "about", "qrc", "file", "merlin"):
            return

        first_party = info.firstPartyUrl()
        fp_host = first_party.host() or url.host()

        if self.settings.get("send_do_not_track"):
            info.setHttpHeader(b"DNT", b"1")
            info.setHttpHeader(b"Sec-GPC", b"1")

        # HTTPS upgrade for top-level navigations
        if (self.settings.get("https_upgrade") and scheme == "http"
                and info.resourceType() == RT.ResourceTypeMainFrame
                and not _is_local(url)):
            upgraded = QUrl(url)
            upgraded.setScheme("https")
            info.redirect(upgraded)
            return

        if not self.settings.shields_enabled_for(fp_host):
            return

        # Never block a top-level document. Blockers exist to stop what a page
        # pulls in, not to stop you going somewhere.
        #
        # This mattered on redirects: firstPartyUrl is still the previous page
        # while the new one is being fetched, so following a redirect to
        # another domain looked like a third-party request, and any
        # $third-party rule that happened to match could take out the whole
        # page. The site simply failed to load with no explanation.
        if info.resourceType() == RT.ResourceTypeMainFrame:
            return

        rtype = RESOURCE_TYPE_NAMES.get(info.resourceType(), "other")
        url_str = url.toString()
        host = url.host()
        is_third_party = bool(fp_host) and not _same_site(host, fp_host)

        try:
            if self.engine.should_block(url_str, rtype, is_third_party,
                                        fp_host, host):
                info.block(True)
                self.counts[fp_host] = self.counts.get(fp_host, 0) + 1
                self.blocked.emit(fp_host, url_str)
        except Exception:                              # never break navigation
            return

    def count_for(self, host: str) -> int:
        return self.counts.get(host, 0)

    def reset_count(self, host: str) -> None:
        self.counts[host] = 0


def _host_of(url: str) -> str:
    start = url.find("://")
    if start < 0:
        return ""
    start += 3
    end = len(url)
    for ch in ("/", "?", "#"):
        pos = url.find(ch, start)
        if pos != -1:
            end = min(end, pos)
    host = url[start:end]
    at = host.rfind("@")
    if at != -1:
        host = host[at + 1:]
    colon = host.rfind(":")
    if colon != -1 and "]" not in host[colon:]:
        host = host[:colon]
    return host.lower()


def _is_local(url: QUrl) -> bool:
    host = url.host()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")


def _registrable(host: str) -> str:
    """Cheap eTLD+1 approximation (no PSL dependency)."""
    parts = (host or "").lower().split(".")
    if len(parts) < 3:
        return ".".join(parts)
    two = ".".join(parts[-2:])
    common = {"co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "com.au", "co.nz",
              "com.br", "co.in", "com.cn", "co.za", "com.mx", "co.kr"}
    if two in common and len(parts) >= 3:
        return ".".join(parts[-3:])
    return two


def _same_site(a: str, b: str) -> bool:
    return _registrable(a) == _registrable(b)
