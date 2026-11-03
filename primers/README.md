# Primers

A primer is a sample of typical content for one file type. Before a file is coded, the compressor
and the decompressor both run the model over the primer, so the model starts with knowledge of that
file type (`docs/context.md` section 2).

This release ships **no primers**. The development primers were built from system files with many
different licenses, so they cannot be published. Every file this release writes uses no primer, and
the size results in `README.md` are measured that way.

Primers built only from public-domain data are planned. They will get new module IDs in
`registry.json`; IDs 1–8 are retired and never reused.

`tools/build_primers.py` builds primers from your own files. A file compressed with a primer needs
the same primer file, registered in `registry.json`, to decompress.
