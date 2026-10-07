"""
Image search — high-volume parallel pipeline:
  1. Wikipedia REST API    — article thumbnails (most relevant, top ~8)
  2. Wikimedia Commons     — file search, up to 200 results
  3. Openverse             — 3 pages × 20 = 60 results in parallel
  4. DDG i.js              — 3 pages × 30 = ~90 results (if not 403)
  5. Bing regex            — fallback ~30

All sources run in parallel. Total potential: 300-400+ results cached client-side.
"Load more" reveals them 20 at a time — no extra network request needed.
"""
import asyncio, logging, re, httpx
import html as _html

log  = logging.getLogger("ponder.images")
TO   = 12
UA   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
WM   = "Ponder/1.0 (https://github.com/DansDesigns/Ponder; contact via GitHub) Python-httpx"
HDRS = {"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"}
DDG  = {"5": "a", "kl": "en-gb"}
IMG  = {".png",".jpg",".jpeg",".gif",".webp",".svg",".bmp",".tiff"}


async def search_images(query: str, safe: bool = False) -> list:
    # Run all sources in parallel
    wiki_r, commons_r, openverse_r, ddg_r = await asyncio.gather(
        _wikipedia(query),
        _commons(query),
        _openverse_multi(query, pages=5, safe=safe),
        _ddg_multi(query, pages=5, safe=safe),
        return_exceptions=True,
    )

    # Order: Openverse first (direct CDN, no rate-limit issues),
    #        then Wikipedia article images, then DDG, then Commons last
    #        (Commons bulk-fetches hit Wikimedia CDN rate limits fastest)
    results = []
    for r in (openverse_r, wiki_r, ddg_r, commons_r):
        if isinstance(r, list):
            results.extend(r)
        # exceptions are silently ignored (source just contributes 0)

    # Bing as extra fallback if total is very low
    if len(results) < 30:
        log.info("All primary sources low — trying Bing")
        r = await _bing(query, safe=safe)
        results.extend(r)

    # Deduplicate by thumbnail URL
    seen, out = set(), []
    for item in results:
        t = item.get("thumbnail","")
        if t and t not in seen:
            seen.add(t)
            out.append(item)

    log.info(f"Images: {len(out)} results for '{query}'")
    return out[:1000]  # cap at 1000 — "load more" reveals in batches


# ── Wikipedia REST API ────────────────────────────────────────────────────

async def _wikipedia(query: str) -> list:
    try:
        async with httpx.AsyncClient(timeout=TO, follow_redirects=True) as c:
            sr = await c.get("https://en.wikipedia.org/w/api.php", params={
                "action":"query","list":"search","srsearch":query,
                "srlimit":"15","format":"json",
            }, headers={"User-Agent": WM})
            titles = [h["title"] for h in sr.json().get("query",{}).get("search",[])]

            # Fetch summaries in parallel
            tasks = [
                c.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{t.replace(' ','_')}",
                      headers={"User-Agent": WM})
                for t in titles[:8]
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        for title, resp in zip(titles[:8], responses):
            if isinstance(resp, Exception): continue
            d     = resp.json()
            thumb = (d.get("thumbnail") or {}).get("source","")
            if not thumb: continue
            orig  = (d.get("originalimage") or {}).get("source", thumb)
            results.append({
                "thumbnail": thumb, "image": orig,
                "title":     d.get("title", title),
                "url":       (d.get("content_urls",{}).get("desktop",{}).get("page","")
                              or f"https://en.wikipedia.org/wiki/{title.replace(' ','_')}"),
                "source":    "Wikipedia",
                "width":     (d.get("thumbnail") or {}).get("width", 0),
                "height":    (d.get("thumbnail") or {}).get("height", 0),
            })
        return results
    except Exception as e:
        log.debug(f"Wikipedia: {e}")
        return []


# ── Wikimedia Commons — up to 200 results ────────────────────────────────

async def _commons(query: str) -> list:
    try:
        async with httpx.AsyncClient(timeout=TO) as c:
            r = await c.get("https://commons.wikimedia.org/w/api.php", params={
                "action":"query","generator":"search","gsrnamespace":"6",
                "gsrsearch":query,"gsrlimit":"500",
                "prop":"imageinfo","iiprop":"url|thumburl","iiurlwidth":"320",
                "format":"json","origin":"*",
            }, headers={"User-Agent": WM})
        out = []
        for p in r.json().get("query",{}).get("pages",{}).values():
            title = p.get("title","")
            ext   = "." + title.rsplit(".",1)[-1].lower() if "." in title else ""
            if ext not in IMG: continue
            ii    = (p.get("imageinfo") or [{}])[0]
            thumb = ii.get("thumburl","") or ii.get("url","")
            if not thumb: continue
            out.append({
                "thumbnail": thumb, "image": ii.get("url", thumb),
                "title":     title.replace("File:","").replace("_"," "),
                "url":       f"https://commons.wikimedia.org/wiki/{title.replace(' ','_')}",
                "source":    "Wikimedia Commons", "width": 0, "height": 0,
            })
        return out
    except Exception as e:
        log.debug(f"Commons: {e}")
        return []


