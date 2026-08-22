"""Command-line entry point: ``link`` / ``fetch`` / ``status``."""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import httpx

from gnucash_ofx import __version__
from gnucash_ofx.config import AppConfig, BankConfig, ConfigError, load_config, load_env
from gnucash_ofx.diagnose import diagnose
from gnucash_ofx.run import (
    ENABLEBANKING,
    BankFailure,
    DryRunReport,
    FetchReport,
    FetchWarning,
    RunError,
    aspsp_lines,
    build_enablebanking_client,
    build_psu_headers,
    complete_link,
    dry_run_enablebanking,
    fetch_enablebanking,
    parse_auth_code,
    require_ordered_window,
    require_valid_batch_size,
    start_link,
    status_report,
)
from gnucash_ofx.runlog import RunLog, scrub_log
from gnucash_ofx.sources.enablebanking import ResponseLimitExceeded


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gnucash-ofx",
        description="Pull bank transactions via Open Banking and emit OFX files for GnuCash.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config", default="config.toml", help="Path to config.toml (default: ./config.toml)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    link = sub.add_parser("link", help="Authorize a bank (one-time browser consent).")
    link.add_argument("bank", help="Bank key from config.toml, e.g. 'alior'.")

    fetch = sub.add_parser("fetch", help="Fetch transactions and write OFX files.")
    fetch.add_argument(
        "--from",
        dest="date_from",
        help="Start date (YYYY-MM-DD). Default: resume from what has already been fetched, with "
        "a few days of deliberate overlap; 89 days back when there is no record yet.",
    )
    fetch.add_argument(
        "--to",
        dest="date_to",
        help="End date (YYYY-MM-DD). Default: today.",
    )
    fetch.add_argument("--bank", default="all", help="Bank key from config.toml, or 'all'.")
    fetch.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached data and re-fetch from the bank (spends rate-limit allowance).",
    )
    fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be fetched and which files would be written, from config.toml "
        "and local state only. Makes no API call and neither reads nor writes the cache.",
    )
    fetch.add_argument(
        "--allow-background",
        action="store_true",
        help="Fetch even when your public IP cannot be determined, at the bank's much smaller "
        "background allowance (~4 requests/day). Off by default: exhausting it by accident "
        "costs hours.",
    )
    # BooleanOptionalAction, and default=None rather than False, so "not given" is distinguishable
    # from "given as off" - config.toml can set `[fetch] combine`, and --no-combine has to be able
    # to turn it off for one run (docs/adr-ofx-batch-splitting.md decision 9).
    fetch.add_argument(
        "--combine",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Write every account's statement into one OFX file instead of one file per "
        "account, so a monthly import is a single pass through GnuCash's assistant. Off by "
        "default: verified on GnuCash 5.16 only (docs/adr-combined-ofx-file.md) - per-file "
        "output remains the default GnuCash import path. Settable as [fetch] combine in "
        "config.toml; --no-combine turns it off for one run.",
    )
    fetch.add_argument(
        "--batch-size",
        type=int,
        default=None,
        metavar="N",
        help="Split each account's statements into files of at most N transactions, oldest "
        "first, so a large catch-up arrives as several review-sized imports instead of one "
        "unwieldy list. A booking date is never split, so a day with more than N transactions "
        "stays whole. Off by default; try 120 to leave ordinary runs alone and split only a "
        "multi-month catch-up. Settable as [fetch] batch_size in config.toml.",
    )
    fetch.add_argument(
        "--no-batch-size",
        action="store_true",
        help="Ignore [fetch] batch_size from config.toml for this run and write one file per "
        "account, as an unbatched fetch does.",
    )

    status = sub.add_parser("status", help="Show consent expiry and coverage per bank.")
    status.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if any bank needs attention (not linked, consent expired or expiring soon, "
        "unreadable state, or a coverage gap still inside the last 90 days). Off by default: "
        "anything already scripting 'status' expects it to exit 0.",
    )

    aspsps = sub.add_parser(
        "aspsps", help="List available banks (ASPSPs) for a country to copy exact names."
    )
    aspsps.add_argument("--country", required=True, help="ISO country code, e.g. PL.")

    return parser


def _require_enablebanking_bank(config: AppConfig, bank_key: str) -> BankConfig:
    bank = config.banks.get(bank_key)
    if bank is None:
        raise RunError(f"unknown bank '{bank_key}' (not in config.toml)")
    if bank.source != ENABLEBANKING:
        raise RunError(f"'link' only applies to Enable Banking banks, not '{bank.source}'")
    return bank


