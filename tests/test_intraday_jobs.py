"""`intraday/jobs.py`: the scheduled loop (S4, `BRIEF_INTRADAY.md`, Phases 5-6) --
`preflight`/`research`/`preopen`/`open_batch`/`cycle` (`run_cycle`, shared)/`carry`. Every
LLM call is a fake `ClaudeCliClient(run=...)` (no subprocess, no network) and every broker
call is `FakeBroker` -- same boundary-mocking convention as every other intraday test file.
`run_research`'s tests use the `market` fixture (`tests/conftest.py`) for its own-data
inputs (OHLC/earnings) -- the real, fixture-backed `MarketData`, not a fake, since building
one correctly is exactly what those tests should exercise.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass, field

import pandas as pd
from fixtures.injection_corpus import INJECTION_CORPUS

from semimc.execution.fake_broker import FakeBroker
from semimc.intraday.jobs import (
    run_carry,
    run_cycle,
    run_preflight,
    run_preopen,
    run_research,
)
from semimc.intraday.ledger import IntradayLedger
from semimc.intraday.llm import ClaudeCliClient
from semimc.intraday.orders import build_entry_order, submit_order_with_fault_isolation
from semimc.intraday.proposals import TradeProposal

AS_OF = pd.Timestamp("2026-09-21")
NOW = dt.datetime(2026, 9, 21, 8, 35)
ACCOUNT = "FAKE-PAPER-0001"


# -- shared setup helpers -----------------------------------------------------------------


def _tradeable_config(config, tmp_path, **risk_overrides):
    intraday = config.intraday.model_copy(
        update={
            "trading_enabled": True,
            "registered_account": ACCOUNT,
            "kill_switch_path": str(tmp_path / "KILL_SWITCH"),
            **(
                {"risk": config.intraday.risk.model_copy(update=risk_overrides)}
                if risk_overrides
                else {}
            ),
        }
    )
    return config.model_copy(update={"intraday": intraday})


def _ledger(tmp_path) -> IntradayLedger:
    return IntradayLedger(tmp_path / "intraday.sqlite3")


@dataclass
class _FakeQuote:
    bid_price: float
    ask_price: float
    timestamp: dt.datetime


@dataclass
class _FakeMarketData:
    quotes: dict[str, _FakeQuote] = field(default_factory=dict)
    raises: bool = False

    def latest_quote(self, symbol: str, at: dt.datetime):
        if self.raises:
            raise RuntimeError("data feed unreachable")
        return self.quotes.get(symbol)


def _quote(price: float, spread: float = 0.05, at: dt.datetime = NOW) -> _FakeQuote:
    return _FakeQuote(bid_price=price - spread / 2, ask_price=price + spread / 2, timestamp=at)


@dataclass
class _FakeArticle:
    id: str
    headline: str
    symbols: list[str] = field(default_factory=list)
    summary: str = ""
    created_at: dt.datetime = NOW
    source: str = "benzinga"


@dataclass
class _FakeNewsClient:
    articles: list[_FakeArticle] = field(default_factory=list)
    raises: bool = False

    def get_news(self, symbols, start, end, limit=50):
        if self.raises:
            raise RuntimeError("news feed unreachable")
        wanted = set(symbols)
        return [a for a in self.articles if wanted & set(a.symbols)]


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _result_json(**overrides) -> str:
    payload = {
        "session_id": "sess-1",
        "result": "done",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "is_error": False,
        "structured_output": {"proposals": []},
    }
    payload.update(overrides)
    return json.dumps(payload)


def _sequenced_llm_client(*stdouts: str) -> ClaudeCliClient:
    """A trader response per call, in order -- the last one repeats if `run_cycle` is
    called more times than responses were supplied."""
    remaining = list(stdouts)

    def run(argv, stdin_text, timeout_seconds):
        stdout = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return _completed(stdout)

    return ClaudeCliClient(run=run)


def _recording_llm_client(stdout: str) -> tuple[list[dict], ClaudeCliClient]:
    """Records every call's stdin (the context payload) -- what the headline-wiring test
    needs to confirm the Trader's context actually carried `recent_headlines_for`'s
    output, not just that `run_cycle` didn't crash."""
    calls: list[dict] = []

    def run(argv, stdin_text, timeout_seconds):
        calls.append({"argv": argv, "stdin": stdin_text})
        return _completed(stdout)

    return calls, ClaudeCliClient(run=run)


