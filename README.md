# mixz

> **Experimental, not production-ready.** The file format can still change, so a later version may not decompress files written by this one. Keep your originals. **paq8px compresses smaller than mixz on 7 of the 9 file types tested here; mixz's output is 12–32% larger on them.** mixz is smaller only on `.gz` and PNG files, which it recompresses. It takes about 1/100 of paq8px's time, and it compresses smaller than gzip, xz, zstd, brotli and zpaq on every file type tested.

`mixz` is a lossless file compressor. *Lossless* means that decompression gives back exactly the original bytes. It predicts each bit with several models and combines their guesses (context mixing), and it undoes gzip, PNG and x86 call encodings first, so the models see the underlying data.

**How this document is organized.** Every section has the same three parts:
- **Summary**: the section in one sentence.
- **Details**: the explanation.
- **Certainty**: how sure the claims are. *Measured* means the number comes from a run. *Tested* means the automatic tests check it. *Estimate* means it is reasoned from measurements, or from how similar programs behave, but was not run.

Sections 1–4 are enough to decide whether `mixz` is of interest. Sections 5–9 are reference material.

---

## 1. Results

**Summary:** Without primers, `mixz` output is 33.8% of the original size on 30.6 MB of test files, against 39.0% for brotli -q11 (the best common tool) and 39.7% for zpaq -m5. paq8px -8 is smaller: 32.3% overall, and 18.5% smaller than `mixz` on the 7 file types that `mixz` does not recompress.

**Details:**
- **Test files:** 3,815 files (30.6 MB) from 35 public sources with known licenses: Python packages, C libraries, JavaScript libraries, API descriptions, documentation, Debian packages (manual pages, programs) and images. The harness downloads them and checks each one by SHA-256 (`tools/harness/`). They are not part of this repository.
- **Method:** each file is compressed on its own. Every `mixz` output was decompressed in a separate process and compared with the original: all matched.
- **No primers:** this release ships without primers (section 5), and the `mixz` column is measured without them.
- **How to read the table:** each cell is the total compressed size divided by the total original size, so lower is better. The best result in each row is in bold.
- **The other tools:** gzip -9 (zlib 1.3.1), xz -9e (liblzma 5.8.3), zstd -19 (1.5.7) and brotli -q11 (1.2.1) are the common general-purpose compressors. zpaq -m5 (7.15) and paq8px -8 (v217) are context-mixing compressors like `mixz`; paq8px is among the strongest open compressors. zpaq is run through its library in streaming mode, with no file name or checksum. It still stores its model description in every file, about 290 bytes (measured), which dominates its result on small files. Both are built from pinned sources by `tools/harness/reference.py`, with the same `-march=native` flag as `mixz`.

| Files | Count | MB | gzip -9 | xz -9e | zstd -19 | brotli -q11 | zpaq -m5 | paq8px -8 | mixz |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Python source | 217 | 3.15 | 24.5% | 23.5% | 23.2% | 20.9% | 20.8% | **15.7%** | 18.4% |
| C source and headers | 905 | 3.15 | 33.0% | 32.2% | 31.1% | 28.7% | 33.5% | **21.6%** | 25.0% |
| JavaScript | 87 | 3.15 | 25.1% | 21.5% | 22.1% | 20.7% | 17.9% | **13.1%** | 16.9% |
| JSON | 192 | 3.15 | 15.3% | 11.6% | 11.8% | 10.2% | 10.6% | **6.4%** | 8.5% |
| HTML, CSS, XML, SVG | 1,257 | 3.15 | 23.0% | 23.9% | 21.6% | 19.2% | 28.7% | **14.7%** | 17.5% |
| Prose (txt, rst, md) | 221 | 3.15 | 33.0% | 31.0% | 30.8% | 27.5% | 26.5% | **21.5%** | 24.2% |
| Manual pages (`.gz`) | 659 | 1.50 | 101.0% | 102.7% | 100.4% | 100.2% | 112.8% | 100.7% | **88.2%** |
| x86-64 programs | 10 | 5.98 | 45.9% | 38.3% | 40.9% | 39.0% | 32.8% | **23.7%** | 31.1% |
| PNG images | 267 | 4.19 | 97.7% | 97.9% | 97.6% | 97.5% | 98.5% | 95.3% | **87.4%** |
| **All of the above** | 3,815 | 30.55 | 43.2% | 40.8% | 40.8% | 39.0% | 39.7% | **32.3%** | 33.8% |

