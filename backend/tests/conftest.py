import pytest

from network_copilot.app import create_app
from network_copilot.config import TestConfig


@pytest.fixture
def app():
    application = create_app(TestConfig)
    yield application


@pytest.fixture
def client(app):
    return app.test_client()
