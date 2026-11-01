# mixz

`mixz` is a lossless file compressor, built as a minimum viable product (MVP).
- **Lossless** means that decompression gives back exactly the original bytes.
- **MVP** means it works and passes its tests, but it is slow and its file format can still change.

**How this document is organized.** Every section has the same three parts:
- **Summary**: the section in one sentence.
- **Details**: the explanation.
- **Certainty**: how sure the claims are. *Measured* means the number comes from a run. *Tested* means the automatic tests check it. *Estimate* means it is reasoned from measurements, or from how similar programs behave, but was not run.

**What to read.** Sections 1–4 take about 6 minutes and are enough to decide whether to use `mixz`. Sections 5–8 are reference material for running or changing it.

---

## 1. Results

**Summary:** On every file type tested, `mixz` produced the smallest files; on text, code and programs its output is 19–34% smaller than the best common compressor.

**Details:**
- **Test files:** real files from an Ubuntu 24.04 virtual machine. A virtual machine is a computer simulated inside another computer; this one had 2 processor cores.
- **Held out:** none of the test files were used to build the primers (section 5).
- **Verified:** each file was compressed on its own, then decompressed and compared byte for byte with the original. All files matched.
- **How to read the table:** each cell is the total compressed size divided by the total original size, so lower is better. The best result in each row is in bold.
- **The other tools:** gzip, xz, zstd and brotli are the common general-purpose compressors. "zstd +dict" is zstd given a 256 KB dictionary (a sample of typical data) built from the same training files that `mixz` uses. That makes it the fairest comparison.

| Files | Count | MB | gzip -9 | xz -9e | zstd -19 | zstd -19 +dict | brotli -q11 | mixz without primer | mixz |
|---|---|---|---|---|---|---|---|---|---|
| Python source | 60 | 1.11 | 22.9% | 21.4% | 21.4% | 18.6% | 19.4% | 16.5% | **13.4%** |
| C headers | 60 | 1.09 | 14.9% | 13.9% | 13.9% | 11.0% | 12.2% | 10.4% | **7.3%** |
| JavaScript | 34 | 3.23 | 30.3% | 23.9% | 24.9% | 24.5% | 23.7% | 19.6% | **19.1%** |
| JSON | 60 | 1.95 | 13.9% | 10.0% | 9.9% | 8.7% | 9.2% | 7.2% | **5.8%** |
| HTML, CSS, XML, SVG | 60 | 0.24 | 28.8% | 28.5% | 27.7% | 17.7% | 23.9% | 23.1% | **11.8%** |
| Prose (md, txt, rst) | 60 | 0.22 | 25.6% | 26.4% | 24.7% | 16.6% | 20.4% | 20.5% | **11.9%** |
| Manual pages (`.gz` files) | 60 | 0.46 | 100.3% | 100.8% | 100.1% | 100.1% | 100.1% | 71.9% | **55.1%** |
| x86-64 programs | 27 | 3.09 | 37.8% | 31.3% | 33.3% | 30.8% | 31.5% | 25.0% | **23.4%** |
| PNG images | 60 | 1.64 | 97.7% | 97.3% | 97.4% | 96.8% | 97.3% | **89.0%** | 89.0% |
| **All of the above** | | 13.1 | 38.5% | 34.6% | 35.3% | 33.5% | 33.9% | 28.7% | **26.5%** |

- **Manual pages:** these files are already compressed (`.gz`), so the other tools cannot shrink them. `mixz` first undoes that compression (section 5, step 2).
- **PNG images:** the gain is small, because most PNG files could not be rebuilt exactly after their compression was undone. Those files are coded as they are.

**Certainty:** measured. The raw numbers are in `results/bench.json`.

---

## 2. Speed

**Summary:** `mixz` spends about 2,100 nanoseconds per byte in both directions, while zstd decompresses in about 1 nanosecond per byte.

**Details:**

Terms used in this section:
- A **nanosecond** (ns) is one billionth of a second.
- A **processor instruction** is one basic operation of the processor, such as an addition or a memory read.
- A **clock cycle** is one tick of the processor clock. The test machine ticks 2.1 billion times per second (2.1 GHz).

