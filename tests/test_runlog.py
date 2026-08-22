"""Tests for the request log used to investigate rate limiting.

Append-only apart from `scrub_log`, its one in-place redaction pass.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gnucash_ofx.runlog import LOG_NAME, RunLog, load_requests, redact_uid_path, scrub_log

FIXED_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
# Synthetic, UUID-shaped like every real value measured (docs/adr-input-hardening.md §2).
SESSION_ID = "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"
ACCOUNT_UID = "0e1f2a3b-4c5d-6e7f-8a9b-0c1d2e3f4a5b"


def _records(path: Path) -> list[dict[str, object]]:
    # Read and split exactly as `load_requests` and `scrub_log` do: `splitlines()` would tear a
    # record whose ASPSP-controlled text holds U+2028, and a helper that disagrees with them about
    # where a line ends cannot check them.
    return [json.loads(line) for line in path.read_bytes().decode("utf-8").split("\n") if line]


def test_run_record_captures_mode_and_window(tmp_path: Path) -> None:
    log = RunLog(tmp_path, now=lambda: FIXED_NOW)
    log.record_run(
        command="fetch",
        banks=["alior", "alior_kantor"],
        window=("2026-07-01", "2026-07-31"),
        psu_mode="online",
        refresh=False,
    )
    (record,) = _records(log.path)
    assert record["event"] == "run"
    assert record["psu_mode"] == "online"
    assert record["banks"] == ["alior", "alior_kantor"]
    assert record["window"] == ["2026-07-01", "2026-07-31"]
    assert record["at"] == FIXED_NOW.isoformat()


def test_requests_are_appended_with_the_current_bank_and_domain(tmp_path: Path) -> None:
    log = RunLog(tmp_path, now=lambda: FIXED_NOW)
    log.set_context(bank="alior", domain="Alior Bank|PL|personal")
    log.record_request(
        method="GET", path="/accounts/x/balances", status=200, attempt=0, elapsed=0.4
    )
    log.set_context(bank="alior_kantor", domain="Alior Bank|PL|personal")
    log.record_request(
        method="GET",
        path="/accounts/y/balances",
        status=429,
        attempt=0,
        elapsed=0.2,
        api_code="ASPSP_RATE_LIMIT_EXCEEDED",
    )
    first, second = _records(log.path)
    assert (first["bank"], first["status"]) == ("alior", 200)
    assert (second["bank"], second["status"]) == ("alior_kantor", 429)
    # The whole point: both connections resolve to one domain, so the log can show whether the
    # second one's *first* request was refused.
    assert first["domain"] == second["domain"] == "Alior Bank|PL|personal"
    assert second["api_code"] == "ASPSP_RATE_LIMIT_EXCEEDED"


def test_a_platform_error_name_is_persisted_and_read_back(tmp_path: Path) -> None:
    """The envelope's `detail.error_name` (docs/enable-banking.md, Error envelope) is the only
    field separating a transient platform-wrapped bank outage (`HttpException`, N26 2026-08-19)
    from any other `ASPSP_ERROR` — without it a recurrence reads as another bare 400.
    """
    log = RunLog(tmp_path, now=lambda: FIXED_NOW)
    log.set_context(bank="n26", domain="N26|DE|personal")
    log.record_request(
        method="GET",
        path="/accounts/x/balances",
        status=400,
        attempt=0,
        elapsed=0.3,
        api_code="ASPSP_ERROR",
        error_name="HttpException",
    )
    (record,) = _records(log.path)
    assert (record["api_code"], record["error_name"]) == ("ASPSP_ERROR", "HttpException")
    _runs, (request,), skipped = load_requests(tmp_path)
    assert skipped == 0
    assert (request.status, request.api_code, request.error_name) == (
        400,
        "ASPSP_ERROR",
        "HttpException",
    )


def test_a_record_written_before_error_name_existed_reads_back_as_none(tmp_path: Path) -> None:
    """Old log lines lack the field entirely, and the log is append-only: the reader must .get it,
    never treat its absence as malformed — these lines are paid-for evidence, not old cruft.
    """
    (tmp_path / LOG_NAME).write_text(
        json.dumps(
            {
                "at": "2026-08-09T12:00:00+00:00",
                "event": "request",
                "method": "GET",
                "path": "/accounts/0e1f***4a5b/balances",
                "status": 400,
                "attempt": 0,
                "api_code": "ASPSP_ERROR",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _runs, (request,), skipped = load_requests(tmp_path)
    assert skipped == 0
    assert (request.api_code, request.error_name) == ("ASPSP_ERROR", None)


def test_only_diagnostic_headers_are_kept(tmp_path: Path) -> None:
    log = RunLog(tmp_path, now=lambda: FIXED_NOW)
    log.record_request(
        method="GET",
        path="/accounts/x/transactions",
        status=429,
        attempt=1,
        elapsed=0.1,
        headers={
            "Retry-After": "60",
            "X-RateLimit-Remaining": "0",
            "X-Request-Id": "abc",
            "Set-Cookie": "session=secret",
            "Content-Length": "12",
        },
    )
    (record,) = _records(log.path)
    kept = record["headers"]
    assert isinstance(kept, dict)
    assert kept == {"retry-after": "60", "x-ratelimit-remaining": "0", "x-request-id": "abc"}
    # An auth/session header must never reach a file we ask users to paste into an issue.
    assert "set-cookie" not in kept


def test_account_uid_is_redacted_out_of_the_path() -> None:
    assert (
        redact_uid_path(f"/accounts/{ACCOUNT_UID}/transactions")
        == "/accounts/0e1f***4a5b/transactions"
    )
    assert redact_uid_path("/aspsps") == "/aspsps"


def test_session_id_is_redacted_out_of_the_path() -> None:
    """A session id is access-granting (SECURITY.md); this file is meant to be pasteable.

    Pins docs/adr-input-hardening.md decision 1. The assertion this replaces pinned the *unmasked*
    behaviour as correct, which is what let `GET /sessions/{id}` write every bank's live session id
    verbatim into the log.
    """
    assert redact_uid_path(f"/sessions/{SESSION_ID}") == "/sessions/1a2b***5c6d"
    # `POST /sessions` carries no id, and the "short values pass" rule is shared with /accounts/.
    assert redact_uid_path("/sessions") == "/sessions"
    assert redact_uid_path("/sessions/abc") == "/sessions/abc"


def test_redaction_is_idempotent() -> None:
    """`scrub_log` re-runs redaction over the whole file, so a second pass must change nothing.

    Masking keeps the head and tail, so re-masking `abcd***wxyz` yields itself. Without this the
    mask would eat four more characters per run and accounts would stop being distinguishable —
    which is the property the head/tail shape exists to provide.
    """
    for once in (
        redact_uid_path(f"/accounts/{ACCOUNT_UID}/transactions"),
        redact_uid_path(f"/sessions/{SESSION_ID}"),
    ):
        assert redact_uid_path(once) == once
    # The boundaries, spelled out rather than left to be inferred: exactly 8 passes through, 9 is
    # the shortest value that masks, and an already-masked value fed back in is a fixed point
    # because head-4 and tail-4 of `abcd***wxyz` are the head and tail it was built from.
    assert redact_uid_path("/accounts/abcdefgh/x") == "/accounts/abcdefgh/x"
    assert redact_uid_path("/accounts/abcdefghi/x") == "/accounts/abcd***fghi/x"
    assert redact_uid_path("/accounts/abcd***wxyz/x") == "/accounts/abcd***wxyz/x"


def test_a_session_request_is_logged_already_masked(tmp_path: Path) -> None:
    log = RunLog(tmp_path, now=lambda: FIXED_NOW)
    log.record_request(
        method="GET", path=f"/sessions/{SESSION_ID}", status=200, attempt=0, elapsed=0.1
    )
    (record,) = _records(log.path)
    assert record["path"] == "/sessions/1a2b***5c6d"
    assert SESSION_ID not in log.path.read_text(encoding="utf-8")


def _leaked_log(state_dir: Path) -> Path:
    """A log as written before decision 1: the session id raw, the account uid already masked."""
    path = state_dir / LOG_NAME
    path.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False)
            for record in (
                {"at": "2026-08-09T12:00:00+00:00", "event": "run", "command": "fetch"},
                {
                    "at": "2026-08-09T12:00:01+00:00",
                    "event": "request",
                    "method": "GET",
                    "path": f"/sessions/{SESSION_ID}",
                    "status": 200,
                    "headers": {"x-request-id": "abc"},
                },
                {
                    "at": "2026-08-09T12:00:02+00:00",
                    "event": "request",
                    "method": "GET",
                    "path": "/accounts/0e1f***4a5b/transactions",
                    "status": 200,
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_scrub_masks_session_ids_already_written(tmp_path: Path) -> None:
    """docs/adr-input-hardening.md decision 1: the fix is remediation, not only prevention.

    The redaction regex alone leaves every already-written line leaking, and the re-measurement of
    2026-08-13 found live session ids on disk — so the same change has to clean the file it was
    written into.
    """
    path = _leaked_log(tmp_path)
    assert scrub_log(tmp_path) == 1
    records = _records(path)
    assert len(records) == 3  # a scrub masks; it never drops a line
    assert records[1]["path"] == "/sessions/1a2b***5c6d"
    assert SESSION_ID not in path.read_text(encoding="utf-8")
    # Everything that is not the path survives the rewrite, including the run record and the
    # allow-listed headers - the file is still the account of what was spent.
    assert records[0]["event"] == "run"
    assert (records[1]["method"], records[1]["status"]) == ("GET", 200)
    assert records[1]["headers"] == {"x-request-id": "abc"}
    # An already-masked account path is left exactly as it was.
    assert records[2]["path"] == "/accounts/0e1f***4a5b/transactions"


def test_scrub_does_not_rewrite_a_clean_log(tmp_path: Path) -> None:
    """A scrub that rewrote the file every run would be a torn-write risk taken for nothing."""
    path = _leaked_log(tmp_path)
    scrub_log(tmp_path)
    before = path.read_bytes()
    assert scrub_log(tmp_path) == 0
    assert path.read_bytes() == before


def test_scrub_keeps_a_line_it_cannot_use(tmp_path: Path) -> None:
    """Tolerated for the reason `load_requests` tolerates it: unreadable is not deletable.

    Three shapes that are not a request record — truncated JSON, valid JSON that is not an object,
    and a blank line — all pass through untouched while the leaking line beside them is masked.
    """
    path = tmp_path / LOG_NAME
    path.write_text(
        "{truncated\n"
        "[1, 2]\n"
        "\n" + json.dumps({"event": "request", "path": f"/sessions/{SESSION_ID}"}) + "\n",
        encoding="utf-8",
    )
    assert scrub_log(tmp_path) == 1
    truncated, not_an_object, blank, masked = path.read_text(encoding="utf-8").splitlines()
    assert (truncated, not_an_object, blank) == ("{truncated", "[1, 2]", "")
    assert SESSION_ID not in masked


def test_scrub_keeps_a_record_whose_text_holds_a_unicode_line_separator(tmp_path: Path) -> None:
    """`str.splitlines()` would tear this record in two and the rejoin would persist the tear.

    U+2028, U+2029 and NEL are line boundaries to `splitlines()` but are not escaped by
    `json.dumps(ensure_ascii=False)`, and `api_code` comes straight from the ASPSP's error body —
    so the lines at risk are exactly the 4xx/429 ones this log exists to explain, and a request
    that was paid for can only be re-earned by spending another.
    """
    path = tmp_path / LOG_NAME
    torn = {
        "at": "2026-08-09T12:00:00+00:00",
        "event": "request",
        "method": "GET",
        "path": "/accounts/0e1f***4a5b/transactions",
        "status": 400,
        "attempt": 0,
        "api_code": "ASPSP_ERROR\u2028injected",  # a real U+2028, spelled as an escape
    }
    leaking = {
        "at": "2026-08-09T12:00:01+00:00",
        "event": "request",
        "method": "GET",
        "path": f"/sessions/{SESSION_ID}",
        "status": 200,
        "attempt": 0,
    }
    path.write_text(
        json.dumps(torn, ensure_ascii=False)
        + "\n"
        + json.dumps(leaking, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    assert scrub_log(tmp_path) == 1

    records = _records(path)
    assert len(records) == 2  # the 400 survived rather than becoming two unparseable halves
    assert records[0] == torn  # separator included, byte for byte
    assert SESSION_ID not in path.read_text(encoding="utf-8")
    # And the reader agrees with the writer about where a line ends.
    _runs, requests, skipped = load_requests(tmp_path)
    assert skipped == 0
    assert [r.status for r in requests] == [400, 200]


def test_scrub_preserves_crlf_terminators_byte_for_byte(tmp_path: Path) -> None:
    """`state/` is synced between machines, so the rewrite must not re-flavour line endings.

    Read through `read_text()` and written through default text mode, a CRLF log scrubbed on Linux
    comes back LF from end to end: content-identical, every byte after the first change different.
    """
    path = tmp_path / LOG_NAME
    leaking = json.dumps({"event": "request", "path": f"/sessions/{SESSION_ID}"})
    keep = json.dumps({"event": "request", "path": "/accounts/0e1f***4a5b/balances"})
    path.write_bytes(f"{leaking}\r\n{keep}\r\n".encode())

    assert scrub_log(tmp_path) == 1

    raw = path.read_bytes()
    assert raw.count(b"\r\n") == 2  # both terminators still CRLF
    assert b"\n" not in raw.replace(b"\r\n", b"")  # and no bare LF crept in
    assert raw.endswith(b"\r\n")
    # The untouched line is byte-identical; the masked one differs only in its path.
    assert raw.split(b"\r\n")[1] == keep.encode()
    assert SESSION_ID not in raw.decode()
    assert scrub_log(tmp_path) == 0 and path.read_bytes() == raw


def test_scrub_keeps_a_line_holding_a_bare_carriage_return(tmp_path: Path) -> None:
    """A bare CR must not tear a record either — and `read_text()` would have hidden it.

    `read_text()` translates a lone `\\r` to `\\n` before any split sees it, so the split-on-newline
    defence covered three of the four line boundaries and not this one. `json.dumps` escapes CR, so
    such a byte can only arrive from a foreign writer; the line is then unparseable and is preserved
    whole rather than rewritten as two halves.
    """
    path = tmp_path / LOG_NAME
    # A raw CR inside a JSON string is invalid JSON whatever else the record carries, so this line
    # is unparseable either way; what is being pinned is that it stays *one* line.
    foreign = '{"at": "2026-08-09T12:00:00+00:00", "event": "request", "api_code": "A\rB"}'
    leaking = json.dumps(
        {
            "at": "2026-08-09T12:00:01+00:00",
            "event": "request",
            "method": "GET",
            "path": f"/sessions/{SESSION_ID}",
            "status": 200,
            "attempt": 0,
        }
    )
    path.write_bytes(f"{foreign}\n{leaking}\n".encode())

    assert scrub_log(tmp_path) == 1

    first, second, _empty = path.read_bytes().decode().split("\n")
    assert first == foreign  # one line still, CR intact, not two halves
    assert SESSION_ID not in second
    # And the reader agrees with the scrub about where that line ends: one unparseable record
    # counted, the good one returned. Splitting on the CR would report two losses instead of one —
    # mis-measuring the fix with the fix's own instrument.
    _runs, requests, skipped = load_requests(tmp_path)
    assert (len(requests), skipped) == (1, 1)


def test_scrub_never_costs_a_fetch(tmp_path: Path) -> None:
    """AGENTS.md: diagnostics must never be able to fail a run that was going to succeed."""
    assert scrub_log(None) == 0
    assert scrub_log(tmp_path / "does-not-exist") == 0
    not_a_directory = tmp_path / "state.txt"
    not_a_directory.write_text("not a directory", encoding="utf-8")
    assert scrub_log(not_a_directory) == 0


def test_scrub_swallows_a_failed_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A full disk while scrubbing must not abort a fetch, and must report nothing scrubbed.

    The count is what the CLI reports to the user, so it has to mean "changed on disk", not
    "would have changed" — otherwise a failed scrub tells them the leak is dealt with.
    """
    path = _leaked_log(tmp_path)
    before = path.read_bytes()

    def _fail(_src: object, _dst: object) -> None:
        raise OSError("no space left on device")

    # os.replace rather than _write_atomic itself, so the real writer runs and its cleanup path is
    # exercised: a leftover .tmp would sit in state/ for good, next to the file it failed to fix.
    monkeypatch.setattr("gnucash_ofx.runlog.os.replace", _fail)
    assert scrub_log(tmp_path) == 0
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_a_broken_log_never_breaks_a_fetch(tmp_path: Path) -> None:
    # AGENTS.md: "RunLog swallows every OSError." Instrumentation exists to protect the
    # rate-limit allowance; it must not be able to cost a run.
    log = RunLog(tmp_path / "state.txt", now=lambda: FIXED_NOW)
    (tmp_path / "state.txt").write_text("not a directory", encoding="utf-8")
    log.record_run(command="fetch", banks=[], window=None, psu_mode="online", refresh=False)
    log.record_request(method="GET", path="/aspsps", status=200, attempt=0, elapsed=0.1)


