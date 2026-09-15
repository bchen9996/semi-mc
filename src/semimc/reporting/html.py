"""Self-contained HTML report.

One file, no external assets: charts are embedded as base64 PNGs and the stylesheet is
inline, so the report can be emailed or opened years later without the repo. Built with
string templates rather than a templating engine, to avoid adding a dependency for what is
a handful of substitutions.

The limitations section is not optional garnish. A report that shows a 90% interval
without saying that the universe is survivorship-biased and the drift is an assumption is
a misleading document, so it is assembled from the run's own facts every time.
"""

from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

import pandas as pd

STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 16px 64px;
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
  color: #1c1d1f; background: #fbfbfa;
}
.wrap { max-width: 1040px; margin: 0 auto; }
header { padding: 40px 0 8px; border-bottom: 2px solid #1c1d1f; margin-bottom: 28px; }
h1 { font-size: 27px; margin: 0 0 6px; letter-spacing: -0.2px; }
h2 { font-size: 20px; margin: 40px 0 10px; padding-bottom: 5px; border-bottom: 1px solid #d8d6d1; }
h3 { font-size: 16px; margin: 24px 0 8px; }
.sub { color: #5f6266; font-size: 13px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; margin: 12px 0 20px; }
th, td { padding: 5px 9px; text-align: right; border-bottom: 1px solid #e6e4df; }
th { background: #f0eeea; font-weight: 600; text-align: right; position: sticky; top: 0; }
th:first-child, td:first-child { text-align: left; }
tbody tr:hover { background: #f5f3ef; }
.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
figure { margin: 18px 0; }
figure img { width: 100%; height: auto; border: 1px solid #e6e4df; border-radius: 4px; }
figcaption { font-size: 12px; color: #5f6266; margin-top: 6px; }
.note {
  background: #fff8e8; border-left: 3px solid #d8a13a; padding: 12px 16px;
  margin: 18px 0; font-size: 13.5px; border-radius: 0 4px 4px 0;
}
.warn { background: #fdeeec; border-left-color: #c1440e; }
code { background: #f0eeea; padding: 1px 5px; border-radius: 3px; font-size: 12.5px; }
.kv { display: grid; grid-template-columns: 200px 1fr; gap: 4px 14px; font-size: 13.5px; }
.kv dt { color: #5f6266; }
.kv dd { margin: 0; font-variant-numeric: tabular-nums; }
footer { margin-top: 48px; padding-top: 14px; border-top: 1px solid #d8d6d1;
         font-size: 12px; color: #5f6266; }
@media (prefers-color-scheme: dark) {
  body { background: #17181a; color: #e8e6e3; }
  header { border-bottom-color: #e8e6e3; }
  h2 { border-bottom-color: #33363a; }
  th { background: #232529; }
  th, td { border-bottom-color: #2c2f33; }
  tbody tr:hover { background: #1e2023; }
  .sub, figcaption, .kv dt, footer { color: #9a9da2; }
  .note { background: #2a2416; border-left-color: #d8a13a; }
  .warn { background: #2d1b17; border-left-color: #c1440e; }
  code, figure img { background: #232529; border-color: #33363a; }
}
@media (max-width: 640px) { .kv { grid-template-columns: 1fr; } h1 { font-size: 22px; } }
"""

LIMITATIONS = [
    (
        "Survivorship bias",
        "The universe is the set of semiconductor leaders as of today. Names that were "
        "prominent and then failed or were acquired are absent, so every historical "
        "statistic here is measured on a sample selected for having survived. Realized "
        "volatility is understated and drift is overstated by an unknown amount.",
    ),
    (
        "Drift is an assumption, not an estimate",
        "Expected return comes from the configured drift policy, not from the data. Under "
        "CAPM a beta-2 name is handed roughly 14% a year for the whole horizon. Nothing "
        "in the backtest validates that number -- calibration tests are far more sensitive "
        "to the width of the distribution than to its centre.",
    ),
    (
        "Regime change",
        "Every model is fitted to a rolling five-year window and assumes the next period "
        "resembles it. Export controls, an AI capex reversal, or a return of the classic "
        "semiconductor cycle would all break that assumption, and the model would give no "
        "warning.",
    ),
    (
        "Parameter uncertainty is not propagated",
        "Parameters are treated as known once estimated. A GARCH persistence of 0.97 "
        "estimated with a standard error of 0.02 is simulated as exactly 0.97, so the "
        "intervals here are narrower than a fully Bayesian treatment would give.",
    ),
    (
        "Monte Carlo error",
        "Tail statistics from a finite path count carry their own error, separate from "
        "model error. The convergence diagnostic in each simulate run quantifies it.",
    ),
    (
        "Not investment advice",
        "This is an educational project. Nothing here is a price target, a recommendation, "
        "or a claim that the distributions are correct -- only a record of what a set of "
        "stated assumptions implies.",
    ),
]


def _table(frame: pd.DataFrame, float_format: str = "{:,.4f}", max_rows: int = 400) -> str:
    """Render a frame as an HTML table, truncating politely if it is enormous."""
    if frame is None or len(frame) == 0:
        return '<p class="sub">No data.</p>'
    shown = frame.head(max_rows)
    body = shown.to_html(
        float_format=lambda v: float_format.format(v),
        border=0,
        na_rep="&ndash;",
        escape=False,
    )
    suffix = (
        f'<p class="sub">Showing {max_rows:,} of {len(frame):,} rows; the full table is in '
        "the run folder as CSV.</p>"
        if len(frame) > max_rows
        else ""
    )
    return f'<div class="scroll">{body}</div>{suffix}'


def _image(base64_png: str, caption: str = "") -> str:
    return (
        f'<figure><img alt="{html.escape(caption)}" src="data:image/png;base64,{base64_png}">'
        + (f"<figcaption>{html.escape(caption)}</figcaption>" if caption else "")
        + "</figure>"
    )


def _section(title: str, *blocks: str) -> str:
    content = "\n".join(b for b in blocks if b)
    return f"<h2>{html.escape(title)}</h2>\n{content}" if content else ""


def _markdown_lite(text: str) -> str:
    """Render the subset of Markdown the written summaries use: headings, bullets, bold."""
    out: list[str] = []
    in_list = False
    for raw in text.splitlines():
        line = html.escape(raw.rstrip())
        while "**" in line:
            line = line.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
        line = line.replace("`", "")
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{line[2:]}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("### "):
            out.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            out.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("# "):
            pass  # the page already has a title
        elif line:
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def build_report(
    title: str,
    subtitle: str,
    metadata: dict[str, str],
    sections: list[tuple[str, str]],
    limitations: list[tuple[str, str]] | None = None,
) -> str:
    """Assemble the full HTML document.

    Parameters
    ----------
    title, subtitle
        Page heading and the line beneath it.
    metadata
        Key-value pairs describing the run (seed, data range, model, config path).
    sections
        ``(heading, html_body)`` pairs, rendered in order.
    limitations
        Overrides the default limitations list; the default is almost always right.

    Returns
    -------
    str
        A complete, self-contained HTML document.
    """
    items = "\n".join(
        f"<dt>{html.escape(str(k))}</dt><dd>{html.escape(str(v))}</dd>" for k, v in metadata.items()
    )
    body = "\n".join(_section(heading, content) for heading, content in sections)
    caveats = "\n".join(
        f"<h3>{html.escape(name)}</h3><p>{html.escape(text)}</p>"
        for name, text in (limitations or LIMITATIONS)
    )
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{STYLE}</style></head>
<body><div class="wrap">
<header>
  <h1>{html.escape(title)}</h1>
  <p class="sub">{html.escape(subtitle)}</p>
</header>
<div class="note warn">
  <strong>Educational project, not investment advice.</strong> These are distributions
  implied by stated assumptions, not predictions. No number here is a price target.
</div>
<h2>Run</h2>
<dl class="kv">{items}</dl>
{body}
<h2>Limitations</h2>
{caveats}
<footer>Generated by semimc on {generated}. Reproduce this run from
<code>config_snapshot.json</code> and the seed recorded above.</footer>
</div></body></html>
"""


def write_report(path: Path, content: str) -> Path:
    """Write the report to disk."""
    path = Path(path)
    path.write_text(content, encoding="utf-8")
    return path
