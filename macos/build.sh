#!/bin/sh
# Build macos/build/Build/Products/{Debug,Release}/ltxq.app
#   ./build.sh            → Release
#   ./build.sh Debug      → Debug
set -e
cd "$(dirname "$0")"
CONFIG="${1:-Release}"
xcodebuild -project Ltxq.xcodeproj -target Ltxq -configuration "$CONFIG" \
    SYMROOT="$(pwd)/build/products" build \
    | tail -n 5
echo "→ build/products/$CONFIG/ltxq.app"
