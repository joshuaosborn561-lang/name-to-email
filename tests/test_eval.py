from pathlib import Path

from finder.config import Settings
from finder.eval import run_eval
from finder.verifiers.mock import MockVerifier


def _write_corpus(path: Path, n_per_domain: int = 20) -> dict[str, set[str]]:
    """Synthetic known-good corpus with consistent per-domain patterns."""
    firsts = [
        "Amy", "Brian", "Cara", "Derek", "Elena", "Frank", "Gina", "Hugo",
        "Iris", "James", "Kara", "Leo", "Mona", "Nate", "Olive", "Paul",
        "Quinn", "Rita", "Sam", "Tina", "Uma", "Vince", "Wendy", "Xander",
        "Yara", "Zane",
    ]
    lasts = [
        "Adams", "Baker", "Cole", "Dunn", "Ellis", "Foster", "Green", "Hayes",
        "Ingram", "Jones", "Klein", "Lang", "Moore", "Nash", "Owen", "Perez",
        "Quinn", "Reed", "Stone", "Turner", "Underwood", "Vale", "West", "Young",
        "Zimmer", "Abbott",
    ]
    specs = [
        ("firstdot.test", "{first}.{last}", False),
        ("flast.test", "{f}{last}", False),
        ("firstonly.test", "{first}", False),
        ("catchalldeal.test", "{first}.{last}", True),
        ("concat.test", "{first}{last}", False),
    ]
    lines = ["first,last,email"]
    valid: set[str] = set()
    catchall: set[str] = set()
    for domain, template, is_ca in specs:
        for i in range(n_per_domain):
            first, last = firsts[i], lasts[i]
            f, l = first[0].lower(), last[0].lower()
            local = (
                template.replace("{first}", first.lower())
                .replace("{last}", last.lower())
                .replace("{f}", f)
                .replace("{l}", l)
            )
            email = f"{local}@{domain}"
            lines.append(f"{first},{last},{email}")
            if is_ca:
                catchall.add(domain)
            else:
                valid.add(email)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"valid": valid, "catchall": catchall}


async def test_holdout_eval_beats_vendor_on_non_catchall(tmp_path, db, settings: Settings):
    factory = db
    csv_path = tmp_path / "known.csv"
    corpus = _write_corpus(csv_path, n_per_domain=20)
    verifier = MockVerifier(valid=corpus["valid"], catchall_domains=corpus["catchall"])
    report = await run_eval(
        factory,
        settings,
        verifier,
        csv_path,
        holdout_size=50,
        seed=42,
        max_cost=25,
    )
    assert report.holdout == 50
    assert report.catchall_domain_share > 0
    # Non-catchall holdout rows should recover via seed + permutation.
    assert report.recovery_rate >= 0.6
    assert report.avg_attempts_per_hit >= 0
    assert report.cost_per_valid is not None
    assert report.cost_per_valid < settings.vendor_cost_per_email
    assert report.beats_vendor is True