# ── Openverse — multiple pages in parallel ────────────────────────────────

async def _openverse_multi(query: str, pages: int = 5, safe: bool = False) -> list:
    try:
        async with httpx.AsyncClient(timeout=TO, follow_redirects=True) as c:
            tasks = [
                c.get("https://api.openverse.org/v1/images/",
                      params={"q":query,"page_size":20,"page":p,"mature":"false" if safe else "true"},
                      headers={"User-Agent":"Ponder/1.0","Accept":"application/json"})
                for p in range(1, pages+1)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        out = []
        for resp in responses:
            if isinstance(resp, Exception) or resp.status_code != 200: continue
            for i in resp.json().get("results",[]):
                thumb = i.get("thumbnail","") or i.get("url","")
                if not thumb: continue
                out.append({
                    "thumbnail": thumb, "image": i.get("url", thumb),
                    "title": i.get("title",""),
                    "url": i.get("foreign_landing_url","") or i.get("url",""),
                    "source": i.get("source",""),
                    "width": i.get("width",0) or 0, "height": i.get("height",0) or 0,
                })
        return out
    except Exception as e:
        log.debug(f"Openverse: {e}")
        return []


# ── DDG i.js — multiple pages in parallel ────────────────────────────────

async def _ddg_multi(query: str, pages: int = 5, safe: bool = False) -> list:
    try:
        async with httpx.AsyncClient(headers=HDRS, cookies=DDG,
                                      timeout=TO, follow_redirects=True) as c:
            # Get vqd (reusable across pages)
            for p in [{"q":query,"iax":"images","ia":"images","kl":"en-gb"},
                      {"q":query,"ia":"web","kl":"en-gb"}]:
                vqd = _vqd((await c.get("https://duckduckgo.com/", params=p)).text)
                if vqd: break
            if not vqd: return []

            # Fetch multiple pages in parallel using s= offset (0, 100, 200...)
            tasks = [
                c.get("https://duckduckgo.com/i.js",
                      params={"q":query,"vqd":vqd,"o":"json","p":"1" if safe else "-1",
                              "s":str(i*100),"u":"bing","f":",,,","l":"en-gb"},
                      headers={**HDRS,"Referer":"https://duckduckgo.com/"})
                for i in range(pages)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        out = []
        for resp in responses:
            if isinstance(resp, Exception) or resp.status_code != 200: continue
            for i in resp.json().get("results",[]):
                thumb = i.get("thumbnail") or i.get("image","")
                url   = i.get("url") or i.get("source","")
                if thumb and url:
                    out.append({"thumbnail":thumb,"image":i.get("image",thumb),
                                "title":i.get("title",""),"url":url,
                                "source":i.get("source",""),"width":0,"height":0})
        return out
    except Exception as e:
        log.debug(f"DDG images: {e}")
        return []


# ── Bing regex fallback ───────────────────────────────────────────────────

async def _bing(query: str, safe: bool = False) -> list:
    try:
        hdrs = {**HDRS,"Referer":"https://www.bing.com/","Accept-Language":"en-US,en;q=0.9",
                "Cookie":"SRCHHPGUSR=ADLT=OFF; MSCC=1"}
        async with httpx.AsyncClient(headers=hdrs, timeout=TO, follow_redirects=True) as c:
            r = await c.get("https://www.bing.com/images/async",
                            params={"q":query,"count":35,"first":1,"adlt":"strict" if safe else "off","cc":"US","mkt":"en-US"})
        text = _html.unescape(r.text)
        out, seen = [], set()
        for m in re.finditer(
            r'"turl"\s*:\s*"(https://[^"]+)"[^}]{0,400}"purl"\s*:\s*"(https://[^"]+)"[^}]{0,200}"t"\s*:\s*"([^"]*)"',
            text, re.DOTALL):
            thumb,url,title = m.group(1),m.group(2),m.group(3)
            if url not in seen and "bing.com" not in url:
                seen.add(url)
                out.append({"thumbnail":thumb,"image":thumb,"title":title,
                            "url":url,"source":_host(url),"width":0,"height":0})
        return out[:30]
    except Exception as e:
        log.warning(f"Bing: {e}")
        return []


def _vqd(h):
    for p in [r'vqd="([^"]+)"',r"vqd='([^']+)'",r'vqd=([0-9\-]+)']:
        m = re.search(p, h)
        if m: return m.group(1)
    return ""
def _host(u):
    try: return u.split("/")[2]
    except: return ""
