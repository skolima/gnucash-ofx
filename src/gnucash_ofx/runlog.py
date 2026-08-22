"""Append-only record of what each run actually sent, and what came back.

Written for one question the tool could not previously answer about itself: *is the rate-limit
allowance shared between two connections to the same institution?* The cache records successes
only, so after the fact a `429`, a `400` and a bank that was never requested are indistinguishable
— see `docs/adr-aspsp-rate-limit-domain.md` §7. Nothing here spends an API request; it only stops
throwing away the evidence a run already produced.

Three things are recorded because three things are in doubt:

- **Which requests were sent, in what order, with which bank and rate-limit domain**, so a 429 on a
  connection's *first* request — before it could have spent anything of its own — is visible.
- **The attempt number**, so a 429 that recovered under backoff (a transient or per-second limit)
  is distinguishable from one that never does (the daily cap).
- **Whether the run was in online or background mode.** PSU headers are what buy the higher limit,
  and the IP behind them comes from a best-effort lookup that can quietly fail — in which case the
  whole run drops to the ~4/day background cap, and the bank with the most accounts trips first.
  That looks exactly like a shared allowance and is not.

The file lives beside the session state, is gitignored with it, and holds no amounts, no
counterparties and no account numbers — paths have their account UID and their session id
redacted. Logging must never be able to cost a fetch, so every write failure is swallowed.

Append-only apart from exactly one rewrite: :func:`scrub_log` re-redacts the file in place, because
`GET /sessions/{id}` was logged unredacted until ``docs/adr-input-hardening.md`` decision 1 and a
regex fix alone would leave the already-written ids on disk. It masks one field per line and never
drops a line.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOG_NAME = "fetch-log.jsonl"

# Kept from a response: anything that might carry quota information, plus Enable Banking's request
# id, which is the identifier their support asks for. Everything else — cookies, auth echoes,
# content negotiation — is dropped rather than filtered later, so the file stays safe to paste.
_HEADER_PREFIXES = ("x-ratelimit", "ratelimit", "x-rate-limit")
_HEADER_NAMES = ("retry-after", "x-request-id")

# Every API path segment that carries an identifier. `/sessions/{id}` was missing until
# docs/adr-input-hardening.md decision 1, so `GET /sessions/{id}` wrote each bank's session id
# verbatim into a file whose whole purpose is to be pasteable — and a session id is access-granting
# (SECURITY.md), which an account UID is not. A new id-bearing endpoint belongs in this alternation;
# `/aspsps`, `/application`, `/auth` and `POST /sessions` carry no identifier at all.
_ID_IN_PATH = re.compile(r"/(accounts|sessions)/([^/]+)")


def redact_uid_path(path: str) -> str:
    """Mask the account UID or session id in an API path, keeping the two kinds distinguishable.

    Same shape as :func:`gnucash_ofx.run._redact_account`: a short head and tail, so two accounts
    of one bank stay tellable apart in a diagnostic. Neither identifier is a bank account number —
    an account UID identifies one, a session id grants access to it — and this file is meant to be
    pasteable into an issue.

    **Idempotent**, which :func:`scrub_log` depends on: masking keeps the head and the tail, so
    re-masking ``abcd***wxyz`` yields itself rather than eating four more characters.
    """

    def _mask(match: re.Match[str]) -> str:
        kind, ident = match.group(1), match.group(2)
        return f"/{kind}/{ident if len(ident) <= 8 else f'{ident[:4]}***{ident[-4:]}'}"

    return _ID_IN_PATH.sub(_mask, path)


def _scrubbed_line(line: str) -> str:
    """One log line with its ``path`` re-redacted, or the line back verbatim.

    ``line`` may still carry the ``\\r`` of a CRLF terminator, because the file is read without
    newline translation (see :func:`scrub_log`); it is split off and put back, so an unchanged line
    is returned byte-for-byte and a changed one keeps its terminator.

    A line this cannot parse is returned unchanged rather than dropped, for the reason
    :func:`load_requests` counts one and continues: a line we cannot read is still evidence of a
    request that was paid for, and a diagnostic must not destroy what it was pointed at.
    """
    body, terminator = (line[:-1], "\r") if line.endswith("\r") else (line, "")
    try:
        record = json.loads(body)
    except (json.JSONDecodeError, RecursionError):
        # RecursionError, not just a decode failure: json.loads recurses on nesting depth, and a
        # foreign line nested deeply enough would otherwise escape scrub_log and abort the fetch it
        # runs ahead of.
        # invariant: diagnostics must never cost a fetch. AGENTS.md#invariants
        return line
    if not isinstance(record, dict):
        return line
    path = record.get("path")
    if not isinstance(path, str):
        return line
    redacted = redact_uid_path(path)
    if redacted == path:
        return line
    record["path"] = redacted
    return json.dumps(record, ensure_ascii=False) + terminator


def scrub_log(state_dir: Path | None) -> int:
    """Re-redact an existing log in place. Returns the number of lines actually changed.

    Extending :func:`redact_uid_path` only fixes the *next* request. On any machine that has
    fetched, the file already holds session ids written before the regex covered them — measured
    live on this one (docs/adr-input-hardening.md §1) — so decision 1 is remediation of a file, not
    only prevention. Running it on every fetch is what makes the fix self-healing rather than a
    migration someone has to remember, and it is free after the first pass because
    :func:`redact_uid_path` is idempotent.

    Swallows every read, decode and write failure by returning 0, and never rewrites a file it did
    not change: the log is diagnostics, so it must not be able to cost a run, and a rewrite on
    every fetch would be a torn-write risk taken for nothing.

    **One accepted blind spot.** Records separated by a *bare* ``\\r`` read as a single unparseable
    line, so that region is left alone — an id in it stays unmasked, on this and every later fetch,
    with no warning, because nothing changed. That is the cost of never tearing a record: the two
    cases are indistinguishable without parsing, and only a foreign writer can produce either, since
    :func:`json.dumps` escapes CR and LF. Preserving a paid-for request wins, but it is a trade and
    not a free win.
    """
    if state_dir is None:
        return 0
    path = state_dir / LOG_NAME
    try:
        # invariant: read the log as bytes, never read_text() - it normalises a bare CR away and
        # re-flavours the file's terminators. AGENTS.md#invariants
        original = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        # invariant: diagnostics must never cost a fetch. AGENTS.md#invariants
        return 0
    # invariant: split on "\n" alone, never str.splitlines() - json.dumps leaves NEL/U+2028/U+2029
    # raw and api_code/error_name are ASPSP-controlled, so splitlines() would tear a paid-for
    # record in two. AGENTS.md#invariants
    lines = original.split("\n")
    scrubbed = [_scrubbed_line(line) for line in lines]
    changed = sum(1 for before, after in zip(lines, scrubbed, strict=True) if before != after)
    if not changed:
        return 0
    try:
        _write_atomic(path, "\n".join(scrubbed))
    except OSError:
        # invariant: diagnostics must never cost a fetch. AGENTS.md#invariants
        return 0
    return changed


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same dir + ``os.replace``, as ``save_session`` does.

    The log is append-only apart from this one rewrite, and it covers the *whole* file rather than
    one month's chunk — so this follows ``state.py``'s fsync-and-clean-up shape rather than
    ``cache.py``'s lighter one. A torn write here would trade a leaked session id for a truncated
    account of what the rate-limit allowance was spent on, which no request can buy back, and a
    leftover temp file would sit in ``state/`` for good.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        # invariant: the rewrite carries the terminators it read, so it writes with newline="" for
        # the same reason OfxWriter does. AGENTS.md#invariants
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def diagnostic_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """The subset of response headers worth keeping, lower-cased."""
    if not headers:
        return {}
    kept: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _HEADER_NAMES or lowered.startswith(_HEADER_PREFIXES):
            kept[lowered] = value
    return kept


class RunLog:
    """Appends one JSON object per line to ``<state_dir>/fetch-log.jsonl``.

    ``state_dir`` of ``None`` disables logging entirely, which is what the tests and any
    non-``fetch`` command use.
    """

    def __init__(
        self,
        state_dir: Path | None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._dir = state_dir
        self._now = now or (lambda: datetime.now(UTC))
        self._bank: str | None = None
        self._domain: str | None = None
        self._window: list[str] | None = None

    @property
    def path(self) -> Path | None:
        return None if self._dir is None else self._dir / LOG_NAME

    def set_context(
        self,
        *,
        bank: str | None,
        domain: str | None,
        window: tuple[str, str] | None = None,
    ) -> None:
        """Name the connection whose requests follow, and the window they cover.

        The client issues the requests and has no idea which bank it is working for, so the
        orchestration layer supplies it here rather than threading it through every call.

        ``window`` is here rather than only on the ``run`` record because a window can now be
        *implied*, and then it is resolved per bank — so there is no single answer at run level, and
        the ``run`` record honestly carries ``null``. Without this the log could no longer say what
        any request actually asked for, which is most of what it exists to explain.
        """
        self._bank = bank
        self._domain = domain
        self._window = list(window) if window else None

    def record_run(
        self,
        *,
        command: str,
        banks: Iterable[str],
        window: tuple[str, str] | None,
        psu_mode: str,
        refresh: bool,
    ) -> None:
        self._append(
            {
                "event": "run",
                "command": command,
                "banks": list(banks),
                "window": list(window) if window else None,
                "psu_mode": psu_mode,
                "refresh": refresh,
            }
        )

    def record_request(
        self,
        *,
        method: str,
        path: str,
        status: int,
        attempt: int,
        elapsed: float,
        api_code: str | None = None,
        error_name: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._append(
            {
                "event": "request",
                "bank": self._bank,
                "domain": self._domain,
                "window": self._window,
                "method": method,
                "path": redact_uid_path(path),
                "status": status,
                "attempt": attempt,
                "elapsed_s": round(elapsed, 3),
                "api_code": api_code,
                # The envelope's detail.error_name: Enable Banking's wrapped platform exception
                # (HttpException, RateLimitException), which is what tells a transient bank-down
                # ASPSP_ERROR apart from any other 400 carrying the same token. ASPSP-controlled
                # text, like api_code - the split-on-"\n" rule covers it for the same reason.
                "error_name": error_name,
                "headers": diagnostic_headers(headers),
            }
        )

    def _append(self, record: dict[str, Any]) -> None:
        if self._dir is None:
            return
        record = {"at": self._now().isoformat(), **record}
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with (self._dir / LOG_NAME).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # invariant: diagnostics must never cost a fetch - this except must stay bare and
            # silent. AGENTS.md#invariants
            # Diagnostics exist to protect the rate-limit allowance. They must never be able to
            # spend it by aborting a run that was otherwise going to succeed.
            return


def _as_window(value: Any) -> tuple[str, str] | None:
    if not value:
        return None
    first, second = value
    return str(first), str(second)


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One ``event: "run"`` line, read back."""

    at: datetime
    command: str
    banks: tuple[str, ...]
    window: tuple[str, str] | None
    psu_mode: str
    refresh: bool


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """One ``event: "request"`` line, read back. ``path`` is already redacted, as written."""

    at: datetime
    bank: str | None
    domain: str | None
    window: tuple[str, str] | None
    method: str
    path: str
    status: int
    attempt: int
    api_code: str | None
    # Absent from every line written before the field existed; read with .get, never required.
    error_name: str | None


