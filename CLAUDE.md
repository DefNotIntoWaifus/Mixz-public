# mixz — instructions for Claude Code

mixz is an experimental lossless, format-aware context-mixing compressor. This release ships **without primers** (`primers/README.md`), so every file it writes uses none. `README.md` has the measured results: smaller output than gzip, xz, zstd, brotli and zpaq -m5 on every tested file type; larger than paq8px -8 on 7 of 9 file types (smaller on `.gz` and PNG); about 100 times faster than paq8px, and about 1,400 times slower than zstd to decompress.

**Read `docs/context.md` before any non-trivial task.** It has every measurement, the decisions taken and their reasons, and the plan.

## How to work
- Be direct and technically precise. State an assumption instead of asking a question it answers.
- Label how certain each claim is: measured, tested, or estimate. Mark example numbers as examples.
- `docs/context.md` assumes no prior knowledge, defines every technical term and uses literal language. The README is short and has the same content in three blocks: English, Spanish and Chinese. Edit all three together; details go in `docs/context.md`.

## First thing on a new machine
The measurements were made on an x86-64 Linux VM (Ubuntu 24.04, Intel Xeon, 2 cores, AVX-512) and, since 2026-10-23, on an AMD Ryzen 3 1200 desktop (4 cores, AVX2, Arch Linux, gcc 15.2). On whatever machine Claude Code runs:
1. `python3 -m venv .venv && .venv/bin/pip install zstandard numpy brotli`, then `make`. Arch, Debian 12+ and other PEP 668 distributions block system-wide `pip install`, so run every command below with `.venv/bin/python`. brotli is only used for the other-tools columns. The build falls back from `-march=native` to `-O3` if the compiler rejects it.
2. `python3 tools/test_roundtrip.py`. All tests must pass.
3. `python3 tests/check_vectors.py`.
   - The vectors `NAME.c3.mxz` / `NAME.b8k.c3.mxz` (format 2, codec 3) were made on the Ryzen desktop without primers. They cover the text, binary and image engine profiles: without primers the encoder never picks the code or web profiles, which are still decoded.
   - Each must decompress to the original, and compressing the original with the vector's codec must give byte-identical files.
   - Codec 3 passed on AMD Zen 1 (AVX2) with gcc `-O0`/`-O3 -march=native` and under UBSan. Its predecessor, codec 2, also passed on an Intel Pentium E5700 (SSSE3 only; gcc and clang) and on AMD Zen 4 (AVX-512). On a different CPU type (for example ARM) it is the cross-platform determinism check, not run yet.
   - gzip/PNG re-creation uses the deflate built into the engine (zlib 1.3.1), so the system zlib does not matter.
   - A mismatch is never silent: decompression checks a CRC32 and refuses to return wrong data.
4. Record a harness baseline on the new machine: `.venv/bin/python tools/harness/run.py --one-core --save-baseline` (and `--tier full --save-baseline`). Speed numbers are not comparable across machines. The codec's mixer has an AVX2 path and a scalar path with identical results; on ARM the scalar path runs (correct, slower). A NEON path must give bit-identical results.

## Current state
- Plan steps 0 (benchmark harness, `tools/harness/`), 1 (file format 2, `docs/context.md` section 2.1), 2 (codec 2, section 2.2; content-hash version IDs, section 2.3), 3 (model kept loaded: workers, write log, `mixz.py serve`; section 2.5), 4 (blocks across cores; section 2.6) and 6 (profiles and tuner, codec 3; section 2.8) are done. Step 5 was dropped (section 2.7). Section 2.9 re-measures every step on one machine and adds bundles (`compress_bundle`) and automatic block size (no split below 1 GiB).
- **Step 7 (public-domain primers) is in progress:** corpus tools in `tools/mind/`, primers not built yet. Primer IDs 1–8 (the development primers, not published) are retired in `registry.json`.
- The file format is a **pre-release draft**: a later version may not read files written by this one.

**Every engine or format change:** run `tools/test_roundtrip.py`, then `tools/harness/run.py`, and report its numbers against the baseline. Exit status 2 means output grew by more than 0.5% and needs the maintainer's approval.

