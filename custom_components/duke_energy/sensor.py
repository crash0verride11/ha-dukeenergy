"""
Diagnostic sensors for the Duke Energy integration.

Two timestamp sensors per meter device:

- ``last_duke_poll`` ("Last updated"): when Duke Energy was last polled.
- ``last_meter_change`` ("Last changed"): when new consumption statistics
  were last written.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import DukeEnergyConfigEntry, DukeEnergyCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: DukeEnergyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up the diagnostic sensors for each meter.

    Meters are enumerated once from the coordinator's first refresh (which
    completed before this platform was forwarded). Meters added later at
    Duke Energy appear after a reload of the config entry.
    """
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for serial_number, service_type in coordinator.meter_info.items():
        entities.append(
            DukeEnergyLastPollSensor(coordinator, entry, serial_number, service_type)
        )
        entities.append(
            DukeEnergyLastChangeSensor(coordinator, entry, serial_number, service_type)
        )
    async_add_entities(entities)


class DukeEnergyDiagnosticSensor(
    CoordinatorEntity["DukeEnergyCoordinator"], SensorEntity
):
    """Base for the per-meter diagnostic timestamp sensors."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        """Report polling health, so never go unavailable on a failed poll."""
        return True

    def __init__(
        self,
        coordinator: DukeEnergyCoordinator,
        entry: DukeEnergyConfigEntry,
        serial_number: str,
        service_type: str,
    ) -> None:
        """Initialize the sensor and its meter device."""
        super().__init__(coordinator)
        self._serial_number = serial_number
        self._attr_unique_id = (
            f"{entry.entry_id}_{serial_number}_{self.translation_key}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial_number)},
            name=f"Duke Energy {service_type.capitalize()} {serial_number}",
            manufacturer="Duke Energy",
            model=f"{service_type.capitalize()} meter",
            serial_number=serial_number,
        )


class DukeEnergyLastPollSensor(DukeEnergyDiagnosticSensor):
    """When the integration last polled Duke Energy (coordinator-wide)."""

    _attr_translation_key = "last_duke_poll"

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the last poll attempt."""
        return self.coordinator.last_poll_time


class DukeEnergyLastChangeSensor(DukeEnergyDiagnosticSensor, RestoreSensor):
    """When this meter last had new consumption statistics written."""

    _attr_translation_key = "last_meter_change"

    def __init__(
        self,
        coordinator: DukeEnergyCoordinator,
        entry: DukeEnergyConfigEntry,
        serial_number: str,
        service_type: str,
    ) -> None:
        """Initialize with no restored value yet."""
        super().__init__(coordinator, entry, serial_number, service_type)
        self._restored_value: datetime | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known value as a fallback across restarts."""
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_sensor_data()) is not None and (
            isinstance(value := last_data.native_value, datetime)
        ):
            self._restored_value = value

    @property
    def native_value(self) -> datetime | None:
        """Return when new data was last written for this meter."""
        return (
            self.coordinator.meter_last_updated.get(self._serial_number)
            or self._restored_value
        )