def _cmd_status(config: AppConfig, check: bool = False) -> int:
    report = status_report(config)
    for status in report:
        for line in status.lines:
            print(line)
    if not check:
        return 0
    needing = [status.bank_key for status in report if status.needs_attention]
    if not needing:
        return 0
    _progress(f"{len(needing)} bank(s) need attention: {', '.join(needing)}")
    return 1


def _cmd_aspsps(config_path: Path, country: str) -> int:
    # Discovery command: it needs credentials (.env) but not a config.toml.
    load_env(config_path)
    client = build_enablebanking_client()
    try:
        lines = aspsp_lines(client, country.strip().upper())
    finally:
        client.close()
    for line in lines:
        print(line)
    return 0


def _cmd_link(config: AppConfig, bank_key: str) -> int:
    bank = _require_enablebanking_bank(config, bank_key)
    client = build_enablebanking_client()
    try:
        url, requested_valid_until = start_link(client, bank)
        print("Open this URL, authenticate at your bank, then paste the URL you land on:\n")
        print(f"  {url}\n")
        redirected = input("Redirect URL: ").strip()
        try:
            code = parse_auth_code(redirected)
        except ValueError as exc:
            raise RunError(str(exc)) from exc
        state = complete_link(client, bank, code, requested_valid_until, config.state_dir)
    finally:
        client.close()
    print(
        f"Linked '{bank_key}': {len(state.account_ids)} account(s), "
        f"consent valid until {state.valid_until}."
    )
    return 0


def _progress(message: str) -> None:
    # Progress goes to stderr so stdout stays a clean, parseable list of written paths.
    print(message, file=sys.stderr, flush=True)


def _reporting_sleep(seconds: float) -> None:
    # The client only sleeps between rate-limit retries, so any wait is worth reporting.
    _progress(f"  rate limited by bank, waiting {seconds:.0f}s before retry...")
    time.sleep(seconds)


def _failure_lines(failure: BankFailure) -> list[str]:
    """Render one failure: what was being attempted, what it usually means, then the raw response.

    The bank's own response is always included verbatim — it is the most useful thing to see when
    debugging, and no explanation replaces it. Known codes gain a paragraph and a next step;
    unrecognised ones deliberately gain nothing but a pointer to the docs.
    """
    where = failure.account or failure.bank_key
    header = f"{failure.bank_key}: {failure.message}"
    if failure.account:
        header = f"{failure.bank_key}: {failure.message} for {where}"
    if failure.window:
        header += f" ({failure.window[0]} -> {failure.window[1]})"
    lines = [header]

    finding = diagnose(failure.status_code, failure.api_code, failure.operation)
    if finding is not None:
        lines += ["", f"  {finding.explanation}"]
        if finding.action:
            lines.append(f"  {finding.action}")
    elif failure.payload:
        lines += ["", "  No known explanation for this one; see docs/enable-banking.md."]

    if failure.payload:
        status = f" (HTTP {failure.status_code})" if failure.status_code else ""
        lines += ["", f"  Enable Banking response{status}:", f"    {failure.payload[:300]}"]
    return lines


def _warning_lines(warnings: list[FetchWarning]) -> list[str]:
    """One block per bank, with an identical per-account warning collapsed onto one line.

    A bank linked before its accounts had stable identifiers, or one whose window skipped the same
    month on every account, produces the same sentence per account and one fix for the bank — so a
    paragraph each would bury whatever else needed attention. Same shape as
    :func:`_dry_run_problem_lines`, deliberately not the same function as :func:`_failure_lines`:
    that one exists to diagnose an HTTP status and a bank's response body, and a warning has
    neither.
    """
    by_bank: dict[str, list[FetchWarning]] = defaultdict(list)
    for warning in warnings:
        by_bank[warning.bank_key].append(warning)

    lines: list[str] = []
    for bank_key, found in by_bank.items():
        lines.append("")
        by_message: dict[str, list[str]] = defaultdict(list)
        for warning in found:
            by_message[warning.message].append(warning.account or "")
        for message, accounts in by_message.items():
            named = [account for account in accounts if account]
            if not named:
                lines.append(f"{bank_key}: {message}")
                continue
            lines.append(f"{bank_key}: {len(named)} account(s) - {message}")
            lines.append(f"  {', '.join(named)}")
    return lines


