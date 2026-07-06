"""Coordinator to handle Duke Energy connections."""

import hashlib
import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any, cast

from .aiodukeenergy import DukeEnergy, DukeEnergyAuthError
from aiohttp import ClientError
from homeassistant.components.recorder import (
    get_instance,  # pyright: ignore[reportPrivateImportUsage]
)
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_SUPPORTED_METER_TYPES = ("ELECTRIC", "GAS")

# Duke Energy data is typically available by 8am ET.
# Check at 9am, 2pm, and 7pm ET, each shifted by a per-user per-day random
# offset in [-2hr, +2hr] derived from the config entry ID and date.
# Using the same offset for all three slots keeps the 10-hour spread intact
# while distributing load across users.
# All of Duke Energy Service Areas are currently in America/New_York timezone
# May need to re-think this if that ever changes and determine timezone based
# on the service address somehow.
_DUKE_TZ = "America/New_York"
_BASE_TIMES_ET = (time(9, 0), time(14, 0), time(19, 0))
_OFFSET_WINDOW = timedelta(hours=2)

type DukeEnergyConfigEntry = ConfigEntry[DukeEnergyCoordinator]


class DukeEnergyCoordinator(DataUpdateCoordinator[None]):
    """Handle inserting statistics."""

    config_entry: DukeEnergyConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: DukeEnergy,
        config_entry: DukeEnergyConfigEntry,
    ) -> None:
        """Initialize the data handler."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Duke Energy",
            update_interval=None,  # Scheduling is handled by _schedule_next_check.
        )
        self.api = api
        self._statistic_ids: set = set()
        self._last_successful_date: date | None = None
        self._unsub_scheduled: Callable[[], None] | None = None
        self._daily_offset: timedelta | None = None
        self._offset_date: date | None = None

        self.config_entry.async_on_unload(self._on_unload)

    def _on_unload(self) -> None:
        """Cancel any pending scheduled update and clear statistics."""
        if self._unsub_scheduled is not None:
            self._unsub_scheduled()
            self._unsub_scheduled = None
        get_instance(self.hass).async_clear_statistics(list(self._statistic_ids))

    def _get_or_create_daily_offset(self, day: date) -> timedelta:
        """Return a stable deterministic offset for the given day, derived from
        the config entry ID and date so it varies per-user and per-day."""
        if self._offset_date != day:
            key = f"{self.config_entry.entry_id}:{day.isoformat()}".encode()
            digest = int(hashlib.sha256(key).hexdigest()[:8], 16)
            max_ms = int(_OFFSET_WINDOW.total_seconds() * 1000)
            offset_ms = (digest % (2 * max_ms + 1)) - max_ms
            self._daily_offset = timedelta(milliseconds=offset_ms)
            self._offset_date = day
            _LOGGER.debug("Daily polling offset for %s: %s", day, self._daily_offset)
        return self._daily_offset  # type: ignore[return-value]

    def _schedule_next_check(self, tz, *, had_success: bool) -> None:
        """Schedule the next data check near 7am, 2pm, or 7pm ET, with a
        per-user per-day offset of up to ±2 hours."""
        if self._unsub_scheduled is not None:
            self._unsub_scheduled()
            self._unsub_scheduled = None

        now = dt_util.now(tz)
        next_time: datetime | None = None

        if not had_success:
            today_offset = self._get_or_create_daily_offset(now.date())
            for base_time in _BASE_TIMES_ET:
                candidate = datetime.combine(now.date(), base_time, tzinfo=tz) + today_offset
                if candidate > now:
                    next_time = candidate
                    break

        if next_time is None:
            # Success, or no remaining windows today — schedule for first slot tomorrow.
            tomorrow = now.date() + timedelta(days=1)
            tomorrow_offset = self._get_or_create_daily_offset(tomorrow)
            next_time = datetime.combine(tomorrow, _BASE_TIMES_ET[0], tzinfo=tz) + tomorrow_offset

        _LOGGER.debug("Next Duke Energy check scheduled for %s", next_time)

        @callback
        def _trigger_refresh(_now: datetime) -> None:
            self.hass.async_create_task(self.async_refresh())

        self._unsub_scheduled = async_track_point_in_time(
            self.hass, _trigger_refresh, next_time
        )

    async def _async_update_data(self) -> None:
        """Insert Duke Energy statistics."""
        tz = await dt_util.async_get_time_zone(_DUKE_TZ)
        today = dt_util.now(tz).date()
        yesterday = today - timedelta(days=1)
        supported_meter_count = 0
        yesterday_data_count = 0
        temp_accounts_seen: set[str] = set()
        cost_entities: dict[str, str] = self.config_entry.options.get(
            "cost_entities", {}
        )

        try:
            if self._last_successful_date == today:
                _LOGGER.debug("Already retrieved today's data, skipping until tomorrow")
                return

            try:
                meters: dict[str, dict[str, Any]] = await self.api.get_meters()
            except DukeEnergyAuthError as err:
                raise ConfigEntryAuthFailed from err

            for serial_number, meter in meters.items():
                if (
                    not isinstance(meter["serviceType"], str)
                    or meter["serviceType"] not in _SUPPORTED_METER_TYPES
                ):
                    _LOGGER.debug(
                        "Skipping unsupported meter type %s", meter["serviceType"]
                    )
                    continue

                id_prefix = f"{meter['serviceType'].lower()}_{serial_number}"
                consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
                self._statistic_ids.add(consumption_statistic_id)
                supported_meter_count += 1
                _LOGGER.debug(
                    "Updating Statistics for %s",
                    consumption_statistic_id,
                )

                last_stat = await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics,
                    self.hass,
                    1,
                    consumption_statistic_id,
                    True,  # noqa: FBT003
                    set(),
                )
                if not last_stat:
                    _LOGGER.debug("Updating statistic for the first time")
                    usage, interval = await self._async_get_energy_usage(meter)
                    consumption_sum = 0.0
                    last_stats_time = None
                else:
                    usage, interval = await self._async_get_energy_usage(
                        meter,
                        last_stat[consumption_statistic_id][0]["start"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
                    )
                    if not usage:
                        _LOGGER.debug("No recent usage data. Skipping update")
                        continue
                    stats = await get_instance(self.hass).async_add_executor_job(
                        statistics_during_period,
                        self.hass,
                        min(usage.keys()),
                        None,
                        {consumption_statistic_id},
                        "hour" if interval == "HOURLY" else "day",
                        None,
                        {"sum"},
                    )
                    consumption_sum = cast(
                        "float",
                        stats[consumption_statistic_id][0]["sum"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
                    )
                    last_stats_time = stats[consumption_statistic_id][0]["start"]  # pyright: ignore[reportTypedDictNotRequiredAccess]

                if any(k.date() == yesterday for k in usage):
                    yesterday_data_count += 1

                consumption_statistics = []

                for start, data in usage.items():
                    if last_stats_time is not None and start.timestamp() <= last_stats_time:
                        continue
                    consumption_sum += data["energy"]

                    # For daily intervals, register usage at noon to better represent
                    # when daily usage occurred rather than at midnight (start of day).
                    stat_start = (
                        start + timedelta(hours=12) if interval == "DAILY" else start
                    )
                    consumption_statistics.append(
                        StatisticData(
                            start=stat_start, state=data["energy"], sum=consumption_sum
                        )
                    )

                name_prefix = (
                    f"Duke Energy {meter['serviceType'].capitalize()} {serial_number}"
                )
                consumption_metadata = StatisticMetaData(
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=f"{name_prefix} Consumption",
                    source=DOMAIN,
                    statistic_id=consumption_statistic_id,
                    unit_class=EnergyConverter.UNIT_CLASS
                    if meter["serviceType"] == "ELECTRIC"
                    else "volume",
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR
                    if meter["serviceType"] == "ELECTRIC"
                    else UnitOfVolume.CENTUM_CUBIC_FEET,
                )

                _LOGGER.debug(
                    "Adding %s statistics for %s",
                    len(consumption_statistics),
                    consumption_statistic_id,
                )
                async_add_external_statistics(
                    self.hass, consumption_metadata, consumption_statistics
                )

                # Cost statistic (only for meters with a configured price sensor)
                cost_entity_id = cost_entities.get(serial_number)
                if cost_entity_id:
                    await self._async_add_cost_statistics(
                        entity_id=cost_entity_id,
                        id_prefix=id_prefix,
                        name_prefix=name_prefix,
                        usage=usage,
                        interval=interval,
                    )

                # Temperature statistic (first meter per account only)
                account_number = meter["account"]["accountNumber"]
                if account_number in temp_accounts_seen:
                    continue
                temp_accounts_seen.add(account_number)

                src_acct_id = meter["account"]["srcAcctId"]
                temperature_statistic_id = f"{DOMAIN}:account_{src_acct_id}_temperature"
                self._statistic_ids.add(temperature_statistic_id)

                last_temp_stat = await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics,
                    self.hass,
                    1,
                    temperature_statistic_id,
                    True,  # noqa: FBT003
                    set(),
                )
                last_temp_time = (
                    last_temp_stat[temperature_statistic_id][0]["start"]  # pyright: ignore[reportTypedDictNotRequiredAccess]
                    if last_temp_stat
                    else None
                )

                daily_temps: dict[date, list] = {}
                for start, data in usage.items():
                    if data["temperature"] is None:
                        continue
                    daily_temps.setdefault(start.date(), []).append(data["temperature"])

                temperature_statistics = []
                for day, temps in sorted(daily_temps.items()):
                    stat_start = datetime.combine(day, time(12, 0), tzinfo=tz)
                    if last_temp_time is not None and stat_start.timestamp() <= last_temp_time:
                        continue
                    temperature_statistics.append(
                        StatisticData(start=stat_start, mean=temps[0])
                    )

                temperature_metadata = StatisticMetaData(
                    mean_type=StatisticMeanType.ARITHMETIC,
                    has_sum=False,
                    name=f"Duke Energy Account {src_acct_id} Temperature",
                    source=DOMAIN,
                    statistic_id=temperature_statistic_id,
                    unit_class="temperature",
                    unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
                )

                _LOGGER.debug(
                    "Adding %s statistics for %s",
                    len(temperature_statistics),
                    temperature_statistic_id,
                )
                async_add_external_statistics(
                    self.hass, temperature_metadata, temperature_statistics
                )

                # --- End temperature statistic addition

            if supported_meter_count > 0 and yesterday_data_count == supported_meter_count:
                self._last_successful_date = today

        finally:
            had_success = self._last_successful_date == today
            if had_success:
                _LOGGER.debug("Duke Energy data retrieval successful")
            else:
                _LOGGER.debug("No new Duke Energy usage data available; will retry at next scheduled time")
            self._schedule_next_check(tz, had_success=had_success)

    async def _async_add_cost_statistics(
        self,
        *,
        entity_id: str,
        id_prefix: str,
        name_prefix: str,
        usage: dict[datetime, dict[str, float | int]],
        interval: str,
    ) -> None:
        """
        Derive and insert a cost statistic for a meter from a price sensor.

        Cost is ``consumption * price`` per interval, where price is read from
        the sensor's long-term statistics. The cost statistic is tracked
        independently of consumption (its own sum baseline) so it can start
        later than consumption and tolerate gaps: intervals with no price are
        simply omitted rather than recorded as zero cost.
        """
        if not usage:
            return

        cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_cost"
        self._statistic_ids.add(cost_statistic_id)

        price_map = await self._async_get_price_map(
            entity_id, min(usage), max(usage), interval
        )
        if not price_map:
            _LOGGER.debug(
                "No price statistics for %s; skipping cost for %s",
                entity_id,
                cost_statistic_id,
            )
            return

        last_cost_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            cost_statistic_id,
            True,  # noqa: FBT003
            {"sum"},
        )
        if not last_cost_stat:
            cost_sum = 0.0
            last_cost_time: float | None = None
        else:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                min(usage.keys()),
                None,
                {cost_statistic_id},
                "hour" if interval == "HOURLY" else "day",
                None,
                {"sum"},
            )
            rows = stats.get(cost_statistic_id, [])
            if rows:
                cost_sum = cast("float", rows[0]["sum"])
                last_cost_time = rows[0]["start"]
            else:
                # Existing cost stats predate the fetch window (e.g. the price
                # sensor was unset for a while). Resume from the last known sum.
                cost_sum = cast("float", last_cost_stat[cost_statistic_id][0]["sum"])  # pyright: ignore[reportTypedDictNotRequiredAccess]
                last_cost_time = last_cost_stat[cost_statistic_id][0]["start"]  # pyright: ignore[reportTypedDictNotRequiredAccess]

        cost_statistics = []
        for start in sorted(usage):
            if last_cost_time is not None and start.timestamp() <= last_cost_time:
                continue
            price = (
                price_map.get(start.timestamp())
                if interval == "HOURLY"
                else price_map.get(start.date())
            )
            if price is None:
                continue
            cost = usage[start]["energy"] * price
            cost_sum += cost
            stat_start = start + timedelta(hours=12) if interval == "DAILY" else start
            cost_statistics.append(
                StatisticData(start=stat_start, state=cost, sum=cost_sum)
            )

        cost_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{name_prefix} Cost",
            source=DOMAIN,
            statistic_id=cost_statistic_id,
            unit_class=None,
            unit_of_measurement=self.hass.config.currency or "USD",
        )

        _LOGGER.debug(
            "Adding %s statistics for %s",
            len(cost_statistics),
            cost_statistic_id,
        )
        async_add_external_statistics(self.hass, cost_metadata, cost_statistics)

    async def _async_get_price_map(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> dict[float, float] | dict[date, float]:
        """
        Return per-interval price from a sensor's long-term statistics.

        Always queries hourly means (aligned to the hour in UTC, so they match
        the hourly usage keys exactly). For HOURLY meters the result is keyed by
        the hour's epoch timestamp; for DAILY meters the hourly means are
        averaged into each Eastern calendar day (keyed by ``date``), which keeps
        the day boundary correct regardless of the recorder's own timezone.
        """
        # Extend the window by a full day so the final interval's hourly buckets
        # are all included (end is exclusive, and DAILY needs the whole day).
        raw = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end + timedelta(days=1),
            {entity_id},
            "hour",
            None,
            {"mean"},
        )
        rows = raw.get(entity_id, [])

        if interval == "HOURLY":
            return {
                row["start"]: row["mean"]
                for row in rows
                if row.get("mean") is not None
            }

        tz = await dt_util.async_get_time_zone(_DUKE_TZ)
        daily: dict[date, list[float]] = {}
        for row in rows:
            if row.get("mean") is None:
                continue
            day = datetime.fromtimestamp(row["start"], tz=tz).date()
            daily.setdefault(day, []).append(row["mean"])
        return {day: sum(means) / len(means) for day, means in daily.items()}

    async def _async_get_energy_usage(
        self, meter: dict[str, Any], start_time: float | None = None
    ) -> tuple[dict[datetime, dict[str, float | int]], str]:
        """
        Get energy usage.

        If start_time is None, get usage since account activation (or as far
        back as possible), otherwise since start_time - 30 days to allow
        corrections in data.

        Duke Energy provides hourly data all the way back to ~3 years.

        Returns a tuple of (usage, interval) where interval is "HOURLY" or "DAILY".
        """
        tz = await dt_util.async_get_time_zone(_DUKE_TZ)
        lookback = timedelta(days=30)
        one = timedelta(days=1)
        if start_time is None:
            # Max 3 years of data
            start = dt_util.now(tz) - timedelta(days=3 * 365)
        else:
            start = datetime.fromtimestamp(start_time, tz=tz) - lookback
        agreement_date = dt_util.parse_datetime(meter["agreementActiveDate"])
        if agreement_date is not None:
            start = max(agreement_date.replace(tzinfo=tz), start)

        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = dt_util.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - one
        _LOGGER.debug("Data lookup range: %s - %s", start, end)

        if meter["serviceType"] == "GAS":
            run_interval = "DAILY"
            run_period = "WEEK"
        else:
            run_interval = "HOURLY"
            run_period = "DAY"

        start_step = max(end - lookback, start)
        end_step = end
        usage: dict[datetime, dict[str, float | int]] = {}
        while True:
            _LOGGER.debug(
                "Getting %s %s usage: %s - %s",
                meter["serviceType"].lower().capitalize(),
                run_interval.lower(),
                start_step,
                end_step,
            )
            try:
                # Get data
                try:
                    results = await self.api.get_energy_usage(
                        meter["serialNum"],
                        run_interval,
                        run_period,
                        start_step,
                        end_step,
                    )
                except DukeEnergyAuthError as err:
                    raise ConfigEntryAuthFailed from err

                usage = {**results["data"], **usage}

                for missing in results["missing"]:
                    _LOGGER.debug("Missing data: %s", missing)

                # Set next range
                end_step = start_step - one
                start_step = max(start_step - lookback, start)

                # Make sure we don't go back too far
                if end_step < start:
                    break
            except (TimeoutError, ClientError):
                # ClientError is raised when there is no more data for the range
                break

        _LOGGER.debug("Got %s meter usage reads", len(usage))
        return usage, run_interval
