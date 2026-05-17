import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEST_ENV_DEFAULTS = {
    'APP_VERSION': '1.1.5',
    'REQUIRE_SERVER_CONNECTION_PIN': 'false',
    'SERVER_CONNECTION_PIN': '',
}

for key, value in TEST_ENV_DEFAULTS.items():
    os.environ[key] = value

from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402


@contextmanager
def temporary_env(values: dict[str, str]) -> Iterator[None]:
    original: dict[str, str | None] = {}
    for key, value in values.items():
        original[key] = os.environ.get(key)
        os.environ[key] = value

    try:
        yield
    finally:
        for key, previous in original.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def build_test_client(env: Optional[dict[str, str]] = None) -> TestClient:
    get_settings.cache_clear()
    if env:
        for key, value in env.items():
            os.environ[key] = value

    app = create_app()
    return TestClient(app)


@pytest.fixture
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()
