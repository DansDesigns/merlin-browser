"""
Ponder web search — parallel fast backends, sequential fallbacks.
Fast tier (parallel): DDG vqd, Qwant, Mojeek, Yahoo  → first winner returned
Slow tier (sequential fallback): SearXNG public, Marginalia
"""
import json as _json, logging, asyncio, re, httpx
from datetime import datetime
from urllib.parse import unquote, quote
from bs4 import BeautifulSoup
from config import Config, CONFIG_DIR

log = logging.getLogger("ponder.web")
TIMEOUT = 8
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}
DDG_COOKIES = {"5": "a", "kl": "en-gb"}
PUBLIC_SEARX = ["https://search.sapti.me","https://etsi.me","https://priv.au",
                "https://searx.be","https://paulgo.io"]


def _parse_date(raw: str) -> str:
    """Normalise various date strings to YYYY-MM-DD; return '' on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    from datetime import datetime as _dt
    formats = [
        "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
        "%d %b %Y", "%b %d, %Y", "%B %d, %Y",
        "%d/%m/%Y", "%m/%d/%Y",
        "%d %B %Y",
    ]
    for fmt in formats:
        try:
            return _dt.strptime(raw[:len(fmt)+2].strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Try just the first 10 chars (ISO prefix)
    try:
        return _dt.fromisoformat(raw[:10]).strftime("%Y-%m-%d")
    except Exception:
        pass
    return ""


async def search_web(query: str, cfg: Config, on_attempt=None, safe: bool = False) -> list:
    try:
        match cfg.web_backend:
            case "google":  return await _google(query, cfg.google_api_key, cfg.google_cx)
            case "brave":   return await _brave(query, cfg.brave_api_key)
            case "serper":  return await _serper(query, cfg.serper_api_key)
            case "searxng": return await _searxng(query, cfg.searxng_url)
            case _:         return await _default(query, on_attempt, safe=safe)
    except Exception as e:
        log.error(f"search_web: {e}")
        return []


async def _default(query: str, on_attempt=None, safe: bool = False) -> list:
    """
    No-key pipeline — Microsoft-free first, DDG silent fallback:
      Tier 1 (parallel): Mojeek, Stract, DDG  — asyncio.gather, first non-empty wins
      Tier 2 (sequential): Yahoo → SearXNG → Marginalia
    """

    # ── Tier 1: parallel, first non-empty result wins ─────────────────────
    if on_attempt:
        await on_attempt("Mojeek · Stract", 1, 2)

    tier1 = await asyncio.gather(
        _mojeek(query),
        _stract(query),
        _ddg_vqd(query, safe=safe),
        return_exceptions=True,
    )
    # Combine ALL tier-1 results (deduplicated by URL)
    combined, seen = [], set()
    names1 = ["Mojeek", "Stract", "DuckDuckGo"]
    for name, result in zip(names1, tier1):
        if isinstance(result, Exception):
            log.debug(f"{name}: {result}")
        elif result:
            log.info(f"{name}: {len(result)} results ✓")
            for item in result:
                url = item.get("url", "")
                if url and url not in seen:
                    seen.add(url); combined.append(item)
        else:
            log.info(f"{name}: 0 results")
    if combined:
        log.info(f"Web tier-1 combined: {len(combined)} results")
        return combined

    # ── Tier 2: sequential fallbacks ──────────────────────────────────────
    if on_attempt:
        await on_attempt("Yahoo · SearXNG · Marginalia", 2, 2)

    for name, coro_fn in [
        ("Yahoo",      lambda: _yahoo(query)),
        ("SearXNG",    lambda: _public_searx(query)),
        ("Marginalia", lambda: _marginalia(query)),
    ]:
        try:
            r = await coro_fn()
            if r:
                log.info(f"{name}: {len(r)} results ✓")
                return r
            log.info(f"{name}: 0 results")
        except Exception as e:
            log.debug(f"{name}: {e}")

    log.warning("All backends failed — get a free Brave key at search.brave.com")
    return []



# ── DDG vqd ───────────────────────────────────────────────────────────────

async def _ddg_vqd(query: str, safe: bool = False) -> list:
    """Fetches two pages simultaneously to get ~20 results."""
    ck = {**DDG_COOKIES, "kp": "1"} if safe else DDG_COOKIES
    async with httpx.AsyncClient(headers=HEADERS, cookies=ck,
                                  timeout=TIMEOUT, follow_redirects=True) as c:
        r = await c.get("https://duckduckgo.com/",
                        params={"q": query, "ia": "web", "kl": "en-gb",
                                "kp": "1" if safe else "-1"})
        vqd = _extract_vqd(r.text)
        if not vqd:
            return []
        # Fetch page 1 (s=0) and page 2 (s=10) simultaneously
        base_params = {"q": query, "vqd": vqd, "kl": "en-gb"}
        r1, r2 = await asyncio.gather(
            c.get("https://links.duckduckgo.com/d.js", params={**base_params, "s": "0"}),
            c.get("https://links.duckduckgo.com/d.js", params={**base_params, "s": "10"}),
        )
    p1 = _parse_vqd(r1.text)
    p2 = _parse_vqd(r2.text)
    # Deduplicate by URL
    seen = set()
    out  = []
    for r in p1 + p2:
        if r["url"] not in seen:
            seen.add(r["url"]); out.append(r)
    return out[:20]


def _extract_vqd(html: str) -> str:
    for pat in [r'vqd="([^"]+)"', r"vqd='([^']+)'", r'vqd=([0-9\-]+)']:
        m = re.search(pat, html)
        if m: return m.group(1)
    return ""


def _no_wiki(url: str) -> bool:
    """True if URL is NOT a Wikipedia page."""
    return 'wikipedia.org' not in url

def _parse_vqd(text: str) -> list:
    m   = re.search(r"DDG\.pageLayout\.load\(['\"]d['\"],(\[.*?\])\)", text, re.DOTALL)
    raw = m.group(1) if m else text.strip()
    try: data = _json.loads(raw)
    except: return []
    out = []
    for item in data:
        if not isinstance(item, dict): continue
        url = item.get("u","")
        if not url or "duckduckgo.com" in url: continue
        if not _no_wiki(url): continue  # wiki belongs in wiki section
        ts   = item.get("p")  # Unix timestamp if available
        date = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d") if ts else ""
        snip = BeautifulSoup(item.get("a","") or "", "lxml").get_text(strip=True)
        out.append({"title": item.get("t", url), "url": url,
                    "snippet": snip, "date": date})
    return out


# ── Qwant ─────────────────────────────────────────────────────────────────

async def _qwant(query: str, safe: bool = False) -> list:
    async with httpx.AsyncClient(timeout=TIMEOUT,
                                  headers={"User-Agent": UA,
                                           "Accept": "application/json"}) as c:
        r = await c.get("https://api.qwant.com/v3/search/web",
                        params={"q": query, "count": 20, "offset": 0,
                                "safesearch": 1, "locale": "en_GB", "device": "desktop"})
    data = r.json()
    if data.get("status") != "success": return []
    mainline = data.get("data",{}).get("result",{}).get("items",{}).get("mainline",[])
    out = []
    for block in mainline:
        if block.get("type") != "web": continue
        for item in block.get("items",[]):
            url = item.get("url","")
            if not url or not _no_wiki(url): continue
            out.append({
                "title":   BeautifulSoup(item.get("title",""),"lxml").get_text(strip=True),
                "url":     url,
                "snippet": BeautifulSoup(item.get("desc",""),"lxml").get_text(strip=True),
                "date":    "",
            })
    return out[:10]


# ── Mojeek ────────────────────────────────────────────────────────────────

async def _mojeek(query: str) -> list:
    """Fetch 3 pages in parallel (own independent index, no Microsoft)."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT,
                                      follow_redirects=True) as c:
            pages = await asyncio.gather(
                c.get("https://www.mojeek.com/search", params={"q":query,"safe":"0","s":"0"}),
                c.get("https://www.mojeek.com/search", params={"q":query,"safe":"0","s":"10"}),
                c.get("https://www.mojeek.com/search", params={"q":query,"safe":"0","s":"20"}),
                return_exceptions=True,
            )
        results, got_any = [], False
        for resp in pages:
            if isinstance(resp, Exception): continue
            soup = BeautifulSoup(resp.text, "lxml")
            for li in soup.select("ul.results-standard > li"):
                a = li.find("a", href=re.compile(
                    r"^https?://(?!.*mojeek\.com)(?!.*wikipedia\.org)"))
                snip = li.find("p", class_=re.compile(r"^s$|snippet|desc", re.I))
                if not a: continue
                got_any = True
                # Try to extract date from result
                dt_el = li.find("time") or li.find(class_=re.compile(r"date|dt|published", re.I))
                raw_dt = ""
                if dt_el:
                    raw_dt = dt_el.get("datetime","") or dt_el.get_text(strip=True)
                # Normalise to YYYY-MM-DD if possible
                norm_dt = _parse_date(raw_dt)
                results.append({
                    "title":   a.get_text(strip=True),
                    "url":     a.get("href",""),
                    "snippet": snip.get_text(strip=True) if snip else "",
                    "date":    norm_dt,
                })
        if not got_any and pages and not isinstance(pages[0], Exception):
            _save_debug("mojeek_debug.html", pages[0].text)
        return results[:30]
    except Exception as e:
        log.debug(f"Mojeek: {e}")
        return []


