# strata-akc-portable

Portable extraction tools for Microsoft Encarta 2009 AKC/LIT content records.

This is the clean, maintained codebase split out from the reverse-engineering
workspace. It contains the portable decoder only: no Windows COM hooks, no injected
DLLs, and no forensic capture scripts.

## What Works

- `CONT*.AKC`: article/content XML records
- `DATA*.AKC`: data/media metadata XML records
- `DATAF*.AKC`: index/data-family XML records
- `REL*.AKC`: related-article XML records
- `CATALOG.STE` and `.EIT` extraction through the LIT reader

The tiny source-selection files such as `DataESK.akc` are not content corpora and do
not use the same AKC record codec.

## Platform

The AKC decoder is pure Python and runs on Windows, macOS, and Linux with Python 3.10+.

The LIT/EIT extractor needs an LZX backend. On Windows, the old workspace used a
libmspack shim DLL. On macOS/Linux, set `STRATA_MSPACK_LZX_DLL` to a compatible
`.dylib`/`.so` shim when you need known-good extraction.

`STRATA_USE_PY_LZX=1` forces the bundled pure-Python fallback. It is quiet, covered by
format tests, and has been byte-checked against the native libmspack shim for the real
Encarta `CATALOG.STE` catalog streams (`content.ecn`, `data.ecn`, and `baggage.ecs`)
and representative main-content `.EIT` files (`MDSTD.EIT`, `MSWORLD.EIT`, and a
`CONTENT/WORLD/7015` asset bundle). Keep a golden fixture in CI before relying on
changes to this path.

Some support/dictionary `.EIT` containers still need separate LIT reset/framing work:
`TIMELINE.EIT`, `MINDMAZE.EIT`, and `EDICT/ENG_FRA.EIT` currently fail in the shared
LIT path before Python-vs-native fallback parity can be claimed.

## Install

```bash
pip install -e .
```

The console command is available as both `strata-akc` and `strata-akc-dump`.

## AKC Usage

Build a key/refid map:

```bash
strata-akc map-akc --akc CONTSTD.AKC --out output/contstd_map.tsv
```

Decode one record by refid:

```bash
strata-akc decode-article --akc CONTSTD.AKC --map output/contstd_map.tsv \
  --refid 761552164 --out dump/articles/761552164.xml
```

Decode all records present in a map:

```bash
strata-akc decode-portable --akc CONTSTD.AKC --map output/contstd_map.tsv \
  --out dump/articles
```

Use a separate map/output directory for each AKC family, for example:

```bash
strata-akc map-akc --akc DATASTD.AKC --out output/datastd_map.tsv
strata-akc decode-portable --akc DATASTD.AKC --map output/datastd_map.tsv \
  --out dump/data
```

## Required Encarta AKC Families

- Standard corpus: `CONTSTD.AKC`, `DATASTD.AKC`, `DATAFSTD.AKC`, `RELSTD.AKC`
- Deluxe/Premium corpus: `CONTDLX.AKC`, `DATADLX.AKC`, `DATAFDLX.AKC`, `RELDLX.AKC`
- Student/compact subset: `CONTSTC.AKC`, `DATASTC.AKC`, `DATAFSTC.AKC`, `RELSTC.AKC`
- Kids corpus: `EE/KIDS/CONTKDC.AKC`, `DATAKDC.AKC`, `DATAFKDC.AKC`, `RELKDC.AKC`

## LIT/EIT Usage

Enumerate the catalog:

```bash
strata-akc enumerate --catalog CATALOG.STE --out index
```

Extract LIT/EIT containers:

```bash
strata-akc extract-lit MDSTD.EIT MDSTD01.EIT MDSTD02.EIT MDSTD03.EIT --out dump
```

## LZX Fallback Tests

The fallback test suite includes a synthetic uncompressed LZX block and an optional
real-fixture parity check. To exercise a captured golden pair:

```bash
STRATA_USE_PY_LZX=1 \
STRATA_LZX_GOLDEN_COMP=/path/to/compressed.lzx \
STRATA_LZX_GOLDEN_RAW=/path/to/decompressed.bin \
python -m unittest tests.test_lzx_fallback
```

If `STRATA_MSPACK_LZX_DLL` is also set, the same fixture is compared against the native
libmspack shim.

## License Note

`lit.py` derives from calibre's GPLv3 LIT reader. Keep downstream distribution choices
aligned with that license. The extracted data is separate from this extractor code.
