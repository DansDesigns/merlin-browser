#!/usr/bin/env bash
# Rename the whole project in one shot:  ./rename.sh shrike
# Branding lives in <pkg>/brand.py, so this only has to move files and rewrite
# the identifiers that are baked into paths.
set -euo pipefail

[[ $# -eq 1 ]] || { echo "usage: $0 <newname>   (lowercase, no spaces)"; exit 1; }
NEW="$(echo "$1" | tr '[:upper:]' '[:lower:]')"
CAP="$(python3 -c "print('$NEW'.capitalize())")"
OLD="$(grep -ohP 'APP_SLUG = "\K[a-z]+' ./*/brand.py | head -1)"
OLDCAP="$(python3 -c "print('$OLD'.capitalize())")"

[[ -n "$OLD" ]] || { echo "Could not find brand.py"; exit 1; }
[[ "$NEW" != "$OLD" ]] || { echo "Already named $OLD"; exit 0; }

echo ">> $OLDCAP -> $CAP"
mv "$OLD" "$NEW"
[[ -f "$OLD-browser" ]] && mv "$OLD-browser" "$NEW-browser"

files=()
while IFS= read -r f; do files+=("$f"); done < <(
  find . -maxdepth 2 -type f \
       \( -name '*.py' -o -name '*.sh' -o -name '*.md' -o -name '*.txt' \
          -o -name '*.desktop' -o -name "$NEW-browser" \) -not -path './.git/*'
)
for f in "${files[@]}"; do
  sed -i "s/\b$OLD\b/$NEW/g; s/\b$OLDCAP\b/$CAP/g" "$f"
done

for d in *.desktop; do
  [[ -f "$d" && "$d" == *"$OLD"* ]] && mv "$d" "${d//$OLD/$NEW}"
done

echo ">> Done."
echo ">> Config now lives in ~/.config/$NEW; move ~/.config/$OLD across to keep"
echo "   your history, bookmarks and settings."
