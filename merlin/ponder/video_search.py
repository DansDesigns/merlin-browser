"""DDG video search — no API key required."""
import logging, re, httpx

log = logging.getLogger("ponder.video")
TIMEOUT = 10
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Accept-Language": "en-GB,en;q=0.9",
}
DDG_COOKIES = {"5": "a", "kl": "en-gb"}


async def search_videos(query: str, safe: bool = False) -> list:
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, cookies=DDG_COOKIES,
            timeout=TIMEOUT, follow_redirects=True,
        ) as c:
            r = await c.get("https://duckduckgo.com/",
                            params={"q": query, "iax": "videos",
                                    "ia": "videos", "kl": "en-gb"})
            vqd = _vqd(r.text)
            if not vqd:
                log.warning("Video: no vqd token")
                return []
            log.debug(f"Video: vqd={vqd[:12]}…")

            r2 = await c.get(
                "https://duckduckgo.com/v.js",
                params={"q": query, "vqd": vqd, "o": "json",
                        "p": "1" if safe else "-1", "s": "0", "dc": "11",
                        "v": "l", "f": ",,,,,", "l": "en-gb"},
                headers={**HEADERS,
                         "Accept": "application/json, */*; q=0.01",
                         "Referer": "https://duckduckgo.com/",
                         "X-Requested-With": "XMLHttpRequest"},
            )
            log.debug(f"Video: status={r2.status_code}")
            data   = r2.json()
            results = []
            for item in data.get("results", [])[:20]:
                imgs  = item.get("images", {})
                thumb = imgs.get("large") or imgs.get("medium") or imgs.get("small","")
                url   = item.get("embed_url") or item.get("url","")
                title = item.get("title","")
                if not url or not title:
                    continue
                results.append({
                    "thumbnail":   thumb,
                    "url":         url,
                    "title":       title,
                    "description": item.get("description",""),
                    "duration":    item.get("duration",""),
                    "published":   item.get("published",""),
                    "uploader":    item.get("uploader",""),
                    "provider":    item.get("provider",""),
                })
            log.info(f"Video: {len(results)} results for '{query}'")
            return results
    except Exception as e:
        log.error(f"Video search error: {e}")
        return []


def _vqd(html: str) -> str:
    for pat in [r'vqd="([^"]+)"', r"vqd='([^']+)'", r'vqd=([0-9\-]+)']:
        m = re.search(pat, html)
        if m: return m.group(1)
    return ""
