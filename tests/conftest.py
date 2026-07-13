"""Common fixtures for the Duke Energy tests."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Generator
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.duke_energy.const import DOMAIN
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _make_id_token(
    user_id: str = "test-user-id",
    email: str = "test@example.com",
    expires_in: int = 3600,
) -> str:
    """
    Build a minimal unsigned JWT for testing.

    PyJWT's decode(options={"verify_signature": False}) only needs the
    base64-encoded header.payload segments to be valid JSON — the signature
    can be anything.
    """
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "internal_identifier": user_id,
                    "email": email,
                    "exp": int(time.time()) + expires_in,
                }
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.fakesig"


@pytest.fixture
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:  # noqa: ARG001
    """Allow custom components to be loaded.

    Not autouse: tests that also need ``recorder_mock`` must let the recorder set
    up before ``hass``, which an autouse hass-dependent fixture would prevent.
    Tests that load the integration request this (or ``enable_custom_integrations``)
    explicitly.
    """


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Prevent real async_setup_entry from running in config-flow tests."""
    with patch(
        "custom_components.duke_energy.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        yield mock_setup_entry


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mocked OAuth2 config entry (not yet added to hass)."""
    id_token = _make_id_token()
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id",
        title="test@example.com",
        version=2,
        minor_version=1,
        data={
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "test-access-token",
                "id_token": id_token,
                "refresh_token": "test-refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "expires_at": time.time() + 3600,
            },
        },
    )


@pytest.fixture
def mock_recorder() -> Generator[Mock]:
    """Mock the HA recorder instance and all statistics helpers.

    Patches all recorder interactions in the coordinator so tests never need
    a real recorder process or database.
    """
    recorder_instance = Mock()

    # async_add_executor_job is awaited; it runs get_last_statistics /
    # statistics_during_period synchronously.  Use a passthrough so that
    # module-level patches on those functions are actually respected — a
    # fixed return_value would ignore the callable argument entirely.
    async def _passthrough(func, *args, **kwargs):
        return func(*args, **kwargs)

    recorder_instance.async_add_executor_job = AsyncMock(side_effect=_passthrough)
    recorder_instance.async_clear_statistics = Mock()

    with (
        patch(
            "custom_components.duke_energy.coordinator.get_instance",
            return_value=recorder_instance,
        ),
        patch(
            "custom_components.duke_energy.coordinator.async_add_external_statistics",
        ) as mock_add_stats,
    ):
        recorder_instance.mock_add_stats = mock_add_stats
        yield recorder_instance


def _to_epoch(value: object) -> float:
    """Coerce a StatisticData/query start (datetime or epoch) to an epoch float."""
    if hasattr(value, "timestamp"):
        return value.timestamp()  # type: ignore[attr-defined]
    return float(value)  # type: ignore[arg-type]


class StatsStore:
    """In-memory stand-in for the recorder's statistics tables.

    Records what the coordinator inserts via ``async_add_external_statistics``
    and answers ``get_last_statistics`` / ``statistics_during_period`` from it,
    so tests exercise the real read-modify-write flow (sum baselines, backfill
    read-back, price lookups) without a recorder process. Row ``start`` values
    are stored and returned as epoch floats, matching what the coordinator reads.

    Period bucketing is intentionally not emulated: rows come back at their
    exact stored start. Tests keep data small and control it directly.
    """

    def __init__(self) -> None:
        self.data: dict[str, list[dict]] = {}
        self.mock_add: Mock

    def seed(self, statistic_id: str, rows: list[dict]) -> None:
        """Preload rows (e.g. a price sensor's mean history) for a statistic."""
        self.data[statistic_id] = sorted(
            ({**r, "start": _to_epoch(r["start"])} for r in rows),
            key=lambda r: r["start"],
        )

    def add_external(self, _hass: object, metadata: dict, statistics: list) -> None:
        """Upsert inserted statistics, keyed by statistic_id and start."""
        rows = self.data.setdefault(metadata["statistic_id"], [])
        for stat in statistics:
            start = _to_epoch(stat["start"])
            keep = {k: stat[k] for k in ("state", "sum", "mean") if k in stat}
            rows[:] = [r for r in rows if r["start"] != start]
            rows.append({"start": start, **keep})
        rows.sort(key=lambda r: r["start"])

    def get_last_statistics(
        self,
        _hass: object,
        _number: int,
        statistic_id: str,
        _convert_units: bool,  # noqa: FBT001
        _types: set,
    ) -> dict:
        """Return the most recent row for a statistic, or empty."""
        rows = self.data.get(statistic_id)
        if not rows:
            return {}
        return {statistic_id: [dict(rows[-1])]}

    def statistics_during_period(
        self,
        _hass: object,
        start: object,
        end: object,
        statistic_ids: set,
        _period: str,
        _units: object,
        _types: set,
    ) -> dict:
        """Return stored rows for the ids within [start, end)."""
        start_ts = _to_epoch(start) if start is not None else None
        end_ts = _to_epoch(end) if end is not None else None
        result: dict[str, list[dict]] = {}
        for statistic_id in statistic_ids:
            rows = [
                dict(r)
                for r in self.data.get(statistic_id, [])
                if (start_ts is None or r["start"] >= start_ts)
                and (end_ts is None or r["start"] < end_ts)
            ]
            if rows:
                result[statistic_id] = rows
        return result

    def clear(self, statistic_ids: list[str]) -> None:
        """Drop the given statistics (recorder async_clear_statistics)."""
        for statistic_id in statistic_ids:
            self.data.pop(statistic_id, None)


@pytest.fixture
def stats_store() -> Generator[StatsStore]:
    """Patch the coordinator's recorder access with a stateful StatsStore."""
    store = StatsStore()

    recorder_instance = Mock()

    async def _passthrough(func, *args, **kwargs):
        return func(*args, **kwargs)

    recorder_instance.async_add_executor_job = AsyncMock(side_effect=_passthrough)
    recorder_instance.async_clear_statistics = Mock(side_effect=store.clear)

    with (
        patch(
            "custom_components.duke_energy.coordinator.get_instance",
            return_value=recorder_instance,
        ),
        patch(
            "custom_components.duke_energy.coordinator.async_add_external_statistics",
            side_effect=store.add_external,
        ) as mock_add,
        patch(
            "custom_components.duke_energy.coordinator.get_last_statistics",
            side_effect=store.get_last_statistics,
        ),
        patch(
            "custom_components.duke_energy.coordinator.statistics_during_period",
            side_effect=store.statistics_during_period,
        ),
    ):
        store.mock_add = mock_add
        yield store


# The current bill cycle starts the day after the most recent invoice's
# billEndDate (invoices are returned most recent first).
INVOICES_PAYLOAD = [
    {"billEndDate": "2026-06-15"},
    {"billEndDate": "2026-05-14"},
]

MONTHLY_USAGE_PAYLOAD = {
    "lastPeriod": {
        "averageTemp": "68",
        "bill": 185.50,
        "days": "30",
        "totalUsage": 900.00,
    },
    "lastYearPeriod": {
        "averageTemp": "75",
        "bill": 310.00,
        "days": "32",
        "totalUsage": 1500.00,
    },
    "thisPeriod": {
        "averageTemp": "72",
        "bill": None,
        "days": "0",
        "totalUsage": 40.00,
    },
}


@pytest.fixture
def mock_api() -> Generator[AsyncMock]:
    """Mock the DukeEnergy client and the OAuth2 session plumbing.

    Patches:
    - ``custom_components.duke_energy.DukeEnergy`` — the class instantiated in
      ``async_setup_entry``; the coordinator receives the already-built instance,
      so only one patch site is needed.
    - ``OAuth2Session.async_ensure_token_valid`` — avoids real token refresh.
    - ``async_get_config_entry_implementation`` — returns a dummy implementation
      so HA doesn't try to look up a registered OAuth2 provider.
    """
    with (
        patch(
            "custom_components.duke_energy.DukeEnergy",
            autospec=True,
        ) as mock_cls,
        patch(
            "custom_components.duke_energy.DukeEnergyAuth",
            autospec=True,
        ),
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid",
            return_value=None,
        ),
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            return_value=AsyncMock(),
        ),
    ):
        mock = mock_cls.return_value
        mock.get_meters = AsyncMock(return_value={})
        mock.get_energy_usage = AsyncMock(return_value={"data": {}, "missing": []})
        mock.get_monthly_usage = AsyncMock(return_value=MONTHLY_USAGE_PAYLOAD)
        mock.get_invoices = AsyncMock(return_value=INVOICES_PAYLOAD)
        yield mock


@pytest.fixture
def mock_api_with_meters(mock_api: AsyncMock) -> AsyncMock:
    """Extend mock_api with a single electric meter and one reading."""
    mock_api.get_meters.return_value = {
        "123": {
            "serialNum": "123",
            "serviceType": "ELECTRIC",
            "agreementActiveDate": "2000-01-01",
            "account": {"accountNumber": "acct-1", "srcAcctId": "src-1"},
        },
    }
    mock_api.get_energy_usage.return_value = {
        "data": {
            dt_util.now(): {
                "energy": 1.3,
                "temperature": 70,
            }
        },
        "missing": [],
    }
    return mock_api


@pytest.fixture
def mock_api_with_gas_meter(mock_api: AsyncMock) -> AsyncMock:
    """Extend mock_api with a single gas meter and one reading."""
    mock_api.get_meters.return_value = {
        "456": {
            "serialNum": "456",
            "serviceType": "GAS",
            "agreementActiveDate": "2000-01-01",
            "account": {"accountNumber": "acct-2", "srcAcctId": "src-2"},
        },
    }
    mock_api.get_energy_usage.return_value = {
        "data": {dt_util.now(): {"energy": 2.5, "temperature": 68}},
        "missing": [],
    }
    return mock_api
