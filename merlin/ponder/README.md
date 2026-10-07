# Ponder - take control of your websearch
![((https://raw.githubusercontent.com/DansDesigns/Ponder/refs/heads/main/Screenshot%202026-05-31%20165516.png))](https://raw.githubusercontent.com/DansDesigns/Ponder/refs/heads/main/Screenshot%202026-05-31%20165516.png)

A locally hosted websearch frontend. Search web, images, video, maps and Wikipedia from your browser at `http://localhost:7000`. No accounts, no tracking.
* multiple search providers
* customisable background
* host on your network
* private locally stored search history



## Install
clone this repo:`git clone https://github.com/DansDesigns/Ponder`

unzip and browse to the folder..

**Windows** — double-click `install.bat`. Creates a silent Start Menu / Desktop shortcut and sets up `http://ponder` as a local hostname.

**Linux** —  in terminal type: `python3 install.py` or from a file browser, double click `install.sh`

## Use

Open `http://localhost:7000`. Press `/` to focus the search bar, `↓` to browse history.

## Search sources

| Mode | Sources |
|---|---|
| Web | Mojeek · Stract · DuckDuckGo (parallel, auto-fallback) |
| Images | Openverse · Wikimedia Commons · Wikipedia |
| Video | DuckDuckGo |
| Maps | OpenStreetMap / Nominatim |
| Wiki | Wikipedia full-text |

## Settings

Open via the ⚙ cog. All settings persist to `~/.config/ponder/config.json`.

| Key | Default | Notes |
|---|---|---|
| `web_backend` | `ddg` | `ddg` · `brave` · `google` · `serper` · `searxng` |
| `brave_api_key` | — | [search.brave.com](https://api.search.brave.com) |
| `show_summary` | `true` | Instant answer bar |
| `safe_search` | `false` | Math puzzle gate |
| `dark_mode` | `light` | `light` · `dark` · `night` |
| `background_image` | `none` | Any image file in `static/` |
| `bg_opacity` | `15` | 2–80% |
| `host` | `127.0.0.1` | `0.0.0.0` for network/server mode |
| `port` | `7000` | |

## Network mode

Settings → **Server Mode** → toggle on → Restart. All devices on your network can then use the URL shown (e.g. `http://192.168.1.50:7000`). On Android/iOS use *Add to Home Screen* from your browser for a full-screen app.

## Files

```
main.py          FastAPI app + all routes
config.py        Config
web_search.py    Web search pipeline
image_search.py  Image search
video_search.py  Video search
wiki.py          Wikipedia
map_search.py    Maps
install.py       Installer
static/
  index.html     Full UI (CSS + JS inline)
  logo.png
  ponder.ico
```