The same results as the change in `mixz`'s output size against each tool (negative: `mixz` is smaller):

| Files | mixz against the best common tool | mixz against zpaq -m5 | mixz against paq8px -8 |
|---|---|---:|---:|
| Python source | −12.3% (brotli -q11) | −11.9% | +16.6% |
| C source and headers | −13.0% (brotli -q11) | −25.4% | +15.4% |
| JavaScript | −18.4% (brotli -q11) | −5.5% | +29.4% |
| JSON | −16.7% (brotli -q11) | −19.6% | +32.0% |
| HTML, CSS, XML, SVG | −8.4% (brotli -q11) | −38.8% | +19.5% |
| Prose (txt, rst, md) | −11.8% (brotli -q11) | −8.4% | +12.5% |
| Manual pages (`.gz`) | −11.9% (brotli -q11) | −21.8% | −12.4% |
| x86-64 programs | −18.9% (xz -9e) | −5.3% | +31.1% |
| PNG images | −10.4% (brotli -q11) | −11.3% | −8.3% |
| **All of the above** | −13.5% (brotli -q11) | −14.9% | +4.8% |

- **Against the common tools:** `mixz` is smaller on every file type, by 8.4–18.9% against the best of them for that type, and by 13.5% overall.
- **Against zpaq -m5:** `mixz` is smaller on every file type, by 5.3–38.8%. zpaq loses most on the many small web and C files (the web files average 2.5 KB), where its fixed 290 bytes per file weigh most.
- **Against paq8px -8:** `mixz` output is 12.5–32.0% larger on 7 of the 9 file types. On those 7 types together it is 21.5% of the original size against 17.5% for paq8px, 22.7% larger. `mixz` is smaller on manual pages (`.gz`, by 12.4%) and PNG images (by 8.3%), because it undoes their deflate compression before modeling (section 5); paq8px stored them almost unchanged. The overall difference (4.8%) is small only because these two types are part of the set.
- **Time:** paq8px took 10,040 CPU seconds for the 30.6 MB, two processes at a time; `mixz` took 106 CPU seconds on one core for the whole command-line pipeline, 95 times less. Speeds on one input are in section 2.

**One stream per file type.** Many small files compress better as one stream, because the model carries what it learned from one file into the next. `mixz` does this with bundles (section 6), and the other context-mixing tools with the files joined into one input. Measured with `tools/experiments/compare_reference.py` on smaller sets: the harness quick tier for code, JSON, web and prose, and 16 log samples from Loghub (2,000 lines each):

| Files | Count | MB | brotli -q11 | mixz | mixz bundle | zpaq -m5 | zpaq -m5, one stream | paq8px -8 | paq8px -8, one stream |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Python source | 38 | 0.52 | 19.4% | 17.1% | 14.3% | 19.7% | 14.7% | 14.5% | **11.3%** |
| JavaScript | 46 | 0.52 | 24.1% | 20.5% | 17.1% | 23.3% | 17.4% | 16.8% | **13.3%** |
| JSON | 64 | 0.52 | 6.5% | 6.5% | 2.5% | 10.6% | 3.1% | 5.3% | **1.5%** |
| HTML, CSS, XML, SVG | 193 | 0.52 | 17.0% | 15.5% | 8.9% | 25.8% | 9.0% | 12.9% | **6.5%** |
| Prose (txt, rst, md) | 51 | 0.52 | 27.7% | 24.3% | 21.4% | 27.2% | 21.6% | 21.6% | **18.5%** |
| Server logs | 16 | 4.46 | 6.9% | 4.8% | 4.8% | 5.3% | 5.2% | 3.6% | **3.5%** |
| **All of the above** | 408 | 7.08 | 11.4% | 9.2% | 7.8% | 11.2% | 8.2% | 7.5% | **6.0%** |