async def _yahoo(query: str) -> list:
    hdrs = {**HEADERS, "Referer": "https://uk.search.yahoo.com/",
            "Cookie": "sB=v=1&pn=10&rw=new"}
    async with httpx.AsyncClient(headers=hdrs, timeout=TIMEOUT,
                                  follow_redirects=True) as c:
        r = await c.get("https://uk.search.yahoo.com/search",
                        params={"p": query, "n": "10", "ei": "UTF-8"})
    soup    = BeautifulSoup(r.text, "lxml")
    results = []
    for div in soup.select("div.algo"):
        a    = div.select_one("h3 a") or div.select_one("h2 a")
        snip = div.select_one("div.compText") or div.select_one("div.s")
        date_el = div.select_one("span.fc-2nd") or div.select_one(".compInfo")
        if not a: continue
        href = a.get("href","")
        m    = re.search(r"/RU=([^/]+)/", href)
        if m:
            try: href = unquote(m.group(1))
            except: pass
        if not href.startswith("http") or not _no_wiki(href): continue
        results.append({"title": a.get_text(strip=True), "url": href,
                        "snippet": snip.get_text(strip=True) if snip else "",
                        "date": date_el.get_text(strip=True) if date_el else ""})
    if not results:
        _save_debug("yahoo_debug.html", r.text)
    return results[:10]


