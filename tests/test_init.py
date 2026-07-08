"""Tests for Duke Energy async_setup_entry and async_unload_entry."""

from __future__ import annotations

import pytest

from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from custom_components.duke_energy import async_setup_entry
from custom_components.duke_energy.const import DOMAIN
from custom_components.duke_energy.coordinator import DukeEnergyCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_setup_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder: object,
) -> None:
    """async_setup_entry wires auth → client → coordinator and returns True."""
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    with (
        patch(
            "custom_components.duke_energy.DukeEnergyAuth", autospec=True
        ) as mock_auth_cls,
        patch(
            "custom_components.duke_energy.DukeEnergy", autospec=True
        ) as mock_client_cls,
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            return_value=AsyncMock(),
        ),
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
            return_value=None,
        ),
        patch(
            "custom_components.duke_energy.coordinator.DukeEnergyCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ),
    ):
        result = await async_setup_entry(hass, mock_config_entry)

    assert result is True

    # DukeEnergyAuth receives the aiohttp ClientSession and an OAuth2Session
    mock_auth_cls.assert_called_once()
    _, oauth_session_arg = mock_auth_cls.call_args.args
    assert isinstance(oauth_session_arg, OAuth2Session)

    # DukeEnergy client receives the auth instance
    mock_client_cls.assert_called_once_with(mock_auth_cls.return_value)

    # Coordinator is stored on the entry
    assert isinstance(mock_config_entry.runtime_data, DukeEnergyCoordinator)


async def test_setup_entry_raises_on_missing_token(
    hass: HomeAssistant,
) -> None:
    """async_setup_entry raises ConfigEntryAuthFailed when token is absent (v1 entry)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        data={},  # no "token" key
    )
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


async def test_setup_entry_raises_on_token_validation_failure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """async_setup_entry raises ConfigEntryAuthFailed when token refresh fails."""
    mock_config_entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            return_value=AsyncMock(),
        ),
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
            side_effect=Exception("token expired"),
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, mock_config_entry)


async def test_unload_entry_keeps_statistics(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AsyncMock,
    mock_recorder: Mock,
) -> None:
    """Unload (which runs on every reload) must NOT clear statistics."""
    mock_config_entry.add_to_hass(hass)
    coordinator = DukeEnergyCoordinator(hass, mock_api, mock_config_entry)
    coordinator._statistic_ids = {
        "duke_energy:electric_123_energy_consumption",
    }

    await mock_config_entry._async_process_on_unload(hass)

    mock_recorder.async_clear_statistics.assert_not_called()


async def test_remove_entry_clears_statistics(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """async_remove_entry clears every statistic whose source is this domain."""
    from custom_components.duke_energy import async_remove_entry

    mock_config_entry.add_to_hass(hass)

    recorder_instance = Mock()

    async def _passthrough(func, *args, **kwargs):
        return func(*args, **kwargs)

    recorder_instance.async_add_executor_job = AsyncMock(side_effect=_passthrough)
    recorder_instance.async_clear_statistics = Mock()

    all_stats = [
        {
            "statistic_id": "duke_energy:electric_123_energy_consumption",
            "source": DOMAIN,
        },
        {"statistic_id": "duke_energy:electric_123_energy_cost", "source": DOMAIN},
        {"statistic_id": "sensor.unrelated", "source": "recorder"},
    ]

    with (
        patch(
            "custom_components.duke_energy.get_instance",
            return_value=recorder_instance,
        ),
        patch(
            "custom_components.duke_energy.list_statistic_ids",
            return_value=all_stats,
        ),
    ):
        await async_remove_entry(hass, mock_config_entry)

    recorder_instance.async_clear_statistics.assert_called_once()
    cleared_ids = set(recorder_instance.async_clear_statistics.call_args.args[0])
    assert cleared_ids == {
        "duke_energy:electric_123_energy_consumption",
        "duke_energy:electric_123_energy_cost",
    }
