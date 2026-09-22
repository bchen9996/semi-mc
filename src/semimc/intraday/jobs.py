"""S4's scheduled jobs (`BRIEF_INTRADAY.md`'s "Daily schedule"), Phases 5-6.

Six of the brief's eight named jobs, given the further split `tasks/todo.md`'s S4
addendum records: `preflight`, `research`, `preopen`, `open_batch`/`cycle` (one shared
implementation, `run_cycle`, since they are the same operation -- `open_batch` is just
the first `cycle` of the day), and `carry`. `monitor` (not in the brief's original list,
but matching its own separate `monitor.py` file) runs standalone on a tight, model-free
cron and is also the first thing `run_cycle` calls. `eod_review`/`weekly_review` are
Phase 9's jobs, not built here -- see `tasks/todo.md`'s S4 addendum for why.

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
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from pydantic import ValidationError

from ..config import Config, IntradayConfig
from ..data import MarketData
from ..data.realized import realized_variance
from ..execution.broker import Broker, OrderSide, Position
from ..execution.market_data import AlpacaMarketDataClient
from ..execution.news import AlpacaNewsClient
from .context import WatchlistCandidate, build_researcher_context, build_trader_context
from .ledger import IntradayLedger
from .llm import ClaudeCliClient, DailyBudgetExhausted, invoke_role
from .monitor import check_invalidation_and_time_stops
from .orders import (
    build_close_order,
    build_entry_order,
    build_replacement_stop_order,
    build_trim_order,
    find_and_cancel_resting_stop,
    submit_order_with_fault_isolation,
)
from .pretrade import IntradayPretradeContext, check_intraday_pretrade, check_spread_gate
from .proposals import OPEN_OR_ADD_ACTIONS, TradeProposal, ValidationContext, validate_proposal
from .reflect import _entry_and_proposal, record_mechanical_reflection
from .security import sanitize_and_log


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
    true, so if one is, it means something. "Playbook version loaded" (brief) is skipped
    -- no `PLAYBOOK.md` exists before Phase 9.
    """
    pretrade_results = check_intraday_pretrade(
        IntradayPretradeContext(now=now, config=config, broker=broker)
    )
    notes = [r for r in pretrade_results if r == _ALWAYS_ROUTINE_PRETRADE_NOTE]
    remaining = [r for r in pretrade_results if r != _ALWAYS_ROUTINE_PRETRADE_NOTE]
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

    return PreflightResult(passed=not failures, failures=failures, notes=notes)


# -- research (Phase 6) ------------------------------------------------------------------


def premarket_moves(
    tickers: list[str],
    market_data_pit: MarketData,
    market_data: AlpacaMarketDataClient,
    now: dt.datetime,
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
    Phase 7 already used renaming `_turnover_so_far_usd`)."""
    prior_close = market_data_pit.spot(tickers)
    moves: dict[str, float] = {}
    for ticker in tickers:
        prior = prior_close.get(ticker)
        if prior is None or pd.isna(prior) or prior <= 0:
            continue
        current = midpoint_price(market_data.latest_quote(ticker, at=now))
        if current is None:
            continue
        moves[ticker] = (current - prior) / prior
    return moves


def _upcoming_earnings(
    tickers: list[str], market_data_pit: MarketData, as_of: pd.Timestamp, lookahead_days: int
) -> dict[str, str]:
    """Nearest scheduled report date within `lookahead_days`, per ticker with one. The
    report *date* is known ahead of time (`data/earnings.py`'s own point-in-time
    reasoning); only the *reaction* is what the Researcher has to weigh -- not a leak."""
    if market_data_pit.earnings is None:
        return {}
    stamp = pd.Timestamp(as_of).normalize()
    horizon = stamp + pd.Timedelta(days=lookahead_days)
    upcoming: dict[str, str] = {}
    for ticker in tickers:
        rows = market_data_pit.earnings.for_ticker(ticker)
        future = rows[(rows["date"] > stamp) & (rows["date"] <= horizon)]
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
    """
    pretrade_failures = check_intraday_pretrade(
        IntradayPretradeContext(now=now, config=config, broker=broker)
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

    premarket_moves_today = premarket_moves(tickers, market_data_pit, market_data, now)
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


def run_cycle(
    cycle: str,
    config: Config,
    broker: Broker,
    ledger: IntradayLedger,
    market_data: AlpacaMarketDataClient,
    llm_client: ClaudeCliClient,
    as_of: pd.Timestamp,
    now: dt.datetime,
) -> CycleResult:
    """One Trader cycle: monitor -> tranche continuation -> propose -> validate ->
    execute. `open_batch` and `cycle` (the CLI/workflow layer) both call this with a
    different `cycle` label -- `open_batch` is simply the first cycle of the day.

    Nothing here is a new order-construction primitive; every action reuses Phase 3-4's
    builders (`build_entry_order`/`build_close_order`/`build_trim_order`/
    `build_replacement_stop_order`) and Phase 2's context/LLM wrapper
    (`build_trader_context`/`invoke_role`). This function only sequences them.
    """
    result = CycleResult()
    intraday = config.intraday

    pretrade_failures = check_intraday_pretrade(
        IntradayPretradeContext(now=now, config=config, broker=broker)
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
                per_tranche, as_of, cycle, price, account.equity, intraday, tranche=next_tranche
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
        )
        failures = validate_proposal(proposal, validation_context)
        status = "rejected" if failures else "accepted"
        reason = "; ".join(failures) if failures else None
        proposal_id = ledger.record_proposal(
            as_of,
            cycle,
            proposal.symbol,
            proposal.action,
            status,
            payload=proposal.model_dump(),
            reason=reason,
        )
        if failures:
            result.proposals_rejected += 1
            continue
        result.proposals_accepted += 1

        if proposal.action in OPEN_OR_ADD_ACTIONS:
            price = prices.get(proposal.symbol)
            if price is None:
                result.order_failures.append(f"{proposal.symbol}: no price to size entry")
                continue
            tranches = max(1, intraday.risk.entry_tranches)
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
