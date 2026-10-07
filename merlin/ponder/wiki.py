import httpx

API     = "https://en.wikipedia.org/w/api.php"
TIMEOUT = 6
# Wikipedia requires a descriptive User-Agent — without it you get 403
HEADERS = {
    "User-Agent": "Ponder/1.0 (https://github.com/DansDesigns/Ponder; contact via GitHub) Python-httpx",
}


async def search_wiki(query: str, limit: int = 10) -> list:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            r = await c.get(API, params={
                "action":   "query",
                "list":     "search",
                "srsearch": query,
                "srlimit":  limit,
                "utf8":     1,
                "format":   "json",
            })
        items   = r.json().get("query", {}).get("search", [])
        results = []
        for item in items:
            title   = item.get("title", "")
            snippet = _strip_html(item.get("snippet", ""))
            slug    = title.replace(" ", "_")
            results.append({
                "title":   title,
                "url":     f"https://en.wikipedia.org/wiki/{slug}",
                "snippet": snippet,
            })
        return results
    except Exception as e:
        import logging
        logging.getLogger("ponder.wiki").warning(f"Wiki search error: {e}")
        return []


def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text)
