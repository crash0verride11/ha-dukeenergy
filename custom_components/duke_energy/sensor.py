"""
Sensors for the Duke Energy integration.

One account device per Duke Energy account, holding:

- ``last_duke_poll`` ("Last updated"): when Duke Energy was last polled.
- ``cost_last_bill_cycle`` / ``cost_last_year``: the account-wide bill
  amounts from the monthly usage summary.

One meter device per supported meter (linked to its account via_device),
holding:

- ``last_meter_change`` ("Last changed"): when new consumption statistics
  were last written.
- ``usage_this_bill_cycle`` / ``usage_last_bill_cycle`` / ``usage_last_year``:
  the meter's bill-cycle usage totals from the monthly usage summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import (
        DukeEnergyConfigEntry,
        DukeEnergyCoordinator,
        MeterInfo,
    )


def _as_float(value: Any) -> float | None:
    """Coerce a billing field to a float, or None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    """Parse an ISO date string (e.g. dueDate), or None."""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _as_sentence(value: Any) -> str | None:
    """Render an all-caps status (e.g. abbreviatedBillStatus) as a sentence."""
    if not isinstance(value, str) or not value:
        return None
    return value.capitalize()


@dataclass(frozen=True, kw_only=True)
class DukeEnergySummarySensorDescription(SensorEntityDescription):
    """Describes a sensor fed from the monthly usage summary."""

    bucket: str  # key within a coordinator monthly_usage / account_costs entry


USAGE_SENSORS: tuple[DukeEnergySummarySensorDescription, ...] = (
    DukeEnergySummarySensorDescription(
        key="usage_this_bill_cycle",
        translation_key="usage_this_bill_cycle",
        bucket="this_cycle",
        suggested_display_precision=2,
    ),
    DukeEnergySummarySensorDescription(
        key="usage_last_bill_cycle",
        translation_key="usage_last_bill_cycle",
        bucket="last_cycle",
        suggested_display_precision=2,
    ),
    DukeEnergySummarySensorDescription(
        key="usage_last_year",
        translation_key="usage_last_year",
        bucket="last_year",
        suggested_display_precision=2,
    ),
)

COST_SENSORS: tuple[DukeEnergySummarySensorDescription, ...] = (
    DukeEnergySummarySensorDescription(
        key="cost_last_bill_cycle",
        translation_key="cost_last_bill_cycle",
        bucket="last_cycle",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
    ),
    DukeEnergySummarySensorDescription(
        key="cost_last_year",
        translation_key="cost_last_year",
        bucket="last_year",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
    ),
)


@dataclass(frozen=True, kw_only=True)
class DukeEnergyBillingSensorDescription(SensorEntityDescription):
    """Describes a sensor fed from the billing and payment info."""

    value_fn: Callable[[dict[str, Any]], Any]


BILLING_SENSORS: tuple[DukeEnergyBillingSensorDescription, ...] = (
    DukeEnergyBillingSensorDescription(
        key="bill_balance",
        translation_key="bill_balance",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda acct: _as_float(acct.get("balance")),
    ),
    DukeEnergyBillingSensorDescription(
        key="bill_due_date",
        translation_key="bill_due_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda acct: _as_date(acct.get("dueDate")),
    ),
    DukeEnergyBillingSensorDescription(
        key="bill_status",
        translation_key="bill_status",
        value_fn=lambda acct: _as_sentence(acct.get("abbreviatedBillStatus")),
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: DukeEnergyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up the account and meter sensors.

    Meters are enumerated once from the coordinator's first refresh (which
    completed before this platform was forwarded). Meters added later at
    Duke Energy appear after a reload of the config entry. Account entities
    are appended before their meters' so the account device exists when a
    meter device references it via_device.
    """
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    accounts_seen: set[str] = set()
    for serial_number, info in coordinator.meter_info.items():
        if info.src_acct_id not in accounts_seen:
            accounts_seen.add(info.src_acct_id)
            entities.append(
                DukeEnergyLastPollSensor(coordinator, entry, info.src_acct_id)
            )
            entities.extend(
                DukeEnergyAccountCostSensor(
                    coordinator, entry, info.src_acct_id, description
                )
                for description in COST_SENSORS
            )
            entities.extend(
                DukeEnergyBillingSensor(
                    coordinator, entry, info.src_acct_id, description
                )
                for description in BILLING_SENSORS
            )
        entities.append(
            DukeEnergyLastChangeSensor(coordinator, entry, serial_number, info)
        )
        entities.extend(
            DukeEnergyMeterUsageSensor(
                coordinator, entry, serial_number, info, description
            )
            for description in USAGE_SENSORS
        )
    async_add_entities(entities)


class DukeEnergyMeterEntity(CoordinatorEntity["DukeEnergyCoordinator"], SensorEntity):
    """Base for sensors on a meter device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DukeEnergyCoordinator,
        entry: DukeEnergyConfigEntry,
        serial_number: str,
        info: MeterInfo,
        key: str,
    ) -> None:
        """Initialize the sensor and its meter device."""
        super().__init__(coordinator)
        self._serial_number = serial_number
        self._attr_unique_id = f"{entry.entry_id}_{serial_number}_{key}"
        service = info.service_type.capitalize()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial_number)},
            name=f"Duke Energy {service} {serial_number}",
            manufacturer="Duke Energy",
            model=f"{service} meter",
            serial_number=serial_number,
            via_device=(DOMAIN, f"account_{info.src_acct_id}"),
        )


