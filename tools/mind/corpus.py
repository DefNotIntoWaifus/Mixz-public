#!/usr/bin/env python3
"""Public-domain corpus for the mind modules (plan step 7): the data primers are built from.

Decided 2026-10-25: public domain only. A source qualifies when its authors dedicated it to the
public domain (an explicit statement, CC0 1.0 or the Unlicense; a dual licence where one option is
one of these counts) or when it is out of copyright (Project Gutenberg books whose every contributor
died before 1945, so they are free in the EU and the US). Each source's 'evidence' says
where its status is stated. Within a source a file is dropped when
  - it carries another licence's text or a copyright notice anywhere (third-party code a project
    includes); only in a source marked 'dual' (a project whose own files offer a public-domain
    option next to another licence, checked by hand) may such a file pass, and only if it also
    states the public-domain option;
  - it lies in a vendored or generated directory (EXCLUDE);
  - its content is in the harness test set or the tuner sets, or it repeats an earlier file.

  corpus.py --discover   add CC0 and Unlicense repositories of ORGS and REPOS from GitHub (needs gh)
  corpus.py --pin        download every source and write its sha256 into sources.json
  corpus.py              extract every source and print the bytes per kind
"""
import argparse
import fnmatch
import gzip
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TOOLS, "harness"))
sys.path.insert(0, os.path.dirname(TOOLS))
import fetch  # noqa: E402
import mixz  # noqa: E402

SOURCES = os.path.join(HERE, "sources.json")
KINDS = ["py", "c", "js", "json", "web", "prose", "man"]
KIND_GLOBS = {
    "py": ["*.py"],
    "c": ["*.c", "*.h"],
    "js": ["*.js", "*.mjs", "*.cjs"],
    "json": ["*.json", "*.geojson"],
    "web": ["*.html", "*.htm", "*.css", "*.svg", "*.xml"],
    "prose": ["*.md", "*.rst", "*.txt"],
    "man": [f"*.{n}" for n in range(1, 9)],
}
EXCLUDE = ["*/node_modules/*", "*/vendor/*", "*/vendors/*", "*/third_party/*", "*/third-party/*", "*/thirdparty/*",
           "*/external/*", "*/extern/*", "*/deps/*", "*/dist/*", "*/build/*", "*/site-packages/*", "*/.git/*",
           "*.min.js", "*.min.css", "*package-lock.json", "*/yarn.lock", "*.map", "*/LICENSE*", "*/COPYING*",
           "*/NOTICE*", "*/requirements*.txt", "*/CHANGELOG*", "*/.github/*"]
MIN_FILE, MAX_FILE = 128, 1024 * 1024
# another licence's text, or a copyright notice, in a file's first 4 KB
FOREIGN = re.compile(rb"permission is hereby granted|redistribution and use in source and binary forms|"
                     rb"permission to use, copy, modify|general public license|apache license|mozilla public license|"
                     rb"eclipse public license|this software is provided 'as-is'|creative commons attribution|"
                     rb"all rights reserved|copyright \(c\)|copyright \xc2\xa9|\(c\) (19|20)\d\d|copyright (19|20)\d\d|"
                     rb"spdx-license-identifier|mit licen[cs]e|bsd licen[cs]e|isc licen[cs]e|@license|licensed under|"
                     rb"under the terms of", re.I)
PD_WORDS = re.compile(rb"public domain|disclaims copyright|cc0|unlicense", re.I)
GH_LICENSES = {"cc0-1.0": "CC0-1.0", "unlicense": "Unlicense"}
# organisations that dedicate their code with CC0 (US federal agencies), and single repositories
ORGS = ["cisagov", "18F", "GSA", "CMSgov", "uswds"]
REPOS = ["yt-dlp/yt-dlp", "pytube/pytube", "SystemRage/py-kms", "IndieSmiths/nodezator",
         "scholarly-python-package/scholarly", "libtom/libtomcrypt", "libtom/libtommath", "lieff/minimp3",
         "troglobit/mg", "scandum/quadsort", "scandum/fluxsort", "wangyi-fudan/wyhash", "joan2937/pigpio",
         "DragonMinded/libdragon", "jslint-org/jslint", "dchest/tweetnacl-js", "Olical/EventEmitter",
         "jankovicsandras/imagetracerjs", "text-mask/text-mask", "dynamoose/dynamoose", "dariusk/corpora",
         "unitedstates/congress-legislators", "json-api/json-api", "simple-icons/simple-icons"]


def load() -> dict:
    with open(SOURCES) as f:
        return json.load(f)


def save(s: dict) -> None:
    with open(SOURCES, "w") as f:
        f.write('{\n "comment": ' + json.dumps(s["comment"]) + ',\n "excluded": ' + json.dumps(s.get("excluded", []))
                + ',\n "sources": [\n')
        f.write(",\n".join("  " + json.dumps(e) for e in s["sources"]))
        f.write("\n ]\n}\n")


def gh(path: str, **params):
    cmd = ["gh", "api", "-X", "GET", path] + [a for k, v in params.items() for a in ("-f", f"{k}={v}")]
    if os.path.exists("/.flatpak-info"):  # inside the VS Code flatpak, gh runs on the host
        cmd = ["flatpak-spawn", "--host"] + cmd
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"gh api {path}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def gh_source(full: str, licence: str) -> dict:
    """A source for a GitHub repository at its current default-branch commit."""
    sha = gh(f"repos/{full}/commits/HEAD")["sha"]
    return {"id": "gh-" + full.replace("/", "-").lower(), "url": f"https://codeload.github.com/{full}/tar.gz/{sha}",
            "archive": "tar", "license": licence, "evidence": f"GitHub licence detection of {full} at {sha[:12]}",
            "kinds": KIND_GLOBS}