## Invariants (never break)
1. **Round-trip is exact.** Every output must decompress to the original bytes. Run `python3 tools/test_roundtrip.py` after every engine or format change.
2. **The engine is deterministic.** Use integer arithmetic only: no floats, no undefined behavior, no dependence on the compiler or CPU. Check with a `-O0` and a `-O3 -march=native` build: their output must be byte-identical.
3. **Files stay readable.** Primers and mind modules are part of the file format. Never edit a primer or module once registered; add new ones. `PRIMER_IDS` in `mixz.py` is append-only. Never edit `engine/pdeflate.c` or `engine/zlib-1.3.1/`: format 2 relies on that exact deflate output. `engine/transform.c` (E8/E9, PNG filters) is part of the format too: change it only with a new transform ID.
   - **Until the first stable release** only the current codec ships, and `registry.json` is a draft:
     - An **output-neutral** edit to `engine/cm.c` is made in place. `tests/check_vectors.py` must pass unchanged (it proves the output is identical); then update the codec's `engine_sha256` and replace `heads` with the current head (`mixz.registry_head()`).
     - An **output-changing** edit gets a new codec ID and new version IDs (in the same `engine/cm.c`; update `source`/`prefix` if needed), new vectors (`check_vectors.py --write`), and the old codec's entries are removed with their IDs added to `retired`. IDs are never reused, so a stale file fails at its header.
   - **From the first stable release on** every `registry.json` entry is append-only, `heads` only grows, and a released codec's source is frozen: a change needs a new codec and the old code stays in the build. `tools/test_roundtrip.py` fails if a codec source no longer matches its registered sha256, or if a recorded head no longer reproduces.
4. **Size and speed are measured.** Any speed change that makes output more than 0.5% larger needs the maintainer's approval. Report changes as measured numbers against the baseline in `docs/context.md` section 3.
5. **Test sets stay clean.** Never benchmark on files used to build primers. `tools/corpora.py` defines the split on the VM's paths; `tools/overlap.py` checks by content (a file with more than 20% of its bytes inside 64-byte strings of a primer is excluded). The harness test set is frozen in `tools/harness/sets.json`.

## Commands
Use `.venv/bin/python` where the system Python has no packages (see setup step 1).
```sh
make                                   # build libmixz.so (mixz.py also auto-builds on first use)
python3 mixz.py c FILE | d FILE.mxz | t FILE.mxz | info FILE.mxz
python3 mixz.py serve                  # keep models loaded; c/d/t use it (same output); --no-server bypasses it
python3 tools/test_roundtrip.py        # full round-trip and robustness suite (~20 s)
python3 tests/check_vectors.py         # determinism check against the committed codec 3 vectors
python3 tools/harness/run.py           # benchmark harness, quick tier (~3-5 min); --tier full (~20 min), --one-core, --save-baseline, --ab REV, --engine-only, --reference
python3 tools/harness/fetch.py         # download/verify the harness test data (run.py does this itself)
python3 tools/harness/reference.py     # paq8px -8 and zpaq -m5 sizes for the harness (pinned sources; hours for the full tier)
python3 tools/experiments/compare_reference.py   # mixz, paq8px and zpaq per kind: files alone and one stream
python3 tools/experiments/speed_compare.py       # speed of every tool on one input, one core, CPU time
python3 tools/build_primers.py         # build primers from your own training files (new module IDs)
```
`MIXZ_LIB=/path/libmixz.so` makes `mixz.py` use a prebuilt engine. The scripts behind each measurement are in `tools/experiments/` (see its README).

## Layout
```
mixz.py                  CLI + Python API: detection, transforms (gzip/PNG/E8E9), primer choice, container (format 2), bundles, snapshots, server
registry.json            format registry: codecs, deflates, mind modules, versions, profiles, retired IDs, chain heads (draft until a stable release)
engine/cm.c              the codec (codec 3): hashed contexts, run/match models, 2-layer mixer (16-bit, AVX2), SSE, arithmetic coder; engine profiles text, code, web, binary, image
engine/transform.c       E8/E9 x86 transform and PNG scanline filters
engine/pdeflate.c        pinned deflate for gzip/PNG re-creation, on engine/zlib-1.3.1/ (unmodified zlib 1.3.1 compressor)
primers/                 no primers in this release (README.md explains)
docs/context.md          full project context: measurements, decisions, plan
tools/                   corpora + split, primer builder, overlap check, old benchmark, tests, experiments/
tools/harness/           benchmark harness: pinned sources, frozen test set, runner, reference tools (see its README)
tools/tuner/             tuning data (train/valid splits, disjoint from the harness) and variants.py, which measures codec variants per kind
tools/mind/              public-domain corpus tools for the planned primers (step 7)
results/harness/         harness baselines per machine (runs/ stays local)
results/compare/         compare_reference.py and speed_compare.py results, and a reviewer's run with the development primers
```

