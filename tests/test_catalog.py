"""Portable tests — run on any OS, no msitss."""
import struct
from strata_akc_dump.catalog import parse_content_ids, KNOWN_IDS


def test_parse_content_ids_finds_known():
    # synthetic data.ecn: header noise, then a sorted ID run including the known IDs
    header = b"ECN\x00" + struct.pack("<8L", *[0x002f0000 | i for i in range(8)])
    ids = list(range(0x2D64A480, 0x2D64A4A0)) + list(range(0x67CE8830, 0x67CE8850))
    body = b"".join(struct.pack("<L", i) for i in ids)
    data = header + body
    got = set(parse_content_ids(data))
    for k in KNOWN_IDS:
        assert k in got, f"{hex(k)} missing"
