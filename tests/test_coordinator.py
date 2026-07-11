"""Tests for the Duke Energy coordinator."""

import pytest

from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import EnergyConverter

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.duke_energy.coordinator import DukeEnergyCoordinator

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
    # 3 years of electric data in 30-day windows: ceil(3*365/30) == 37
    assert mock_api_with_meters.get_energy_usage.call_count == 37

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
