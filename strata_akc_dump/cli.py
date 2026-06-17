"""
cli.py — portable extractor orchestrator.

  enumerate    CATALOG.STE -> index/ (content_ids.txt, baggage_map.json)   [any OS]
  extract-lit  *.EIT/*.STE -> dump/baggage + dump/articles (bib XML)        [any OS]
  map-akc      *.AKC -> key/refid TSV                                      [any OS]
  decode-*     *.AKC -> XML records                                        [any OS]
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path


def cmd_enumerate(args) -> int:
    from .lit import extract_lit
    from .catalog import load_catalog

    tmp = Path(args.out) / "_catalog_raw"
    extract_lit(args.catalog, tmp)
    cat_dir = next((p.parent for p in tmp.rglob("data.ecn")), None)
    if cat_dir is None:
        print("error: data.ecn not found in extracted CATALOG.STE", file=sys.stderr)
        return 1
    idx = load_catalog(cat_dir)
    idx.write(args.out)
    print(f"enumerated {len(idx.content_ids)} content IDs -> {args.out}/content_ids.txt")
    print(f"baggage map: {len(idx.baggage_map)} entries (TODO if 0)")
    return 0


def cmd_extract_lit(args) -> int:
    from .lit import extract_lit
    out = Path(args.out)
    for f in args.files:
        n = extract_lit(f, out / "_lit" / Path(f).stem)
        print(f"{f}: extracted {n} files")
    return 0


def cmd_map_akc(args) -> int:
    from .portable_akc import write_refid_key_map

    mapped = write_refid_key_map(
        Path(args.akc),
        Path(args.out),
        start_key=args.start_key,
        count=args.count,
        seek_start=args.seek_start,
        seek_size=args.seek_size,
        progress=args.progress,
    )
    print(f"mapped {mapped} keys -> {args.out}")
    return 0


def cmd_decode_article(args) -> int:
    from .portable_akc import PortableAkcDecoder

    dec = PortableAkcDecoder(
        Path(args.akc),
        map_path=Path(args.map) if args.map else None,
        seek_start=args.seek_start,
        seek_size=args.seek_size,
    )
    if args.key is not None:
        article = dec.decode_key(args.key)
    elif args.refid is not None:
        article = dec.decode_refid(args.refid)
    else:
        raise SystemExit("pass --refid or --key")

    out = Path(args.out)
    if out.is_dir() or str(args.out).endswith(("/", "\\")):
        refid = article.refid or args.refid or article.key
        out = out / f"{refid}.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(article.xml)
    print(f"decoded key=0x{article.key:X} refid={article.refid} bytes={len(article.xml)} -> {out}")
    return 0


def _read_key_map_rows(path: Path):
    from .portable_akc import ArticleWindow

    rows: list[tuple[int, ArticleWindow]] = []
    with path.open("r", encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        key_i = header.index("key")
        refid_i = header.index("refid")
        body_off_i = header.index("body_off") if "body_off" in header else -1
        body_size_i = header.index("body_size") if "body_size" in header else -1
        xml_i = header.index("xml_bytes") if "xml_bytes" in header else -1
        token_off_i = header.index("token_offset") if "token_offset" in header else -1
        token_count_i = header.index("token_count") if "token_count" in header else -1
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) <= max(key_i, refid_i) or not parts[refid_i]:
                continue
            key = int(parts[key_i])
            if min(body_off_i, body_size_i, xml_i, token_off_i, token_count_i) >= 0:
                win = ArticleWindow(
                    key=key,
                    body_off=int(parts[body_off_i]),
                    body_size=int(parts[body_size_i]),
                    xml_size=int(parts[xml_i]),
                    token_offset=int(parts[token_off_i]),
                    token_count=int(parts[token_count_i]),
                )
            else:
                win = ArticleWindow(key=key, body_off=-1, body_size=0, xml_size=0, token_offset=0, token_count=0)
            rows.append((int(parts[refid_i]), win))
    return sorted(rows, key=lambda row: row[1].key)


def cmd_decode_portable(args) -> int:
    from .portable_akc import PortableAkcDecoder

    map_path = Path(args.map)
    dec = PortableAkcDecoder(
        Path(args.akc),
        map_path=map_path,
        seek_start=args.seek_start,
        seek_size=args.seek_size,
    )
    map_rows = _read_key_map_rows(map_path)
    if args.refid:
        wanted = set(args.refid)
        targets = [row for row in map_rows if row[0] in wanted]
        missing = wanted - {refid for refid, _win in targets}
        if missing:
            raise SystemExit(f"refids not present in map: {sorted(missing)}")
    else:
        targets = map_rows
    if args.limit is not None:
        targets = targets[: args.limit]

    out = Path(args.out)
    done = 0
    for refid, win in targets:
        article = dec.decode_window(win) if win.body_off >= 0 else dec.decode_key(win.key)
        dest = out / f"{refid}.xml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(article.xml)
        done += 1
        if args.progress and done % args.progress == 0:
            print(f"decoded {done}/{len(targets)} articles...")
    print(f"decoded {done} articles -> {out}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="strata-akc-dump", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enumerate", help="CATALOG.STE -> content IDs + baggage map")
    e.add_argument("--catalog", required=True)
    e.add_argument("--out", default="./index")
    e.set_defaults(func=cmd_enumerate)

    x = sub.add_parser("extract-lit", help="extract .EIT/.STE LIT containers")
    x.add_argument("files", nargs="+")
    x.add_argument("--out", default="./dump")
    x.set_defaults(func=cmd_extract_lit)

    m = sub.add_parser("map-akc", help="portable AKC key -> refid map")
    m.add_argument("--akc", required=True, help="path to an AKC file with the DA30/E560/E770 record codec")
    m.add_argument("--out", default="./output/key_refid_map.tsv")
    m.add_argument("--start-key", type=lambda s: int(s, 0), default=0)
    m.add_argument("--count", type=int)
    m.add_argument("--seek-start", type=lambda s: int(s, 0), help="override seek table offset; defaults to AKC header")
    m.add_argument("--seek-size", type=lambda s: int(s, 0), help="override seek table size; defaults to AKC header")
    m.add_argument("--progress", type=int, default=5000)
    m.set_defaults(func=cmd_map_akc)

    a = sub.add_parser("decode-article", help="portable decode of one AKC XML record")
    a.add_argument("--akc", required=True, help="path to an AKC file with the DA30/E560/E770 record codec")
    a.add_argument("--map", default="./output/key_refid_map.tsv")
    group = a.add_mutually_exclusive_group(required=True)
    group.add_argument("--refid", type=int)
    group.add_argument("--key", type=lambda s: int(s, 0))
    a.add_argument("--out", required=True, help="output XML path or directory")
    a.add_argument("--seek-start", type=lambda s: int(s, 0), help="override seek table offset; defaults to AKC header")
    a.add_argument("--seek-size", type=lambda s: int(s, 0), help="override seek table size; defaults to AKC header")
    a.set_defaults(func=cmd_decode_article)

    pdec = sub.add_parser("decode-portable", help="portable decode mapped AKC XML records")
    pdec.add_argument("--akc", required=True, help="path to an AKC file with the DA30/E560/E770 record codec")
    pdec.add_argument("--map", default="./output/key_refid_map.tsv")
    pdec.add_argument("--out", default="./dump/articles")
    pdec.add_argument("--refid", type=int, action="append", help="decode only this refid; may repeat")
    pdec.add_argument("--limit", type=int, help="decode only the first N targets")
    pdec.add_argument("--seek-start", type=lambda s: int(s, 0), help="override seek table offset; defaults to AKC header")
    pdec.add_argument("--seek-size", type=lambda s: int(s, 0), help="override seek table size; defaults to AKC header")
    pdec.add_argument("--progress", type=int, default=500)
    pdec.set_defaults(func=cmd_decode_portable)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
