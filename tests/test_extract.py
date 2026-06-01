import json

import src.extract as extract_module
from src.extract import extract_article_from_html, extract_metadata, looks_like_paywall


def test_extract_simple_html() -> None:
    paragraph = " ".join([f"palabra{i}" for i in range(180)])
    html = f"""
    <html>
      <head>
        <title>Titulo de prueba</title>
        <meta property="article:published_time" content="2026-05-31T10:00:00+02:00" />
      </head>
      <body>
        <article>
          <h1>Titulo de prueba</h1>
          <p>{paragraph}</p>
        </article>
      </body>
    </html>
    """
    article = extract_article_from_html("https://example.com/2026/05/31/test", html, "2026-05-31")
    assert article.extraction_status == "ok_live"
    assert article.word_count >= 150
    assert article.text_hash is not None


def test_extract_marks_articles_outside_target_date() -> None:
    paragraph = " ".join([f"palabra{i}" for i in range(180)])
    html = f"""
    <html>
      <head>
        <meta property="article:published_time" content="2026-05-02T10:00:00+02:00" />
      </head>
      <body><article><h1>Titulo</h1><p>{paragraph}</p></article></body>
    </html>
    """
    article = extract_article_from_html("https://example.com/story", html, "2026-05-01", min_words=150)
    assert article.extraction_status == "out_of_target_date"


def test_paywall_detection() -> None:
    assert looks_like_paywall("<p>Contenido exclusivo para suscriptores</p>")


def test_subscribe_button_in_header_does_not_mark_long_article_paywalled() -> None:
    paragraph = " ".join([f"palabra{i}" for i in range(220)])
    html = f"""
    <html>
      <body>
        <header><button>Suscríbete</button></header>
        <article><h1>Titulo</h1><p>{paragraph}</p></article>
      </body>
    </html>
    """
    article = extract_article_from_html("https://example.com/2026/05/31/test", html, "2026-05-31")
    assert article.extraction_status == "ok_live"
    assert article.is_paywalled is False


def test_extract_metadata_prefers_json_ld() -> None:
    html = """
    <html lang="es">
      <head>
        <link rel="canonical" href="https://example.com/noticia" />
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "NewsArticle",
          "headline": "Titular JSON-LD",
          "author": {"@type": "Person", "name": "Ana Perez"},
          "datePublished": "2026-05-31T09:00:00+02:00",
          "dateModified": "2026-05-31T10:00:00+02:00",
          "articleSection": "Espana",
          "keywords": ["politica", "congreso"],
          "isAccessibleForFree": false,
          "inLanguage": "es"
        }
        </script>
      </head>
      <body><time datetime="2026-05-30T10:00:00+02:00"></time></body>
    </html>
    """
    metadata = extract_metadata("https://example.com/noticia", html)
    assert metadata["title"] == "Titular JSON-LD"
    assert metadata["author"] == "Ana Perez"
    assert metadata["article_published_at"] == "2026-05-31T09:00:00+02:00"
    assert metadata["article_modified_at"] == "2026-05-31T10:00:00+02:00"
    assert metadata["section"] == "Espana"
    assert metadata["tags"] == ["politica", "congreso"]
    assert metadata["is_paywalled"] is True


def test_long_paywalled_article_is_not_marked_incomplete() -> None:
    paragraph = " ".join([f"palabra{i}" for i in range(360)])
    html = f"""
    <html>
      <head>
        <script type="application/ld+json">
        {{
          "@context": "https://schema.org",
          "@type": "NewsArticle",
          "headline": "Titular",
          "isAccessibleForFree": false
        }}
        </script>
      </head>
      <body><article><p>{paragraph}</p></article></body>
    </html>
    """
    article = extract_article_from_html("https://example.com/2026/05/01/test", html, "2026-05-01")
    assert article.extraction_status == "ok_live"
    assert article.is_paywalled is True


def test_extract_marks_too_short() -> None:
    html = "<html><body><article><h1>Titulo</h1><p>Texto breve de articulo.</p></article></body></html>"
    article = extract_article_from_html("https://example.com/2026/05/31/test", html, "2026-05-31", min_words=150)
    assert article.extraction_status in {"too_short", "no_text"}


def test_extract_retries_with_recall_when_precision_is_too_short(monkeypatch) -> None:
    long_text = " ".join([f"palabra{i}" for i in range(180)])

    def fake_extract(*args, **kwargs):
        text = "breve" if kwargs["favor_precision"] else long_text
        return json.dumps({"text": text, "title": "Titulo"})

    monkeypatch.setattr(extract_module.trafilatura, "extract", fake_extract)
    article = extract_article_from_html("https://example.com/2026/05/31/test", "<html></html>", "2026-05-31")
    assert article.extraction_status == "ok_live"
    assert article.word_count == 180
