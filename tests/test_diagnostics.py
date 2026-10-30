"""Tests for Duke Energy config entry diagnostics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from homeassistant.core import HomeAssistant

from custom_components.duke_energy.diagnostics import (
    async_get_config_entry_diagnostics,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_diagnostics_redacts_identifying_material(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Tokens, serials and account identifiers never reach the dump."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            "cost_meters": {
                "serial-1": {"mode": "sensor", "entity_id": "sensor.private_rate"}
            },
            "backfill_cost": False,
        },
    )
    mock_config_entry.runtime_data = SimpleNamespace(
        status="complete",
        last_poll_time=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
        next_poll_time=None,
        account_info={"acct-1": "0001"},
        meter_info={"serial-1": SimpleNamespace(service_type="ELECTRIC")},
        meter_last_updated={"serial-1": datetime(2026, 10, 19, 11, 0, tzinfo=UTC)},
        statistic_ids={
            "duke_energy:electric_serial-1_energy_consumption",
            "duke_energy:account_private-account_temperature",
        },
    )

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    dump = json.dumps(result)

    assert "test-access-token" not in dump
    assert "test-refresh-token" not in dump
    assert "test-user-id" not in dump
    assert "test@example.com" not in dump
    assert "serial-1" not in dump
    assert "private-account" not in dump
    assert "sensor.private_rate" not in dump
    assert "acct-1" not in dump

    assert result["coordinator"]["status"] == "complete"
    assert result["coordinator"]["last_poll"] == "2026-10-19T12:00:00+00:00"
    assert result["coordinator"]["next_poll"] is None
    assert result["coordinator"]["accounts"] == 1
    assert result["coordinator"]["meters"]["meter_1"] == {
        "service_type": "ELECTRIC",
        "last_updated": "2026-10-19T11:00:00+00:00",
    }
    assert result["coordinator"]["statistics"] == {
        "consumption": 1,
        "cost": 0,
        "temperature": 1,
    }
    assert result["entry"]["options"]["cost_meters"]["meter_1"] == {
        "mode": "sensor",
        "entity_id": "**REDACTED**",
    }
    # Relabelling is for the dump only; the stored options keep real serials.
    assert "serial-1" in mock_config_entry.options["cost_meters"]


async def test_diagnostics_without_coordinator(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A setup that never produced a coordinator still dumps redacted config."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={"cost_meters": {"serial-2": {"mode": "static", "price": 0.15}}},
    )
    mock_config_entry.runtime_data = None

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert result["coordinator"] is None
    assert result["entry"]["data"]["token"] == "**REDACTED**"
    assert "serial-2" not in json.dumps(result)
    assert result["entry"]["options"]["cost_meters"] == {
        "meter_1": {"mode": "static", "price": 0.15}
    }
