"""HTML dashboard (brief's last stretch item).

Sits on top of the CLI rather than re-implementing it: every action the dashboard offers
launches the same ``semimc`` command a terminal user would type, as a subprocess, and
renders the run directory it produces. ``jobs.py`` has no web-framework dependency and is
covered by the normal test suite; ``server.py`` is the stdlib ``http.server`` backend
(``uv run semimc dashboard``), serving the static frontend in ``web/`` and a small JSON API
that ``web/app.js`` calls.
"""
