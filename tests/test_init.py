"""Tests for Duke Energy async_setup_entry and async_unload_entry."""

from __future__ import annotations

import pytest

from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from custom_components.duke_energy import async_migrate_entry, async_setup_entry
from custom_components.duke_energy.const import DOMAIN
from custom_components.duke_energy.coordinator import DukeEnergyCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_setup_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder: object,
    auto_enable_custom_integrations: None,
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
        mock_client_cls.return_value.get_meters = AsyncMock(return_value={})
        # Forwarding the sensor platform requires the setup lock, which the
        # config entries manager holds during a real setup.
        async with mock_config_entry.setup_lock:
            result = await async_setup_entry(hass, mock_config_entry)
        await hass.async_block_till_done()

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
        version=3,
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
        "sensor.duke_electric_123_energy_consumption",
    }

    await mock_config_entry._async_process_on_unload(hass)

    mock_recorder.async_clear_statistics.assert_not_called()


def _mock_recorder_instance() -> Mock:
    """Return a recorder Mock whose executor job runs synchronously."""
    recorder_instance = Mock()

    async def _passthrough(func, *args, **kwargs):
        return func(*args, **kwargs)

    recorder_instance.async_add_executor_job = AsyncMock(side_effect=_passthrough)
    recorder_instance.async_clear_statistics = Mock()
    return recorder_instance


async def test_remove_entry_clears_statistics(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """async_remove_entry clears the entry's entity statistics and legacy externals.

    The registry rows include a cost carrier with no live entity (cost mode
    disabled after stats were written) — its orphaned statistics must clear
    too. Unrelated recorder statistics stay.
    """
    from custom_components.duke_energy import async_remove_entry

    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    carrier = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "duke_electric_123_energy_consumption",
        suggested_object_id="duke_electric_123_energy_consumption",
        config_entry=mock_config_entry,
    )
    orphaned_cost = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "duke_electric_123_total_cost",
        suggested_object_id="duke_electric_123_total_cost",
        config_entry=mock_config_entry,
    )

    recorder_instance = _mock_recorder_instance()
    all_stats = [
        {"statistic_id": "duke_energy:gas_456_energy_consumption", "source": DOMAIN},
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
        carrier.entity_id,
        orphaned_cost.entity_id,
        "duke_energy:gas_456_energy_consumption",
    }


async def test_migrate_entry_v2_to_v3_clears_external_statistics(
    hass: HomeAssistant,
) -> None:
    """v2 → v3 clears the external duke_energy:* statistics and bumps the version."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, minor_version=1, data={"x": 1})
    entry.add_to_hass(hass)

    recorder_instance = _mock_recorder_instance()
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
        assert await async_migrate_entry(hass, entry)

    assert entry.version == 3
    # v2 data (the OAuth token) is preserved — only v1 entries lose data.
    assert entry.data == {"x": 1}
    cleared_ids = set(recorder_instance.async_clear_statistics.call_args.args[0])
    assert cleared_ids == {
        "duke_energy:electric_123_energy_consumption",
        "duke_energy:electric_123_energy_cost",
    }


async def test_migrate_entry_v1_to_v3(
    hass: HomeAssistant,
) -> None:
    """v1 chains through v2 (data reset for reauth) to v3 in one pass."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=1, data={"old": "auth"}
    )
    entry.add_to_hass(hass)

    recorder_instance = _mock_recorder_instance()
    with (
        patch(
            "custom_components.duke_energy.get_instance",
            return_value=recorder_instance,
        ),
        patch(
            "custom_components.duke_energy.list_statistic_ids",
            return_value=[],
        ),
    ):
        assert await async_migrate_entry(hass, entry)

    assert entry.version == 3
    assert entry.data == {}
    # Nothing to clear: no duke_energy-sourced statistics existed.
    recorder_instance.async_clear_statistics.assert_not_called()


async def test_setup_entry_unloads_platforms_on_failed_first_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_recorder: Mock,
    auto_enable_custom_integrations: None,
) -> None:
    """A failed first refresh unloads the forwarded platforms before re-raising.

    HA does not unload forwarded platforms when setup fails, so a retry
    would double-forward without this cleanup.
    """
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    with (
        patch("custom_components.duke_energy.DukeEnergyAuth", autospec=True),
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
            side_effect=ConfigEntryNotReady,
        ),
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_unload,
    ):
        mock_client_cls.return_value.get_meters = AsyncMock(return_value={})
        async with mock_config_entry.setup_lock:
            with pytest.raises(ConfigEntryNotReady):
                await async_setup_entry(hass, mock_config_entry)
        await hass.async_block_till_done()

    mock_unload.assert_awaited_once()
