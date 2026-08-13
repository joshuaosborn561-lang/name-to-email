"""Name and domain normalization.

Cleans production scrape exports before any permutation is generated.
All strip-lists live in config/finder.yaml so they can be retuned without a code change.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse

_PAREN_RE = re.compile(r"\(([^)]+)\)")
_NON_NAME = re.compile(r"[^a-z\-\s']")
_MULTISPACE = re.compile(r"\s+")


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def title_fix(value: str) -> str:
    """Preserve internal capitalization intent while accepting ALL CAPS / all lower."""
    return value.strip()


def _tokens(value: str) -> list[str]:
    return [t for t in _MULTISPACE.sub(" ", value.strip()).split(" ") if t]


def _is_roman_or_suffix(token: str, suffixes: set[str], credentials: set[str]) -> bool:
    cleaned = token.lower().rstrip(".")
    return cleaned in suffixes or cleaned in credentials


def strip_trailing_qualifiers(value: str, suffixes: set[str], credentials: set[str]) -> str:
    tokens = _tokens(value)
    while tokens and _is_roman_or_suffix(tokens[-1], suffixes, credentials):
        tokens.pop()
    # Also drop leading/internal credentials that show up as their own token.
    tokens = [t for t in tokens if not _is_roman_or_suffix(t, suffixes, credentials)]
    return " ".join(tokens)


def extract_nickname(value: str) -> tuple[str, str | None]:
    """Return (name_without_parens, nickname_or_none)."""
    match = _PAREN_RE.search(value)
    nickname = match.group(1).strip() if match else None
    stripped = _PAREN_RE.sub(" ", value)
    stripped = _MULTISPACE.sub(" ", stripped).strip()
    return stripped, nickname or None


def ascii_local_part(value: str) -> str:
    """Lowercase, strip accents, drop apostrophes and internal spaces."""
    value = strip_accents(value).lower()
    value = value.replace("'", "").replace("’", "").replace("`", "")
    value = value.replace(" ", "")
    value = value.replace(".", "")
    return value


def last_name_variants(last: str) -> list[str]:
    """Hyphenated surnames: full hyphenated, concatenated, final segment.

    Internal spaces are concatenated (Del Rosario -> delrosario), not treated as hyphens.
    """
    cleaned = last.strip().lower()
    cleaned = strip_accents(cleaned)
    cleaned = cleaned.replace("'", "").replace("’", "")
    cleaned = _MULTISPACE.sub(" ", cleaned).strip()
    if not cleaned:
        return []
    if "-" in cleaned:
        hyphenated = cleaned.replace(" ", "")
        concatenated = hyphenated.replace("-", "")
        final = hyphenated.split("-")[-1]
        variants = [hyphenated, concatenated, final]
        seen: set[str] = set()
        out: list[str] = []
        for item in variants:
            item = item.strip("-")
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out
    return [cleaned.replace(" ", "")]


def is_initial_only(value: str) -> bool:
    token = value.strip().rstrip(".")
    return len(token) == 1 and token.isalpha()


def split_full_name(full_name: str, suffixes: set[str], credentials: set[str]) -> tuple[str, str]:
    """Split a single name column into (first, last).

    Qualifiers are stripped first so 'Leo Karl III' becomes Leo / Karl, not last='III'.
    Remaining tokens: first token is first name, the rest is last name (handles Del Rosario).
    Comma form 'Last, First' is also accepted.
    """
    raw = title_fix(full_name)
    raw, _ = extract_nickname(raw)
    raw = strip_trailing_qualifiers(raw, suffixes, credentials)
    if "," in raw:
        left, right = raw.split(",", 1)
        return right.strip(), left.strip()
    tokens = _tokens(raw)
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0], ""
    return tokens[0], " ".join(tokens[1:])


@dataclass
class NormalizedPerson:
    original_first: str
    original_last: str
    original_domain: str
    first_variants: list[str] = field(default_factory=list)
    last_variants: list[str] = field(default_factory=list)
    domain: str = ""
    insufficient_name: bool = False
    personal_domain: bool = False
    nickname: str | None = None

    @property
    def primary_first(self) -> str:
        return self.first_variants[0] if self.first_variants else ""

    @property
    def primary_last(self) -> str:
        return self.last_variants[0] if self.last_variants else ""


def normalize_person(
    first: str,
    last: str,
    domain: str,
    *,
    suffixes: set[str],
    credentials: set[str],
    personal_domains: set[str],
    full_name: str | None = None,
) -> NormalizedPerson:
    first = title_fix(first or "")
    last = title_fix(last or "")
    if full_name and (not first or not last):
        split_first, split_last = split_full_name(full_name, suffixes, credentials)
        first = first or split_first
        last = last or split_last

    first_clean, nickname = extract_nickname(first)
    first_clean = strip_trailing_qualifiers(first_clean, suffixes, credentials)
    last_clean, last_nick = extract_nickname(last)
    last_clean = strip_trailing_qualifiers(last_clean, suffixes, credentials)
    if last_nick and not nickname:
        nickname = last_nick

    # Production scrape: last name column is "III" / "Jr" while first holds "Leo Karl".
    if not last_clean and first_clean:
        tokens = _tokens(first_clean)
        if len(tokens) >= 2:
            first_clean = tokens[0]
            last_clean = " ".join(tokens[1:])
    if not last_clean and full_name:
        split_first, split_last = split_full_name(full_name, suffixes, credentials)
        first_clean = first_clean or split_first
        last_clean = split_last

    domain_norm = normalize_domain(domain)
    personal = domain_norm in personal_domains if domain_norm else False

    if not last_clean or is_initial_only(last_clean):
        return NormalizedPerson(
            original_first=first,
            original_last=last,
            original_domain=domain,
            domain=domain_norm,
            insufficient_name=True,
            personal_domain=personal,
            nickname=ascii_local_part(nickname) if nickname else None,
        )

    first_primary = ascii_local_part(nickname) if nickname else ascii_local_part(first_clean)
    first_original = ascii_local_part(first_clean)
    first_variants: list[str] = []
    for candidate in (first_primary, first_original):
        if candidate and candidate not in first_variants and not is_initial_only(candidate):
            first_variants.append(candidate)
        elif candidate and is_initial_only(candidate) and candidate not in first_variants:
            # A single-letter first name is still usable for {f}{last} patterns.
            first_variants.append(candidate)

    lasts = [ascii_local_part(v) if "-" not in v else v.replace("'", "") for v in last_name_variants(last_clean)]
    lasts = [v for v in lasts if v and not is_initial_only(v)]

    if not first_variants or not lasts:
        return NormalizedPerson(
            original_first=first,
            original_last=last,
            original_domain=domain,
            domain=domain_norm,
            insufficient_name=True,
            personal_domain=personal,
            nickname=ascii_local_part(nickname) if nickname else None,
        )

    return NormalizedPerson(
        original_first=first,
        original_last=last,
        original_domain=domain,
        first_variants=first_variants,
        last_variants=lasts,
        domain=domain_norm,
        insufficient_name=False,
        personal_domain=personal,
        nickname=ascii_local_part(nickname) if nickname else None,
    )


def normalize_domain(raw: str) -> str:
    """Strip protocol, www., paths, query strings; lowercase."""
    value = (raw or "").strip().lower()
    if not value:
        return ""
    value = value.split()[0]
    if "://" not in value and value.startswith("www."):
        value = "http://" + value
    elif "://" not in value and ("/" in value or value.count(".") >= 1):
        # Treat bare host or host/path as a URL so urlparse extracts the host.
        if "/" in value:
            value = "http://" + value
    parsed = urlparse(value if "://" in value else "http://" + value)
    host = parsed.netloc or parsed.path.split("/")[0]
    host = host.split("@")[-1]
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    host = host.strip(".")
    return host
