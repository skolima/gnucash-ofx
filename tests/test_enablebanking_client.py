"""Offline tests for the Enable Banking HTTP client (httpx MockTransport)."""

from __future__ import annotations

import json

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gnucash_ofx.sources.enablebanking import (
    _MAX_PAGES_PER_ACCOUNT,
    _MAX_RESPONSE_BYTES,
    EnableBankingClient,
    PageBudget,
    ResponseLimitExceeded,
    _operation_of_path,
    _value_shape,
    api_error_code,
    api_error_name,
    finite_decimal,
)


@pytest.fixture(scope="module")
def private_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _client(private_key_pem: bytes, handler) -> EnableBankingClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url="https://api.enablebanking.com", transport=transport)
    return EnableBankingClient(application_id="app-123", private_key=private_key_pem, http=http)


def test_jwt_has_expected_header_and_claims(private_key_pem: bytes) -> None:
    client = _client(private_key_pem, lambda r: httpx.Response(404))
    token = client._jwt()
    header = pyjwt.get_unverified_header(token)
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert header["kid"] == "app-123"
    assert header["alg"] == "RS256"
    assert claims["iss"] == "enablebanking.com"
    assert claims["aud"] == "api.enablebanking.com"
    assert claims["exp"] > claims["iat"]


def test_list_aspsps_passes_country_and_unwraps(private_key_pem: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/aspsps"
        assert request.url.params.get("country") == "PL"
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"aspsps": [{"name": "Alior Bank", "country": "PL"}]})

    client = _client(private_key_pem, handler)
    aspsps = client.list_aspsps(country="PL")
    assert aspsps == [{"name": "Alior Bank", "country": "PL"}]


def test_start_authorization_posts_body_and_returns_url(private_key_pem: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth"
        body = json.loads(request.content)
        assert body["aspsp"] == {"name": "Alior Bank", "country": "PL"}
        assert body["redirect_url"] == "https://cb.example/return"
        assert body["psu_type"] == "personal"
        assert body["access"]["valid_until"]
        return httpx.Response(200, json={"url": "https://bank.example/sca?x=1"})

    client = _client(private_key_pem, handler)
    url = client.start_authorization(
        aspsp_name="Alior Bank",
        country="PL",
        redirect_url="https://cb.example/return",
        valid_until="2026-09-01T00:00:00+00:00",
    )
    assert url == "https://bank.example/sca?x=1"


def test_create_session(private_key_pem: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sessions"
        assert json.loads(request.content) == {"code": "auth-code-1"}
        return httpx.Response(
            200, json={"session_id": "sess-1", "accounts": [{"uid": "acc-1", "currency": "PLN"}]}
        )

    client = _client(private_key_pem, handler)
    session = client.create_session("auth-code-1")
    assert session["session_id"] == "sess-1"
    assert session["accounts"][0]["uid"] == "acc-1"


def test_iter_transactions_follows_continuation_key(private_key_pem: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/acc-1/transactions"
        assert request.url.params.get("date_from") == "2026-05-01"
        assert request.url.params.get("date_to") == "2026-05-31"
        if request.url.params.get("continuation_key") is None:
            return httpx.Response(
                200, json={"transactions": [{"transaction_id": "a"}], "continuation_key": "p2"}
            )
        assert request.url.params.get("continuation_key") == "p2"
        return httpx.Response(
            200, json={"transactions": [{"transaction_id": "b"}], "continuation_key": None}
        )

    client = _client(private_key_pem, handler)
    raw = list(client.iter_transactions("acc-1", date_from="2026-05-01", date_to="2026-05-31"))
    assert [t["transaction_id"] for t in raw] == ["a", "b"]


def test_retries_on_429_then_succeeds(private_key_pem: bytes) -> None:
    # AGENTS.md: "Other 429s keep the ladder." (ASPSP_RATE_LIMIT_EXCEEDED is the one that does not.)
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # A transient 429: the daily cap is the one code that is never retried, see below.
            return httpx.Response(429, json={"error": "TOO_MANY_REQUESTS"})
        return httpx.Response(200, json={"aspsps": []})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123", private_key=private_key_pem, http=http, sleep=slept.append
    )
    client.list_aspsps(country="PL")
    assert calls["n"] == 2  # one 429, one success
    assert slept == [1.0]  # first backoff is the base delay


def test_retry_honours_numeric_retry_after_header(private_key_pem: bytes) -> None:
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not slept:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"aspsps": []})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123", private_key=private_key_pem, http=http, sleep=slept.append
    )
    client.list_aspsps(country="PL")
    assert slept == [7.0]  # server-provided delay wins over backoff


