"""Parity test for the table-driven Huffman decoder in lzxbuild.lzx.

The fast path (primary lookup table) must produce byte-for-byte identical results
to the slow bit-by-bit walk, including consuming the same number of bits — and
must handle codes longer than PRIMARY_BITS via the slow fallback.
"""
import hashlib
import unittest

from lzxbuild.lzx import _Bits, _HTree


def _pseudo_random_bytes(n: int) -> bytes:
    out = bytearray()
    seed = b"strata-lzx-parity"
    while len(out) < n:
        seed = hashlib.sha256(seed).digest()
        out.extend(seed)
    return bytes(out[:n])


class HuffmanParityTests(unittest.TestCase):
    def _assert_parity(self, lengths, count=200):
        tree = _HTree(lengths, len(lengths))
        data = _pseudo_random_bytes(4096)
        fast_bits, slow_bits = _Bits(data), _Bits(data)
        fast = [tree.decode(fast_bits) for _ in range(count)]
        slow = [tree._decode_slow(slow_bits) for _ in range(count)]
        self.assertEqual(fast, slow)
        # identical bit consumption -> compare logical position (bits read), not the
        # raw buffer fill, since peek(12) buffers more words than read(1).
        self.assertEqual(fast_bits._pos * 8 - fast_bits._avail,
                         slow_bits._pos * 8 - slow_bits._avail)

    def test_short_codes_only(self):
        # complete canonical code, max length 4 (well within the 12-bit table)
        self._assert_parity([1, 2, 3, 4, 4])

    def test_codes_longer_than_primary_table(self):
        # complete code with max length 13 (> PRIMARY_BITS=12) -> exercises fallback
        self._assert_parity([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 13])

    def test_single_symbol_tree(self):
        tree = _HTree([1, 1], 2)
        bits = _Bits(_pseudo_random_bytes(64))
        for _ in range(50):
            self.assertIn(tree.decode(bits), (0, 1))


if __name__ == "__main__":
    unittest.main()
