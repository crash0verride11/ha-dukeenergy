"""Coordinator to handle Duke Energy connections."""

import hashlib
import logging
from bisect import bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, cast

from aiodukeenergy_co import DukeEnergy, DukeEnergyAuthError
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
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfVolume,
)
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


@dataclass(slots=True, frozen=True)
class MeterInfo:
    """A supported meter's identity, for device creation at platform setup."""

    service_type: str
    src_acct_id: str


def _summary_value(summary: dict[str, Any], period: str, key: str) -> float | None:
    """Pull a numeric field out of a monthly usage summary, or None."""
    value = (summary.get(period) or {}).get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class _PriceMap:
    """
    Per-interval price lookup from a price sensor's long-term statistics.

    ``keys`` are epoch timestamps for HOURLY meters and Eastern calendar dates
    for DAILY meters, kept sorted (ascending) alongside their ``prices`` so a
    lookup is a single bisect. An empty map is falsy.
    """

    interval: str
    keys: list[float | date]
    prices: list[float]

    def __bool__(self) -> bool:
        return bool(self.keys)

    @property
    def earliest(self) -> float | None:
        """The oldest known price, or None when the map is empty."""
        return self.prices[0] if self.prices else None

    def price_at(self, start: datetime) -> float | None:
        """
        Return the most recent price at or before ``start``, carried forward.

        Utility rates change irregularly, so the last known price stays correct
        across recording gaps (e.g. HA downtime while Duke kept metering).
        Returns None when no price precedes the interval — pricing never reaches
        backward in time (and so an empty map always returns None).
        """
        key: float | date = (
            start.timestamp() if self.interval == "HOURLY" else start.date()
        )
        index = bisect_right(self.keys, key) - 1
        if index < 0:
            return None
        return self.prices[index]


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
        # Sensor sources (see sensor.py):
        # when the last poll attempt ran (success or not), when each meter
        # last had consumption statistics written, each supported meter's
        # identity, each account's display number, per-meter bill-cycle
        # usage summaries, and per-account bill-cycle costs.
        self.last_poll_time: datetime | None = None
        self.meter_last_updated: dict[str, datetime] = {}
        self.meter_info: dict[str, MeterInfo] = {}
        self.account_info: dict[str, str] = {}
        self.monthly_usage: dict[str, dict[str, float | None]] = {}
        self.account_costs: dict[str, dict[str, float | None]] = {}
        self._unsub_scheduled: Callable[[], None] | None = None
        self._daily_offset: timedelta | None = None
        self._offset_date: date | None = None

        self.config_entry.async_on_unload(self._on_unload)

    def _on_unload(self) -> None:
        """
        Cancel any pending scheduled update.

        Statistics are intentionally NOT cleared here. Unload runs on every
        reload and restart, so wiping the external statistics would discard all
        history. History is cleared only when the config entry is removed (see
        async_remove_entry).
        """
        if self._unsub_scheduled is not None:
            self._unsub_scheduled()
            self._unsub_scheduled = None

    def _get_or_create_daily_offset(self, day: date) -> timedelta:
        """
        Return a stable deterministic offset for the given day.

        Derived from the config entry ID and date so it varies per-user
        and per-day.
        """
        if self._offset_date != day:
            key = f"{self.config_entry.entry_id}:{day.isoformat()}".encode()
            digest = int(hashlib.sha256(key).hexdigest()[:8], 16)
            max_ms = int(_OFFSET_WINDOW.total_seconds() * 1000)
            offset_ms = (digest % (2 * max_ms + 1)) - max_ms
            self._daily_offset = timedelta(milliseconds=offset_ms)
            self._offset_date = day
            _LOGGER.debug("Daily polling offset for %s: %s", day, self._daily_offset)
        return self._daily_offset  # type: ignore[return-value]

    def _schedule_next_check(self, tz: tzinfo, *, had_success: bool) -> None:
        """
        Schedule the next data check near 7am, 2pm, or 7pm ET.

        Applies a per-user per-day offset of up to ±2 hours.
        """
        if self._unsub_scheduled is not None:
            self._unsub_scheduled()
            self._unsub_scheduled = None

        now = dt_util.now(tz)
        next_time: datetime | None = None

        if not had_success:
            today_offset = self._get_or_create_daily_offset(now.date())
            for base_time in _BASE_TIMES_ET:
                candidate = (
                    datetime.combine(now.date(), base_time, tzinfo=tz) + today_offset
                )
                if candidate > now:
                    next_time = candidate
                    break

        if next_time is None:
            # Success, or no remaining windows today — schedule for first slot tomorrow.
            tomorrow = now.date() + timedelta(days=1)
            tomorrow_offset = self._get_or_create_daily_offset(tomorrow)
            next_time = (
                datetime.combine(tomorrow, _BASE_TIMES_ET[0], tzinfo=tz)
                + tomorrow_offset
            )

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
        monthly_accounts_seen: set[str] = set()
        bill_cycle_starts: dict[str, datetime | None] = {}
        cost_meters: dict[str, dict[str, Any]] = self.config_entry.options.get(
            "cost_meters", {}
        )
        do_backfill = bool(cost_meters) and self.config_entry.options.get(
            "backfill_cost", False
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

                src_acct_id = meter["account"]["srcAcctId"]
                self.meter_info[serial_number] = MeterInfo(
                    meter["serviceType"], src_acct_id
                )
                self.account_info[src_acct_id] = meter["account"]["accountNumber"]
                # Before the statistics work: its early `continue`s must not
                # skip the bill-cycle summary refresh.
                await self._async_update_monthly_usage(
                    serial_number, src_acct_id, monthly_accounts_seen, bill_cycle_starts
                )

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
                    if (
                        last_stats_time is not None
                        and start.timestamp() <= last_stats_time
                    ):
                        continue
                    consumption_sum += data["energy"]
                    consumption_statistics.append(
                        StatisticData(
                            start=self._stat_start(start, interval),
                            state=data["energy"],
                            sum=consumption_sum,
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
                if consumption_statistics:
                    # New consumption rows landed: the meter's data changed.
                    # Cost/temperature are derived, so they don't count.
                    self.meter_last_updated[serial_number] = dt_util.utcnow()

                # Cost statistic (per-meter mode: sensor / static / off)
                cost_config = cost_meters.get(serial_number)
                if cost_config:
                    await self._async_update_cost(
                        cost_config=cost_config,
                        id_prefix=id_prefix,
                        name_prefix=name_prefix,
                        usage=usage,
                        interval=interval,
                        do_backfill=do_backfill,
                    )

                # Temperature statistic (first meter per account only)
                account_number = meter["account"]["accountNumber"]
                if account_number in temp_accounts_seen:
                    continue
                temp_accounts_seen.add(account_number)

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
                    if (
                        last_temp_time is not None
                        and stat_start.timestamp() <= last_temp_time
                    ):
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

            if do_backfill:
                # One-shot: clear the flag now that history has been repriced, so
                # the full reprice does not repeat on every poll or on restart.
                # No update listener is registered, so this does not reload.
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    options={**self.config_entry.options, "backfill_cost": False},
                )

            if (
                supported_meter_count > 0
                and yesterday_data_count == supported_meter_count
            ):
                self._last_successful_date = today

        finally:
            self.last_poll_time = dt_util.utcnow()
            had_success = self._last_successful_date == today
            if had_success:
                _LOGGER.debug("Duke Energy data retrieval successful")
            else:
                _LOGGER.debug(
                    "No new Duke Energy usage data available; "
                    "will retry at next scheduled time"
                )
            self._schedule_next_check(tz, had_success=had_success)

    async def _async_get_bill_cycle_start(self, serial_number: str) -> datetime | None:
        """
        Derive the current bill cycle's start from the MONTHLY usage graph.

        The graph returns only completed cycles, so the current cycle starts
        the day after the last entry's endDate. Returns None when the graph
        is unavailable, in which case get_monthly_usage falls back to
        yesterday and thisPeriod only covers the current day.
        """
        tz = await dt_util.async_get_time_zone(_DUKE_TZ)
        end = dt_util.now(tz) - timedelta(days=1)
        # A ~2-month window guarantees at least one completed ~30-day cycle.
        start = end - timedelta(days=62)
        try:
            result = await self.api.get_energy_usage(
                serial_number, "MONTHLY", "YEAR", start, end
            )
        except DukeEnergyAuthError as err:
            raise ConfigEntryAuthFailed from err
        except (TimeoutError, ClientError) as err:
            _LOGGER.warning(
                "Could not fetch billing cycles for meter %s: %s", serial_number, err
            )
            return None

        try:
            cycles = result["data"]
            cycle_start = date.fromisoformat(cycles[-1]["endDate"]) + timedelta(days=1)
        except (IndexError, KeyError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "Could not derive the bill cycle start for meter %s: %s",
                serial_number,
                err,
            )
            return None
        return datetime.combine(cycle_start, time.min)

    async def _async_update_monthly_usage(
        self,
        serial_number: str,
        src_acct_id: str,
        accounts_seen: set[str],
        bill_cycle_starts: dict[str, datetime | None],
    ) -> None:
        """
        Refresh a meter's bill-cycle usage summary (see sensor.py).

        The billing cycle is account-wide, so its start is derived once per
        account per poll (cached in ``bill_cycle_starts``) from the first
        meter seen and shared by the account's meters. The summary's bill
        amounts are likewise account-wide, so costs are stored once per
        account from the first meter seen. A failure here only keeps the
        previous values; it must not abort statistics ingestion.
        """
        if src_acct_id not in bill_cycle_starts:
            bill_cycle_starts[src_acct_id] = await self._async_get_bill_cycle_start(
                serial_number
            )

        try:
            summary = await self.api.get_monthly_usage(
                serial_number, start_date=bill_cycle_starts[src_acct_id]
            )
        except DukeEnergyAuthError as err:
            raise ConfigEntryAuthFailed from err
        except (TimeoutError, ClientError) as err:
            _LOGGER.warning(
                "Could not fetch the bill-cycle summary for meter %s: %s",
                serial_number,
                err,
            )
            return

        self.monthly_usage[serial_number] = {
            "this_cycle": _summary_value(summary, "thisPeriod", "totalUsage"),
            "last_cycle": _summary_value(summary, "lastPeriod", "totalUsage"),
            "last_year": _summary_value(summary, "lastYearPeriod", "totalUsage"),
        }
        if src_acct_id not in accounts_seen:
            accounts_seen.add(src_acct_id)
            self.account_costs[src_acct_id] = {
                "last_cycle": _summary_value(summary, "lastPeriod", "bill"),
                "last_year": _summary_value(summary, "lastYearPeriod", "bill"),
            }

    @staticmethod
    def _stat_start(start: datetime, interval: str) -> datetime:
        """
        Return the statistic timestamp for an interval's start.

        Daily usage is registered at noon rather than midnight to better
        represent when it occurred; hourly usage is recorded as-is.
        """
        return start + timedelta(hours=12) if interval == "DAILY" else start

    async def _async_update_cost(  # noqa: PLR0913
        self,
        *,
        cost_config: dict[str, Any],
        id_prefix: str,
        name_prefix: str,
        usage: dict[datetime, dict[str, float | int]],
        interval: str,
        do_backfill: bool,
    ) -> None:
        """
        Route a meter's cost update according to its configured mode.

        ``sensor`` prices from a price entity's statistics/state; ``static``
        prices every interval at a fixed rate; anything else (``off`` or an
        unconfigured meter) skips cost entirely, leaving existing cost
        statistics untouched. Backfill and incremental paths share the flat
        rate, so a static rate can also (re)price full history.
        """
        mode = cost_config.get("mode")
        if mode not in ("sensor", "static"):
            return
        entity_id = cost_config.get("entity_id")
        static_price = cost_config.get("price") if mode == "static" else None
        if mode == "sensor" and not entity_id:
            return
        if mode == "static" and static_price is None:
            return

        if do_backfill:
            await self._async_backfill_cost(
                entity_id=entity_id,
                static_price=static_price,
                id_prefix=id_prefix,
                name_prefix=name_prefix,
                interval=interval,
            )
        else:
            await self._async_add_cost_statistics(
                entity_id=entity_id,
                static_price=static_price,
                id_prefix=id_prefix,
                name_prefix=name_prefix,
                usage=usage,
                interval=interval,
            )

    async def _async_add_cost_statistics(  # noqa: PLR0913
        self,
        *,
        entity_id: str | None,
        static_price: float | None,
        id_prefix: str,
        name_prefix: str,
        usage: dict[datetime, dict[str, float | int]],
        interval: str,
    ) -> None:
        """
        Derive and insert a cost statistic for a meter.

        Cost is ``consumption * price`` per interval. In sensor mode the price
        is read from the entity's long-term statistics, carried forward across
        recording gaps (see _PriceMap); in static mode a fixed ``static_price``
        applies to every interval. The cost statistic is tracked independently
        of consumption (its own sum baseline) so it can start later than
        consumption: intervals before the sensor's first known price are
        omitted rather than recorded at a guessed or zero cost.

        For the full-history (re)price, see _async_backfill_cost.
        """
        cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_cost"
        self._statistic_ids.add(cost_statistic_id)

        if not usage:
            return

        usage_keys = sorted(usage)
        # A static rate is a flat price at every interval: an empty map (so
        # price_at() always returns None) plus the rate as the fallback price.
        if static_price is not None:
            price_map = _PriceMap(interval, [], [])
            pre_history_price: float | None = static_price
        else:
            if entity_id is None:
                return
            price_map = await self._async_get_price_map(
                entity_id, usage_keys[0], usage_keys[-1], interval
            )
            # With no long-term statistics, every interval is "pre-history" and
            # this flat current-state fallback prices all of them. With a map
            # present it stays None, so pre-first-price intervals are skipped.
            pre_history_price = None
            if not price_map:
                pre_history_price = self._get_current_price(entity_id)
                if pre_history_price is None:
                    # A price sensor with neither LTS in the usage window
                    # (~30 days) nor a numeric state is dead/misconfigured.
                    _LOGGER.warning(
                        "Price sensor %s has no statistics in the usage window "
                        "and no numeric state; skipping cost for %s",
                        entity_id,
                        cost_statistic_id,
                    )
                    return

        # Resume the running sum from the last recorded cost point and append
        # only newer intervals. Cost is price-gated, so the series is NOT
        # contiguous like consumption; re-summing from the oldest bucket would
        # drop skipped intervals and corrupt the sum (a new point's sum falling
        # below the previous one's shows as negative cost).
        last_cost_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            cost_statistic_id,
            True,  # noqa: FBT003
            {"sum"},
        )
        if last_cost_stat:
            starting_sum = cast("float", last_cost_stat[cost_statistic_id][0]["sum"])  # pyright: ignore[reportTypedDictNotRequiredAccess]
            last_cost_time: float | None = last_cost_stat[cost_statistic_id][0]["start"]  # pyright: ignore[reportTypedDictNotRequiredAccess]
        else:
            starting_sum = 0.0
            last_cost_time = None

        samples = (
            (start, self._stat_start(start, interval), usage[start]["energy"])
            for start in usage_keys
            if last_cost_time is None or start.timestamp() > last_cost_time
        )
        self._write_cost_statistics(
            cost_statistic_id=cost_statistic_id,
            name_prefix=name_prefix,
            samples=samples,
            price_map=price_map,
            pre_history_price=pre_history_price,
            starting_sum=starting_sum,
            log_verb="Adding",
        )

    def _write_cost_statistics(  # noqa: PLR0913
        self,
        *,
        cost_statistic_id: str,
        name_prefix: str,
        samples: Iterable[tuple[datetime, datetime, float]],
        price_map: _PriceMap,
        pre_history_price: float | None,
        starting_sum: float,
        log_verb: str,
    ) -> None:
        """
        Price each ``(lookup_dt, stat_start, energy)`` sample and write the series.

        ``price_map.price_at`` supplies the carried-forward price; an interval
        with no prior price falls back to ``pre_history_price`` (None skips it).
        The running sum accumulates from ``starting_sum``.
        """
        cost_sum = starting_sum
        cost_statistics: list[StatisticData] = []
        for lookup_dt, stat_start, energy in samples:
            price = price_map.price_at(lookup_dt)
            if price is None:
                price = pre_history_price
            if price is None:
                continue
            cost = energy * price
            cost_sum += cost
            cost_statistics.append(
                StatisticData(start=stat_start, state=cost, sum=cost_sum)
            )

        _LOGGER.debug(
            "%s %s statistics for %s", log_verb, len(cost_statistics), cost_statistic_id
        )
        async_add_external_statistics(
            self.hass,
            self._cost_metadata(cost_statistic_id, name_prefix),
            cost_statistics,
        )

    def _cost_metadata(
        self, cost_statistic_id: str, name_prefix: str
    ) -> StatisticMetaData:
        """Return the metadata for a meter's cost statistic."""
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{name_prefix} Cost",
            source=DOMAIN,
            statistic_id=cost_statistic_id,
            unit_class=None,
            unit_of_measurement=self.hass.config.currency or "USD",
        )

    async def _async_backfill_cost(
        self,
        *,
        entity_id: str | None,
        static_price: float | None,
        id_prefix: str,
        name_prefix: str,
        interval: str,
    ) -> None:
        """
        Reprice the full consumption history into the cost statistic.

        Reads the stored consumption statistics (rather than re-fetching from
        Duke) and prices every interval. In sensor mode, gaps inside the price
        history carry the most recent prior price forward (see _PriceMap), and
        intervals that predate the sensor's history use the earliest available
        price — a deliberate backward-fill that only this explicit full-history
        action performs (the incremental path skips pre-history intervals
        instead). In static mode every interval is priced at ``static_price``.
        The sum is rebuilt from zero across the whole series, overwriting any
        incremental cost rows.
        """
        consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
        cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_cost"

        # Read at hourly resolution so the stored starts come back exactly as
        # written (top-of-hour for electric, noon for gas) — a "day" period would
        # re-bucket gas to midnight and misalign it against the incremental path.
        # Consumption reaches back ~3 years; 4 years of headroom covers it.
        history_start = dt_util.now() - timedelta(days=4 * 365)
        consumption = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            history_start,
            None,
            {consumption_statistic_id},
            "hour",
            None,
            {"state"},
        )
        rows = sorted(
            consumption.get(consumption_statistic_id, []),
            key=lambda row: row["start"],
        )
        if not rows:
            _LOGGER.debug(
                "No consumption history to backfill for %s", cost_statistic_id
            )
            return

        # A static rate prices the whole history flat: empty map, rate as the
        # (backward-filled) fallback for every interval.
        if static_price is not None:
            price_map = _PriceMap(interval, [], [])
            earliest_price: float | None = static_price
        else:
            if entity_id is None:
                return
            price_map = await self._async_get_price_map(
                entity_id,
                dt_util.utc_from_timestamp(rows[0]["start"]),
                dt_util.utc_from_timestamp(rows[-1]["start"]),
                interval,
            )
            # Pre-history intervals (before the sensor's first price) are
            # backward-filled with the earliest known price; with no LTS map at
            # all the whole history is repriced at the current state as a flat rate.
            earliest_price = price_map.earliest
            if earliest_price is None:
                earliest_price = self._get_current_price(entity_id)
                if earliest_price is None:
                    # The user explicitly requested a backfill; make its failure
                    # visible instead of completing silently with no cost data.
                    _LOGGER.warning(
                        "No price statistics or current state to backfill cost for %s",
                        cost_statistic_id,
                    )
                    return

        tz = await dt_util.async_get_time_zone(_DUKE_TZ)
        # An ET datetime satisfies both _PriceMap key derivations:
        # .timestamp() is tz-independent and .date() needs Eastern. The stored
        # start (already top-of-hour/noon) is reused verbatim as the stat start.
        samples = (
            (
                datetime.fromtimestamp(row["start"], tz=tz),
                dt_util.utc_from_timestamp(row["start"]),
                row["state"],
            )
            for row in rows
            if row.get("state") is not None
        )
        self._write_cost_statistics(
            cost_statistic_id=cost_statistic_id,
            name_prefix=name_prefix,
            samples=samples,
            price_map=price_map,
            pre_history_price=earliest_price,
            starting_sum=0.0,
            log_verb="Backfilling",
        )

    def _get_current_price(self, entity_id: str) -> float | None:
        """
        Return the price sensor's current numeric state, or None.

        Used as a flat-price fallback when the sensor has no long-term
        statistics to read a per-interval price from.
        """
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    async def _async_get_price_map(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> _PriceMap:
        """
        Build a _PriceMap from a price sensor's long-term statistics.

        Always queries hourly means (aligned to the hour in UTC, so they match
        the hourly usage keys exactly). For HOURLY meters the map is keyed by
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
            pairs: list[tuple[float | date, float]] = sorted(
                (row["start"], row["mean"])
                for row in rows
                if row.get("mean") is not None
            )
        else:
            tz = await dt_util.async_get_time_zone(_DUKE_TZ)
            daily: dict[date, list[float]] = {}
            for row in rows:
                if row.get("mean") is None:
                    continue
                day = datetime.fromtimestamp(row["start"], tz=tz).date()
                daily.setdefault(day, []).append(row["mean"])
            pairs = sorted(
                (day, sum(means) / len(means)) for day, means in daily.items()
            )

        return _PriceMap(
            interval=interval,
            keys=[key for key, _ in pairs],
            prices=[price for _, price in pairs],
        )

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
