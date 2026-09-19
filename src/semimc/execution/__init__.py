"""Order execution against a broker (semis_paper_trading_brief.md).

``broker.py`` defines the small :class:`~semimc.execution.broker.Broker` Protocol --
account, positions, asset lookup, clock and calendar, submit, look up by client order id,
cancel, list open orders -- and nothing above this package may depend on a real brokerage
SDK type directly. ``fake_broker.py``'s :class:`~semimc.execution.fake_broker.FakeBroker`
implements the same Protocol in memory with injectable fills and failures, and is the only
broker the normal test suite constructs. ``alpaca_broker.py``'s
:class:`~semimc.execution.alpaca_broker.AlpacaPaperBroker` implements the same Protocol
against Alpaca's real paper-trading API (never live -- ``paper=True`` is hardcoded, not a
parameter). Verified both offline (``tests/test_alpaca_broker.py``, mocked, runs in the
default suite) and against a real paper account (``tests/test_alpaca_broker_integration.
py``'s ``@pytest.mark.network`` tests, excluded by default -- ``uv run pytest -m network``
to run them once ``.env`` has real paper keys). ``live/execute.py`` and ``live/mark.py``
(Phase 5) are the only code that constructs one outside tests, via
``AlpacaPaperBroker.from_env()``.
"""