def test_retry_falls_back_to_backoff_on_a_non_numeric_retry_after(private_key_pem: bytes) -> None:
    # An HTTP-date Retry-After ("Wed, 21 Oct 2026 07:28:00 GMT") is not parsed - it must fall
    # through to the same capped exponential backoff as no header at all.
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not slept:
            return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        return httpx.Response(200, json={"aspsps": []})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123", private_key=private_key_pem, http=http, sleep=slept.append
    )
    client.list_aspsps(country="PL")
    assert slept == [1.0]  # base backoff, not a value parsed from the header


def test_retry_exhaustion_raises(private_key_pem: bytes) -> None:
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "TOO_MANY_REQUESTS"})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123",
        private_key=private_key_pem,
        http=http,
        sleep=slept.append,
        max_retries=3,
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.list_aspsps(country="PL")
    # 3 retries -> capped exponential backoff sequence, then the 4th attempt raises.
    assert slept == [1.0, 2.0, 4.0]


def test_psu_headers_sent_on_data_endpoints_only(private_key_pem: bytes) -> None:
    seen: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers
        if request.url.path == "/aspsps":
            return httpx.Response(200, json={"aspsps": []})
        if request.url.path.endswith("/balances"):
            return httpx.Response(200, json={"balances": []})
        if request.url.path.endswith("/transactions"):
            return httpx.Response(200, json={"transactions": [], "continuation_key": None})
        return httpx.Response(404)

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123",
        private_key=private_key_pem,
        http=http,
        psu_headers={"Psu-Ip-Address": "203.0.113.5", "Psu-User-Agent": "gnucash-ofx/test"},
    )

    client.get_balances("acc-1")
    list(client.iter_transactions("acc-1", date_from="2026-05-01", date_to="2026-05-31"))
    client.list_aspsps(country="PL")

    # Data-retrieval endpoints carry the PSU headers (online fetch).
    assert seen["/accounts/acc-1/balances"]["Psu-Ip-Address"] == "203.0.113.5"
    assert seen["/accounts/acc-1/transactions"]["Psu-User-Agent"] == "gnucash-ofx/test"
    # Discovery endpoints must not carry them.
    assert "Psu-Ip-Address" not in seen["/aspsps"]


def test_negative_max_retries_rejected(private_key_pem: bytes) -> None:
    with pytest.raises(ValueError, match="max_retries must be >= 0"):
        EnableBankingClient(application_id="app-123", private_key=private_key_pem, max_retries=-1)


def test_close_closes_owned_client(private_key_pem: bytes) -> None:
    client = EnableBankingClient(application_id="app-123", private_key=private_key_pem)
    client.close()
    assert client._http.is_closed


def test_context_manager_closes_owned_client(private_key_pem: bytes) -> None:
    with EnableBankingClient(application_id="app-123", private_key=private_key_pem) as client:
        pass
    assert client._http.is_closed


def test_close_leaves_injected_client_open(private_key_pem: bytes) -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    client = EnableBankingClient(application_id="app-123", private_key=private_key_pem, http=http)
    client.close()
    assert not http.is_closed


def test_audience_follows_origin(private_key_pem: bytes) -> None:
    client = EnableBankingClient(
        application_id="app-123",
        private_key=private_key_pem,
        origin="https://sandbox.enablebanking.com",
    )
    claims = pyjwt.decode(client._jwt(), options={"verify_signature": False})
    assert claims["aud"] == "sandbox.enablebanking.com"
    client.close()


