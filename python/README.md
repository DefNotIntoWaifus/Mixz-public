# mixz in Python

[English](#english) · [Español](#español) · [中文](#中文)

## English

The whole compressor in Python only, for reading without knowing C. It writes and reads the same `.mxz` files as `mixz.py` with its C engine, byte for byte: `test_python.py` checks this against the committed test vectors made by the C version.

It is about 400 times slower than the C version (1.4 KB per second against 500 KB per second, on one core, for a 20 KB Python file; measured), so it is for reading, not for use.

| File | What it does | C original |
|---|---|---|
| `mixz_py/formats.py` | detection; gzip, PNG and x86 handling; primer choice | `mixz.py` |
| `mixz_py/transform.py` | x86 E8/E9 transform, PNG row filters | `engine/transform.c` |
| `mixz_py/codec.py` | the models, the mixers and the arithmetic coder | `engine/cm.c` |
| `mixz_py/deflate.py` | zlib 1.3.1's deflate, to rebuild `.gz` and PNG files exactly | `engine/zlib-1.3.1/` |
| `mixz_py/container.py` | the `.mxz` format, the registry, bundles | `mixz.py` |

```sh
python3 python/mixz_py c file           # -> file.mxz
python3 python/mixz_py d file.mxz       # -> file
python3 python/test_python.py --quick   # same files as the C version (about 4 minutes; without --quick about 8)
```

Requires Python 3.9 or newer and no other packages. The standard library's `zlib` is used to decompress, for checksums, and to test whether a file is already compressed; never to produce the bytes of a file.

## Español

El compresor completo solo en Python, para leerlo sin saber C. Escribe y lee los mismos archivos `.mxz` que `mixz.py` con su motor en C, byte a byte: `test_python.py` lo comprueba con los vectores de prueba del repositorio, hechos con la versión en C.

Es unas 400 veces más lento que la versión en C (1,4 KB por segundo frente a 500 KB por segundo, en un núcleo, con un archivo Python de 20 KB; medido), así que sirve para leerlo, no para usarlo.

| Archivo | Qué hace | Original en C |
|---|---|---|
| `mixz_py/formats.py` | detección; gzip, PNG y x86; elección del *primer* | `mixz.py` |
| `mixz_py/transform.py` | transformación E8/E9 de x86, filtros de fila de PNG | `engine/transform.c` |
| `mixz_py/codec.py` | los modelos, los mezcladores y el codificador aritmético | `engine/cm.c` |
| `mixz_py/deflate.py` | el deflate de zlib 1.3.1, para rehacer `.gz` y PNG exactos | `engine/zlib-1.3.1/` |
| `mixz_py/container.py` | el formato `.mxz`, el registro, los paquetes (*bundles*) | `mixz.py` |

```sh
python3 python/mixz_py c archivo           # -> archivo.mxz
python3 python/mixz_py d archivo.mxz       # -> archivo
python3 python/test_python.py --quick      # mismos archivos que la versión en C (unos 4 minutos; sin --quick unos 8)
```

Requiere Python 3.9 o posterior y ningún otro paquete. El `zlib` de la biblioteca estándar se usa para descomprimir, para sumas de verificación y para ver si un archivo ya está comprimido; nunca para producir los bytes de un archivo.

## 中文

完全用 Python 写成的整个压缩器，不懂 C 也能读懂。它与带 C 引擎的 `mixz.py` 读写完全相同的 `.mxz` 文件，逐字节一致：`test_python.py` 用仓库中由 C 版本生成的测试向量进行验证。

它比 C 版本慢约 400 倍（单核处理 20 KB 的 Python 文件时，每秒 1.4 KB 对比每秒 500 KB；实测），因此用于阅读，而不是实际使用。

| 文件 | 作用 | C 原版 |
|---|---|---|
| `mixz_py/formats.py` | 格式检测；gzip、PNG 和 x86 处理；primer 选择 | `mixz.py` |
| `mixz_py/transform.py` | x86 E8/E9 变换，PNG 行过滤器 | `engine/transform.c` |
| `mixz_py/codec.py` | 模型、混合器和算术编码器 | `engine/cm.c` |
| `mixz_py/deflate.py` | zlib 1.3.1 的 deflate，用于精确重建 `.gz` 和 PNG 文件 | `engine/zlib-1.3.1/` |
| `mixz_py/container.py` | `.mxz` 文件格式、注册表、打包（bundle） | `mixz.py` |

```sh
python3 python/mixz_py c file           # -> file.mxz
python3 python/mixz_py d file.mxz       # -> file
python3 python/test_python.py --quick   # 与 C 版本文件相同（约 4 分钟；不加 --quick 约 8 分钟）
```

只需要 Python 3.9 或更新版本，不需要其他包。标准库的 `zlib` 用于解压、计算校验和以及判断文件是否已被压缩，从不用于生成文件中的字节。
