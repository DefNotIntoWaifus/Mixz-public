# Experiments

Scripts behind the measurements in `docs/context.md`. Linux paths in the corpora are from an Ubuntu 24.04 VM.

| File | Measures | Run |
|---|---|---|
| `blocks.py` | Blocks across cores (step 4): output size against block size, speed against thread count, on each kind's full tier concatenated into one file, or on `--file PATH` | `python3 blocks.py --sizes 0,4M,2M,1M,512K --speed 2M --threads 1,2,4` (about 10 minutes) |
| `steps.py` | Pipeline speed and size per plan step (each commit from its own worktree), per kind: 1 core, N threads, N processes, one joined stream, bundles | `python3 steps.py --revs 0696daa,ff28c4f,. --kinds py,json,logs` (about 5 minutes per commit). The commits in this example are in the development repository, which is not published; here only `.` and later commits of this repository exist |
| `restore_pages.c` | Share of 64-byte lines and 4 KB pages one file writes in the primed model, and the cost of a log restore and a full copy | `cc -O3 -march=native -o /tmp/rp restore_pages.c && /tmp/rp ../../primers/py.bin FILE 3` |
| `compare_reference.py` | mixz against paq8px -8 and zpaq -m5 (pinned builds of `tools/harness/reference.py`) and the common tools per kind (harness quick tier; logs: the Loghub samples): each file alone, and the kind's files as one stream (mixz bundle; paq8px and zpaq on the files joined; with primers installed, paq8px after mixz's primer). Adapted from a reviewer's script | `python3 compare_reference.py [py js json web prose logs]` (hours: paq8px runs at about 5 KB/s); writes `results/compare/` |
| `speed_compare.py` | Compress and decompress speed of mixz, paq8px -8, zpaq -m5, gzip, xz, zstd and brotli on one input (a kind's quick-tier files joined), one core, CPU time; every output round-tripped | `python3 speed_compare.py [py]` (about 4 minutes); writes `results/compare/<machine>-speed.json` |
| `logs.json` | Pinned log test data (16 Loghub samples, the 205 MB NASA HTTP log), outside the harness set | data for `steps.py --kinds logs` and `blocks.py --file` |
| `time_engine.c` | Engine speed (ns/byte) and size for one file, one core, any `maxbits` | `cc -O3 -march=native -Dmz_compress=cm_compress -o /tmp/te time_engine.c ../../engine/cm.c && /tmp/te FILE PROFILE MAXBITS` (profile 0=text 1=binary 2=image) |
| `mem_latency.c` | Dependent random-read latency by table size | `cc -O2 -o /tmp/ml mem_latency.c && /tmp/ml` |
| `lzcov.c` + `repeat_coverage.py` | Share of bytes inside repeats of >= 8/16/32 bytes, with and without primer | `cc -O2 -fPIC -shared -o liblzcov.so lzcov.c && python3 repeat_coverage.py` |
| `primer_scaling.py` | Compressed size vs primer size (512 KB, 2 MB, 8 MB) on 40 held-out Python files | `python3 primer_scaling.py` |
| `tune_params.py` | Engine tunables sweep (`-DLR1=2,LR2=1`...) on a text and a binary file. It builds codec 1, whose tunables were build options; codec 1 was dropped and its source (`engine/mixz.c`) is not in this repository, so the script is kept as the record of that measurement | `python3 tune_params.py TEXTFILE BINFILE base LR1=2 SHIFT1=12,LR1=3` |
| `gzip_repro.py`, `png_repro.py` | Share of system `.gz` / PNG files zlib can reproduce bit-exactly | `python3 gzip_repro.py` |

Instruction counts: `valgrind --tool=cachegrind --cache-sim=yes ./te FILE 0 20` (build with `-O3 -mavx2 -mfma`, valgrind does not support AVX-512). Per-function shares: build with `-O2 -fno-inline` and run `cg_annotate`.