- Columns without "one stream" and "bundle" code each file alone, as in the first table.
- One stream makes every context-mixing tool smaller: `mixz` from 9.2% to 7.8% overall, zpaq from 11.2% to 8.2%, and paq8px from 7.5% to 6.0%.
- As one stream, `mixz` is smaller than zpaq on every file type (by 0.6–19.0%) and larger than paq8px on every file type (by 16–71%, 30% overall). The largest gap is JSON: 2.5% against 1.5%.
- Logs gain little from one stream with any tool, because each log sample is already large (280 KB on average).
- Time for all six sets (7.1 MB), one stream: `mixz` 10 CPU seconds to compress, zpaq 19, paq8px 1,024.

**With primers.** During development `mixz` used primers built from system files with many different licenses, so they cannot be published (`primers/README.md`). With them, its output on the same 30.6 MB was 30.9% overall, against 39.0% for brotli. In a reviewer's run with those primers (on an Intel Xeon virtual machine), `mixz` was smaller than paq8px -8 on Python (−7.9%), web (−26.6%) and prose (−6.5%) files coded alone, and larger on JavaScript (+5.9%), JSON (+31.9%) and logs (+31.9%). When paq8px was given the same primer, it was 22–30% smaller than a `mixz` bundle on Python and JSON (`results/compare/reviewer-xeon-vm.json`, `docs/context.md` section 3.6). Primers built only from public-domain data are planned.

**Certainty:** measured on an AMD Ryzen 3 1200 desktop (Arch Linux, gcc 15.2), 2026-11-01. The raw numbers are in `results/harness/` (`baseline-full.json`) and `results/compare/`. `python3 tools/harness/run.py --tier full --reference` reproduces the first table; the paq8px column takes about 2 hours with two processes at a time (`tools/harness/reference.py` runs as many as free memory allows). `python3 tools/experiments/compare_reference.py` reproduces the second table in about 40 minutes, and `python3 tools/experiments/speed_compare.py` the speed table of section 2 in about 4 minutes.

---

## 2. Speed

**Summary:** On one core `mixz` compresses and decompresses about 0.7 MB/s: about 100 times faster than paq8px -8, about 1.8 times faster than zpaq -m5, and about 1,400 times slower than zstd decompresses.

**Details:**

Terms used in this section:
- A **nanosecond** (ns) is one billionth of a second.
- **CPU time** is the time the processor spent on the program itself, which leaves out time taken by other programs.
- **MB/s** is millions of bytes of original data per second.

Speeds below are for one processor core and CPU time, on one input: the 38 Python files of the harness quick tier joined into one 0.52 MB file (`tools/experiments/speed_compare.py`).

| Tool | Size | Compress | Decompress | Decompression time per byte |
|---|---:|---:|---:|---:|
| mixz | 14.3% | 0.69 MB/s | 0.67 MB/s | 1,484 ns |
| paq8px -8 | 11.3% | 0.007 MB/s | 0.007 MB/s | 150,084 ns |
| zpaq -m5 | 14.7% | 0.39 MB/s | 0.38 MB/s | 2,657 ns |
| gzip -9 | 21.7% | 8.9 MB/s | 334 MB/s | 3.0 ns |
| xz -9e | 18.5% | 3.0 MB/s | 129 MB/s | 7.8 ns |
| zstd -19 | 19.1% | 2.9 MB/s | 951 MB/s | 1.1 ns |
| brotli -q11 | 18.0% | 0.56 MB/s | 419 MB/s | 2.4 ns |

- gzip, xz, zstd and brotli ran through their Python libraries (the fastest of 3 timings); `mixz` through its Python interface, so its times include format detection and the file container; paq8px and zpaq as their own programs, one run each way. Every output was decompressed and compared.
- **The engine alone** (the benchmark harness, one core, CPU time): about 1,430 ns per byte to compress and 1,460 ns to decompress text, 1,540 and 1,580 ns for programs, and 1,410 and 1,450 ns for images.
- **The whole command line on the 3,815 test files:** 0.26 MB/s to compress and 0.32 MB/s to decompress on one core (wall clock). Two things make it slower than on the single input above: format detection and transforms take 22% of the compress time (measured), and `.gz` and PNG files grow when their deflate compression is undone, so the model codes more bytes than the files contain.
- **Memory:** `mixz` uses at most about 170 MB per file being coded. paq8px -8 needs up to 2.4 GB per process (its help text; 2.2 GB measured), and zpaq -m5 about 100 MB (measured).

