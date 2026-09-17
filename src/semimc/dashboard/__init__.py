"""Streamlit dashboard (brief's last stretch item).

Sits on top of the CLI rather than re-implementing it: every action the dashboard offers
launches the same ``semimc`` command a terminal user would type, as a subprocess, and
renders the run directory it produces. ``jobs.py`` has no Streamlit dependency and is
covered by the normal test suite; ``app.py`` is the Streamlit entry point, run with
``streamlit run src/semimc/dashboard/app.py`` and not imported by anything else.
"""
