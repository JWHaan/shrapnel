# Blast Radius experiment log

SQLGlot pin: `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. Previous reported
official score: 196.8. These experiments use the public 30 patches, not the
hidden workload. The official total includes CPU and is the median of three.

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