Speeds below are for one processor core, on a 1–2 MB text file:

| Tool | Compress | Decompress | Decompression time per byte |
|---|---|---|---|
| mixz | 0.4–0.5 MB/s | 0.4–0.5 MB/s | ~2,100 ns (~4,300 clock cycles) |
| xz -9e | 2.8 MB/s | 87 MB/s | ~11 ns |
| zstd -19 | 3.3 MB/s | 1,086 MB/s | ~0.9 ns (~2 clock cycles) |
| brotli -q11 | not measured | 465 MB/s | ~2 ns |

On top of that, `mixz` has a fixed start-up of about 1.5 s per command when it uses a primer. Peak memory is about 170 MB.

*Example (numbers calculated from the table):* decompressing 1 GB of source code takes zstd about 1 second, xz about 11 seconds, and `mixz` about 35 minutes.

**Why it is slow:**
1. It makes 8 predictions per byte, one per bit, and each prediction must wait for the previous one. Extra cores cannot speed up a single stream.
2. Each prediction is a lot of arithmetic: about 15,000 processor instructions per byte, against about 24 for gzip decompression and 54 for xz decompression. More than half of it is the mixer (section 5).
3. Decompression has to repeat all of that work. gzip, xz and zstd store "copy these bytes from earlier" instructions, so their decompressors only copy. `mixz` stores only how surprising each bit was, so the decompressor must run the same model to recover the bits. This causes the large decompression gap, and it is part of the method, not a defect in the code.
4. Loading a primer costs as much as compressing 512 KB.

Memory is a smaller factor: making the model's tables 16 times smaller made `mixz` only 6% faster.

**How it can be made faster:**

| Change | Expected gain | Price | Certainty |
|---|---|---|---|
| Keep the primed model loaded between files. Batch mode already does this: 60 files in one command took 5.1 s instead of about 45 s. | Removes ~1.5 s per command | Memory stays in use | Measured for batches; estimate for a background process |
| Cheaper mixer arithmetic: 16-bit numbers and the processor's vector instructions, which do 16 multiply-adds in one instruction | 1.5–2× | Little or no size cost | Estimate |
| A fast mode with fewer models | 2–3× | Output roughly 5–10% larger | Estimate |
| Split large files into blocks, one block per core, for both compression and decompression | 1.6× on 2 cores (measured); ~6× on 8 cores (estimate) | Output 4% larger with 4 blocks | Measured on 2 cores |
| Hybrid design: store repeats as copy instructions and model only the new bytes | Decompression roughly 10–50 MB/s | Output size between xz and current `mixz` | Estimate; not built |

**Certainty:** the speeds and causes are measured (instructions counted with cachegrind, a tool that runs a program on a simulated processor). The gains are estimates unless marked measured.

---

## 3. When to use it

**Summary:** Use it for data that is compressed once and read rarely; do not use it for data that is read often, or for photos, video and zip files.

**Details:**
- **Good fit:** backups, archives, long-term storage, and distributing large text, code or program collections that are downloaded once. In these cases storage space or transfer time costs more than processor time.
- **Poor fit:** web pages, databases, logs that are searched often, and anything opened many times a day. Each opening would pay the decompression time from section 2.
- **No gain:** JPEG photos, video, music and zip files. These are already compressed, `mixz` does not handle their formats, and it stores them almost unchanged.

**Certainty:** estimate, based on the measurements in sections 1 and 2.

---

## 4. Known limitations

**Summary:** Speed is the main limitation; format coverage and primer training data are the next ones.

**Details:**
- **Speed:** see section 2.
- **Recompression:** `.gz` and PNG files are only recompressed when the zlib library reproduces their compressed bytes exactly. On the test machine this worked for 79% of `.gz` files and 31% of PNG files. Files made by other programs (for example zopfli or optipng) are coded as they are, which saves little.
- **Formats not handled:** JPEG, MP3, MP4 and ZIP.
- **One file in, one file out:** there is no archive mode, and one file uses one processor core.
- **Untrusted input:** the decoder trusts the sizes written in a file's header, so a deliberately broken `.mxz` file can make it allocate a lot of memory.
- **Primers:** they were built from Ubuntu system files. On other kinds of data the gain over the "mixz without primer" column will be smaller.