def _report_warnings(warnings: list[FetchWarning]) -> None:
    """Render warnings to stderr. They never move the exit code; the files were still written."""
    if not warnings:
        return
    banks = sorted({warning.bank_key for warning in warnings})
    _progress("")
    _progress(f"{len(banks)} bank(s) need attention: {', '.join(banks)}")
    for line in _warning_lines(warnings):
        _progress(line)


def _report(config: AppConfig, report: FetchReport) -> int:
    """Print the outcome; stdout is nothing but the written paths, the rest goes to stderr.

    The count-and-directory header is for the human, so it goes where the other human-facing text
    already is. That leaves stdout literally consumable — ``fetch > files.txt | xargs`` needs no
    header skipped and no indent stripped — and leaves an empty run an empty file rather than a
    line of prose a reader would have to recognise.
    """
    if not report.written and report.failures:
        # Distinct from the empty-but-successful case: nothing was fetched, so we cannot claim
        # anything about whether there were transactions.
        _progress("No OFX files written (every bank failed - see below).")
    elif not report.written:
        _progress("No OFX files written (no transactions in the requested range).")
    else:
        _progress(f"Wrote {len(report.written)} OFX file(s) to {config.output_dir}:")
        for path in report.written:
            print(path)
        # stdout is block-buffered when it is a file or a pipe, so flush before writing the
        # stderr blocks below: with both streams pointed at one terminal the paths would
        # otherwise surface after the failures they precede.
        sys.stdout.flush()

    # Warnings first, failures last: a failure is the thing to act on now, so it should be what the
    # terminal is left showing. Neither moves the exit code except the failures.
    _report_warnings(report.warnings)

    if not report.failures:
        return 0

    banks = report.failed_banks
    _progress("")
    _progress(f"{len(banks)} bank(s) failed: {', '.join(banks)}")
    for failure in report.failures:
        _progress("")
        for line in _failure_lines(failure):
            _progress(line)
    _progress("")
    # Failures cache nothing, so the successful accounts are served from cache on a retry and
    # spend no further rate-limit allowance.
    _progress("Re-run the same command to retry only the failures.")
    return 1


def _dry_run_problem_lines(problems: list[BankFailure]) -> list[str]:
    """One block per bank, with identical per-account problems collapsed into a single line.

    A bank linked before its accounts' currencies were captured has the same problem on every one
    of its accounts, and the fix is one re-link for the bank — so a paragraph each would bury the
    banks that need something different. ``_failure_lines`` is deliberately not reused: it exists
    to diagnose an HTTP status and a bank's response payload, and a dry run has neither.
    """
    by_bank: dict[str, list[BankFailure]] = defaultdict(list)
    for problem in problems:
        by_bank[problem.bank_key].append(problem)

    lines: list[str] = []
    for bank_key, found in by_bank.items():
        lines.append("")
        by_message: dict[str, list[str]] = defaultdict(list)
        for problem in found:
            by_message[problem.message].append(problem.account or "")
        for message, accounts in by_message.items():
            named = [account for account in accounts if account]
            if not named:  # bank-scoped: not linked, consent lapsed, unreadable state file
                lines.append(f"{bank_key}: {message}")
                continue
            lines.append(f"{bank_key}: {len(named)} account(s) - {message}")
            lines.append(f"  {', '.join(named)}")
    return lines


def _report_dry_run(config: AppConfig, report: DryRunReport, batch_size: int | None = None) -> int:
    """Print the prediction; stdout is the predicted paths alone, everything else is stderr.

    Exit 0 whenever the config resolved, including for a bank whose consent has lapsed — that is
    a real finding, reported loudly, but it is the user's next action rather than this command's
    failure. The non-zero cases are the global ones, and they raise before reaching here.

    **With ``batch_size`` set, no path is predicted for the whole run** — see the explanation
    printed below. Everything else a dry run is for (a lapsed consent, a coverage gap, a bank
    that cannot be predicted at all) is still reported, because none of it depends on knowing the
    filenames.
    """
    if batch_size is not None:
        # docs/adr-ofx-batch-splitting.md decision 6. A dry run never sees transaction data, and
        # batch boundaries depend entirely on transaction counts and dates, so predicting one file
        # per account here would be confidently wrong rather than merely incomplete. This mirrors
        # the existing rule that one unpredictable account suppresses its whole bank's prediction:
        # --batch-size is global, so every account is equally unknowable and the whole run goes.
        _progress(
            "No paths predicted: --batch-size is set, and batch filenames depend on transaction "
            "counts and dates, which a dry run never sees. Everything else below still applies."
        )
    elif not report.planned:
        _progress("No OFX files would be written (see below).")
    else:
        _progress(f"Would write up to {len(report.planned)} OFX file(s) to {config.output_dir}:")
        for planned in report.planned:
            print(planned.path)
        sys.stdout.flush()  # see _report: keep the paths ahead of the stderr blocks below

    _progress("")
    _progress(
        "Dry run: nothing was fetched, and no file was written. An account with no transactions "
        "in this range writes no file, which is why the count above is an upper bound."
    )
    _report_warnings(report.warnings)
    if not report.problems:
        return 0

    banks = sorted({problem.bank_key for problem in report.problems})
    _progress("")
    _progress(f"{len(banks)} bank(s) would not produce every file: {', '.join(banks)}")
    for line in _dry_run_problem_lines(report.problems):
        _progress(line)
    return 0


