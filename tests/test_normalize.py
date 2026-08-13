from finder.config import Settings
from finder.normalize import (
    ascii_local_part,
    is_initial_only,
    last_name_variants,
    normalize_domain,
    normalize_person,
    split_full_name,
    strip_trailing_qualifiers,
)


def _n(first: str, last: str, domain: str = "acme.test"):
    s = Settings.load()
    return normalize_person(
        first,
        last,
        domain,
        suffixes=s.suffixes,
        credentials=s.credentials,
        personal_domains=s.personal_domains,
    )


def test_strip_generational_suffix_from_last():
    s = Settings.load()
    assert split_full_name("Leo Karl III", s.suffixes, s.credentials) == ("Leo", "Karl")
    person = _n("Leo Karl", "III")
    # Last name III is stripped; remaining last is empty -> insufficient if only qualifier.
    # When first carries both given names and last is the suffix:
    person = normalize_person(
        "Leo Karl",
        "III",
        "dealer.test",
        suffixes=s.suffixes,
        credentials=s.credentials,
        personal_domains=s.personal_domains,
        full_name="Leo Karl III",
    )
    assert person.primary_first == "leo"
    assert person.primary_last == "karl"
    assert not person.insufficient_name


def test_strip_credentials():
    s = Settings.load()
    cleaned = strip_trailing_qualifiers("Jane Smith MBA", s.suffixes, s.credentials)
    assert cleaned == "Jane Smith"


def test_accents_apostrophes_spaces():
    assert ascii_local_part("D'Andrea") == "dandrea"
    assert ascii_local_part("Del Rosario") == "delrosario"
    assert ascii_local_part("José") == "jose"
    person = _n("José", "D'Andrea")
    assert person.primary_first == "jose"
    assert person.primary_last == "dandrea"


def test_nickname_preferred():
    person = _n("Robert (Bob)", "Cohen")
    assert person.first_variants[0] == "bob"
    assert "robert" in person.first_variants


def test_hyphenated_last_three_forms():
    assert last_name_variants("Rothbart-Mooney") == [
        "rothbart-mooney",
        "rothbartmooney",
        "mooney",
    ]


def test_initial_only_last_is_insufficient():
    assert is_initial_only("C.")
    person = _n("Oliver", "C.")
    assert person.insufficient_name
    person = _n("Louis", "P.")
    assert person.insufficient_name


def test_casing():
    assert _n("LIZ", "DELUCA").primary_first == "liz"
    assert _n("jose", "medero").primary_last == "medero"


def test_domain_normalization():
    assert normalize_domain("https://www.Acme.com/path?q=1") == "acme.com"
    assert normalize_domain("www.foo.com") == "foo.com"
    assert normalize_domain("FOO.COM") == "foo.com"
    assert normalize_domain("http://dealer.test/staff") == "dealer.test"


def test_personal_domain_flagged():
    person = _n("Ann", "Lee", "gmail.com")
    assert person.personal_domain
    person = _n("Ann", "Lee", "https://Yahoo.COM")
    assert person.personal_domain