def test_observer_sees_every_response_including_retries(private_key_pem: bytes) -> None:
    """The rate-limit investigation needs the whole request sequence, not just the final outcome.

    A 429 that recovers under backoff (transient, or a per-second limit) and one that never does
    (the daily cap) are the same final exception; only the attempt sequence tells them apart.
    """
    seen: list[dict[str, object]] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(
                429,
                json={"error": "TOO_MANY_REQUESTS"},
                headers={"Retry-After": "1", "Set-Cookie": "s=secret"},
            )
        return httpx.Response(200, json={"aspsps": []})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123",
        private_key=private_key_pem,
        http=http,
        sleep=lambda _s: None,
        observer=lambda **kw: seen.append(kw),
    )
    client.list_aspsps(country="PL")

    assert [(r["status"], r["attempt"]) for r in seen] == [(429, 0), (429, 1), (200, 2)]
    assert [r["api_code"] for r in seen] == ["TOO_MANY_REQUESTS"] * 2 + [None]
    # No `detail` in this envelope, and never a name on a success.
    assert [r["error_name"] for r in seen] == [None, None, None]
    assert all(r["method"] == "GET" and r["path"] == "/aspsps" for r in seen)
    assert all(isinstance(r["elapsed"], float) for r in seen)
    # Headers are passed through raw; the log decides what to keep.
    assert seen[0]["headers"]["retry-after"] == "1"


def test_observer_sees_a_failure_that_is_never_retried(private_key_pem: bytes) -> None:
    """The envelope is the N26 shape measured live 2026-08-19: a 400 `ASPSP_ERROR` whose `detail`
    wraps the platform's own exception. `api_code` alone reads as a bare 400; `error_name` is what
    makes a recurrence of the transient bank-down shape recognisable in the log.
    """
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "ASPSP_ERROR",
                "message": "Error",
                "detail": {"message": "Service unavailable", "error_name": "HttpException"},
            },
        )

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123",
        private_key=private_key_pem,
        http=http,
        observer=lambda **kw: seen.append(kw),
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.list_aspsps(country="PL")
    assert [(r["status"], r["api_code"], r["error_name"]) for r in seen] == [
        (400, "ASPSP_ERROR", "HttpException")
    ]


def test_a_broken_observer_never_breaks_a_request(private_key_pem: bytes) -> None:
    def boom(**_kwargs: object) -> None:
        raise RuntimeError("observer is broken")

    http = httpx.Client(
        base_url="https://api.enablebanking.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"aspsps": []})),
    )
    client = EnableBankingClient(
        application_id="app-123", private_key=private_key_pem, http=http, observer=boom
    )
    assert client.list_aspsps(country="PL") == []


def test_api_error_code_reads_error_not_message(private_key_pem: bytes) -> None:
    # AGENTS.md: "Match on error, never message." docs/enable-banking.md: `message` is prose and
    # must never be matched on.
    assert (
        api_error_code(
            httpx.Response(
                429,
                json={
                    "code": 429,
                    "message": "Too many requests",
                    "error": "ASPSP_RATE_LIMIT_EXCEEDED",
                },
            )
        )
        == "ASPSP_RATE_LIMIT_EXCEEDED"
    )
    assert api_error_code(httpx.Response(500, text="<html>gateway</html>")) is None
    assert api_error_code(httpx.Response(200, json={"aspsps": []})) is None
    # A gateway or proxy can answer with well-formed JSON that is not the error envelope at all.
    assert api_error_code(httpx.Response(502, json=["upstream down"])) is None
    assert api_error_code(httpx.Response(400, json={"error": {"nested": "not a token"}})) is None


def test_api_error_name_reads_the_platform_exception_out_of_detail() -> None:
    """`detail.error_name` is the platform's wrapped exception (docs/enable-banking.md, Error
    envelope): `HttpException` on the N26 bank-down 400 of 2026-08-19, `RateLimitException` on the
    verified 429. `detail` is null on plain ASPSP errors, so every miss must read as None.
    """
    assert (
        api_error_name(
            httpx.Response(
                400,
                json={
                    "code": 400,
                    "message": "Error interacting with ASPSP",
                    "error": "ASPSP_ERROR",
                    "detail": {"message": "Service unavailable", "error_name": "HttpException"},
                },
            )
        )
        == "HttpException"
    )
    assert api_error_name(httpx.Response(400, json={"error": "ASPSP_ERROR", "detail": None})) is (
        None
    )
    assert api_error_name(httpx.Response(400, json={"error": "ASPSP_ERROR"})) is None
    assert api_error_name(httpx.Response(500, text="<html>gateway</html>")) is None
    assert api_error_name(httpx.Response(502, json=["upstream down"])) is None
    # A detail that is not the documented object, and an error_name that is not a token.
    assert api_error_name(httpx.Response(400, json={"detail": "not an object"})) is None
    assert api_error_name(httpx.Response(400, json={"detail": {"error_name": 500}})) is None


