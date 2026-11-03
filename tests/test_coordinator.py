"""Tests for the Duke Energy coordinator."""

import pytest

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory

from aiodukeenergy_co import DukeEnergyAuthError, DukeEnergyBlockedError
from aiohttp import ClientConnectorError
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util.unit_conversion import EnergyConverter

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.duke_energy.const import (
    DOMAIN,
    STATUS_FAILED,
    STATUS_FETCHING,
)
from custom_components.duke_energy.coordinator import DukeEnergyCoordinator, MeterInfo

from .conftest import StatsStore


def _call_for(store: StatsStore, statistic_id: str) -> tuple[dict, list]:
    """Return (metadata, statistics) of the most recent insert for a statistic_id."""
    for call in reversed(store.mock_add.call_args_list):
        _, metadata, statistics = call.args
        if metadata["statistic_id"] == statistic_id:
            return metadata, statistics
    msg = f"no external statistics inserted for {statistic_id}"
    raise AssertionError(msg)


def _set_cost_entities(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mapping: dict,
    *,
    backfill: bool = False,
) -> None:
    """Configure the given meters in sensor mode (serial -> price entity)."""
    cost_meters = {
        serial: {"mode": "sensor", "entity_id": entity_id}
        for serial, entity_id in mapping.items()
    }
    hass.config_entries.async_update_entry(
        entry,
        options={"cost_meters": cost_meters, "backfill_cost": backfill},
    )


def _set_cost_meters(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    cost_meters: dict,
    *,
    backfill: bool = False,
) -> None:
    """Set the structured per-meter cost configuration on the entry."""
    hass.config_entries.async_update_entry(
        entry,
        options={"cost_meters": cost_meters, "backfill_cost": backfill},
    )


