"""Test the Duke Energy config flow."""

from __future__ import annotations

import base64
import json
import time
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

# Importing the flow handler registers it in config_entries.HANDLERS["duke_energy"].
from custom_components.duke_energy.config_flow import DukeEnergyOAuth2FlowHandler  # noqa: F401
from custom_components.duke_energy.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _make_id_token(claims: dict[str, Any] | None = None) -> str:
    """Build a minimal unsigned JWT."""
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    default_claims = {
        "internal_identifier": "test-user-id",
        "email": "test@example.com",
        "exp": int(time.time()) + 3600,
    }
    if claims:
        default_claims.update(claims)
    payload = (
        base64.urlsafe_b64encode(json.dumps(default_claims).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.fakesig"


def _make_oauth_data(id_token: str | None = None) -> dict[str, Any]:
    """Build a token dict as HA OAuth2 flow would deliver it."""
    return {
        "auth_implementation": DOMAIN,
        "token": {
            "access_token": "test-access-token",
            "id_token": id_token or _make_id_token(),
            "refresh_token": "test-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "expires_at": time.time() + 3600,
        },
    }


def _make_flow(
    hass: HomeAssistant, source: str = config_entries.SOURCE_USER
) -> DukeEnergyOAuth2FlowHandler:
    """Instantiate a flow handler with hass, handler, and context pre-filled.

    The flow manager normally sets ``flow.handler = domain`` when creating a
    flow via ``async_init``.  We set it manually here so that helper methods
    like ``_abort_if_unique_id_configured`` can look up existing entries.
    """
    flow = DukeEnergyOAuth2FlowHandler()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {"source": source}
    return flow


# ---------------------------------------------------------------------------
# Business-logic tests for async_oauth_create_entry
# These call the method directly to test JWT parsing and abort conditions
# without needing to drive the full OAuth redirect machinery.
# ---------------------------------------------------------------------------


async def test_oauth_create_entry(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """async_oauth_create_entry extracts user_id/email from id_token and creates entry."""
    flow = _make_flow(hass)
    result = await flow.async_oauth_create_entry(_make_oauth_data())

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "test@example.com"
    # Unique ID is internal_identifier, not email — drives deduplication logic.
    assert flow.unique_id == "test-user-id"


async def test_oauth_create_entry_aborts_on_missing_id_token(
    hass: HomeAssistant,
) -> None:
    """Missing id_token results in an oauth_error abort."""
    data = _make_oauth_data()
    del data["token"]["id_token"]

    result = await _make_flow(hass).async_oauth_create_entry(data)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "oauth_error"


async def test_oauth_create_entry_aborts_on_missing_user_id(
    hass: HomeAssistant,
) -> None:
    """id_token without internal_identifier results in oauth_error abort."""
    id_token = _make_id_token({"internal_identifier": ""})
    result = await _make_flow(hass).async_oauth_create_entry(_make_oauth_data(id_token))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "oauth_error"


# ---------------------------------------------------------------------------
# Flow-manager tests — drive the full async_init / async_configure path so
# deduplication, result-dict shape, and the reauth success path are covered.
# ---------------------------------------------------------------------------


async def test_abort_if_already_configured(
    recorder_mock: object,
    enable_custom_integrations: object,
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Second setup for the same account aborts as already_configured."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id",
        title="test@example.com",
        version=2,
        minor_version=1,
        data=_make_oauth_data(),
    )
    existing.add_to_hass(hass)

    with patch(
        "custom_components.duke_energy.oauth.DukeEnergyOAuth2Implementation.async_resolve_external_data",
        return_value=_make_oauth_data()["token"],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.EXTERNAL_STEP

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "auth-code", "state": "state"}
        )
        assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE

        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_success(
    recorder_mock: object,
    enable_custom_integrations: object,
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
) -> None:
    """Reauth with matching account updates the entry and aborts with reauth_successful."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id",
        title="test@example.com",
        version=2,
        minor_version=1,
        data=_make_oauth_data(),
    )
    existing.add_to_hass(hass)

    with patch(
        "custom_components.duke_energy.oauth.DukeEnergyOAuth2Implementation.async_resolve_external_data",
        return_value=_make_oauth_data()["token"],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": existing.entry_id,
            },
            data=existing.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        # Submit the confirm form — advances to pick_implementation → auth step.
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.EXTERNAL_STEP

        # Simulate the OAuth redirect callback.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "auth-code", "state": "state"}
        )
        assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE

        # Final step: resolve token → async_oauth_create_entry → update entry.
        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


# ---------------------------------------------------------------------------
# Options flow — per-meter cost-entity selection.
# ---------------------------------------------------------------------------


@contextmanager
def _patch_price_validation(*, has_lts: bool):
    """Patch the options flow's LTS lookup used by _entity_usable_for_price."""
    recorder = Mock()

    async def _passthrough(func, *args, **kwargs):
        return func(*args, **kwargs)

    recorder.async_add_executor_job = AsyncMock(side_effect=_passthrough)
    with (
        patch(
            "custom_components.duke_energy.config_flow.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.duke_energy.config_flow.list_statistic_ids",
            return_value=[{"statistic_id": "x"}] if has_lts else [],
        ),
    ):
        yield


def _entry_with_meters(hass: HomeAssistant, meters: dict) -> MockConfigEntry:
    """Add an entry whose runtime coordinator reports the given meters."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user-id",
        title="test@example.com",
        version=2,
        minor_version=1,
        data=_make_oauth_data(),
    )
    entry.add_to_hass(hass)
    coordinator = Mock()
    coordinator.api.get_meters = AsyncMock(return_value=meters)
    entry.runtime_data = coordinator
    return entry


_ELECTRIC_METER = {
    "123": {"serialNum": "123", "serviceType": "ELECTRIC", "account": {}},
}


async def test_options_flow_saves_selection(
    recorder_mock: object,
    enable_custom_integrations: object,
    hass: HomeAssistant,
) -> None:
    """Selecting a price sensor with LTS stores it in cost_entities."""
    entry = _entry_with_meters(hass, _ELECTRIC_METER)

    with _patch_price_validation(has_lts=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"123": "sensor.price", "backfill_cost": False},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cost_entities"] == {"123": "sensor.price"}
    assert entry.options["backfill_cost"] is False


async def test_options_flow_blank_skips_meter(
    recorder_mock: object,
    enable_custom_integrations: object,
    hass: HomeAssistant,
) -> None:
    """Leaving a meter blank records no cost entity for it."""
    entry = _entry_with_meters(hass, _ELECTRIC_METER)

    with _patch_price_validation(has_lts=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"backfill_cost": False}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cost_entities"] == {}


async def test_options_flow_rejects_unusable_entity(
    recorder_mock: object,
    enable_custom_integrations: object,
    hass: HomeAssistant,
) -> None:
    """A sensor with neither LTS nor a numeric state is rejected."""
    entry = _entry_with_meters(hass, _ELECTRIC_METER)

    with _patch_price_validation(has_lts=False):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        # sensor.price has no state and no LTS.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"123": "sensor.price", "backfill_cost": False}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_price_data"}


async def test_options_flow_state_only_entity_allowed(
    recorder_mock: object,
    enable_custom_integrations: object,
    hass: HomeAssistant,
) -> None:
    """A sensor with no LTS but a numeric current state is accepted."""
    entry = _entry_with_meters(hass, _ELECTRIC_METER)
    hass.states.async_set("sensor.price", "0.12")

    with _patch_price_validation(has_lts=False):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"123": "sensor.price", "backfill_cost": False}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cost_entities"] == {"123": "sensor.price"}


async def test_options_flow_backfill_schedules_reload(
    recorder_mock: object,
    enable_custom_integrations: object,
    hass: HomeAssistant,
) -> None:
    """Enabling backfill schedules a reload; leaving it off does not."""
    entry = _entry_with_meters(hass, _ELECTRIC_METER)

    with (
        _patch_price_validation(has_lts=True),
        patch.object(hass.config_entries, "async_schedule_reload") as mock_reload,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"123": "sensor.price", "backfill_cost": True},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["backfill_cost"] is True
    mock_reload.assert_called_once_with(entry.entry_id)