**Certainty:** measured for recompression coverage; the rest are facts about the current code.

---

## 5. How it works

**Summary:** `mixz` undoes encodings that hide patterns, loads knowledge of the file type, and then predicts each bit with several models whose guesses a small neural network combines.

**Details:**

Terms:
- **Bit / byte:** a bit is a 0 or a 1. A byte is 8 bits. A file is a sequence of bytes.
- **Model:** the part that guesses the next bit, as a probability. Better guesses give smaller output.
- **Context:** what a model looks at when guessing, for example the previous 3 bytes, the current word, or the line above.
- **Arithmetic coding:** a way of writing bits where each bit costs −log₂(the probability the model gave it). *Example:* a bit predicted with 99% probability costs about 0.015 bits, and a bit predicted with 1% probability costs about 6.6 bits.
- **Mixer:** a small neural network that combines the guesses of all models into one guess, and learns while it runs how much to trust each model in each situation. Using one is called **context mixing**.
- **Primer:** a 512 KB sample of typical content for one file type. Before the file starts, both the compressor and the decompressor run the model over the primer, so the model starts with knowledge of that file type. The primer is never written into the output.
- **Deflate:** the compression used inside `.gz`, `.zip` and PNG files.
- **Transform:** a reversible change to the bytes that makes patterns easier to predict.
- **Checksum (CRC32):** a 4-byte number computed from the file's contents. If the contents change, the number almost certainly changes, so a mismatch shows corruption.

Steps, for each file:
1. **Detect the format from the content,** not from the file name: gzip, PNG, x86 program, text, or other binary data.
2. **Undo encodings, but only if the original bytes can be rebuilt exactly.** The rebuild is checked before it is used.
   - `.gz`: decompress the content, then search for the zlib settings that recreate the exact original compressed bytes, and store those settings (3 bytes).
   - PNG: decompress the image data and undo the PNG row filters, so the model sees the actual pixels and the row above them.
   - x86 programs: call and jump instructions store their target as a distance from the current position, so calls to the same function look different at each location. The transform rewrites them as absolute positions, which repeat.
3. **Pick a primer.** Each candidate primer is tried on a 64 KB sample of the file using zstd, which takes milliseconds. A candidate is chosen only if it does at least 1% better than the current best; the starting choice is no primer.
4. **Predict and code each bit** (the C engine, `engine/mixz.c`):
   - 8 context models. Each stores, for its context, a compact count of the 0s and 1s seen after it. The engine turns that count into a probability, and learns as it goes how to do that.
   - 8 run models, which track the last byte seen in each context and how often it repeated.
   - A model that uses no context at all, and a match model that finds the longest earlier repeat of the recent bytes and predicts that it continues.
   - 4 mixers, each chosen by a different context, then a final mixer that combines their outputs.
   - 2 correction tables (called APM or SSE in the literature) that adjust the final probability based on the previous byte.
   - The arithmetic coder.
5. **Fallback:** if the result is not smaller than the input, the file is stored unchanged. This costs a header of 10–14 bytes.
6. **Integrity:** every file carries a CRC32 checksum of the original. Decompression checks it and refuses to return data that does not match.

**How the original idea maps to this.** The starting idea was "per file type, find the 10,000 most repeated 30-bit patterns and give them shorter codes".
- The per-file-type part survives as the primers. Each primer is the 512 KB of training content whose 8-byte pieces appear in the most training files. That is "the most repeated patterns per file type", but with patterns of any length that start on byte boundaries.
- Fixed 30-bit blocks were replaced by predicting every bit from contexts of many lengths.
- Fixed-length codes were replaced by arithmetic coding.
- An escape mechanism (step 5) and repeats within the same file (the match model) were added.

