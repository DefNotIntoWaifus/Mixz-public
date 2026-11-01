# Benchmark harness (plan step 0)

Every engine or format change is measured with this harness against a stored baseline from the same machine.

```sh
.venv/bin/python tools/harness/run.py                   # quick tier, ~6 MB, a few minutes
.venv/bin/python tools/harness/run.py --one-core        # quick tier plus the 1-core pipeline and its time breakdown
.venv/bin/python tools/harness/run.py --tier full       # full tier, ~29 MB, for milestones
.venv/bin/python tools/harness/run.py --save-baseline   # also store the run as this machine's baseline
.venv/bin/python tools/harness/reference.py --tier full # paq8px -8 and zpaq -m5 sizes into the cache (hours)
.venv/bin/python tools/harness/run.py --reference       # add their columns to the report
```

The first run downloads about 130 MB of test data into `~/.cache/mixz` (set `MIXZ_CACHE` to move it).

## What one run reports

| Item | How it is measured |
|---|---|
| Size per kind and in total | Compressed bytes / input bytes, through the full `mixz.compress` pipeline, and the change against the baseline |
| Round-trip | Every output is decompressed in a separate process, the CRC is checked, and the result is compared with the input |
| Determinism | Output from 1 and N threads must be identical. A `-O0` build must give the same bytes as the release build on one file per kind. Repeated engine runs must give the same bytes. |
| Engine speed, 1 core | `speed.c` on fixed 512 KiB inputs (text, binary, image) built from the quick tier, with no primer, pinned to the last CPU. Reported as CPU time per byte, fastest of 5 runs: CPU time leaves out the time other programs hold the core (13–15% of wall time on this desktop, measured), which cut the spread between runs from 15% to under 2%. The speed tool links the source of the codec `mixz.py` writes new files with (`mixz.CODEC`, whose source and function prefix are in `registry.json`). When that source differs from the baseline's, the codec the baseline's commit wrote is built too and the two are run alternately (A/B), so slow drift of the machine affects both alike; identical engines measured this way agree within 0.1%. `--ab REV` compares with any git revision, `--engine-only` runs only this part (about 1 minute). |
| Pipeline speed, all cores | Wall time of compressing, then decompressing, the whole tier with N threads, each phase in a fresh process. This includes primer replay, detection and transforms. |
| Pipeline speed, 1 core | The same with 1 thread pinned to one CPU, plus a time breakdown: priming, primer choice, coding, transforms. Always included in the full tier; in the quick tier only with `--one-core`. |
| Start-up | CLI compress and decompress of one ~28 KB Python file, median of 3, in a fresh process with `--no-server`. Then the same with `mixz.py serve` running on a private socket (warmed by one call each; wall clock, since the work runs in the server); the server's output must be identical, which counts in the round-trip result |
| Other tools | gzip -9, xz -9e, zstd -19, zstd -19 with the primer mixz chose as a raw dictionary (left out when no primers are installed), and brotli -q11. These are computed once per file and cached. |
| Reference tools (`--reference`) | paq8px -8 (v217) and zpaq -m5 (libzpaq 7.15, streaming format without file name or checksum), built by `reference.py` from pinned, sha256-checked downloads with `-march=native`. Each file is compressed alone, in its own process. paq8px -8 runs at about 3 KB/s on the full tier (start-up per file included) and needs up to 2.4 GB per process, so the first full-tier run takes about 2 hours with two processes at a time; sizes and per-file CPU time are cached in `~/.cache/mixz/reference.json`. The number of parallel processes is set by free memory (4 GB reserve). |

Exit status:
- 0: OK.
- 1: broken (round-trip or determinism failed).
- 2: needs the maintainer's approval, because a kind or the total grew by more than 0.5% (CLAUDE.md invariant 4).

Results go to `results/harness/<machine>/`:
- `baseline-<tier>.json` is committed.
- `runs/` stays local.

Speed is only comparable on the same machine. Every run records the machine description (CPU, compiler, flags, kernel, governor, load), and the report lists the fields that differ from the baseline.

## Test data

- **`sources.json`**: pinned downloads. Each has a URL, a sha256, an SPDX license id, and the archive members used per kind (py, c, js, json, web, prose, man, elf, png). Debian packages come from snapshot.debian.org, whose file URLs are permanent.
- **`sets.json`**: the frozen test set, made by `fetch.py --freeze`. Do not edit it by hand: a different set makes results incomparable with earlier runs.
  - Order: files are taken in a fixed pseudo-random order that balances bytes across the sources of a kind.
  - Size budget: quick is 512 KiB per text kind, 256 KiB of `.gz` for man, and 1.5 MiB each for elf and png. Full is about 6× that. Quick is part of full.
  - Clean test data (invariant 5), checked in two ways:
    - Debian packages use the same paths as the Ubuntu VM that built the primers, so their members in the VM's training half (`tools/corpora.py`) are dropped.
    - Every candidate is checked against the primers by content (`tools/overlap.py`). A file is excluded if more than 20% of its bytes lie inside 64-byte strings that occur in a primer mixz could choose for it. At that point it is treated as a near-copy of training data.
- **Calibration** (`fetch.py --calibrate`, 2026-10-23): sources that were not on the VM had a 90th-percentile overlap of at most 10% per kind. botocore (median 45%), lodash (up to 90%) and the Tango SVGs (median 27%, from Inkscape metadata boilerplate) were dropped as sources.

## Files

| File | Purpose |
|---|---|
| `run.py` | runs the measurements, compares them with the baseline, saves results |
| `fetch.py` | downloads and checks sources, extracts members into the cache, freezes and calibrates the test set |
| `speed.c` | engine-only speed test |
| `reference.py` | builds and runs paq8px -8 and zpaq -m5, caches their sizes |
| `sources.json`, `sets.json` | pinned sources, frozen test set |
