import asyncio
import httpx
import re
import json as _json
import logging
import os
import zipfile
import subprocess
import webbrowser
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

from config import Config, CONFIG_DIR
from indexer import Indexer
from web_search import search_web, check_internet
from wiki import search_wiki
from image_search import search_images
from video_search import search_videos
from map_search   import search_maps

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="  %(levelname)-8s %(name)s — %(message)s",
)
logging.getLogger("ponder.web").setLevel(logging.DEBUG)
logging.getLogger("ponder.images").setLevel(logging.DEBUG)
logging.getLogger("ponder.video").setLevel(logging.DEBUG)
logging.getLogger("pdfminer").setLevel(logging.ERROR)  # suppress font warnings
log = logging.getLogger("ponder")

BASE = Path(__file__).parent.resolve()

# Support both ponder/static/index.html and ponder/index.html
_candidates = [BASE / "static" / "index.html", BASE / "index.html"]
INDEX_HTML  = next((p for p in _candidates if p.exists()), _candidates[0])

cfg = Config()
idx = Indexer(cfg)


# ── Startup / shutdown ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _wm_semaphore
    _wm_semaphore = asyncio.Semaphore(4)  # max 4 concurrent Wikimedia fetches
    try:
        idx.start()
        log.info(f"Ponder ready → http://localhost:{cfg.port}")
    except Exception as e:
        log.error(f"Startup error: {e}")
        # Non-fatal — routes still work, just without file watching
    yield
    try:
        idx.stop()
    except Exception:
        pass


app = FastAPI(title="Ponder", lifespan=lifespan)

# Serve logo, favicon and other static assets
_static_dir = BASE / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ── Pages ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home():
    if not INDEX_HTML.exists():
        return HTMLResponse(
            f"<pre>Ponder: static/index.html not found.\n"
            f"Expected: {INDEX_HTML}\n\n"
            f"Make sure you have the full project structure:\n"
            f"  ponder/\n"
            f"    main.py\n"
            f"    static/\n"
            f"      index.html\n"
            f"</pre>",
            status_code=500,
        )
    return INDEX_HTML.read_text(encoding="utf-8")


# ── Search — one OS thread per active source ─────────────────────────────
# web, wiki, and local each get their own thread + event loop so they
# run truly in parallel and can't block each other.



