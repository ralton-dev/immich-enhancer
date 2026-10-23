#!/usr/bin/env bash
# Download the model weights the image bakes in and verify each one against a
# pinned sha256. The pod has no internet access, so every file the pipeline
# loads has to be here at build time: the two Real-ESRGAN upscalers that
# ENHANCER_SR_MODEL chooses between, both BSD-3-Clause.
#
#   scripts/fetch-weights.sh [target-dir]      default: /opt/models
#
# A file that is already present with the right hash is kept, so the script is
# safe to rerun over a cache (CI restores one keyed on the hashes below). A
# download whose hash does not match is deleted and the script exits non-zero.
#
# The hashes were recorded from the first fetch on 2026-10-23 and cross-checked
# against independent Hugging Face mirrors of the same files (LFS sha256).
# GitHub publishes no digests for these release assets.
set -euo pipefail

dest=${1:-/opt/models}

# <sha256> <path under target-dir> <url>
weights() {
  cat <<'LIST'
4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1 RealESRGAN_x4plus.pth https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth
8dc7edb9ac80ccdc30c3a5dca6616509367f05fbc184ad95b731f05bece96292 realesr-general-x4v3.pth https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth
LIST
}

if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d' ' -f1; }
else # macOS
  sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
fi

failed=0
while read -r want rel url; do
  file="$dest/$rel"
  mkdir -p "$(dirname "$file")"
  if [ -f "$file" ] && [ "$(sha256 "$file")" = "$want" ]; then
    echo "ok      $rel (already present)"
    continue
  fi
  echo "fetch   $rel"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --retry 5 --retry-delay 3 --output "$file.part" "$url"
  got=$(sha256 "$file.part")
  if [ "$got" != "$want" ]; then
    echo "MISMATCH $rel: expected $want, got $got" >&2
    rm -f "$file.part"
    failed=1
    continue
  fi
  mv "$file.part" "$file"
  echo "ok      $rel"
done < <(weights)

[ "$failed" = 0 ] || exit 1

# World-readable: the image runs as uid 1000 and the files are owned by root.
chmod -R a+rX "$dest"
echo "all $(weights | wc -l | tr -d ' ') weights verified in $dest"
