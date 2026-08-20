"""The payer API client.

An API can be partially successful in a way a file cannot, so these tests are
mostly about the failure modes: rate limits, transient errors, a server that
loops, and a fetch that quietly comes back short.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hourglass import sources
from hourglass.sources import APIError, FakePayerAPI, Page, RateLimited, fetch_all

NO_SLEEP = lambda _: None


def rows(n: int) -> list[dict]:
    return [{"auth_id": f"AUTH-{i:05d}", "units_authorized": float(i)} for i in range(n)]


class TestPagination:
    def test_collects_every_row_in_order(self):
        api = FakePayerAPI(rows=rows(1000), page_size=200)
        got, _ = fetch_all(api.fetch, sleep=NO_SLEEP)
        assert len(got) == 1000
        assert [r["auth_id"] for r in got] == [r["auth_id"] for r in rows(1000)]

    def test_a_partial_final_page_is_handled(self):
        api = FakePayerAPI(rows=rows(250), page_size=200)
        got, stats = fetch_all(api.fetch, sleep=NO_SLEEP)
        assert len(got) == 250
        assert stats.pages == 2

    def test_an_empty_result_set_is_not_an_error(self):
        api = FakePayerAPI(rows=[], page_size=200, rate_limit_every=0, fail_every=0)
        got, stats = fetch_all(api.fetch, sleep=NO_SLEEP)
        assert got == []
        assert stats.pages == 1

    def test_a_single_page_needs_one_request(self):
        api = FakePayerAPI(rows=rows(10), page_size=200,
                           rate_limit_every=0, fail_every=0)
        _, stats = fetch_all(api.fetch, sleep=NO_SLEEP)
        assert stats.requests == 1

    def test_cursors_do_not_skip_or_repeat(self):
        api = FakePayerAPI(rows=rows(1000), page_size=200)
        fetch_all(api.fetch, sleep=NO_SLEEP)
        assert api.served_pages == ["0:200", "200:400", "400:600", "600:800", "800:1000"]


class TestRetries:
    def test_recovers_from_rate_limiting(self):
        api = FakePayerAPI(rows=rows(600), page_size=200,
                           rate_limit_every=2, fail_every=0)
        got, stats = fetch_all(api.fetch, sleep=NO_SLEEP)
        assert len(got) == 600
        assert stats.rate_limited > 0

    def test_recovers_from_transient_server_errors(self):
        api = FakePayerAPI(rows=rows(600), page_size=200,
                           rate_limit_every=0, fail_every=2)
        got, stats = fetch_all(api.fetch, sleep=NO_SLEEP)
        assert len(got) == 600
        assert stats.retries > 0

    def test_honours_retry_after(self):
        """When the server says how long to wait, guessing is worse."""
        waits = []
        api = FakePayerAPI(rows=rows(200), page_size=200, rate_limit_every=1,
                           fail_every=0, retry_after=2.5)
        api.rate_limit_every = 0        # let the first call through after one 429

        calls = {"n": 0}

        def flaky(cursor):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RateLimited(2.5)
            return Page(rows=rows(3), next_cursor=None, total=3)

        fetch_all(flaky, sleep=waits.append)
        assert waits == [2.5]

    def test_gives_up_after_the_retry_limit(self):
        def always_limited(cursor):
            raise RateLimited(0.01)

        with pytest.raises(APIError, match="still rate limited"):
            fetch_all(always_limited, max_retries=2, sleep=NO_SLEEP)

    def test_backoff_grows(self):
        waits = []

        def always_broken(cursor):
            raise APIError("503")

        with pytest.raises(APIError):
            fetch_all(always_broken, max_retries=3, backoff_seconds=1.0,
                      sleep=waits.append)
        assert waits == [1.0, 2.0, 4.0]


class TestCorrectnessGuards:
    def test_a_looping_server_raises_instead_of_hanging(self):
        """Otherwise the symptom is a hang, not a bug report."""
        def looping(cursor=None):
            return Page(rows=[{"auth_id": "A"}], next_cursor="0", total=99)

        with pytest.raises(APIError, match="looping"):
            fetch_all(looping, sleep=NO_SLEEP)

    def test_an_incomplete_fetch_raises(self):
        """The check that catches a pagination bug.

        A short fetch returns a plausible number of rows and no error. Only
        comparing what arrived against the total the server declared catches
        it -- and a silently-short authorisation extract makes every
        utilisation figure downstream too high.
        """
        def short(cursor=None):
            return Page(rows=[{"auth_id": "A"}], next_cursor=None, total=500)

        with pytest.raises(APIError, match="incomplete fetch"):
            fetch_all(short, sleep=NO_SLEEP)

    def test_a_complete_fetch_passes_the_total_check(self):
        def exact(cursor=None):
            return Page(rows=[{"auth_id": "A"}, {"auth_id": "B"}],
                        next_cursor=None, total=2)
        got, _ = fetch_all(exact, sleep=NO_SLEEP)
        assert len(got) == 2

    def test_the_page_budget_is_enforced(self):
        def endless(cursor=None):
            nxt = str(int(cursor or 0) + 1)
            return Page(rows=[{"auth_id": nxt}], next_cursor=nxt, total=10 ** 9)

        with pytest.raises(APIError, match="exceeded"):
            fetch_all(endless, max_pages=5, sleep=NO_SLEEP)


class TestDeterminism:
    def test_the_same_call_sequence_fails_the_same_way(self):
        """A flaky test suite teaches people to re-run rather than investigate."""
        a = FakePayerAPI(rows=rows(1000), page_size=200)
        b = FakePayerAPI(rows=rows(1000), page_size=200)
        _, sa = fetch_all(a.fetch, sleep=NO_SLEEP)
        _, sb = fetch_all(b.fetch, sleep=NO_SLEEP)
        assert (sa.requests, sa.retries, sa.rate_limited) == (
            sb.requests, sb.retries, sb.rate_limited)


class TestFrameOutput:
    def test_returns_a_dataframe(self):
        api = FakePayerAPI(rows=rows(300), page_size=100)
        df, stats = sources.fetch_authorizations(api, sleep=NO_SLEEP)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 300
        assert stats.rows == 300

    def test_serves_a_real_extract_through_the_api_shape(self, tmp_path):
        path = tmp_path / "auths.csv"
        pd.DataFrame(rows(450)).to_csv(path, index=False)
        api = sources.api_from_csv(path, page_size=100)
        df, _ = sources.fetch_authorizations(api, sleep=NO_SLEEP)
        assert len(df) == 450
