"""
catalog.py — parse Encarta's master index (CATALOG.STE) into the content-ID list
and baggage map. PORTABLE: pure byte parsing, runs on any OS.

CATALOG.STE is a LIT container; extract it with lit.py first, then point this at the
resulting catalog2/ folder (data.ecn, baggage.ecs, relart.ecn, ...).

Findings (see CATALOG_FINDINGS.md / Linear NEX-391/392):
    content.ecn : after a header/secondary section, sorted LE u32 runs that match
                high content IDs seen in live traces.
    data.ecn    : broader sorted LE u32 runs that include low resource IDs too.
  baggage.ecs : the gid -> baggage-id map (ECS format, record pairs). PARSE TODO.
  relart.ecn  : related-articles graph.

KNOWN-GOOD anchors (verified present in data.ecn):
  0x2D64A494 (761570452)  — content id seen live in the debugger
  0x67CE8843 (1741588547) — refid seen in decoded XML
"""
from __future__ import annotations
import struct
import json
from pathlib import Path
from dataclasses import dataclass, field


# IDs we captured live; used to sanity-check that parsing found the real array.
KNOWN_IDS = (0x2D64A494, 0x67CE8843)


@dataclass
class CatalogIndex:
    content_ids: list[int] = field(default_factory=list)
    baggage_map: dict[int, str] = field(default_factory=dict)  # gid -> baggage hexid (TODO)

    def write(self, out_dir: str | Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "content_ids.txt").write_text(
            "\n".join(str(i) for i in self.content_ids)
        )
        (out / "baggage_map.json").write_text(json.dumps(self.baggage_map, indent=2))


def _u32_stream(data: bytes, start: int = 0) -> list[int]:
    end = start + (len(data) - start) // 4 * 4
    return [v for (v,) in struct.iter_unpack("<L", data[start:end])]


def parse_content_ids(ecn: bytes) -> list[int]:
    """
    Extract sorted content-ID-like runs from an ECN stream.

    Strategy: these files begin with a header/secondary section whose entries pack as
    (small u16 tag, u16 val) and read as small-ish u32s. The useful ID arrays are long
    runs of large (>16M) values that increase by small positive deltas. We locate the
    longest such run and take all large values from there on.

    NOTE: this is heuristic. The exact record format / article-vs-media tag split is
    still a TODO. When choosing between extracted catalog streams, prefer content.ecn;
    data.ecn is a broader resource index and includes low IDs that do not validate as
    article refids.
    """
    body = ecn[4:] if ecn[:4] == b"ECN\x00" else ecn
    u32 = _u32_stream(body)

    # find the longest locally-sorted run of large values
    best_start = best_len = 0
    cur_start, cur_len = 0, 1
    for i in range(1, len(u32)):
        d = u32[i] - u32[i - 1]
        if 0 < d < 256 and u32[i] > 0x1000000:
            cur_len += 1
        else:
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
            cur_start, cur_len = i, 1
    if cur_len > best_len:
        best_len, best_start = cur_len, cur_start

    ids: list[int] = []
    for v in u32[best_start:]:
        if v > 0x1000000:
            ids.append(v)
        elif ids and len(ids) > 1000:
            break  # left the ID region after collecting a solid run
    return sorted(set(ids))


def parse_baggage_map(baggage_ecs: bytes) -> dict[int, str]:
    """
    Parse baggage.ecs into a gid -> baggage-hexid map.

    TODO (NEX-392): the ECS record format is (u16, u16) pairs; the exact semantics
    (which field is gid, which is the baggage index, how it maps to the <hexid>.xml
    stem) need pinning. Returns empty until implemented.
    """
    # body = baggage_ecs[4:] if baggage_ecs[:4] == b"ECS\x00" else baggage_ecs
    return {}


def load_catalog(catalog_dir: str | Path) -> CatalogIndex:
    """Read an already-extracted catalog2/ folder into a CatalogIndex."""
    d = Path(catalog_dir)
    # content.ecn is the narrower high-ID content index. data.ecn contains a much
    # larger mixed resource set whose low 210xxxxxx IDs do not validate via MSENCDAT.
    ids_path = d / "content.ecn"
    if not ids_path.exists():
        ids_path = d / "data.ecn"
    ids = parse_content_ids(ids_path.read_bytes())

    # sanity check against known-live IDs
    idset = set(ids)
    missing = [hex(k) for k in KNOWN_IDS if k not in idset]
    if missing:
        raise RuntimeError(
            f"known content IDs not found in data.ecn: {missing} — "
            "parser likely landed on the wrong section"
        )

    baggage = {}
    bag_path = d / "baggage.ecs"
    if bag_path.exists():
        baggage = parse_baggage_map(bag_path.read_bytes())

    return CatalogIndex(content_ids=ids, baggage_map=baggage)