def _seed_open_position(
    broker: FakeBroker,
    ledger: IntradayLedger,
    cfg,
    symbol: str = "NVDA",
    side: str = "buy",
    price: float = 190.0,
    invalidation_price: float = 185.0,
    time_stop_minutes: int = 120,
    horizon: str | None = "intraday",
    confidence: float | None = 0.6,
) -> int:
    """A real, filled entry (via the same `build_entry_order` ->
    `submit_order_with_fault_isolation` pipeline `run_cycle` itself uses), so its OTO
    stop leg and ledger trail are exactly what a real accepted `open` would produce."""
    proposal = TradeProposal(
        action="open",
        symbol=symbol,
        side=side,
        size_pct=0.05,
        limit_price=price,
        thesis="seed",
        invalidation_price=invalidation_price,
        time_stop_minutes=time_stop_minutes,
        horizon=horizon,
        confidence=confidence,
    )
    proposal_id = ledger.record_proposal(
        AS_OF, "seed", symbol, "open", "accepted", payload=proposal.model_dump()
    )
    request = build_entry_order(proposal, AS_OF, "seed", price, 100_000.0, cfg.intraday)
    submit_order_with_fault_isolation(
        request,
        broker,
        ledger,
        NOW,
        proposal_id=proposal_id,
        tranche_index=1,
        tranche_total=1,
        leg="entry",
    )
    return proposal_id


# -- run_preflight --------------------------------------------------------------------------


def test_run_preflight_passes_when_everything_is_clear(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData(quotes={"SPY": _quote(500.0)})
    result = run_preflight(cfg, FakeBroker(), market_data, _FakeNewsClient(), ledger, NOW)
    assert result.passed
    assert result.failures == []
    ledger.close()


def test_run_preflight_passes_with_routine_notes_when_not_yet_enabled(config, tmp_path):
    """`trading_enabled=False` and a closed market are both true every single morning
    before S4's own rollout -- neither should fail the job or fire a GitHub failure
    email, unlike a genuine problem (kill switch, a dead feed)."""
    ledger = _ledger(tmp_path)  # config.intraday.trading_enabled defaults to False
    broker = FakeBroker(market_open=False)
    market_data = _FakeMarketData(quotes={"SPY": _quote(500.0)})
    result = run_preflight(config, broker, market_data, _FakeNewsClient(), ledger, NOW)
    assert result.passed
    assert result.failures == []
    assert "intraday trading is not trading_enabled" in result.notes
    assert "market is closed" in result.notes
    incidents = ledger.incidents()
    assert any(i["severity"] == "warning" and "trading_enabled" in i["message"] for i in incidents)
    assert not any(i["severity"] == "error" for i in incidents)
    ledger.close()


def test_run_preflight_fails_on_the_kill_switch(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    (tmp_path / "KILL_SWITCH").write_text("stop", encoding="utf-8")
    ledger = _ledger(tmp_path)
    result = run_preflight(cfg, FakeBroker(), _FakeMarketData(), _FakeNewsClient(), ledger, NOW)
    assert not result.passed
    assert any("kill switch" in f for f in result.failures)
    assert any("kill switch" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_preflight_fails_when_market_data_is_unreachable(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    result = run_preflight(
        cfg, FakeBroker(), _FakeMarketData(raises=True), _FakeNewsClient(), ledger, NOW
    )
    assert not result.passed
    assert any("market data unreachable" in f for f in result.failures)
    ledger.close()


def test_run_preflight_fails_when_the_news_feed_is_unreachable(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData(quotes={"SPY": _quote(500.0)})
    result = run_preflight(
        cfg, FakeBroker(), market_data, _FakeNewsClient(raises=True), ledger, NOW
    )
    assert not result.passed
    assert any("news feed unreachable" in f for f in result.failures)
    ledger.close()


def test_run_preflight_flags_a_bad_reconciliation(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_reconciliation(AS_OF, status="mismatch", discrepancies=[{"symbol": "NVDA"}])
    result = run_preflight(
        cfg,
        FakeBroker(),
        _FakeMarketData(quotes={"SPY": _quote(500.0)}),
        _FakeNewsClient(),
        ledger,
        NOW,
    )
    assert not result.passed
    assert any("reconciliation" in f for f in result.failures)
    ledger.close()


# -- run_preopen ----------------------------------------------------------------------------


def test_run_preopen_keeps_a_tight_fresh_quote(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    result = run_preopen(cfg, market_data, _FakeNewsClient(), ledger, AS_OF, NOW)
    assert result.tradable == ["NVDA"]
    assert result.dropped == []
    ledger.close()


def test_run_preopen_drops_a_wide_spread_candidate(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, max_spread_bps=10.0)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0, spread=2.0)})  # ~105 bps
    result = run_preopen(cfg, market_data, _FakeNewsClient(), ledger, AS_OF, NOW)
    assert result.tradable == []
    assert result.dropped[0][0] == "NVDA"
    assert any("spread" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_preopen_records_and_logs_a_new_headline(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline="NVDA guidance raised", symbols=["NVDA"])]
    )

    result = run_preopen(cfg, market_data, news_client, ledger, AS_OF, NOW)

    assert result.new_headlines == [("NVDA", "NVDA guidance raised")]
    assert ledger.headline_already_seen("alpaca_news", "a1")
    assert any("new headline" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_preopen_never_drops_a_candidate_over_a_headline(config, tmp_path):
    """`preopen` is a *deterministic* refresh (the brief's own word) -- a new headline is
    surfaced, never used here to judge whether a thesis broke. See `run_preopen`'s
    docstring."""
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline="NVDA misses guidance", symbols=["NVDA"])]
    )

    result = run_preopen(cfg, market_data, news_client, ledger, AS_OF, NOW)

    assert result.tradable == ["NVDA"]
    assert result.dropped == []


def test_run_preopen_skips_an_already_seen_headline(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    ledger.record_headline_seen(AS_OF, "alpaca_news", "a1")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline="NVDA guidance raised", symbols=["NVDA"])]
    )

    result = run_preopen(cfg, market_data, news_client, ledger, AS_OF, NOW)

    assert result.new_headlines == []
    ledger.close()