*Example (calculated from the table):* decompressing 1 GB of source code takes zstd about 1 second and `mixz` about 25 minutes on one core.

**Why it is slow:**
1. It makes 8 predictions per byte, one per bit, and each prediction must wait for the previous one. Extra cores cannot speed up a single stream.
2. Each prediction is a lot of arithmetic: about 11,000 processor instructions per byte, against about 24 for gzip decompression and 54 for xz decompression. The work is spread over many parts of the model: updating the models is about 47% of it and predicting about 45%, and no single part dominates.
3. Decompression has to repeat all of that work. gzip, xz and zstd store "copy these bytes from earlier" instructions, so their decompressors mostly copy. `mixz` stores only how surprising each bit was, so the decompressor must run the same model to recover the bits. This is part of the method, not a defect in the code. paq8px and zpaq work the same way.

**Using more cores:**
- Many files are compressed and decompressed at the same time, one per core (`-j N`). On 4 cores against 1: 3.4 times faster to compress and 3.1 times to decompress on the harness quick tier (938 files), and 2.2 and 2.1 times on the full tier (3,815 files; the cause of the lower figure was not measured). These are wall-clock times.
- A file over 1 GiB is split into blocks of at least 512 MiB, which are coded at the same time on several cores. Each block starts without what the earlier blocks taught the model, which costs size: +0.9% for two 50 MB blocks of a 100 MB file, and +12.5% for 4 MiB blocks of a 205 MB log (measured). The large minimum keeps the cost under about 0.5% (estimate).

**Certainty:** measured on the AMD Ryzen 3 1200 desktop; speeds on other machines differ. The instruction counts come from `perf` on the real build.

---

## 3. When to use it

**Summary:** For testing and research; not for data you cannot afford to lose.

**Details:**
- **Today:** comparing compression methods, research on context mixing, and trying the format-aware steps (gzip, PNG and x86 recompression). The file format may change, so do not keep `.mxz` files as your only copy.
- **Where this kind of compressor fits once the format is stable:** data that is compressed once and read rarely, such as backups and archives, where storage costs more than processor time.
- **Poor fit:** anything read often, because each read pays the decompression time from section 2.
- **No gain:** JPEG photos, video, music and zip files. They are already compressed, `mixz` does not handle their formats, and it stores them almost unchanged.
- **If size matters more than time:** paq8px compresses smaller (section 1).

**Certainty:** estimate, based on the measurements in sections 1 and 2.

---

## 4. Known limitations

**Summary:** Experimental format, no primers, much slower than common compressors, and larger output than paq8px on most file types.

**Details:**
- **The file format is not stable.** Until a stable release, a change to the engine's output gets a new engine number and the old one is dropped, so older `.mxz` files stop decompressing. Every file is rejected with an error in that case, never decoded wrongly.
- **No primers in this release.** The development primers contain third-party files and are not published. Without them, small files lose most: for example 33.5% against 52.4% for 185 small web files, measured in a separate run with and without the development primers.
- **paq8px is smaller** on 7 of the 9 file types in section 1, by 11–24% of `mixz`'s output, and as one stream on every file type tested.
- **Speed:** see section 2. The command line is a Python program; each command also pays about 0.13 s of start-up (measured, 25 KB file).
- **Recompression:** `.gz` and PNG files are only recompressed when zlib reproduces their compressed bytes exactly. A fixed version of zlib (1.3.1) is built into `mixz` for this, so the result does not depend on the computer. On the system files of the first test machine (Ubuntu 24.04) this worked for 79% of `.gz` files and 31% of PNG files. Files made by other programs (for example zopfli or optipng) are coded as they are, which saves little.
- **Formats not handled:** JPEG, MP3, MP4 and ZIP.
- **No archive mode:** one file in, one file out. Bundles (several files in one stream) exist only in the Python interface.
- **Processors:** the engine's fast path uses AVX2 when the computer has it, and a plain path gives the same files otherwise. ARM processors have not been tested.
- **Untrusted input:** the decoder checks every size in a file's header before it allocates memory. A deliberately broken `.mxz` file can make it use at most about 2,000 times its own size, plus about 20 MB (estimate from the code).

