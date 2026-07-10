"""Config flow for Duke Energy integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

import jwt
import voluptuous as vol
from homeassistant.components.recorder import (
    get_instance,  # pyright: ignore[reportPrivateImportUsage]
)
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

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


# Expected price unit per meter service type, surfaced in the form so users
# enter (or pick a sensor for) a rate that multiplies cleanly against usage.
_EXPECTED_UNIT = {"ELECTRIC": "$/kWh", "GAS": "$/CCF"}

# Menu sentinel that finishes the flow instead of routing to a meter.
_SAVE = "save"


class DukeEnergyOptionsFlow(OptionsFlow):
    """
    Handle per-meter cost tracking options.

    A menu lists each supported meter; picking one opens a sub-step where its
    cost source is set to ``sensor`` (price from a sensor's statistics/state),
    ``static`` (a fixed rate), or ``off`` (no cost tracking). Choosing "Save"
    writes the per-meter configuration to ``cost_meters`` and, if requested,
    backfills historical cost.
    """

    config_entry: DukeEnergyConfigEntry
    _meters: dict[str, dict[str, Any]]
    _cost_meters: dict[str, dict[str, Any]]
    _current_serial: str

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the meter menu and route to the chosen meter or to Save."""
        if not hasattr(self, "_meters"):
            coordinator = self.config_entry.runtime_data
            meters = await coordinator.api.get_meters()
            self._meters = {
                serial: meter
                for serial, meter in meters.items()
                if meter.get("serviceType") in _SUPPORTED_METER_TYPES
            }
            if not self._meters:
                return self.async_abort(reason="no_meters")
            # Working copy so per-meter edits accumulate before a single save.
            self._cost_meters = {
                serial: dict(config)
                for serial, config in self.config_entry.options.get(
                    "cost_meters", {}
                ).items()
            }

        if user_input is not None:
            choice = user_input["meter"]
            if choice == _SAVE:
                return await self.async_step_save()
            self._current_serial = choice
            return await self.async_step_meter()

        options = [
            SelectOptionDict(value=serial, label=self._meter_menu_label(serial, meter))
            for serial, meter in self._meters.items()
        ]
        options.append(SelectOptionDict(value=_SAVE, label="Save and apply"))
        schema = vol.Schema(
            {
                vol.Required("meter", default=_SAVE): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    def _meter_menu_label(self, serial: str, meter: dict[str, Any]) -> str:
        """Return a menu label summarizing a meter's current cost source."""
        service = str(meter["serviceType"]).capitalize()
        config = self._cost_meters.get(serial, {})
        mode = config.get("mode", "off")
        fixed: str | None = None
        if mode == "sensor":
            detail = config.get("entity_id", "?")
            fixed = config.get("fixed_cost_entity_id")
        elif mode == "static":
            unit = _EXPECTED_UNIT.get(meter["serviceType"], "")
            detail = f"{config.get('price')} {unit}".strip()
            if config.get("fixed_monthly_cost"):
                fixed = f"{config['fixed_monthly_cost']}/month"
        else:
            detail = "Off"
        if fixed:
            detail = f"{detail} + {fixed}"
        return f"{service} meter {serial}: {detail}"

    async def _validate_meter_input(  # noqa: PLR0911
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """
        Validate one meter's submitted cost source.

        Returns the config to store, or None plus a translation key for the
        error to show. The mode picks both the per-unit rate and the fixed
        monthly cost from the same source, so the two never mix. A blank or
        zero fixed cost stores no key at all, leaving the meter priced exactly
        as it was before fixed costs existed.
        """
        mode = user_input["mode"]
        if mode == "sensor":
            entity_id = user_input.get("entity_id")
            fixed_cost_entity_id = user_input.get("fixed_cost_entity_id")
            if not entity_id:
                return None, "entity_required"
            if not await self._entity_usable_for_price(entity_id):
                return None, "no_price_data"
            if fixed_cost_entity_id and not await self._entity_usable_for_price(
                fixed_cost_entity_id
            ):
                return None, "no_fixed_cost_data"
            config: dict[str, Any] = {"mode": "sensor", "entity_id": entity_id}
            if fixed_cost_entity_id:
                config["fixed_cost_entity_id"] = fixed_cost_entity_id
            return config, None

        if mode == "static":
            price = user_input.get("price")
            fixed_monthly_cost = user_input.get("fixed_monthly_cost")
            if price is None:
                return None, "price_required"
            if price <= 0:
                return None, "invalid_price"
            # Negatives are already rejected by the selector's min=0.
            config = {"mode": "static", "price": price}
            if fixed_monthly_cost:
                config["fixed_monthly_cost"] = fixed_monthly_cost
            return config, None

        return {"mode": "off"}, None

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the cost source (sensor / static / off) for one meter."""
        serial = self._current_serial
        meter = self._meters[serial]
        service_type = str(meter["serviceType"])
        errors: dict[str, str] = {}

        if user_input is not None:
            config, error = await self._validate_meter_input(user_input)
            if config is not None:
                self._cost_meters[serial] = config
                return await self.async_step_init()
            errors["base"] = cast("str", error)

        defaults = (
            user_input if user_input is not None else self._cost_meters.get(serial, {})
        )
        currency = self.hass.config.currency or "USD"
        schema = vol.Schema(
            {
                vol.Required(
                    "mode", default=defaults.get("mode", "off")
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["off", "sensor", "static"],
                        translation_key="cost_mode",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(
                    "entity_id",
                    description={"suggested_value": defaults.get("entity_id")},
                ): EntitySelector(
                    EntitySelectorConfig(domain=["sensor", "input_number", "number"])
                ),
                vol.Optional(
                    "price",
                    description={"suggested_value": defaults.get("price")},
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        step="any",
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement=_EXPECTED_UNIT.get(service_type, ""),
                    )
                ),
                vol.Optional(
                    "fixed_cost_entity_id",
                    description={
                        "suggested_value": defaults.get("fixed_cost_entity_id")
                    },
                ): EntitySelector(
                    EntitySelectorConfig(domain=["sensor", "input_number", "number"])
                ),
                vol.Optional(
                    "fixed_monthly_cost",
                    description={"suggested_value": defaults.get("fixed_monthly_cost")},
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        step="any",
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement=f"{currency}/month",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="meter",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "meter": f"{service_type.capitalize()} meter {serial}",
                "unit": _EXPECTED_UNIT.get(service_type, ""),
            },
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Persist the per-meter configuration and optionally backfill."""
        if user_input is not None:
            backfill = user_input.get("backfill_cost", False)
            # Only reload when backfill is requested; otherwise the coordinator
            # picks up the new configuration at its next scheduled poll. A reload
            # (not async_request_refresh) is required because a fresh coordinator
            # resets _last_successful_date, bypassing the "already retrieved
            # today" guard that would otherwise defer the backfill.
            if backfill:
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
            return self.async_create_entry(
                data={"cost_meters": self._cost_meters, "backfill_cost": backfill}
            )

        # Backfill always defaults to off: it is a one-shot action the coordinator
        # resets after populating history, so it should never persist as enabled.
        schema = vol.Schema(
            {vol.Optional("backfill_cost", default=False): BooleanSelector()}
        )
        return self.async_show_form(step_id="save", data_schema=schema)

    async def _entity_usable_for_price(self, entity_id: str) -> bool:
        """
        Return whether the entity can price usage.

        Preferred: long-term statistics (per-interval historical price). Failing
        that, a numeric current state is enough — the coordinator falls back to
        it as a flat rate for ongoing usage.
        """
        result = await get_instance(self.hass).async_add_executor_job(
            list_statistic_ids, self.hass, {entity_id}
        )
        if result:
            return True
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        try:
            float(state.state)
        except (ValueError, TypeError):
            return False
        return True
