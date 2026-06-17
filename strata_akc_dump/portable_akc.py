"""Portable Encarta AKC XML-record decoder.

This module promotes the validated probe path into package code:

* packed AKC lookup table -> body/token/XML window
* shared positive DA30 dictionary
* per-body negative DA30 dictionary
* E560/E220 token production
* E770 token-to-XML expansion

It is validated for Encarta 2009 ``CONTSTD.AKC`` article records and ``DATASTD.AKC``
media/data records. Tiny source-gate files such as ``DataESK.akc`` do not use this
same dictionary constructor path.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
import struct
from typing import Iterable, TextIO

from .akc_codec import BitReader, CanonicalHuffman, read_code_lengths
from .akc_tables import HUFFMAN_TABLES, OPCODE_PATH_INDEX, POSITION_SLOTS

DEFAULT_SEEK_START = 0x0207A1B2
DEFAULT_SEEK_SIZE = 0x0003A848
DEFAULT_MAP_COUNT = 44055
REFID_RE = re.compile(br'<(?:content|data)\s+refid="([0-9]+)"')

POSITION_EXTRA = [extra for extra, _base in POSITION_SLOTS]
POSITION_BASE = [base for _extra, base in POSITION_SLOTS]
POSITION_ENDS = POSITION_BASE + [POSITION_SLOTS[-1][1] + (1 << POSITION_SLOTS[-1][0])]


def u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def akc_header_fields(data: bytes) -> dict[str, int]:
    if data[:4] != b" CKA":
        raise ValueError("not an AKC file")
    return {
        "positive_off": u32(data, 0x1C),
        "positive_size": u32(data, 0x20),
        "seek_start": u32(data, 0x24),
        "seek_size": u32(data, 0x28),
        "xml_size": u32(data, 0x2C),
    }


def clone_reader(br: BitReader) -> BitReader:
    other = BitReader(br.data, br.base, br.size)
    other.ptr = br.ptr
    other.bitbuf = br.bitbuf
    other.bits = br.bits
    other.error = br.error
    return other


@dataclass(frozen=True)
class ArticleWindow:
    key: int
    body_off: int
    body_size: int
    xml_size: int
    token_offset: int
    token_count: int
    token_limit: int = 0x7FFFFFFF


@dataclass
class DecodedArticle:
    key: int
    refid: int | None
    xml: bytes
    window: ArticleWindow


@dataclass
class DictionaryState:
    data: bytes
    table: list[int]
    info: dict[str, object]


class LookupBitReader:
    """Bitreader variant used by the packed seek/lookup table."""

    def __init__(self, data: bytes, base: int, size: int) -> None:
        self.data = data
        self.base = base
        self.size = size
        self.ptr = base
        self.bitbuf = 0
        self.bits = 0
        self.error = bool(size & 1)

    @property
    def end(self) -> int:
        return self.base + self.size

    def _word32_at_ptr_plus_4(self, ptr: int) -> tuple[int, int]:
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

    def seek_bits(self, bitpos: int) -> None:
        byte_off = ((bitpos >> 3) & ~1)
        ptr = self.base + byte_off
        if ptr >= self.end:
            self.ptr = self.end
            self.bits = 0 if ptr == self.end else -32
            if ptr != self.end:
                self.error = True
            return
        ptr, val = self._word32_at_ptr_plus_4(ptr)
        shift = bitpos & 0x0F
        self.ptr = ptr
        self.bitbuf = (val << shift) & 0xFFFFFFFF
        self.bits = 32 - shift if ptr - self.base >= byte_off + 4 else 16 - shift

    def _refill(self, need: int) -> int:
        old_buf = self.bitbuf
        ptr, newbits = self._word32_at_ptr_plus_4(self.ptr)
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
        if nbits == 0:
            return 0
        self.bits -= nbits
        if self.bits < 0:
            return self._refill(nbits)
        val = self.bitbuf >> (32 - nbits)
        self.bitbuf = (self.bitbuf << nbits) & 0xFFFFFFFF
        return val

    def clone(self) -> "LookupBitReader":
        other = LookupBitReader(self.data, self.base, self.size)
        other.ptr = self.ptr
        other.bitbuf = self.bitbuf
        other.bits = self.bits
        other.error = self.error
        return other

    def read_record_pair(self) -> tuple[int, int, int]:
        start = self.read(32)
        nbits = self.read(5)
        delta = self.read(nbits) if nbits else 0
        return start, start + delta, nbits

    def read_six_record_pairs(self) -> list[tuple[int, int, int]]:
        return [self.read_record_pair() for _ in range(6)]


def _read_base_delta(br: LookupBitReader, rec: tuple[int, int, int]) -> int:
    start, _end, nbits = rec
    return start + br.read(nbits)


def _decode_lookup_fields(
    data: bytes,
    br: LookupBitReader,
    rec_a: list[tuple[int, int, int]],
    rec_b: list[tuple[int, int, int]],
    secondary_table: int,
) -> dict[str, int]:
    out: dict[str, int] = {}
    use_primary = False
    if rec_a[0][1] != 0:
        if rec_b[0][1] == 0:
            use_primary = True
        else:
            use_primary = br.read(1) != 0

    if use_primary:
        out["field04"] = _read_base_delta(br, rec_a[3])
        out["field08"] = _read_base_delta(br, rec_a[2])
        out["field0c"] = _read_base_delta(br, rec_a[1])
        tail = _read_base_delta(br, rec_a[4])
        out["field10"] = 0
        out["field14"] = tail
        out["field18"] = tail
        return out

    index = br.read(rec_b[0][2]) if rec_b[0][2] else 0
    if index >= rec_b[0][1]:
        br.error = True
        return out

    tmp = br.clone()
    tmp.seek_bits(u32(data, secondary_table + index * 4))
    out["field04"] = _read_base_delta(tmp, rec_b[3])
    out["field08"] = _read_base_delta(tmp, rec_b[2])
    base0c = _read_base_delta(tmp, rec_b[1])
    width0c = tmp.read(5)
    base14 = _read_base_delta(tmp, rec_b[4])
    width14 = tmp.read(5)
    width10 = tmp.read(5)
    if tmp.error:
        br.error = True
        return out

    out["field0c"] = base0c + br.read(width0c)
    out["field14"] = base14 + br.read(width14)
    out["field10"] = br.read(width10)
    out["field18"] = 0x7FFFFFFF
    return out


def _window_from_fields(fields: dict[str, int]) -> ArticleWindow:
    return ArticleWindow(
        key=int(fields["field00"]),
        body_off=int(fields["field04"]),
        body_size=int(fields["field08"]),
        xml_size=int(fields["field0c"]),
        token_offset=int(fields["field10"]),
        token_count=int(fields["field14"]),
        token_limit=int(fields.get("field18", 0x7FFFFFFF)),
    )


def lookup_window(data: bytes, seek_start: int, seek_size: int, key: int) -> ArticleWindow:
    windows = iter_lookup_windows(data, seek_start, seek_size, start_key=key, count=1)
    if len(windows) != 1 or windows[0].key != key:
        raise ValueError(f"lookup failed for key 0x{key:X}")
    return windows[0]


def lookup_key_count(data: bytes, seek_start: int) -> int:
    return u32(data, seek_start)


def iter_lookup_windows(
    data: bytes,
    seek_start: int,
    seek_size: int,
    start_key: int = 0,
    count: int = DEFAULT_MAP_COUNT,
) -> list[ArticleWindow]:
    """Sequentially decode packed lookup windows for a contiguous key range."""
    br = LookupBitReader(data, seek_start, seek_size)
    first_bits = u32(data, br.base)
    if seek_size <= 4 or first_bits == 0:
        return []

    ebp = br.base + 4
    edi = (first_bits + 0x1E) >> 5
    header2 = ebp + edi * 8 + 0x0C
    edx = u32(data, header2 - 4)

    br.seek_bits((edx + edi * 2 + 4) << 5)
    selector = br.read(5)
    rec_a = br.read_six_record_pairs()
    rec_b = br.read_six_record_pairs()

    wanted_end = start_key + count
    windows: list[ArticleWindow] = []
    for entry_index in range(edi + 1):
        entry = ebp + entry_index * 8
        current_key = u32(data, entry)
        br.seek_bits(u32(data, entry + 4))
        if entry_index == edi:
            group_count = 1
        elif entry_index == edi - 1:
            group_count = (first_bits - 1) & 0x1F
        else:
            group_count = 32
        if group_count == 0:
            group_count = 32

        for group_pos in range(group_count):
            fields = _decode_lookup_fields(data, br, rec_a, rec_b, header2)
            fields["field00"] = current_key
            if current_key >= start_key and current_key < wanted_end:
                windows.append(_window_from_fields(fields))
                if len(windows) == count:
                    return windows
            if current_key >= wanted_end:
                return windows
            if group_pos == group_count - 1:
                break
            delta = 1
            if selector and br.read(1):
                delta = br.read(selector)
            current_key += delta
    return windows


def positive_input_from_akc_bytes(data: bytes) -> bytes:
    if data[:4] != b" CKA":
        raise ValueError("not an AKC file")
    start = u32(data, 0x1C)
    size = u32(data, 0x20)
    return data[start : start + size]


def _read_segments(br: BitReader, n2d00: int, n2d04: int) -> tuple[list[int], list[int], list[int]]:
    slot_counts: list[int] = []
    data_starts: list[int] = []
    data_ends: list[int] = []
    pos = 0
    remaining = n2d04
    slot = 0
    while True:
        count = min(POSITION_ENDS[slot + 1], n2d00) - POSITION_ENDS[slot]
        delta = br.read_value()
        if delta > remaining:
            raise ValueError("segment data delta overflow")
        slot_counts.append(count)
        data_starts.append(pos)
        pos += delta
        data_ends.append(pos)
        remaining -= delta
        if n2d00 > POSITION_ENDS[slot + 1]:
            slot += 1
            continue
        break
    if remaining != 0:
        raise ValueError(f"segment data sum mismatch (remaining={remaining})")
    return slot_counts, data_starts, data_ends


def _validate_record(opcode: int, payload: bytes, record_len: int) -> None:
    if opcode >= len(OPCODE_PATH_INDEX):
        raise ValueError(f"opcode out of range: {opcode}")
    path = OPCODE_PATH_INDEX[opcode]
    if path == 0 and record_len <= 1:
        raise ValueError(f"opcode {opcode} requires payload")
    if path == 2:
        if record_len != 3:
            raise ValueError("opcode 9 requires exactly two payload bytes")
        if len(payload) != 2 or ((payload[0] << 8) + payload[1]) >= 0x100:
            raise ValueError("opcode 9 payload out of range")
    if path == 3 and record_len != 1:
        raise ValueError("opcode 10 requires empty payload")


def decode_dictionary(data: bytes, mode: int = 0, return_state: bool = False) -> DictionaryState:
    br = BitReader(data, 0, len(data))
    flags: dict[str, int] = {}
    if mode == 0:
        flags["_continue_bit"] = br.read(1)
        if flags["_continue_bit"]:
            raise ValueError("mode 0 continuation path is not a fresh dictionary block")
    elif mode == 1:
        flags["_mode1_start"] = br.read(1)
        if not flags["_mode1_start"]:
            raise ValueError("mode 1 block missing start bit")
        flags["_flag_1c"] = br.read(1)
        flags["_flag_20"] = br.read(1)
    else:
        raise ValueError(f"unsupported DA30 mode: {mode}")

    counts = [br.read_value() for _ in range(5)]
    n2d00, n2d04 = counts[2], counts[3]
    table_lengths: dict[str, list[int]] = {}

    def read_table(name: str) -> list[int]:
        if mode == 0 and br.read(1):
            raise NotImplementedError("table reuse path not exercised")
        lengths = read_code_lengths(br, HUFFMAN_TABLES[name][1])
        table_lengths[name] = lengths
        return lengths

    read_table("main")
    slot_counts, data_starts, data_ends = _read_segments(br, n2d00, n2d04)
    selector = CanonicalHuffman(read_table("selector"))
    table3 = CanonicalHuffman(read_table("table3"))
    table4 = CanonicalHuffman(read_table("table4"))

    buf = bytearray(8192)
    out_data = bytearray(n2d04)
    out_table = [0] * (n2d00 + 1)
    slot_used = [0] * len(slot_counts)
    data_pos = data_starts[:]
    prev_len = 0

    for entry in range(n2d00):
        if entry == 0:
            shared = 0
        else:
            shared = selector.decode(br)
            if shared >= 0x20:
                nb = shared - 0x20
                shared = (br.read(nb) if nb else 0) + (1 << nb) + 0x1F
        if shared > prev_len:
            raise ValueError(f"front-code overflow at {entry}: {shared} > {prev_len}")

        cur = shared
        while True:
            sym = (table3 if (cur & 1) == 0 else table4).decode(br)
            if sym >= 0x100:
                slot = sym - 0x100
                break
            buf[cur] = sym
            cur += 1

        if slot >= len(slot_counts):
            raise ValueError(f"terminator slot out of range at {entry}: {slot}")
        opcode = buf[0]
        payload = bytes(buf[1:cur])
        _validate_record(opcode, payload, cur)

        used = slot_used[slot]
        if used >= slot_counts[slot]:
            raise ValueError(f"slot {slot} record overflow")
        data_off = data_pos[slot]
        data_end = data_off + len(payload)
        if data_end > data_ends[slot]:
            raise ValueError(f"slot {slot} data overflow")
        out_data[data_off:data_end] = payload
        out_table[POSITION_ENDS[slot] + used] = (data_off << 4) | opcode
        slot_used[slot] = used + 1
        data_pos[slot] = data_end

        prev_len = cur
        if br.error:
            raise ValueError(f"bit reader error at entry {entry}")

    for slot, (used, expected, pos, end) in enumerate(zip(slot_used, slot_counts, data_pos, data_ends)):
        if used != expected:
            raise ValueError(f"slot {slot} count mismatch: {used} != {expected}")
        if pos != end:
            raise ValueError(f"slot {slot} data mismatch: {pos} != {end}")

    out_table[n2d00] = n2d04 << 4
    info: dict[str, object] = {
        "n2CF8": counts[0],
        "n2D14": counts[1],
        "n2D00": n2d00,
        "n2D04": n2d04,
        "n2D0C": counts[4],
        **flags,
    }
    if return_state:
        info["_reader"] = br
        info["_table_lengths"] = table_lengths
    return DictionaryState(bytes(out_data), out_table, info)


def _read_bits(br: BitReader, nbits: int) -> int:
    return br.read(nbits) if nbits else 0


def _decode_lz_length_and_distance(br: BitReader, sym: int) -> tuple[int, int]:
    v = sym - 0x40
    dist_bits = v & 0x0F
    length_class = v >> 4
    if length_class < 8:
        length = length_class
    elif length_class < 12:
        length = _read_bits(br, 1) + length_class * 2 - 8
    elif length_class < 15:
        n = length_class - 10
        length = (1 << n) + _read_bits(br, n) + 0x0C
    else:
        n = _read_bits(br, 4)
        length = (1 << n) + _read_bits(br, n) + 0x2B
    length += 2
    distance = (1 << dist_bits) + _read_bits(br, dist_bits)
    return length, distance


def decode_tokens(
    br: BitReader,
    main: CanonicalHuffman,
    neg_count: int,
    pos_count: int,
    count: int,
) -> list[int]:
    tokens: list[int] = []
    while len(tokens) < count:
        sym = main.decode(br)
        if sym < 0x20:
            idx = POSITION_BASE[sym] + _read_bits(br, POSITION_EXTRA[sym])
            if idx >= neg_count:
                raise ValueError(f"negative token index out of range: {idx} >= {neg_count}")
            tokens.append((idx - 0x80000000) & 0xFFFFFFFF)
        elif sym < 0x40:
            slot = sym - 0x20
            idx = POSITION_BASE[slot] + _read_bits(br, POSITION_EXTRA[slot])
            if idx >= pos_count:
                raise ValueError(f"positive token index out of range: {idx} >= {pos_count}")
            tokens.append(idx)
        else:
            length, distance = _decode_lz_length_and_distance(br, sym)
            if distance > len(tokens):
                raise ValueError(f"LZ distance underflow: distance={distance} at token={len(tokens)}")
            for _ in range(length):
                if len(tokens) >= count:
                    break
                tokens.append(tokens[-distance])
    return tokens


def _s32(v: int) -> int:
    return v - 0x100000000 if v & 0x80000000 else v


class E770State:
    def __init__(self, out_limit: int) -> None:
        self.out = bytearray()
        self.out_limit = out_limit
        self.mode_1dc = 0
        self.case_1e0 = 0
        self.stack_top = -1
        self.stack: list[dict[str, object]] = []
        self.allow_short_close = True

    def ensure(self, extra: int) -> None:
        if len(self.out) + extra > self.out_limit:
            raise ValueError("output overflow")

    def put(self, b: int) -> None:
        self.ensure(1)
        self.out.append(b)

    def copy(self, data: bytes, trailing: int = 0, casefold_first: bool = False) -> None:
        self.ensure(len(data) + (1 if trailing else 0))
        start = len(self.out)
        self.out.extend(data)
        if trailing:
            self.out.append(trailing)
        if casefold_first and self.case_1e0 == 0 and data:
            self.case_1e0 = 1
            c = self.out[start]
            if 0x61 <= c <= 0x7A:
                self.out[start] = c - 0x20
            elif 0x41 <= c <= 0x5A:
                self.out[start] = c + 0x20

    def push_tag(self, data: bytes) -> None:
        if self.stack_top >= 0:
            cur = self.stack[self.stack_top]
            if cur["data"] == data:
                cur["count"] = int(cur["count"]) + 1
                return
        self.stack_top += 1
        if self.stack_top == len(self.stack):
            self.stack.append({"data": data, "count": 1})
        else:
            self.stack[self.stack_top] = {"data": data, "count": 1}

    def dec_tag(self, idx: int) -> None:
        if idx < 0 or idx > self.stack_top:
            raise ValueError("tag stack underflow")
        cur = self.stack[idx]
        cur["count"] = int(cur["count"]) - 1
        if int(cur["count"]) == 0 and idx == self.stack_top:
            while self.stack_top >= 0 and int(self.stack[self.stack_top]["count"]) == 0:
                self.stack_top -= 1

    def close_ref(self, payload: bytes) -> None:
        if len(payload) < 2:
            raise ValueError("short close-ref payload")
        idx = self.stack_top - (payload[0] << 8) - payload[1]
        if idx < 0:
            raise ValueError("bad close-ref index")
        if self.mode_1dc < 0:
            self.out.append(ord(">"))
        self.mode_1dc = 0
        idx &= 0xFF
        tag = bytes(self.stack[idx]["data"])
        self.out.extend(b"</")
        self.copy(tag, ord(">"))
        self.stack_top = idx
        self.dec_tag(idx)

    def empty_close(self) -> None:
        if not self.allow_short_close or self.stack_top < 0 or self.mode_1dc >= 0:
            raise ValueError("bad empty-close state")
        self.ensure(2)
        self.out.extend(b"/>")
        self.dec_tag(self.stack_top & 0xFF)
        self.mode_1dc = 0


def entry_payload(
    token: int,
    pos_tab: list[int],
    pos_data: bytes,
    neg_tab: list[int],
    neg_data: bytes,
) -> tuple[int, bytes]:
    if _s32(token) < 0:
        index = token & 0x3FFFFFFF
        table = neg_tab
        payload = neg_data
    else:
        index = token
        table = pos_tab
        payload = pos_data
    if index + 1 >= len(table):
        raise IndexError(f"token index out of range: 0x{token:08X} -> {index}")
    first = table[index]
    second = table[index + 1]
    op = first & 0xF
    start = first >> 4
    end = second >> 4
    if end < start or end > len(payload):
        raise ValueError(f"bad payload slice token=0x{token:08X} start={start} end={end}")
    return op, payload[start:end]


def expand_tokens(
    tokens: list[int],
    pos_tab: list[int],
    pos_data: bytes,
    neg_tab: list[int],
    neg_data: bytes,
    out_limit: int,
    stop_after: int | None = None,
) -> bytes:
    st = E770State(out_limit)
    for n, token in enumerate(tokens):
        op, data = entry_payload(token, pos_tab, pos_data, neg_tab, neg_data)
        try:
            if op == 0:
                if st.mode_1dc > 0:
                    st.put(ord(" "))
                elif st.mode_1dc < 0:
                    st.put(ord(">"))
                st.mode_1dc = 1
                st.copy(data, casefold_first=True)
            elif op == 1:
                if st.mode_1dc < 0:
                    st.put(ord(">"))
                st.mode_1dc = 0
                st.copy(data)
            elif op == 2:
                st.case_1e0 = 0
                if st.mode_1dc < 0:
                    st.put(ord(">"))
                st.mode_1dc = 0
                st.copy(data)
            elif op == 3:
                if st.mode_1dc < 0:
                    st.put(ord(">"))
                st.mode_1dc = -1
                st.put(ord("<"))
                st.push_tag(data)
                st.copy(data)
            elif op == 4:
                if st.mode_1dc >= 0:
                    raise ValueError("space requires open tag")
                st.put(ord(" "))
                st.copy(data)
            elif op == 5:
                if st.mode_1dc >= 0:
                    raise ValueError("equals requires open tag")
                st.put(ord("="))
                st.copy(data)
            elif op == 6:
                if st.mode_1dc >= 0:
                    raise ValueError("single quote attr requires open tag")
                st.out.extend(b"='")
                st.copy(data, ord("'"))
            elif op == 7:
                if st.mode_1dc >= 0:
                    raise ValueError("double quote attr requires open tag")
                st.out.extend(b'="')
                st.copy(data, ord('"'))
            elif op == 8:
                if st.mode_1dc < 0:
                    st.out.extend(b">")
                st.mode_1dc = 0
                st.out.extend(b"</")
                st.copy(data, ord(">"))
            elif op == 9:
                st.close_ref(data)
            elif op == 10:
                st.empty_close()
            else:
                raise ValueError(f"unsupported op {op}")
        except Exception as exc:
            raise RuntimeError(f"token #{n} token=0x{token:08X} op={op} data={data[:32]!r}: {exc}") from exc
        if stop_after is not None and len(st.out) >= stop_after:
            return bytes(st.out)
    return bytes(st.out)


def extract_refid(xml: bytes) -> int | None:
    match = REFID_RE.search(xml[:256])
    return int(match.group(1)) if match else None


def load_refid_key_map(path: Path) -> dict[int, int]:
    rows: dict[int, int] = {}
    with path.open("r", encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        try:
            key_i = header.index("key")
            refid_i = header.index("refid")
        except ValueError as exc:
            raise ValueError(f"{path} is not a key/refid TSV") from exc
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) <= max(key_i, refid_i) or not parts[refid_i]:
                continue
            rows[int(parts[refid_i])] = int(parts[key_i])
    return rows


def write_refid_key_map(
    akc_path: Path,
    out_path: Path,
    start_key: int = 0,
    count: int | None = None,
    seek_start: int | None = None,
    seek_size: int | None = None,
    progress: int = 0,
) -> int:
    decoder = PortableAkcDecoder(akc_path, seek_start=seek_start, seek_size=seek_size)
    if count is None:
        count = lookup_key_count(decoder.akc, decoder.seek_start) - start_key
    windows = decoder.iter_windows(start_key, count)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as out:
        out.write("key\trefid\txml_bytes\tbody_off\tbody_size\ttoken_offset\ttoken_count\n")
        decoder.write_map_rows(windows, out, progress=progress)
    return len(windows)


class PortableAkcDecoder:
    def __init__(
        self,
        akc_path: Path,
        map_path: Path | None = None,
        seek_start: int | None = None,
        seek_size: int | None = None,
    ) -> None:
        self.akc_path = akc_path
        self.akc = akc_path.read_bytes()
        header = akc_header_fields(self.akc)
        self.seek_start = header["seek_start"] if seek_start is None else seek_start
        self.seek_size = header["seek_size"] if seek_size is None else seek_size
        self.refid_keys = load_refid_key_map(map_path) if map_path and map_path.exists() else {}
        self.pos = decode_dictionary(positive_input_from_akc_bytes(self.akc), mode=1)
        self._neg_cache: dict[tuple[int, int], DictionaryState] = {}

    def iter_windows(self, start_key: int = 0, count: int = DEFAULT_MAP_COUNT) -> list[ArticleWindow]:
        return iter_lookup_windows(self.akc, self.seek_start, self.seek_size, start_key, count)

    def resolve_key(self, refid: int) -> int:
        try:
            return self.refid_keys[refid]
        except KeyError as exc:
            raise KeyError(f"refid {refid} is not present in the loaded key map") from exc

    def window_for_key(self, key: int) -> ArticleWindow:
        return lookup_window(self.akc, self.seek_start, self.seek_size, key)

    def _negative_dictionary(self, win: ArticleWindow) -> DictionaryState:
        cache_key = (win.body_off, win.body_size)
        cached = self._neg_cache.get(cache_key)
        if cached is None:
            body = self.akc[win.body_off : win.body_off + win.body_size]
            cached = decode_dictionary(body, mode=0, return_state=True)
            self._neg_cache[cache_key] = cached
        return cached

    def decode_key(self, key: int) -> DecodedArticle:
        win = self.window_for_key(key)
        return self.decode_window(win)

    def decode_refid(self, refid: int) -> DecodedArticle:
        return self.decode_key(self.resolve_key(refid))

    def decode_window(self, win: ArticleWindow, stop_after: int | None = None, token_prefix: int | None = None) -> DecodedArticle:
        neg = self._negative_dictionary(win)
        br = clone_reader(neg.info["_reader"])  # type: ignore[arg-type]
        lengths = neg.info["_table_lengths"]  # type: ignore[assignment]
        main = CanonicalHuffman(lengths["main"])  # type: ignore[index]
        want_tokens = win.token_count if token_prefix is None else min(win.token_count, token_prefix)
        raw_count = win.token_offset + want_tokens
        raw_tokens = decode_tokens(br, main, int(neg.info["n2D00"]), int(self.pos.info["n2D00"]), raw_count)
        tokens = raw_tokens[win.token_offset : raw_count]
        xml = expand_tokens(tokens, self.pos.table, self.pos.data, neg.table, neg.data, win.xml_size, stop_after=stop_after)
        return DecodedArticle(win.key, extract_refid(xml), xml, win)

    def write_map_rows(
        self,
        windows: Iterable[ArticleWindow],
        out: TextIO,
        progress: int = 0,
        prefix_bytes: int = 256,
        token_prefix: int = 64,
    ) -> None:
        groups: dict[tuple[int, int], list[ArticleWindow]] = defaultdict(list)
        window_list = list(windows)
        for win in window_list:
            groups[(win.body_off, win.body_size)].append(win)

        rows: list[tuple[int, int | None, int, ArticleWindow]] = []
        done = 0
        for wins in groups.values():
            neg = self._negative_dictionary(wins[0])
            br = clone_reader(neg.info["_reader"])  # type: ignore[arg-type]
            lengths = neg.info["_table_lengths"]  # type: ignore[assignment]
            main = CanonicalHuffman(lengths["main"])  # type: ignore[index]
            raw_count = max(win.token_offset + min(win.token_count, token_prefix) for win in wins)
            raw_tokens = decode_tokens(br, main, int(neg.info["n2D00"]), int(self.pos.info["n2D00"]), raw_count)
            for win in wins:
                start = win.token_offset
                end = start + min(win.token_count, token_prefix)
                xml = expand_tokens(
                    raw_tokens[start:end],
                    self.pos.table,
                    self.pos.data,
                    neg.table,
                    neg.data,
                    win.xml_size,
                    stop_after=prefix_bytes,
                )
                rows.append((win.key, extract_refid(xml), win.xml_size, win))
                done += 1
                if progress and done % progress == 0:
                    print(f"decoded {done}/{len(window_list)} keys (groups={len(groups)})")

        for key, refid, xml_len, win in sorted(rows):
            out.write(
                f"{key}\t{refid or ''}\t{xml_len}\t{win.body_off}\t{win.body_size}"
                f"\t{win.token_offset}\t{win.token_count}\n"
            )
