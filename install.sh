#!/usr/bin/env bash
# Merlin Browser - Linux installer.
#
# Everything goes into a virtualenv so nothing touches your system Python.
# That also sidesteps PEP 668 ("externally-managed-environment"), which makes
# pip --user fail outright on current Debian and Ubuntu.
#
#   --venv-only     force a self-contained venv (pip engine, no H.264/AAC)
#   --system-qt     force reuse of the distro engine (licensed codecs)
#   --yes           accept defaults, no prompts
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HOME/.local/lib/merlin-browser"
VENV="$LIB/venv"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"

MODE=""
ASSUME_YES="${MERLIN_ASSUME_YES:-0}"
for arg in "$@"; do
  case "$arg" in
    --venv-only)  MODE="venv" ;;
    --system-qt)  MODE="system" ;;
    --yes|-y)     ASSUME_YES=1 ;;
    -h|--help)    sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

ask() {                      # ask "question" -> 0 for yes
  [[ "$ASSUME_YES" == "1" ]] && return 0
  read -r -p "$1 [Y/n] " reply
  [[ -z "$reply" || "${reply,,}" == "y" ]]
}

say() { printf '  %s\n' "$*"; }

# ---------------------------------------------------------------- progress UI
# The bar is pinned to the last line of the terminal using a DECSTBM scroll
# region: everything the installer prints scrolls in the lines above it, while
# the bar itself is redrawn in place. Printing a bar after each section, which
# is what this did before, is a log line rather than a progress bar.
TOTAL_STEPS=7
CURRENT_STEP=0
CURRENT_LABEL="Starting"
UI_ACTIVE=0
TERM_ROWS=24
TERM_COLS=80

# Styling only when a terminal is attached, so redirecting to a log file gives
# plain readable text rather than escape sequences.
if [[ -t 1 ]]; then
  B=$'\033[1m'; D=$'\033[2m'; R=$'\033[0m'
else
  B=""; D=""; R=""
fi

ui_init() {
  [[ -t 1 ]] || return 0                     # piped or logged: plain output
  TERM_ROWS="$(tput lines 2>/dev/null || echo 24)"
  TERM_COLS="$(tput cols 2>/dev/null || echo 80)"
  [[ "$TERM_ROWS" -lt 8 ]] && return 0
  UI_ACTIVE=1
  printf '\n\n'
  printf '\033[1;%dr' "$((TERM_ROWS - 1))"   # reserve the bottom line
  printf '\033[%d;1H' "$((TERM_ROWS - 1))"   # park the cursor above it
  return 0
}

ui_done() {
  [[ "$UI_ACTIVE" == "1" ]] || return 0
  printf '\0337'                              # save cursor
  printf '\033[%d;1H\033[2K' "$TERM_ROWS"    # clear the bar line
  printf '\0338'
  printf '\033[r'                             # release the scroll region
  UI_ACTIVE=0
  return 0
}
trap ui_done EXIT INT TERM

bar() {                      # bar [spinner-char]
  local spin="${1:- }"
  local width=$(( TERM_COLS - 30 ))
  [[ "$width" -gt 46 ]] && width=46
  [[ "$width" -lt 10 ]] && width=10
  local pct=$(( CURRENT_STEP * 100 / TOTAL_STEPS ))
  local filled=$(( CURRENT_STEP * width / TOTAL_STEPS ))
  local track="" i
  for ((i = 0; i < filled; i++)); do track+="#"; done
  for ((i = filled; i < width; i++)); do track+="."; done

  if [[ "$UI_ACTIVE" == "1" ]]; then
    printf '\0337'
    printf '\033[%d;1H\033[2K' "$TERM_ROWS"
    printf '  %s [%s] %3d%%  %s' "$spin" "$track" "$pct" "$CURRENT_LABEL"
    printf '\0338'
  fi
  return 0
}

step() {                     # step <n> <label>
  CURRENT_STEP="$1"
  CURRENT_LABEL="$2"
  echo
  printf '  %s[%d/%d] %s%s\n' "$B" "$1" "$TOTAL_STEPS" "$2" "$R"
  bar
  return 0
}

status() {                   # status <label>   update the bar without a heading
  CURRENT_LABEL="$1"
  bar
  return 0
}

# Run a command with its output visible, spinning the bar while it works, so a
# long step never looks like a hung cursor.
run() {                      # run <label> <command...>
  local label="$1"; shift
  CURRENT_LABEL="$label"
  printf '  %s$ %s%s\n' "$D" "$*" "$R"
  if [[ "$UI_ACTIVE" != "1" ]]; then
    "$@"
    return $?
  fi
  "$@" &
  local pid=$! frames='|/-\\' i=0
  while kill -0 "$pid" 2>/dev/null; do
    bar "${frames:i++%4:1}"
    sleep 0.15
  done
  wait "$pid"
  local rc=$?
  bar
  return $rc
}