def _get_system_fonts() -> list[str]:
    """Return installed system font families — Windows (winreg) or Linux (font dirs + fc-list)."""
    import platform, re as _re
    fonts: set[str] = set()
    system = platform.system()

    if system == "Windows":
        try:
            import winreg
            _STYLE = re.compile(
                r"\s+(Bold|Italic|Light|Regular|Medium|Black|Thin|"
                r"Condensed|SemiBold|ExtraBold|ExtraLight|Narrow|Heavy)$", re.I)
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = winreg.OpenKey(
                        hive,
                        r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
                    i = 0
                    while True:
                        try:
                            name, _, _ = winreg.EnumValue(key, i)
                            family = name.split(" (")[0].strip()   # strip "(TrueType)"
                            family = _STYLE.sub("", family).strip()
                            if family:
                                fonts.add(family)
                            i += 1
                        except OSError:
                            break
                except Exception:
                    pass
        except ImportError:
            pass

    else:  # Linux / macOS
        from pathlib import Path as _P
        font_dirs = [
            _P("/usr/share/fonts"), _P("/usr/local/share/fonts"),
            _P("/usr/share/truetype"),
            _P.home() / ".fonts", _P.home() / ".local/share/fonts",
            _P("/System/Library/Fonts"), _P("/Library/Fonts"),   # macOS
        ]
        for d in font_dirs:
            if not d.exists():
                continue
            for f in d.rglob("*"):
                if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    name = f.stem.replace("-", " ").replace("_", " ")
                    name = re.sub(
                        r"\s+(Bold|Italic|Light|Regular|Medium|Black|Thin|"
                        r"Condensed|SemiBold|ExtraBold|ExtraLight|Narrow|Heavy|"
                        r"\d+|BoldItalic|LightItalic|MediumItalic)\b.*$",
                        "", name, flags=re.I).strip()
                    if name:
                        fonts.add(name)
        # Also try fc-list for accurate family names
        try:
            r = subprocess.run(
                ["fc-list", "--format=%{family[0]}\n"],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                for f in r.stdout.split("\n"):
                    f = f.strip()
                    if f:
                        fonts.add(f)
        except Exception:
            pass

    if fonts:
        return sorted(fonts, key=str.casefold)

    return ["Arial", "Consolas", "Courier New", "DM Mono", "Fira Code",
            "Georgia", "JetBrains Mono", "Tahoma", "Times New Roman", "Verdana"]


@app.get("/api/site-info")
async def site_info(url: str = Query(...)):
    """Fetch title, description, favicon for a URL — shown when user searches a web address."""
    try:
        if not url.startswith(("http://","https://")):
            url = "https://" + url
        async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                      headers={"User-Agent": "Mozilla/5.0 (compatible; Ponder/1.0)"}) as c:
            r = await c.get(url)
        from bs4 import BeautifulSoup as _BS
        from urllib.parse import urlparse as _up
        soup = _BS(r.text, "lxml")
        def _m(prop=None, name=None):
            t = soup.find("meta", property=prop) if prop else soup.find("meta", attrs={"name": name})
            return (t or {}).get("content","")
        title  = _m("og:title") or (soup.title.string if soup.title else "") or url
        desc   = _m("og:description") or _m(name="description") or ""
        image  = _m("og:image") or ""
        parsed = _up(url)
        domain = parsed.netloc
        favicon = f"{parsed.scheme}://{domain}/favicon.ico"
        return JSONResponse({
            "url":     url,
            "title":   str(title).strip()[:200],
            "description": str(desc).strip()[:500],
            "image":   image,
            "favicon": favicon,
            "domain":  domain,
        })
    except Exception as e:
        log.debug(f"site-info: {e}")
        from urllib.parse import urlparse as _up
        domain = _up(url).netloc if "://" in url else url
        return JSONResponse({"url":url,"title":url,"description":"","image":"","favicon":"","domain":domain})



@app.post("/api/upload-background")
async def upload_background(file: UploadFile = File(...)):
    """Save an uploaded image to static/ for use as a background."""
    import mimetypes
    ct = file.content_type or ""
    if not ct.startswith("image/"):
        return JSONResponse({"error": "not an image"}, status_code=400)
    # Sanitise filename
    fname = re.sub(r"[^\w.\-]", "_", file.filename or "background.png")
    dest  = BASE / "static" / fname
    dest.write_bytes(await file.read())
    return JSONResponse({"filename": fname})

@app.get("/api/backgrounds")
async def list_backgrounds():
    """List image files in static/ for use as backgrounds."""
    static = BASE / "static"
    images = sorted(
        f.name for f in static.iterdir()
        if f.is_file()
        and f.suffix.lower() in (".png",".jpg",".jpeg",".gif",".webp")
        and f.name not in ("logo.png","ponder.ico")
    )
    return JSONResponse({"images": ["none"] + images})

@app.get("/api/fonts")
async def list_fonts():
    fonts = await asyncio.to_thread(_get_system_fonts)
    return JSONResponse({"fonts": fonts})

@app.get("/api/search")
async def search(
    q:     str = Query(..., min_length=1),
    modes: str = Query("web,local,wiki"),
):
    active  = {m.strip() for m in modes.split(",")}
    mixed_q = asyncio.Queue()
    loop    = asyncio.get_running_loop()

    # Progress callback callable from worker threads
    def threadsafe_progress(backend: str, n: int, total: int):
        asyncio.run_coroutine_threadsafe(
            mixed_q.put({"type": "progress", "backend": backend,
                         "n": n, "total": total}),
            loop,
        )

    # Each worker runs in its own thread with its own asyncio event loop
    def _run_web(query: str) -> list:
        import asyncio as _aio
        nl = _aio.new_event_loop()
        _aio.set_event_loop(nl)
        async def _cb(backend, n, total):
            threadsafe_progress(backend, n, total)
        try:
            return nl.run_until_complete(search_web(query, cfg, on_attempt=_cb))
        except Exception as e:
            log.warning(f"web thread: {e}"); return []
        finally:
            nl.close()

    def _run_wiki(query: str) -> list:
        import asyncio as _aio
        nl = _aio.new_event_loop()
        _aio.set_event_loop(nl)
        try:
            return nl.run_until_complete(search_wiki(query))
        except Exception as e:
            log.warning(f"wiki thread: {e}"); return []
        finally:
            nl.close()

    def _run_images(query: str) -> list:
        import asyncio as _aio
        nl = _aio.new_event_loop(); _aio.set_event_loop(nl)
        try:    return nl.run_until_complete(search_images(query, safe=cfg.safe_search))
        except Exception as e:
            log.warning(f"images thread: {e}"); return []
        finally: nl.close()

    def _run_video(query: str) -> list:
        import asyncio as _aio
        nl = _aio.new_event_loop(); _aio.set_event_loop(nl)
        try:    return nl.run_until_complete(search_videos(query, safe=cfg.safe_search))
        except Exception as e:
            log.warning(f"video thread: {e}"); return []
        finally: nl.close()

    def _run_maps(query: str) -> list:
        import asyncio as _aio
        nl = _aio.new_event_loop(); _aio.set_event_loop(nl)
        try:    return nl.run_until_complete(search_maps(query))
        except Exception as e:
            log.warning(f"maps thread: {e}"); return []
        finally: nl.close()

    def _run_local(query: str) -> list:
        try:    return idx.search(query)
        except Exception as e:
            log.warning(f"local thread: {e}"); return []


    async def run(key: str, fn, *args):
        result = await asyncio.to_thread(fn, *args)
        await mixed_q.put({"type": "results", "source": key, "results": result})

    tasks = []
    if "web"   in active: tasks.append(asyncio.create_task(run("web",   _run_web,   q)))
    if "local"  in active: tasks.append(asyncio.create_task(run("local",  _run_local,  q)))
    if "images" in active: tasks.append(asyncio.create_task(run("images", _run_images, q)))
    if "video"  in active: tasks.append(asyncio.create_task(run("video",  _run_video,  q)))
    if "maps"   in active: tasks.append(asyncio.create_task(run("maps",   _run_maps,   q)))
    if "wiki"  in active: tasks.append(asyncio.create_task(run("wiki",  _run_wiki,  q)))

    async def generate():
        received = 0
        while received < len(tasks):
            item = await mixed_q.get()
            yield _json.dumps(item) + "\n"
            if item.get("type") == "results":
                received += 1

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ── File viewer ───────────────────────────────────────────────────────────

@app.get("/api/file")
async def read_file(path: str = Query(...)):
    p = Path(os.path.expanduser(path))
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "File not found"}, status_code=404)
    try:
        if p.stat().st_size > 2 * 1024 * 1024:
            return {"content": "[File too large to preview — open externally]",
                    "ext": p.suffix.lstrip(".")}
        return {"content": p.read_text(encoding="utf-8", errors="replace"),
                "ext": p.suffix.lower().lstrip(".")}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/open")