async def test_update(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Coordinator inserts consumption + temperature stats, then refreshes incrementally."""
    mock_config_entry.add_to_hass(hass)
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    consumption_id = "duke_energy:electric_123_energy_consumption"

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert mock_api_with_meters.get_meters.call_count == 1
    # 3 years of electric data in 30-day windows (ceil(3*365/30) == 37),
    # plus one MONTHLY billing-cycle lookup.
    assert mock_api_with_meters.get_energy_usage.call_count == 38

    metadata, statistics = _call_for(stats_store, consumption_id)
    assert metadata["source"] == "duke_energy"
    assert metadata["name"] == "Duke Energy Electric 123 Consumption"
    assert metadata["unit_class"] == EnergyConverter.UNIT_CLASS
    assert metadata["unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR
    assert metadata["has_sum"] is True
    assert metadata["mean_type"] == StatisticMeanType.NONE

    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    assert len(statistics) == 1
    assert statistics[0]["start"] == reading_ts
    assert statistics[0]["state"] == pytest.approx(1.3)
    assert statistics[0]["sum"] == pytest.approx(1.3)

    # A temperature stat is registered per account.
    assert "duke_energy:account_src-1_temperature" in stats_store.data

    # --- Incremental refresh on a later day: the one reading equals the last
    # stored start, so it is filtered out and the consumption insert is empty.
    # Tick 48h so we deterministically cross both the next scheduled slot (at
    # most ~26h out given the ±2h offset) and into a new ET day — otherwise the
    # scheduled poll could land on the same day and short-circuit on the
    # "already retrieved today" guard before get_meters is called. ---
    freezer.tick(timedelta(hours=48))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert coordinator.last_update_success is True
    assert mock_api_with_meters.get_meters.call_count == 2
    _, incremental_stats = _call_for(stats_store, consumption_id)
    assert incremental_stats == []


async def test_blocked_request_fails_poll_without_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    auto_enable_custom_integrations: None,
) -> None:
    """An edge/WAF refusal is a failed poll, not an auth failure: no reauth."""
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.LOADED)
    mock_api_with_meters.get_meters.side_effect = DukeEnergyBlockedError("403")
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.status == STATUS_FAILED
    assert not hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, match_context={"source": SOURCE_REAUTH}
    )


async def test_auth_error_starts_reauth(
    recorder_mock: object,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    auto_enable_custom_integrations: None,
) -> None:
    """A rejected token is still an auth failure and starts the reauth flow."""
    mock_config_entry.add_to_hass(hass)
    mock_config_entry.mock_state(hass, ConfigEntryState.LOADED)
    mock_api_with_meters.get_meters.side_effect = DukeEnergyAuthError("401")
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert coordinator.status == STATUS_FAILED
    assert hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, match_context={"source": SOURCE_REAUTH}
    )


async def test_discover_meters_registers_supported_meters(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
) -> None:
    """Discovery registers supported meters and accounts, skipping the rest."""
    mock_config_entry.add_to_hass(hass)
    mock_api_with_meters.get_meters.return_value["999"] = {
        "serialNum": "999",
        "serviceType": "WATER",
        "account": {"accountNumber": "acct-2", "srcAcctId": "src-2"},
    }
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)

    await coordinator.async_discover_meters()

    assert coordinator.meter_info == {"123": MeterInfo("ELECTRIC", "src-1")}
    assert coordinator.account_info == {"src-1": "acct-1"}
    # Discovery only enumerates meters; the backfill is left to the refresh.
    mock_api_with_meters.get_energy_usage.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ClientConnectorError(Mock(), OSError("dns")),
        DukeEnergyBlockedError("403"),
    ],
    ids=["timeout", "network", "blocked"],
)
async def test_discover_meters_outage_not_ready(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    error: Exception,
) -> None:
    """An outage or CDN block during discovery retries setup."""
    mock_config_entry.add_to_hass(hass)
    mock_api_with_meters.get_meters.side_effect = error
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)

    with pytest.raises(ConfigEntryNotReady):
        await coordinator.async_discover_meters()


async def test_discover_meters_auth_error_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
) -> None:
    """A rejected token during discovery is an auth failure, not a retry."""
    mock_config_entry.add_to_hass(hass)
    mock_api_with_meters.get_meters.side_effect = DukeEnergyAuthError("401")
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_discover_meters()


async def test_refresh_skipped_while_fetching(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
) -> None:
    """A refresh requested mid-backfill does not start a second fetch."""
    mock_config_entry.add_to_hass(hass)
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    coordinator.status = STATUS_FETCHING

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    mock_api_with_meters.get_meters.assert_not_called()
    assert coordinator.status == STATUS_FETCHING
    # The running refresh owns rescheduling; the skipped one leaves it alone.
    assert coordinator.next_poll_time is None


async def test_gas_meter_update(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_gas_meter: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """Coordinator handles gas meters: DAILY/WEEK API params, noon offset, CCF unit."""
    mock_config_entry.add_to_hass(hass)
    coordinator = DukeEnergyCoordinator(
        hass, mock_api_with_gas_meter, mock_config_entry
    )

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    sample_call = mock_api_with_gas_meter.get_energy_usage.call_args
    assert sample_call.args[1] == "DAILY"
    assert sample_call.args[2] == "WEEK"

    metadata, statistics = _call_for(
        stats_store, "duke_energy:gas_456_energy_consumption"
    )
    assert metadata["name"] == "Duke Energy Gas 456 Consumption"
    assert metadata["unit_class"] == "volume"
    assert metadata["unit_of_measurement"] == UnitOfVolume.CENTUM_CUBIC_FEET
    assert metadata["has_sum"] is True
    assert metadata["mean_type"] == StatisticMeanType.NONE

    # Daily readings are registered at noon (start + 12h), not at midnight.
    reading_ts = next(
        iter(mock_api_with_gas_meter.get_energy_usage.return_value["data"])
    )
    assert len(statistics) == 1
    assert statistics[0]["start"] == reading_ts + timedelta(hours=12)
    assert statistics[0]["state"] == pytest.approx(2.5)
    assert statistics[0]["sum"] == pytest.approx(2.5)


async def test_cost_incremental_electric(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A configured price sensor with LTS yields energy x price cost stats."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"})
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    stats_store.seed("sensor.price", [{"start": reading_ts, "mean": 0.10}])

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    metadata, statistics = _call_for(
        stats_store, "duke_energy:electric_123_energy_cost"
    )
    assert metadata["name"] == "Duke Energy Electric 123 Cost"
    assert metadata["has_sum"] is True
    assert metadata["mean_type"] == StatisticMeanType.NONE
    assert metadata["unit_of_measurement"] == (hass.config.currency or "USD")

    assert len(statistics) == 1
    assert statistics[0]["start"] == reading_ts
    assert statistics[0]["state"] == pytest.approx(0.13)  # 1.3 kWh x $0.10
    assert statistics[0]["sum"] == pytest.approx(0.13)


async def test_cost_incremental_continues_sum(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """Cost resumes its running sum from the existing cost stat baseline."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"})
    base = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    t1 = base - timedelta(hours=1)
    t2 = base
    mock_api_with_meters.get_energy_usage.return_value = {
        "data": {
            t1: {"energy": 1.0, "temperature": 70},
            t2: {"energy": 1.0, "temperature": 70},
        },
        "missing": [],
    }
    stats_store.seed(
        "sensor.price",
        [{"start": t1, "mean": 0.10}, {"start": t2, "mean": 0.10}],
    )
    # t1 was already priced in a prior run (sum 0.10); t2 is new.
    stats_store.seed(
        "duke_energy:electric_123_energy_cost",
        [{"start": t1, "state": 0.10, "sum": 0.10}],
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    # t1 kept its baseline; t2 accumulates onto it (0.10 -> 0.20).
    assert [r["sum"] for r in cost_rows] == pytest.approx([0.10, 0.20])


async def test_cost_unpriced_interval_omitted(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """An interval before the sensor's first known price gets no cost (head skip).

    Prices never reach backward-in-time: carry-forward only heals gaps *after*
    a known price, so an interval preceding all price history is omitted.
    """
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"})
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    # Price exists in the queried window but only AFTER the reading's hour.
    stats_store.seed(
        "sensor.price", [{"start": reading_ts + timedelta(hours=2), "mean": 0.10}]
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    _, statistics = _call_for(stats_store, "duke_energy:electric_123_energy_cost")
    assert statistics == []


async def test_cost_gap_carried_forward(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """An interval with no price of its own uses the most recent prior price.

    Covers the price sensor going quiet (e.g. HA downtime) while Duke kept
    metering: rates change irregularly, so the last known price still applies.
    """
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"})
    base = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    t1 = base - timedelta(hours=1)
    t2 = base
    mock_api_with_meters.get_energy_usage.return_value = {
        "data": {
            t1: {"energy": 1.0, "temperature": 70},
            t2: {"energy": 2.0, "temperature": 70},
        },
        "missing": [],
    }
    # Price recorded only at t1; t2 must carry the $0.10 forward.
    stats_store.seed("sensor.price", [{"start": t1, "mean": 0.10}])

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    _, statistics = _call_for(stats_store, "duke_energy:electric_123_energy_cost")
    assert [s["state"] for s in statistics] == pytest.approx([0.10, 0.20])
    assert [s["sum"] for s in statistics] == pytest.approx([0.10, 0.30])


async def test_cost_current_state_fallback(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """With no LTS, cost is priced at the sensor's current numeric state."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"})
    hass.states.async_set("sensor.price", "0.20")
    # No LTS seeded for sensor.price -> fallback to current state.

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    _, statistics = _call_for(stats_store, "duke_energy:electric_123_energy_cost")
    assert len(statistics) == 1
    assert statistics[0]["state"] == pytest.approx(0.26)  # 1.3 kWh x $0.20


async def test_cost_gas_daily(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_gas_meter: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """Gas cost prices the daily reading by its Eastern calendar day."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"456": "sensor.gas_price"})

    # Fix the reading to an Eastern-midnight day so day bucketing is unambiguous.
    et = ZoneInfo("America/New_York")
    reading = datetime(2024, 1, 15, 0, 0, tzinfo=et)
    mock_api_with_gas_meter.get_energy_usage.return_value = {
        "data": {reading: {"energy": 2.5, "temperature": 68}},
        "missing": [],
    }
    # Hourly price mean on the same ET day → averaged to that date.
    stats_store.seed(
        "sensor.gas_price", [{"start": reading + timedelta(hours=12), "mean": 0.50}]
    )

    coordinator = DukeEnergyCoordinator(
        hass, mock_api_with_gas_meter, mock_config_entry
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    _, statistics = _call_for(stats_store, "duke_energy:gas_456_energy_cost")
    assert len(statistics) == 1
    # Cost is registered at noon (matching daily consumption), 2.5 CCF × $0.50.
    assert statistics[0]["start"] == reading + timedelta(hours=12)
    assert statistics[0]["state"] == pytest.approx(1.25)
    assert statistics[0]["sum"] == pytest.approx(1.25)


async def test_cost_backfill_electric(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """Backfill reprices the full consumption history and resets the flag."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"}, backfill=True)

    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    t0 = reading_ts - timedelta(hours=3)
    t1 = reading_ts - timedelta(hours=2)
    # Pre-existing consumption history (as if written by prior runs).
    stats_store.seed(
        "duke_energy:electric_123_energy_consumption",
        [
            {"start": t0, "state": 1.0, "sum": 1.0},
            {"start": t1, "state": 1.0, "sum": 2.0},
            {"start": reading_ts, "state": 1.3, "sum": 3.3},
        ],
    )
    # Price only from t1 onward -> t0 must use the earliest available price.
    stats_store.seed(
        "sensor.price",
        [
            {"start": t1, "mean": 0.10},
            {"start": reading_ts, "mean": 0.10},
        ],
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    # Full history repriced: t0 via earliest price, cumulative sum from zero.
    assert [r["state"] for r in cost_rows] == pytest.approx([0.10, 0.10, 0.13])
    assert [r["sum"] for r in cost_rows] == pytest.approx([0.10, 0.20, 0.33])

    # One-shot: the flag is cleared after the backfill runs.
    assert mock_config_entry.options["backfill_cost"] is False


async def test_cost_backfill_mid_gap_uses_previous_price(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A gap inside the price history carries the previous price forward.

    Guards against pricing a gap at the *earliest* price: after a rate change
    (0.10 -> 0.30), an unpriced hour must cost 0.30 (most recent prior), while
    earliest_price remains reserved for pre-history (head) intervals only.
    """
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"}, backfill=True)

    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    t0 = reading_ts - timedelta(hours=2)
    t1 = reading_ts - timedelta(hours=1)
    stats_store.seed(
        "duke_energy:electric_123_energy_consumption",
        [
            {"start": t0, "state": 1.0, "sum": 1.0},
            {"start": t1, "state": 1.0, "sum": 2.0},
            {"start": reading_ts, "state": 1.0, "sum": 3.0},
        ],
    )
    # Rate change at t1, then the sensor goes quiet for reading_ts.
    stats_store.seed(
        "sensor.price",
        [
            {"start": t0, "mean": 0.10},
            {"start": t1, "mean": 0.30},
        ],
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    # reading_ts priced at 0.30 (carried forward), NOT 0.10 (earliest).
    assert [r["state"] for r in cost_rows] == pytest.approx([0.10, 0.30, 0.30])
    assert [r["sum"] for r in cost_rows] == pytest.approx([0.10, 0.40, 0.70])


async def test_cost_static_incremental(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A static mode meter prices every interval at its fixed rate."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_meters(
        hass, mock_config_entry, {"123": {"mode": "static", "price": 0.15}}
    )
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    _, statistics = _call_for(stats_store, "duke_energy:electric_123_energy_cost")
    assert len(statistics) == 1
    assert statistics[0]["start"] == reading_ts
    assert statistics[0]["state"] == pytest.approx(0.195)  # 1.3 kWh x $0.15
    assert statistics[0]["sum"] == pytest.approx(0.195)


async def test_cost_static_backfill(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """Static backfill reprices the full history flat, contiguous from zero."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_meters(
        hass,
        mock_config_entry,
        {"123": {"mode": "static", "price": 0.20}},
        backfill=True,
    )
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    t0 = reading_ts - timedelta(hours=2)
    t1 = reading_ts - timedelta(hours=1)
    stats_store.seed(
        "duke_energy:electric_123_energy_consumption",
        [
            {"start": t0, "state": 1.0, "sum": 1.0},
            {"start": t1, "state": 2.0, "sum": 3.0},
            {"start": reading_ts, "state": 1.5, "sum": 4.5},
        ],
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    # Every interval priced at the flat 0.20, cumulative sum from zero.
    assert [r["state"] for r in cost_rows] == pytest.approx([0.20, 0.40, 0.30])
    assert [r["sum"] for r in cost_rows] == pytest.approx([0.20, 0.60, 0.90])
    assert mock_config_entry.options["backfill_cost"] is False


async def test_cost_mode_off_skips(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A meter explicitly set to off records no cost statistic."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_meters(hass, mock_config_entry, {"123": {"mode": "off"}})

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert "duke_energy:electric_123_energy_cost" not in stats_store.data


async def test_cost_first_enable_limited_to_window(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A meter's first costs reach back 30 days, not over all stored usage."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_meters(
        hass, mock_config_entry, {"123": {"mode": "static", "price": 0.10}}
    )
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    old = reading_ts - timedelta(days=60)
    recent = reading_ts - timedelta(days=10)
    stats_store.seed(
        "duke_energy:electric_123_energy_consumption",
        [
            {"start": old, "state": 5.0, "sum": 5.0},
            {"start": recent, "state": 1.0, "sum": 6.0},
        ],
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    # The 60-day-old row is left for an explicit backfill.
    assert [r["start"] for r in cost_rows] == [
        recent.timestamp(),
        reading_ts.timestamp(),
    ]
    assert [r["sum"] for r in cost_rows] == pytest.approx([0.10, 0.23])


async def test_cost_catches_up_beyond_fetch_window(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """After an outage, usage past Duke's 30-day fetch window is still priced.

    Without a re-enable from the options flow, a gap after the last cost row
    is caught up in full, not cut back to the current bill cycle.
    """
    mock_config_entry.add_to_hass(hass)
    _set_cost_meters(
        hass, mock_config_entry, {"123": {"mode": "static", "price": 0.10}}
    )
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    last_cost = reading_ts - timedelta(days=90)
    unpriced = reading_ts - timedelta(days=60)
    stats_store.seed(
        "duke_energy:electric_123_energy_consumption",
        [
            {"start": last_cost, "state": 1.0, "sum": 1.0},
            {"start": unpriced, "state": 2.0, "sum": 3.0},
        ],
    )
    stats_store.seed(
        "duke_energy:electric_123_energy_cost",
        [{"start": last_cost, "state": 0.10, "sum": 0.10}],
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    assert [r["start"] for r in cost_rows] == [
        last_cost.timestamp(),
        unpriced.timestamp(),
        reading_ts.timestamp(),
    ]
    assert [r["sum"] for r in cost_rows] == pytest.approx([0.10, 0.30, 0.43])


async def test_cost_priced_on_poll_that_skips_duke(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """Usage waiting on a price is priced by a later same-day poll."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"})
    # No price yet: the first poll records consumption but no cost.
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert "duke_energy:electric_123_energy_cost" not in stats_store.data

    hass.states.async_set("sensor.price", "0.20")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    # "Already retrieved today": no second Duke request.
    assert mock_api_with_meters.get_meters.call_count == 1
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    assert [r["state"] for r in cost_rows] == pytest.approx([0.26])


async def test_cost_rebuild_without_price_raises(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """An explicit rebuild with nothing to price with fails loudly."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"})
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    with pytest.raises(ValueError, match="sensor.price"):
        await coordinator.async_rebuild_costs()


async def test_cost_rebuild_reprices_stored_history(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A rebuild replaces existing costs from stored consumption, no Duke call."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_meters(
        hass, mock_config_entry, {"123": {"mode": "static", "price": 0.10}}
    )
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    calls = mock_api_with_meters.get_energy_usage.call_count

    _set_cost_meters(
        hass, mock_config_entry, {"123": {"mode": "static", "price": 0.30}}
    )
    await coordinator.async_rebuild_costs()

    assert mock_api_with_meters.get_energy_usage.call_count == calls
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    assert [r["sum"] for r in cost_rows] == pytest.approx([0.39])  # 1.3 x 0.30


async def test_cost_legacy_backfill_without_price_clears_flag(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A saved backfill that cannot be priced is dropped, not retried forever."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"}, backfill=True)

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert mock_config_entry.options["backfill_cost"] is False


def _set_cycle_start(api: AsyncMock, cycle_start: date) -> None:
    """Make Duke's billing-cycle graph report the current cycle's start."""
    cycles = [
        {
            "endDate": (cycle_start - timedelta(days=1)).isoformat(),
            "startDate": (cycle_start - timedelta(days=30)).isoformat(),
        }
    ]
    default = api.get_energy_usage.side_effect

    def _dispatch(serial: str, interval: str, *args: object, **kwargs: object):
        if interval == "MONTHLY":
            return {"data": cycles, "missing": []}
        return default(serial, interval, *args, **kwargs)

    api.get_energy_usage.side_effect = _dispatch


def _cycle_rows(reading_ts: datetime) -> tuple[date, datetime, datetime]:
    """Return a cycle start 11 days back plus a row before and inside it."""
    et = ZoneInfo("America/New_York")
    cycle_start = reading_ts.astimezone(et).date() - timedelta(days=10)
    midnight = datetime.combine(cycle_start, datetime.min.time(), tzinfo=et)
    return cycle_start, midnight - timedelta(days=2), midnight + timedelta(days=1)


async def test_cost_first_enable_starts_at_bill_cycle(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A meter's first costs start at the current bill cycle, inside 30 days."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_meters(
        hass, mock_config_entry, {"123": {"mode": "static", "price": 0.10}}
    )
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    cycle_start, before, inside = _cycle_rows(reading_ts)
    _set_cycle_start(mock_api_with_meters, cycle_start)
    stats_store.seed(
        "duke_energy:electric_123_energy_consumption",
        [
            {"start": before, "state": 5.0, "sum": 5.0},
            {"start": inside, "state": 1.0, "sum": 6.0},
        ],
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    # The previous cycle's row is left for an explicit backfill.
    assert [r["start"] for r in cost_rows] == [
        inside.timestamp(),
        reading_ts.timestamp(),
    ]
    assert [r["sum"] for r in cost_rows] == pytest.approx([0.10, 0.23])


async def test_cost_reenabled_flat_rate_starts_at_bill_cycle(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A static meter turned back on skips the gap; its sum carries on."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            "cost_meters": {"123": {"mode": "static", "price": 0.10}},
            "cost_reenabled": ["123"],
        },
    )
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    cycle_start, before, inside = _cycle_rows(reading_ts)
    _set_cycle_start(mock_api_with_meters, cycle_start)
    last_cost = reading_ts - timedelta(days=90)
    stats_store.seed(
        "duke_energy:electric_123_energy_consumption",
        [
            {"start": last_cost, "state": 1.0, "sum": 1.0},
            {"start": before, "state": 5.0, "sum": 6.0},
            {"start": inside, "state": 1.0, "sum": 7.0},
        ],
    )
    stats_store.seed(
        "duke_energy:electric_123_energy_cost",
        [{"start": last_cost, "state": 0.10, "sum": 0.10}],
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    assert [r["start"] for r in cost_rows] == [
        last_cost.timestamp(),
        inside.timestamp(),
        reading_ts.timestamp(),
    ]
    assert [r["sum"] for r in cost_rows] == pytest.approx([0.10, 0.20, 0.33])
    # Settled: later polls carry on from the new rows.
    assert mock_config_entry.options["cost_reenabled"] == []


async def test_cost_reenabled_price_history_fills_gap(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A sensor meter turned back on prices the whole gap from its history."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            "cost_meters": {"123": {"mode": "sensor", "entity_id": "sensor.price"}},
            "cost_reenabled": ["123"],
        },
    )
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    cycle_start, before, _ = _cycle_rows(reading_ts)
    _set_cycle_start(mock_api_with_meters, cycle_start)
    last_cost = reading_ts - timedelta(days=90)
    gap = reading_ts - timedelta(days=60)
    stats_store.seed(
        "duke_energy:electric_123_energy_consumption",
        [
            {"start": last_cost, "state": 1.0, "sum": 1.0},
            {"start": gap, "state": 2.0, "sum": 3.0},
            {"start": before, "state": 1.0, "sum": 4.0},
        ],
    )
    stats_store.seed(
        "duke_energy:electric_123_energy_cost",
        [{"start": last_cost, "state": 0.10, "sum": 0.10}],
    )
    stats_store.seed(
        "sensor.price",
        [{"start": gap, "mean": 0.10}, {"start": before, "mean": 0.20}],
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    # Gap and previous-cycle rows priced from history; no day limit.
    assert [r["state"] for r in cost_rows] == pytest.approx([0.10, 0.20, 0.20, 0.26])
    assert mock_config_entry.options["cost_reenabled"] == []


async def test_cost_first_enable_price_history_fills_back(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """A new sensor meter is priced as far back as its price history reaches."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "sensor.price"})
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    cycle_start, _, _ = _cycle_rows(reading_ts)
    _set_cycle_start(mock_api_with_meters, cycle_start)
    unpriced = reading_ts - timedelta(days=300)
    first_price = reading_ts - timedelta(days=200)
    stats_store.seed(
        "duke_energy:electric_123_energy_consumption",
        [
            {"start": unpriced, "state": 4.0, "sum": 4.0},
            {"start": first_price, "state": 2.0, "sum": 6.0},
        ],
    )
    stats_store.seed("sensor.price", [{"start": first_price, "mean": 0.10}])

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    # Not limited by the bill cycle or 30 days; usage before the first known
    # price waits for a backfill.
    assert [r["start"] for r in cost_rows] == [
        first_price.timestamp(),
        reading_ts.timestamp(),
    ]
    assert [r["sum"] for r in cost_rows] == pytest.approx([0.20, 0.33])


async def test_cost_first_enable_without_price_history_starts_at_bill_cycle(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    stats_store: StatsStore,
) -> None:
    """An entity with only a current value prices from the bill cycle on."""
    mock_config_entry.add_to_hass(hass)
    _set_cost_entities(hass, mock_config_entry, {"123": "input_number.price"})
    hass.states.async_set("input_number.price", "0.10")
    reading_ts = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
    cycle_start, before, inside = _cycle_rows(reading_ts)
    _set_cycle_start(mock_api_with_meters, cycle_start)
    stats_store.seed(
        "duke_energy:electric_123_energy_consumption",
        [
            {"start": reading_ts - timedelta(days=300), "state": 4.0, "sum": 4.0},
            {"start": before, "state": 5.0, "sum": 9.0},
            {"start": inside, "state": 1.0, "sum": 10.0},
        ],
    )

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    cost_rows = stats_store.data["duke_energy:electric_123_energy_cost"]
    assert [r["start"] for r in cost_rows] == [
        inside.timestamp(),
        reading_ts.timestamp(),
    ]