**Certainty:** measured for recompression coverage and sizes; the rest are facts about the current code.

---

## 5. How it works

**Summary:** `mixz` undoes encodings that hide patterns, and then predicts each bit with several models whose guesses a small neural network combines.

**Details:**

Terms:
- **Bit / byte:** a bit is a 0 or a 1. A byte is 8 bits. A file is a sequence of bytes.
- **Model:** the part that guesses the next bit, as a probability. Better guesses give smaller output.
- **Context:** what a model looks at when guessing, for example the previous 3 bytes, the current word, or the line above.
- **Arithmetic coding:** a way of writing bits where each bit costs −log₂(the probability the model gave it). *Example:* a bit predicted with 99% probability costs about 0.015 bits, and a bit predicted with 1% probability costs about 6.6 bits.
- **Mixer:** a small neural network that combines the guesses of all models into one guess, and learns while it runs how much to trust each model in each situation. Using one is called **context mixing**.
- **Primer:** a sample of typical content for one file type. Before the file starts, both the compressor and the decompressor run the model over the primer, so the model starts with knowledge of that file type. The primer is never written into the output. This release ships none.
- **Deflate:** the compression used inside `.gz`, `.zip` and PNG files.
- **Transform:** a reversible change to the bytes that makes patterns easier to predict.
- **Checksum (CRC32):** a 4-byte number computed from the file's contents. If the contents change, the number almost certainly changes, so a mismatch shows corruption.

Steps, for each file:
1. **Detect the format from the content,** not from the file name: gzip, PNG, x86 program, text, or other binary data.
2. **Undo encodings, but only if the original bytes can be rebuilt exactly.** The rebuild is checked before it is used.
   - `.gz`: decompress the content, then search for the zlib settings that recreate the exact original compressed bytes, and store those settings (3 bytes).
   - PNG: decompress the image data and undo the PNG row filters, so the model sees the actual pixels and the row above them.
   - x86 programs: call and jump instructions store their target as a distance from the current position, so calls to the same function look different at each location. The transform rewrites them as absolute positions, which repeat.
3. **Pick a primer,** if any are installed: each candidate is tried on a 64 KB sample of the file using zstd, which takes milliseconds, and is chosen only if it does at least 1% better than none. The primer also selects the engine's settings for program code or web files. Without primers, text uses the general text settings; the code and web settings would make files only 0–2% smaller without a primer (measured).
4. **Predict and code each bit** (the C engine, `engine/cm.c`):
   - 8 context models. Each stores, for its context, a compact count of the 0s and 1s seen after it. The engine turns that count into a probability, and learns as it goes how to do that.
   - 8 run models, which track the last byte seen in each context and how often it repeated.
   - A model that uses no context at all, and a match model that finds the longest earlier repeat of the recent bytes and predicts that it continues.
   - 4 mixers, each chosen by a different context, then a final mixer that combines their outputs. The 4 mixers use 16-bit numbers, so the processor's vector instructions do 16 multiply-adds at once.
   - 2 correction tables (called APM or SSE in the literature) that adjust the final probability based on the previous byte.
   - The arithmetic coder. All of it uses whole-number arithmetic only, so every computer produces the same bits.
5. **Fallback:** if the result is not smaller than the input, the file is stored unchanged. This costs a header of 10–14 bytes.
6. **Integrity:** every file carries a CRC32 checksum of the original. Decompression checks it and refuses to return data that does not match.
7. **File format:** the header names a version from `registry.json`. A version is everything the decoder needs: the engine algorithm and the primers it replays.
   - A version's real identity is a hash of that content, so it means the same thing on every computer. The header usually stores a 1-byte short number for it; it can store the hash instead (16 bytes more), for versions from another registry.
   - The registry carries a hash chain over its entries, and the tests check it, so a changed or removed entry is detected.
   - Large files can be split into blocks, which are decoded on several cores (section 2).