async def open_file(req: Request):
    data = await req.json()
    path = os.path.expanduser(data.get("path", ""))
    if not os.path.exists(path):
        return JSONResponse({"error": "Path not found"}, status_code=404)
    opener = "xdg-open" if os.name != "nt" else "explorer"
    subprocess.Popen([opener, path])
    return {"ok": True}


@app.get("/api/availability")
async def availability():
    """Returns which search sources are currently usable."""
    from pathlib import Path
    internet  = await check_internet()
    st        = idx.status()
    # Local is available as long as at least one watch dir exists on disk.
    # doc_count == 0 just means indexing is still running — don't grey it out.
    local_ok  = any(
        Path(os.path.expanduser(d)).exists()
        for d in cfg.watch_dirs
    )
    return {
        "web":      internet,
        "wiki":     internet,
        "images":   internet,
        "video":    internet,
        "maps":     internet,
        "local":    local_ok,
        "indexing": local_ok and st["doc_count"] == 0,
    }


# ── Settings ──────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    return cfg.to_dict()


@app.post("/api/settings")
async def save_settings(req: Request):
    data = await req.json()
    cfg.update(data)
    cfg.save()
    return {"ok": True}


# ── Index management ──────────────────────────────────────────────────────

@app.get("/api/status")
async def status():
    return idx.status()


