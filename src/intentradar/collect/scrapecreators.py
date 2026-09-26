"""ScrapeCreators provider — migrated from ``scripts/monitor.py``.

Two deliberate changes versus the original script:

1. **The hardcoded proxy is gone.** ``httpx`` is created with the default
   ``trust_env=True``, so the standard ``HTTPS_PROXY`` / ``HTTP_PROXY`` /
   ``ALL_PROXY`` environment variables are honoured. No proxy configured means a
   direct connection. Never reintroduce a literal proxy address here —
   ``tests/test_collect.py`` scans the source and fails the build if one appears.
2. **Credits are reported** instead of being stashed on a function attribute.
"""

from __future__ import annotations

import logging
import time

import httpx

from intentradar.collect import SourceResult
from intentradar.errors import MissingCredential, ProviderError
from intentradar.models import Post

log = logging.getLogger(__name__)

SC_BASE = "https://api.scrapecreators.com"
DEFAULT_TIMEOUT_S = 40.0
DEFAULT_MAX_RETRIES = 2
PAGE_SLEEP_S = 1.0  # politeness delay between pages (kept from monitor.py)


class ScrapeCreatorsProvider:
    """Fetch subreddit posts through the ScrapeCreators Reddit endpoint."""

    name: str = "scrapecreators"

    def __init__(
        self,
        api_key: str,
        base_url: str = SC_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        page_sleep_s: float = PAGE_SLEEP_S,
        client: httpx.Client | None = None,
    ) -> None:
        """Initialise.

        Args:
            api_key: ScrapeCreators API key (``x-api-key`` header).
            base_url: API base URL.
            timeout_s: Per-request timeout.
            max_retries: Retries for transient errors (5xx / transport).
            page_sleep_s: Delay between pages.
            client: Optional pre-built httpx client (tests inject a mock
                transport here; production lets httpx read the env proxy).
        """
        if not api_key:
            raise MissingCredential("SCRAPECREATORS_API_KEY")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.page_sleep_s = page_sleep_s
        self._client = client

    # ── internals ──────────────────────────────────────────────────────────
    def _request(self, params: dict[str, str]) -> dict:
        """Perform one GET, retrying transient failures. Raises ProviderError."""

        url = f"{self.base_url}/v1/reddit/subreddit"
        headers = {"x-api-key": self.api_key, "Accept": "application/json"}
        last_error = ""

        for attempt in range(self.max_retries + 1):
            try:
                if self._client is not None:
                    response = self._client.get(url, params=params, headers=headers)
                else:
                    with httpx.Client(timeout=self.timeout_s) as client:
                        response = client.get(url, params=params, headers=headers)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self.max_retries:
                    break
                time.sleep(1.5 * (attempt + 1))
                continue

            code = response.status_code
            sub_label = params.get("subreddit", "?")
            if 400 <= code < 500:
                # 4xx is a client error (bad key / bad params): do not retry.
                raise ProviderError(
                    self.name,
                    f"HTTP {code} for r/{sub_label}: {response.text[:200]}",
                )
            if code >= 400:
                # 5xx is transient — fall through to the backoff below.
                last_error = f"HTTP {code} for r/{sub_label}: {response.text[:200]}"
                if attempt >= self.max_retries:
                    break
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                return response.json()
            except ValueError as exc:
                last_error = f"invalid JSON for r/{sub_label}: {exc}"
                if attempt >= self.max_retries:
                    break
                time.sleep(1.5 * (attempt + 1))

        raise ProviderError(
            self.name,
            f"r/{params.get('subreddit', '?')} failed after "
            f"{self.max_retries + 1} attempt(s): {last_error}",
        )

    def _fetch_page(
        self, sub: str, cache: str | None, after: str | None
    ) -> tuple[list[Post], int, int | None, str | None]:
        """Fetch one page.

        Returns:
            ``(posts, credits_charged, credits_remaining, next_cursor)``. The
            cursor is the Reddit fullname (``t3_...``) when the API provides it,
            falling back to the bare id — exactly how ``monitor.py`` paginated.
        """
        # Only sort=new is supported; timeframe requires sort=top (else HTTP 400).
        params: dict[str, str] = {"subreddit": sub, "sort": "new", "trim": "true"}
        if cache:
            params["cache_max_age"] = cache
        if after:
            params["after"] = after

        data = self._request(params)
        raw_posts = [r for r in (data.get("posts") or []) if isinstance(r, dict)]
        posts = [Post.from_raw(raw, sub=sub) for raw in raw_posts]
        charged = int(data.get("credits_charged", 0) or 0)
        remaining = data.get("credits_remaining")
        remaining = None if remaining is None else int(remaining)
        next_cursor = None
        if raw_posts:
            last = raw_posts[-1]
            next_cursor = str(last.get("name") or last.get("id") or "") or None
        return posts, charged, remaining, next_cursor

    # ── public API ─────────────────────────────────────────────────────────
    def fetch_subreddit(
        self, sub: str, pages: int = 2, cache: str | None = "1d"
    ) -> SourceResult:
        """Fetch up to ``pages`` pages of new posts for one subreddit."""
        collected: list[Post] = []
        after: str | None = None
        charged_total = 0
        remaining: int | None = None

        for page_index in range(max(1, int(pages))):
            posts, charged, page_remaining, next_cursor = self._fetch_page(sub, cache, after)
            charged_total += charged
            if page_remaining is not None:
                remaining = page_remaining
            if not posts:
                break
            collected.extend(posts)
            after = next_cursor
            if page_index + 1 < pages and self.page_sleep_s:
                time.sleep(self.page_sleep_s)

        log.debug(
            "r/%s: %d posts, %d credits charged, remaining=%s", sub, len(collected), charged_total, remaining
        )
        return SourceResult(posts=collected, credits_charged=charged_total, credits_remaining=remaining)
