import io
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from lzxbuild import lzx


def _pack_lzx_bits(bits: str) -> bytes:
    while len(bits) % 16:
        bits += "0"
    out = bytearray()
    for i in range(0, len(bits), 16):
        word = int(bits[i : i + 16], 2)
        out += word.to_bytes(2, "little")
    return bytes(out)


def _uncompressed_lzx_block(payload: bytes) -> bytes:
    if len(payload) > 0xFFFFFF:
        raise ValueError("payload too large for one LZX block")
    bits = "0"  # no Intel E8 transform header
    bits += "011"  # block type 3: uncompressed
    bits += f"{len(payload) >> 8:016b}"
    bits += f"{len(payload) & 0xFF:08b}"
    prefix = _pack_lzx_bits(bits)
    return prefix + (1).to_bytes(4, "little") * 3 + payload + (b"\0" if len(payload) & 1 else b"")


class LzxFallbackTests(unittest.TestCase):
    def test_uncompressed_block_round_trip_without_stderr_noise(self):
        payload = b"hello strata lzx"
        lzx.init(17)
        err = io.StringIO()
        with redirect_stderr(err):
            got = lzx.decompress(_uncompressed_lzx_block(payload), len(payload))
        self.assertEqual(got, payload)
        self.assertEqual(err.getvalue(), "")

    def test_optional_golden_pair_matches_libmspack(self):
        comp = os.environ.get("STRATA_LZX_GOLDEN_COMP")
        raw = os.environ.get("STRATA_LZX_GOLDEN_RAW")
        if not comp or not raw:
            self.skipTest("set STRATA_LZX_GOLDEN_COMP and STRATA_LZX_GOLDEN_RAW to run parity fixture")
        comp_bytes = Path(comp).read_bytes()
        expected = Path(raw).read_bytes()

        lzx.init(int(os.environ.get("STRATA_LZX_WINDOW_BITS", "17")))
        got = lzx.decompress(comp_bytes, len(expected))
        self.assertEqual(got, expected)

        if os.environ.get("STRATA_MSPACK_LZX_DLL"):
            from strata_akc_dump import mspack_lzx

            native = mspack_lzx.decompress(
                comp_bytes,
                len(expected),
                window_bits=int(os.environ.get("STRATA_LZX_WINDOW_BITS", "17")),
                reset_interval=int(os.environ.get("STRATA_LZX_RESET_INTERVAL", "0")),
            )
            self.assertEqual(native, expected)


if __name__ == "__main__":
    unittest.main()
