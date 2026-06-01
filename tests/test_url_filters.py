from src.models import MediaConfig
from src.url_filters import should_include_article_url


def test_url_filter_excludes_tags_video_gallery_newsletter_podcast_and_sections() -> None:
    media = MediaConfig(name="Medio", domain="example.com")
    excluded = [
        "https://example.com/tags/politica",
        "https://example.com/videos/noticia.html",
        "https://example.com/galeria/fotos.html",
        "https://example.com/newsletter/diaria",
        "https://example.com/podcast/episodio",
        "https://example.com/economia/",
    ]
    assert all(not should_include_article_url(url, media).included for url in excluded)


def test_url_filter_includes_normal_articles_and_opinion_by_default() -> None:
    media = MediaConfig(name="Medio", domain="example.com")
    assert should_include_article_url("https://example.com/espana/2026/05/31/noticia.html", media).included
    assert should_include_article_url("https://example.com/opinion/2026/05/31/columna.html", media).included


def test_url_filter_respects_liveblog_opinion_sports_flags() -> None:
    media = MediaConfig(name="Medio", domain="example.com", include_opinion=False, include_sports=False)
    assert should_include_article_url("https://example.com/directo/2026/05/31/ultima-hora.html", media).reason == "liveblog"
    assert should_include_article_url("https://example.com/opinion/2026/05/31/columna.html", media).reason == "opinion"
    assert should_include_article_url("https://example.com/deportes/2026/05/31/partido.html", media).reason == "sports"