def _cmd_dry_run(
    config: AppConfig,
    date_from: date | None,
    date_to: date | None,
    bank: str,
    combine: bool = False,
    batch_size: int | None = None,
) -> int:
    # Deliberately ahead of build_psu_headers (which looks up the public IP over the network), the
    # run log (an account of what was spent) and the client: a dry run must have nothing to spend.
    report = dry_run_enablebanking(
        config,
        date_from=date_from,
        date_to=date_to,
        only=None if bank == "all" else bank,
        progress=_progress,
        combine=combine,
        batch_size=batch_size,
    )
    return _report_dry_run(config, report, batch_size)


def _resolve_packaging(
    config: AppConfig,
    *,
    combine: bool | None,
    batch_size: int | None,
    no_batch_size: bool,
) -> tuple[bool, int | None]:
    """Settle ``--combine``/``--batch-size`` against ``config.toml``.

    Precedence is CLI explicit > ``config.toml`` > built-in default, and the built-in defaults do
    not move: no combining, no batching (docs/adr-ofx-batch-splitting.md decision 9).
    ``--no-combine``/``--no-batch-size`` are how a run opts out of a configured setting, which a
    bare ``store_true`` could not express.
    """
    if no_batch_size and batch_size is not None:
        raise RunError("--batch-size and --no-batch-size cannot both be given")
    # Checked here so a bad value is refused before the public-IP lookup and the run log, for the
    # same reason `require_ordered_window` is — but the function lives in `run.py` and is called
    # from both orchestration entry points too, so the library cannot disagree with the CLI.
    require_valid_batch_size(batch_size)

    resolved_combine = config.combine if combine is None else combine
    if no_batch_size:
        resolved_batch = None
    elif batch_size is not None:
        resolved_batch = batch_size
    else:
        resolved_batch = config.batch_size
    return resolved_combine, resolved_batch


def _packaging_line(combine: bool, batch_size: int | None) -> str | None:
    """How this run will package its output, or ``None`` when it is the plain default.

    Config-set packaging is invisible in ``--help`` and invisible in the command the user typed,
    so a surprising output shape would otherwise be hard to trace back (decision 9's named cost).
    Silent on a default run: a line saying "one file per account" on every ordinary fetch is noise.
    """
    if batch_size is None and not combine:
        return None
    parts = ["one file for every account's statement" if combine else "one file per account"]
    if batch_size is not None:
        parts.append(f"split into batches of at most {batch_size} transaction(s)")
    return f"packaging: {', '.join(parts)}"