def test_the_daily_cap_is_not_retried(private_key_pem: bytes) -> None:
    """ASPSP_RATE_LIMIT_EXCEEDED recovers in ~6h; the ladder tops out at 31s.

    AGENTS.md: "ASPSP_RATE_LIMIT_EXCEEDED is never retried." Every retry is certain to fail and
    is another counted request at an ASPSP that has just said it has had enough, so it is raised
    on the first response instead.
    """
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123", private_key=private_key_pem, http=http, sleep=slept.append
    )
    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.list_aspsps(country="PL")
    assert exc.value.response.status_code == 429
    assert calls["n"] == 1  # no retries
    assert slept == []  # and no waiting to learn nothing


def test_a_generic_429_keeps_its_backoff(private_key_pem: bytes) -> None:
    """Platform-level throttling is transient, which is exactly what backoff is for."""
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "TOO_MANY_REQUESTS"})
        return httpx.Response(200, json={"aspsps": []})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123", private_key=private_key_pem, http=http, sleep=slept.append
    )
    client.list_aspsps(country="PL")
    assert calls["n"] == 2
    assert slept == [1.0]


def test_a_429_with_no_body_keeps_its_backoff(private_key_pem: bytes) -> None:
    # Only the named code is known to be un-retryable; an unparseable 429 gets the benefit of it.
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="<html>throttled</html>")
        return httpx.Response(200, json={"aspsps": []})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123", private_key=private_key_pem, http=http, sleep=slept.append
    )
    client.list_aspsps(country="PL")
    assert calls["n"] == 2
    assert slept == [1.0]


def test_a_huge_retry_after_is_clamped_to_the_backoff_ceiling(private_key_pem: bytes) -> None:
    """docs/adr-input-hardening.md decision 2: the server does not get to pick the sleep.

    Unclamped, `Retry-After: 999999999` parked the run in `time.sleep` for roughly 31 years. The
    ceiling reused here is the ladder's own, so there is one answer to how long a retry can wait.
    """
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not slept:
            return httpx.Response(429, headers={"Retry-After": "999999999"})
        return httpx.Response(200, json={"aspsps": []})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123", private_key=private_key_pem, http=http, sleep=slept.append
    )
    client.list_aspsps(country="PL")
    assert slept == [60.0]  # _MAX_BACKOFF_SECONDS, not the header


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "Infinity"])
def test_a_non_finite_retry_after_falls_back_to_backoff(private_key_pem: bytes, value: str) -> None:
    """`float("inf")` passed the old `>= 0` check and raised OverflowError inside time.sleep.

    NaN failed that check already and reached the ladder by accident; it now fails it on purpose.
    """
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not slept:
            return httpx.Response(429, headers={"Retry-After": value})
        return httpx.Response(200, json={"aspsps": []})

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123", private_key=private_key_pem, http=http, sleep=slept.append
    )
    client.list_aspsps(country="PL")
    assert slept == [1.0]  # the base backoff, and nothing non-finite reached sleep