def test_run_preopen_redacts_a_malicious_new_headline_and_logs_an_incident(config, tmp_path):
    """Phase 8: a headline carrying an injection attempt is neutralized before it's
    persisted or logged -- `recent_headlines_for` (what the Trader's context reads later)
    must never carry the raw payload."""
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    _label, malicious_headline, _categories = INJECTION_CORPUS[0]
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline=malicious_headline, symbols=["NVDA"])]
    )

    result = run_preopen(cfg, market_data, news_client, ledger, AS_OF, NOW)

    assert malicious_headline not in result.new_headlines[0][1]
    assert "[redacted:" in result.new_headlines[0][1]
    stored = ledger.recent_headlines_for(AS_OF, ["NVDA"])
    assert malicious_headline not in stored[0]["headline"]
    assert any("suspected prompt injection" in i["message"] for i in ledger.incidents())
    ledger.close()


# -- run_cycle: pretrade gate ---------------------------------------------------------------


def test_run_cycle_is_skipped_when_trading_is_disabled(config, tmp_path):
    ledger = _ledger(tmp_path)  # config.intraday.trading_enabled defaults to False
    client = _sequenced_llm_client(_result_json())
    result = run_cycle(
        "cycle_1", config, FakeBroker(), ledger, _FakeMarketData(), client, AS_OF, NOW
    )
    assert result.skipped_reason is not None
    assert result.proposals_seen == 0
    ledger.close()


# -- run_cycle: opening and tranche-splitting -------------------------------------------------


def test_run_cycle_submits_only_the_first_tranche_of_an_accepted_open(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, entry_tranches=2)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    proposal = {
        "action": "open",
        "symbol": "NVDA",
        "side": "buy",
        "size_pct": 0.05,
        "limit_price": 190.0,
        "thesis": "AI capex",
        "invalidation_price": 185.0,
        "time_stop_minutes": 60,
    }
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert result.proposals_accepted == 1
    assert len(result.orders_submitted) == 1
    pending = ledger.pending_tranche_for("NVDA")
    assert pending is not None
    assert pending["tranche_index"] == 1
    assert pending["tranche_total"] == 2
    ledger.close()


def test_run_cycle_submits_the_next_tranche_on_a_later_call(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, entry_tranches=2)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    proposal = {
        "action": "open",
        "symbol": "NVDA",
        "side": "buy",
        "size_pct": 0.05,
        "limit_price": 190.0,
        "thesis": "AI capex",
        "invalidation_price": 185.0,
        "time_stop_minutes": 60,
    }
    broker = FakeBroker(now=NOW)
    client = _sequenced_llm_client(
        _result_json(structured_output={"proposals": [proposal]}),
        _result_json(structured_output={"proposals": []}),
    )

    run_cycle("cycle_1", cfg, broker, ledger, market_data, client, AS_OF, NOW)
    result2 = run_cycle(
        "cycle_2",
        cfg,
        broker,
        ledger,
        market_data,
        client,
        AS_OF,
        NOW + dt.timedelta(minutes=20),
    )

    assert len(result2.orders_submitted) == 1
    assert ledger.pending_tranche_for("NVDA") is None
    order = ledger.get_order(result2.orders_submitted[0])
    assert order["tranche_index"] == 2
    assert order["tranche_total"] == 2
    ledger.close()


