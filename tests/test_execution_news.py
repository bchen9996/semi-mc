"""``execution/news.py``: S4's news source for the Researcher (Phase 6). Mocked
``NewsClient``, same boundary-mocking approach as ``tests/test_execution_market_data.py`` --
no network, no credentials.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest
from alpaca.data.models.news import News

from semimc.execution.news import AlpacaNewsClient

START = dt.datetime(2026, 1, 5, 0, 0, 0, tzinfo=dt.UTC)
END = dt.datetime(2026, 1, 5, 8, 0, 0, tzinfo=dt.UTC)


def _article(id_: int, headline: str, symbols: list[str]) -> News:
    return News.model_construct(
        id=id_,
        headline=headline,
        source="benzinga",
        url="https://example.com",
        summary="",
        created_at=START,
        updated_at=START,
        symbols=symbols,
        author="",
        content="",
        images=[],
    )


def _client_returning(articles: list[News]) -> MagicMock:
    client = MagicMock()
    client.get_news.return_value = MagicMock(data={"news": articles})
    return client


def test_get_news_returns_the_flat_article_list():
    articles = [_article(1, "NVDA rallies", ["NVDA"]), _article(2, "AMD guidance cut", ["AMD"])]
    client = AlpacaNewsClient(_client_returning(articles))
    result = client.get_news(["NVDA", "AMD"], start=START, end=END)
    assert result == articles


def test_get_news_comma_joins_symbols_into_one_request():
    sdk_client = _client_returning([])
    client = AlpacaNewsClient(sdk_client)
    client.get_news(["NVDA", "AMD", "TSM"], start=START, end=END, limit=10)
    request = sdk_client.get_news.call_args[0][0]
    assert request.symbols == "NVDA,AMD,TSM"
    assert request.start == START
    assert request.end == END
    assert request.limit == 10
    assert request.exclude_contentless is True


def test_get_news_is_an_empty_list_when_no_coverage():
    client = AlpacaNewsClient(_client_returning([]))
    assert client.get_news(["NVDA"], start=START, end=END) == []


def test_from_env_raises_a_clear_error_when_keys_are_missing(monkeypatch):
    monkeypatch.delenv("ALPACA_S4_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_S4_SECRET_KEY", raising=False)
    monkeypatch.setattr("semimc.execution.news.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match=".env"):
        AlpacaNewsClient.from_env()


def test_from_env_defaults_to_s4s_own_key_pair(monkeypatch):
    from alpaca.data.historical.news import NewsClient

    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALPACA_S4_API_KEY", "s4-key")
    monkeypatch.setenv("ALPACA_S4_SECRET_KEY", "s4-secret")
    client = AlpacaNewsClient.from_env()
    assert isinstance(client._client, NewsClient)
