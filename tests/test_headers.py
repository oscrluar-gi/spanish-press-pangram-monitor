from src.utils import build_request_headers


def test_build_request_headers_wraps_project_user_agent() -> None:
    headers = build_request_headers("SpanishPressPangramMonitor/0.1 (+local research)")
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert "SpanishPressPangramMonitor/0.1" in headers["User-Agent"]
    assert headers["Accept-Language"].startswith("es-ES")