def test_an_oversized_response_is_refused_before_it_is_parsed(private_key_pem: bytes) -> None:
    """docs/adr-input-hardening.md decision 5: a body past the cap fails loudly.

    Checked before `.json()`, which is where the bytes would become a structure several times
    their size.
    """
    # Deliberately NOT valid JSON: if the size check ever moved after .json(), this would raise a
    # JSON error instead and the test would notice. A huge-but-valid body could not tell them apart.
    oversized = b"x" * (_MAX_RESPONSE_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    with pytest.raises(ResponseLimitExceeded, match="over the"):
        _client(private_key_pem, handler).list_aspsps(country="PL")


def test_pagination_stops_at_the_page_cap_instead_of_following_forever(
    private_key_pem: bytes,
) -> None:
    """A server that always offers another page held the run here forever, a request per turn.

    The cap raises rather than returning what it has: a short answer that looked complete would
    advance the coverage ledger over transactions nobody ever wrote.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Always another key, never an end - the malicious/broken shape decision 5 exists for.
        return httpx.Response(200, json={"transactions": [], "continuation_key": "more"})

    client = _client(private_key_pem, handler)
    with pytest.raises(ResponseLimitExceeded, match="paginated past"):
        list(client.iter_transactions("uid-1", date_from="2026-07-01"))
    assert calls["n"] == _MAX_PAGES_PER_ACCOUNT  # bounded, and it stopped at the cap


def test_pagination_still_follows_a_finite_chain(private_key_pem: bytes) -> None:
    """The cap must not change the ordinary two-page case, which is what real banks send."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json={"transactions": [{"a": 1}], "continuation_key": "next"}
            )
        return httpx.Response(200, json={"transactions": [{"a": 2}]})

    got = list(_client(private_key_pem, handler).iter_transactions("uid-1", date_from="2026-07-01"))
    assert got == [{"a": 1}, {"a": 2}]
    assert calls["n"] == 2


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/accounts/u1/balances", "balances"),
        ("/accounts/u1/transactions", "transactions"),
        ("/sessions/s1", "session"),
        ("/aspsps", "request"),
    ],
)
def test_operation_of_path_names_the_endpoint(path: str, expected: str) -> None:
    """A hard-coded label would report an oversized /balances body as a transactions failure.

    AGENTS.md: never invent an error explanation.
    """
    assert _operation_of_path(path) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1 234,56", "8 chars, digits+whitespace+punctuation"),
        ("100 PLN", "7 chars, digits+letters+whitespace"),
        ("abc", "3 chars, letters"),
        ("", "0 chars, empty"),
    ],
)
def test_value_shape_describes_without_quoting(value: str, expected: str) -> None:
    """`InvalidOperation` fires on non-canonical *real* money, and the message is user-visible.

    So a rejected amount is described by length and character class, never quoted — AGENTS.md#data
    forbids putting a real balance in anything a user is asked to paste.
    """
    assert _value_shape(value) == expected


def test_a_rejected_amount_is_never_quoted_in_the_error(private_key_pem: bytes) -> None:
    with pytest.raises(ValueError, match=r"8 chars, digits\+whitespace\+punctuation") as caught:
        finite_decimal("1 234,56", field="transaction_amount.amount")
    assert "1 234,56" not in str(caught.value)  # the value itself never reaches the message


def test_a_page_budget_is_shared_across_an_accounts_spans(private_key_pem: bytes) -> None:
    """One account-window can be several request spans, so the bound has to span them.

    Passing the constant per call gave each span its own fresh allowance, making the real bound
    `cap x spans`. With a shared budget of 3, a first span that uses two pages leaves one — so the
    second span raises after a single request, at 3 calls rather than 5.
    """
    budget = PageBudget(remaining=3)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Call 2 ends the first span; every other response offers another page.
        key = None if calls["n"] == 2 else "more"
        return httpx.Response(200, json={"transactions": [], "continuation_key": key})

    client = _client(private_key_pem, handler)
    assert list(client.iter_transactions("uid-1", date_from="2026-06-01", budget=budget)) == []
    assert (calls["n"], budget.remaining) == (2, 1)  # the first span spent two of the three

    with pytest.raises(ResponseLimitExceeded, match="paginated past"):
        list(client.iter_transactions("uid-1", date_from="2026-07-01", budget=budget))
    assert calls["n"] == 3  # not 5: the second span inherited what was left, not a fresh 3


def test_an_oversized_error_body_is_bounded_before_anything_parses_it(
    private_key_pem: bytes,
) -> None:
    """The cap sat after `_observe` and after `raise_for_status()`, so no 4xx ever reached it.

    `_observe` reads an error body for its `api_code` and the 429 branch reads it again, so an
    over-cap error body was parsed twice, unbounded — while a check placed after
    `raise_for_status()` could only ever see a success. The request is still recorded: it was
    still spent.
    """
    observed: list[dict[str, object]] = []

    def observer(**kwargs: object) -> None:
        observed.append(kwargs)

    def handler(request: httpx.Request) -> httpx.Response:
        # Not valid JSON: if anything parsed it, the failure would be a JSON error instead.
        return httpx.Response(400, content=b"x" * (_MAX_RESPONSE_BYTES + 1))

    http = httpx.Client(
        base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
    )
    client = EnableBankingClient(
        application_id="app-123", private_key=private_key_pem, http=http, observer=observer
    )
    with pytest.raises(ResponseLimitExceeded, match="over the"):
        client.list_aspsps(country="PL")
    # Logged, with the body deliberately left unparsed - so no api_code, but the spend is on record.
    assert len(observed) == 1
    assert observed[0]["status"] == 400
    assert observed[0]["api_code"] is None
