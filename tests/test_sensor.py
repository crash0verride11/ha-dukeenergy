"""Tests for the Duke Energy sensors."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import DEFAULT, AsyncMock

from aiohttp import ClientError
from homeassistant.const import STATE_UNKNOWN, UnitOfEnergy, UnitOfVolume
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


def _eid(hass: HomeAssistant, unique_id: str) -> str:
    """Resolve a unique_id to its entity_id."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


def _meter_eid(
    hass: HomeAssistant, entry: MockConfigEntry, serial: str, key: str
) -> str:
    """Resolve a meter sensor's entity_id."""
    return _eid(hass, f"{entry.entry_id}_{serial}_{key}")


def _account_eid(
    hass: HomeAssistant, entry: MockConfigEntry, src_acct_id: str, key: str
) -> str:
    """Resolve an account sensor's entity_id."""
    return _eid(hass, f"{entry.entry_id}_account_{src_acct_id}_{key}")


async def test_devices_and_diagnostic_sensors(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """Account device owns Last updated; meter device owns Last changed."""
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)

    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    account_device = device_registry.async_get_device(
        identifiers={(DOMAIN, "account_src-1")}
    )
    meter_device = device_registry.async_get_device(identifiers={(DOMAIN, "123")})
    assert account_device is not None
    assert account_device.name == "Duke Energy Account acct-1"
    assert meter_device is not None
    assert meter_device.via_device_id == account_device.id

    poll_id = _account_eid(hass, mock_config_entry, "src-1", "last_duke_poll")
    change_id = _meter_eid(hass, mock_config_entry, "123", "last_meter_change")
    poll_entry = registry.async_get(poll_id)
    change_entry = registry.async_get(change_id)
    assert poll_entry.device_id == account_device.id
    assert poll_entry.entity_category is EntityCategory.DIAGNOSTIC
    assert change_entry.device_id == meter_device.id
    assert change_entry.entity_category is EntityCategory.DIAGNOSTIC

    # The first refresh polled and wrote statistics, so both have timestamps.
    assert dt_util.parse_datetime(hass.states.get(poll_id).state) is not None
    assert dt_util.parse_datetime(hass.states.get(change_id).state) is not None


