"""Command line: python3 python/mixz_py c FILE | d FILE.mxz | t FILE.mxz | info FILE.mxz"""
import argparse
import os
import sys
import time

if __package__ in (None, ""):  # run as a folder (python3 python/mixz_py): make the package importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mixz_py import DEFAULT_MAXBITS, MixzError, compress, decompress, describe  # noqa: E402


def parse_size(text: str) -> int:
    """'65536', '64K', '1M' -> bytes."""
    t = text.strip().upper()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(t[-1:], 1)
    return int(t[:-1] if mult > 1 else t) * mult


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mixz_py", description="mixz in Python only (same files as mixz.py)")
    ap.add_argument("cmd", choices=["c", "d", "t", "info"], help="c=compress d=decompress t=test info=describe")
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--output", help="output path (single input only)")
    ap.add_argument("-f", "--force", action="store_true", help="overwrite existing outputs")
    ap.add_argument("--maxbits", type=int, default=DEFAULT_MAXBITS, help="model size cap (16..24, default 20)")
    ap.add_argument("--no-primer", action="store_true", help="do not use per-format primers")
    ap.add_argument("--block-size", type=parse_size, default=-1, metavar="N",
                    help="split each file into blocks of about N bytes (0: one block)")
    ap.add_argument("--verify", action="store_true", help="decompress after compressing and compare")
    args = ap.parse_intermixed_args(argv)
    if args.output and len(args.files) > 1:
        ap.error("-o needs a single input file")
    ok = True
    for path in args.files:
        t = time.time()
        try:
            with open(path, "rb") as f:
                data = f.read()
            if args.cmd == "info":
                print(f"{path}: {describe(data)}")
                continue
            if args.cmd == "t":
                decompress(data)
                print(f"{path}: OK")
                continue
            if args.cmd == "c":
                out = compress(data, maxbits=args.maxbits, use_primers=not args.no_primer, verify=args.verify,
                               block_size=args.block_size)
                dst = args.output or path + ".mxz"
                msg = f"{path}: {len(data)} -> {len(out)} ({len(out) / max(len(data), 1):.1%})"
            else:
                out = decompress(data)
                dst = args.output or (path[:-4] if path.endswith(".mxz") else path + ".out")
                msg = f"{path}: -> {dst} ({len(out)} bytes)"
            if os.path.exists(dst) and not args.force:
                print(f"{path}: {dst} exists (use -f to overwrite)", file=sys.stderr)
                ok = False
                continue
            with open(dst, "wb") as f:
                f.write(out)
            print(f"{msg}  [{time.time() - t:.1f}s]")
        except (OSError, MixzError) as e:
            print(f"{path}: error: {e}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