# ── Public SearXNG ────────────────────────────────────────────────────────

async def _public_searx(query: str) -> list:
    for base in PUBLIC_SEARX:
        r = await _try_searx(base, query)
        if r: return r
    return []


async def _try_searx(base: str, query: str) -> list:
    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=False,
                                      headers=HEADERS) as c:
            try:   cookies = dict((await c.get(f"{base}/", timeout=3)).cookies)
            except: cookies = {}
            try:
                rj = await c.get(f"{base}/search",
                                 params={"q": query, "format": "json"}, cookies=cookies)
                if rj.status_code == 200:
                    items = rj.json().get("results",[])[:10]
                    if items:
                        return [{"title": i.get("title",""), "url": i.get("url",""),
                                 "snippet": i.get("content",""), "date": i.get("publishedDate","")}
                                for i in items]
            except Exception: pass
            rh = await c.get(f"{base}/search", params={"q": query}, cookies=cookies)
            if rh.status_code == 200 and len(rh.text) > 3000:
                return _parse_searx_html(rh.text)
    except Exception as e:
        log.debug(f"SearXNG {base}: {e}")
    return []


def _parse_searx_html(html: str) -> list:
    soup, out = BeautifulSoup(html,"lxml"), []
    for art in soup.select("article.result")[:10]:
        a    = art.select_one("h3 a") or art.select_one(".result_title a")
        snip = art.select_one("p.content") or art.select_one(".result-content")
        if not a: continue
        href = a.get("href","")
        if not href.startswith("http"): continue
        out.append({"title": a.get_text(strip=True), "url": href,
                    "snippet": snip.get_text(strip=True) if snip else "", "date": ""})
    return out


# ── Marginalia ────────────────────────────────────────────────────────────

