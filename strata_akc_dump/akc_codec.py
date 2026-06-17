"""Portable port of MSENCDAT's AKC body codec (the ``MSENCDAT+0x1DA30`` family).

This module ports the **verified primitive layer** of the custom decompressor that
turns a packed ``CONTSTD.AKC`` body block into the DWORD token stream + positive/
negative dictionaries that ``MSENCDAT+0x1E770`` later expands into XML (see
``scripts/probe_e770_expand.py`` for the now-complete E770 stage).

Reverse-engineered from ``output/msencdat_disasm.txt`` (image base 0x45480000):

================  ==============================  ===================================
MSENCDAT addr     this module                     role
================  ==============================  ===================================
0x1C760 (C760)    ``BitReader._refill``           refill 32-bit accumulator
0x1C7F0 (C7F0)    ``BitReader.drop``              consume N bits (>=32 safe)
0x1C830 (C830)    ``BitReader.peek``              peek N bits (MSB-first)
0x1C890 (C890)    ``BitReader.drop`` (alias)      consume N bits
0x1D870 (D870)    ``BitReader.read_value``        Elias-ish: 5-bit n, then n bits
0x1D030 (D030)    ``build_decode_table`` /        canonical Huffman (table or, for
                  ``CanonicalHuffman``            long codes, the equivalent decoder)
0x1D290 (D290)    ``read_code_lengths``           3-mode code-length transmission
0x1DA30 (DA30)    ``decode_block`` (DRIVER: TODO)  main literal/match decode loop
================  ==============================  ===================================

The ``BitReader`` here is the same model already proven against live ``DicT`` rows in
``scripts/probe_msencdat_lookup.py``: 16-bit little-endian words fed MSB-first into a
32-bit window. ``BitReader``, ``read_value`` (D870) and ``read_code_lengths`` (all
three D290 modes) are now validated **byte-for-byte against a live Encarta decode** of
the sodium article — see ``scripts/probe_da30_tables.py``, which decodes the DA30 block
header and all four Huffman code-length tables in pure Python. The remaining piece is
the ``decode_block`` main loop (the LZ77 literal/match decode at 0x1DDF3 that emits the
token stream and fills the pos/neg dictionaries); it is documented in INVESTIGATION.md
and not yet wired here.
"""
from __future__ import annotations

from dataclasses import dataclass

# Number of root-table index bits per Huffman alphabet, from the DA30 D8F0 call
# sites (edx = maxbits): main = 10, selector = 5, the two run tables = 8.
MAIN_TABLE_BITS = 10
SELECTOR_TABLE_BITS = 5
RUN_TABLE_BITS = 8


@dataclass
class BitReader:
    """MSB-first bit reader over 16-bit little-endian words.

    Ported from MSENCDAT+0x1C760/+0x1C830/+0x1C890. ``bitbuf`` holds valid bits at
    the MSB end; ``bits`` counts how many are valid. Words are loaded as
    ``b1<<24 | b0<<16 | b3<<8 | b2`` (two LE 16-bit words), matching the on-disk
    layout the live app reads.
    """

    data: bytes
    base: int
    size: int
    ptr: int = 0
    bitbuf: int = 0
    bits: int = 0
    error: bool = False

    def __post_init__(self) -> None:
        self.ptr = self.base
        if self.size & 1:
            # The codec only ever loads whole 16-bit words; an odd span is invalid.
            self.error = True

    @property
    def end(self) -> int:
        return self.base + self.size

    def _load_word32(self, ptr: int) -> tuple[int, int]:
        """MSENCDAT+0x1C760 load: advance 4 bytes, assemble two LE 16-bit words."""
        cand = ptr + 4
        if cand <= self.end:
            b0, b1, b2, b3 = self.data[ptr : ptr + 4]
            return cand, ((b1 << 24) | (b0 << 16) | (b3 << 8) | b2) & 0xFFFFFFFF
        cand = ptr + 2
        if cand <= self.end:
            b0, b1 = self.data[ptr : ptr + 2]
            return cand, ((b1 << 24) | (b0 << 16)) & 0xFFFFFFFF
        self.error = True
        return ptr, 0

    def _refill(self, need: int) -> int:
        old_buf = self.bitbuf
        ptr, newbits = self._load_word32(self.ptr)
        if self.error:
            return 0
        old_bits = self.bits
        self.ptr = ptr
        self.bits = old_bits + 32
        take_from_new = newbits >> self.bits if self.bits < 32 else 0
        low_count = 32 - self.bits
        self.bitbuf = (newbits << low_count) & 0xFFFFFFFF
        return ((old_buf >> (32 - need)) + take_from_new) & ((1 << need) - 1)

    def read(self, nbits: int) -> int:
        """Consume ``nbits`` and return them (MSB-first). MSENCDAT inline reader."""
        if nbits == 0:
            return 0
        self.bits -= nbits
        if self.bits < 0:
            return self._refill(nbits)
        val = self.bitbuf >> (32 - nbits)
        self.bitbuf = (self.bitbuf << nbits) & 0xFFFFFFFF
        return val

    def peek(self, nbits: int) -> int:
        """MSENCDAT+0x1C830: return top ``nbits`` without consuming (refills if short)."""
        if nbits == 0:
            return 0
        # Ensure enough bits are buffered, mirroring C830's top-up-by-16 loop.
        while self.bits < nbits and not self.error:
            ptr, newbits = self._load_word32_16(self.ptr)
            if self.error:
                break
            self.ptr = ptr
            self.bitbuf = (self.bitbuf | (newbits >> self.bits)) & 0xFFFFFFFF
            self.bits += 16
        return self.bitbuf >> (32 - nbits)

    def _load_word32_16(self, ptr: int) -> tuple[int, int]:
        """C830's single 16-bit LE word load, placed at the MSB end."""
        cand = ptr + 2
        if cand > self.end:
            self.error = True
            return ptr, 0
        b0, b1 = self.data[ptr : ptr + 2]
        return cand, ((b1 << 8) | b0) << 16

    def drop(self, nbits: int) -> None:
        """MSENCDAT+0x1C890/+0x1C7F0: consume ``nbits`` already peeked."""
        if nbits == 0:
            return
        self.bits -= nbits
        if self.bits < 0:
            self._refill(nbits)
            return
        self.bitbuf = (self.bitbuf << nbits) & 0xFFFFFFFF

    def read_value(self) -> int:
        """MSENCDAT+0x1D870: read 5-bit ``n``; return ``(1<<n) - 1 + read(n)``.

        n==0 yields 0. Used for header counts/dimensions in the DA30 block header.
        """
        n = self.read(5)
        if n == 0:
            return 0
        return (1 << n) - 1 + self.read(n)


