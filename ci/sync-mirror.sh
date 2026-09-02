#!/usr/bin/env bash
# Copy the buildable subset of this project into .mirror/ and push it to the
# PUBLIC GitHub repo that GitHub Actions builds from.
#
# Why a mirror instead of pushing this repo: Actions minutes are only free on a
# public repo, and this project's own history carries NOTES.md and
# metadata.json, which name the customer and hold the internal quality notes.
# The mirror carries code only.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
mirror="$root/.mirror"
repo="${YTSCORE_MIRROR_REPO:-git@github.com:bf-dev/youtube-score-pdf.git}"

mkdir -p "$mirror"
rsync -a --delete \
  --exclude '.git' \
  "$root/main.py" "$root/requirements.txt" "$root/_읽어주세요.txt" "$mirror/"
rsync -a --delete --exclude '__pycache__' "$root/ytscore" "$root/ci" "$root/assets" "$mirror/"
mkdir -p "$mirror/.github"
rsync -a --delete "$root/.github/workflows" "$mirror/.github/"

cat > "$mirror/.gitignore" <<'GIT'
__pycache__/
build/
dist/
installer/
out/
bin/
screenshots/
*.spec
GIT

cd "$mirror"
[ -d .git ] || { git init -b main; git remote add origin "$repo"; }
git add -A
if git diff --cached --quiet; then echo "mirror: nothing to push"; exit 0; fi
git -c user.name="bf-dev" -c user.email="bfdev.main@gmail.com" \
    commit -m "${1:-sync from the project repo}" -q
git push -u origin main "${2:-}"
echo "mirror: pushed to $repo"
