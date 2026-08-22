"""Tests for the CLI: argument parsing and the no-network commands."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gnucash_ofx import __version__
from gnucash_ofx.cli import _build_parser, main

CONFIG = """
[output]
dir = "{output}"
[state]
dir = "{state}"
[cache]
dir = "{cache}"

[banks.alior]
source = "enablebanking"
aspsp = "Alior Bank"
country = "PL"
"""


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(
            output=(tmp_path / "out").as_posix(),
            state=(tmp_path / "state").as_posix(),
            cache=(tmp_path / "cache").as_posix(),
        ),
        encoding="utf-8",
    )
    return path


def test_version_is_set() -> None:
    assert __version__


def test_parser_requires_a_command() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_fetch_parses_date_range() -> None:
    parser = _build_parser()
    args = parser.parse_args(["fetch", "--from", "2026-05-01", "--to", "2026-05-31"])
    assert args.command == "fetch"
    assert args.date_from == "2026-05-01"
    assert args.date_to == "2026-05-31"
    assert args.bank == "all"


def test_status_lists_configured_banks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _write_config(tmp_path)
    assert main(["--config", str(config), "status"]) == 0
    out = capsys.readouterr().out
    assert "alior" in out
    assert "not linked" in out


def test_link_unknown_bank_errors(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    with pytest.raises(SystemExit, match="unknown bank"):
        main(["--config", str(config), "link", "nope"])


def test_link_wrong_source_bank_errors(tmp_path: Path) -> None:
    """'link' is Enable-Banking-specific; a configured bank of another source must be rejected."""
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[output]
dir = "{(tmp_path / "out").as_posix()}"
[state]
dir = "{(tmp_path / "state").as_posix()}"

[banks.wise_personal]
source = "wise"
token_env = "X"
""",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="only applies to Enable Banking banks"):
        main(["--config", str(config), "link", "wise_personal"])


