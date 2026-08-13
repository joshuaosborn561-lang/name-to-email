from finder.config import Settings
from finder.ingest import detect_columns, ingest_csv_text


def test_auto_detect_website_and_full_name():
    settings = Settings.load()
    csv = """full_name,website,city
Jane Doe,https://www.acme.test/about,Austin
John Smith,acme.test,Austin
Jane Doe,https://www.acme.test,Austin
"""
    result = ingest_csv_text(
        csv,
        aliases=settings.column_aliases,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )
    assert result.raw_count == 3
    assert result.duplicates_dropped == 1
    assert len(result.rows) == 2
    assert result.rows[0].normalized.domain == "acme.test"
    assert result.rows[0].normalized.primary_first == "jane"
    assert "city" in result.rows[0].passthrough


def test_detect_common_headers():
    settings = Settings.load()
    mapping = detect_columns(
        ["First Name", "Last Name", "URL"],
        settings.column_aliases,
    )
    assert mapping["first"] == "First Name"
    assert mapping["last"] == "Last Name"
    assert mapping["domain"] == "URL"


def test_gmail_not_permuted_flag():
    settings = Settings.load()
    csv = "first,last,domain\nAnn,Lee,gmail.com\n"
    result = ingest_csv_text(
        csv,
        aliases=settings.column_aliases,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )
    assert result.rows[0].normalized.personal_domain


def test_mixed_json_keys_website_fallback():
    settings = Settings.load()
    from finder.ingest import ingest_records

    result = ingest_records(
        [
            {"first": "Jane", "last": "Doe", "domain": "acme.test"},
            {"first": "Oliver", "last": "C.", "website": "acme.test"},
        ],
        aliases=settings.column_aliases,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )
    assert len(result.rows) == 2
    assert result.rows[1].normalized.insufficient_name
    assert result.rows[1].normalized.domain == "acme.test"