async def test_summary_sensor_values(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """Usage sensors carry the meter totals; cost sensors the account bills."""
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)

    # The cycle start is the last completed cycle's endDate + 1 day, from the
    # MONTHLY usage graph.
    assert mock_api_with_meters.get_monthly_usage.await_count == 1
    assert mock_api_with_meters.get_monthly_usage.await_args.kwargs[
        "start_date"
    ] == datetime(2026, 6, 27)

    for key, expected in (
        ("usage_this_bill_cycle", "40.0"),
        ("usage_last_bill_cycle", "900.0"),
        ("usage_last_year", "1500.0"),
    ):
        state = hass.states.get(_meter_eid(hass, mock_config_entry, "123", key))
        assert state.state == expected
        assert state.attributes["unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR

    for key, expected in (
        ("cost_last_bill_cycle", "185.5"),
        ("cost_last_year", "310.0"),
    ):
        state = hass.states.get(_account_eid(hass, mock_config_entry, "src-1", key))
        assert state.state == expected
        assert state.attributes["device_class"] == "monetary"


async def test_gas_usage_sensor_units(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_gas_meter: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """Gas meters report bill-cycle usage in CCF with the gas device class."""
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)

    state = hass.states.get(
        _meter_eid(hass, mock_config_entry, "456", "usage_this_bill_cycle")
    )
    assert state.state == "40.0"
    assert state.attributes["unit_of_measurement"] == UnitOfVolume.CENTUM_CUBIC_FEET
    assert state.attributes["device_class"] == "gas"


async def test_billing_cycles_fetched_once_per_account(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """Two meters on one account share a single cycle lookup and start date."""
    electric = mock_api_with_meters.get_meters.return_value["123"]
    mock_api_with_meters.get_meters.return_value["456"] = {
        "serialNum": "456",
        "serviceType": "GAS",
        "agreementActiveDate": "2000-01-01",
        "account": electric["account"],
    }
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)

    monthly_calls = [
        call
        for call in mock_api_with_meters.get_energy_usage.await_args_list
        if call.args[1] == "MONTHLY"
    ]
    assert len(monthly_calls) == 1
    assert monthly_calls[0].args[0] == "123"  # the account's first meter
    assert monthly_calls[0].args[2] == "YEAR"
    assert mock_api_with_meters.get_monthly_usage.await_count == 2
    start_dates = {
        call.kwargs["start_date"]
        for call in mock_api_with_meters.get_monthly_usage.await_args_list
    }
    assert start_dates == {datetime(2026, 6, 27)}


async def test_billing_cycle_failure_falls_back_to_default_window(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """A failing cycle lookup still fetches the summary, with no start date."""

    def _fail_monthly(
        _serial: str, interval: str, *_args: object, **_kwargs: object
    ) -> object:
        if interval == "MONTHLY":
            raise ClientError
        return DEFAULT

    mock_api_with_meters.get_energy_usage.side_effect = _fail_monthly
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)

    assert (
        mock_api_with_meters.get_monthly_usage.await_args.kwargs["start_date"] is None
    )
    usage_id = _meter_eid(hass, mock_config_entry, "123", "usage_this_bill_cycle")
    assert hass.states.get(usage_id).state == "40.0"


async def test_summary_failure_keeps_poll_working(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    auto_enable_custom_integrations: None,
) -> None:
    """A failing summary endpoint leaves usage/cost unknown but stats still ingest."""
    mock_api_with_meters.get_monthly_usage.side_effect = ClientError
    mock_config_entry.add_to_hass(hass)
    await _setup_entry(hass, mock_config_entry)

    coordinator = mock_config_entry.runtime_data
    assert coordinator.last_update_success is True
    assert "duke_energy:electric_123_energy_consumption" in stats_store.data

    usage_id = _meter_eid(hass, mock_config_entry, "123", "usage_this_bill_cycle")
    cost_id = _account_eid(hass, mock_config_entry, "src-1", "cost_last_bill_cycle")
    assert hass.states.get(usage_id).state == STATE_UNKNOWN
    assert hass.states.get(cost_id).state == STATE_UNKNOWN


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

    poll_id = _account_eid(hass, mock_config_entry, "src-1", "last_duke_poll")
    change_id = _meter_eid(hass, mock_config_entry, "123", "last_meter_change")

    assert dt_util.parse_datetime(hass.states.get(poll_id).state) is not None
    assert hass.states.get(change_id).state == STATE_UNKNOWN


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

    poll_id = _account_eid(hass, mock_config_entry, "src-1", "last_duke_poll")
    change_id = _meter_eid(hass, mock_config_entry, "123", "last_meter_change")
    original_change = hass.states.get(change_id).state
    assert dt_util.parse_datetime(original_change) is not None

    # Reload: the new coordinator polls, but the single reading matches the
    # stored statistic and is filtered out, so no statistics are written and
    # only the restored value can populate last_meter_change.
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await _setup_entry(hass, mock_config_entry)

    assert hass.states.get(change_id).state == original_change

    # last_duke_poll is never restored — the reload's own poll repopulates it.
    assert dt_util.parse_datetime(hass.states.get(poll_id).state) is not None


async def test_diagnostic_sensors_stay_available_on_failed_poll(
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

    poll_id = _account_eid(hass, mock_config_entry, "src-1", "last_duke_poll")
    change_id = _meter_eid(hass, mock_config_entry, "123", "last_meter_change")

    coordinator = mock_config_entry.runtime_data
    mock_api_with_meters.get_meters.side_effect = TimeoutError
    coordinator._last_successful_date = None  # force a real poll
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    for entity_id in (poll_id, change_id):
        assert hass.states.get(entity_id).state != "unavailable"


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
    poll_id = _account_eid(hass, mock_config_entry, "src-1", "last_duke_poll")

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(poll_id).state == "unavailable"
