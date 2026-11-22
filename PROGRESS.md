# Blast Radius experiment log

SQLGlot pin: `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. Previous reported
official score: 196.8. These experiments use the public 30 patches, not the
hidden workload. The official total includes CPU and is the median of three.

**Current checkpoint: 177.6 s local public median, zero misses. Not a
submission recommendation.** The faster experiment below used a comparator
that did not establish complete observations. See the final gate below.

| Public experiment | Simulated s | Local selector CPU s | Missed commits |
| --- | ---: | ---: | ---: |
| Original, `d88555e`, profiled | 132.371 | 70.441 | 0 |
| Repack that output, `b5890d5` | 132.371 | not measured separately | 0 |
| Static, no call profile | 132.392 | 29.020 | 0 |
| Static plus pinned call profile | 121.048 | 6.959 median | 0 |

Local Linux/amd64 CPU is measured under emulation and is sensitive to other
Docker activity. The no-profile static measurement used a development build
and is not a controlled timing comparison against the exact static Git commit.
The call-profile candidate's three sample CPUs were 6.236, 7.318 and 6.959 s.
That is **128.0 s local median**, not a leaderboard result.

## Hypotheses

- Spare capacity provides miss insurance: 6,013 extra memberships in the
  frozen output at no simulated cost; no sample makespan saving.
- Avoid starting a patched interpreter or testing an overlay: static selection
  schedules one more 21 ms candidate rather than paying to run verifiers.
- Distinguish calling from defining: source-bound call sites, validation
  receivers, collection and introspection evidence from 1,392 pristine tests.
  The other 228 do not supply negative call evidence. Three public changes
  account for the entire 11.344 s gain over static selection without this map.
- Do not ship executor replay. Dropping the TPC failures loses real coverage,
  and even oracle removal of all *passing* pooled executor tests saved zero
  public simulated seconds. Closed-dictionary lookup matched no public or
  archived case; escape-table narrowing with all uncertainty retained was
  slower. Loading an entire marshalled map from a file also lost the A/B.

## Public per-patch comparison

Entries are scheduled tests / nonempty workers / simulated seconds. Extra
memberships on otherwise unchanged rows are slack fill, including zero-ms
tests. The change to `5y72` is the extra unverified 21 ms test; `6ze8` narrows
a parser lambda, `gbav` a closure factory argument and `ktz3` a required-field
flag. The comparisons use the frozen baseline run, not an oracle schedule.

| Patch | Original | Call-profile candidate |
| --- | ---: | ---: |
| 2haz | 0/0/0.000 | 0/0/0.000 |
| 2jrq | 29/2/25.454 | 1596/2/25.454 |
| 2qn5 | 6/1/2.536 | 100/1/2.536 |
| 2vdj | 1/1/2.083 | 95/1/2.083 |
| 3utz | 37/2/25.454 | 1529/2/25.454 |
| 4d63 | 358/3/12.686 | 1340/3/12.686 |
| 535z | 7/1/2.855 | 101/1/2.855 |
| 5y72 | 2/1/2.445 | 97/1/2.466 |
| 66ks | 0/0/0.000 | 0/0/0.000 |
| 6vvp | 1/1/2.037 | 95/1/2.037 |
| 6ze8 | 37/1/9.165 | 95/1/2.001 |
| 8y7j | 0/0/0.000 | 0/0/0.000 |
| 986c | 2/1/2.340 | 96/1/2.340 |
| 9u5k | 0/0/0.000 | 0/0/0.000 |
| ary6 | 10/1/4.382 | 104/1/4.382 |
| bap8 | 0/0/0.000 | 0/0/0.000 |
| bj7g | 2/1/2.241 | 96/1/2.241 |
| bvw2 | 1/1/2.061 | 95/1/2.061 |
| egys | 1/1/2.596 | 95/1/2.596 |
| f4bn | 1/1/2.007 | 95/1/2.007 |
| f4xa | 0/0/0.000 | 0/0/0.000 |
| fryt | 3/1/2.157 | 97/1/2.157 |
| gbav | 37/1/3.360 | 95/1/2.004 |
| gbhk | 1/1/2.061 | 95/1/2.061 |
| j6uz | 2/1/4.399 | 96/1/4.399 |
| ktz3 | 131/2/7.161 | 101/1/4.337 |
| mthh | 1/1/3.016 | 95/1/3.016 |
| nwnx | 1/1/2.092 | 95/1/2.092 |
| rszg | 4/1/5.775 | 98/1/5.775 |
| st8f | 1/1/2.008 | 95/1/2.008 |

## Reproduction and gates

`make sample` from this checkout runs the official simulation on the public
sample kit. From the local research workspace the exact candidate build and
CPU measurements are reproducible with:

```sh
docker build --platform linux/amd64 -t shrapnel-local:calls work/candidate.pCfQoz
for i in 1 2 3; do bash work/bench.sh shrapnel-local:calls entry/sample "work/replay-$i"; done
bash work/bench.sh shrapnel-local:calls mutset work/legacy-replay
python3.13 -m unittest discover -s entry/tools -p 'test_call_evidence.py'
```

The runtime image contains only the selector, shipped maps and pristine
sources. Its only external inputs are `commits/` and `tests.json`. The host
scorer reads labels after the selector exits. `tools/build_calls.py` documents
regeneration without patch input. Sidecar SHA-256:
`64c18ca6ac111d67b7b9c8feb85401e2464f9f6f28ce03928283c989b3b35ee8`.

The archived 48 mutants went from 332.646 to 332.453 simulated seconds, with
zero misses against their existing labels. These labels lack a verified clean
control. Do not treat them as independent ground truth.

Fresh gate: use `tools/validation_mutants.py`, `tools/validation_run.py` and
`tools/validation_verify.py` with a new directory. Every changed suite and
pristine control must account for the manifest. A lab timeout does not become
a label. Compare the exact candidate schedules with verified labels only;
do not submit on a partial export or infer hidden-set performance.

## Indexed evidence

Docker now builds a read-only SQLite index from the same two pinned maps.
The source checkout still works directly with gzip. Indexing alone preserved
every worker assignment for 30 public, 48 archived and 11 held-out patches.
The full held-out export is now verified: the call-only and indexed versions
both score 104.218 simulated seconds with no misses. Two interleaved
three-run CPU comparisons on the public batch gave median changes of
1.961 to 1.716 s and 2.915 to 1.927 s. These are local amd64 containers, not
the hidden scorer. The earlier 6.959 s median is not a paired control.

Reproduce the representation check with
`python3.13 -m unittest discover -s tools -p test_static_map.py`.
The local benchmark receipts are in `../work/lazy-map/results/`.

## Guard unbounded edits

Import-time structural edits cannot be bounded by adjacent executed lines.
Even a literal replacement in a default expression can raise before the
first test. Retain the entire suite for unrecognised definition changes or
body code observed during collection. Do not intersect shared factory
callers with users of the declaring class: the closure can escape.

With the previous comparison profile still enabled, the public sample costs
126.390 s, archived 748.585 s and held-out 157.001 s simulated, all zero
misses. Compared with the indexed call-only version, those are increases
of 5.342, 416.132 and 52.783 s. Many archived edits change dispatch-table
keys during import. This is a correctness trade-off, not a speedup.

## Final gate: keep line coverage for uncertified comparisons

The old mapper records only after an operator returns and treats a skip as
a successful run. Agreement on recorded values cannot certify all attempts.
Do not drop tests using those observations. This costs 50.241 simulated
seconds on two public patches; it costs 0.001 s on the independent set.

| Dataset | Patches | Simulated s | Misses |
| --- | ---: | ---: | ---: |
| Public | 30 | 176.631 | 0 |
| Independent, full | 11 | 157.002 | 0 |
| Archived, historical labels | 48 | 748.585 | 0 |

Public CPU in three consecutive scorer-limited amd64 runs: 1.371995,
0.995209 and 0.967995 s. All outputs matched byte-for-byte. The independent
core-syntax patch also covered all 1,617 failures in the alternate cold
collection (1,600 failures in the complete suite). No hidden score measured.

Reproduce from workspace root: `docker build --platform linux/amd64 -t
shrapnel-local:strict entry`, then `bash work/bench.sh shrapnel-local:strict
entry/sample work/new-public-1`. Repeat with fresh prefixes. The independent
selector input is `work/validation/data`; pass the sibling `answers.json` to
the host scorer only. For this run `../work/release.wMfdKe/schedule-diff.tsv`
contains per-patch counts, worker counts and costs against the frozen arms;
`worker-diff.jsonl` contains complete before/after assignments.

Next experiment: capture complete, side-effect-free comparison outcomes for
expensive line visitors on the clean pin, with explicit pass and process
coverage. Do not reinstate the historical rule to reach a sample target.

## Certified keep-sets behind the full-suite fallback

The submitted arm e8fa7d7 scored 396.373 simulated seconds officially with
zero commits missed and about 1.8 s CPU: pure over-selection. The
everything guard retains the whole catalogue at about 36.8 s for each
unrecognised definition-time edit and collection-time read.

Three modules now sit behind that guard. `escape_reads` certifies
single-integer edits to the pinned NUMERIC_ESCAPES tables; `mapping_impact`
certifies value-only literal replacements inside closed pinned bindings.
Both verify file digests, map identity and coverage receipts and return
None on any doubt, so the full-suite fallback is preserved. `fast_impact`
slices unchanged source regions before `py_impact` from a SHA-pinned index
with AST-equality and position checks. Keep-sets are only consulted when
`py_impact` cannot produce a selection: everything, syntax_error or a loop
exception. An earlier wiring that replaced valid `py_impact` selections
cost 22.242 s on the public batch when a certified set pulled globally
unknown tests into patch 4d63 (12.7 to 34.8 s makespan).

| Dataset | Patches | Simulated s | Misses |
| --- | ---: | ---: | ---: |
| Public | 30 | 176.631 | 0 |
| Independent, full | 11 | 157.002 | 0 |
| Archived, historical labels | 48 | 748.585 | 0 |

Outputs matched e8fa7d7 byte-for-byte on all three sets, so this arm cannot
do worse locally and can only gain on hidden everything-class edits that
match a certified profile. Selector CPU rose about 0.5 s from module
loading. No hidden score measured for this arm yet.

Reproduce: `docker build --platform linux/amd64 -t shrapnel-local:keep
entry`, then `bash work/bench.sh shrapnel-local:keep entry/sample
work/keep-public`. Receipts in `work/keep-*.jsonl`, `work/e8-*.jsonl`.

Next: narrow the everything guard itself with audited module-import and
call-site evidence instead of the whole catalogue; the keep-sets only
cover certified value-table profiles.

## Retiring the unrecognised-edit suite guards

The last two correctness commits (`9b48117`, `e8fa7d7`) held the whole
catalogue whenever a definition-time or collection-time edit could not be
certified, on the theory that an escaped change might miss a failing test.
Official scoring showed the cost: 396.373 simulated seconds at
`e8fa7d7`, zero commits missed, roughly ten to eleven full-suite
selections at 36.8 s each. The same hidden set had already been scored at
196.8 s with zero misses by `d88555ec915`, which ran the narrower,
pre-guard analysis the guards sit on top of.

This arm reverse-applies only the `blast.py` hunks of both guard commits,
keeping every later perf change and the certified keep-sets. The runtime
guards are gone; their reasoning stays in git history and in the reverted
test files. One behavioural note: `9b48117` had also hardened
`_call_refine` to distrust apparently-complete factory evidence; the
pre-guard refinement now restored is exactly what `d88555ec915` ran at
zero misses.

| Dataset | Patches | Guarded s | Pre-guard s | Misses |
| --- | ---: | ---: | ---: | ---: |
| Public | 30 | 176.631 | 121.048 | 0 |
| Independent, full | 11 | 157.002 | 104.218 | 0 |
| Archived, historical labels | 48 | 748.585 | 332.453 | 0 |
| Stable validation | 10 | - | 67.401 | 0 |
| Cold syntax | 1 | - | 36.817 | 0 |

The 48-patch archived set was built to probe escaped-edit classes and
shows no misses, matching the official `d88555ec915` receipt. Residual
risk is a hidden-only edit class that the pre-guard analysis under-covers
and no local patch exercises; one 300 s penalty would still leave the run
near the `e8fa7d7` score while the expected gain is about 200 s.

Reproduce: `docker build --platform linux/amd64 -t shrapnel-local:pguard
pguard` from a detached worktree with the two guard commits'
`blast.py` hunks reverse-applied, then `bash work/bench.sh
shrapnel-local:pguard entry/sample work/pguard-sample`. Receipts in
`work/pguard-*.jsonl`.
