#!/bin/bash
set -e

# 1. Clean up and clone the script repository
echo "[*] Fetching redroid-script..."
rm -rf /tmp/redroid
git clone --depth 1 https://github.com/ayasa520/redroid-script.git /tmp/redroid

# 2. Run the patcher using uv
# -a 14.0.0: Android 14
# -m: Magisk
# -n: libndk (ARM translation for x86_64)
# -i: Install houdini (translation)
# -lg: LiteGapps
# -mtg: MindTheGapps
# -w: Widevine
# Dont need Widevine for now & libndk only for And 11/12
echo "[*] Patching Android 14 image (this may take a few minutes)..."
cd /tmp/redroid
uv run --with requests --with tqdm redroid.py -a 14.0.0 -lg -i -m
cd -

DOCKER_NAME=$(docker image ls "redroid/redroid:*" --format "{{.Repository}}:{{.Tag}}")
echo "[*] Found:"
echo "$DOCKER_NAME"

# 3. Build the Research Layer
echo "[*] Building Android Research Image..."
docker compose build

