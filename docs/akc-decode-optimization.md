# TODO: optimize the AKC content/data decode

**Status:** open · identified 2026-06-19 · the remaining pure-Python bottleneck.

The LZX/EIT path is solved — native arm64 LZX (`native/build/lzx.so`, from
calibre/libmspack) made the full 119-container quarry asset pass ~2 min
(**~185×** over pure-Python, byte-identical). The **AKC record decoder is now the
slow part** and gates content/media build times.

## Symptom

- Full content build (`CONT*.AKC` → articles): **~76 min**.
- Media phase (`DATA*.AKC` → media/article_media link, NEX-392): similarly slow
  — building/decoding ~120k-record maps per family. Left incomplete (`media = 0`
  in `quarry/build/encarta.sqlite`) because it's too slow to wait on.

## Where the time goes (profiled, 60 real CONTSTD records)

~70% of decode time is **`CanonicalHuffman.decode`** (`strata_akc_dump/akc_codec.py:259`)
— a **bit-by-bit Huffman walk** (`code = (code << 1) | br.read(1)` per bit;
1.97M calls / 11.2M `read(1)` calls). Used on the hot paths:

- `decode_tokens` (`portable_akc.py:481`) — the main content token stream
- `decode_dictionary` — the DA30 dictionary build (`selector`/`table3`/`table4`)

Irony: `akc_codec.py` already has a fast two-level table decoder
(`build_decode_table` / `decode_symbol`) — it's just not used on these paths.

> Caveat: cProfile over-weights call-heavy code. Measure **cold wall-clock**, not
> profiler proportions (this burned us on the LZX work).

## Why this is harder than LZX

LZX was fixed by dropping in native C — that library already existed. **AKC has
no equivalent**: it's our own reverse-engineered format, no upstream C decoder.
So the levers, roughly in effort order:

1. **Table-drive `CanonicalHuffman.decode`** — point the hot paths at
   `build_decode_table`, or give the class a primary lookup table like we did for
   LZX's `_HTree`. *Expected: modest.* The LZX precedent showed table-driving
   Huffman alone gave only ~1.1–1.5× real wall-clock, and the AKC loop has no
   debug-string-style waste to delete either.
2. **PyPy** — JIT the same pure-Python. The per-symbol loop is PyPy's best case
   (potentially ~5–10×, **zero code change**). Strongest effort-to-payoff.
3. **C extension for the AKC codec** — reimplement DA30/E560/E770 + Huffman in C.
   Biggest win, biggest effort. Only worth it if this becomes a recurring cost.

## Acceptance

- Profile cold wall-clock first; pick a lever.
- **Parity-check byte-for-byte** against the content already decoded into
  `quarry/build/encarta.sqlite` (re-decode a sample, diff).
- Target: content + media builds in **minutes, not tens of minutes**.

See also: Linear NEX-394 (comment, 2026-06-19) and the native LZX work in
`native/lzx/` + `lit._lzx()`.
