"""Config is read from the environment at call time, and the upstream host is
never hardcoded outside the documented default."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import config
from app.main import app


def test_upstream_base_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("FX_UPSTREAM_BASE", raising=False)
    assert config.upstream_base() == "https://api.frankfurter.dev"


def test_upstream_base_follows_the_environment(monkeypatch):
    monkeypatch.setenv("FX_UPSTREAM_BASE", "http://127.0.0.1:9")
    assert config.upstream_base() == "http://127.0.0.1:9"


def test_empty_upstream_base_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("FX_UPSTREAM_BASE", "")
    assert config.upstream_base() == "https://api.frankfurter.dev"


def test_upstream_url_adds_the_v1_prefix(monkeypatch):
    # The real API 404s without /v1, while the documented default base has no
    # prefix, so the service has to add it.
    monkeypatch.setenv("FX_UPSTREAM_BASE", "http://fake.test")
    assert config.upstream_url("latest") == "http://fake.test/v1/latest"
    assert config.upstream_url("2026-08-28") == "http://fake.test/v1/2026-08-28"


def test_upstream_url_tolerates_a_trailing_slash(monkeypatch):
    monkeypatch.setenv("FX_UPSTREAM_BASE", "http://fake.test/")
    assert config.upstream_url("latest") == "http://fake.test/v1/latest"


def test_port_defaults_to_8080(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert config.port() == 8080


def test_port_follows_the_environment(monkeypatch):
    monkeypatch.setenv("PORT", "9999")
    assert config.port() == 9999


def test_health_does_not_touch_the_upstream(monkeypatch):
    # Pointed at a closed port: /health must still answer.
    monkeypatch.setenv("FX_UPSTREAM_BASE", "http://127.0.0.1:9")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
