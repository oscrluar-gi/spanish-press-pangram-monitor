import httpx
import pytest

from src import pangram_client


class FakeClient:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, headers: dict[str, str], json: dict[str, object]) -> httpx.Response:
        assert url == pangram_client.PANGRAM_ENDPOINT
        assert headers["x-api-key"] == "test-key"
        assert json["public_dashboard_link"] is False
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"prediction": "human", "score": 0.12}, request=request)


def test_analyze_text_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANGRAM_API_KEY", "test-key")
    monkeypatch.setattr(pangram_client.httpx, "Client", FakeClient)
    assert pangram_client.analyze_text("Texto de prueba") == {"prediction": "human", "score": 0.12}


def test_analyze_text_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANGRAM_API_KEY", "")
    with pytest.raises(pangram_client.PangramAuthError):
        pangram_client.analyze_text("Texto de prueba")
