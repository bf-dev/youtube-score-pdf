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

# 1.7.2's gate, and it runs FIRST because it costs two seconds and it is the one
# that was live-broken. The customer's output folder is HIS, and on 2026-09-05 it
# was `Desktop\유튜브악보`: cv2.imwrite cannot write outside the Windows ANSI code
# page, so build_pdf's temp page PNG went nowhere and insert_image took the blame
# for a missing file, after the whole 3-7 minute pipeline had already succeeded.
# Not a 1.7.1 regression, latent since 1.0.0. --assert-legacy is the red direction.
python3 ci/unicode_path_check.py 2>&1 | grep -v '^\[0' | tee -a "$out/batch.log"
echo "unicode_gate=${PIPESTATUS[0]} fixtures" >> "$out/batch.log"
python3 ci/unicode_path_check.py --assert-legacy 2>&1 | grep -v '^\[0' | tee -a "$out/batch.log"
echo "unicode_legacy_gate=${PIPESTATUS[0]} fixtures" >> "$out/batch.log"

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

# The customer's Ling Ling chart, plus the notehead-fill gate on it. Not
# optional, and deliberately not a count: the hollow-notehead defect shipped
# twice because system counts, page counts and strip-to-strip comparisons are
# all structurally blind to ink vanishing from INSIDE a glyph. The customer
# found it both times, by opening his own PDF. See ci/notehead_check.py.
run ling "https://youtu.be/G1J0ZLF8fI8" work/ling < /dev/null
python3 ci/notehead_check.py "$out/ling.pdf" 2>&1 | tee -a "$out/batch.log"
echo "notehead_gate=${PIPESTATUS[0]} ling" >> "$out/batch.log"

# 1.5.0's two gates, on the customer's own videos. Same reason as the notehead
# gate above: neither defect moved a system count or a page count.
#   caseA - a crop boundary cutting THROUGH the notation (beams amputated,
#           measure numbers sliced). ci/clip_check.py fails at 0.90 on the
#           broken strips and passes at 0.00 on the fixed ones.
#   caseB - coloured noteheads erased as overlay chrome. ci/colour_check.py
#           scores the shipped rule 1.00 against the pre-fix rule's 0.11;
#           --assert-legacy proves it can fail.
run caseA-crop "https://youtu.be/LtNIc3oinEs" work/caseA < /dev/null
python3 ci/clip_check.py "$out/caseA-crop_systems" 2>&1 | tee -a "$out/batch.log"
echo "clip_gate=${PIPESTATUS[0]} caseA-crop" >> "$out/batch.log"

run caseB-colour "https://youtu.be/umZcjiNpEOw" work/caseB < /dev/null
python3 ci/colour_check.py work/caseB/video.mp4 --band 141 1041 --gap 16 2>&1 \
    | tee -a "$out/batch.log"
echo "colour_gate=${PIPESTATUS[0]} caseB-colour" >> "$out/batch.log"

# 1.6.0's gate, on the customer's EVprtoI_3eY. A group whose frames were
# composited a whole staff space apart prints as a double exposure, and 40
# systems / 4 pages before and after means no count can see it. ci/ghost_check.py
# fails at share 0.05 on the preserved pre-fix strips (out/v150/caseD-fixed_systems)
# and passes at 0.00 here.
run caseD-ghost "https://youtu.be/EVprtoI_3eY" work/caseD < /dev/null
python3 ci/ghost_check.py "$out/caseD-ghost_systems" 2>&1 | tee -a "$out/batch.log"
echo "ghost_gate=${PIPESTATUS[0]} caseD-ghost" >> "$out/batch.log"

# 1.7.0's gate, on the customer's YkjcWb63v0o. The video scrolls its page out of
# the band at the end and the three frames caught in the slide each printed the
# last system again: "마지막장 같은마디 반복". Unlike the four defects above, THIS
# one moves the count -- 1.6.0 printed 23 systems on 3 pages -- so the count is
# asserted as well as the discriminator. ci/slide_check.py is the discriminator's
# own test and carries its own --assert-legacy proof.
# 21/2 is correct, and each of the three drops was read off the rendered page:
#   -3  the mid-slide copies of the last music system (sliding_groups)
#   -1  the faint broken copy of the outro system, his "깨짐" (INK_SHARE_FLOOR)
#   +2  bars 10 and 15, which no version before 1.7.0 ever printed (playhead)
run caseC-repeat "https://youtu.be/YkjcWb63v0o" work/caseC < /dev/null
python3 ci/slide_check.py 2>&1 | tee -a "$out/batch.log"
echo "slide_gate=${PIPESTATUS[0]} fixtures" >> "$out/batch.log"
python3 - "$out/caseC-repeat.run.json" <<'PY' 2>&1 | tee -a "$out/batch.log"
import json, sys
j = json.load(open(sys.argv[1]))
ok = ((j["systems"], j["pages"]) == (20, 2) and j.get("sliding_dropped") == 3
      and j.get("empty_dropped") == 1
      and len(j.get("fade_copies_dropped") or []) == 2)
print(f"caseC: {j['systems']} systems / {j['pages']} pages, "
      f"{j.get('sliding_dropped', 0)} mid-slide + {j.get('empty_dropped', 0)} empty "
      f"+ {len(j.get('fade_copies_dropped') or [])} fade "
      f"copies dropped (want 20/2, 3, 1 and 2)")
