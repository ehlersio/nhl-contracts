"""A small polite HTTP client: one User-Agent, redirects, timeouts and retries."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import USER_AGENT

log = logging.getLogger(__name__)


class Client:
    def __init__(self, *, retries: int = 3, backoff: float = 1.5, pause: float = 0.1) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
        )
        self.retries = retries
        self.backoff = backoff
        self.pause = pause

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{resp.status_code} from {url}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                if self.pause:
                    time.sleep(self.pause)
                return resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                wait = self.backoff**attempt
                log.warning("GET %s failed (%s); retrying in %.1fs", url, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"GET {url} failed after {self.retries} attempts") from last

    def get_bytes(self, url: str, params: dict[str, Any] | None = None, *, ok_statuses: tuple[int, ...] = (200,)
                  ) -> tuple[int, bytes, str]:
        """Return (status_code, body, final_url). Retries on 5xx/429/transport errors; other
        statuses are returned as-is so callers can treat 404 as 'missing'."""
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"{resp.status_code} from {url}", request=resp.request, response=resp
                    )
                if self.pause:
                    time.sleep(self.pause)
                return resp.status_code, resp.content, str(resp.url)
            except httpx.HTTPError as exc:
                last = exc
                wait = self.backoff ** (attempt + 1)
                log.warning("GET %s failed (%s); retrying in %.1fs", url, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"GET {url} failed after {self.retries} attempts") from last

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
