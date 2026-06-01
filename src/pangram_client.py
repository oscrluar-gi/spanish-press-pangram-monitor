from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

import httpx

from src.utils import load_environment

LOGGER = logging.getLogger(__name__)
PANGRAM_ENDPOINT = "https://text.api.pangram.com/v3"


class PangramError(RuntimeError):
    pass


class PangramAuthError(PangramError):
    pass


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


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), 60.0)
        except ValueError:
            pass
    return float(min(2**attempt, 30)) + random.uniform(0, 0.5)