print("CASEC_OK" if ok else "CASEC_FAIL")
sys.exit(0 if ok else 1)
PY
echo "casec_gate=${PIPESTATUS[0]} caseC-repeat" >> "$out/batch.log"
python3 ci/blank_check.py "$out/caseC-repeat_systems" 2>&1 | tee -a "$out/batch.log"
echo "blank_gate=${PIPESTATUS[0]} caseC-repeat" >> "$out/batch.log"
# 1.7.1's gate. The SAME video prints its Intro twice at the top of page 1 and
# nothing on the page could see it: the fade pass missed by 0.005 on the ink
# ratio and the repeat pass by 0.003 on the picture. FADE_RATIO 0.78 -> 0.80
# takes it, and because that is a DELETE threshold getting wider, the gate
# asserts the whole surviving system list on three videos plus a no-collateral
# sweep over every run in this tree. ci/fade_sweep.py is the measurement behind
# the cut; ci/fade_check.py --assert-legacy is its red direction.
python3 ci/fade_check.py 2>&1 | grep -v '^\[' | tee -a "$out/batch.log"
echo "fade_gate=${PIPESTATUS[0]} fixtures" >> "$out/batch.log"
python3 ci/fade_check.py --assert-legacy 2>&1 | grep -v '^\[' | tee -a "$out/batch.log"
echo "fade_legacy_gate=${PIPESTATUS[0]} fixtures" >> "$out/batch.log"

# The customer's SECOND "마지막장 같은마디 반복" video, and it fails a different
# way: no slide at all, the last frame is a single-frame group whose picture
# matches the 58-frame group before it at 0.999 and it printed anyway because
# the header band came back empty on a fading frame (header distance 1.000).
# REPEAT_CERTAIN is what takes it; ci/repeat_check.py is its both-directions
# proof. 1.6.0 printed 15/2; 13/2 is correct, one copy off the repeat and one
# off his OTHER complaint on the same video, "맨윗줄 공백": system 000 was the
# uploader's intro panel, a white box with 0.4% ink. INK_SHARE_FLOOR takes it
# and ci/blank_check.py is that rule's gate.
run caseE-repeat2 "https://youtu.be/KsSlNq-ciko" work/caseE < /dev/null
python3 - "$out/caseE-repeat2.run.json" <<'PY' 2>&1 | tee -a "$out/batch.log"
import json, sys
j = json.load(open(sys.argv[1]))
ok = (j["systems"], j["pages"]) == (13, 2) and j.get("empty_dropped") == 1
print(f"caseE: {j['systems']} systems / {j['pages']} pages, "
      f"{j.get('empty_dropped', 0)} empty box dropped (want 13/2 and 1)")
print("CASEE_OK" if ok else "CASEE_FAIL")
sys.exit(0 if ok else 1)
PY
echo "casee_gate=${PIPESTATUS[0]} caseE-repeat2" >> "$out/batch.log"
python3 ci/repeat_check.py 2>&1 | tee -a "$out/batch.log"
echo "repeat_gate=${PIPESTATUS[0]} fixtures" >> "$out/batch.log"
python3 ci/blank_check.py "$out/caseE-repeat2_systems" 2>&1 | tee -a "$out/batch.log"
echo "blank_gate=${PIPESTATUS[0]} caseE-repeat2" >> "$out/batch.log"

# d3t9j6DObN0, the OPPOSITE failure and the one no similarity metric can see:
# `group_lines` merged five 8-second screens into ONE 160-frame group (every
# other content group on this video holds exactly 32) because the video engraves
# the same groove five systems running, so four whole systems never became
# candidates and the one that printed was a median of all five with a broken
# ghost fill in its last bar. The playhead is what separates them: it sweeps
# +0.029 per sample and jumps -0.89 at each boundary. 1.6.0 printed 23/3, 27/3
# is correct. ci/playhead_check.py is the discriminator's both-directions proof.
run caseF-merged "https://youtu.be/d3t9j6DObN0" work/caseF < /dev/null
python3 ci/playhead_check.py 2>&1 | tee -a "$out/batch.log"
echo "playhead_gate=${PIPESTATUS[0]} fixtures" >> "$out/batch.log"
python3 - "$out/caseF-merged.run.json" <<'PY' 2>&1 | tee -a "$out/batch.log"
import json, sys
j = json.load(open(sys.argv[1]))
ok = (j["systems"], j["pages"]) == (27, 3)
print(f"caseF: {j['systems']} systems / {j['pages']} pages (want 27/3)")
print("CASEF_OK" if ok else "CASEF_FAIL")
sys.exit(0 if ok else 1)
PY
echo "casef_gate=${PIPESTATUS[0]} caseF-merged" >> "$out/batch.log"

# ...and the empty-box rule over the WHOLE corpus, not just the two videos it was
# written for. a09-kimyongtae was printing two invisible white boxes (strips 000
# and 041) that nobody had noticed for five versions, so this sweep is the part
# that says "and nowhere else".
python3 ci/blank_check.py "$out"/*_systems 2>&1 | tail -25 | tee -a "$out/batch.log"
echo "blank_gate=${PIPESTATUS[0]} corpus" >> "$out/batch.log"

# ...and the same "and nowhere else" for the 1.7.1 fade cut, replayed over every
# run in this tree: widening a DELETE threshold may remove the caseC Intro copy
# at t=8.0s and NOTHING ELSE anywhere in the corpus.
python3 ci/fade_check.py --corpus "$out" 2>&1 | grep -v '^\[' | tee -a "$out/batch.log"
echo "fade_corpus_gate=${PIPESTATUS[0]} corpus" >> "$out/batch.log"

cat "$out/batch.log"
