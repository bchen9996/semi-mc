"""`SignalsConfig.origin_frequency` -- the higher-frequency Phase 4B signal search knob.

Mirrors `test_config_paper.py`'s style: default/override/extra="forbid" checks for one
new config field.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from semimc.config import load_config


def test_origin_frequency_defaults_to_monthly():
    """Monthly by default -- a higher-frequency search is opt-in, never a changed default."""
    assert load_config().signals.origin_frequency == "ME"


def test_origin_frequency_is_overridable_with_set():
    cfg = load_config(overrides=["signals.origin_frequency=W-FRI"])
    assert cfg.signals.origin_frequency == "W-FRI"


def test_unknown_signals_key_is_still_rejected():
    """`extra='forbid'` must still cover signals.* after adding the new field."""
    with pytest.raises(ValidationError, match="signals"):
        load_config(overrides=["signals.not_a_real_field=1"])
