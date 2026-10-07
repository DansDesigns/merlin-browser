"""OpenStreetMap / Nominatim place search — free, no key required."""
import logging, httpx

log = logging.getLogger("ponder.maps")
TIMEOUT = 8
UA = "Ponder/1.0 (local-first search app; non-commercial)"


async def search_maps(query: str) -> list:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as c:
            r = await c.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 15,
                        "addressdetails": "1", "extratags": "1"},
                headers={"User-Agent": UA,
                         "Accept-Language": "en-GB,en;q=0.9"},
            )
        results = []
        for item in r.json()[:15]:
            addr  = item.get("address", {})
            parts = [addr.get(k,"") for k in
                     ["road","suburb","city","town","village","county","state","country"]
                     if addr.get(k)]
            lat, lon = item.get("lat",""), item.get("lon","")
            osm_t = item.get("osm_type","way")
            osm_id = item.get("osm_id","")
            results.append({
                "title":    item.get("display_name","").split(",")[0].strip(),
                "address":  ", ".join(parts[:4]),
                "full":     item.get("display_name",""),
                "lat":      lat,
                "lon":      lon,
                "type":     item.get("type",""),
                "category": item.get("class",""),
                "url":      f"https://www.openstreetmap.org/{osm_t}/{osm_id}",
                "map_url":  f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}&zoom=15",
            })
        log.info(f"Maps: {len(results)} results for '{query}'")
        return results
    except Exception as e:
        log.error(f"Map search error: {e}")
        return []