# --- Canonical two-level Huffman decode table (MSENCDAT+0x1D030) ----------------
#
# Table words are 16-bit: a non-negative entry packs ``(symbol << 4) | length``
# (length in the low nibble); a negative entry (top bit set, value - 0x8000) marks
# a pointer/offset into a sub-table for codes longer than ``root_bits``. The
# decoder peeks ``root_bits``, indexes the root, and for sub-table markers reads
# the extra bits and indexes again. ``decode_symbol`` implements that walk.

SUBTABLE_FLAG = 0x8000


def build_decode_table(lengths: list[int], root_bits: int) -> list[int]:
    """Build the canonical lookup table for ``lengths`` (0 == unused symbol).

    Returns a list of 16-bit table words laid out exactly as MSENCDAT+0x1D030
    fills ``[ebx + slot*2]``: root slots in ``[0, 2**root_bits)`` followed by any
    sub-tables. Mirrors D030's histogram, well-formedness check, and the
    "spread each code over ``2**(root_bits-len)`` consecutive slots" fill.

    NOTE: This is a faithful algorithmic port pending end-to-end validation
    against the live ``da30_*`` dumps; the sub-table layout for codes longer than
    ``root_bits`` still needs to be checked byte-for-byte against a dumped table.
    """
    max_len = max(lengths) if lengths else 0
    counts = [0] * (max_len + 1)
    for ln in lengths:
        if ln:
            counts[ln] += 1

    # First code per length (canonical, MSB-first).
    next_code: list[int] = [0] * (max_len + 2)
    code = 0
    for ln in range(1, max_len + 1):
        code = (code + counts[ln - 1]) << 1
        next_code[ln] = code

    table = [0] * (1 << root_bits)
    for sym, ln in enumerate(lengths):
        if ln == 0 or ln > root_bits:
            # Sub-table handling (ln > root_bits) is the remaining piece; see note.
            continue
        c = next_code[ln]
        next_code[ln] = c + 1
        # Codes are MSB-first: the root slot is the code left-justified to root_bits.
        slot = c << (root_bits - ln)
        word = ((sym << 4) | ln) & 0xFFFF
        for i in range(1 << (root_bits - ln)):
            table[slot + i] = word
    return table


def decode_symbol(br: BitReader, table: list[int], root_bits: int) -> int:
    """Decode one short-code symbol from a ``build_decode_table`` table (C830 + C890).

    Only valid when every code is <= ``root_bits`` (raises otherwise). For tables
    with longer codes use :class:`CanonicalHuffman`, which yields identical symbols.
    """
    word = table[br.peek(root_bits)]
    if word & SUBTABLE_FLAG:
        raise NotImplementedError("long-code sub-table walk not ported; use CanonicalHuffman")
    length = word & 0xF
    br.drop(length)
    return word >> 4


