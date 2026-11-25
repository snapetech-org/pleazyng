#!/usr/bin/env bash
set -euo pipefail
work="$(mktemp -d "$PWD/.pleazyng-dmg.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir "$work/staging"
cp -R build/macos/Build/Products/Release/Pleazyng.app "$work/staging/"
ln -s /Applications "$work/staging/Applications"

image="$work/pleazyng-macos.dmg"
for attempt in 1 2 3 4 5; do
  if hdiutil create -format ULMO -srcfolder "$work/staging" -volname "Pleazyng" "$image" > "$work/hdiutil.log" 2>&1; then
    cat "$work/hdiutil.log"
    mv -f "$image" pleazyng-macos.dmg
    exit 0
  else
    status=$?
  fi
  cat "$work/hdiutil.log" >&2
  if [[ "$attempt" == 5 ]] || ! grep -Fq 'create failed - Resource busy' "$work/hdiutil.log"; then
    exit "$status"
  fi
  rm -f "$image"
  delay=$((2 ** attempt))
  printf 'Disk image creation is busy. Retrying in %s seconds.\n' "$delay" >&2
  sleep "$delay"
done