## Known gotchas
- **Corpus globs are Linux paths** from an Ubuntu 24.04 VM. The path split matches only that VM's layout. `bench.py` drops files by content overlap; use the harness for decisions.
- **Speed noise on a desktop.** Other programs hold a pinned core 13–15% of the time (measured on the Ryzen desktop). Wall-clock numbers vary by up to ±10% between runs, and CPU-time engine numbers drift by about ±5%. Judge engine changes only by the harness's same-session A/B on CPU time (automatic when the engine differs from the baseline; `--engine-only --ab REV` for a 1-minute check). Never compare wall-clock numbers from different runs. Don't run other heavy work during a harness run, and don't edit `mixz.py` or `tools/harness/` while one runs: workers re-import them.
- **Memory.** A model takes about 155 MB, a server or batch keeps up to 8 primed models, and paq8px -8 takes up to 2.4 GB per process. Parallel runs: measure peak memory over several tasks in one process, not one task; run each task in a fresh process with an address-space limit; size the worker count to free memory with a 4 GB reserve; stop below 2 GB free. `tools/tuner/variants.py` and `tools/harness/reference.py` do this.
- **valgrind does not support AVX-512.** Build with `-O3 -mavx2 -mfma` for cachegrind.
- **Snapshots are keyed by a size class** (`cm_size_class`). A file in a different size class falls back to replaying the primer, which gives identical output but is slower. Without primers no snapshot is used.
- **Pinned deflate refuses level 0.** zlib's level 0 sizes stored blocks by the caller's output buffer, so it is not reproducible; the parameter search uses levels 1–9 only.
- **The engine library is several C files** (`mixz.ENGINE_SOURCES`, built with `-DZ_PREFIX`). Build it through `make`, `mixz.build_engine` or the harness, not by compiling one codec alone. The harness speed tool links only the source of the codec `mixz.CODEC` names (and, for `--ab REV`, the codec that revision wrote).
- **Profile with the real build.** gcc vectorizes at `-O3 -march=native`; an `-O2 -fno-inline` cachegrind profile showed the mixer at 55% when it was 16% in the real build (step 2). Use `perf` on the `-O3 -march=native` binary, or callgrind on an `-O3 -mavx2` build with `-g` for exact per-line counts.
- **Engine experiments:** measure size on per-kind probe streams and speed by alternating old and new speed tools on one core; keep output-neutral changes separate and check them for identical output. Removing model components (run models, the deterministic-state input) or freezing StateMaps cost +0.55% to +2% (measured), so the model has no cheap parts to drop.
- **The worker write log must cover every write to the model's large tables** (`log_w` in `engine/cm.c`: buckets, run entries, match table, APM cells; match buffer by range). An engine edit that adds a table write elsewhere makes a worker's next file differ from the replay path. After any engine edit run `tools/test_roundtrip.py` (worker tests), the vectors, and the worker tests with a `-DMZ_CHECK_RESTORE` build, which aborts on any restore difference. The worker path runs only with primers installed.
- **A running `mixz.py serve` takes CLI calls.** The client checks a build identity (paths, sizes, mtimes of code, registry, engine, primers) and works without a server whose files differ. Measurements of the CLI must pass `--no-server` (the harness does) unless they measure the server.
- **The E8/E9 transform must consume an opcode's 4 operand bytes even when it does not transform them.** An earlier version did not, and its inverse could take different paths.
- **Per-file speed is restore, re-priming and primer choice, not the model** (section 2.9). Bundles are the measured fix. Per-step speeds: `tools/experiments/steps.py`. Log test data: `tools/experiments/logs.json`.
- **The codec fits at most 9 hashed contexts as written:** the mixer input array `x[NIN]` (3·NC + 4 entries, NIN 32) and mixer 3's selector table (10 rows, one per count of known contexts). More contexts need both enlarged, and the AVX2 mixer reads exactly 32 inputs. `variants.py` handles this for experiments; exceeding it silently corrupts memory.
