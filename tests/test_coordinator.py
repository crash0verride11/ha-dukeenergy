"""Tests for the Duke Energy coordinator."""

import pytest

from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

from freezegun.api import FrozenDateTimeFactory

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.duke_energy.coordinator import DukeEnergyCoordinator


async def test_update(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_meters: AsyncMock,
    mock_recorder: Mock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Coordinator fetches meters and inserts statistics on first run, then incrementally."""
    mock_config_entry.add_to_hass(hass)

    coordinator = DukeEnergyCoordinator(hass, mock_api_with_meters, mock_config_entry)

    stat_id = "duke_energy:electric_123_energy_consumption"
    now_ts = dt_util.now().timestamp()

    # side_effect list: first call (first refresh) returns {} → full backfill path.
    # Second call (second refresh) returns populated dict → incremental path.
    last_stats_responses = [
        {},
        {stat_id: [{"start": now_ts}]},
    ]
    # statistics_during_period is only called on the incremental path; must include
    # both "sum" and "start" keys that the coordinator reads (coordinator.py:136-138).
    stats_during_response = {
        stat_id: [{"start": now_ts, "sum": 1.3}]
    }

    with (
        patch(
            "custom_components.duke_energy.coordinator.get_last_statistics",
            side_effect=last_stats_responses,
        ),
        patch(
            "custom_components.duke_energy.coordinator.statistics_during_period",
            return_value=stats_during_response,
        ),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert mock_api_with_meters.get_meters.call_count == 1
        # 3 years of electric data in 30-day windows: ceil(3*365/30) == 37
        assert mock_api_with_meters.get_energy_usage.call_count == 37

        # --- Phase 1: verify statistics were actually written ---
        assert mock_recorder.mock_add_stats.call_count == 1
        _, call_metadata, call_statistics = mock_recorder.mock_add_stats.call_args.args

        # StatisticMetaData is a TypedDict — access as dict keys
        assert call_metadata["statistic_id"] == "duke_energy:electric_123_energy_consumption"
        assert call_metadata["source"] == "duke_energy"
        assert call_metadata["name"] == "Duke Energy Electric 123 Consumption"
        assert call_metadata["unit_class"] == EnergyConverter.UNIT_CLASS
        assert call_metadata["unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR
        assert call_metadata["has_sum"] is True
        assert call_metadata["mean_type"] == StatisticMeanType.NONE

        # StatisticData is a TypedDict — access as dict keys
        assert len(call_statistics) == 1
        stat = call_statistics[0]
        expected_start = next(iter(mock_api_with_meters.get_energy_usage.return_value["data"]))
        assert stat["start"] == expected_start
        assert stat["state"] == pytest.approx(1.3)
        assert stat["sum"] == pytest.approx(1.3)  # 0.0 initial + 1.3 reading

        freezer.tick(timedelta(hours=12))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

        assert mock_api_with_meters.get_meters.call_count == 2
        # Stats exist now, so only one incremental call
        assert mock_api_with_meters.get_energy_usage.call_count == 38

        # --- Phase 2: incremental refresh writes empty stats (reading filtered as already seen) ---
        # The single mock reading's timestamp equals last_stats_time, so it is skipped.
        assert mock_recorder.mock_add_stats.call_count == 2
        _, call_metadata_2, call_statistics_2 = mock_recorder.mock_add_stats.call_args_list[1].args
        assert call_metadata_2["statistic_id"] == "duke_energy:electric_123_energy_consumption"
        assert call_metadata_2["source"] == "duke_energy"
        assert call_metadata_2["has_sum"] is True
        assert call_statistics_2 == []

        # Coordinator must not be in error state — confirms the second refresh
        # completed rather than silently dying on KeyError: 'sum'.
        assert coordinator.last_update_success is True


async def test_gas_meter_update(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api_with_gas_meter: AsyncMock,
    mock_recorder: Mock,
) -> None:
    """Coordinator handles gas meters: DAILY/WEEK API params, noon offset, CCF unit."""
    mock_config_entry.add_to_hass(hass)
    coordinator = DukeEnergyCoordinator(hass, mock_api_with_gas_meter, mock_config_entry)

    with patch(
        "custom_components.duke_energy.coordinator.get_last_statistics",
        return_value={},
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    # get_energy_usage is called with DAILY/WEEK for gas
    sample_call = mock_api_with_gas_meter.get_energy_usage.call_args
    assert sample_call.args[1] == "DAILY"
    assert sample_call.args[2] == "WEEK"

    # Metadata: volume units, gas statistic_id and name
    assert mock_recorder.mock_add_stats.call_count == 1
    _, metadata, statistics = mock_recorder.mock_add_stats.call_args.args
    assert metadata["statistic_id"] == "duke_energy:gas_456_energy_consumption"
    assert metadata["name"] == "Duke Energy Gas 456 Consumption"
    assert metadata["unit_class"] == "volume"
    assert metadata["unit_of_measurement"] == UnitOfVolume.CENTUM_CUBIC_FEET
    assert metadata["has_sum"] is True
    assert metadata["mean_type"] == StatisticMeanType.NONE

    # Daily readings are registered at noon (start + 12h), not at midnight
    assert len(statistics) == 1
    expected_start = next(iter(mock_api_with_gas_meter.get_energy_usage.return_value["data"]))
    assert statistics[0]["start"] == expected_start + timedelta(hours=12)
    assert statistics[0]["state"] == pytest.approx(2.5)
    assert statistics[0]["sum"] == pytest.approx(2.5)
