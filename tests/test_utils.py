from src.utils import build_request_headers, count_words, normalize_search_text, normalize_url, parse_time_window, text_sha256


def test_normalize_url_removes_tracking_and_fragment() -> None:
    assert (
        normalize_url("HTTPS://www.Example.com/news/story/?utm_source=x&cmp=foo&ref=home&id=42#comments")
        == "https://example.com/news/story?id=42"
    )


def test_normalize_url_keeps_only_safe_allowlisted_params() -> None:
    assert normalize_url("https://elpais.com/story?outputType=amp&page=2&foo=bar") == "https://elpais.com/story?page=2"


def test_text_hash_uses_normalized_whitespace() -> None:
    assert text_sha256("hola   mundo") == text_sha256("hola mundo")


def test_count_words_handles_spanish_accents() -> None:
    assert count_words("Espa\u00f1a publica art\u00edculos con informaci\u00f3n \u00fatil.") == 6


def test_normalize_search_text_is_accent_insensitive() -> None:
    assert normalize_search_text("Pedro S\u00e1nchez preside La Moncloa") == "pedro sanchez preside la moncloa"