**How the original idea maps to this.** The starting idea was "per file type, find the 10,000 most repeated 30-bit patterns and give them shorter codes". Tested as stated, it gave 75% on Python files against 24% for gzip (measured, `docs/context.md` section 1), so it was rebuilt:
- The per-file-type part survives as the primers: each is the training content whose 8-byte pieces appear in the most training files.
- Fixed 30-bit blocks were replaced by predicting every bit from contexts of many lengths.
- Fixed-length codes were replaced by arithmetic coding.
- An escape mechanism (step 5) and repeats within the same file (the match model) were added.

**Certainty:** tested: two different builds of the engine (`-O0` and `-O3 -march=native`) give identical output, and the tests check every step's inverse.

---

## 6. How to run

**Summary:** Run `python3 mixz.py c FILE` to compress and `python3 mixz.py d FILE.mxz` to decompress.

**Details:**

Requirements:
- Linux on x86-64 (other systems are untested), Python 3.9 or newer, and a C compiler (`cc`, `gcc` or `clang`). The engine compiles itself the first time it runs. The tests ran on Python 3.13 and 3.14; for 3.9–3.12 the code was only checked for syntax.
- Optional: the `zstandard` Python package, used to choose primers when primers are installed.
- The benchmark harness needs `numpy`, `zstandard` and `brotli`; its reference columns also need `cmake` and `g++`.
- Some Linux distributions (for example Arch, and Debian 12 or newer) do not allow `pip install` into the system Python. There, create a private environment once and use its Python for every command: `python3 -m venv .venv && .venv/bin/pip install zstandard numpy brotli`, then `.venv/bin/python mixz.py ...`.

```sh
python3 mixz.py c file.py            # -> file.py.mxz
python3 mixz.py d file.py.mxz        # -> file.py
python3 mixz.py t *.mxz              # check that files decompress correctly
python3 mixz.py info *.mxz           # show format, transform and engine settings used
python3 mixz.py c -j 4 src/*.py      # many files, 4 at a time
python3 mixz.py c --verify big.bin   # decompress after compressing and compare
python3 mixz.py serve &              # keep primed models loaded (useful only with primers installed)
```

Options:
- `--maxbits N` limits model memory. The default, 20, uses at most about 170 MB per file being coded. The decoder reads this value from the file.
- `--block-size N` splits each file into blocks of about N bytes, coded at the same time on several cores (`--block-threads N` limits how many; the output is the same for any number). The default splits only files over 1 GiB (section 2).
- `--no-server` makes a command do its work itself even when a server is running.

**Server (`mixz.py serve`):** a background process that keeps primed models loaded, so a command does not load a primer each time (about 1 s saved per command with a primer). Without primers it saves nothing: a command on a 25 KB file took about 0.13 s with or without the server (measured).
- `c`, `d` and `t` send each file's bytes to the server when one is running, and do the work themselves when none is. The output is the same either way.
- The server listens on a socket file that only your user can open (in `$XDG_RUNTIME_DIR`, or `$MIXZ_SOCKET`), and refuses connections from other users.
- If the `mixz` files change while a server runs, commands notice, print a warning and work without it.
- Stop it with Ctrl-C or `kill`; it removes its socket file.

**From Python:**
```python
import mixz
blob = mixz.compress(data)                      # bytes -> bytes
data = mixz.decompress(blob)
b = mixz.compress_bundle([d1, d2, d3])          # several files as one stream: smaller on small files
files = mixz.decompress_bundle(b)               # the list back
one = mixz.decompress_bundle(b, index=1)        # one member: decodes its group up to that member
```

**Your own primers:** edit the file patterns in `tools/corpora.py`, run `python3 tools/build_primers.py`, and register the new files in `registry.json` as new module and version entries. A file compressed with a primer needs that same primer to decompress.

**Certainty:** tested.

---

## 7. Tests

**Summary:** `python3 tools/test_roundtrip.py` checks that every kind of input decompresses back exactly and that damaged files are rejected; `python3 tests/check_vectors.py` checks that a machine produces the same files as the reference machine.

