"""Desk-side developer tools (not deployed).

``python -m tools.<name>`` from ``backend/`` with the venv active.
Unlike ``workers/`` these never run in Cloud Run — they are offline
diagnostics that must work with no Redis and no Cloud SQL access.
"""
