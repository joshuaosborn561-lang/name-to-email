from finder.config import Settings
from finder.normalize import normalize_person
from finder.permute import generate_candidates, infer_pattern


def _person(first: str, last: str, domain: str = "acme.test"):
    s = Settings.load()
    return normalize_person(
        first,
        last,
        domain,
        suffixes=s.suffixes,
        credentials=s.credentials,
        personal_domains=s.personal_domains,
    )


def test_pattern_order_matches_config():
    settings = Settings.load()
    person = _person("Jane", "Doe")
    cands = generate_candidates(person, settings.patterns, max_candidates=10)
    emails = [c.email for c in cands]
    assert emails == [
        "jane.doe@acme.test",
        "jdoe@acme.test",
        "jane@acme.test",
        "janedoe@acme.test",
        "j.doe@acme.test",
        "doe@acme.test",
        "jane_doe@acme.test",
        "janed@acme.test",
        "doe.jane@acme.test",
        "jd@acme.test",
    ]


def test_known_pattern_emits_only_that_address():
    settings = Settings.load()
    person = _person("Jane", "Doe")
    cands = generate_candidates(
        person, settings.patterns, known_pattern="{f}{last}", max_candidates=10
    )
    assert [c.email for c in cands] == ["jdoe@acme.test"]


def test_nickname_candidate_comes_first():
    settings = Settings.load()
    person = _person("Robert (Bob)", "Cohen")
    cands = generate_candidates(person, settings.patterns, max_candidates=3)
    assert cands[0].email == "bob.cohen@acme.test"


def test_infer_pattern():
    settings = Settings.load()
    person = _person("Jane", "Doe")
    assert infer_pattern("jdoe@acme.test", person, settings.patterns) == "{f}{last}"
    assert infer_pattern("jane.doe@acme.test", person, settings.patterns) == "{first}.{last}"
