from src.main import _article_row_in_time_window, _limit_rows_per_media
from src.utils import parse_time_window


def test_limit_rows_per_media_samples_up_to_limit(monkeypatch):
    rows = [
        {"media_name": "A", "url": f"https://a.example/{index}"}
        for index in range(5)
    ] + [
        {"media_name": "B", "url": f"https://b.example/{index}"}
        for index in range(2)
    ]
    monkeypatch.setenv("PANGRAM_ARTICLE_SAMPLE_SEED", "123")

    selected = _limit_rows_per_media(rows, 3)

    assert len([row for row in selected if row["media_name"] == "A"]) == 3
    assert len([row for row in selected if row["media_name"] == "B"]) == 2


def test_limit_rows_per_media_zero_means_unlimited():
    rows = [{"media_name": "A", "url": f"https://a.example/{index}"} for index in range(5)]

    assert _limit_rows_per_media(rows, 0) == rows


def test_article_row_in_time_window_uses_article_published_at():
    window = parse_time_window("2026-05-01", start_time="05:00", hours=4)

    assert _article_row_in_time_window(
        {"article_published_at": "2026-05-01T06:15:00+02:00", "published_at": None},
        window,
    )
    assert not _article_row_in_time_window(
        {"article_published_at": "2026-05-01T12:00:00+02:00", "published_at": None},
        window,
    )


def test_article_row_outside_when_time_window_has_no_article_datetime():
    window = parse_time_window("2026-05-01", start_time="05:00", hours=4)

    assert not _article_row_in_time_window({"article_published_at": None, "published_at": None}, window)
