#!/usr/bin/env bash
# Re-crawl a site using the base URL + feed list recorded in its existing
# index, then rebuild its knowledge digest. Cron-friendly: exits nonzero if
# the crawl or the digest build fails (build_knowledge.py keeps the previous
# knowledge.md on failure).
#
# Usage: scripts/refresh_site.sh <site> [extra crawl_site.py args]
#   e.g. scripts/refresh_site.sh spacechannel --max-pages 50
set -euo pipefail

site="${1:?usage: refresh_site.sh <site> [crawler args]}"
shift || true

root="$(cd "$(dirname "$0")/.." && pwd)"
index="$root/data/$site/site_index.json"
if [ ! -f "$index" ]; then
  echo "No $index — run scripts/crawl_site.py once first to seed it." >&2
  exit 1
fi

python="$root/.venv-test/bin/python"
[ -x "$python" ] || python="python3"

base="$("$python" -c "import json,sys; print(json.load(open(sys.argv[1]))['base'])" "$index")"

feed_args=()
while IFS= read -r feed; do
  [ -n "$feed" ] && feed_args+=(--feed "$feed")
done < <("$python" -c "import json,sys; [print(u) for u in json.load(open(sys.argv[1])).get('feeds', {})]" "$index")

# ${arr[@]+...} keeps empty-array expansion safe under `set -u` on bash 3.2
# (macOS system bash, which cron's default PATH resolves).
echo "Refreshing $site from $base ($((${#feed_args[@]} / 2)) feeds)"
"$python" "$root/scripts/crawl_site.py" "$base" --name "$site" \
  ${feed_args[@]+"${feed_args[@]}"} "$@"
"$python" "$root/scripts/build_knowledge.py" "$site"