def test_run_cycle_rejects_a_proposal_over_the_new_positions_budget(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, new_positions_per_day=0)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    proposal = {
        "action": "open",
        "symbol": "NVDA",
        "side": "buy",
        "size_pct": 0.05,
        "limit_price": 190.0,
        "thesis": "AI capex",
        "invalidation_price": 185.0,
        "time_stop_minutes": 60,
    }
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert result.proposals_rejected == 1
    assert result.orders_submitted == []
    rejected = [r for r in ledger.proposals_for(AS_OF) if r["status"] == "rejected"]
    assert "new_positions_per_day" in rejected[0]["reason"]
    ledger.close()


def test_run_cycle_rejects_an_oversized_proposal_even_if_the_model_looks_compromised(
    config, tmp_path
):
    """Phase 8's actual backstop, not the text-pattern scanner in `security.py`: even a
    schema-valid Trader proposal that reads as if the model had been steered by injected
    content (a thesis quoting the corpus's own override/command phrase, sized to the
    schema's maximum) is still bounded by `validate_proposal`'s `single_name_cap_pct`
    check -- "no text from a source can raise a limit... by itself" holds structurally,
    independent of whether any scanner caught the phrase that (hypothetically) produced it.
    """
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    _label, injected_phrase, _categories = INJECTION_CORPUS[0]
    proposal = {
        "action": "open",
        "symbol": "NVDA",
        "side": "buy",
        "size_pct": 0.9,
        "limit_price": 190.0,
        "thesis": injected_phrase,
        "invalidation_price": 185.0,
        "time_stop_minutes": 60,
    }
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert result.proposals_rejected == 1
    assert result.orders_submitted == []
    rejected = [r for r in ledger.proposals_for(AS_OF) if r["status"] == "rejected"]
    assert "single_name_cap_pct" in rejected[0]["reason"]
    ledger.close()


