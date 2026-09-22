"""Shared fixtures: an isolated state directory and database per test."""

from __future__ import annotations

import sqlite3

import pytest

from busypanel import db
from busypanel.config import settings


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    """Point every test at its own state directory and database."""
    monkeypatch.setenv("BUSYPANEL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", tmp_path / "busypanel.db")
    # The login gate must be off unless a test turns it on.
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "business_name", "")
    return tmp_path


@pytest.fixture
def con(state_dir) -> sqlite3.Connection:
    c = db.connect(state_dir / "busypanel.db")
    yield c
    c.close()


@pytest.fixture
def client(state_dir):
    from fastapi.testclient import TestClient

    from busypanel.web import app as webapp

    return TestClient(webapp.app)


def add_client(con: sqlite3.Connection, name: str = "Acme", rate: int = 20000) -> int:
    cur = con.execute(
        "INSERT INTO client (name, video_rate_cents, created_at) VALUES (?,?,?)",
        (name, rate, "2026-08-01"),
    )
    return int(cur.lastrowid)


def add_video(con: sqlite3.Connection, client_id: int, shot_on: str,
              title: str, rate_cents: int) -> int:
    cur = con.execute(
        "INSERT INTO video (client_id, shot_on, title, rate_cents, created_at) "
        "VALUES (?,?,?,?,?)",
        (client_id, shot_on, title, rate_cents, "2026-08-01"),
    )
    return int(cur.lastrowid)
