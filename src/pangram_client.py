from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from src.utils import load_environment, response_summary, sanitize_pangram_response

LOGGER = logging.getLogger(__name__)
PANGRAM_ENDPOINT = "https://text.api.pangram.com/v3"
DEFAULT_FRAGMENT_MIN_WORDS = 300
DEFAULT_FRAGMENT_MAX_WORDS = 500
DEFAULT_FRAGMENT_MIN_COUNT = 2
DEFAULT_FRAGMENT_MAX_COUNT = 3


class PangramError(RuntimeError):
    pass


class PangramAuthError(PangramError):
    pass


@dataclass(frozen=True)
class TextFragment:
    index: int
    start_word: int
    word_count: int
    text: str


def analyze_text(text: str) -> dict[str, Any]:
    load_environment()
    api_key = os.getenv("PANGRAM_API_KEY")
    if not api_key:
        raise PangramAuthError("PANGRAM_API_KEY is not configured")

    timeout = float(os.getenv("PANGRAM_TIMEOUT_SECONDS", "30"))
    retries = int(os.getenv("PANGRAM_MAX_RETRIES", "3"))
    payload = {"text": text, "public_dashboard_link": False}
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    last_error: Exception | None = None
    with httpx.Client(timeout=timeout) as client:
        for attempt in range(retries + 1):
            try:
                response = client.post(PANGRAM_ENDPOINT, headers=headers, json=payload)
                if response.status_code in {401, 403}:
                    raise PangramAuthError(f"Pangram authorization failed with HTTP {response.status_code}")
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < retries:
                        sleep_seconds = _retry_delay(response, attempt)
                        LOGGER.info("Pangram retry in %.1fs after HTTP %s", sleep_seconds, response.status_code)
                        time.sleep(sleep_seconds)
                        continue
                response.raise_for_status()
                return response.json()
            except PangramAuthError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < retries:
                    sleep_seconds = min(2**attempt, 30) + random.uniform(0, 0.5)
                    LOGGER.info("Pangram retry in %.1fs after error: %s", sleep_seconds, exc)
                    time.sleep(sleep_seconds)
                    continue
                break

    raise PangramError(f"Pangram request failed: {last_error}")


def analyze_text_fragments(text: str) -> dict[str, Any]:
    """Analyze random text fragments and return metadata plus Pangram responses.

    Fragment text is intentionally not included in the returned payload, so the
    stored Pangram result does not retain scraped article text.
    """
    fragments = select_random_fragments(text)
    if not fragments:
        raise PangramError("No text fragments available for Pangram analysis")

    responses: list[dict[str, Any]] = []
    for fragment in fragments:
        response = sanitize_pangram_response(analyze_text(fragment.text))
        responses.append(
            {
                "fragment_index": fragment.index,
                "start_word": fragment.start_word,
                "word_count": fragment.word_count,
                "response": response,
            }
        )

    predictions: list[Any] = []
    numeric_scores: list[float] = []
    raw_scores: list[Any] = []
    for item in responses:
        prediction, score = response_summary(item["response"])
        if prediction is not None:
            predictions.append(prediction)
        if score is not None:
            raw_scores.append(score)
            try:
                numeric_scores.append(float(score))
            except (TypeError, ValueError):
                pass

    summary: dict[str, Any] = {
        "predictions": predictions,
        "scores": raw_scores,
    }
    if numeric_scores:
        summary["average_score"] = sum(numeric_scores) / len(numeric_scores)
        summary["max_score"] = max(numeric_scores)
        summary["min_score"] = min(numeric_scores)

    return {
        "analysis_mode": "random_fragments",
        "fragment_config": {
            "min_fragments": _env_int("PANGRAM_FRAGMENT_MIN_COUNT", DEFAULT_FRAGMENT_MIN_COUNT),
            "max_fragments": _env_int("PANGRAM_FRAGMENT_MAX_COUNT", DEFAULT_FRAGMENT_MAX_COUNT),
            "min_words": _env_int("PANGRAM_FRAGMENT_MIN_WORDS", DEFAULT_FRAGMENT_MIN_WORDS),
            "max_words": _env_int("PANGRAM_FRAGMENT_MAX_WORDS", DEFAULT_FRAGMENT_MAX_WORDS),
            "actual_fragments": len(fragments),
        },
        "fragments": responses,
        "summary": summary,
    }


def select_random_fragments(text: str, rng: random.Random | None = None) -> list[TextFragment]:
    words = _words(text)
    if not words:
        return []

    min_words = _env_int("PANGRAM_FRAGMENT_MIN_WORDS", DEFAULT_FRAGMENT_MIN_WORDS)
    max_words = _env_int("PANGRAM_FRAGMENT_MAX_WORDS", DEFAULT_FRAGMENT_MAX_WORDS)
    min_count = _env_int("PANGRAM_FRAGMENT_MIN_COUNT", DEFAULT_FRAGMENT_MIN_COUNT)
    max_count = _env_int("PANGRAM_FRAGMENT_MAX_COUNT", DEFAULT_FRAGMENT_MAX_COUNT)
    if min_words <= 0 or max_words < min_words:
        raise PangramError("Invalid Pangram fragment word bounds")
    if min_count <= 0 or max_count < min_count:
        raise PangramError("Invalid Pangram fragment count bounds")

    random_source = rng or _random_source()
    total_words = len(words)
    if total_words < min_words:
        return [TextFragment(index=1, start_word=0, word_count=total_words, text=" ".join(words))]

    fragment_count = random_source.randint(min_count, max_count)
    fragment_count = min(fragment_count, total_words)
    fragments: list[TextFragment] = []
    seen: set[tuple[int, int]] = set()
    max_attempts = max(fragment_count * 10, 20)
    attempts = 0
    while len(fragments) < fragment_count and attempts < max_attempts:
        attempts += 1
        length = random_source.randint(min_words, min(max_words, total_words))
        start = random_source.randint(0, total_words - length)
        key = (start, length)
        if key in seen:
            continue
        seen.add(key)
        fragments.append(
            TextFragment(
                index=len(fragments) + 1,
                start_word=start,
                word_count=length,
                text=" ".join(words[start : start + length]),
            )
        )

    return fragments


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return float(min(2**attempt, 30)) + random.uniform(0, 0.5)


def _words(text: str) -> list[str]:
    return re.findall(r"\S+", text or "")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise PangramError(f"{name} must be an integer") from exc


def _random_source() -> random.Random:
    seed = os.getenv("PANGRAM_FRAGMENT_RANDOM_SEED")
    if seed:
        return random.Random(seed)
    return random.SystemRandom()