@app.post("/api/reindex")
async def reindex():
    asyncio.create_task(asyncio.to_thread(idx.build_index))
    return {"ok": True, "message": "Reindexing started in background"}


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    url = f"http://localhost:{cfg.port}"
    print(f"\n  Ponder  →  {url}\n")
    if cfg.open_browser:
        webbrowser.open(url)
    uvicorn.run("main:app", host=cfg.host, port=cfg.port, reload=False)


# ── Summary (DDG Instant Answers — factual, no AI) ────────────────────────



@app.get("/api/local-ip")
async def local_ip():
    """Return the machine's LAN IP address and the Ponder URL for network access."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"
    return JSONResponse({
        "ip":   ip,
        "url":  f"http://{ip}:{cfg.port}",
        "host": cfg.host,
        "port": cfg.port,
    })

    return ["Arial", "Consolas"]


# ── Version / update helpers ─────────────────────────────────────────────
def _parse_version(raw: str, branch: str = "") -> dict | None:
    """Parse a version.txt line like 'main 2026-06-01 RC_MAIN.zip' or 'v1.3.0'.

    Returns {"branch": ..., "version": ...} or None on parse failure.
    """
    if not raw or "\n" in raw:
        return _parse_version(raw.split("\n")[-1].strip() if "\n" in raw else "")
    parts = raw.strip().split(maxsplit=3)
    if len(parts) >= 3:
        branch, ver = parts[0], " ".join(parts[1:])
    elif len(parts) == 2 and re.match(r"^v?\d", parts[0]):
        branch, ver = "", parts[0]
    else:
        return None
    if not branch:
        branch = ""
    if not ver:
        ver = ""
    # Try semver comparison first; fall back to string compare for date-tagged versions
    try:
        import packaging.version as _pv
        v1, v2 = _pv.parse(ver.split()[0]) if " " in ver else _pv.parse(ver)
        return {"branch": branch, "version": ver, "_semver": True}
    except Exception:
        pass
    # String fallback (works for dates like 2026-06-01 too)
    v1 = ver.split()[0] if " " in ver else ver
    return {"branch": branch, "version": ver, "_semver": False}


def _fetch_remote_version_text(repo_url: str) -> tuple[bool, str | None]:
    """Fetch version.txt from GitHub via raw.githubusercontent.com.

    Returns (ok, content). content is the raw text or None on failure.
    """
    try:
        import urllib.request as _urllib
        url = f"{repo_url.rstrip('/')}/raw/main/version.txt"
        with _urllib.urlopen(url) as r:  # type: ignore[attr-defined]
            if r.status == 200:
                return True, r.read().decode("utf-8", errors="replace")
            else:
                return False, ""
    except Exception as e:
        log.debug(f"version.txt fetch error: {e}")
        return False, None


@app.get("/api/version")
async def api_version():
    """Return local and remote version info for comparison.

    GET /api/version?local_path=<path> — reads a local version file (optional).
    """
    repo_url = cfg.github_repo or ""
    # Read local version.txt if it exists
    local_path = Path(__file__).parent / "version.txt"
    local_ok = False
    local_data = {}
    if local_path.exists():
        text = local_path.read_text(errors="replace").strip()
        parsed = _parse_version(text)
        if parsed:
            local_data = {"branch": parsed["branch"], "version": parsed["version"]}
            local_ok = True

    # Fetch remote version.txt
    ok, remote_text = _fetch_remote_version_text(repo_url)
    remote_data = {}
    if ok and remote_text.strip():
        parsed = _parse_version(remote_text)
        if parsed:
            remote_data = {"branch": parsed["branch"], "version": parsed["version"]}

    result = {
        "local_available": local_ok,
        "remote_available": ok and bool(remote_text),
        "has_update": False,
    }
    if result["local_available"] and result["remote_available"]:
        result["has_update"] = remote_data.get("version", "") > local_data.get("version", "")

    return JSONResponse({
        "repo_url": repo_url,
        **result,
        **local_data,
        **remote_data,
    })


@app.post("/api/update-download/{filename}")
async def api_update_download(filename: str):
    """Download a single file from the GitHub release zip (unzipped via raw URL).

    POST /api/update-download/main.py?branch=main&path=<file_path>

    The file is written to BASE/<branch>/<filename>. If branch has no files, writes directly to BASE.
    """
    repo_url = cfg.github_repo or ""
    branch = "main"  # default; can be overridden by query param
    path = filename  # e.g. "main.py", "config.py", "static/index.html"

    url = f"{repo_url.rstrip('/')}/raw/{branch}/{path}"
    try:
        import urllib.request as _urllib
        with _urllib.urlopen(url) as r:  # type: ignore[attr-defined]
            if r.status != 200:
                return JSONResponse({"error": f"HTTP {r.status}"}, status_code=404)
            data = await asyncio.to_thread(r.read)
    except Exception as e:
        log.warning(f"download error for {path}: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)

    # Determine destination
    BASE = Path(__file__).parent.resolve()
    dest_dir = BASE / "update" if branch else BASE
    dest_path = dest_dir / path

    await asyncio.to_thread(dest_path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(dest_path.write_bytes, data)

    return JSONResponse({"ok": True, "path": str(dest_path), "size": len(data)})


@app.post("/api/update-apply")
async def api_update_apply():
    """Perform the full update: copy files from 'update/' into root and restart."""
    BASE = Path(__file__).parent.resolve()

    # Copy all files from 'update/' directory into BASE (overwriting)
    update_dir = BASE / "update"
    if not update_dir.exists():
        return JSONResponse({"error": "No updates to apply — run the update check first."}, status_code=400)

    copied = 0
    skipped = 0
    for f in update_dir.iterdir():
        dest = BASE / f.name
        if not dest.exists() or f.stat().st_size > dest.stat().st_size:
            await asyncio.to_thread(dest.write_bytes, f.read_bytes())
            copied += 1
        else:
            skipped += 1

    # Remove the update staging directory
    try:
        import shutil as _shutil
        _shutil.rmtree(str(update_dir))
    except Exception:
        pass

    return JSONResponse({
        "ok": True,
        "copied": copied,
        "skipped_newer": skipped,
        "message": f"Copied {copied} files from update/ into project root.",
    })



# ── Updater ───────────────────────────────────────────────────────────────
GITHUB_RAW   = "https://raw.githubusercontent.com/DansDesigns/Ponder/main"
GITHUB_ZIP   = "https://github.com/DansDesigns/Ponder/archive/refs/heads/main.zip"
VERSION_FILE = BASE / "version.txt"

UPDATE_FILES = {
    "main.py", "config.py", "indexer.py", "web_search.py",
    "image_search.py", "video_search.py", "wiki.py", "map_search.py",
    "install.py", "requirements.txt", "README.md", "version.txt",
    "static/index.html",
}

def _local_version() -> str:
    try:    return VERSION_FILE.read_text().strip()
    except: return "0.0.0"

@app.get("/api/update/check")
async def update_check():
    local = _local_version()
    remote = None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            # Try main branch first, fall back to master
            for branch in ("main", "master"):
                r = await c.get(
                    f"https://raw.githubusercontent.com/DansDesigns/Ponder/{branch}/version.txt"
                )
                if r.status_code == 200:
                    remote = r.text.strip()
                    break
        if remote is None:
            return JSONResponse({"error": "version.txt not found on GitHub"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse({"local": local, "remote": remote, "up_to_date": local == remote})

@app.post("/api/update/download")
async def update_download():
    update_dir = BASE / "update"
    try:
        update_dir.mkdir(exist_ok=True)
        zip_path = update_dir / "ponder_update.zip"
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(GITHUB_ZIP)
            zip_path.write_bytes(r.content)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(update_dir)
        zip_path.unlink()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/update/apply")
async def update_apply():
    import shutil as _sh
    update_dir = BASE / "update"
    extracted  = next((d for d in update_dir.iterdir() if d.is_dir()), None)
    if not extracted:
        return JSONResponse({"ok": False, "error": "No extracted folder found"}, status_code=500)
    try:
        for rel in UPDATE_FILES:
            src_path  = extracted / rel
            dest_path = BASE / rel
            if src_path.exists():
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                _sh.copy2(src_path, dest_path)
        _sh.rmtree(update_dir, ignore_errors=True)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/restart")
async def restart_server():
    """Restart Ponder process to apply host/port changes."""
    import os, sys, threading
    def _do():
        import time; time.sleep(0.3)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do, daemon=True).start()
    return JSONResponse({"ok": True})

@app.get("/api/summary")
async def summary(q: str = Query(...)):
    ddg_r, wiki_r = await asyncio.gather(
        asyncio.to_thread(_ddg_only, q),
        _wiki_summary(q),
        return_exceptions=True,
    )
    def _relevant(result, query):
        """Check summary heading/source contains at least one query word."""
        if not isinstance(result, dict) or not result.get("text"):
            return False
        q_words = {w.lower() for w in re.split(r'\W+', query) if len(w) > 2}
        haystack = (result.get("heading","") + " " + result.get("source","") +
                    " " + result.get("url","")).lower()
        return any(w in haystack for w in q_words)
    # Prefer Wikipedia (more accurate for multi-word queries)
    if _relevant(wiki_r, q):
        return JSONResponse(wiki_r)
    # Fall back to DDG only if it's actually about the query
    if _relevant(ddg_r, q):
        return JSONResponse(ddg_r)
    return JSONResponse({})  # Nothing relevant found — show no summary

def _ddg_only(query: str) -> dict:
    """DDG Instant Answers only (sync, runs in thread)."""
    try:
        r = httpx.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1",
                    "skip_disambig": "1", "no_redirect": "1"},
            timeout=3, headers={"User-Agent": "Ponder/1.0"},
        )
        d = r.json()
        text = d.get("Abstract", "").strip()
        if text:
            return {"text": text, "source": d.get("AbstractSource",""),
                    "url": d.get("AbstractURL",""), "heading": d.get("Heading","")}
    except Exception:
        pass
    return {}


async def _wiki_summary(query: str) -> dict:
    """Wikipedia REST API summary (async)."""
    WM = "Ponder/1.0 (https://github.com/DansDesigns/Ponder; contact via GitHub)"
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as c:
            sr = await c.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action":"query","list":"search","srsearch":query,
                        "srlimit":"1","format":"json"},
                headers={"User-Agent": WM},
            )
            hits = sr.json().get("query",{}).get("search",[])
            if not hits:
                return {}
            title = hits[0]["title"]
            s = await c.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ','_')}",
                headers={"User-Agent": WM},
            )
            d = s.json()
            text = d.get("extract","").strip()
            if not text:
                return {}
            if len(text) > 400:
                cut = text[:400].rfind(". ")
                text = text[:cut+1] if cut > 100 else text[:400] + "…"
            page_url = d.get("content_urls",{}).get("desktop",{}).get("page","")
            return {"text": text, "source": "Wikipedia",
                    "url": page_url, "heading": d.get("title", title)}
    except Exception as e:
        log.debug(f"wiki_summary: {e}")
        return {}


# Keep old name as alias for backward compat
def _ddg_summary(query: str) -> dict:
    return _ddg_only(query)





# ── Search history ────────────────────────────────────────────────────────

HIST_FILE = CONFIG_DIR / "history.json"

def _load_hist() -> list:
    try:   return _json.loads(HIST_FILE.read_text()) if HIST_FILE.exists() else []
    except: return []

def _save_hist(h: list): HIST_FILE.write_text(_json.dumps(h, indent=2))

@app.get("/api/history")
async def get_history():
    return _load_hist()

@app.post("/api/history")
async def add_history(req: Request):
    data = await req.json()
    q = (data.get("q","") or "").strip()
    if not q: return {"ok": False}
    hist = [h for h in _load_hist() if h.lower() != q.lower()]
    hist.insert(0, q)
    _save_hist(hist[:50])
    return {"ok": True}

@app.delete("/api/history")
async def delete_history(q: str = Query("")):
    if q:
        _save_hist([h for h in _load_hist() if h.lower() != q.lower()])
    else:
        _save_hist([])
    return {"ok": True}

# ── OpenSearch descriptor (lets browsers add Ponder as a search engine) ──

@app.get("/opensearch.xml")
async def opensearch(request: Request):
    from fastapi.responses import Response
    base = str(request.base_url).rstrip("/")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>Ponder</ShortName>
  <Description>Search with Ponder — web, files and wiki</Description>
  <Url type="text/html" template="{base}/?q={{searchTerms}}"/>
  <Url type="application/opensearchdescription+xml" rel="self" template="{base}/opensearch.xml"/>
  <InputEncoding>UTF-8</InputEncoding>
</OpenSearchDescription>"""
    return Response(content=xml, media_type="application/opensearchdescription+xml")