def test_disabled_log_writes_nothing(tmp_path: Path) -> None:
    log = RunLog(None, now=lambda: FIXED_NOW)
    log.record_request(method="GET", path="/aspsps", status=200, attempt=0, elapsed=0.1)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [200, 429])
def test_retries_are_distinguishable_from_first_attempts(tmp_path: Path, status: int) -> None:
    log = RunLog(tmp_path, now=lambda: FIXED_NOW)
    for attempt in range(3):
        log.record_request(
            method="GET", path="/accounts/x/balances", status=status, attempt=attempt, elapsed=0.1
        )
    assert [r["attempt"] for r in _records(log.path)] == [0, 1, 2]


def test_a_request_record_carries_the_window_it_covered(tmp_path: Path) -> None:
    """With an implied window the run record cannot say; the request records have to.

    Found by running a real fetch: the run record read `window: null` and nothing else carried it,
    so the log could not say what any request had asked for.
    """
    log = RunLog(tmp_path)
    log.set_context(
        bank="alior", domain="Alior Bank|PL|personal", window=("2026-07-27", "2026-08-09")
    )
    log.record_request(
        method="GET", path="/accounts/abc/transactions", status=200, attempt=1, elapsed=0.5
    )
    record = json.loads((tmp_path / "fetch-log.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["window"] == ["2026-07-27", "2026-08-09"]
    assert record["bank"] == "alior"


def test_a_request_with_no_window_context_records_null(tmp_path: Path) -> None:
    log = RunLog(tmp_path)
    log.set_context(bank="alior", domain="d")
    log.record_request(method="GET", path="/sessions/s", status=200, attempt=1, elapsed=0.1)
    record = json.loads((tmp_path / "fetch-log.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["window"] is None


def test_load_requests_reads_back_run_and_request_records(tmp_path: Path) -> None:
    log = RunLog(tmp_path, now=lambda: FIXED_NOW)
    log.record_run(
        command="fetch",
        banks=["alior"],
        window=("2026-07-01", "2026-07-31"),
        psu_mode="online",
        refresh=False,
    )
    log.set_context(bank="alior", domain="d", window=("2026-07-01", "2026-07-31"))
    log.record_request(
        method="GET",
        path="/accounts/abc/transactions",
        status=200,
        attempt=0,
        elapsed=0.4,
        api_code=None,
    )
    runs, requests, skipped = load_requests(tmp_path)
    assert skipped == 0
    (run,) = runs
    assert (run.command, run.banks, run.psu_mode) == ("fetch", ("alior",), "online")
    assert run.window == ("2026-07-01", "2026-07-31")
    (request,) = requests
    assert (request.bank, request.status, request.method) == ("alior", 200, "GET")
    assert request.path == "/accounts/abc/transactions"
    assert request.window == ("2026-07-01", "2026-07-31")


def test_load_requests_is_tolerant_of_a_malformed_line(tmp_path: Path) -> None:
    log = RunLog(tmp_path, now=lambda: FIXED_NOW)
    log.record_request(method="GET", path="/aspsps", status=200, attempt=0, elapsed=0.1)
    with (tmp_path / "fetch-log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
        handle.write('{"event": "request", "at": "2026-08-09T00:00:00+00:00"}\n')  # missing keys
        handle.write('{"event": "mystery-future-event", "at": "2026-08-09T00:00:00+00:00"}\n')
        handle.write("\n")  # a torn write can leave a trailing blank line
    runs, requests, skipped = load_requests(tmp_path)
    assert len(requests) == 1
    assert skipped == 2  # the unparsable line and the one missing required keys
    assert runs == []


def test_load_requests_on_a_missing_file_returns_empty(tmp_path: Path) -> None:
    runs, requests, skipped = load_requests(tmp_path / "does-not-exist")
    assert (runs, requests, skipped) == ([], [], 0)
