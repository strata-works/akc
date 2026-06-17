"""
Pure-Python LZX decompressor — drop-in for calibre's compiled lzx C extension.
Stateful module-level API matching the C extension:
    init(window_size_bits)
    reset()
    decompress(data, uncompressed_length) -> bytes
"""
from __future__ import annotations
import struct

def _build_position_tables(n: int = 50):
    """Generate EXTRA_BITS and POS_BASE tables for up to n position slots."""
    eb = []
    pb = [0] * n
    for i in range(n):
        if i < 4:
            eb.append(0)
        elif i < 36:
            eb.append(i // 2 - 1)
        else:
            eb.append(17)
        if i:
            pb[i] = pb[i - 1] + (1 << eb[i - 1])
    return eb, pb

_EXTRA_BITS, _POS_BASE = _build_position_tables(50)

_NUM_CHARS             = 256
_NUM_PRIMARY_LENGTHS   = 7
_NUM_SECONDARY_LENGTHS = 249
_PRETREE_SYMS          = 20
_ALIGNED_SYMS          = 8
_BLOCK_VERBATIM        = 1
_BLOCK_ALIGNED         = 2
_BLOCK_UNCOMPRESSED    = 3
_MIN_MATCH             = 2


class _State:
    __slots__ = ('window_size', 'window', 'pos_slots', 'wpos', 'R0', 'R1', 'R2',
                 'main_len', 'sec_len', 'debug_context')
    def __init__(self) -> None:
        self.window_size = 0
        self.window:    bytearray = bytearray()
        self.pos_slots: int       = 0
        self.wpos: int            = 0
        self.R0 = self.R1 = self.R2 = 1
        self.main_len: list[int]  = []
        self.sec_len:  list[int]  = []
        self.debug_context: str    = ""


_s = _State()


def init(window_size_bits: int) -> None:
    ws = 1 << window_size_bits
    _s.window_size = ws
    _s.window      = bytearray(ws)
    _s.pos_slots   = sum(1 for pb in _POS_BASE if pb < ws)
    _s.main_len    = [0] * (_NUM_CHARS + _s.pos_slots * 8)
    _s.sec_len     = [0] * _NUM_SECONDARY_LENGTHS
    _s.wpos = 0
    _s.R0 = _s.R1 = _s.R2 = 1


def reset() -> None:
    _s.main_len[:] = [0] * len(_s.main_len)
    _s.sec_len[:]  = [0] * _NUM_SECONDARY_LENGTHS
    _s.wpos = 0
    _s.R0 = _s.R1 = _s.R2 = 1
    _s.debug_context = ""


# ── bit reader ────────────────────────────────────────────────────────────────

class _Bits:
    """LZX: little-endian 16-bit words injected into an MSB-first bit buffer."""
    __slots__ = ('_d', '_pos', '_buf', '_avail')

    def __init__(self, data: bytes, start: int = 0) -> None:
        self._d     = data
        self._pos   = start
        self._buf   = 0
        self._avail = 0

    def _refill(self) -> None:
        while self._avail < 17 and self._pos + 1 < len(self._d):
            w            = self._d[self._pos] | (self._d[self._pos + 1] << 8)
            self._buf   |= (w << (32 - 16 - self._avail)) & 0xFFFFFFFF
            self._avail += 16
            self._pos   += 2

    def peek(self, n: int) -> int:
        while self._avail < n:
            before = self._pos
            self._refill()
            if self._avail < n and self._pos == before:
                raise EOFError("LZX: unexpected end of bitstream")
        return (self._buf >> (32 - n)) & ((1 << n) - 1)

    def drop(self, n: int) -> None:
        self._buf = (self._buf << n) & 0xFFFFFFFF
        self._avail -= n

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        v = self.peek(n)
        self.drop(n)
        return v

    def align16(self) -> None:
        consumed_bits = (self._pos * 8) - self._avail
        self._pos = (consumed_bits + 7) // 8
        if self._avail > 8:
            self._pos += 1
        self._buf = 0
        self._avail = 0

    def raw_pos(self) -> int:
        """Byte position in source, accounting for bits still buffered."""
        return self._pos - (self._avail >> 3)


# ── Huffman decoder ───────────────────────────────────────────────────────────

class _HTree:
    """Canonical Huffman decoder using bit-by-bit walk (correct for any code lengths)."""
    __slots__ = ('_sym_map', '_max_l')

    def __init__(self, lengths: list[int], n: int) -> None:
        ls = lengths[:n]
        max_l = max(ls, default=0)
        self._max_l = max_l

        count = [0] * (max_l + 2)
        for length in ls:
            if length:
                count[length] += 1

        next_code = [0] * (max_l + 2)
        code = 0
        for bit_num in range(1, max_l + 1):
            code = (code + count[bit_num - 1]) << 1
            next_code[bit_num] = code

        sym_map: dict[tuple[int, int], int] = {}
        for sym, length in enumerate(ls):
            if length:
                sym_map[(next_code[length], length)] = sym
                next_code[length] += 1

        self._sym_map = sym_map

    def decode(self, bits: _Bits) -> int:
        code = 0
        sym_map = self._sym_map
        for l in range(1, self._max_l + 1):
            code = (code << 1) | bits.read(1)
            sym = sym_map.get((code, l))
            if sym is not None:
                return sym
        ctx = f" {_s.debug_context}" if _s.debug_context else ""
        raise ValueError(f"LZX: bad Huffman code{ctx} (avail={bits._avail} code={code:#x} max_l={self._max_l} sym_map_size={len(self._sym_map)})")



# ── tree-length reading (delta-encoded via pretree) ───────────────────────────

def _read_lens(bits: _Bits, tree: list[int], first: int, last: int) -> None:
    pre_l = [bits.read(4) for _ in range(_PRETREE_SYMS)]
    pt = _HTree(pre_l, _PRETREE_SYMS)

    i = first
    while i < last:
        sym = pt.decode(bits)
        if sym <= 16:
            value = tree[i] - sym
            if value < 0:
                value += 17
            tree[i] = value
            i += 1
        elif sym == 17:
            run = bits.read(4) + 4
            for _ in range(run):
                if i < len(tree):
                    tree[i] = 0
                i += 1
        elif sym == 18:
            run = bits.read(5) + 20
            for _ in range(run):
                if i < len(tree):
                    tree[i] = 0
                i += 1
        else:
            run = bits.read(1) + 4
            sym2 = pt.decode(bits)
            value = tree[i] - sym2
            if value < 0:
                value += 17
            for _ in range(run):
                if i < len(tree):
                    tree[i] = value
                i += 1


# ── main decompressor ─────────────────────────────────────────────────────────

def decompress(data: bytes, uncomp_len: int) -> bytes:
    bits = _Bits(data)
    out = bytearray()
    window = _s.window
    wsize = _s.window_size
    wpos = _s.wpos
    R0, R1, R2 = _s.R0, _s.R1, _s.R2
    n_main = len(_s.main_len)
    n_sec = len(_s.sec_len)
    main_len = _s.main_len
    sec_len = _s.sec_len

    intel = bits.read(1)
    intel_filesize = 0
    intel_started = False
    if intel:
        hi = bits.read(16)
        lo = bits.read(16)
        intel_filesize = (hi << 16) | lo

    block_type = 0
    block_len = 0
    block_rem = 0
    uncompressed_pos = 0
    mt: _HTree | None = None
    st: _HTree | None = None
    at: _HTree | None = None
    block_index = 0
    frame_index = 0
    produced = 0

    def align_frame() -> None:
        if bits._avail > 0:
            bits._refill()
            extra = bits._avail & 15
            if extra:
                bits.drop(extra)

    def apply_intel_e8(frame_start: int, frame_size: int, curpos: int) -> None:
        if not (intel_started and intel_filesize and frame_index < 32768 and frame_size > 10):
            return
        pos = frame_start
        end = frame_start + frame_size - 10
        while pos < end:
            if out[pos] != 0xE8:
                pos += 1
                curpos += 1
                continue
            abs_off = int.from_bytes(out[pos + 1:pos + 5], "little", signed=True)
            if abs_off >= -curpos and abs_off < intel_filesize:
                rel_off = abs_off - curpos if abs_off >= 0 else abs_off + intel_filesize
                out[pos + 1:pos + 5] = (rel_off & 0xFFFFFFFF).to_bytes(4, "little")
            pos += 5
            curpos += 5

    while produced < uncomp_len:
        frame_size = min(32768, uncomp_len - produced)
        frame_start = len(out)
        frame_goal = frame_start + frame_size

        while len(out) < frame_goal:
            if block_rem == 0:
                if block_type == _BLOCK_UNCOMPRESSED:
                    if block_len & 1:
                        uncompressed_pos += 1
                    bits = _Bits(data, uncompressed_pos)  # type: ignore[assignment]

                block_type = bits.read(3)
                block_len = (bits.read(16) << 8) | bits.read(8)
                block_rem = block_len
                _s.debug_context = (
                    f"start_block={block_index} frame={frame_index} type={block_type} "
                    f"size={block_len} out={len(out)} bitpos={bits.raw_pos()} bits={bits._avail}"
                )
                block_index += 1

                if block_type == _BLOCK_UNCOMPRESSED:
                    intel_started = True
                    bits.align16()
                    bp = bits.raw_pos()
                    if bp + 12 > len(data):
                        raise EOFError("LZX: truncated uncompressed block header")
                    R0 = struct.unpack_from('<I', data, bp)[0]; bp += 4
                    R1 = struct.unpack_from('<I', data, bp)[0]; bp += 4
                    R2 = struct.unpack_from('<I', data, bp)[0]; bp += 4
                    uncompressed_pos = bp
                    mt = st = at = None
                elif block_type in (_BLOCK_VERBATIM, _BLOCK_ALIGNED):
                    at = None
                    if block_type == _BLOCK_ALIGNED:
                        at = _HTree([bits.read(3) for _ in range(_ALIGNED_SYMS)], _ALIGNED_SYMS)

                    _read_lens(bits, main_len, 0, 256)
                    _read_lens(bits, main_len, 256, n_main)
                    mt = _HTree(main_len, n_main)
                    if main_len[0xE8] != 0:
                        intel_started = True

                    _read_lens(bits, sec_len, 0, n_sec)
                    st = _HTree(sec_len, n_sec)
                else:
                    raise ValueError(f"LZX: bad block type {block_type}")

            this_run = min(block_rem, frame_goal - len(out))
            block_rem -= this_run

            if block_type == _BLOCK_UNCOMPRESSED:
                end = uncompressed_pos + this_run
                if end > len(data):
                    missing = end - len(data)
                    if missing > 2:
                        raise EOFError(f"LZX: truncated uncompressed block payload ctx={_s.debug_context}")
                    chunk = data[uncompressed_pos:] + (b"\0" * missing)
                else:
                    chunk = data[uncompressed_pos:end]
                uncompressed_pos = end
                for b in chunk:
                    window[wpos] = b
                    wpos = (wpos + 1) % wsize
                out.extend(chunk)
                continue

            if mt is None or st is None:
                raise ValueError("LZX: compressed block missing Huffman tables")

            run_left = this_run
            while run_left > 0:
                _s.debug_context = (
                    f"block={block_index - 1} frame={frame_index} out={len(out)} "
                    f"frame_left={frame_goal - len(out)} run_left={run_left} "
                    f"block_rem={block_rem} bitpos={bits.raw_pos()} bits={bits._avail}"
                )
                sym = mt.decode(bits)

                if sym < _NUM_CHARS:
                    window[wpos] = sym
                    out.append(sym)
                    wpos = (wpos + 1) % wsize
                    run_left -= 1
                    continue

                sym -= _NUM_CHARS
                len_hdr = sym & 7
                pos_slot = sym >> 3

                if len_hdr == _NUM_PRIMARY_LENGTHS:
                    _s.debug_context = (
                        f"length block={block_index - 1} frame={frame_index} out={len(out)} "
                        f"run_left={run_left} block_rem={block_rem} bitpos={bits.raw_pos()} "
                        f"bits={bits._avail} pos_slot={pos_slot}"
                    )
                    length_footer = st.decode(bits)
                    match_len = _NUM_PRIMARY_LENGTHS + _MIN_MATCH + length_footer
                else:
                    match_len = len_hdr + _MIN_MATCH

                if pos_slot == 0:
                    match_pos = R0
                elif pos_slot == 1:
                    match_pos = R1; R1 = R0; R0 = match_pos
                elif pos_slot == 2:
                    match_pos = R2; R2 = R0; R0 = match_pos
                else:
                    extra = _EXTRA_BITS[pos_slot]
                    match_pos = _POS_BASE[pos_slot] - 2
                    if block_type == _BLOCK_ALIGNED and extra >= 3:
                        if extra > 3:
                            match_pos += bits.read(extra - 3) << 3
                        if at is None:
                            raise ValueError("LZX: aligned block missing aligned tree")
                        match_pos += at.decode(bits)
                    else:
                        match_pos += bits.read(extra)
                    R2 = R1; R1 = R0; R0 = match_pos

                if match_len > run_left:
                    overrun = match_len - run_left
                    if overrun > block_rem:
                        raise ValueError("LZX: match overruns block")
                    block_rem -= overrun
                if len(out) + match_len > frame_goal:
                    raise ValueError("LZX: match overruns frame")

                src = (wpos - match_pos) % wsize
                for _ in range(match_len):
                    b = window[src]
                    window[wpos] = b
                    out.append(b)
                    src = (src + 1) % wsize
                    wpos = (wpos + 1) % wsize
                run_left -= match_len

        apply_intel_e8(frame_start, frame_size, produced)
        align_frame()
        produced += frame_size
        frame_index += 1

    _s.wpos = wpos
    _s.R0, _s.R1, _s.R2 = R0, R1, R2
    _s.debug_context = ""
    return bytes(out[:uncomp_len])