def test_run_cycle_handles_a_malformed_trader_proposal_without_crashing(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    market_data = _FakeMarketData()
    malformed = {"action": "open"}  # missing every other required field
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [malformed]}))

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    assert result.proposals_seen == 1
    assert result.proposals_accepted == 0
    assert any("malformed trader proposal" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_cycle_returns_skipped_reason_once_the_daily_budget_is_exhausted(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    budget = cfg.intraday.budget.model_copy(update={"max_cost_usd_per_day": 1.0})
    cfg = cfg.model_copy(update={"intraday": cfg.intraday.model_copy(update={"budget": budget})})
    ledger = _ledger(tmp_path)
    ledger.record_model_call(AS_OF, "cycle_0", "trader", "haiku", "hash", 1000, 1000, cost_usd=1.0)
    client = _sequenced_llm_client(_result_json())

    result = run_cycle("cycle_1", cfg, FakeBroker(), ledger, _FakeMarketData(), client, AS_OF, NOW)

    assert result.skipped_reason is not None
    assert "budget" in result.skipped_reason
    ledger.close()


# -- run_cycle: the monitor runs first --------------------------------------------------------


def test_run_cycle_closes_an_invalidated_position_before_asking_the_trader(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, invalidation_price=185.0)
    market_data = _FakeMarketData(quotes={"NVDA": _quote(180.0)})  # through invalidation
    client = _sequenced_llm_client(_result_json())  # empty proposals -- shouldn't matter

    result = run_cycle("cycle_1", cfg, broker, ledger, market_data, client, AS_OF, NOW)

    assert broker.list_positions() == []
    assert any(i["message"] == "stopout:NVDA" for i in ledger.incidents())
    assert result.skipped_reason is None
    ledger.close()


# -- run_cycle: trim and close proposals -------------------------------------------------------


def test_run_cycle_close_proposal_cancels_the_resting_stop_and_exits(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, invalidation_price=185.0)
    resting_stop = ledger.open_stop_leg_for("NVDA")
    assert resting_stop is not None
    market_data = _FakeMarketData(quotes={"NVDA": _quote(192.0)})
    proposal = {"action": "close", "symbol": "NVDA"}
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    result = run_cycle("cycle_1", cfg, broker, ledger, market_data, client, AS_OF, NOW)

    assert broker.list_positions() == []
    assert broker.get_order_by_client_id(resting_stop["client_order_id"]).status == "canceled"
    assert len(result.orders_submitted) == 1
    ledger.close()


def test_run_cycle_trim_proposal_reprotects_the_remainder_at_the_original_invalidation(
    config, tmp_path
):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, price=190.0, invalidation_price=185.0)
    held_qty = broker.list_positions()[0].qty
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    # size_pct small enough to leave a real remainder.
    proposal = {"action": "trim", "symbol": "NVDA", "size_pct": 0.01}
    client = _sequenced_llm_client(_result_json(structured_output={"proposals": [proposal]}))

    run_cycle("cycle_1", cfg, broker, ledger, market_data, client, AS_OF, NOW)

    [position] = broker.list_positions()
    assert 0 < position.qty < held_qty
    new_stop = ledger.open_stop_leg_for("NVDA")
    assert new_stop is not None
    assert new_stop["broker_order_id"] is not None
    stop_order = broker.get_order_by_client_id(new_stop["client_order_id"])
    assert stop_order.status in ("new", "accepted")
    ledger.close()


# -- run_cycle: headlines reach the Trader (Phase 6) --------------------------------------


def test_run_cycle_passes_recent_headlines_into_the_trader_context(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="test")
    ledger.record_headline_seen(
        AS_OF, "alpaca_news", "h1", symbols=["NVDA"], headline="NVDA guidance raised"
    )
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})
    calls, client = _recording_llm_client(_result_json())

    run_cycle("cycle_1", cfg, FakeBroker(), ledger, market_data, client, AS_OF, NOW)

    payload = json.loads(calls[0]["stdin"])
    headlines = payload["sections"]["headlines"]
    assert any(h["headline"] == "NVDA guidance raised" for h in headlines)
    assert "headlines" in payload["untrusted"]
    ledger.close()


# -- run_research (Phase 6) ---------------------------------------------------------------


def test_run_research_is_skipped_when_trading_is_disabled(config, tmp_path, market):
    ledger = _ledger(tmp_path)  # config.intraday.trading_enabled defaults to False
    client = _sequenced_llm_client(_result_json())
    result = run_research(
        config,
        FakeBroker(),
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        ledger,
        client,
        AS_OF,
        "research",
        NOW,
    )
    assert result.skipped_reason is not None
    assert ledger.watchlist_for(AS_OF) == []
    ledger.close()


def test_run_research_records_a_brief_and_watchlist(config, tmp_path, market):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline="NVDA rallies", symbols=["NVDA"])]
    )
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0), "AMD": _quote(150.0)})
    candidate = {"symbol": "NVDA", "thesis": "AI capex cycle"}
    client = _sequenced_llm_client(
        _result_json(structured_output={"morning_brief": "quiet morning", "watchlist": [candidate]})
    )

    result = run_research(
        cfg, FakeBroker(), market_data, news_client, market, ledger, client, AS_OF, "research", NOW
    )

    assert result.brief_recorded
    assert result.watchlist_size == 1
    assert ledger.latest_brief()["content"] == {"text": "quiet morning"}
    assert [row["symbol"] for row in ledger.watchlist_for(AS_OF)] == ["NVDA"]
    assert ledger.headline_already_seen("alpaca_news", "a1")
    ledger.close()


def test_run_research_redacts_a_malicious_headline_before_the_model_and_the_ledger_see_it(
    config, tmp_path, market
):
    """Phase 8: `news_sections` (what the Researcher's own context payload carries) and
    `headlines_seen` (what the Trader reads later via `recent_headlines_for`) must both
    carry the redacted text, not the injection corpus's raw payload -- and a "warning"
    incident records the attempt."""
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    _label, malicious_headline, _categories = INJECTION_CORPUS[1]
    news_client = _FakeNewsClient(
        articles=[_FakeArticle(id="a1", headline=malicious_headline, symbols=["NVDA"])]
    )
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0), "AMD": _quote(150.0)})
    calls, client = _recording_llm_client(
        _result_json(structured_output={"morning_brief": "quiet morning", "watchlist": []})
    )

    run_research(
        cfg, FakeBroker(), market_data, news_client, market, ledger, client, AS_OF, "research", NOW
    )

    sent_payload = json.loads(calls[0]["stdin"])
    sent_news = sent_payload["sections"]["news"]
    assert all(malicious_headline not in article["headline"] for article in sent_news)
    assert any("[redacted:" in article["headline"] for article in sent_news)
    stored = ledger.recent_headlines_for(AS_OF, ["NVDA"])
    assert malicious_headline not in stored[0]["headline"]
    incidents = [i["message"] for i in ledger.incidents()]
    assert any("suspected prompt injection" in m for m in incidents)
    ledger.close()


