"""Test setup — runs before any app imports.

Settings() is constructed at app.config import time and requires DB fields,
so we set dummies here. setdefault() means a real .env still wins for
integration runs.
"""
import os

os.environ.setdefault("CLOUD_SQL_INSTANCE", "test:us-central1:test")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("DB_NAME", "test")