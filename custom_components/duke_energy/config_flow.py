"""Config flow for Duke Energy integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import jwt
import voluptuous as vol
from homeassistant.components.recorder import (
    get_instance,  # pyright: ignore[reportPrivateImportUsage]
)
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import DOMAIN
from .coordinator import _SUPPORTED_METER_TYPES, DukeEnergyConfigEntry
from .oauth import DukeEnergyOAuth2Implementation

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)


class DukeEnergyOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """Handle a config flow for Duke Energy."""

    VERSION = 2
    MINOR_VERSION = 1

    DOMAIN = DOMAIN

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    @staticmethod
    @callback
    def async_get_options_flow(
        _config_entry: DukeEnergyConfigEntry,
    ) -> DukeEnergyOptionsFlow:
        """Return the options flow for per-meter cost tracking."""
        return DukeEnergyOptionsFlow()

    async def async_step_pick_implementation(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle picking implementation - directly use our implementation."""
        self.flow_impl = DukeEnergyOAuth2Implementation(self.hass)
        return await self.async_step_auth()

    async def async_step_reauth(self, _: Mapping[str, Any]) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth dialog."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Create an entry for the flow."""
        # Extract user info from id_token
        try:
            id_token = data["token"]["id_token"]
            token_data = jwt.decode(id_token, options={"verify_signature": False})
            user_id = token_data.get("internal_identifier", "").lower()
            email = token_data.get("email", "").lower()
        except (KeyError, ValueError):
            _LOGGER.exception("Failed to decode ID token")
            return self.async_abort(reason="oauth_error")

        if not user_id:
            _LOGGER.error("No internal_identifier in ID token claims")
            return self.async_abort(reason="oauth_error")

        await self.async_set_unique_id(user_id)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates=data,
            )
        self._abort_if_unique_id_configured()

        return self.async_create_entry(title=email or user_id, data=data)


# Expected price-sensor unit per meter service type, surfaced in the form so
# users pick a sensor whose value multiplies cleanly against consumption.
_EXPECTED_UNIT = {"ELECTRIC": "$/kWh", "GAS": "$/CCF"}


class DukeEnergyOptionsFlow(OptionsFlow):
    """
    Handle per-meter cost tracking options.

    Each supported meter can be paired with a price sensor. Cost statistics
    are derived at poll time from that sensor's long-term statistics, so the
    chosen entity must have statistics (``state_class: measurement``). Leaving
    a meter blank skips cost tracking for it.
    """

    config_entry: DukeEnergyConfigEntry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the price sensor selected for each meter."""
        coordinator = self.config_entry.runtime_data
        meters = await coordinator.api.get_meters()
        supported = {
            serial: meter
            for serial, meter in meters.items()
            if meter.get("serviceType") in _SUPPORTED_METER_TYPES
        }
        if not supported:
            return self.async_abort(reason="no_meters")

        errors: dict[str, str] = {}
        if user_input is not None:
            selected = {
                serial: user_input[serial]
                for serial in supported
                if user_input.get(serial)
            }
            missing = [
                entity_id
                for entity_id in selected.values()
                if not await self._entity_has_statistics(entity_id)
            ]
            if missing:
                errors["base"] = "no_statistics"
            else:
                # Reload so the coordinator picks up the new mapping immediately
                # rather than at the next scheduled poll. Only when it changed —
                # a reload triggers a full data refresh.
                if selected != self.config_entry.options.get("cost_entities", {}):
                    self.hass.config_entries.async_schedule_reload(
                        self.config_entry.entry_id
                    )
                return self.async_create_entry(data={"cost_entities": selected})

        prefill = (
            user_input
            if user_input is not None
            else self.config_entry.options.get("cost_entities", {})
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    serial,
                    description={"suggested_value": prefill.get(serial)},
                ): EntitySelector(EntitySelectorConfig(domain="sensor"))
                for serial in supported
            }
        )
        meter_lines = "\n".join(
            f"- {meter['serviceType'].capitalize()} meter {serial} "
            f"— expects {_EXPECTED_UNIT.get(meter['serviceType'], '')}"
            for serial, meter in supported.items()
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"meters": meter_lines},
        )

    async def _entity_has_statistics(self, entity_id: str) -> bool:
        """Return whether the entity has long-term statistics recorded."""
        result = await get_instance(self.hass).async_add_executor_job(
            list_statistic_ids, self.hass, {entity_id}
        )
        return bool(result)
