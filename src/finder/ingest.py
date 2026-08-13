"""CSV / table ingest with header auto-detection and domain-level dedupe."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from finder.normalize import NormalizedPerson, normalize_domain, normalize_person, split_full_name


@dataclass
class IngestedRow:
    first: str
    last: str
    domain: str
    normalized: NormalizedPerson
    passthrough: dict[str, Any]
    source_email: str | None = None
    skipped_duplicate: bool = False


@dataclass
class IngestResult:
    rows: list[IngestedRow] = field(default_factory=list)
    duplicates_dropped: int = 0
    missing_required: int = 0
    raw_count: int = 0


def _norm_header(value: str) -> str:
    return (value or "").strip().lower().replace(" ", "_").replace("-", "_")


def detect_columns(
    headers: Sequence[str],
    aliases: Mapping[str, Sequence[str]],
) -> dict[str, str | None]:
    lookup = {_norm_header(h): h for h in headers}
    mapping: dict[str, str | None] = {}
    for role, names in aliases.items():
        mapping[role] = None
        for alias in names:
            key = _norm_header(alias)
            if key in lookup:
                mapping[role] = lookup[key]
                break
    return mapping


def _cell(row: Mapping[str, Any], column: str | None) -> str:
    if not column:
        return ""
    value = row.get(column, "")
    if value is None:
        return ""
    return str(value).strip()


def _cell_role(
    row: Mapping[str, Any],
    role: str,
    detected: Mapping[str, str | None],
    aliases: Mapping[str, Sequence[str]],
) -> str:
    value = _cell(row, detected.get(role))
    if value:
        return value
    lookup = {_norm_header(k): k for k in row.keys()}
    for alias in aliases.get(role, []):
        key = lookup.get(_norm_header(alias))
        if key:
            value = _cell(row, key)
            if value:
                return value
    return ""


def _iter_dicts(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def ingest_records(
    records: Sequence[Mapping[str, Any]],
    *,
    aliases: Mapping[str, Sequence[str]],
    suffixes: set[str],
    credentials: set[str],
    personal_domains: set[str],
    column_map: Mapping[str, str] | None = None,
) -> IngestResult:
    if not records:
        return IngestResult()
    headers: list[str] = []
    seen_headers: set[str] = set()
    for record in records:
        for key in record.keys():
            if key not in seen_headers:
                seen_headers.add(key)
                headers.append(key)
    detected = detect_columns(headers, aliases)
    if column_map:
        for role, col in column_map.items():
            if col:
                detected[role] = col

    result = IngestResult(raw_count=len(records))
    seen: set[tuple[str, str, str]] = set()
    for raw in records:
        first = _cell_role(raw, "first", detected, aliases)
        last = _cell_role(raw, "last", detected, aliases)
        full_name = _cell_role(raw, "name", detected, aliases)
        domain_raw = _cell_role(raw, "domain", detected, aliases)
        source_email = _cell_role(raw, "email", detected, aliases) or None

        if not first and not last and full_name:
            first, last = split_full_name(full_name, suffixes, credentials)

        if not domain_raw and source_email and "@" in source_email:
            domain_raw = source_email.rsplit("@", 1)[-1]

        if not domain_raw:
            result.missing_required += 1
            continue

        person = normalize_person(
            first,
            last,
            domain_raw,
            suffixes=suffixes,
            credentials=credentials,
            personal_domains=personal_domains,
            full_name=full_name or None,
        )
        if not person.domain:
            result.missing_required += 1
            continue

        key = (person.primary_first, person.primary_last, person.domain)
        if key in seen:
            result.duplicates_dropped += 1
            continue
        seen.add(key)

        passthrough = {k: ("" if v is None else v) for k, v in raw.items()}
        result.rows.append(
            IngestedRow(
                first=first or person.original_first,
                last=last or person.original_last,
                domain=person.domain,
                normalized=person,
                passthrough=passthrough,
                source_email=source_email.lower() if source_email else None,
            )
        )
    return result


def ingest_csv_text(
    text: str,
    *,
    aliases: Mapping[str, Sequence[str]],
    suffixes: set[str],
    credentials: set[str],
    personal_domains: set[str],
    column_map: Mapping[str, str] | None = None,
) -> IngestResult:
    reader = csv.DictReader(io.StringIO(text))
    records = [{k: v for k, v in row.items() if k is not None} for row in reader]
    return ingest_records(
        records,
        aliases=aliases,
        suffixes=suffixes,
        credentials=credentials,
        personal_domains=personal_domains,
        column_map=column_map,
    )


def ingest_csv_path(path: Path, **kwargs: Any) -> IngestResult:
    text = Path(path).read_text(encoding="utf-8-sig")
    return ingest_csv_text(text, **kwargs)


def domain_from_email(email: str) -> str:
    if "@" not in email:
        return ""
    return normalize_domain(email.rsplit("@", 1)[-1])
