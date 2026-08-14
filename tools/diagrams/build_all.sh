#!/usr/bin/env bash
# Собирает все схемы из описаний и рисует их в PNG и SVG.
#   ./build_all.sh                 — все схемы из schemes/
#   ./build_all.sh schemes/x.yaml  — одну
set -euo pipefail
cd "$(dirname "$0")"

FILES=("$@")
if [ ${#FILES[@]} -eq 0 ]; then
  # схемы можно раскладывать по подпапкам — по проекту или по процессу
  while IFS= read -r line; do FILES+=("$line"); done \
    < <(find schemes -name '*.yaml' | sort)
fi

python3 build.py "${FILES[@]}"
npm run bundle --silent
for f in "${FILES[@]}"; do
  name=$(basename "$f" .yaml)
  node render.build.mjs "out/${name}.excalidraw" "out/${name}.png" 2000 \
    2>/dev/null | tail -1
done
