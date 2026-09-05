#!/usr/bin/env bash
# Finish a run_batch.sh output directory that died part-way through: run every
# case that has no .pdf yet, with the exact same invocation run_batch.sh uses so
# the results stay comparable to the 1.5.0 baseline. Two lanes have now been
# stopped mid-batch, and re-running the 4 that already finished wastes 15 min.
set -u
cd "$(dirname "$0")/.."
out="${1:-out/v160b}"
mkdir -p "$out"
export YTSCORE_DIAG=1
export PATH="$HOME/.deno/bin:$PATH"

run() {                     # run <name> <url> <workdir>
  local name="$1" url="$2" work="$3"
  [ -f "$out/$name.pdf" ] && { echo "skip=$name (already done)" >> "$out/batch.log"; return; }
  python3 -m ytscore.pipeline "$url" --work "$work" --out "$out" --name "$name" \
      --title "$name" > "$out/$name.log" 2>&1
  echo "exit=$? $name" >> "$out/batch.log"
}

n=0
while read -r name url; do
  [ -z "${name:-}" ] && continue
  run "$name" "$url" "work/acc/$name" < /dev/null &
  n=$((n + 1))
  if [ $((n % 3)) -eq 0 ]; then wait; fi
done < work/acc/urls.txt
wait

run case0-original      "https://youtu.be/2RIsnf--0VY"  work/case0 < /dev/null &
run case1-two-line-top  "https://youtu.be/lLKmEm1g078"  work/case1 < /dev/null &
run case2-dark-inverted "https://youtu.be/RIlZto_0j8w"  work/case2 < /dev/null &
wait
run case3-translucent   "https://youtu.be/4M0XHHuAexI"  work/case3 < /dev/null &
run ling                "https://youtu.be/G1J0ZLF8fI8"  work/ling  < /dev/null &
run caseA-crop          "https://youtu.be/LtNIc3oinEs"  work/caseA < /dev/null &
wait
run caseB-colour        "https://youtu.be/umZcjiNpEOw"  work/caseB < /dev/null
echo "ALL DONE" >> "$out/batch.log"
