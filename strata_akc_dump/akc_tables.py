"""Static tables baked into MSENCDAT.DLL, extracted for the portable AKC decoder.

All values were read directly from MSENCDAT.DLL (image base 0x45480000) — see
``scripts/extract_akc_tables.py`` for the extraction. They drive the DA30 main
decode loop and confirm the codec is an LZX-derivative with position slots.
"""
from __future__ import annotations

# --- Opcode dispatch (DA30 main loop, jump table @0x4549E204 + byte map @0x4549E214) ---
#
# A decoded record's leading control byte (0..10) selects a validation/emit path.
# These are the SAME 11 opcodes that scripts/probe_e770_expand.py expands into XML.
#
# byte map @0x4549E214: opcode -> jump-table index
OPCODE_PATH_INDEX = [0, 0, 0, 0, 0, 1, 1, 1, 0, 2, 3]  # opcodes 0..10
#
# jump table @0x4549E204: index -> handler address (in MSENCDAT)
OPCODE_HANDLER_ADDR = [0x4549E01E, 0x4549E06B, 0x4549E02B, 0x4549E055]
#
# Path meanings (from the handler disassembly):
#   path 0 (0x4549E01E): opcodes 0,1,2,3,4,8 -> require record length > 1, then common emit
#   path 1 (0x4549E06B): opcodes 5,6,7        -> common emit directly
#   path 2 (0x4549E02B): opcode 9 (close_ref) -> require length == 3 and 16-bit field < 0x100
#   path 3 (0x4549E055): opcode 10 (empty)    -> require length == 1 and ctx[+0x20] set
OPCODE_NOTES = {
    0: "open-tag name (casefold first char)",
    1: "text run",
    2: "text run (reset case state)",
    3: "open tag (push)",
    4: "attribute name (space-separated)",
    5: "attribute name (= separated)",
    6: "single-quoted attr value",
    7: "double-quoted attr value",
    8: "explicit close </name>",
    9: "close-ref (pop by relative index)",
    10: "empty self-close />",
}

# --- Position-slot base/extra-bits table (DicT_create @0x4549EB60) ---------------
#
# Seeded from interleaved (count, shift) pairs at globals 0x454AE0B0 / 0x454AE0B4.
# The create loop runs to offset 0xA8 (21 pairs) and expands each pair into `count`
# slots, each contributing `1 << shift` to a running base — the classic LZX
# position_base / extra_bits layout. Stored in the context at +0x128.
POSITION_SLOT_SEED = [
    (12, 0), (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7),
    (1, 8), (1, 9), (1, 10), (1, 11), (1, 12), (1, 13), (1, 14), (1, 15),
    (1, 16), (1, 17), (1, 18), (1, 19), (1, 20),
]


def _expand_position_slots() -> list[tuple[int, int]]:
    """Expand POSITION_SLOT_SEED into per-slot (extra_bits, base) pairs."""
    slots: list[tuple[int, int]] = []
    base = 0
    for count, shift in POSITION_SLOT_SEED:
        for _ in range(count):
            slots.append((shift, base))
            base += 1 << shift
    return slots


# 32 slots: 0..11 are direct (0 extra bits, base 0..11), 12..31 grow by extra bits.
# Max base ~2,097,162 => ~2 MB maximum window/dictionary size.
POSITION_SLOTS = _expand_position_slots()
POSITION_SLOT_COUNT = len(POSITION_SLOTS)

# --- Huffman alphabets built by DA30 (the four 0x1D8F0 calls) --------------------
#
# (root_bits, symbol_count, context-relative table offset, lengths-buffer offset)
# Names are functional, inferred from how the main loop indexes each table.
HUFFMAN_TABLES = {
    "main": (10, 0x140, 0x0DF8, 0x1AF8),       # literal/length, decoded by routine @0x4549E220
    "selector": (5, 0x40, 0x2B78, 0x2CB8),     # decoded first in the DA30 main loop
    "table3": (8, 0x120, 0x1C38, 0x22B8),      # main-loop second table (with 0x3D0 parity stride)
    "table4": (8, 0x120, 0x23D8, 0x2A58),      # main-loop continuation table
}
