"""Common fixtures for the Duke Energy tests."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Generator
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.duke_energy.const import DOMAIN
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _make_id_token(
    user_id: str = "test-user-id",
    email: str = "test@example.com",
    expires_in: int = 3600,
) -> str:
    """Build a minimal unsigned JWT for testing.

    PyJWT's decode(options={"verify_signature": False}) only needs the
    base64-encoded header.payload segments to be valid JSON — the signature
    can be anything.
    """
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "internal_identifier": user_id,
                "email": email,
                "exp": int(time.time()) + expires_in,
            }
        ).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:  # noqa: ARG001
    """Allow custom components to be loaded in every test."""


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Prevent real async_setup_entry from running in config-flow tests."""
    with patch(
        "custom_components.duke_energy.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        yield mock_setup_entry


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mocked OAuth2 config entry (not yet added to hass)."""
    id_token = _make_id_token()
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id",
        title="test@example.com",
        version=2,
        minor_version=1,
        data={
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "test-access-token",
                "id_token": id_token,
                "refresh_token": "test-refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "expires_at": time.time() + 3600,
            },
        },
    )


@pytest.fixture
def mock_recorder() -> Generator[Mock]:
    """Mock the HA recorder instance and all statistics helpers.

    Patches all recorder interactions in the coordinator so tests never need
    a real recorder process or database.
    """
    recorder_instance = Mock()
    # async_add_executor_job is awaited; it runs get_last_statistics /
    # statistics_during_period synchronously.  Use a passthrough so that
    # module-level patches on those functions are actually respected — a
    # fixed return_value would ignore the callable argument entirely.
    async def _passthrough(func, *args, **kwargs):
        return func(*args, **kwargs)

    recorder_instance.async_add_executor_job = AsyncMock(side_effect=_passthrough)
    recorder_instance.async_clear_statistics = Mock()

    with (
        patch(
            "custom_components.duke_energy.coordinator.get_instance",
            return_value=recorder_instance,
        ),
        patch(
            "custom_components.duke_energy.coordinator.async_add_external_statistics",
        ) as mock_add_stats,
    ):
        recorder_instance.mock_add_stats = mock_add_stats
        yield recorder_instance


@pytest.fixture
def mock_api() -> Generator[AsyncMock]:
    """Mock the DukeEnergy client and the OAuth2 session plumbing.

    Patches:
    - ``custom_components.duke_energy.DukeEnergy`` — the class instantiated in
      ``async_setup_entry``; the coordinator receives the already-built instance,
      so only one patch site is needed.
    - ``OAuth2Session.async_ensure_token_valid`` — avoids real token refresh.
    - ``async_get_config_entry_implementation`` — returns a dummy implementation
      so HA doesn't try to look up a registered OAuth2 provider.
    """
    with (
        patch(
            "custom_components.duke_energy.DukeEnergy",
            autospec=True,
        ) as mock_cls,
        patch(
            "custom_components.duke_energy.DukeEnergyAuth",
            autospec=True,
        ),
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
            return_value=None,
        ),
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            return_value=AsyncMock(),
        ),
    ):
        mock = mock_cls.return_value
        mock.get_meters = AsyncMock(return_value={})
        mock.get_energy_usage = AsyncMock(return_value={"data": {}, "missing": []})
        yield mock


@pytest.fixture
def mock_api_with_meters(mock_api: AsyncMock) -> AsyncMock:
    """Extend mock_api with a single electric meter and one reading."""
    mock_api.get_meters.return_value = {
        "123": {
            "serialNum": "123",
            "serviceType": "ELECTRIC",
            "agreementActiveDate": "2000-01-01",
        },
    }
    mock_api.get_energy_usage.return_value = {
        "data": {
            dt_util.now(): {
                "energy": 1.3,
                "temperature": 70,
            }
        },
        "missing": [],
    }
    return mock_api


@pytest.fixture
def mock_api_with_gas_meter(mock_api: AsyncMock) -> AsyncMock:
    """Extend mock_api with a single gas meter and one reading."""
    mock_api.get_meters.return_value = {
        "456": {
            "serialNum": "456",
            "serviceType": "GAS",
            "agreementActiveDate": "2000-01-01",
        },
    }
    mock_api.get_energy_usage.return_value = {
        "data": {dt_util.now(): {"energy": 2.5, "temperature": 68}},
        "missing": [],
    }
    return mock_api