def discover(s: dict) -> None:
    have = {e["id"] for e in s["sources"]}
    found = []
    for org in ORGS:
        for key, spdx in GH_LICENSES.items():
            page = gh("search/repositories", q=f"org:{org} license:{key} fork:false archived:false", per_page=100)
            found += [(it["full_name"], spdx) for it in page["items"]]
    for full in REPOS:
        lic = (gh(f"repos/{full}").get("license") or {}).get("key")
        if lic in GH_LICENSES:
            found.append((full, GH_LICENSES[lic]))
        else:
            print(f"  skipped {full}: GitHub licence {lic}", file=sys.stderr)
    for full, spdx in sorted(set(found)):
        sid = "gh-" + full.replace("/", "-").lower()
        if sid not in have:
            s["sources"].append(gh_source(full, spdx))
            have.add(sid)
            print(f"  added {full} ({spdx})", file=sys.stderr)


def protected_hashes() -> set:
    """Content of the harness test set and the tuner sets: never in a primer."""
    out = set()
    with open(fetch.SETS) as f:
        out |= {e["sha256"] for ks in json.load(f)["kinds"].values() for e in ks["files"]}
    with open(os.path.join(TOOLS, "tuner", "sets.json")) as f:
        out |= {e["sha256"] for ks in json.load(f)["kinds"].values() for sp in ks.values() for e in sp["files"]}
    return out


def gutenberg_body(b: bytes) -> bytes:
    """A Project Gutenberg text without its licence header and footer."""
    a = re.search(rb"\*\*\* ?START OF (THE|THIS) PROJECT GUTENBERG[^\n]*\n", b)
    z = re.search(rb"\*\*\* ?END OF (THE|THIS) PROJECT GUTENBERG", b)
    return b[a.end(): z.start() if z else len(b)].strip() if a else b""


def accept(kind: str, name: str, b: bytes, exclude: list = (), dual: bool = False) -> bool:
    if not MIN_FILE <= len(b) <= MAX_FILE or any(fnmatch.fnmatchcase(name, p) for p in EXCLUDE + list(exclude)):
        return False
    head = b[:4096]
    if FOREIGN.search(b) and not (dual and PD_WORDS.search(b)):
        return False
    if kind == "man" and not re.search(rb"^\.(TH|Dd) ", head, re.M):
        return False
    return mixz.text_score(b[:65536]) > 0.97


def members(src: dict, path: str):
    """(name, bytes) of the source's members that match a kind, skipping members above MAX_FILE
    before reading them (a data repository can hold files of hundreds of MB)."""
    want = lambda n: bool(fetch.kinds_of(src, n))  # noqa: E731
    t = fetch.archive_type(src)
    if t == "tar":
        import tarfile
        with tarfile.open(path, "r:*") as tf:
            for m in tf:
                name = fetch.safe_name(m.name)
                if m.isfile() and name and m.size <= MAX_FILE and want(name):
                    yield name, tf.extractfile(m).read()
    elif t == "zip":
        import zipfile
        with zipfile.ZipFile(path) as zf:
            for zi in zf.infolist():
                name = fetch.safe_name(zi.filename)
                if not zi.is_dir() and name and zi.file_size <= MAX_FILE and want(name):
                    yield name, zf.read(zi)
    else:
        yield from fetch.members(src, path, want)


def extract(src: dict) -> dict:
    """The source's matching members in the cache (mind/ID/), with an index {name: {kinds, bytes}}."""
    out = os.path.join(fetch.cache_dir(), "mind", src["id"])
    index_path = os.path.join(out, ".index.json")
    key = fetch.source_key(src) + json.dumps(src.get("exclude", []))
    if os.path.exists(index_path):
        with open(index_path) as f:
            index = json.load(f)
        if index.get("key") == key:
            return index["files"]
    path = fetch.download(src)
    if os.path.isdir(out):
        import shutil
        shutil.rmtree(out)
    index = {}
    for name, data in members(src, path):
        dest = os.path.join(out, *name.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        index[name] = {"kinds": fetch.kinds_of(src, name), "bytes": len(data)}
    os.makedirs(out, exist_ok=True)
    with open(index_path, "w") as f:
        json.dump({"key": key, "files": index}, f)
    return index


def files(s: dict):
    """Yield (kind, source id, name, bytes) of every accepted file, without repeats or protected content."""
    seen = protected_hashes()
    for src in s["sources"]:
        index = extract(src)
        for name in sorted(index):
            kinds = [k for k in KINDS if k in index[name]["kinds"]]
            if not kinds:
                continue
            with open(os.path.join(fetch.cache_dir(), "mind", src["id"], *name.split("/")), "rb") as f:
                b = f.read()
            if src.get("gutenberg"):
                b = gutenberg_body(b)
            elif name.endswith(".gz"):
                b = gzip.decompress(b)
            h = fetch.sha256_bytes(b)
            if h not in seen and accept(kinds[0], name, b, src.get("exclude", []), src.get("dual", False)):
                seen.add(h)
                yield kinds[0], src["id"], name, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--pin", action="store_true")
    args = ap.parse_args()
    s = load()
    if args.discover:
        discover(s)
        save(s)
    if args.pin:
        for src in s["sources"]:
            if not src.get("sha256"):
                fetch.download(src, pin=True)
                save(s)
    if not (args.discover or args.pin):
        total = {k: 0 for k in KINDS}
        nfiles = {k: 0 for k in KINDS}
        for kind, _, _, b in files(s):
            total[kind] += len(b)
            nfiles[kind] += 1
        for k in KINDS:
            print(f"{k:6s} {nfiles[k]:6d} files {total[k] / 1e6:8.2f} MB")


if __name__ == "__main__":
    main()