async def _marginalia(query: str) -> list:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"https://api.marginalia.nu/beta/search/{quote(query)}",
                            params={"key": "PUBLIC"}, headers={"User-Agent": UA})
        if r.status_code != 200: return []
        return [{"title": i.get("title",""), "url": i.get("url",""),
                 "snippet": i.get("description",""), "date": ""}
                for i in r.json().get("results",[])[:10]]
    except Exception as e:
        log.debug(f"Marginalia: {e}")
        return []


# ── Keyed backends ────────────────────────────────────────────────────────


async def _serper(query: str, api_key: str) -> list:
    """Serper.dev — real Google results. Free tier: 2,500 searches, no credit card.
    Get a key at https://serper.dev (takes ~1 minute)."""
    if not api_key:
        return [{"title": "Serper: API key required", "url": "",
                 "snippet": "Free key at serper.dev — no credit card. Paste in Settings.",
                 "date": ""}]
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": 20, "gl": "gb", "hl": "en"},
            )
        data = r.json()
        if "error" in data:
            return [{"title": f"Serper error: {data['error']}", "url":"","snippet":"","date":""}]
        results = [
            {"title":   i.get("title",""),
             "url":     i.get("link",""),
             "snippet": i.get("snippet",""),
             "date":    i.get("date","")}
            for i in data.get("organic", [])
        ]
        log.info(f"Serper (Google): {len(results)} results ✓")
        return results
    except Exception as e:
        log.error(f"Serper: {e}")
        return []


async def _stract(query: str) -> list:
    """Stract — open-source independent search engine. No key required.
    Own crawler, no Microsoft/Google. https://stract.com"""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(
                "https://api.stract.com/beta/search",
                params={"query": query, "page": 0, "safeSearch": "Off"},
                headers={"User-Agent": UA,
                         "Accept": "application/json"},
            )
        log.debug(f"Stract: {r.status_code}")
        if r.status_code != 200:
            return []
        data = r.json()
        results = []
        for item in data.get("webpages", [])[:20]:
            url = item.get("url","")
            if not url or not _no_wiki(url): continue
            # snippet may be a string or nested object
            snip = item.get("snippet","") or item.get("body","")
            if isinstance(snip, dict):
                snip = snip.get("text","") or ""
            results.append({
                "title":   item.get("title",""),
                "url":     url,
                "snippet": snip,
                "date":    "",
            })
        log.info(f"Stract: {len(results)} results")
        return results
    except Exception as e:
        log.debug(f"Stract: {e}")
        return []

async def _google(query: str, api_key: str, cx: str) -> list:
    if not api_key or not cx:
        return [{"title":"Google: API key + CX required","url":"",
                 "snippet":"Add both in Settings. Free: 100/day.","date":""}]
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get("https://www.googleapis.com/customsearch/v1",
                        params={"key":api_key,"cx":cx,"q":query,"num":10})
    data = r.json()
    if "error" in data:
        return [{"title":f"Google error: {data['error'].get('message','')}",
                 "url":"","snippet":"","date":""}]
    return [{"title":i.get("title",""),"url":i.get("link",""),
             "snippet":i.get("snippet","").replace("\n"," "),"date":""}
            for i in data.get("items",[])]


async def _brave(query: str, api_key: str) -> list:
    if not api_key:
        return [{"title":"Brave Search: API key required","url":"",
                 "snippet":"Free key at api.search.brave.com — paste in Settings.","date":""}]
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get("https://api.search.brave.com/res/v1/web/search",
                        params={"q":query,"count":10},
                        headers={"Accept":"application/json",
                                 "X-Subscription-Token":api_key})
    return [{"title":i.get("title",""),"url":i.get("url",""),
             "snippet":i.get("description",""),"date":""}
            for i in r.json().get("web",{}).get("results",[])]


async def _searxng(query: str, base_url: str) -> list:
    if not base_url:
        return [{"title":"SearXNG URL not set","url":"",
                 "snippet":"Add your instance URL in Settings.","date":""}]
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{base_url.rstrip('/')}/search",
                        params={"q":query,"format":"json"})
    return [{"title":i.get("title",""),"url":i.get("url",""),
             "snippet":i.get("content",""),"date":""}
            for i in r.json().get("results",[])[:10]]


def _save_debug(fn: str, html: str):
    try: (CONFIG_DIR/fn).write_text(html, encoding="utf-8", errors="replace")
    except: pass


async def check_internet() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            await c.head("https://duckduckgo.com")
        return True
    except: return False