ui_init
echo
echo "  ==========================================================="
echo "   Merlin Browser"
echo "  ==========================================================="

[[ -f "$SRC/merlin/app.py" ]] || { say "Run this from the extracted folder."; exit 1; }

SRC_VERSION="$(head -1 "$SRC/version.txt" 2>/dev/null | tr -d '\r' | awk '{print $1}')"
say "Installing version ${SRC_VERSION:-unknown} from $SRC"

# ------------------------------------------------------------------ python
PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || { say "python3 not found. Install it and try again."; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
  say "Python 3.9 or newer is required."; exit 1; }
step 1 "$("$PYTHON" -V)"

if ! "$PYTHON" -c 'import venv' 2>/dev/null; then
  say "The venv module is missing. Install it:"
  say "    sudo apt install python3-venv        # Debian / Ubuntu"
  say "    sudo dnf install python3-virtualenv  # Fedora"
  exit 1
fi

# --------------------------------------------------- which engine to reuse
SYSTEM_QT=0
if "$PYTHON" -c 'import PyQt6.QtWebEngineWidgets' 2>/dev/null; then
  SYSTEM_QT=1
fi

if [[ -z "$MODE" ]]; then
  if [[ "$SYSTEM_QT" == "1" ]]; then
    MODE="system"
  else
    MODE="venv"
  fi
fi

echo
if [[ "$MODE" == "system" ]]; then
  step 2 "Using the system Qt WebEngine"
  say "      Distro builds enable H.264 and AAC; pip wheels do not."
  if [[ "$SYSTEM_QT" == "0" ]]; then
    say "      WARNING: no system PyQt6 found. Install it first:"
    say "          sudo apt install python3-pyqt6 python3-pyqt6.qtwebengine"
    exit 1
  fi
else
  step 2 "Self-contained venv"
  say "      That build has no H.264, AAC or HEVC. A media player covers them."
  if [[ "$SYSTEM_QT" == "1" ]]; then
    say "      Note: your distro already ships the engine with more codecs."
    say "      Re-run with --system-qt to use it instead."
  fi
fi

# -------------------------------------------------------------------- venv
echo
step 3 "Creating the virtualenv"
say "in $VENV"
rm -rf "$VENV"
mkdir -p "$LIB"
if [[ "$MODE" == "system" ]]; then
  run "Creating venv (with system packages)" \
      "$PYTHON" -m venv --system-site-packages "$VENV"
else
  run "Creating venv" "$PYTHON" -m venv "$VENV"
fi
VPY="$VENV/bin/python"
run "Updating pip" "$VPY" -m pip install --upgrade pip || true

if [[ "$MODE" == "venv" ]]; then
  say "Installing PyQt6 and the web engine, roughly 150 MB."
  say "This is the slow part. pip's own progress appears below;"
  say "the bar at the bottom of the window tracks the install overall."
  echo
  run "Downloading PyQt6 and the engine" \
      "$VPY" -m pip install --progress-bar on PyQt6 PyQt6-WebEngine || {
    say "pip install failed. Run it by hand to see why:"
    say "    $VPY -m pip install PyQt6 PyQt6-WebEngine"
    exit 1
  }
fi

"$VPY" -c 'import PyQt6.QtWebEngineWidgets' 2>/dev/null || {
  say "The engine still will not import from the venv. Stopping."
  exit 1
}
ENGINE_PATH="$("$VPY" -c 'import PyQt6.QtWebEngineCore as m; print(m.__file__)' 2>/dev/null || true)"
say "      Engine importable from the venv."
if [[ "$MODE" == "system" ]]; then
  case "$ENGINE_PATH" in
    /usr/lib/python3*|/usr/lib64/python3*)
      say "      Source: distribution package. Licensed codecs expected." ;;
    "")
      : ;;
    *)
      say "      Source: $ENGINE_PATH"
      say "      That is a pip install shadowing the distro package, so it may"
      say "      lack H.264 and AAC. The codec check below reports the truth." ;;
  esac
fi

# python-vlc is only useful if libvlc is on the system already
if command -v vlc >/dev/null 2>&1 || [[ -e /usr/lib/x86_64-linux-gnu/libvlc.so.5 ]]; then
  if ask "      libVLC found. Add the python-vlc bindings to the venv (optional)?"; then
    "$VPY" -m pip install python-vlc >/dev/null 2>&1 \
      && say "      python-vlc installed. In-process mode is available but off by default." \
      || say "      python-vlc install failed; the process-separated modes still work."
  fi
fi

# -------------------------------------------------------------------- copy
echo
step 4 "Copying files to $LIB"
status "Copying the merlin package"
rm -rf "$LIB/merlin"
cp -r "$SRC/merlin" "$LIB/merlin"
say "$(find "$LIB/merlin" -type f | wc -l) files copied"
[[ -f "$SRC/README.md" ]] && cp "$SRC/README.md" "$LIB/README.md"

