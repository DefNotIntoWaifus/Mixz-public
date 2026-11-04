"""mixz in Python only: the same compressor as ../mixz.py with its C engine, written in Python.

It reads and writes exactly the same .mxz files as the C version, byte for byte, and is about 400
times slower. It exists so the whole method can be read without knowing C. Reading order:
  formats.py    what each file type gets: detection, gzip/PNG/x86 handling, primer choice
  transform.py  the E8/E9 and PNG filter transforms (engine/transform.c)
  codec.py      the context-mixing model and the arithmetic coder (engine/cm.c)
  deflate.py    zlib 1.3.1's deflate compressor, to rebuild .gz and PNG files exactly
  container.py  the .mxz file format, the registry and bundles

    import mixz_py
    blob = mixz_py.compress(data)
    data = mixz_py.decompress(blob)
"""
from .container import (DEFAULT_MAXBITS, compress, compress_bundle, decompress, decompress_bundle, describe,
                        parse_header)
from .formats import MixzError

__all__ = ["compress", "decompress", "compress_bundle", "decompress_bundle", "describe", "parse_header",
           "MixzError", "DEFAULT_MAXBITS"]
