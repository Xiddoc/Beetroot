#!/bin/bash
set -e

# Usage: ./scripts/setup.sh [none|lite|full|mindthegapps]
# Default: lite (LiteGapps — same as before)
VARIANT="${1:-lite}"

case "$VARIANT" in
    lite)
        GAPPS_FLAG="-lg"
        GAPPS_SLUG="_litegapps"
        ;;
    full)
        GAPPS_FLAG="-g"
        GAPPS_SLUG="_gapps"
        ;;
    mindthegapps)
        GAPPS_FLAG="-mtg"
        GAPPS_SLUG="_mindthegapps"
        ;;
    none)
        GAPPS_FLAG=""
        GAPPS_SLUG=""
        ;;
    *)
        echo "Unknown gapps variant: $VARIANT"
        echo "Valid values: none, lite (default), full, mindthegapps"
        exit 1
        ;;
esac

BASE_IMAGE="redroid/redroid:14.0.0${GAPPS_SLUG}_houdini_magisk"

# 1. Clean up and clone the script repository
echo "[*] Fetching redroid-script..."
rm -rf /tmp/redroid
git clone --depth 1 https://github.com/ayasa520/redroid-script.git /tmp/redroid

# 2. Run the patcher using uv
# -a 14.0.0: Android 14
# -m: Magisk
# -i: Install houdini (ARM translation)
# -lg: LiteGapps / -g: full gapps / -mtg: MindTheGapps / (omitted): no gapps
echo "[*] Patching Android 14 image (variant: $VARIANT, this may take a few minutes)..."
cd /tmp/redroid
# shellcheck disable=SC2086
uv run --with requests --with tqdm python -W ignore redroid.py -a 14.0.0 $GAPPS_FLAG -i -m
cd -

echo "[*] Base image: $BASE_IMAGE"

# 3. Build the Research Layer on top of the freshly produced base image
echo "[*] Building Android Research Image..."
BASE_IMAGE="$BASE_IMAGE" docker compose build
