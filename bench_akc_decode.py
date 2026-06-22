"""Benchmark + parity harness for the AKC record decoder.

Decodes a fixed slice of CONTSTD.AKC records and reports cold wall-clock plus a
parity signature (sha1 over refid+xml in map order). Run under both CPython and
PyPy with identical args; the signature MUST match byte-for-byte.

Usage: python bench_akc_decode.py [N]
"""
import hashlib
import sys
import time
from pathlib import Path

from strata_akc_dump.cli import _read_key_map_rows
from strata_akc_dump.portable_akc import PortableAkcDecoder

AKC = Path.home() / "Downloads/encarta/EE/ENCARTA/CONTSTD.AKC"
MAP = Path(__file__).parent / "output/key_refid_map.tsv"


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    impl = sys.implementation.name

    t0 = time.perf_counter()
    dec = PortableAkcDecoder(AKC, map_path=MAP)
    rows = _read_key_map_rows(MAP)
    targets = rows[:n]
    t_setup = time.perf_counter() - t0

    h = hashlib.sha1()
    total_bytes = 0
    t1 = time.perf_counter()
    for refid, win in targets:
        article = dec.decode_window(win) if win.body_off >= 0 else dec.decode_key(win.key)
        h.update(refid.to_bytes(4, "little"))
        h.update(article.xml)
        total_bytes += len(article.xml)
    t_decode = time.perf_counter() - t1

    print(f"impl={impl}  records={len(targets)}  decoded_bytes={total_bytes}")
    print(f"setup_s={t_setup:.3f}  decode_s={t_decode:.3f}  "
          f"rec_per_s={len(targets) / t_decode:.1f}")
    print(f"parity_sha1={h.hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