def load_requests(state_dir: Path) -> tuple[list[RunRecord], list[RequestRecord], int]:
    """Read ``<state_dir>/fetch-log.jsonl`` back. Returns ``(runs, requests, skipped)``.

    Tolerant of a malformed or truncated line: it is counted in ``skipped`` and reading continues,
    never raised. ``RunLog`` itself can never fail to write a record it started (an ``OSError``
    during ``_append`` is swallowed before anything reaches the file), so on this machine there is
    nothing to be tolerant of yet — but the log's own contract is that a diagnostic must never be
    able to cost a run, and a reader used to *feed* diagnosis should not import a risk the writer
    was built to avoid. A line whose ``event`` is not ``"run"`` or ``"request"`` is skipped without
    being counted: an unrecognised-but-parseable event is a future format this reader does not yet
    know about, not a malformed one.
    """
    path = state_dir / LOG_NAME
    if not path.exists():
        return [], [], 0
    runs: list[RunRecord] = []
    requests: list[RequestRecord] = []
    skipped = 0
    # invariant: read the log as bytes and split on "\n" - the reader must agree with the writer
    # about where a line ends, or it cannot check it. AGENTS.md#invariants
    for line in path.read_bytes().decode("utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            at = datetime.fromisoformat(str(record["at"]))
            event = record["event"]
            if event == "run":
                runs.append(
                    RunRecord(
                        at=at,
                        command=str(record["command"]),
                        banks=tuple(record.get("banks") or ()),
                        window=_as_window(record.get("window")),
                        psu_mode=str(record["psu_mode"]),
                        refresh=bool(record["refresh"]),
                    )
                )
            elif event == "request":
                requests.append(
                    RequestRecord(
                        at=at,
                        bank=record.get("bank"),
                        domain=record.get("domain"),
                        window=_as_window(record.get("window")),
                        method=str(record["method"]),
                        path=str(record["path"]),
                        status=int(record["status"]),
                        attempt=int(record["attempt"]),
                        api_code=record.get("api_code"),
                        error_name=record.get("error_name"),
                    )
                )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            skipped += 1
    return runs, requests, skipped