def _cmd_fetch(
    config: AppConfig,
    date_from: date | None,
    date_to: date | None,
    bank: str,
    refresh: bool,
    allow_background: bool = False,
    dry_run: bool = False,
    combine: bool = False,
    batch_size: int | None = None,
) -> int:
    line = _packaging_line(combine, batch_size)
    if line is not None:
        _progress(line)
    if dry_run:
        return _cmd_dry_run(config, date_from, date_to, bank, combine, batch_size)
    only = None if bank == "all" else bank
    # Decision 1 of docs/adr-input-hardening.md is remediation as well as a fix: a log written
    # before the redaction covered `/sessions/{id}` still holds live, access-granting ids. Scrubbed
    # here rather than inside RunLog so the count can be reported - a user who has already pasted
    # that file needs to know, and only they can decide whether to re-link.
    #
    # Ahead of build_psu_headers for the reason require_ordered_window sits ahead of it too: that
    # call raises when no public IP can be found, and a leak fix that needs no network must not be
    # blocked by one.
    if scrubbed := scrub_log(config.state_dir):
        _progress(
            f"fetch log: masked an unredacted identifier on {scrubbed} earlier line(s); "
            "if you have shared that file, re-link to rotate those sessions"
        )
    # The user is present and triggering this fetch, so send PSU headers to fetch in online mode.
    psu_headers = build_psu_headers(progress=_progress, allow_background=allow_background)
    runlog = RunLog(config.state_dir)
    # Whether the run was online or background is the first thing to know when a 429 shows up:
    # without PSU headers the whole run drops to the ~4/day background cap, and the bank with the
    # most accounts trips first — which looks exactly like a shared allowance and is not.
    runlog.record_run(
        command="fetch",
        banks=[bank],
        window=_logged_window(date_from, date_to),
        psu_mode="online" if psu_headers else "background",
        refresh=refresh,
    )
    client = build_enablebanking_client(
        sleep=_reporting_sleep, psu_headers=psu_headers, observer=runlog.record_request
    )
    try:
        report = fetch_enablebanking(
            config,
            client,
            date_from=date_from,
            date_to=date_to,
            only=only,
            progress=_progress,
            refresh=refresh,
            runlog=runlog,
            combine=combine,
            batch_size=batch_size,
        )
    finally:
        client.close()
    return _report(config, report)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RunError(f"invalid date '{value}' (expected YYYY-MM-DD)") from exc


def _logged_window(date_from: date | None, date_to: date | None) -> tuple[str, str] | None:
    """What to write in the run log's `run` record: the window **as given**.

    Not the resolved one. A run record says what the user asked for, and with an implied window
    there is no single answer at this level - each bank resolves its own. The window actually
    requested is recorded per request, where the requests are.
    """
    if date_from is None or date_to is None:
        return None
    return date_from.isoformat(), date_to.isoformat()


def _parse_window(args: argparse.Namespace) -> tuple[date | None, date | None]:
    """``--from``/``--to`` as dates, refused here if they do not describe a window.

    Either may be absent, in which case the orchestration layer resolves it per bank from the
    coverage ledger. What is given is still checked here rather than deeper: ``_cmd_fetch`` looks
    up the public IP over the network and opens the run log before reaching the orchestration
    layer's own :func:`require_ordered_window`, so a reversed window left to be caught there is
    reported as a rate-limit-mode problem instead.
    """
    date_from = _parse_date(args.date_from) if args.date_from else None
    date_to = _parse_date(args.date_to) if args.date_to else None
    require_ordered_window(date_from, date_to)
    return date_from, date_to


def _dispatch(args: argparse.Namespace) -> int:
    # aspsps is a discovery command and must not require a config.toml to exist.
    if args.command == "aspsps":
        return _cmd_aspsps(Path(args.config), args.country)

    config = load_config(Path(args.config))
    if args.command == "status":
        return _cmd_status(config, args.check)
    if args.command == "link":
        return _cmd_link(config, args.bank)
    if args.command == "fetch":
        date_from, date_to = _parse_window(args)
        combine, batch_size = _resolve_packaging(
            config,
            combine=args.combine,
            batch_size=args.batch_size,
            no_batch_size=args.no_batch_size,
        )
        return _cmd_fetch(
            config,
            date_from,
            date_to,
            args.bank,
            args.refresh,
            args.allow_background,
            args.dry_run,
            combine,
            batch_size,
        )
    raise RunError(f"unknown command: {args.command}")  # pragma: no cover


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except FileNotFoundError as exc:
        raise SystemExit(f"config file not found: {exc}") from exc
    except ResponseLimitExceeded as exc:
        # _request raises this for *any* endpoint, so it can arrive from `link`/`aspsps`
        # discovery as well as from a fetch. Without this it escaped every handler as a bare
        # exception and the user got a traceback instead of a message.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (RunError, ConfigError) as exc:
        raise SystemExit(str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        # Last-resort net for the commands that do not report per-bank failures (link, aspsps);
        # `fetch` renders its own, with the bank and account in context.
        try:
            detail = str(exc.response.json())
        except ValueError:
            detail = exc.response.text
        message = (
            f"Enable Banking API error {exc.response.status_code} for {exc.request.url}: "
            f"{detail[:300]}"
        )
        finding = diagnose(exc.response.status_code, None)
        if finding is not None:
            message += f"\n{finding.explanation}"
            if finding.action:
                message += f"\n{finding.action}"
        raise SystemExit(message) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