cp "$SRC/merlin-run.py" "$LIB/merlin-run.py"
# version.txt is the only place the version is written, so the installed copy
# needs it: without it the browser reports 0.0.0
[[ -f "$SRC/version.txt" ]] && cp "$SRC/version.txt" "$LIB/version.txt"

cat > "$LIB/merlin-browser" <<LAUNCH
#!/usr/bin/env bash
exec "$VENV/bin/python" "$LIB/merlin-run.py" "\$@"
LAUNCH
chmod +x "$LIB/merlin-browser"
mkdir -p "$BIN"
ln -sf "$LIB/merlin-browser" "$BIN/merlin-browser"
status "Verifying that Merlin starts"
if ! VERSION_LINE="$("$VPY" "$LIB/merlin-run.py" --version 2>&1)"; then
  say "      Merlin will not start:"
  printf '%s\n' "$VERSION_LINE"
  exit 1
fi
say "      $VERSION_LINE starts correctly."
say "      Launcher: $BIN/merlin-browser"

# ------------------------------------------------------------------- icons
mkdir -p "$ICONS" "$APPS"
if [[ -f "$SRC/merlin/merlin.png" ]]; then
  mkdir -p "$HOME/.local/share/icons/hicolor/256x256/apps"
  cp "$SRC/merlin/merlin.png" \
     "$HOME/.local/share/icons/hicolor/256x256/apps/merlin-browser.png"
fi
if [[ -f "$SRC/merlin/merlin.svg" ]]; then
  cp "$SRC/merlin/merlin.svg" "$ICONS/merlin-browser.svg"
fi

echo
step 5 "Desktop entries"
for entry in merlin-browser.desktop merlin-browser-frameless.desktop; do
  [[ -f "$SRC/$entry" ]] || continue
  sed "s|^Exec=merlin-browser|Exec=$BIN/merlin-browser|" "$SRC/$entry" > "$APPS/$entry"
done
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && \
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

# ------------------------------------------------------------- codec check
echo
step 6 "Asking the engine what it can decode"
status "Probing the engine for codec support"
CODEC_OUT="$(QT_QPA_PLATFORM=offscreen timeout 90 "$VPY" "$LIB/merlin-run.py" --codecs 2>/dev/null || true)"
if grep -q "H.264" <<<"$CODEC_OUT"; then
  H264_LINE="$(grep "H.264" <<<"$CODEC_OUT")"
  if grep -qE "yes" <<<"$H264_LINE"; then
    say "      H.264 and AAC: available in the engine."
  else
    say "      H.264 and AAC: NOT in this engine build."
    say "      Patent licensing, not a bug. A media player covers them."
  fi
else
  say "      Could not probe the engine here; run merlin-browser --codecs later."
fi

# ------------------------------------------------------------------ player
echo
step 7 "Media player"
PLAYER=""
for candidate in mpv vlc mplayer; do
  if command -v "$candidate" >/dev/null 2>&1; then PLAYER="$candidate"; break; fi
done
if [[ -n "$PLAYER" ]]; then
  say "      Found $PLAYER. H.264, HEVC and AAC will play through it."
else
  say "      No player found. Install one, both are C and run out of process:"
  say "          sudo apt install mpv        # or vlc"
  say "          pip install --user yt-dlp   # for streaming sites"
fi

# -------------------------------------------------------------- uninstaller
cat > "$LIB/uninstall.sh" <<UNINST
#!/usr/bin/env bash
set -euo pipefail
echo "Removing Merlin Browser..."
rm -f "$BIN/merlin-browser"
rm -f "$APPS/merlin-browser.desktop" "$APPS/merlin-browser-frameless.desktop"
rm -f "$ICONS/merlin-browser.svg"
rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/merlin-browser.png"
read -r -p "Delete bookmarks, history and settings too? [y/N] " reply
if [[ "\${reply,,}" == "y" ]]; then
  rm -rf "\$HOME/.config/merlin" "\$HOME/.local/share/merlin" "\$HOME/.cache/merlin"
  echo "Profile deleted."
else
  echo "Profile kept in \$HOME/.config/merlin"
fi
rm -rf "$LIB"
echo "Done."
UNINST
chmod +x "$LIB/uninstall.sh"

CURRENT_STEP=$TOTAL_STEPS
status "Done"
ui_done
echo
echo "  ==========================================================="
echo "   Installed version ${SRC_VERSION:-unknown}. Mode: $MODE"
echo "  ==========================================================="
echo
say "Launch           merlin-browser"
say "No decorations   merlin-browser --no-decorations"
say "Codec report     merlin-browser --codecs"
say "Uninstall        $LIB/uninstall.sh"
echo
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say "NOTE: $BIN is not on your PATH. Add it to ~/.profile." ;;
esac
say "Default browser  xdg-settings set default-web-browser merlin-browser.desktop"
echo
