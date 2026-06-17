"""
dumper.py — write decoded resources to a dump folder + a manifest for resumable runs.
PORTABLE.

Layout:
  dump/
    articles/<contentid>.xml      # decoded article bodies (from msitss)
    baggage/<hexid>.<ext>         # media + bib XML (from LIT extraction or msitss)
    manifest.json                 # what's been written, for resume + provenance
"""
from __future__ import annotations
import json
from pathlib import Path
from dataclasses import dataclass, field, asdict


@dataclass
class Manifest:
    articles: dict[str, int] = field(default_factory=dict)   # id -> byte length
    baggage: dict[str, int] = field(default_factory=dict)    # hexid -> byte length
    errors: dict[str, str] = field(default_factory=dict)     # id/url -> error msg

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if path.exists():
            return cls(**json.loads(path.read_text()))
        return cls()

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))


class Dumper:
    def __init__(self, out_dir: str | Path):
        self.root = Path(out_dir)
        (self.root / "articles").mkdir(parents=True, exist_ok=True)
        (self.root / "baggage").mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.manifest = Manifest.load(self.manifest_path)

    def has_article(self, content_id: int) -> bool:
        return str(content_id) in self.manifest.articles

    def write_article(self, content_id: int, data: bytes) -> None:
        (self.root / "articles" / f"{content_id}.xml").write_bytes(data)
        self.manifest.articles[str(content_id)] = len(data)

    def write_baggage(self, hexid: str, data: bytes, ext: str = "bin") -> None:
        name = hexid if "." in hexid else f"{hexid}.{ext}"
        (self.root / "baggage" / name).write_bytes(data)
        self.manifest.baggage[hexid] = len(data)

    def record_error(self, key: str, msg: str) -> None:
        self.manifest.errors[key] = msg

    def flush(self) -> None:
        self.manifest.save(self.manifest_path)
