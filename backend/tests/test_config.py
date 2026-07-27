"""Configuration resolution, especially the database path.

A relative sqlite path in DATABASE_URL would otherwise be resolved by Flask
against its instance folder, so `flask db upgrade` and a plain script could end
up using two different database files.
"""

import importlib

import pytest

from network_copilot.app import create_app
from network_copilot.config import BASE_DIR


@pytest.fixture
def reloaded_config(monkeypatch):
    def _load(database_url: str | None):
        if database_url is None:
            monkeypatch.delenv("DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("DATABASE_URL", database_url)
        module = importlib.import_module("network_copilot.config")
        return importlib.reload(module).Config

    yield _load
    importlib.reload(importlib.import_module("network_copilot.config"))


def test_relative_sqlite_path_is_anchored_to_the_backend_directory(reloaded_config):
    config = reloaded_config("sqlite:///network_copilot.db")
    assert config.SQLALCHEMY_DATABASE_URI == (
        f"sqlite:///{BASE_DIR / 'network_copilot.db'}"
    )


def test_nested_relative_sqlite_path_is_anchored(reloaded_config):
    config = reloaded_config("sqlite:///data/lab.db")
    assert config.SQLALCHEMY_DATABASE_URI.startswith(f"sqlite:///{BASE_DIR}")
    assert config.SQLALCHEMY_DATABASE_URI.endswith("lab.db")


def test_absolute_sqlite_path_is_left_alone(reloaded_config):
    absolute = BASE_DIR / "explicit.db"
    config = reloaded_config(f"sqlite:///{absolute}")
    assert config.SQLALCHEMY_DATABASE_URI == f"sqlite:///{absolute}"


def test_in_memory_sqlite_is_left_alone(reloaded_config):
    config = reloaded_config("sqlite:///:memory:")
    assert config.SQLALCHEMY_DATABASE_URI == "sqlite:///:memory:"


def test_non_sqlite_urls_are_left_alone(reloaded_config):
    url = "postgresql+psycopg://user@localhost:5432/network_copilot"
    assert reloaded_config(url).SQLALCHEMY_DATABASE_URI == url


def test_default_database_is_under_the_backend_directory(reloaded_config):
    config = reloaded_config(None)
    assert config.SQLALCHEMY_DATABASE_URI == (
        f"sqlite:///{BASE_DIR / 'network_copilot.db'}"
    )


def test_every_entry_point_resolves_to_the_same_file(monkeypatch):
    """The app must not depend on Flask's instance folder for its database."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///network_copilot.db")
    import network_copilot.config as config_module

    importlib.reload(config_module)
    app = create_app(config_module.Config)

    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    assert str(BASE_DIR) in uri
    assert app.instance_path not in uri
    importlib.reload(config_module)