# ── Directory browser (for folder picker in settings) ─────────────────────

@app.get("/api/browse")
async def browse_dirs(path: str = Query("~")):
    p = Path(os.path.expanduser(path)).resolve()
    if not p.exists() or not p.is_dir():
        p = Path.home()
    try:
        entries = sorted(
            [d.name for d in p.iterdir()
             if d.is_dir() and not d.name.startswith(".")],
            key=str.lower,
        )
    except PermissionError:
        entries = []
    return {
        "path":   str(p),
        "dirs":   entries[:60],
        "parent": str(p.parent) if p != p.parent else str(p),
        "home":   str(Path.home()),
    }



# ── Image proxy (bypasses CDN referer restrictions) ──────────────────────

@app.get("/api/img-proxy")
async def img_proxy(url: str = Query(...)):
    """Proxy image CDN requests server-side to avoid localhost hotlink blocks."""
    try:
        if not url.startswith("https://"):
            return Response(status_code=403)
        is_wm = "wikimedia.org" in url or "wikipedia.org" in url
        sem    = _wm_semaphore if (is_wm and _wm_semaphore) else None
        async def _fetch():
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                return await c.get(url, headers={
                    "User-Agent": "Ponder/1.0 (https://github.com/DansDesigns/Ponder)",
                    "Referer": "https://commons.wikimedia.org/",
                })
        if sem:
            async with sem:
                r = await _fetch()
        else:
            r = await _fetch()
        # Retry once on 429 — do NOT re-acquire sem (would deadlock)
        if r.status_code == 429:
            await asyncio.sleep(1.5)
            r = await _fetch()
        ct = r.headers.get("content-type","image/jpeg")
        if r.status_code not in (200, 206) or not ct.startswith("image/"):
            return Response(status_code=404)
        return Response(content=r.content, media_type=ct,
                        headers={"Cache-Control":"public, max-age=3600"})
    except Exception as e:
        log.debug(f"img-proxy: {e}")
        return Response(status_code=404)

# ── Favourites ────────────────────────────────────────────────────────────

FAV_FILE = Path.home() / ".config" / "ponder" / "favourites.json"

def _load_favs() -> list:
    try:
        return _json.loads(FAV_FILE.read_text()) if FAV_FILE.exists() else []
    except Exception:
        return []

def _save_favs(favs: list):
    FAV_FILE.write_text(_json.dumps(favs, indent=2))

@app.get("/api/favourites")
async def get_favourites():
    return _load_favs()

@app.post("/api/favourites")
async def add_favourite(req: Request):
    data = await req.json()
    favs = _load_favs()
    url  = data.get("url", "")
    if url and not any(f.get("url") == url for f in favs):
        from datetime import date
        data["saved_at"] = str(date.today())
        favs.append(data)
        _save_favs(favs)
    return {"ok": True}

@app.delete("/api/favourites")
async def remove_favourite(url: str = Query(...)):
    favs = [f for f in _load_favs() if f.get("url") != url]
    _save_favs(favs)
    return {"ok": True}
