#!/usr/bin/env bash
# Re-run the acceptance set (and the four earlier samples) through the current
# pipeline. Videos are already in work/, so nothing is downloaded; a run is
# ~2-5 minutes of ffmpeg decode + compositing. 3 at a time keeps 8 cores busy
# without thrashing memory (each run holds ~140 full frames).
set -u
cd "$(dirname "$0")/.."
out="${1:-out/acc2}"
mkdir -p "$out"
export YTSCORE_DIAG=1
export PATH="$HOME/.deno/bin:$PATH"

run() {                     # run <name> <url> <workdir>
  local name="$1" url="$2" work="$3"
  python3 -m ytscore.pipeline "$url" --work "$work" --out "$out" --name "$name" \
      --title "$name" > "$out/$name.log" 2>&1
  echo "exit=$? $name" >> "$out/batch.log"
}

touch "$out/batch.log"
# read the list up front: a backgrounded job shares this shell's stdin, and
# ffmpeg used to swallow the rest of the file (fixed with -nostdin, belt and
# braces here).
mapfile -t JOBS < work/acc/urls.txt
n=0
for line in "${JOBS[@]}"; do
  set -- $line
  [ -z "${1:-}" ] && continue
  only="${YTSCORE_ONLY:-}"
  if [ -n "$only" ] && [[ " $only " != *" $1 "* ]]; then continue; fi
  run "$1" "$2" "work/acc/$1" < /dev/null &
  n=$((n + 1))
  if [ $((n % 3)) -eq 0 ]; then wait; fi
done
wait
[ -n "${YTSCORE_ONLY:-}" ] && { cat "$out/batch.log"; exit 0; }

# the four samples the customer already saw: proof the change did not regress them
run case0-original    "https://youtu.be/2RIsnf--0VY" work/case0 < /dev/null &
run case1-two-line-top "https://youtu.be/lLKmEm1g078" work/case1 < /dev/null &
run case2-dark-inverted "https://youtu.be/RIlZto_0j8w" work/case2 < /dev/null &
wait
run case3-translucent "https://youtu.be/4M0XHHuAexI" work/case3 < /dev/null
cat "$out/batch.log"