**Details:**
- **Edge cases:** empty and tiny files, random data, runs of zeros.
- **gzip files:** every header field, extra bytes after the end, several files joined together, damaged data.
- **PNG files:** 1-bit, palette, 16-bit and RGBA images, interlaced images, bad checksums, extra bytes after the end.
- **x86 transform:** 3,000 adversarial buffers. This test exists because the first version of the transform had a real bug, which a checksum caught.
- **Damaged and malicious `.mxz` files:** cut short at every header byte, one bit flipped, impossible sizes, broken block tables, unknown version hashes. Every case must be rejected quickly with an error.
- **Bundles:** every member alone, the whole bundle, damage, nested bundles.
- **Batch mode and server:** the same output as single-file mode.
- **Memory safety:** the engine was also run under AddressSanitizer and UndefinedBehaviorSanitizer, tools that detect invalid memory use and undefined operations. They found nothing.
- **Same output on different machines:** `tests/check_vectors.py` uses files compressed on the reference machine (AMD Ryzen 3 1200).
  - It checks that all of them decompress, and that compressing the originals again gives identical files.
  - The engine has a fast path that uses AVX2 vector instructions and a plain path for processors without them. Both must give identical files.
  - The current engine passed on AMD Zen 1 with gcc at `-O0` and `-O3 -march=native`. The previous engine, with the same arithmetic in the mixer, also passed on an Intel Pentium without AVX2 and on AMD Zen 4 with AVX-512, with gcc and clang. ARM has not been tested.
- **Registry:** the tests recompute the registry's hash chain and fail if a recorded entry was changed or removed, or if a removed entry's number was used again.
- **Benchmark harness:** `python3 tools/harness/run.py` measures every change: size per file type, round trip, independence from thread count and compiler optimization, and speed on one core and on all cores, against a stored baseline. See `tools/harness/README.md`.

**Certainty:** tested.

---

## 8. Files in this project

**Summary:** One Python file for the command line and the file format, C files for the engine plus the built-in zlib compressor, and the tools.

**Details:**
```
mixz.py                  command line and Python interface: detection, transforms, primer choice, file format, bundles, server
engine/cm.c              the engine (codec 3): models, mixers, arithmetic coder (C, no dependencies)
engine/transform.c       the x86 call/jump transform and the PNG row filters
engine/pdeflate.c        the built-in zlib 1.3.1 compressor used to rebuild .gz and PNG files exactly
engine/zlib-1.3.1/       the unmodified zlib 1.3.1 files it uses
registry.json            the file-format registry: engines (codecs), primers, versions, retired numbers, and a hash chain over the entries
primers/                 no primers in this release (README.md explains)
tools/harness/           the benchmark harness: pinned test data, frozen test set, runner, paq8px/zpaq reference builds
tools/test_roundtrip.py  the tests
tests/check_vectors.py   same-output check against the committed vectors
tools/experiments/       scripts behind the measurements in docs/context.md
tools/corpora.py, tools/build_primers.py, tools/overlap.py   primer training split, primer builder, overlap check against training data
tools/tuner/             tuning data and the engine-variant runner used to choose the code and web settings
tools/mind/              public-domain corpus tools for planned primers
tools/bench.py           an older benchmark on the computer's own files
results/harness/         harness results (baselines) per machine
results/compare/         one-stream and speed comparisons, and a reviewer's run with the development primers
docs/context.md          the full development record: every measurement, decision and plan step
CLAUDE.md                instructions for Claude Code, which was used to write most of the code
LICENSE, NOTICE          Apache License 2.0; zlib's notice
```

**Certainty:** not applicable.

---

## 9. License

**Summary:** Apache License 2.0, except the bundled zlib files, which keep the zlib license.

**Details:**
- `mixz` is licensed under the Apache License, Version 2.0 (`LICENSE`). Copyright 2026 The mixz authors (`NOTICE`).
- `engine/zlib-1.3.1/` is zlib 1.3.1, unmodified, under the zlib license (`engine/zlib-1.3.1/LICENSE`).
- The test data, paq8px (GPL-3.0) and zpaq (public domain) are not part of this repository. The harness downloads them from their own sources, each under its own license (`tools/harness/sources.json`, `tools/harness/reference.py`).

**Certainty:** not applicable.
