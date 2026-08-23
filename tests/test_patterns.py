from finder.config import Settings
from finder.normalize import normalize_person
from finder.patterns import agreeing_pattern


def _person(first: str, last: str, domain: str = "acme.test"):
    settings = Settings.load()
    return normalize_person(
        first,
        last,
        domain,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )


def test_two_colleagues_agree_on_first_dot_last():
    settings = Settings.load()
    pairs = [
        (_person("Alice", "Anderson"), "alice.anderson@acme.test"),
        (_person("Brian", "Baker"), "brian.baker@acme.test"),
    ]
    assert agreeing_pattern(pairs, settings.patterns, min_agree=2) == "{first}.{last}"


def test_one_known_person_is_not_enough():
    settings = Settings.load()
    pairs = [(_person("Alice", "Anderson"), "alice.anderson@acme.test")]
    assert agreeing_pattern(pairs, settings.patterns, min_agree=2) is None


def test_same_person_twice_counts_once():
    settings = Settings.load()
    alice = _person("Alice", "Anderson")
    pairs = [
        (alice, "alice.anderson@acme.test"),
        (alice, "alice.anderson@acme.test"),
        (_person("Alice", "Anderson"), "aanderson@acme.test"),
    ]
    assert agreeing_pattern(pairs, settings.patterns, min_agree=2) is None


def test_split_vote_does_not_deduce():
    settings = Settings.load()
    pairs = [
        (_person("Alice", "Anderson"), "alice.anderson@acme.test"),
        (_person("Brian", "Baker"), "bbaker@acme.test"),
    ]
    assert agreeing_pattern(pairs, settings.patterns, min_agree=2) is None


def test_majority_wins_when_three_known():
    settings = Settings.load()
    pairs = [
        (_person("Alice", "Anderson"), "alice.anderson@acme.test"),
        (_person("Brian", "Baker"), "brian.baker@acme.test"),
        (_person("Cara", "Cole"), "ccole@acme.test"),
    ]
    assert agreeing_pattern(pairs, settings.patterns, min_agree=2) == "{first}.{last}"
