"""Run directories.

Every command that produces output writes one ``runs/<timestamp>/`` folder holding the
artifacts, a snapshot of the exact configuration used, the random seed, and the date range
of the data that went in. That is the whole reproducibility story: given the folder, the
run can be repeated exactly, and if it cannot, something was read from outside the config
and that is a bug.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

LOGGER = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
CONFIG_SNAPSHOT_NAME = "config_snapshot.json"


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp | dt.datetime | dt.date):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):  # numpy scalars
        return value.item()
    raise TypeError(f"cannot serialize {type(value).__name__} to JSON")


@dataclass
class RunDirectory:
    """A single run's output folder."""

    path: Path
    command: str
    config_snapshot: dict[str, Any]
    seed: int | None = None
    manifest: dict[str, Any] = field(default_factory=dict)

    # -- creation -------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        root: Path,
        command: str,
        config_snapshot: dict[str, Any],
        seed: int | None = None,
        label: str | None = None,
    ) -> RunDirectory:
        """Make a fresh timestamped run folder.

        A same-second collision gets a numeric suffix rather than overwriting, since
        losing a previous run's outputs silently would be worse than an ugly folder name.
        """
        root.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{command}" + (f"-{label}" if label else "")
        path = root / name
        suffix = 1
        while path.exists():
            path = root / f"{name}-{suffix}"
            suffix += 1
        path.mkdir(parents=True)

        run = cls(path=path, command=command, config_snapshot=config_snapshot, seed=seed)
        run.manifest = {
            "command": command,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "seed": seed,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "artifacts": [],
        }
        (path / CONFIG_SNAPSHOT_NAME).write_text(
            json.dumps(config_snapshot, indent=2, default=_json_default), encoding="utf-8"
        )
        return run

    # -- recording ------------------------------------------------------------------

    def record(self, key: str, value: Any) -> None:
        """Add a key to the manifest (data ranges, model parameters, timings)."""
        self.manifest[key] = value

    def record_data_range(self, frame: pd.DataFrame | pd.Series, name: str = "data") -> None:
        """Record the first and last date and the row count of an input frame."""
        if len(frame) == 0:
            self.manifest[f"{name}_range"] = None
            return
        self.manifest[f"{name}_range"] = {
            "first_date": str(pd.Timestamp(frame.index.min()).date()),
            "last_date": str(pd.Timestamp(frame.index.max()).date()),
            "rows": int(len(frame)),
            "columns": list(map(str, getattr(frame, "columns", []))),
        }

    def _register(self, path: Path) -> Path:
        self.manifest.setdefault("artifacts", []).append(path.name)
        return path

    # -- artifact writers -----------------------------------------------------------

    def write_table(self, frame: pd.DataFrame | pd.Series, name: str, index: bool = True) -> Path:
        """Write a CSV artifact and register it in the manifest."""
        path = self.path / f"{name}.csv"
        if isinstance(frame, pd.Series):
            frame.to_frame(name=frame.name or "value").to_csv(path, index=index)
        else:
            frame.to_csv(path, index=index)
        return self._register(path)

    def write_json(self, payload: Any, name: str) -> Path:
        path = self.path / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
        return self._register(path)

    def write_text(self, text: str, name: str, extension: str = "md") -> Path:
        path = self.path / f"{name}.{extension}"
        path.write_text(text, encoding="utf-8")
        return self._register(path)

    def figure_path(self, name: str) -> Path:
        """Path a chart should be saved to. Registered immediately."""
        return self._register(self.path / f"{name}.png")

    def close(self) -> Path:
        """Write the manifest. Call once, at the end of the run."""
        path = self.path / MANIFEST_NAME
        path.write_text(
            json.dumps(self.manifest, indent=2, default=_json_default), encoding="utf-8"
        )
        LOGGER.info("run written to %s", self.path)
        return self.path


def find_run(root: Path, selector: str = "latest") -> Path:
    """Resolve a run selector to a folder.

    ``latest`` picks the most recently created run; anything else is treated as a folder
    name, or a unique prefix of one.
    """
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"no runs directory at {root}")
    candidates = sorted(p for p in root.iterdir() if p.is_dir())
    if not candidates:
        raise FileNotFoundError(f"{root} contains no runs")

    if selector == "latest":
        return candidates[-1]
    exact = root / selector
    if exact.is_dir():
        return exact
    matches = [p for p in candidates if p.name.startswith(selector)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"no run matching {selector!r} in {root}")
    raise ValueError(f"{selector!r} matches {len(matches)} runs: {[p.name for p in matches]}")


def load_manifest(run_path: Path) -> dict[str, Any]:
    """Read a run's manifest."""
    path = Path(run_path) / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(f"{run_path} has no {MANIFEST_NAME}")
    return json.loads(path.read_text(encoding="utf-8"))
