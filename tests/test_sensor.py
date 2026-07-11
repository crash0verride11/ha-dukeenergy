"""Tests for the Duke Energy diagnostic sensors."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.duke_energy.const import DOMAIN

from .conftest import StatsStore


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up through the config entries manager."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_ids(
    hass: HomeAssistant, entry: MockConfigEntry, serial: str
) -> tuple[str, str]:
    """Return (last_duke_poll, last_meter_change) entity ids for a meter."""
    registry = er.async_get(hass)
    poll = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{serial}_last_duke_poll"
    )
    change = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{serial}_last_meter_change"
    )
    assert poll is not None
    assert change is not None
    return poll, change


async def test_sensors_created_with_values(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """Both diagnostic sensors exist per meter and hold timestamps after setup."""
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)

    poll_id, change_id = _entity_ids(hass, mock_config_entry, "123")

    # Both are diagnostic entities on the meter's device.
    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, "123")})
    assert device is not None
    assert device.manufacturer == "Duke Energy"
    for entity_id in (poll_id, change_id):
        reg_entry = registry.async_get(entity_id)
        assert reg_entry is not None
        assert reg_entry.entity_category is EntityCategory.DIAGNOSTIC
        assert reg_entry.device_id == device.id

    # The first refresh polled and wrote statistics, so both have timestamps.
    poll_state = hass.states.get(poll_id)
    change_state = hass.states.get(change_id)
    assert poll_state is not None
    assert change_state is not None
    assert dt_util.parse_datetime(poll_state.state) is not None
    assert dt_util.parse_datetime(change_state.state) is not None


async def test_last_meter_change_unknown_without_new_data(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """A poll that adds no statistics sets last_duke_poll but not last_meter_change."""
    mock_api_with_meters.get_energy_usage.return_value = {"data": {}, "missing": []}
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)

    poll_id, change_id = _entity_ids(hass, mock_config_entry, "123")

    poll_state = hass.states.get(poll_id)
    assert poll_state is not None
    assert dt_util.parse_datetime(poll_state.state) is not None

    change_state = hass.states.get(change_id)
    assert change_state is not None
    assert change_state.state == STATE_UNKNOWN


async def test_last_meter_change_restored_across_reload(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """last_meter_change survives a reload whose poll adds no new statistics."""
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)

    poll_id, change_id = _entity_ids(hass, mock_config_entry, "123")
    original_change = hass.states.get(change_id).state
    assert dt_util.parse_datetime(original_change) is not None

    # Reload: the new coordinator polls, but the single reading matches the
    # stored statistic and is filtered out, so no statistics are written and
    # only the restored value can populate last_meter_change.
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await _setup_entry(hass, mock_config_entry)

    change_state = hass.states.get(change_id)
    assert change_state is not None
    assert change_state.state == original_change

    # last_duke_poll is never restored — the reload's own poll repopulates it.
    poll_state = hass.states.get(poll_id)
    assert poll_state is not None
    assert dt_util.parse_datetime(poll_state.state) is not None


async def test_sensors_stay_available_on_failed_poll(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """A failed refresh must not flip the diagnostic sensors to unavailable."""
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)
    poll_id, change_id = _entity_ids(hass, mock_config_entry, "123")

    coordinator = mock_config_entry.runtime_data
    mock_api_with_meters.get_meters.side_effect = TimeoutError
    coordinator._last_successful_date = None  # force a real poll
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    for entity_id in (poll_id, change_id):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state != "unavailable"


async def test_unload_entry(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """Unloading the entry unloads the sensor platform cleanly."""
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)
    poll_id, _change_id = _entity_ids(hass, mock_config_entry, "123")

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(poll_id)
    assert state is not None
    assert state.state == "unavailable"
