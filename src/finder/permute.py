"""Candidate address generation from config-driven pattern templates."""

from __future__ import annotations

from dataclasses import dataclass

from finder.normalize import NormalizedPerson


@dataclass(frozen=True)
class Candidate:
    email: str
    pattern: str
    first: str
    last: str


def _local_part(template: str, first: str, last: str) -> str:
    first = first or ""
    last = last or ""
    f = first[:1] if first else ""
    l = last[:1] if last else ""
    # Hyphenated last keeps the hyphen only where the template uses {last} as-is.
    return (
        template.replace("{first}", first)
        .replace("{last}", last)
        .replace("{f}", f)
        .replace("{l}", l)
    )


def apply_pattern(template: str, first: str, last: str, domain: str) -> str:
    local = _local_part(template, first, last)
    local = local.strip("._-")
    return f"{local}@{domain}"


def generate_candidates(
    person: NormalizedPerson,
    patterns: list[str],
    *,
    known_pattern: str | None = None,
    max_candidates: int = 10,
) -> list[Candidate]:
    if person.insufficient_name or not person.domain:
        return []

    templates = [known_pattern] if known_pattern else list(patterns)
    templates = [t for t in templates if t]
    if not templates:
        return []

    # Highest-probability identity first: preferred first × preferred last × pattern order.
    # Remaining name variants follow, still walking templates in frequency order.
    pairs: list[tuple[str, str]] = []
    if person.first_variants and person.last_variants:
        pairs.append((person.first_variants[0], person.last_variants[0]))
        for last in person.last_variants[1:]:
            pairs.append((person.first_variants[0], last))
        for first in person.first_variants[1:]:
            for last in person.last_variants:
                pairs.append((first, last))

    seen: set[str] = set()
    out: list[Candidate] = []
    for first, last in pairs:
        for template in templates:
            email = apply_pattern(template, first, last, person.domain)
            if email in seen or not email.split("@")[0]:
                continue
            seen.add(email)
            out.append(Candidate(email=email, pattern=template, first=first, last=last))
            if len(out) >= max_candidates:
                return out
    return out


def infer_pattern(
    email: str,
    person: NormalizedPerson,
    patterns: list[str],
) -> str | None:
    """Return the template that produced `email`, or None if none match."""
    target = email.strip().lower()
    if "@" not in target:
        return None
    for candidate in generate_candidates(
        person, patterns, max_candidates=max(40, len(patterns) * 6)
    ):
        if candidate.email.lower() == target:
            return candidate.pattern
    return None