def test_fetch_invalid_date_errors(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    with pytest.raises(SystemExit, match="invalid date 'not-a-date'"):
        main(["--config", str(config), "fetch", "--from", "not-a-date", "--to", "2026-05-31"])


def test_fetch_rejects_a_reversed_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two valid dates in the wrong order, refused before the run is set up at all.

    The check has to sit ahead of build_psu_headers - which goes out to the network to find the
    public IP, and raises its own error about background mode when it cannot - or the user is told
    about their rate-limit allowance instead of about the typo that is actually in their command.
    """
    config = _write_config(tmp_path)
    _refuse_to_fetch(monkeypatch)
    with pytest.raises(SystemExit, match="--from 2026-07-31 is after --to 2026-07-01"):
        main(["--config", str(config), "fetch", "--from", "2026-07-31", "--to", "2026-07-01"])
    # Refusing the window is not a run: nothing is recorded as having been spent.
    assert not (tmp_path / "state" / "fetch-log.jsonl").exists()


def test_missing_config_errors() -> None:
    with pytest.raises(SystemExit, match="config file not found"):
        main(["--config", "does-not-exist.toml", "status"])


def test_malformed_config_errors_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[output\ndir = './out'\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="not valid TOML"):
        main(["--config", str(path), "status"])


def test_status_reports_a_corrupt_state_file_instead_of_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_config(tmp_path)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "alior.json").write_text("{ not valid json", encoding="utf-8")
    assert main(["--config", str(config), "status"]) == 0
    out = capsys.readouterr().out
    assert "alior" in out
    assert "corrupted" in out


class _FakeFetchClient:
    """Duck-typed stand-in for EnableBankingClient used by fetch."""

    def get_session(self, session_id: str) -> dict:
        # Real GET /sessions returns account UIDs as plain strings (no currency/IBAN).
        return {"accounts": ["a"]}

    def iter_transactions(self, uid: str, *, date_from: str, date_to: str, budget: int = 100):
        yield {
            "transaction_id": "t1",
            "booking_date": "2026-05-02",
            "transaction_amount": {"currency": "PLN", "amount": "10.00"},
            "credit_debit_indicator": "DBIT",
            "remittance_information": ["Coffee"],
        }

    def get_balances(self, uid: str) -> dict:
        # Currency is inferred from balances when GET /sessions returns bare UIDs.
        return {
            "balances": [
                {"balance_amount": {"currency": "PLN", "amount": "0.00"}, "balance_type": "CLBD"}
            ]
        }

    def close(self) -> None:
        pass


def test_fetch_command_writes_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date

    from gnucash_ofx.state import LinkedAccount, SessionState, save_session

    config = _write_config(tmp_path)
    save_session(
        tmp_path / "state",
        SessionState("alior", "sess-1", date(2099, 1, 1), (LinkedAccount("a"),)),
    )
    monkeypatch.setattr(
        "gnucash_ofx.cli.build_enablebanking_client", lambda **_kw: _FakeFetchClient()
    )
    # Avoid a real public-IP lookup (network) during the test.
    monkeypatch.setattr("gnucash_ofx.cli.build_psu_headers", lambda **_kw: None)

    exit_code = main(
        ["--config", str(config), "fetch", "--from", "2026-05-01", "--to", "2026-05-31"]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    written = tmp_path / "out" / "alior_PLN_2026_05_01-2026_05_31.ofx"
    assert written.exists()
    # stdout is exactly the written paths: no header, no indent, nothing to strip.
    assert captured.out == f"{written}\n"
    # The header and the progress lines go to stderr so they don't pollute stdout.
    assert f"Wrote 1 OFX file(s) to {tmp_path / 'out'}:" in captured.err
    assert "alior" in captured.err
    assert "transaction(s)" in captured.err


def test_fetch_scrubs_a_session_id_left_in_the_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/adr-input-hardening.md decision 1: a log written before the fix is still leaking.

    Wired into `fetch` rather than into `RunLog` so the count can be reported — and the notice goes
    to stderr like every other progress line, because stdout is the file list.
    """
    from gnucash_ofx.state import LinkedAccount, SessionState, save_session

    session_id = "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"  # synthetic, shaped like the real ones
    config = _write_config(tmp_path)
    state_dir = tmp_path / "state"
    save_session(
        state_dir,
        SessionState("alior", "sess-1", date(2099, 1, 1), (LinkedAccount("a"),)),
    )
    log = state_dir / "fetch-log.jsonl"
    log.write_text(
        json.dumps(
            {
                "at": "2026-08-09T12:00:00+00:00",
                "event": "request",
                "method": "GET",
                "path": f"/sessions/{session_id}",
                "status": 200,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "gnucash_ofx.cli.build_enablebanking_client", lambda **_kw: _FakeFetchClient()
    )
    monkeypatch.setattr("gnucash_ofx.cli.build_psu_headers", lambda **_kw: None)

    exit_code = main(
        ["--config", str(config), "fetch", "--from", "2026-05-01", "--to", "2026-05-31"]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert session_id not in log.read_text(encoding="utf-8")
    assert "masked an unredacted identifier on 1 earlier line(s)" in captured.err
    assert "masked an unredacted identifier" not in captured.out


def test_fetch_parses_combine() -> None:
    """`--combine` defaults to None, not False — "not given" must differ from "given as off".

    docs/adr-ofx-batch-splitting.md decision 9: `config.toml` can set `[fetch] combine`, so
    `--no-combine` needs a way to turn it off for one run, which a bare `store_true` cannot
    express. `_resolve_packaging` is what turns None back into today's default.
    """
    parser = _build_parser()
    assert parser.parse_args(["fetch"]).combine is None
    assert parser.parse_args(["fetch", "--combine"]).combine is True
    assert parser.parse_args(["fetch", "--no-combine"]).combine is False


def test_fetch_command_combine_writes_one_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/adr-combined-ofx-file.md decision 5: stdout keeps its shape, one path where there
    were N."""
    from gnucash_ofx.state import LinkedAccount, SessionState, save_session

    config = _write_config(tmp_path)
    save_session(
        tmp_path / "state",
        SessionState("alior", "sess-1", date(2099, 1, 1), (LinkedAccount("a"),)),
    )
    monkeypatch.setattr(
        "gnucash_ofx.cli.build_enablebanking_client", lambda **_kw: _FakeFetchClient()
    )
    monkeypatch.setattr("gnucash_ofx.cli.build_psu_headers", lambda **_kw: None)

    exit_code = main(
        [
            "--config",
            str(config),
            "fetch",
            "--from",
            "2026-05-01",
            "--to",
            "2026-05-31",
            "--combine",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    written = tmp_path / "out" / "combined_2026_05_01-2026_05_31.ofx"
    assert written.exists()
    assert captured.out == f"{written}\n"
    assert f"Wrote 1 OFX file(s) to {tmp_path / 'out'}:" in captured.err


@pytest.fixture(scope="module")
def private_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def test_fetch_reports_a_rate_limit_backoff_via_reporting_sleep(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    private_key_pem: bytes,
) -> None:
    """_reporting_sleep is wired in as the client's sleep= callback; a real 429 retry during a
    real fetch must be reported, not silently waited out. Every other fetch test uses a
    duck-typed fake that never sleeps, so this uses a real EnableBankingClient + MockTransport,
    as test_enablebanking_client.py does for the client-level retry tests."""
    from datetime import date

    from gnucash_ofx.sources.enablebanking import EnableBankingClient
    from gnucash_ofx.state import LinkedAccount, SessionState, save_session

    config = _write_config(tmp_path)
    save_session(
        tmp_path / "state",
        SessionState("alior", "sess-1", date(2099, 1, 1), (LinkedAccount("a", currency="PLN"),)),
    )
    calls = {"transactions": 0}

    # The window has closed and the currency is known, so no /balances call is made at all
    # (decision 4); the retry has to be exercised on the call that is still sent.
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions/sess-1":
            return httpx.Response(200, json={"accounts": ["a"]})
        if path.endswith("/transactions"):
            calls["transactions"] += 1
            if calls["transactions"] == 1:
                # A transient 429. The daily cap is deliberately never retried, so it would fail
                # the fetch outright rather than exercise the backoff reporting this is about.
                return httpx.Response(429, json={"error": "TOO_MANY_REQUESTS"})
            return httpx.Response(200, json={"transactions": [], "continuation_key": None})
        return httpx.Response(404)

    def fake_build_client(
        *, sleep: object, psu_headers: object = None, observer: object = None
    ) -> EnableBankingClient:
        http = httpx.Client(
            base_url="https://api.enablebanking.com", transport=httpx.MockTransport(handler)
        )
        return EnableBankingClient(
            application_id="app",
            private_key=private_key_pem,
            http=http,
            sleep=sleep,
            observer=observer,
        )

    monkeypatch.setattr("gnucash_ofx.cli.build_enablebanking_client", fake_build_client)
    monkeypatch.setattr("gnucash_ofx.cli.build_psu_headers", lambda **_kw: None)
    # _reporting_sleep really does call time.sleep(); avoid the real 1s wait in the test suite.
    monkeypatch.setattr("gnucash_ofx.cli.time.sleep", lambda _seconds: None)

    exit_code = main(
        ["--config", str(config), "fetch", "--from", "2026-05-01", "--to", "2026-05-31"]
    )
    assert exit_code == 0
    assert "rate limited by bank, waiting" in capsys.readouterr().err

    # The same retry must be recoverable afterwards: a 429 that recovered under backoff is what
    # tells a transient limit apart from the daily cap, and the CLI is where the log is wired up.
    records = [
        json.loads(line)
        for line in (tmp_path / "state" / "fetch-log.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    run_record = records[0]
    assert (run_record["event"], run_record["psu_mode"]) == ("run", "background")
    data_calls = [r for r in records if r.get("path", "").endswith("/transactions")]
    assert [(r["status"], r["attempt"], r["bank"]) for r in data_calls] == [
        (429, 0, "alior"),
        (200, 1, "alior"),
    ]
    assert data_calls[0]["api_code"] == "TOO_MANY_REQUESTS"
    assert data_calls[0]["domain"] == "Alior Bank|PL|personal"
    # And nothing was spent on a balance that could not have been this statement's closing one.
    assert not [r for r in records if r.get("path", "").endswith("/balances")]


class _FakeAspspsClient:
    def __init__(self, seen: list[str] | None = None) -> None:
        self._seen = seen if seen is not None else []

    def list_aspsps(self, country: str) -> list[dict]:
        self._seen.append(country)
        return [{"name": "Alior Bank", "country": "PL"}, {"name": "Wise", "country": "PL"}]

    def close(self) -> None:
        pass


def test_aspsps_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    monkeypatch.setattr("gnucash_ofx.cli.build_enablebanking_client", lambda: _FakeAspspsClient())
    assert main(["--config", str(config), "aspsps", "--country", "PL"]) == 0
    out = capsys.readouterr().out
    assert "Alior Bank (PL)" in out
    assert "Wise (PL)" in out


def test_aspsps_works_without_config(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Discovery must not require a config.toml to exist, and should normalize the country.
    seen: list[str] = []
    monkeypatch.setattr(
        "gnucash_ofx.cli.build_enablebanking_client", lambda: _FakeAspspsClient(seen)
    )
    assert main(["--config", "no-such-config.toml", "aspsps", "--country", " pl "]) == 0
    assert "Alior Bank (PL)" in capsys.readouterr().out
    assert seen == ["PL"]  # trimmed + uppercased


class _FakeLinkClient:
    def list_aspsps(self, country: str) -> list[dict]:
        # 180 days, as every real bank we use advertises.
        return [{"name": "Alior Bank", "country": "PL", "maximum_consent_validity": 15552000}]

    def get_application(self) -> dict:
        return {"redirect_urls": ["https://localhost/callback"]}

    def start_authorization(self, **kwargs: object) -> str:
        return "https://bank.example/sca"

    def create_session(self, code: str) -> dict:
        return {"session_id": "s1", "accounts": [{"uid": "a"}]}

    def close(self) -> None:
        pass


def test_link_command_flow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    monkeypatch.setattr("gnucash_ofx.cli.build_enablebanking_client", lambda: _FakeLinkClient())
    monkeypatch.setattr("builtins.input", lambda _prompt: "https://localhost/callback?code=XYZ")

    assert main(["--config", str(config), "link", "alior"]) == 0
    out = capsys.readouterr().out
    assert "Linked 'alior'" in out
    from gnucash_ofx.state import load_session

    state = load_session(tmp_path / "state", "alior")
    assert state is not None and state.session_id == "s1"


def test_link_redirect_url_without_code_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """parse_auth_code's ValueError is unit-tested in isolation; this is the CLI's handling."""
    config = _write_config(tmp_path)
    monkeypatch.setattr("gnucash_ofx.cli.build_enablebanking_client", lambda: _FakeLinkClient())
    monkeypatch.setattr("builtins.input", lambda _prompt: "https://localhost/callback?state=xyz")
    with pytest.raises(SystemExit, match="no 'code'"):
        main(["--config", str(config), "link", "alior"])


class _ErrorLinkClient:
    def list_aspsps(self, country: str) -> list[dict]:
        return []

    def get_application(self) -> dict:
        return {"redirect_urls": ["https://localhost/callback"]}

    def start_authorization(self, **kwargs: object) -> str:
        import httpx

        request = httpx.Request("POST", "https://api.enablebanking.com/auth")
        response = httpx.Response(422, json={"message": "invalid aspsp"}, request=request)
        raise httpx.HTTPStatusError("422", request=request, response=response)

    def close(self) -> None:
        pass


def test_link_api_error_shows_clean_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    monkeypatch.setattr("gnucash_ofx.cli.build_enablebanking_client", lambda: _ErrorLinkClient())
    with pytest.raises(SystemExit, match="422"):
        main(["--config", str(config), "link", "alior"])


class _RateLimitedLinkClient:
    """422's cousin: a status diagnose() actually recognises, with a non-JSON body."""

    def list_aspsps(self, country: str) -> list[dict]:
        return []

    def get_application(self) -> dict:
        return {"redirect_urls": ["https://localhost/callback"]}

    def start_authorization(self, **kwargs: object) -> str:
        request = httpx.Request("POST", "https://api.enablebanking.com/auth")
        response = httpx.Response(429, text="Too Many Requests", request=request)
        raise httpx.HTTPStatusError("429", request=request, response=response)

    def close(self) -> None:
        pass


def test_link_last_resort_handler_falls_back_to_raw_text_and_adds_a_known_diagnosis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one existing last-resort test (422) hits neither branch this covers: a non-JSON body,
    and a status diagnose() actually explains (the api_code passed here is always None, so only
    a 429 can match)."""
    config = _write_config(tmp_path)
    monkeypatch.setattr(
        "gnucash_ofx.cli.build_enablebanking_client", lambda: _RateLimitedLinkClient()
    )
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(config), "link", "alior"])
    message = str(exc_info.value)
    assert "Too Many Requests" in message  # non-JSON body fell back to response.text
    assert "per bank" in message  # diagnose()'s rate-limit explanation
    assert "psu_mode" in message  # its suggested action


# ------------------------------------------------------------------- partial-success reporting


class _FailingFetchClient(_FakeFetchClient):
    """Two accounts; the second one's data calls fail with the given response."""

    def __init__(self, status: int = 400, body: dict | None = None) -> None:
        self._status = status
        # The real envelope: prose in `message`, the machine-readable code in `error`.
        self._body = (
            body
            if body is not None
            else {
                "code": 400,
                "message": "Error interacting with ASPSP",
                "error": "ASPSP_ERROR",
                "detail": None,
            }
        )

    def get_session(self, session_id: str) -> dict:
        return {"accounts": ["a", "b"]}

    def _fail(self, uid: str) -> None:
        if uid == "b":
            request = httpx.Request(
                "GET", f"https://api.enablebanking.com/accounts/{uid}/transactions"
            )
            response = httpx.Response(self._status, json=self._body, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)

    def iter_transactions(self, uid: str, *, date_from: str, date_to: str, budget: int = 100):
        self._fail(uid)
        yield from super().iter_transactions(uid, date_from=date_from, date_to=date_to)

    def get_balances(self, uid: str) -> dict:
        self._fail(uid)
        return super().get_balances(uid)


def _fetch_with(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: object,
    *,
    bank: str | None = None,
    linked: bool = True,
) -> int:
    from datetime import date

    from gnucash_ofx.state import LinkedAccount, SessionState, save_session

    config = _write_config(tmp_path)
    if linked:
        save_session(
            tmp_path / "state",
            SessionState(
                "alior",
                "sess-1",
                date(2099, 1, 1),
                (LinkedAccount("a", currency="PLN"), LinkedAccount("b", currency="PLN")),
            ),
        )
    monkeypatch.setattr("gnucash_ofx.cli.build_enablebanking_client", lambda **_kw: client)
    monkeypatch.setattr("gnucash_ofx.cli.build_psu_headers", lambda **_kw: None)
    argv = ["--config", str(config), "fetch", "--from", "2026-05-01", "--to", "2026-05-31"]
    if bank is not None:
        argv += ["--bank", bank]
    return main(argv)


def test_partial_success_exits_1_but_keeps_the_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted and cron use must notice, without losing the accounts that worked."""
    exit_code = _fetch_with(tmp_path, monkeypatch, _FailingFetchClient())
    captured = capsys.readouterr()

    assert exit_code == 1
    assert ".ofx" in captured.out  # account a still written and reported
    assert "1 bank(s) failed: alior" in captured.err
    assert "Re-run the same command to retry only the failures." in captured.err


def test_stdout_is_the_written_paths_and_nothing_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fetch > files.txt` must be consumable with no header to skip and no indent to strip.

    AGENTS.md: "fetch stdout is the file list; everything else is stderr."
    """
    _fetch_with(tmp_path, monkeypatch, _FailingFetchClient())
    captured = capsys.readouterr()
    assert "failed" not in captured.out
    assert "ASPSP_ERROR" not in captured.out
    assert "Wrote " in captured.err  # the count-and-directory header is for the human
    lines = captured.out.splitlines()
    assert lines  # a bank failed, but the account that worked is still listed
    for line in lines:
        assert line == line.strip()
        assert Path(line).suffix == ".ofx"
        assert Path(line).exists()


def test_known_error_gets_an_explanation_and_the_raw_response(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fetch_with(tmp_path, monkeypatch, _FailingFetchClient())
    err = capsys.readouterr().err
    assert "refused the requested date range" in err  # the explanation
    assert "--from" in err  # the next step
    assert "ASPSP_ERROR" in err  # the bank's own words, verbatim
    assert "HTTP 400" in err


def test_unknown_error_gets_no_invented_explanation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FailingFetchClient(status=500, body={"error": "SOMETHING_NEW"})
    _fetch_with(tmp_path, monkeypatch, client)
    err = capsys.readouterr().err
    assert "No known explanation" in err
    assert "docs/enable-banking.md" in err
    assert "SOMETHING_NEW" in err  # still shows what the bank actually said


def test_all_ok_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _fetch_with(tmp_path, monkeypatch, _FakeFetchClient()) == 0
    assert "failed" not in capsys.readouterr().err


class _NoTransactionsFetchClient(_FakeFetchClient):
    def iter_transactions(self, uid: str, *, date_from: str, date_to: str, budget: int = 100):
        return iter(())


def test_fetch_reports_no_transactions_in_range(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct from 'every bank failed': the run succeeded, there was just nothing to write."""
    exit_code = _fetch_with(tmp_path, monkeypatch, _NoTransactionsFetchClient())
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "no transactions in the requested range" in captured.err
    assert captured.out == ""  # no files, so no lines: `> files.txt` is an empty file


def test_single_bank_failure_reports_like_the_all_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--bank alior` must not fall back to the raw last-resort dump."""
    exit_code = _fetch_with(tmp_path, monkeypatch, _FailingFetchClient(), bank="alior")
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "1 bank(s) failed: alior" in err
    assert "refused the requested date range" in err


def test_unknown_bank_still_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit request that cannot be satisfied is a usage error, not a partial success."""
    with pytest.raises(SystemExit, match="not a configured Enable Banking bank"):
        _fetch_with(tmp_path, monkeypatch, _FakeFetchClient(), bank="ghost")


def test_failure_output_is_ascii(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cp1252 console must not turn a bank failure into a UnicodeEncodeError."""
    _fetch_with(tmp_path, monkeypatch, _FailingFetchClient())
    captured = capsys.readouterr()
    captured.err.encode("ascii")
    captured.out.encode("ascii")


def test_status_output_is_ascii(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _write_config(tmp_path)
    main(["--config", str(config), "status"])  # unlinked bank: the branch with the dash
    capsys.readouterr().out.encode("ascii")


def test_total_failure_does_not_claim_there_were_no_transactions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was fetched, so nothing can be said about whether transactions existed."""
    client = _FailingFetchClient()
    client.get_session = lambda session_id: {"accounts": ["b"]}  # type: ignore[method-assign]
    assert _fetch_with(tmp_path, monkeypatch, client) == 1
    captured = capsys.readouterr()
    assert "every bank failed" in captured.err
    assert "no transactions in the requested range" not in captured.err
    assert captured.out == ""


def test_fetch_refuses_background_mode_unless_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falling into the ~4/day background allowance is the expensive mistake, so it is never the
    quiet default; --allow-background is how the user says they meant it."""
    from datetime import date

    from gnucash_ofx.state import LinkedAccount, SessionState, save_session

    config = _write_config(tmp_path)
    save_session(
        tmp_path / "state",
        SessionState("alior", "sess-1", date(2099, 1, 1), (LinkedAccount("a"),)),
    )
    monkeypatch.delenv("EB_PSU_IP", raising=False)
    monkeypatch.setattr("gnucash_ofx.run._detect_public_ip", lambda: None)

    with pytest.raises(SystemExit, match="background mode"):
        main(["--config", str(config), "fetch", "--from", "2026-05-01", "--to", "2026-05-31"])

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "gnucash_ofx.cli.build_enablebanking_client",
        lambda **kw: seen.update(kw) or _FakeFetchClient(),
    )
    assert (
        main(
            [
                "--config",
                str(config),
                "fetch",
                "--from",
                "2026-05-01",
                "--to",
                "2026-05-31",
                "--allow-background",
            ]
        )
        == 0
    )
    assert seen["psu_headers"] is None
    run_record = json.loads(
        (tmp_path / "state" / "fetch-log.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert run_record["psu_mode"] == "background"


# --------------------------------------------------------------------------- fetch --dry-run

# Synthetic, checksum-valid Polish IBAN (unassigned bank code 9999 9999).
_DRY_IBAN = "PL03999999990000000000000005"


def _linked_alior(tmp_path: Path, valid_until: date) -> None:
    from gnucash_ofx.state import LinkedAccount, SessionState, save_session

    save_session(
        tmp_path / "state",
        SessionState(
            bank="alior",
            session_id="sess-1",
            valid_until=valid_until,
            accounts=(LinkedAccount(uid="a", iban=_DRY_IBAN, currency="PLN"),),
        ),
    )


def _refuse_to_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every route out to the network fail loudly, so a dry run proves it took none."""

    def boom(**_kw: object) -> object:
        raise AssertionError("a dry run must not reach the network")

    monkeypatch.setattr("gnucash_ofx.cli.build_enablebanking_client", boom)
    monkeypatch.setattr("gnucash_ofx.cli.build_psu_headers", boom)
    monkeypatch.setattr("gnucash_ofx.run._detect_public_ip", lambda: None)


def _dry_run_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Credentials that exist but could never authenticate - a dry run never parses the key."""
    key = tmp_path / "key.pem"
    key.write_text("not-a-real-key", encoding="utf-8")
    monkeypatch.setenv("EB_APP_ID", "app-x")
    monkeypatch.setenv("EB_PRIVATE_KEY", str(key))


def test_fetch_parses_dry_run() -> None:
    parser = _build_parser()
    args = parser.parse_args(["fetch", "--from", "2026-05-01", "--to", "2026-05-31", "--dry-run"])
    assert args.dry_run is True
    assert parser.parse_args(["fetch", "--from", "2026-05-01", "--to", "2026-05-31"]).dry_run is (
        False
    )


def test_dry_run_lists_paths_on_stdout_and_spends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    _linked_alior(tmp_path, date(2099, 1, 1))
    _refuse_to_fetch(monkeypatch)
    _dry_run_credentials(monkeypatch, tmp_path)

    exit_code = main(
        [
            "--config",
            str(config),
            "fetch",
            "--from",
            "2026-05-01",
            "--to",
            "2026-05-31",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    # stdout is the bare predicted paths, so `--dry-run > files.txt` previews the import set in
    # exactly the shape a real fetch would produce.
    predicted = tmp_path / "out" / "alior_PLN_2026_05_01-2026_05_31.ofx"
    assert captured.out == f"{predicted}\n"
    assert "Would write up to 1 OFX file(s)" in captured.err
    # ...and predicting a file is not writing one.
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "cache").exists()
    # A dry run sends no request, so it must not appear in the log of what was spent.
    assert not (tmp_path / "state" / "fetch-log.jsonl").exists()
    # The account and its currency are detail, and detail belongs on stderr.
    assert "PLN" in captured.err
    assert _DRY_IBAN not in captured.err + captured.out


def test_dry_run_redacts_the_account_number(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from gnucash_ofx.run import _redact_account

    config = _write_config(tmp_path)
    _linked_alior(tmp_path, date(2099, 1, 1))
    _refuse_to_fetch(monkeypatch)
    _dry_run_credentials(monkeypatch, tmp_path)

    main(
        [
            "--config",
            str(config),
            "fetch",
            "--from",
            "2026-05-01",
            "--to",
            "2026-05-31",
            "--dry-run",
        ]
    )
    assert _redact_account(_DRY_IBAN) in capsys.readouterr().err


def test_dry_run_reports_a_lapsed_consent_and_still_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config resolved; the consent is the user's next action, not this command's failure."""
    config = _write_config(tmp_path)
    _linked_alior(tmp_path, date(2020, 1, 1))
    _refuse_to_fetch(monkeypatch)
    _dry_run_credentials(monkeypatch, tmp_path)

    exit_code = main(
        [
            "--config",
            str(config),
            "fetch",
            "--from",
            "2026-05-01",
            "--to",
            "2026-05-31",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No OFX files would be written" in captured.err
    assert captured.out == ""
    assert "expired" in captured.err
    assert "gnucash-ofx link alior" in captured.err


def test_dry_run_still_aborts_on_an_unconfigured_bank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    _refuse_to_fetch(monkeypatch)
    _dry_run_credentials(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match="not a configured Enable Banking bank"):
        main(
            [
                "--config",
                str(config),
                "fetch",
                "--from",
                "2026-05-01",
                "--to",
                "2026-05-31",
                "--bank",
                "nope",
                "--dry-run",
            ]
        )


def test_dry_run_still_aborts_on_a_bad_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    _refuse_to_fetch(monkeypatch)
    with pytest.raises(SystemExit, match="invalid date"):
        main(
            [
                "--config",
                str(config),
                "fetch",
                "--from",
                "nope",
                "--to",
                "2026-05-31",
                "--dry-run",
            ]
        )


def test_dry_run_still_aborts_on_a_reversed_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both commands refuse the same window, so the prediction never disagrees with the fetch."""
    config = _write_config(tmp_path)
    _linked_alior(tmp_path, date(2099, 1, 1))
    _refuse_to_fetch(monkeypatch)
    _dry_run_credentials(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match="--from 2026-07-31 is after --to 2026-07-01"):
        main(
            [
                "--config",
                str(config),
                "fetch",
                "--from",
                "2026-07-31",
                "--to",
                "2026-07-01",
                "--dry-run",
            ]
        )


def test_dry_run_still_aborts_on_missing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    _linked_alior(tmp_path, date(2099, 1, 1))
    _refuse_to_fetch(monkeypatch)
    monkeypatch.delenv("EB_APP_ID", raising=False)
    monkeypatch.delenv("EB_PRIVATE_KEY", raising=False)
    with pytest.raises(SystemExit, match="EB_APP_ID"):
        main(
            [
                "--config",
                str(config),
                "fetch",
                "--from",
                "2026-05-01",
                "--to",
                "2026-05-31",
                "--dry-run",
            ]
        )


def test_dry_run_output_is_ascii(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    _linked_alior(tmp_path, date(2099, 1, 1))
    _refuse_to_fetch(monkeypatch)
    _dry_run_credentials(monkeypatch, tmp_path)

    main(
        [
            "--config",
            str(config),
            "fetch",
            "--from",
            "2026-05-01",
            "--to",
            "2026-05-31",
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    (captured.out + captured.err).encode("ascii")


def test_dry_run_collapses_one_banks_repeated_account_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bank linked before currencies were captured has the problem on every account.

    The fix is one re-link for the bank, so repeating the whole sentence per account would bury
    the banks that need something different. The accounts are still named, on one line.
    """
    from gnucash_ofx.run import _redact_account
    from gnucash_ofx.state import LinkedAccount, SessionState, save_session

    second = "PL03999999990000000000009999"  # synthetic, checksum-valid
    config = _write_config(tmp_path)
    save_session(
        tmp_path / "state",
        SessionState(
            bank="alior",
            session_id="sess-1",
            valid_until=date(2099, 1, 1),
            accounts=(
                LinkedAccount(uid="a", iban=_DRY_IBAN),
                LinkedAccount(uid="b", iban=second),
            ),
        ),
    )
    _refuse_to_fetch(monkeypatch)
    _dry_run_credentials(monkeypatch, tmp_path)

    assert (
        main(
            [
                "--config",
                str(config),
                "fetch",
                "--from",
                "2026-05-01",
                "--to",
                "2026-05-31",
                "--dry-run",
            ]
        )
        == 0
    )
    err = capsys.readouterr().err
    assert err.count("gnucash-ofx link alior") == 1
    assert "2 account(s) -" in err
    assert f"  {_redact_account(_DRY_IBAN)}, {_redact_account(second)}" in err


# ------------------------------------------------------- optional window, warnings, status --check


def test_fetch_no_longer_requires_a_date_range() -> None:
    """The retype-it-every-month habit is the mistake; the default is the fix."""
    parser = _build_parser()
    args = parser.parse_args(["fetch"])
    assert args.date_from is None
    assert args.date_to is None


def test_fetch_still_accepts_only_one_end() -> None:
    parser = _build_parser()
    args = parser.parse_args(["fetch", "--from", "2026-05-01"])
    assert args.date_from == "2026-05-01"
    assert args.date_to is None


def test_a_reversed_window_is_still_refused(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    with pytest.raises(SystemExit, match="is after"):
        main(["--config", str(config), "fetch", "--from", "2026-07-31", "--to", "2026-07-01"])


def test_dry_run_with_no_dates_resolves_a_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interlock that makes an implied window safe: it can be previewed for free."""
    config = _write_config(tmp_path)
    monkeypatch.setenv("EB_APP_ID", "app")
    key = tmp_path / "key.pem"
    key.write_bytes(b"-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("EB_PRIVATE_KEY", str(key))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "alior.json").write_text(
        json.dumps(
            {
                "version": 2,
                "bank": "alior",
                "session_id": "s",
                "valid_until": "2027-01-01",
                "accounts": [
                    {"uid": "u1", "iban": "PL69999999990000000000000001", "currency": "PLN"}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(config), "fetch", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "nothing fetched yet on record" in captured.err
    assert ".ofx" in captured.out


def test_status_check_exits_1_when_a_bank_needs_attention(tmp_path: Path) -> None:
    config = _write_config(tmp_path)  # alior is configured but never linked
    assert main(["--config", str(config), "status"]) == 0
    assert main(["--config", str(config), "status", "--check"]) == 1


def test_status_check_exits_0_when_everything_is_fine(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    far_off = (date.today().replace(year=date.today().year + 1)).isoformat()
    (state / "alior.json").write_text(
        json.dumps(
            {
                "version": 2,
                "bank": "alior",
                "session_id": "s",
                "valid_until": far_off,
                "accounts": [{"uid": "u1", "iban": "PL69999999990000000000000001"}],
            }
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(config), "status", "--check"]) == 0


def test_warning_lines_collapse_identical_per_account_messages() -> None:
    from gnucash_ofx.cli import _warning_lines
    from gnucash_ofx.run import FetchWarning

    lines = _warning_lines(
        [
            FetchWarning(
                "alior", "coverage", "never fetched 2026-07-01 -> 2026-07-31", "PL61***0001"
            ),
            FetchWarning(
                "alior", "coverage", "never fetched 2026-07-01 -> 2026-07-31", "PL61***0002"
            ),
            FetchWarning("alior", "consent", "consent expires in 12 day(s)"),
        ]
    )
    joined = "\n".join(lines)
    assert "alior: 2 account(s) - never fetched 2026-07-01 -> 2026-07-31" in joined
    assert "PL61***0001, PL61***0002" in joined
    assert "alior: consent expires in 12 day(s)" in joined


def test_the_run_log_records_the_window_as_given_not_as_resolved() -> None:
    """With an implied window there is no single answer at run level; each bank resolves its own."""
    from gnucash_ofx.cli import _logged_window

    assert _logged_window(date(2026, 5, 1), date(2026, 5, 31)) == ("2026-05-01", "2026-05-31")
    assert _logged_window(None, date(2026, 5, 31)) is None
    assert _logged_window(None, None) is None


def test_an_oversized_discovery_response_is_a_message_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_request` raises ResponseLimitExceeded for *any* endpoint, not just a fetch.

    Only `fetch_bank` translated it, so an over-cap `/aspsps`, `/application`, `/auth` or
    `POST /sessions` body escaped every handler and the user got a traceback.
    """
    from gnucash_ofx.sources.enablebanking import ResponseLimitExceeded

    config = _write_config(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise ResponseLimitExceeded("the request response was 99 bytes, over the 10-byte cap")

    monkeypatch.setattr("gnucash_ofx.cli.build_enablebanking_client", boom)

    exit_code = main(["--config", str(config), "aspsps", "--country", "PL"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "error: the request response was" in captured.err
    assert "Traceback" not in captured.err
