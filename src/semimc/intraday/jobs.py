"""S4's scheduled jobs (`BRIEF_INTRADAY.md`'s "Daily schedule"), Phases 5-6.

All eight of the brief's named jobs, given the further split `tasks/todo.md`'s S4
addendum records: `preflight`, `research`, `preopen`, `open_batch`/`cycle` (one shared
implementation, `run_cycle`, since they are the same operation -- `open_batch` is just
the first `cycle` of the day), `carry`, `eod_review` (Phase 9a/9b), and `weekly_review`
(Phase 10). `monitor` (not in the brief's original list, but matching its own separate
`monitor.py` file) runs standalone on a tight, model-free cron and is also the first
thing `run_cycle` calls.

`run_weekly_review` calls no model at all -- confirmed directly against the brief before
designing it: the schedule table names an agent for `research`/`eod_review` but not for
`weekly_review`, and "Agent roles" lists exactly three model roles. Accepting/rejecting/
retiring a playbook proposal is a human action (`intraday/playbook.py`, `cli.py`'s
`intraday playbook` commands), never called from here -- the brief's own "Ask first:
Accepting a playbook proposal" and "accepted or rejected by me... not automatically"
both rule out a cron job doing it.

Every `run_<verb>` function here is a plain function returning a result dataclass, never
raising for a routine outcome -- the same shape `live/{decide,execute,mark}.py` already
use, right down to taking the full `Config` (not just `Config.intraday`) as their own
`config` parameter does: `check_intraday_pretrade` needs the full object (it resolves
`kill_switch_path` via `Config.path()`), so every job function accepts `Config` and
narrows to `config.intraday` itself wherever a Phase 2-4 helper (`build_trader_context`,
`invoke_role`, `build_entry_order`, ...) expects the narrower type. No job trusts a
previous job's in-memory output: state passes through the ledger and the broker only, and
every job re-polls fresh broker/market-data state rather than assuming anything about a
prior run, the same "re-check fresh state" discipline `live/mark.py`'s own docstring
names explicitly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd
from pydantic import ValidationError

from ..config import Config, IntradayConfig
from ..data import MarketData
from ..data.realized import realized_variance
from ..execution.broker import Broker, OrderSide, Position
from ..execution.fake_broker import FakeBroker
from ..execution.market_data import AlpacaMarketDataClient, AlpacaOptionsMarketDataClient
from ..execution.news import AlpacaNewsClient
from .context import (
    ReflectionUpdate,
    WatchlistCandidate,
    build_researcher_context,
    build_reviewer_context,
    build_trader_context,
)
from .ledger import IntradayLedger
from .llm import ClaudeCliClient, DailyBudgetExhausted, invoke_role
from .monitor import check_invalidation_and_time_stops
from .options import OptionPricing, OptionQuote, price_contract
from .orders import (
    build_close_order,
    build_entry_order,
    build_replacement_stop_order,
    build_trim_order,
    find_and_cancel_resting_stop,
    submit_order_with_fault_isolation,
)
from .pretrade import IntradayPretradeContext, check_intraday_pretrade, check_spread_gate
from .proposals import (
    OPEN_OR_ADD_ACTIONS,
    TradeProposal,
    ValidationContext,
    validate_proposal,
    whole_shares,
)
from .reflect import _entry_and_proposal, record_mechanical_reflection
from .security import sanitize_and_log
from .session import session_snapshot


def midpoint_price(quote: Any) -> float | None:
    """`None`-safe NBBO midpoint of a `Quote` -- public (not `_`-prefixed) because the
    `intraday monitor` CLI command (Phase 5) needs the identical price derivation this
    module already uses internally, rather than a second copy."""
    if quote is None or quote.bid_price is None or quote.ask_price is None:
        return None
    return (quote.bid_price + quote.ask_price) / 2.0


def quote_map(
    symbols: set[str], market_data: AlpacaMarketDataClient, now: dt.datetime
) -> dict[str, Any]:
    """Best-effort quotes for `symbols` -- a symbol with no coverage is simply absent,
    same "a single missing quote must not crash a whole report" tolerance
    `market_data.py::arrival_price` already documents."""
    quotes: dict[str, Any] = {}
    for symbol in symbols:
        quote = market_data.latest_quote(symbol, at=now)
        if quote is not None and quote.bid_price is not None and quote.ask_price is not None:
            quotes[symbol] = quote
    return quotes


def _apply_spread_gate(
    rows: list[dict[str, Any]],
    market_data: AlpacaMarketDataClient,
    config: IntradayConfig,
    ledger: IntradayLedger,
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[tuple[str, str]]]:
    """Splits `rows` (each with a `"symbol"` key) into what's currently tradable and
    what got dropped, logging a warning incident per drop. Shared by `run_preopen` and
    `run_cycle` so both apply the identical gate -- `run_preopen`'s result is never
    trusted by `run_cycle`, but the *logic* is the same either way."""
    tradable: list[dict[str, Any]] = []
    quotes: dict[str, Any] = {}
    dropped: list[tuple[str, str]] = []
    for row in rows:
        symbol = row["symbol"]
        quote = market_data.latest_quote(symbol, at=now)
        if quote is None or quote.bid_price is None or quote.ask_price is None:
            reason = f"{symbol}: no quote available"
            dropped.append((symbol, reason))
            ledger.record_incident("warning", reason)
            continue
        reason = check_spread_gate(
            symbol, quote.bid_price, quote.ask_price, quote.timestamp, now, config
        )
        if reason is not None:
            dropped.append((symbol, reason))
            ledger.record_incident("warning", reason)
            continue
        quotes[symbol] = quote
        tradable.append(row)
    return tradable, quotes, dropped


def _original_proposal_payload(ledger: IntradayLedger, symbol: str) -> dict[str, Any] | None:
    """The payload of the proposal that opened `symbol`'s currently-held position, if
    traceable -- what `run_cycle`'s trim path (the remainder's invalidation level) and
    `run_carry` (a carried position's horizon/confidence) both read. Thin wrapper around
    `reflect.py::_entry_and_proposal`, the same lookup `record_mechanical_reflection`
    uses (Phase 9a) -- one shared implementation, not two independently-maintained
    copies of "find the entry that opened this position.\""""
    found = _entry_and_proposal(ledger, symbol)
    return found[1]["payload"] if found is not None else None


def turnover_so_far_usd(ledger: IntradayLedger, as_of: pd.Timestamp, equity: float) -> float:
    """Approximates today's turnover from proposed (not filled) notional of every
    accepted proposal so far -- the same accepted-proposals-today read
    `validate_proposal`'s own budget checks already use, so this stays consistent with
    what the validator enforces rather than a second, independently-derived figure.

    Not `_`-prefixed (Phase 5-6 had it as `_turnover_so_far_usd`, jobs.py-only) since
    `intraday/placebo.py` (Phase 7) needs the identical computation against its own shadow
    ledger -- shared utility, not private to this module anymore."""
    accepted = [row for row in ledger.proposals_for(as_of) if row["status"] == "accepted"]
    total = 0.0
    for row in accepted:
        size_pct = row["payload"].get("size_pct")
        if size_pct is not None:
            total += size_pct * equity
    return total


def mark_broker_to_market(
    shadow_broker: FakeBroker,
    shadow_ledger: IntradayLedger,
    market_data: AlpacaMarketDataClient,
    starting_equity: float,
    now: dt.datetime,
) -> None:
    """Sets a `FakeBroker`'s `.cash`/`.equity` fresh, every call, from `starting_equity`
    plus `shadow_ledger.total_realized_cash_flow()` (every fill this ledger has ever
    recorded) plus a live mark of whatever's currently held. A missing quote for a held
    symbol falls back to its own `avg_entry_price` -- never crashes on one bad quote, same
    tolerance `quote_map` already uses.

    Deliberately recomputed from scratch rather than incrementally maintained: this is
    the one place equity/cash truth lives, so it can never drift out of sync with a
    persisted running total, and it stays correct no matter which code path produced a
    fill.

    Promoted here from `intraday/placebo.py::_mark_to_market` (Phase 7) in Phase 11 --
    same "second caller needs the identical computation" reasoning `turnover_so_far_usd`
    just above was already promoted for: Stage 1's shadow mode needs the exact same
    recompute-from-ledger-history mark-to-market the placebo already built, against its
    own real-loop shadow broker/ledger rather than the placebo's. Nothing about the logic
    itself is placebo-specific -- only which `FakeBroker` instance and which
    `IntradayLedger` get passed in differs between the two callers.
    """
    cash = starting_equity + shadow_ledger.total_realized_cash_flow()
    market_value = 0.0
    for position in shadow_broker.list_positions():
        quote = market_data.latest_quote(position.symbol, at=now)
        price = midpoint_price(quote)
        if price is None:
            price = position.avg_entry_price
        market_value += position.qty * price
    shadow_broker.cash = cash
    shadow_broker.equity = cash + market_value


# -- preflight --------------------------------------------------------------------------


# "market is closed" is permanently, unconditionally true for preflight specifically --
# it runs on its own fixed ~06:45 Central schedule, before the market opens (~08:30) by
# design, every single day regardless of anything else -- so it never carries information
# worth a daily failure email and is always a routine note, not a failure.
_ALWAYS_ROUTINE_PRETRADE_NOTE = "market is closed"


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Phase 10: `ledger.current_playbook_version()`, `None` before any acceptance. Kept
    # separate from `notes` deliberately -- every `notes` entry is logged as a `"warning"`
    # incident below, and "playbook version 5 loaded" is routine information, not a
    # warning worth polluting the incident log (and therefore every later eod_review's
    # incident count) with on every single preflight run.
    playbook_version: int | None = None
    # Phase 11: which mode preflight actually checked under -- surfaced for the same
    # reason `playbook_version` is: informational context for reading the CLI output,
    # not a pass/fail input.
    shadow_mode: bool = True


def run_preflight(
    config: Config,
    broker: Broker,
    market_data: AlpacaMarketDataClient,
    news_client: AlpacaNewsClient,
    ledger: IntradayLedger,
    now: dt.datetime,
) -> PreflightResult:
    """A canary, not a gate other jobs depend on -- every later job re-runs
    `check_intraday_pretrade` (or an order-level check) itself fresh, the same "no job
    trusts a prior job" discipline s0/s3 already use. Exists so a 06:45 GitHub Actions
    failure email catches a dead account or feed before the day's real jobs waste a
    cycle on it -- which means it must not fire on conditions that are routine and
    by-design, or the alert stops meaning anything.

    Checks: `check_intraday_pretrade` (kill switch, trading_enabled, registered account,
    account status, market open), yesterday's reconciliation status (if any), a
    market-data smoke test, and (Phase 6) a news-feed smoke test. Two kinds of routine
    decline are logged as `"warning"` incidents in `notes` rather than counted toward
    `passed`: `_ALWAYS_ROUTINE_PRETRADE_NOTE` (see its own comment), and -- while
    `config.intraday.trading_enabled` is still `False` -- every other
    `check_intraday_pretrade` finding, since nothing about an account that isn't live yet
    can be "wrong" in a way worth an email (an unregistered account is definitionally
    always a mismatch before the registration itself happens at go-live). Once
    `trading_enabled` flips `True`, those same findings (kill switch, account mismatch,
    account status) become real failures again -- by then none of them should still be
    true, so if one is, it means something. "Playbook version loaded" (brief) is
    reported via `PreflightResult.playbook_version` (Phase 10, `ledger.
    current_playbook_version()`) -- always informational, never a failure; an empty
    playbook is normal for a long time, the same reasoning every other pre-go-live
    finding here is already treated as routine.

    Phase 11: this command's own broker is always the real one (unlike `open-batch`/
    `cycle`/`carry`/`monitor`, which switch to a shadow `FakeBroker` under `cfg.intraday.
    shadow_mode` -- see `cli.py`), so an account mismatch/status finding here is still
    real, useful infrastructure information -- worth surfacing, just not worth a failure
    email while `shadow_mode=True` means no real order was ever going to be placed
    against it anyway. Found by diffing two `check_intraday_pretrade` calls (`shadow=
    False` -- the full set -- against `shadow=True`) rather than hardcoding which
    message strings are "account-related" a second time here: whatever `shadow=True`
    would have suppressed becomes an extra routine note, on top of `_ALWAYS_ROUTINE_
    PRETRADE_NOTE`, exactly when `config.intraday.shadow_mode` is set. The kill switch
    and `trading_enabled` are never in that suppressed set (`shadow` only ever affects
    the account-identity checks), so they're untouched by this and keep flowing through
    the unchanged trading_enabled-based split below.
    """
    context = IntradayPretradeContext(now=now, config=config, broker=broker)
    pretrade_results = check_intraday_pretrade(context)
    shadow_suppressed = set(pretrade_results) - set(check_intraday_pretrade(context, shadow=True))
    notes = [
        r
        for r in pretrade_results
        if r == _ALWAYS_ROUTINE_PRETRADE_NOTE
        or (config.intraday.shadow_mode and r in shadow_suppressed)
    ]
    remaining = [r for r in pretrade_results if r not in notes]
    if config.intraday.trading_enabled:
        failures = remaining
    else:
        failures = []
        notes += remaining

    reconciliation = ledger.latest_reconciliation()
    if reconciliation is not None and reconciliation["status"] != "ok":
        failures.append(f"latest reconciliation status is {reconciliation['status']!r}, not 'ok'")

    try:
        market_data.latest_quote("SPY", at=now)
    except Exception as exc:
        failures.append(f"market data unreachable: {exc}")

    try:
        news_client.get_news(["SPY"], start=now - dt.timedelta(hours=1), end=now)
    except Exception as exc:
        failures.append(f"news feed unreachable: {exc}")

    for note in notes:
        ledger.record_incident("warning", note)
    for failure in failures:
        ledger.record_incident("error", failure)

    return PreflightResult(
        passed=not failures,
        failures=failures,
        notes=notes,
        playbook_version=ledger.current_playbook_version(),
        shadow_mode=config.intraday.shadow_mode,
    )


# -- research (Phase 6) ------------------------------------------------------------------


def _session_context(
    symbols: list[str],
    market_data: AlpacaMarketDataClient,
    config: IntradayConfig,
    ledger: IntradayLedger,
    now: dt.datetime,
) -> dict[str, dict[str, Any]]:
    """The Trader's `session` section (`intraday/session.py`) for the symbols in view.
    One bars request covers today and the volume lookback: `volume_lookback_sessions`
    trading days need about 7/5 as many calendar days, plus slack for holidays. A failed
    request is a warning and an empty section -- the cycle still runs on prices alone,
    as it did before this existed."""
    if not symbols:
        return {}
    settings = config.session_data
    now_utc = now if now.tzinfo else now.replace(tzinfo=dt.UTC)
    calendar_days = settings.volume_lookback_sessions * 7 // 5 + 5
    try:
        bars = market_data.bars(
            symbols,
            start=now_utc - dt.timedelta(days=calendar_days),
            end=now_utc,
            minutes=settings.bar_minutes,
        )
    except Exception as exc:  # noqa: BLE001 - missing context must not cost the cycle
        ledger.record_incident("warning", f"session bars unavailable: {exc}")
        return {}
    return session_snapshot(
        bars, now_utc, settings.opening_range_minutes, settings.volume_lookback_sessions
    )


def premarket_moves(
    tickers: list[str],
    market_data_pit: MarketData,
    market_data: AlpacaMarketDataClient,
    now: dt.datetime,
    diagnostics: dict[str, list[str]] | None = None,
) -> dict[str, float]:
    """Percent move from the last cached close to the latest Alpaca quote at `now`.
    `market_data_pit`'s cache is refreshed by the workflow's own `semimc fetch` step
    before `research` runs and today's session hasn't opened yet, so its most recent
    cached row already *is* the prior close -- no separate "yesterday" lookup needed. A
    ticker missing either side is simply absent, same "a single missing quote must not
    crash a whole report" tolerance `quote_map` already uses.

    Despite the name, the same call also gives a full day's return when `now` is near or
    after the close -- "prior cached close to whatever quote is live right now" doesn't
    care what time of day it's called at. `eod_report.py`'s SOXX benchmark (Phase 9a)
    reuses it exactly this way, which is also why this dropped its leading underscore --
    a second real caller outside `jobs.py`, not `jobs.py`-private anymore (same reasoning
    Phase 7 already used renaming `_turnover_so_far_usd`).

    **A missing quote falls back to today's latest trade** (2026-09-30): the 07:30 CT
    research run got no pre-market move for any ticker, since a +/-5 minute window
    rarely holds an IEX pre-market quote. Only a trade printed on `now`'s New York date
    and not after `now` is used, so yesterday's after-hours print never stands in for
    this morning. `diagnostics`, when passed, collects which tickers had no prior close,
    were priced from a trade, or got no price at all -- what `run_research` logs so the
    next empty result says why."""
    prior_close = market_data_pit.spot(tickers)
    moves: dict[str, float] = {}
    trades: dict[str, float] | None = None  # fetched once, only if some quote is missing
    notes: dict[str, list[str]] = {"no_prior_close": [], "from_trade": [], "no_price": []}
    for ticker in tickers:
        prior = prior_close.get(ticker)
        if prior is None or pd.isna(prior) or prior <= 0:
            notes["no_prior_close"].append(ticker)
            continue
        current = midpoint_price(market_data.latest_quote(ticker, at=now))
        if current is None:
            if trades is None:
                trades = _todays_trade_prices(tickers, market_data, now)
            current = trades.get(ticker)
            if current is not None:
                notes["from_trade"].append(ticker)
        if current is None:
            notes["no_price"].append(ticker)
            continue
        moves[ticker] = (current - prior) / prior
    if diagnostics is not None:
        diagnostics.update(notes)
    return moves


def _todays_trade_prices(
    tickers: list[str], market_data: AlpacaMarketDataClient, now: dt.datetime
) -> dict[str, float]:
    """Latest trade per ticker, kept only if it printed on `now`'s New York date, at or
    before `now`. A failed lookup is an empty result, never a crash -- the same tolerance
    as a single missing quote."""
    try:
        latest = market_data.latest_trades(tickers)
    except Exception:  # noqa: BLE001 - a fallback must never take research down with it
        return {}
    now_utc = pd.Timestamp(now if now.tzinfo else now.replace(tzinfo=dt.UTC))
    today = now_utc.tz_convert("America/New_York").date()
    prices: dict[str, float] = {}
    for ticker, (price, printed_at) in latest.items():
        stamp = pd.Timestamp(printed_at)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp
        if stamp <= now_utc and stamp.tz_convert("America/New_York").date() == today:
            prices[ticker] = price
    return prices


def _upcoming_earnings(
    tickers: list[str], market_data_pit: MarketData, as_of: pd.Timestamp, lookahead_days: int
) -> dict[str, str]:
    """Nearest scheduled report date within `lookahead_days`, per ticker with one,
    **including today**. The report *date* is known ahead of time (`data/earnings.py`'s
    own point-in-time reasoning); only the *reaction* is what the Researcher has to weigh
    -- not a leak. Found 2026-09-30: this used `date > today`, so a same-day report (the
    one that matters most to a day's trading) was dropped -- MU reported after that day's
    close and the Researcher was told nothing was coming up."""
    if market_data_pit.earnings is None:
        return {}
    stamp = pd.Timestamp(as_of).normalize()
    horizon = stamp + pd.Timedelta(days=lookahead_days)
    upcoming: dict[str, str] = {}
    for ticker in tickers:
        rows = market_data_pit.earnings.for_ticker(ticker)
        future = rows[(rows["date"] >= stamp) & (rows["date"] <= horizon)]
        if not future.empty:
            upcoming[ticker] = str(pd.Timestamp(future.iloc[0]["date"]).date())
    return upcoming


def _realized_vol(tickers: list[str], market_data_pit: MarketData) -> dict[str, float]:
    """Latest Garman-Klass daily variance, annualized -- a deterministic, no-fit
    volatility snapshot. Deliberately not a live GARCH/HAR/regime re-fit for the whole
    universe every morning: no existing daily-refresh job to hang that on, and it would be
    a real latency/cost addition deserving its own design pass (Phase 6 plan's scope
    decision 2). A ticker with no cached intraday range is simply absent."""
    vol: dict[str, float] = {}
    for ticker in tickers:
        if not market_data_pit.has_ohlc(ticker):
            continue
        try:
            series = realized_variance(market_data_pit.ohlc, ticker)
        except ValueError:
            continue
        if series.empty:
            continue
        vol[ticker] = (float(series.iloc[-1]) * 252) ** 0.5
    return vol


def _cap_news_per_symbol(articles: list[Any], max_per_symbol: int) -> list[Any]:
    """Caps how many articles reach the model's context per symbol, most recent first --
    BRIEF_INTRADAY.md's "a short list of high-quality candidates is worth more than a long
    list," and it directly bounds Researcher call cost. Every fetched article is still
    recorded in `headlines_seen` regardless of this cap (`run_research` iterates the
    uncapped list for that) -- this cap is about context size, not dedup completeness."""
    counts: dict[str, int] = {}
    capped: list[Any] = []
    for article in sorted(articles, key=lambda a: a.created_at, reverse=True):
        symbols = article.symbols or ["_none_"]
        if all(counts.get(s, 0) >= max_per_symbol for s in symbols):
            continue
        for s in symbols:
            counts[s] = counts.get(s, 0) + 1
        capped.append(article)
    return capped


@dataclass(frozen=True)
class ResearchResult:
    brief_recorded: bool = False
    watchlist_size: int = 0
    skipped_reason: str | None = None


def run_research(
    config: Config,
    broker: Broker,
    market_data: AlpacaMarketDataClient,
    news_client: AlpacaNewsClient,
    market_data_pit: MarketData,
    ledger: IntradayLedger,
    llm_client: ClaudeCliClient,
    as_of: pd.Timestamp,
    cycle: str,
    now: dt.datetime,
) -> ResearchResult:
    """Once per trading day: reads overnight news, pre-market moves, the earnings
    calendar, and a realized-vol snapshot, and writes a morning brief plus a watchlist
    (`BRIEF_INTRADAY.md`'s `research` row). Two of the brief's named inputs are
    deliberately not built -- analyst actions (no data source exists anywhere in this
    project; the brief itself lists this as an open question, not a settled requirement)
    and a live daily re-fit of the project's forecast/vol models (see `_realized_vol`).

    Idempotent per `as_of`: if `ledger.watchlist_for(as_of)` is already non-empty, this is
    a repeat call (most likely a retried GitHub Actions attempt) and returns immediately
    without spending anything -- the same `ON CONFLICT DO NOTHING` reasoning
    `record_session_open_equity` already uses, generalized to "don't re-call the model."

    Passes `check_market_open=False` (Phase 11) -- research runs at 07:30 Central, before
    the 08:30 open, so the default market-open check would otherwise fail every single
    call regardless of `trading_enabled`, a real bug found while building Stage 1's
    shadow mode (every existing test happened to use `FakeBroker()`'s default
    `market_open=True`, which is why it went uncaught).
    """
    pretrade_failures = check_intraday_pretrade(
        IntradayPretradeContext(now=now, config=config, broker=broker), check_market_open=False
    )
    if pretrade_failures:
        for failure in pretrade_failures:
            ledger.record_incident("error", failure)
        return ResearchResult(skipped_reason="; ".join(pretrade_failures))

    if ledger.watchlist_for(as_of):
        return ResearchResult(skipped_reason=f"research already ran for {as_of}")

    intraday = config.intraday
    research_cfg = intraday.research
    tickers = config.universe.tickers

    news_start = now - dt.timedelta(hours=research_cfg.news_lookback_hours)
    articles = news_client.get_news(tickers, start=news_start, end=now)
    # Sanitize each article's free text exactly once, up front: `capped_articles` below is
    # a subset of `articles`, and the headline-logging loop further down iterates the full
    # list again -- scanning per use site would double-scan (and double-incident) any
    # article present in both. Every downstream reader looks up the already-clean text by
    # id instead of touching `article.headline`/`.summary` directly.
    clean_text = {
        article.id: (
            sanitize_and_log(
                article.headline, ledger=ledger, source=article.source, context="research news"
            ),
            sanitize_and_log(
                article.summary, ledger=ledger, source=article.source, context="research news"
            ),
        )
        for article in articles
    }
    capped_articles = _cap_news_per_symbol(articles, research_cfg.max_headlines_per_symbol)
    news_sections = [
        {
            "headline": clean_text[article.id][0],
            "summary": clean_text[article.id][1],
            "symbols": article.symbols,
            "created_at": str(article.created_at),
            "source": article.source,
        }
        for article in capped_articles
    ]

    premarket_notes: dict[str, list[str]] = {}
    premarket_moves_today = premarket_moves(
        tickers, market_data_pit, market_data, now, diagnostics=premarket_notes
    )
    # One line per morning saying how many pre-market moves the Researcher actually got,
    # and why the rest are missing -- 2026-09-30's brief said "data is thin" with no way
    # to tell which input had failed.
    summary = f"premarket moves: {len(premarket_moves_today)}/{len(tickers)} tickers priced"
    for label, key in (
        ("from latest trade", "from_trade"),
        ("no prior close", "no_prior_close"),
        ("no price", "no_price"),
    ):
        if premarket_notes.get(key):
            summary += f"; {label}: {', '.join(premarket_notes[key])}"
    ledger.record_incident("info", summary)
    upcoming_earnings = _upcoming_earnings(
        tickers, market_data_pit, as_of, research_cfg.earnings_lookahead_days
    )
    realized_vol = _realized_vol(tickers, market_data_pit)

    try:
        context = build_researcher_context(
            as_of,
            cycle,
            intraday,
            news_sections,
            premarket_moves_today,
            upcoming_earnings,
            realized_vol,
        )
        response = invoke_role(
            "researcher",
            as_of=as_of,
            cycle=cycle,
            config=intraday,
            ledger=ledger,
            client=llm_client,
            context=context,
        )
    except DailyBudgetExhausted as exc:
        return ResearchResult(skipped_reason=str(exc))

    payload = response.structured_output if isinstance(response.structured_output, dict) else None
    if payload is None or "morning_brief" not in payload or "watchlist" not in payload:
        ledger.record_incident(
            "error", "malformed researcher response: missing morning_brief/watchlist"
        )
        return ResearchResult(skipped_reason="malformed researcher response")

    candidates: list[WatchlistCandidate] = []
    for item in payload.get("watchlist", []):
        try:
            candidates.append(WatchlistCandidate(**item))
        except (ValidationError, TypeError) as exc:
            ledger.record_incident("error", f"malformed watchlist candidate: {exc}")

    ledger.record_brief(as_of, {"text": payload["morning_brief"]})
    for candidate in candidates:
        # `WatchlistCandidate`'s own free-text fields are the Researcher model's output,
        # not raw external text -- but if an injected headline ever got echoed into them
        # despite the untrusted-label defense, this is what stops it reaching the Trader's
        # own "watchlist" context unfiltered (context.py labels that section untrusted too).
        ledger.record_watchlist_item(
            as_of,
            candidate.symbol,
            sanitize_and_log(
                candidate.thesis, ledger=ledger, source="researcher", context="watchlist thesis"
            ),
            entry_trigger=sanitize_and_log(
                candidate.entry_trigger,
                ledger=ledger,
                source="researcher",
                context="watchlist entry_trigger",
            ),
            invalidation=sanitize_and_log(
                candidate.invalidation,
                ledger=ledger,
                source="researcher",
                context="watchlist invalidation",
            ),
            size_band=sanitize_and_log(
                candidate.size_band,
                ledger=ledger,
                source="researcher",
                context="watchlist size_band",
            ),
            horizon=candidate.horizon,
            expected_move=candidate.expected_move,
            expected_cost_bps=candidate.expected_cost_bps,
        )

    for article in articles:
        ledger.record_headline_seen(
            as_of,
            "alpaca_news",
            str(article.id),
            symbols=article.symbols,
            headline=clean_text[article.id][0],
        )

    return ResearchResult(brief_recorded=True, watchlist_size=len(candidates))


# -- preopen ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PreopenResult:
    tradable: list[str] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    new_headlines: list[tuple[str, str]] = field(default_factory=list)


def run_preopen(
    config: Config,
    market_data: AlpacaMarketDataClient,
    news_client: AlpacaNewsClient,
    ledger: IntradayLedger,
    as_of: pd.Timestamp,
    now: dt.datetime,
) -> PreopenResult:
    """Refreshes a quote per watchlist symbol and runs `check_spread_gate`, logging a
    warning incident per drop -- read-only/logging only. `open_batch`/`cycle` re-run the
    identical gate themselves rather than trusting this result, so a missed or late
    `preopen` run never blocks trading.

    Also refreshes headlines per watchlist symbol (Phase 6): a genuinely new article (not
    already in `headlines_seen`) is recorded and logged as an info incident, but never
    used here to drop a candidate -- `preopen` is a *deterministic* refresh (the brief's
    own word), and judging whether a headline breaks a thesis is exactly the kind of call
    this project reserves for the model. `run_cycle`'s `build_trader_context` call is what
    actually lets the Trader see and react to it.
    """
    watchlist = ledger.watchlist_for(as_of)
    intraday = config.intraday

    lookback = dt.timedelta(hours=intraday.research.preopen_lookback_hours)
    new_headlines: list[tuple[str, str]] = []
    for row in watchlist:
        symbol = row["symbol"]
        for article in news_client.get_news([symbol], start=now - lookback, end=now):
            headline_hash = str(article.id)
            if ledger.headline_already_seen("alpaca_news", headline_hash):
                continue
            clean_headline = sanitize_and_log(
                article.headline, ledger=ledger, source=article.source, context="preopen headline"
            )
            ledger.record_headline_seen(
                as_of,
                "alpaca_news",
                headline_hash,
                symbols=article.symbols,
                headline=clean_headline,
            )
            ledger.record_incident("info", f"{symbol}: new headline -- {clean_headline}")
            new_headlines.append((symbol, clean_headline))

    tradable, _quotes, dropped = _apply_spread_gate(watchlist, market_data, intraday, ledger, now)
    return PreopenResult(
        tradable=[row["symbol"] for row in tradable], dropped=dropped, new_headlines=new_headlines
    )


# -- open_batch / cycle (shared implementation) ------------------------------------------


@dataclass
class CycleResult:
    proposals_seen: int = 0
    proposals_accepted: int = 0
    proposals_rejected: int = 0
    orders_submitted: list[str] = field(default_factory=list)
    order_failures: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def _price_option_proposal(
    proposal: TradeProposal,
    options_market_data: AlpacaOptionsMarketDataClient,
    spot: float,
    rate: float,
    as_of: pd.Timestamp,
) -> tuple[OptionQuote | None, OptionPricing | None]:
    """Fetches this one proposal's own contract quote (Phase 12b) and prices it.
    `(None, None)` on no quote (an illiquid contract, a bad strike/expiry, or an
    unentitled feed with nothing to fall back to) rather than raising --
    `validate_option_proposal`'s own "no options pricing available for this proposal"
    failure already covers that case cleanly, the same "a single missing quote must not
    crash a whole cycle" tolerance `quote_map` already applies to equities."""
    market_quote = options_market_data.quote_contract(
        proposal.symbol, proposal.expiry, proposal.strike, proposal.right, as_of
    )
    if market_quote is None:
        return None, None
    quote = OptionQuote(
        bid=market_quote.bid,
        ask=market_quote.ask,
        open_interest=market_quote.open_interest,
        volume=market_quote.volume,
        feed=market_quote.feed,
    )
    pricing = price_contract(proposal, quote, spot, rate, as_of)
    return quote, pricing


def run_cycle(
    cycle: str,
    config: Config,
    broker: Broker,
    ledger: IntradayLedger,
    market_data: AlpacaMarketDataClient,
    llm_client: ClaudeCliClient,
    as_of: pd.Timestamp,
    now: dt.datetime,
    market_data_pit: MarketData | None = None,
    options_market_data: AlpacaOptionsMarketDataClient | None = None,
) -> CycleResult:
    """One Trader cycle: monitor -> tranche continuation -> propose -> validate ->
    execute. `open_batch` and `cycle` (the CLI/workflow layer) both call this with a
    different `cycle` label -- `open_batch` is simply the first cycle of the day.

    Nothing here is a new order-construction primitive; every action reuses Phase 3-4's
    builders (`build_entry_order`/`build_close_order`/`build_trim_order`/
    `build_replacement_stop_order`) and Phase 2's context/LLM wrapper
    (`build_trader_context`/`invoke_role`). This function only sequences them.

    Passes `shadow=config.intraday.shadow_mode` (Phase 11) -- in Stage 1, `broker` is a
    `FakeBroker` with no real account identity to match, so the pretrade gate skips that
    check the same way `intraday/placebo.py::run_placebo_cycle` already does. Everything
    else about this function is unchanged by shadow mode: the same model call, the same
    `validate_proposal` risk controls, the same order-construction primitives -- only
    which broker actually receives the resulting `submit_order` call differs, decided by
    the caller (`cli.py`), not here.

    `market_data_pit`/`options_market_data` (Phase 12b, Rollout Stage 4) are both `None`
    by default -- every existing equity-only caller and test is unaffected. `cli.py` only
    builds and passes them once `config.intraday.options.shadow_pricing` or `.enabled` is
    set; when a raw proposal names `instrument="option"` and neither is available, it
    prices as `(None, None)` the same as a genuinely empty quote would, and
    `validate_option_proposal` fails it cleanly ("no options pricing available") rather
    than crashing. An accepted option proposal never reaches `build_entry_order`/
    `submit_order_with_fault_isolation` regardless of `config.intraday.options.enabled` --
    there is no Stage 5 order-construction path for options yet (no OCC symbol handling
    anywhere in `orders.py`/`broker.py`), so flipping that flag today does not, by itself,
    submit anything. The one invariant this phase must get exactly right: an option
    proposal is priced and gated exactly like a real one would be, and only ever logged.
    """
    result = CycleResult()
    intraday = config.intraday

    # The opening batch runs once per day. Added 2026-09-27 with external triggering: the
    # external scheduler fires `open-batch` on time and GitHub's own cron copy still
    # lands hours later. A second pass would make a fresh Trader call that could pick
    # different symbols (order ids only dedupe the same symbol) and duplicate the
    # open-batch rows the "do nothing" benchmark reads. Keyed on the Trader call, not on
    # proposals: a batch that ran and proposed nothing still ran. A batch that declined
    # at pretrade (market not open yet) made no call, so a later trigger may still run it.
    if cycle == "open_batch" and any(
        row["cycle"] == "open_batch" for row in ledger.model_calls_for(as_of)
    ):
        result.skipped_reason = f"open_batch already ran for {pd.Timestamp(as_of).date()}"
        return result

    pretrade_failures = check_intraday_pretrade(
        IntradayPretradeContext(now=now, config=config, broker=broker),
        shadow=intraday.shadow_mode,
    )
    if pretrade_failures:
        for failure in pretrade_failures:
            ledger.record_incident("error", failure)
        result.skipped_reason = "; ".join(pretrade_failures)
        return result

    account = broker.get_account()
    ledger.record_session_open_equity(as_of, account.equity)
    equity_at_session_open = ledger.session_open_equity(as_of) or account.equity

    positions = broker.list_positions()
    position_quotes = quote_map({p.symbol for p in positions}, market_data, now)
    prices = {s: midpoint_price(q) for s, q in position_quotes.items()}

    monitor_result = check_invalidation_and_time_stops(
        intraday, broker, ledger, prices, as_of, cycle, now
    )
    if monitor_result.closed:
        positions = broker.list_positions()

    # Tranche continuation: submit the next piece of any partially-tranched-in entry
    # (no new model call -- the original accepted proposal already justified the full
    # size; splitting is an execution technique, not a re-decision).
    for position in list(positions):
        pending = ledger.pending_tranche_for(position.symbol)
        if pending is None:
            continue
        proposal_row = None
        if pending["proposal_id"] is not None:
            proposal_row = ledger.get_proposal(pending["proposal_id"])
        price = prices.get(position.symbol)
        if proposal_row is None or price is None:
            continue
        try:
            original_proposal = TradeProposal(**proposal_row["payload"])
        except ValidationError:
            continue
        next_tranche = pending["tranche_index"] + 1
        per_tranche = original_proposal.model_copy(
            update={"size_pct": original_proposal.size_pct / pending["tranche_total"]}
        )
        try:
            entry_request = build_entry_order(
                per_tranche,
                as_of,
                cycle,
                price,
                account.equity,
                intraday,
                tranche=next_tranche,
                at_least_one_share=True,
            )
        except ValueError as exc:
            result.order_failures.append(str(exc))
            continue
        submitted = submit_order_with_fault_isolation(
            entry_request,
            broker,
            ledger,
            now,
            proposal_id=pending["proposal_id"],
            tranche_index=next_tranche,
            tranche_total=pending["tranche_total"],
            leg="entry",
        )
        if submitted is not None:
            result.orders_submitted.append(submitted.client_order_id)
        else:
            result.order_failures.append(
                f"{position.symbol}: tranche {next_tranche} submission failed"
            )

    positions = broker.list_positions()
    positions_notional = {p.symbol: p.market_value for p in positions}

    watchlist = ledger.watchlist_for(as_of)
    tradable_watchlist, watchlist_quotes, _dropped = _apply_spread_gate(
        watchlist, market_data, intraday, ledger, now
    )
    prices.update({s: midpoint_price(q) for s, q in watchlist_quotes.items()})

    symbols_in_view = {row["symbol"] for row in tradable_watchlist} | set(positions_notional)
    headlines = ledger.recent_headlines_for(as_of, list(symbols_in_view)) if symbols_in_view else []
    session = _session_context(sorted(symbols_in_view), market_data, intraday, ledger, now)

    try:
        context = build_trader_context(
            as_of,
            cycle,
            intraday,
            ledger,
            tradable_watchlist,
            positions_notional,
            prices,
            headlines=headlines,
            session=session,
        )
        response = invoke_role(
            "trader",
            as_of=as_of,
            cycle=cycle,
            config=intraday,
            ledger=ledger,
            client=llm_client,
            context=context,
        )
    except DailyBudgetExhausted as exc:
        result.skipped_reason = str(exc)
        return result

    turnover_usd = turnover_so_far_usd(ledger, as_of, account.equity)
    last_stopout_at = ledger.last_stopout_times()

    raw_proposals = (
        (response.structured_output or {}).get("proposals", [])
        if isinstance(response.structured_output, dict)
        else []
    )

    for item in raw_proposals:
        result.proposals_seen += 1
        try:
            proposal = TradeProposal(**item)
        except (ValidationError, TypeError) as exc:
            ledger.record_incident("error", f"malformed trader proposal: {exc}")
            continue

        # Phase 12b (Rollout Stage 4): price this proposal's own contract before building
        # the ValidationContext -- validate_option_proposal (intraday/options.py) requires
        # option_quote/option_pricing to already be populated, and can't fetch them itself
        # (it never receives the market-data clients, only the already-gathered context).
        # Priced regardless of what validate_proposal ends up deciding, and recorded either
        # way (see options_pricing_snapshot below) -- Stage 4's whole purpose is judging
        # whether the feed produces sane pricing, which needs the pricing on a *rejected*
        # proposal too, not only on one that also happened to clear every other gate.
        option_quote = None
        option_pricing = None
        option_forecast_vol = None
        next_earnings_date = None
        spot_price = None
        options_cfg = intraday.options
        if (
            proposal.instrument == "option"
            and proposal.action in OPEN_OR_ADD_ACTIONS
            and (options_cfg.shadow_pricing or options_cfg.enabled)
        ):
            spot_price = prices.get(proposal.symbol)
            if options_market_data is not None and spot_price is not None:
                option_quote, option_pricing = _price_option_proposal(
                    proposal, options_market_data, spot_price, config.drift.risk_free_rate, as_of
                )
            if market_data_pit is not None:
                option_forecast_vol = _realized_vol([proposal.symbol], market_data_pit).get(
                    proposal.symbol
                )
                next_earnings_date = _upcoming_earnings(
                    [proposal.symbol], market_data_pit, as_of, options_cfg.max_dte
                )

        validation_context = ValidationContext(
            as_of=as_of,
            now=now,
            config=intraday,
            ledger=ledger,
            equity=account.equity,
            equity_at_session_open=equity_at_session_open,
            positions=positions_notional,
            prices=prices,
            turnover_so_far_usd=turnover_usd,
            last_stopout_at=last_stopout_at,
            option_quote=option_quote,
            option_pricing=option_pricing,
            option_forecast_vol=option_forecast_vol,
            spot_price=spot_price,
            next_earnings_date=next_earnings_date,
        )
        failures = validate_proposal(proposal, validation_context)
        status = "rejected" if failures else "accepted"
        reason = "; ".join(failures) if failures else None
        options_pricing_snapshot = None
        if option_quote is not None and option_pricing is not None:
            options_pricing_snapshot = {
                "quote": asdict(option_quote),
                "pricing": asdict(option_pricing),
            }
        proposal_id = ledger.record_proposal(
            as_of,
            cycle,
            proposal.symbol,
            proposal.action,
            status,
            payload=proposal.model_dump(),
            reason=reason,
            # Phase 10: stamped mechanically, not asked of the Trader -- which playbook
            # version was active is a fact about when this call happened, not a
            # judgment call. Its own column, not folded into `payload` -- see
            # `record_proposal`'s own docstring for why that would break
            # `TradeProposal(**payload)` reconstruction elsewhere.
            playbook_version=ledger.current_playbook_version(),
            options_pricing=options_pricing_snapshot,
        )
        if failures:
            result.proposals_rejected += 1
            continue
        result.proposals_accepted += 1

        if proposal.instrument == "option":
            # Stage 4 (BRIEF_INTRADAY.md rollout) -- priced and logged above, never
            # submitted. Deliberately unconditional on config.intraday.options.enabled:
            # there is no Stage 5 order-construction path for options yet (no OCC symbol
            # handling anywhere in orders.py/broker.py), so this must never fall through
            # to the dispatch below regardless of that flag. That matters beyond "don't
            # submit an order Stage 5 can't build yet" -- an option trim/close shares
            # `symbol` with its underlying stock, so falling through would wrongly act on
            # the *equity* position of the same name (trim/close below look up positions
            # by `proposal.symbol` alone, with no instrument check of their own).
            continue

        if proposal.action in OPEN_OR_ADD_ACTIONS:
            price = prices.get(proposal.symbol)
            if price is None:
                result.order_failures.append(f"{proposal.symbol}: no price to size entry")
                continue
            # "Two tranches for a full-size position" (brief): split only as far as every
            # piece still buys a whole share. A size worth one share goes in whole; one worth
            # none was already rejected by validate_proposal.
            full_shares = whole_shares(proposal.size_pct, account.equity, price)
            tranches = max(1, min(intraday.risk.entry_tranches, full_shares))
            tranche_proposal = proposal.model_copy(
                update={"size_pct": proposal.size_pct / tranches}
            )
            try:
                entry_request = build_entry_order(
                    tranche_proposal, as_of, cycle, price, account.equity, intraday, tranche=1
                )
            except ValueError as exc:
                result.order_failures.append(str(exc))
                continue
            submitted = submit_order_with_fault_isolation(
                entry_request,
                broker,
                ledger,
                now,
                proposal_id=proposal_id,
                tranche_index=1,
                tranche_total=tranches,
                leg="entry",
            )
            if submitted is not None:
                result.orders_submitted.append(submitted.client_order_id)
            else:
                result.order_failures.append(f"{proposal.symbol}: entry order failed")

        elif proposal.action == "trim":
            position = next((p for p in positions if p.symbol == proposal.symbol), None)
            price = prices.get(proposal.symbol)
            if position is None or price is None:
                result.order_failures.append(
                    f"{proposal.symbol}: cannot trim -- no position or price"
                )
                continue
            find_and_cancel_resting_stop(proposal.symbol, broker, ledger)
            try:
                trim_request = build_trim_order(
                    proposal, position, as_of, cycle, price, account.equity, intraday
                )
            except ValueError as exc:
                result.order_failures.append(str(exc))
                continue
            submitted = submit_order_with_fault_isolation(
                trim_request, broker, ledger, now, proposal_id=proposal_id, leg="exit"
            )
            if submitted is None:
                result.order_failures.append(f"{proposal.symbol}: trim order failed")
                continue
            result.orders_submitted.append(submitted.client_order_id)
            record_mechanical_reflection(
                proposal.symbol, submitted.client_order_id, "trader_trim", ledger, intraday, as_of
            )

            remaining_qty = abs(position.qty) - abs(trim_request.qty)
            original_payload = _original_proposal_payload(ledger, proposal.symbol) or {}
            original_invalidation = original_payload.get("invalidation_price")
            if remaining_qty > 0 and original_invalidation is not None:
                stop_side: OrderSide = "sell" if position.side == "long" else "buy"
                replacement = build_replacement_stop_order(
                    proposal.symbol, stop_side, remaining_qty, original_invalidation, as_of, cycle
                )
                submit_order_with_fault_isolation(
                    replacement, broker, ledger, now, proposal_id=proposal_id, leg="stop"
                )
            elif remaining_qty > 0:
                ledger.record_incident(
                    "warning",
                    f"{proposal.symbol}: trimmed but no original invalidation_price found "
                    "-- remainder left without a resting stop",
                )

        elif proposal.action == "close":
            position = next((p for p in positions if p.symbol == proposal.symbol), None)
            price = prices.get(proposal.symbol)
            if position is None or price is None:
                result.order_failures.append(
                    f"{proposal.symbol}: cannot close -- no position or price"
                )
                continue
            find_and_cancel_resting_stop(proposal.symbol, broker, ledger)
            close_request = build_close_order(position, as_of, cycle, price, intraday)
            submitted = submit_order_with_fault_isolation(
                close_request, broker, ledger, now, proposal_id=proposal_id, leg="exit"
            )
            if submitted is not None:
                result.orders_submitted.append(submitted.client_order_id)
                record_mechanical_reflection(
                    proposal.symbol,
                    submitted.client_order_id,
                    "trader_close",
                    ledger,
                    intraday,
                    as_of,
                )
            else:
                result.order_failures.append(f"{proposal.symbol}: close order failed")

        # hold/no_trade: nothing to do.

    return result


# -- carry ------------------------------------------------------------------------------


@dataclass
class CarryResult:
    closed: list[str] = field(default_factory=list)
    carried: list[str] = field(default_factory=list)
    closed_for_cap: list[str] = field(default_factory=list)


def run_carry(
    config: Config,
    broker: Broker,
    ledger: IntradayLedger,
    market_data: AlpacaMarketDataClient,
    as_of: pd.Timestamp,
    now: dt.datetime,
) -> CarryResult:
    """At 14:45: closes every position whose opening proposal's `horizon` is
    `"intraday"` or untraceable (a safe default -- never carry something with no
    written multi-day thesis on file, per the brief's own "everything carried needs a
    written multi-day thesis"). Remaining (multi-day/multi-week) positions are carried
    unless their aggregate notional exceeds `risk.overnight_gross_cap_pct * equity`, in
    which case the lowest-confidence ones are closed one at a time (a full close, not a
    partial trim -- simpler, and this is already an off-thesis risk-cap action, not a
    sizing decision) until back under cap. Every close here records a mechanical
    reflection tagged `"carry"` (`reflect.py`, Phase 9a).
    """
    result = CarryResult()
    intraday = config.intraday
    positions = broker.list_positions()
    account = broker.get_account()

    carried_candidates: list[tuple[Position, dict[str, Any]]] = []
    for position in positions:
        payload = _original_proposal_payload(ledger, position.symbol)
        horizon = payload.get("horizon") if payload else None
        if horizon in ("multi-day", "multi-week"):
            carried_candidates.append((position, payload or {}))
            continue

        price = midpoint_price(market_data.latest_quote(position.symbol, at=now))
        if price is None:
            ledger.record_incident(
                "error", f"{position.symbol}: carry could not price a close -- left open"
            )
            result.carried.append(position.symbol)
            continue
        find_and_cancel_resting_stop(position.symbol, broker, ledger)
        close_request = build_close_order(position, as_of, "carry", price, intraday)
        submitted = submit_order_with_fault_isolation(
            close_request, broker, ledger, now, leg="exit"
        )
        if submitted is not None:
            record_mechanical_reflection(
                position.symbol, submitted.client_order_id, "carry", ledger, intraday, as_of
            )
        result.closed.append(position.symbol)

    carried_candidates.sort(key=lambda item: item[1].get("confidence") or 0.0)
    overnight_notional = sum(abs(p.market_value) for p, _ in carried_candidates)
    cap = intraday.risk.overnight_gross_cap_pct * account.equity

    for position, _payload in carried_candidates:
        if overnight_notional <= cap:
            result.carried.append(position.symbol)
            continue
        price = midpoint_price(market_data.latest_quote(position.symbol, at=now))
        if price is None:
            result.carried.append(position.symbol)
            continue
        find_and_cancel_resting_stop(position.symbol, broker, ledger)
        close_request = build_close_order(position, as_of, "carry", price, intraday)
        submitted = submit_order_with_fault_isolation(
            close_request, broker, ledger, now, leg="exit"
        )
        if submitted is not None:
            record_mechanical_reflection(
                position.symbol, submitted.client_order_id, "carry", ledger, intraday, as_of
            )
        overnight_notional -= abs(position.market_value)
        result.closed_for_cap.append(position.symbol)

    return result


# -- eod_review (Phase 9b) ----------------------------------------------------------------


@dataclass(frozen=True)
class EodReviewResult:
    report_path: str | None = None
    reflections_updated: int = 0
    playbook_proposal_written: bool = False
    skipped_reason: str | None = None
    # The report was written but the Reviewer deliberately not called (a quiet day).
    reviewer_skipped_reason: str | None = None


def run_eod_review(
    config: Config,
    market_data: AlpacaMarketDataClient,
    news_client: AlpacaNewsClient,
    market_data_pit: MarketData,
    paper_ledger: Any,
    shadow_ledger: IntradayLedger | None,
    ledger: IntradayLedger,
    llm_client: ClaudeCliClient,
    as_of: pd.Timestamp,
    cycle: str,
    now: dt.datetime,
) -> EodReviewResult:
    """15:15: the Reviewer grades the day and writes `reports/YYYY-MM-DD/eod.md`
    (`BRIEF_INTRADAY.md`'s `eod_review` row). Everything computable without judgment --
    P&L, three of the four benchmarks, the process-scorecard numbers, day attribution --
    was already built by Phase 9a/this phase's own `review.py`/`eod_report.py`; this
    wires the Reviewer's one daily call on top of it: narration, grading (the
    `category`/`process_grade`/`lesson` fields Phase 9a's `reflect.py` left `None`), the
    closing market report, and a playbook proposal.

    On a quiet day (no proposals and no reflections) the mechanical report is still
    written -- blotter, P&L, benchmarks including the placebo, scorecard numbers, monitor
    cadence -- but the Reviewer is not called: there is nothing to grade, so nothing
    worth paying for. Changed 2026-09-27: this used to skip the whole report, so every
    Stage-1 day (zero proposals, before the `_intraday_now` timezone fix) produced no
    eod.md at all, and a legitimately quiet day lost its placebo comparison -- the brief
    wants it in every daily report, and "the agent held back while the placebo traded"
    is exactly the kind of day it exists to measure. Idempotent per `as_of` for a graded
    day: if today's reflections already carry a non-null `category`, this already ran
    (mirrors `run_research`'s `watchlist_for(as_of)`-non-empty check); a quiet-day re-run
    just rewrites the same mechanical report.

    `shadow_ledger` is optional -- `None` skips the placebo benchmark gracefully (Phase 7
    might not be enabled in every deployment) rather than requiring a second ledger no
    caller has to open.
    """
    proposals_today = ledger.proposals_for(as_of)
    reflections_today = ledger.reflections_for(as_of)
    nothing_to_grade = not proposals_today and not reflections_today
    if any(r["content"].get("category") is not None for r in reflections_today) or any(
        row["role"] == "reviewer" for row in ledger.model_calls_for(as_of)
    ):
        # The graded-reflection check alone missed a day with proposals but no closes:
        # nothing gets graded, so a second trigger (external scheduler + late cron, added
        # 2026-09-27) would pay for a second Reviewer call. The recorded call covers it.
        return EodReviewResult(skipped_reason=f"eod_review already ran for {as_of}")

    # Local imports: `review.py` and `placebo.py` both import *from* this module at their
    # own top level (`turnover_so_far_usd`, `run_carry`/`quote_map`/...), so importing
    # them back at jobs.py's own top level would be a real circular import. Deferred to
    # call time, after both modules have already fully loaded, this is safe -- the same
    # reasoning `cli.py` already relies on for its own many function-local imports.
    from .eod_report import (
        DayBenchmarks,
        build_eod_report,
        do_nothing_return,
        render_eod_markdown,
        s0_day_return,
    )
    from .placebo import placebo_day_pnl
    from .review import day_attribution, process_scorecard

    intraday = config.intraday
    research_cfg = intraday.research
    tickers = config.universe.tickers

    soxx_return = None
    try:
        moves = premarket_moves(["SOXX"], market_data_pit, market_data, now)
        soxx_return = moves.get("SOXX")
    except Exception as exc:  # noqa: BLE001 -- a benchmark must never block the report
        ledger.record_incident("warning", f"SOXX benchmark unavailable: {exc}")

    s0_return = None
    try:
        s0_return = s0_day_return(paper_ledger, as_of)
    except Exception as exc:  # noqa: BLE001 -- same reasoning as the SOXX benchmark
        ledger.record_incident("warning", f"S0 benchmark unavailable: {exc}")

    do_nothing = None
    try:
        do_nothing = do_nothing_return(ledger, market_data, as_of, now)
    except Exception as exc:  # noqa: BLE001 -- same reasoning
        ledger.record_incident("warning", f"'do nothing' benchmark unavailable: {exc}")

    placebo_pnl = None
    if shadow_ledger is not None:
        try:
            placebo_pnl = placebo_day_pnl(shadow_ledger, market_data, config, as_of, now)
        except Exception as exc:  # noqa: BLE001 -- same reasoning
            ledger.record_incident("warning", f"placebo benchmark unavailable: {exc}")

    benchmarks = DayBenchmarks(
        soxx_return=soxx_return,
        s0_return=s0_return,
        placebo_pnl=placebo_pnl,
        do_nothing_return=do_nothing,
    )

    scorecard = process_scorecard(ledger, intraday, as_of)
    attribution = day_attribution(reflections_today, soxx_return)

    if nothing_to_grade:
        reviewer_note = "nothing to grade today"
        report = build_eod_report(
            ledger,
            as_of,
            benchmarks=benchmarks,
            process_scorecard=scorecard,
            attribution=attribution,
            reviewer_note=f"{reviewer_note} (no proposals and no closed trades)",
        )
        out_path = config.path(intraday.reports_dir) / str(pd.Timestamp(as_of).date()) / "eod.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_eod_markdown(report), encoding="utf-8")
        return EodReviewResult(report_path=str(out_path), reviewer_skipped_reason=reviewer_note)

    # `news_lookback_hours` (16h, tuned for the Researcher's overnight coverage) reaches
    # well back past this session's own 08:30 open when called near the 15:15 close --
    # already covers the whole trading day, so no separate lookback config is needed.
    news_start = now - dt.timedelta(hours=research_cfg.news_lookback_hours)
    articles = news_client.get_news(tickers, start=news_start, end=now)
    clean_text = {
        article.id: (
            sanitize_and_log(
                article.headline, ledger=ledger, source=article.source, context="eod_review news"
            ),
            sanitize_and_log(
                article.summary, ledger=ledger, source=article.source, context="eod_review news"
            ),
        )
        for article in articles
    }
    capped_articles = _cap_news_per_symbol(articles, research_cfg.max_headlines_per_symbol)
    news_sections = [
        {
            "headline": clean_text[article.id][0],
            "summary": clean_text[article.id][1],
            "symbols": article.symbols,
            "created_at": str(article.created_at),
            "source": article.source,
        }
        for article in capped_articles
    ]

    playbook_path = config.path(intraday.playbook_path)
    playbook_current = (
        playbook_path.read_text(encoding="utf-8")
        if playbook_path.exists()
        else "no playbook exists yet"
    )
    blotter = {"proposals": proposals_today, "reflections": reflections_today}
    account = {
        "session_open_equity": ledger.session_open_equity(as_of),
        "latest_snapshot": ledger.latest_snapshot(),
    }
    benchmarks_section = {
        "soxx_return": soxx_return,
        "s0_return": s0_return,
        "placebo_pnl": placebo_pnl,
        "do_nothing_return": do_nothing,
    }
    process_metrics = {**scorecard, "attribution": attribution}

    try:
        context = build_reviewer_context(
            as_of,
            cycle,
            intraday,
            blotter,
            account,
            benchmarks_section,
            process_metrics,
            news_sections,
            playbook_current,
        )
        response = invoke_role(
            "reviewer",
            as_of=as_of,
            cycle=cycle,
            config=intraday,
            ledger=ledger,
            client=llm_client,
            context=context,
        )
    except DailyBudgetExhausted as exc:
        return EodReviewResult(skipped_reason=str(exc))

    payload = response.structured_output if isinstance(response.structured_output, dict) else None
    required_keys = {
        "reflection_updates",
        "closing_market_report",
        "attribution_narrative",
        "process_scorecard_narrative",
        "playbook_proposal",
    }
    if payload is None or not required_keys.issubset(payload):
        ledger.record_incident("error", "malformed reviewer response: missing required keys")
        return EodReviewResult(skipped_reason="malformed reviewer response")

    reflections_by_id = {row["id"]: row for row in reflections_today}
    updated = 0
    for item in payload.get("reflection_updates", []):
        try:
            update = ReflectionUpdate(**item)
        except (ValidationError, TypeError) as exc:
            ledger.record_incident("error", f"malformed reflection update: {exc}")
            continue
        existing = reflections_by_id.get(update.reflection_id)
        if existing is None:
            ledger.record_incident(
                "error", f"reviewer referenced unknown reflection_id {update.reflection_id}"
            )
            continue
        lesson = sanitize_and_log(
            update.lesson, ledger=ledger, source="reviewer", context="reflection lesson"
        )
        new_content = {
            **existing["content"],
            "category": update.category,
            "process_grade": update.process_grade,
            "lesson": lesson,
        }
        ledger.update_reflection(update.reflection_id, new_content)
        updated += 1

    closing_market_report = sanitize_and_log(
        payload["closing_market_report"],
        ledger=ledger,
        source="reviewer",
        context="closing market report",
    )
    attribution_narrative = sanitize_and_log(
        payload["attribution_narrative"],
        ledger=ledger,
        source="reviewer",
        context="attribution narrative",
    )
    process_scorecard_narrative = sanitize_and_log(
        payload["process_scorecard_narrative"],
        ledger=ledger,
        source="reviewer",
        context="process scorecard narrative",
    )
    raw_playbook_proposal = payload.get("playbook_proposal")
    playbook_proposal = sanitize_and_log(
        raw_playbook_proposal or "None proposed today.",
        ledger=ledger,
        source="reviewer",
        context="playbook proposal",
    )

    proposal_written = False
    if raw_playbook_proposal:
        proposals_dir = config.path(intraday.proposals_dir)
        proposals_dir.mkdir(parents=True, exist_ok=True)
        (proposals_dir / f"{pd.Timestamp(as_of).date()}.md").write_text(
            playbook_proposal, encoding="utf-8"
        )
        proposal_written = True

    report = build_eod_report(
        ledger,
        as_of,
        benchmarks=benchmarks,
        process_scorecard=scorecard,
        process_scorecard_narrative=process_scorecard_narrative,
        attribution=attribution,
        attribution_narrative=attribution_narrative,
        closing_market_report=closing_market_report,
        playbook_proposal=playbook_proposal,
    )
    out_path = config.path(intraday.reports_dir) / str(pd.Timestamp(as_of).date()) / "eod.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_eod_markdown(report), encoding="utf-8")

    return EodReviewResult(
        report_path=str(out_path),
        reflections_updated=updated,
        playbook_proposal_written=proposal_written,
    )


@dataclass(frozen=True)
class WeeklyReviewResult:
    report_path: str | None = None
    pending_proposals: int = 0
    entries_due_for_review: int = 0


def run_weekly_review(
    config: Config, ledger: IntradayLedger, as_of: pd.Timestamp
) -> WeeklyReviewResult:
    """Friday 15:45: surfaces the week's numbers, any pending playbook proposals, and any
    active entries whose review date has passed -- and nothing more. No broker, no LLM
    client, no market data: confirmed directly against the brief before writing this
    (see module docstring) that `weekly_review` names no agent, so there is no judgment
    call for this function to make. Accepting/rejecting/retiring stays a separate,
    human-invoked action (`intraday/playbook.py`) run whenever the user actually does
    their weekly review -- never called from here, and not gated to Friday either.

    No idempotency guard, unlike `run_eod_review` -- this only aggregates and renders; a
    second same-day run just overwrites the same report file with the same numbers,
    unlike `eod_review`'s reflection-grading side effects.
    """
    # Local imports: `review.py`/`playbook.py` both import *from* this module at their
    # own top level in spirit (review.py imports `turnover_so_far_usd`), so importing
    # them back at jobs.py's own top level risks the same circular import
    # `run_eod_review`'s own comment already explains. Deferred to call time is safe
    # for the identical reason.
    from .playbook import pending_proposal_paths
    from .review import render_weekly_review_markdown, weekly_summary

    intraday = config.intraday
    summary = weekly_summary(ledger, intraday, as_of)
    pending = pending_proposal_paths(config, ledger)
    today = pd.Timestamp(as_of).normalize()
    due_for_review = [
        row
        for row in ledger.active_playbook_entries()
        if row["review_date"] is not None and pd.Timestamp(row["review_date"]) <= today
    ]

    text = render_weekly_review_markdown(summary, pending, due_for_review, as_of)
    out_path = config.path(intraday.reports_dir) / str(today.date()) / "weekly_review.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    return WeeklyReviewResult(
        report_path=str(out_path),
        pending_proposals=len(pending),
        entries_due_for_review=len(due_for_review),
    )
