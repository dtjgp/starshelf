#!/bin/bash
# Build "Starshelf.app" from this folder and (optionally) install it into /Applications.
#   ./build_app.sh            build into ./build
#   ./build_app.sh --install  build and copy to /Applications
set -euo pipefail
cd "$(dirname "$0")"
SRC="$(pwd)"
ROOT="$(cd .. && pwd)"
APP="$SRC/build/Starshelf.app"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# universal binary: Apple silicon + Intel
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for arch in arm64 x86_64; do
  xcrun swiftc -O -swift-version 5 -target "$arch-apple-macos14.0" -o "$TMP/Starshelf-$arch" "$SRC/main.swift"
done
lipo -create "$TMP/Starshelf-arm64" "$TMP/Starshelf-x86_64" -output "$APP/Contents/MacOS/Starshelf"
cp "$SRC/Info.plist" "$APP/Contents/Info.plist"
cp "$SRC/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp "$ROOT/star_lists.py" "$ROOT/star_lists_ai.py" "$ROOT/star_lists_ui.py" "$ROOT/star_lists_ui.html" "$APP/Contents/Resources/"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
echo "built: $APP"

if [ "${1:-}" = "--install" ]; then
  ditto "$APP" "/Applications/Starshelf.app"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "/Applications/Starshelf.app" || true
  echo "installed: /Applications/Starshelf.app"
fi
