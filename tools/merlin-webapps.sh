#!/usr/bin/env bash
# Merlin versions of the Brave web-app entries.
#
#   --app=URL                    ->  --app URL
#   --new-window                 ->  not needed, an app always gets its own
#   --disable-frame              ->  not needed, --app is frameless already
#   --force-device-scale-factor  ->  --zoom
#
# --app-name gives each one its own WM_CLASS, so the desktop shows it under its
# own icon instead of grouping them all under Merlin.

APP_DIR="${APP_DIR:-$HOME/.local/share/applications}"
mkdir -p "$APP_DIR"

cat <<EOF > "$HOME/.local/share/applications/openmap.desktop"
[Desktop Entry]
Version=1.0
Type=Application
Name=OpenStreetMap
Comment=OpenStreetMap WebApp (Merlin)
Exec=merlin-browser --app https://openstreetmap.org/ --app-name openmap --zoom 1.3
Icon=openmap
Terminal=false
Categories=Network;WebBrowser;Utility;
StartupNotify=true
StartupWMClass=openmap
EOF

cat <<EOF > "$HOME/.local/share/applications/breakout.desktop"
[Desktop Entry]
Version=1.0
Type=Application
Name=Breakout
Comment=Break the block game
Exec=merlin-browser --app "file://$HOME/extras/games/breakout.html" --app-name breakout --zoom 1.3
Icon=breakout
Terminal=false
Categories=Game;
StartupNotify=true
StartupWMClass=breakout
EOF

cat <<EOF > "$HOME/.local/share/applications/alternitech-forums.desktop"
[Desktop Entry]
Version=1.0
Type=Application
Name=Alternitech Forums
Comment=Alternitech Community Forum WebApp (Merlin)
Exec=merlin-browser --app https://alternitech.freeforums.net/ --app-name alternitech-forums --zoom 1.3
Icon=alternitech-forum
Terminal=false
Categories=Network;WebBrowser;Utility;
StartupNotify=true
StartupWMClass=alternitech-forums
EOF

cat <<EOF > "$APP_DIR/youtube-webapp.desktop"
[Desktop Entry]
Version=1.0
Type=Application
Name=Youtube
Comment=Youtube WebApp (Merlin)
Exec=merlin-browser --app https://www.youtube.com/ --app-name youtube --zoom 1.3
Icon=youtube
Terminal=false
Categories=Network;WebBrowser;Utility;
StartupNotify=true
StartupWMClass=youtube
EOF

update-desktop-database "$APP_DIR" 2>/dev/null || true
