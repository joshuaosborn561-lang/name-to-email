"""Segmented CSV export. Catch-all is never merged into valid."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Iterable, Sequence

from finder.models import Person

SEGMENTS = ("valid", "catchall", "unresolved")

OUTPUT_FIELDS = [
    "email",
    "status",
    "pattern_used",
    "attempts",
    "verifier",
    "domain_is_catchall",
    "confidence",
    "first",
    "last",
    "domain",
]


def segment_for(person: Person) -> str:
    if person.status == "valid":
        return "valid"
    if person.status == "catchall":
        return "catchall"
    return "unresolved"


def _row(person: Person) -> dict[str, str]:
    extra = {k: "" if v is None else str(v) for k, v in (person.passthrough or {}).items()}
    extra.update(
        {
            "email": person.email or "",
            "status": person.status,
            "pattern_used": person.pattern_used or "",
            "attempts": str(person.attempts or 0),
            "verifier": person.verifier or "",
            "domain_is_catchall": "true" if person.domain_is_catchall else "false",
            "confidence": person.confidence or "",
            "first": person.first,
            "last": person.last,
            "domain": person.norm_domain or person.domain,
        }
    )
    return extra


def _headers(people: Sequence[Person]) -> list[str]:
    extras: list[str] = []
    seen = set(OUTPUT_FIELDS)
    for person in people:
        for key in person.passthrough or {}:
            if key not in seen:
                extras.append(key)
                seen.add(key)
    return OUTPUT_FIELDS + extras


def write_csv(people: Sequence[Person], dest: Path | None = None) -> str:
    headers = _headers(people)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for person in people:
        writer.writerow(_row(person))
    text = buffer.getvalue()
    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return text


def export_segments(people: Iterable[Person], out_dir: Path) -> dict[str, Path]:
    grouped: dict[str, list[Person]] = {s: [] for s in SEGMENTS}
    for person in people:
        grouped[segment_for(person)].append(person)
    paths: dict[str, Path] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for segment, rows in grouped.items():
        path = out_dir / f"{segment}.csv"
        write_csv(rows, path)
        paths[segment] = path
    return paths
