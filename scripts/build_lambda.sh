#!/usr/bin/env bash
# Builds the Lambda package into build/lambda for arm64, Python 3.13 on Amazon Linux 2023 (glibc 2.34).
set -euo pipefail
cd "$(dirname "$0")/.."

out=build/lambda
rm -rf "$out" && mkdir -p "$out"
uv export --frozen --no-dev --no-hashes --no-emit-project -o build/requirements.txt -q
uv pip install -q \
  --target "$out" \
  --python-version 3.13 \
  --python-platform aarch64-manylinux_2_28 \
  --only-binary :all: \
  -r build/requirements.txt
cp -r src/wine_cellar "$out/"
find "$out" -name "__pycache__" -type d -prune -exec rm -rf {} +
echo "Built $out ($(du -sh "$out" | cut -f1))"
