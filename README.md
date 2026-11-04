# mixz

[English](#english) · [Español](#español) · [中文](#中文)

## English

A lossless context-mixing compressor: several models predict each bit, and a small neural network combines their predictions. Before modeling it undoes gzip, PNG and x86 encodings, so the models see the underlying data.

> **Experimental.** The file format can still change. Keep your originals.

Size after compression, for 3,815 files (30.6 MB) compressed one at a time. Lower is better:

| gzip -9 | xz -9e | zstd -19 | brotli -q11 | zpaq -m5 | paq8px -8 | mixz |
|---:|---:|---:|---:|---:|---:|---:|
| 43.2% | 40.8% | 40.8% | 39.0% | 39.7% | **32.3%** | 33.8% |

- Smaller than gzip, xz, zstd, brotli and zpaq on all 9 file types tested.
- paq8px is smaller on 7 of the 9 types. mixz is smaller on `.gz` and PNG, which it recompresses.
- About 0.7 MB/s per core, both to compress and to decompress. That is about 100 times faster than paq8px, and decompression is about 1,400 times slower than zstd.
- No gain on JPEG, MP3, MP4 or ZIP.

```sh
python3 mixz.py c file           # -> file.mxz
python3 mixz.py d file.mxz       # -> file
python3 tools/test_roundtrip.py  # tests
```

Requires Linux on x86-64, Python 3.9 or newer (tested on 3.13 and 3.14) and a C compiler. Measured on an AMD Ryzen 3 1200, 2026-11-01. The full results and the design are in `docs/context.md`. `python/` has the same compressor in Python only, for reading without knowing C: same files, about 400 times slower. License: Apache-2.0.

## Español

Compresor sin pérdida por mezcla de contextos (*context mixing*): varios modelos predicen cada bit y una pequeña red neuronal combina sus predicciones. Antes de modelar deshace la codificación de gzip, PNG y programas x86, para que los modelos vean los datos originales.

> **Experimental.** El formato de archivo aún puede cambiar. Guarda los originales.

Tamaño tras la compresión, para 3.815 archivos (30,6 MB) comprimidos de uno en uno. Cuanto menor, mejor:

| gzip -9 | xz -9e | zstd -19 | brotli -q11 | zpaq -m5 | paq8px -8 | mixz |
|---:|---:|---:|---:|---:|---:|---:|
| 43,2 % | 40,8 % | 40,8 % | 39,0 % | 39,7 % | **32,3 %** | 33,8 % |

- Comprime más que gzip, xz, zstd, brotli y zpaq en los 9 tipos de archivo probados.
- paq8px comprime más en 7 de los 9 tipos. mixz comprime más en `.gz` y PNG, porque los recomprime.
- Unos 0,7 MB/s por núcleo, tanto al comprimir como al descomprimir. Es unas 100 veces más rápido que paq8px, y descomprime unas 1.400 veces más despacio que zstd.
- No reduce JPEG, MP3, MP4 ni ZIP.

```sh
python3 mixz.py c archivo          # -> archivo.mxz
python3 mixz.py d archivo.mxz      # -> archivo
python3 tools/test_roundtrip.py    # pruebas
```

Requiere Linux en x86-64, Python 3.9 o posterior (probado en 3.13 y 3.14) y un compilador de C. Medido en un AMD Ryzen 3 1200 el 1 de noviembre de 2026. Los resultados completos y el diseño están en `docs/context.md` (en inglés). En `python/` está el mismo compresor solo en Python, para leerlo sin saber C: mismos archivos, unas 400 veces más lento. Licencia: Apache-2.0.

## 中文

无损上下文混合（context mixing）压缩器：多个模型分别预测每一个比特，再由一个小型神经网络合并它们的预测。建模前先解开 gzip、PNG 和 x86 程序原有的编码，让模型直接看到原始数据。

> **实验性项目。** 文件格式仍可能改变，请保留原始文件。

3,815 个文件（30.6 MB）逐个压缩后的大小，越小越好：

| gzip -9 | xz -9e | zstd -19 | brotli -q11 | zpaq -m5 | paq8px -8 | mixz |
|---:|---:|---:|---:|---:|---:|---:|
| 43.2% | 40.8% | 40.8% | 39.0% | 39.7% | **32.3%** | 33.8% |

- 在测试的全部 9 种文件类型上都比 gzip、xz、zstd、brotli 和 zpaq 更小。
- paq8px 在 9 种类型中的 7 种上更小。mixz 在 `.gz` 和 PNG 上更小，因为它会对这两种格式重新压缩。
- 单核压缩和解压速度都约为 0.7 MB/s：比 paq8px 快约 100 倍，解压比 zstd 慢约 1,400 倍。
- 对 JPEG、MP3、MP4 和 ZIP 文件没有压缩效果。

```sh
python3 mixz.py c file           # -> file.mxz
python3 mixz.py d file.mxz       # -> file
python3 tools/test_roundtrip.py  # 测试
```

需要 x86-64 Linux、Python 3.9 或更新版本（已在 3.13 和 3.14 上测试）以及 C 编译器。测量环境：AMD Ryzen 3 1200，2026-11-01。完整结果和设计见 `docs/context.md`（英文）。`python/` 中是纯 Python 版本的同一个压缩器，不懂 C 也能阅读：文件相同，速度慢约 400 倍。许可证：Apache-2.0。