class CanonicalHuffman:
    """Canonical, MSB-first Huffman decoder built from a code-length array.

    MSENCDAT builds a two-level lookup table (``0x1D030``) for speed, but the
    decoded symbols are fully determined by the canonical code assignment: codes
    are handed out in ascending symbol order within ascending length, MSB-first.
    This class reproduces exactly those symbols without the table's memory layout,
    so it is correct for any code length (including the long pretree codes).
    """

    def __init__(self, lengths: list[int]) -> None:
        self.lengths = lengths
        max_len = max(lengths) if lengths else 0
        counts = [0] * (max_len + 1)
        for ln in lengths:
            if ln:
                counts[ln] += 1
        # Canonical first-code per length (standard, MSB-first).
        code = 0
        first_code = [0] * (max_len + 2)
        next_code = [0] * (max_len + 2)
        for ln in range(1, max_len + 1):
            code = (code + counts[ln - 1]) << 1
            first_code[ln] = code
            next_code[ln] = code
        # symbols, grouped by ascending length then ascending symbol index;
        # first_index[ln] is where length ``ln``'s block starts in self.symbols.
        self.max_len = max_len
        self.first_code = first_code
        self.first_index: list[int] = [0] * (max_len + 2)
        self.symbols: list[int] = []
        idx = 0
        for ln in range(1, max_len + 1):
            self.first_index[ln] = idx
            for sym, sl in enumerate(lengths):
                if sl == ln:
                    self.symbols.append(sym)
                    idx += 1
        self.first_index[max_len + 1] = idx  # one past the last block

    def decode(self, br: BitReader) -> int:
        code = 0
        for ln in range(1, self.max_len + 1):
            code = (code << 1) | br.read(1)
            count = self.first_index[ln + 1] - self.first_index[ln]
            offset = code - self.first_code[ln]
            if count and 0 <= offset < count:
                return self.symbols[self.first_index[ln] + offset]
        raise ValueError("invalid Huffman code")


def _bit_width(value: int) -> int:
    """MSENCDAT+0x1C650: number of bits needed to represent ``value`` (0 -> 0)."""
    return value.bit_length()


def read_code_lengths(br: BitReader, count: int) -> list[int]:
    """Read ``count`` per-symbol code lengths (MSENCDAT+0x1D290).

    Three transmission modes, selected by a 1-2 bit prefix (read MSB-first):

    * prefix ``1``  -> mode A (pretree): 6-bit ``g1``; if 0 all lengths are 0.
      Else 4-bit ``g2`` (<=0x0B); read ``g1`` then ``g2`` 3-bit values into a
      44-symbol pretree alphabet at indices ``[0,g1)`` and ``[32,32+g2)``; build
      the pretree and decode ``count`` lengths: pretree symbols 0..31 are literal
      lengths, 32..43 are "repeat the previous length ``(1<<(s-32)) + extra``
      times" where ``extra`` is ``(s-32)`` bits.
    * prefix ``01`` -> mode B (sparse): ``w = bit_width(count-1)``;
      ``n = read(w)+1`` entries; ``cur = read(5)``; for each entry read a position
      ``p`` (``w`` bits); if ``p`` repeats the previous position, bump ``cur`` and
      do not consume the entry, else ``lengths[p] = cur``.
    * prefix ``00`` -> mode C (fixed): 3-bit width ``w`` (<=5); each of ``count``
      lengths is a raw ``w``-bit value.

    Returns the length array (``count`` entries). Faithful port pending end-to-end
    validation against the live ``da30_*`` token dumps.
    """
    lengths = [0] * count

    if br.read(1):
        # --- mode A: pretree (0x1D48B) ---
        g1 = br.read(6)
        if g1 == 0:
            return lengths  # all-zero alphabet
        if g1 > 0x20:
            raise ValueError("mode A g1 out of range")
        g2 = br.read(4)
        if g2 > 0x0B:
            raise ValueError("mode A g2 out of range")
        pre_lengths = [0] * 0x2C  # 44-symbol pretree alphabet
        for i in range(g1):
            pre_lengths[i] = br.read(3)
        for i in range(g2):
            pre_lengths[0x20 + i] = br.read(3)
        pretree = CanonicalHuffman(pre_lengths)
        out = 0
        while out < count:
            sym = pretree.decode(br)
            if sym <= 0x1F:
                lengths[out] = sym
                out += 1
            else:
                if out == 0:
                    raise ValueError("mode A repeat with no previous length")
                extra_bits = sym - 0x20
                extra = br.read(extra_bits) if extra_bits else 0
                run = (1 << extra_bits) + extra
                if out + run > count:
                    raise ValueError("mode A run overflow")
                prev = lengths[out - 1]
                for _ in range(run):
                    lengths[out] = prev
                    out += 1
        return lengths

    if br.read(1):
        # --- mode B: sparse (0x1D39E) ---
        width = _bit_width(count - 1)
        n_entries = br.read(width) + 1
        if n_entries > count:
            raise ValueError("mode B entry count overflow")
        cur = br.read(5)
        prev_pos = -1
        remaining = n_entries
        while remaining:
            pos = br.read(width) if width else 0
            if pos >= count:
                raise ValueError("mode B position overflow")
            if pos == prev_pos:
                cur += 1  # entry not consumed
                continue
            lengths[pos] = cur
            prev_pos = pos
            remaining -= 1
        return lengths

    # --- mode C: fixed width (0x1D302) ---
    width = br.read(3)
    if width > 5:
        raise ValueError("mode C width out of range")
    for i in range(count):
        lengths[i] = br.read(width)
    return lengths