def test_run_research_is_a_no_op_on_a_repeat_call_the_same_day(config, tmp_path, market):
    """A retried GitHub Actions attempt must not re-call the model or double the
    watchlist -- the same `ON CONFLICT DO NOTHING` reasoning `record_session_open_equity`
    already uses."""
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    ledger.record_watchlist_item(AS_OF, "NVDA", thesis="already here")
    client = _sequenced_llm_client(_result_json())

    result = run_research(
        cfg,
        FakeBroker(),
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        ledger,
        client,
        AS_OF,
        "research",
        NOW,
    )

    assert result.skipped_reason is not None
    assert ledger.model_calls_for(AS_OF) == []
    ledger.close()


def test_run_research_handles_a_malformed_response_without_crashing(config, tmp_path, market):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    client = _sequenced_llm_client(_result_json(structured_output={"not": "the right shape"}))

    result = run_research(
        cfg,
        FakeBroker(),
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        ledger,
        client,
        AS_OF,
        "research",
        NOW,
    )

    assert result.skipped_reason is not None
    assert any("malformed researcher response" in i["message"] for i in ledger.incidents())
    ledger.close()


def test_run_research_returns_skipped_reason_once_the_daily_budget_is_exhausted(
    config, tmp_path, market
):
    cfg = _tradeable_config(config, tmp_path)
    budget = cfg.intraday.budget.model_copy(update={"max_cost_usd_per_day": 1.0})
    cfg = cfg.model_copy(update={"intraday": cfg.intraday.model_copy(update={"budget": budget})})
    ledger = _ledger(tmp_path)
    ledger.record_model_call(
        AS_OF, "cycle_0", "researcher", "sonnet", "hash", 1000, 1000, cost_usd=1.0
    )
    client = _sequenced_llm_client(_result_json())

    result = run_research(
        cfg,
        FakeBroker(),
        _FakeMarketData(),
        _FakeNewsClient(),
        market,
        ledger,
        client,
        AS_OF,
        "research",
        NOW,
    )

    assert result.skipped_reason is not None
    assert "budget" in result.skipped_reason
    ledger.close()


# -- run_carry --------------------------------------------------------------------------------


def test_run_carry_closes_an_intraday_horizon_position(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, horizon="intraday")
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})

    result = run_carry(cfg, broker, ledger, market_data, AS_OF, NOW)

    assert result.closed == ["NVDA"]
    assert broker.list_positions() == []
    ledger.close()


def test_run_carry_carries_a_multi_day_position_under_the_cap(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, overnight_gross_cap_pct=1.0)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(broker, ledger, cfg, horizon="multi-day", confidence=0.8)
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0)})

    result = run_carry(cfg, broker, ledger, market_data, AS_OF, NOW)

    assert result.carried == ["NVDA"]
    assert result.closed == []
    assert len(broker.list_positions()) == 1
    ledger.close()


def test_run_carry_closes_the_lowest_confidence_position_over_the_overnight_cap(config, tmp_path):
    cfg = _tradeable_config(config, tmp_path, overnight_gross_cap_pct=0.05)
    ledger = _ledger(tmp_path)
    broker = FakeBroker(now=NOW)
    _seed_open_position(
        broker, ledger, cfg, symbol="NVDA", horizon="multi-day", confidence=0.9, price=190.0
    )
    _seed_open_position(
        broker, ledger, cfg, symbol="AMD", horizon="multi-day", confidence=0.2, price=150.0
    )
    market_data = _FakeMarketData(quotes={"NVDA": _quote(190.0), "AMD": _quote(150.0)})

    result = run_carry(cfg, broker, ledger, market_data, AS_OF, NOW)

    assert "AMD" in result.closed_for_cap  # lowest confidence closed first
    remaining_symbols = {p.symbol for p in broker.list_positions()}
    assert "AMD" not in remaining_symbols
    ledger.close()
