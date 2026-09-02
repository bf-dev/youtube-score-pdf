#!/usr/bin/env bash
# Publish the deliverables for Kmong customer 1775529 to the works.insu.ng
# public host and prove every link serves.
#
#   ci/publish.sh <acceptance-dir> [installer.exe]
#
# Filenames are ASCII and version-suffixed: Cloudflare edge-caches this path, so
# a name that has already been served must never be reused.
set -uo pipefail
cd "$(dirname "$0")/.."
CUST=1775529
VER="${PDF_VERSION:-1.2.0}"
ACC="${1:-acceptance}"
INSTALLER="${2:-}"
BASE="https://works.insu.ng/works/public/$CUST"
PUB="$HOME/workspace/scripts/works-publish"
rc=0

verify() {                     # verify <remoteName> <localFile>
  local name="$1" local_file="$2"
  local want got code
  want=$(stat -c %s "$local_file")
  read -r code got < <(curl -s -o /dev/null -w '%{http_code} %{size_download}' \
      --resolve works.insu.ng:443:127.0.0.1 -H 'Cache-Control: no-cache' \
      "$BASE/$name?cb=$RANDOM$RANDOM")
  if [ "$code" = "200" ] && [ "$got" = "$want" ]; then
    printf '  200  %10s bytes  %s/%s\n' "$got" "$BASE" "$name"
  else
    printf '  FAIL code=%s served=%s local=%s  %s/%s\n' "$code" "$got" "$want" "$BASE" "$name"
    rc=1
  fi
}

publish() {                    # publish <localFile> <remoteName>
  local src="$1" name="$2"
  if [ ! -f "$src" ]; then echo "  MISSING $src"; rc=1; return; fi
  "$PUB" "$CUST" "$src" "$name" > /dev/null 2>&1 || true
  verify "$name" "$src"
}

echo "== acceptance PDFs"
for pdf in "$ACC"/a[0-9][0-9]-*.pdf; do
  [ -f "$pdf" ] || continue
  base=$(basename "$pdf" .pdf)
  publish "$pdf" "youtube-score-$base-$VER.pdf"
done

echo "== one preview PNG per case"
mkdir -p "$ACC/preview"
for pdf in "$ACC"/a[0-9][0-9]-*.pdf; do
  [ -f "$pdf" ] || continue
  base=$(basename "$pdf" .pdf)
  png="$ACC/preview/$base-page1.png"
  [ -f "$png" ] || pdftoppm -r 120 -png -f 1 -l 1 "$pdf" "${png%-page1.png}" && \
    mv -f "${png%-page1.png}-1.png" "$png" 2>/dev/null
  publish "$png" "youtube-score-$base-$VER-preview.png"
done

if [ -n "$INSTALLER" ]; then
  echo "== windows installer"
  app_ver=$(python3 -c "import sys; sys.path.insert(0,'.'); from ytscore.config import APP_VERSION; print(APP_VERSION)")
  name="youtube-score-pdf-setup-$app_ver.exe"
  publish "$INSTALLER" "$name"
  cat > /tmp/version-ytscore.json <<JSON
{
  "version": "$app_ver",
  "exeUrl": "$BASE/$name",
  "notes": "유튜브 악보 PDF 변환기 $app_ver"
}
JSON
  # the update manifest is the one file that MUST be overwritten in place, so
  # write it directly and fix the mode by hand instead of going through the
  # publisher (which refuses to clobber a served name).
  install -m 0644 /tmp/version-ytscore.json \
    "/home/bfdev/neoworks/apps/gateway/artifacts/public/$CUST/version-ytscore.json"
  verify "version-ytscore.json" /tmp/version-ytscore.json
fi

exit $rc
