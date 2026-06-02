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


def test_select_random_fragments_uses_expected_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PANGRAM_FRAGMENT_RANDOM_SEED", raising=False)
    text = " ".join(f"palabra{i}" for i in range(1200))
    fragments = pangram_client.select_random_fragments(text)

    assert 2 <= len(fragments) <= 3
    assert all(300 <= fragment.word_count <= 500 for fragment in fragments)
    assert all(fragment.text for fragment in fragments)


def test_select_random_fragments_short_text_sends_single_short_fragment() -> None:
    text = " ".join(f"breve{i}" for i in range(220))
    fragments = pangram_client.select_random_fragments(text)

    assert len(fragments) == 1
    assert fragments[0].word_count == 220


def test_analyze_text_fragments_does_not_return_fragment_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_analyze_text(text: str) -> dict[str, object]:
        calls.append(text)
        return {
            "prediction": "human",
            "score": 0.1,
            "segments": [{"text": text, "label": "human"}],
        }

    monkeypatch.setattr(pangram_client, "analyze_text", fake_analyze_text)
    monkeypatch.setenv("PANGRAM_FRAGMENT_RANDOM_SEED", "123")
    text = " ".join(f"palabra{i}" for i in range(1000))

    response = pangram_client.analyze_text_fragments(text)

    assert 2 <= len(calls) <= 3
    assert response["analysis_mode"] == "random_fragments"
    for fragment in response["fragments"]:
        assert "text" not in fragment
        assert 300 <= fragment["word_count"] <= 500
        assert fragment["response"]["prediction"] == "human"
        assert fragment["response"]["segments"][0]["text"] == "[redacted]"