**Primers and the train/test split.** `tools/build_primers.py` builds the primers from the training half of the files only. A file is in the training half if a hash of its path, with version numbers removed, is even. The benchmark tests only on the other half, and skips any test file whose content also appears in the training half. The decoder needs the same primer files the encoder used. Each compressed file records a 16-bit tag of its primer, so decompressing with a different primer stops with an error instead of producing wrong output.

| Primer (40 held-out files) | Python | x86 programs |
|---|---|---|
| None | 17.56% | 27.30% |
| Random training files, 512 KB | 14.24% | 25.40% |
| Most repeated segments, 512 KB (used) | **13.67%** | **25.14%** |

**Certainty:** the primer table is measured. That a file compressed on one machine decompresses on another is tested: all engine arithmetic uses whole numbers, and two different builds of the engine (`-O0` and `-O3 -march=native`) produced identical output.

---

## 6. How to run

**Summary:** Run `python3 mixz.py c FILE` to compress and `python3 mixz.py d FILE.mxz` to decompress.

**Details:**

Requirements:
- Python 3.9 or newer, and a C compiler (`cc`, `gcc` or `clang`). The engine compiles itself the first time it runs.
- Optional: the `zstandard` Python package, which makes primer choice more accurate. Without it, `mixz` uses zlib for that test.
- Building new primers needs `numpy`.

```sh
python3 mixz.py c file.py            # -> file.py.mxz
python3 mixz.py d file.py.mxz        # -> file.py
python3 mixz.py t *.mxz              # check that files decompress correctly
python3 mixz.py info *.mxz           # show format, transform and primer used
python3 mixz.py c -j 4 src/*.py      # many files: each primer loaded once, 4 files at a time
python3 mixz.py c --verify big.bin   # decompress after compressing and compare
```

Options:
- `--maxbits N` limits model memory. The default, 20, uses at most about 170 MB. The decoder reads this value from the file.
- `--no-primer` turns primers off.

From Python: `mixz.compress(data: bytes) -> bytes` and `mixz.decompress(blob: bytes) -> bytes`.

To train primers on your own kind of files:
1. Edit the file patterns in `tools/corpora.py`.
2. Run `python3 tools/build_primers.py`.

Keep the old `primers/` folder for any files that were compressed with it.

**Certainty:** tested.

---

## 7. Tests

**Summary:** `python3 tools/test_roundtrip.py` checks that every kind of input decompresses back exactly, and that damaged files are rejected.

**Details:**
- **Edge cases:** empty and tiny files, random data, runs of zeros.
- **gzip files:** every header field, extra bytes after the end, several files joined together, damaged data.
- **PNG files:** 1-bit, palette, 16-bit and RGBA images, interlaced images, bad checksums, extra bytes after the end.
- **x86 transform:** 3,000 adversarial buffers. This test exists because the first version of the transform had a real bug, which the benchmark's checksum caught.
- **Damaged `.mxz` files:** cut short or with one bit flipped.
- **Batch mode:** produces the same output as single-file mode.
- **Memory safety:** the engine was also run under AddressSanitizer and UndefinedBehaviorSanitizer, tools that detect invalid memory use and undefined operations. They found nothing.
- **Benchmark:** `python3 tools/bench.py` reproduces section 1 in about 10 minutes on 2 cores.

**Certainty:** tested.

---

## 8. Files in this project

**Summary:** One Python file for the command line, one C file for the engine, the primers, and the tools.

**Details:**
```
mixz.py                  command line and Python interface: detection, transforms, primer choice, file format
engine/mixz.c            the engine: models, mixers, arithmetic coder (C, no dependencies)
primers/*.bin            512 KB primer per file type (needed for decompression)
results/bench.json       raw benchmark numbers behind section 1
tools/corpora.py         which files are used for training and testing, and the split rule
tools/build_primers.py   builds the primers
tools/bench.py           the benchmark
tools/test_roundtrip.py  the tests
tools/experiments/       scripts behind the measurements in docs/context.md
docs/context.md          full project context: measurements, decisions, plan, business
CLAUDE.md                instructions for Claude Code
```

**Certainty:** not applicable.