class DukeEnergyAccountEntity(CoordinatorEntity["DukeEnergyCoordinator"], SensorEntity):
    """Base for sensors on an account device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DukeEnergyCoordinator,
        entry: DukeEnergyConfigEntry,
        src_acct_id: str,
        key: str,
    ) -> None:
        """Initialize the sensor and its account device."""
        super().__init__(coordinator)
        self._src_acct_id = src_acct_id
        self._attr_unique_id = f"{entry.entry_id}_account_{src_acct_id}_{key}"
        account_number = coordinator.account_info.get(src_acct_id, src_acct_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"account_{src_acct_id}")},
            name=f"Duke Energy Account {account_number}",
            manufacturer="Duke Energy",
        )


class DukeEnergyLastPollSensor(DukeEnergyAccountEntity):
    """When the integration last polled Duke Energy (account-wide)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_duke_poll"

    def __init__(
        self,
        coordinator: DukeEnergyCoordinator,
        entry: DukeEnergyConfigEntry,
        src_acct_id: str,
    ) -> None:
        """Initialize on the account device."""
        super().__init__(coordinator, entry, src_acct_id, "last_duke_poll")

    @property
    def available(self) -> bool:
        """Report polling health, so never go unavailable on a failed poll."""
        return True

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the last poll attempt."""
        return self.coordinator.last_poll_time


class DukeEnergyLastChangeSensor(DukeEnergyMeterEntity, RestoreSensor):
    """When this meter last had new consumption statistics written."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_meter_change"

    def __init__(
        self,
        coordinator: DukeEnergyCoordinator,
        entry: DukeEnergyConfigEntry,
        serial_number: str,
        info: MeterInfo,
    ) -> None:
        """Initialize with no restored value yet."""
        super().__init__(coordinator, entry, serial_number, info, "last_meter_change")
        self._restored_value: datetime | None = None

    @property
    def available(self) -> bool:
        """Report polling health, so never go unavailable on a failed poll."""
        return True

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


class DukeEnergyMeterUsageSensor(DukeEnergyMeterEntity):
    """A meter's bill-cycle usage total from the monthly usage summary."""

    entity_description: DukeEnergySummarySensorDescription

    def __init__(
        self,
        coordinator: DukeEnergyCoordinator,
        entry: DukeEnergyConfigEntry,
        serial_number: str,
        info: MeterInfo,
        description: DukeEnergySummarySensorDescription,
    ) -> None:
        """Initialize with the meter's service-type unit."""
        self.entity_description = description
        super().__init__(coordinator, entry, serial_number, info, description.key)
        if info.service_type == "GAS":
            self._attr_device_class = SensorDeviceClass.GAS
            self._attr_native_unit_of_measurement = UnitOfVolume.CENTUM_CUBIC_FEET
            self._attr_suggested_unit_of_measurement = UnitOfVolume.CENTUM_CUBIC_FEET
        else:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    @property
    def native_value(self) -> float | None:
        """Return the usage total for this sensor's period."""
        usage = self.coordinator.monthly_usage.get(self._serial_number)
        return usage.get(self.entity_description.bucket) if usage else None


class DukeEnergyAccountCostSensor(DukeEnergyAccountEntity):
    """An account-wide bill amount from the monthly usage summary."""

    entity_description: DukeEnergySummarySensorDescription

    def __init__(
        self,
        coordinator: DukeEnergyCoordinator,
        entry: DukeEnergyConfigEntry,
        src_acct_id: str,
        description: DukeEnergySummarySensorDescription,
    ) -> None:
        """Initialize with the HA-configured currency."""
        self.entity_description = description
        super().__init__(coordinator, entry, src_acct_id, description.key)
        self._attr_native_unit_of_measurement = (
            coordinator.hass.config.currency or "USD"
        )

    @property
    def native_value(self) -> float | None:
        """Return the bill amount for this sensor's period."""
        costs = self.coordinator.account_costs.get(self._src_acct_id)
        return costs.get(self.entity_description.bucket) if costs else None


class DukeEnergyBillingSensor(DukeEnergyAccountEntity):
    """An account-wide billing and payment headline value."""

    entity_description: DukeEnergyBillingSensorDescription

    def __init__(
        self,
        coordinator: DukeEnergyCoordinator,
        entry: DukeEnergyConfigEntry,
        src_acct_id: str,
        description: DukeEnergyBillingSensorDescription,
    ) -> None:
        """Initialize, adding the currency unit for the monetary balance."""
        self.entity_description = description
        super().__init__(coordinator, entry, src_acct_id, description.key)
        if description.device_class is SensorDeviceClass.MONETARY:
            self._attr_native_unit_of_measurement = (
                coordinator.hass.config.currency or "USD"
            )

    @property
    def native_value(self) -> float | date | str | None:
        """Return this sensor's billing value for the account."""
        account_number = self.coordinator.account_info.get(self._src_acct_id)
        account = self.coordinator.billing_payment_info.get(account_number or "")
        return self.entity_description.value_fn(account) if account else None
