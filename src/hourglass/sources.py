"""Reading authorisations from a payer API instead of a file.

The role this project was written for names AWS, Azure, Salesforce, MuleSoft
and "third-party APIs" as sources. A file drop and an HTTP endpoint are not the
same engineering problem, and the difference is not the transport -- it is that
an API can be *partially* successful. A CSV either arrives or does not. A
paginated endpoint can give you four pages, rate-limit the fifth, and return a
different total on the sixth because somebody wrote a record while you were
reading.

So this module implements the client-side behaviour that difference demands:

* **Cursor pagination**, following the server's ``next_cursor`` rather than
  incrementing an offset. Offsets shift under inserts and silently skip or
  duplicate rows; a cursor does not.
* **Retry with exponential backoff**, and only on the statuses that can
  succeed on a second attempt -- 429 and 5xx. A 400 means the request is
  wrong, and sending it again three times is just noise in somebody's logs.
* **``Retry-After`` is honoured** when the server sends it. Guessing a backoff
  when the server has told you the answer is how a client gets throttled
  harder.
* **A page budget**, so a server that returns a cursor pointing at itself
  produces an error rather than an infinite loop and a full disk.
* **Completeness is verified**, not assumed: the count of rows collected is
  checked against the ``total`` the server declared, and a mismatch is an
  error. This is the check that catches the pagination bug where page 4 comes
  back twice.

``FakePayerAPI`` is a deliberately awkward in-process server used by the tests
and by the demo: it paginates, rate-limits, and fails intermittently, so the
client's error handling is exercised by something that actually misbehaves.
No network is involved anywhere in this module.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd


class APIError(RuntimeError):
    """A request that cannot be retried into success."""


class RateLimited(RuntimeError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


@dataclass
class Page:
    rows: list[dict]
    next_cursor: str | None
    total: int


# ---------------------------------------------------------------------------
# The awkward server
# ---------------------------------------------------------------------------


@dataclass
class FakePayerAPI:
    """An in-process stand-in for a payer authorisation endpoint.

    Misbehaves on purpose and deterministically: the same call sequence
    produces the same failures every time, so a test that passes today is not
    passing by luck.
    """

    rows: list[dict]
    page_size: int = 200
    rate_limit_every: int = 4      # every Nth request returns 429
    fail_every: int = 7            # every Nth request returns 503
    retry_after: float = 0.01
    request_count: int = 0
    served_pages: list[str] = field(default_factory=list)

    def fetch(self, cursor: str | None = None) -> Page:
        self.request_count += 1

        if self.rate_limit_every and self.request_count % self.rate_limit_every == 0:
            raise RateLimited(self.retry_after)
        if self.fail_every and self.request_count % self.fail_every == 0:
            raise APIError("503 Service Unavailable")

        start = int(cursor) if cursor else 0
        end = min(start + self.page_size, len(self.rows))
        self.served_pages.append(f"{start}:{end}")
        return Page(
            rows=self.rows[start:end],
            next_cursor=str(end) if end < len(self.rows) else None,
            total=len(self.rows),
        )


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


@dataclass
class APIClientStats:
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    pages: int = 0
    rows: int = 0
    wait_seconds: float = 0.0


def fetch_all(
    fetch: Callable[[str | None], Page],
    max_retries: int = 4,
    backoff_seconds: float = 0.05,
    max_pages: int = 10_000,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict], APIClientStats]:
    """Follow the cursor to the end, retrying what is worth retrying.

    ``sleep`` is injected so tests exercise the backoff path without spending
    real seconds on it.
    """
    stats = APIClientStats()
    rows: list[dict] = []
    cursor: str | None = None
    declared_total: int | None = None
    seen_cursors: set[str] = set()

    for _ in range(max_pages):
        page = None
        for attempt in range(1, max_retries + 2):
            try:
                stats.requests += 1
                page = fetch(cursor)
                break
            except RateLimited as exc:
                stats.rate_limited += 1
                if attempt > max_retries:
                    raise APIError(
                        f"still rate limited after {max_retries} retries") from exc
                stats.retries += 1
                # The server said how long to wait. Believe it.
                stats.wait_seconds += exc.retry_after
                sleep(exc.retry_after)
            except APIError:
                if attempt > max_retries:
                    raise
                stats.retries += 1
                wait = backoff_seconds * (2 ** (attempt - 1))
                stats.wait_seconds += wait
                sleep(wait)

        assert page is not None
        rows.extend(page.rows)
        stats.pages += 1
        declared_total = page.total

        if page.next_cursor is None:
            break
        if page.next_cursor in seen_cursors:
            # A cursor that repeats means the server is looping. Without this
            # the client happily fetches the same page until it runs out of
            # memory, and the symptom looks like a hang rather than a bug.
            raise APIError(f"cursor {page.next_cursor!r} repeated; server is looping")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
    else:
        raise APIError(f"exceeded {max_pages} pages; refusing to keep going")

    stats.rows = len(rows)

    # Completeness is checked, not assumed. Pagination bugs do not raise --
    # they return a plausible number of rows, and the only thing that catches
    # them is comparing what arrived against what the server said existed.
    if declared_total is not None and len(rows) != declared_total:
        raise APIError(
            f"incomplete fetch: collected {len(rows):,} rows but the API "
            f"declared {declared_total:,}. Do not use this extract."
        )

    return rows, stats


def fetch_authorizations(
    api: FakePayerAPI,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[pd.DataFrame, APIClientStats]:
    """Pull the full authorisation set and hand back a frame."""
    rows, stats = fetch_all(api.fetch, sleep=sleep)
    return pd.DataFrame(rows), stats


def api_from_csv(path, page_size: int = 200, **kwargs) -> FakePayerAPI:
    """Serve an existing extract through the API shape.

    Lets the pipeline exercise the API code path against the same data the
    file path uses, so the two are comparable and the switch is a one-line
    change rather than a rewrite.
    """
    df = pd.read_csv(path)
    return FakePayerAPI(rows=df.to_dict("records"), page_size=page_size, **kwargs)
