"""Config flow for Duke Energy integration."""

from __future__ import annotations

import logging
from math import isfinite
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
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from yarl import URL

from .const import DOMAIN
from .coordinator import _SUPPORTED_METER_TYPES, DukeEnergyConfigEntry
from .oauth import DukeEnergyOAuth2Implementation

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)

CONF_CALLBACK_URL = "callback_url"

# Translation keys under `config.error` in strings.json.
ERROR_AUTHORIZE_REJECTED = "authorize_rejected"
ERROR_INVALID_CALLBACK_URL = "invalid_callback_url"
ERROR_STATE_MISMATCH = "state_mismatch"

STEP_AUTH_MANUAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CALLBACK_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        )
    }
)


class InvalidCallbackURLError(Exception):
    """Raised when the pasted callback URL cannot be used."""

    def __init__(self, error_key: str) -> None:
        """Initialize with the translation key describing the problem."""
        super().__init__(error_key)
        self.error_key = error_key


class DukeEnergyOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """
    Handle a config flow for Duke Energy.

    Sign-in starts at a menu with two paths:

    - ``auth``: the stock external step, where the browser extension forwards
      the callback to HA's OAuth callback view, which resumes the flow;
    - ``auth_manual``: the user opens the authorize URL themselves and pastes
      back the callback address Duke redirects to.

    They must be separate steps chosen up front: an external step can only
    transition to another external step, so it cannot fall back to a form. Nor
    can the paste form also accept the forwarded callback, because only
    external steps notify the frontend when the flow advances.
    """

    VERSION = 2
    MINOR_VERSION = 1

    DOMAIN = DOMAIN

    def __init__(self) -> None:
        """Initialize the Duke Energy config flow."""
        super().__init__()
        self._authorize_url: str | None = None
        self._expected_state: str | None = None

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
        # Create the implementation once per flow: it holds the PKCE verifier
        # that the token exchange must send, so replacing it after an authorize
        # URL was issued would fail the exchange with invalid_grant.
        if self.flow_impl is None:
            self.flow_impl = DukeEnergyOAuth2Implementation(self.hass)
        return await self.async_step_pick_method()

    async def async_step_pick_method(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the browser extension or the manual paste sign-in."""
        return self.async_show_menu(
            step_id="pick_method",
            menu_options=["auth", "auth_manual"],
        )

    async def async_step_auth_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the authorize URL and accept the pasted callback URL."""
        if self._authorize_url is None:
            self._authorize_url = await self.async_generate_authorize_url()
            self._expected_state = URL(self._authorize_url).query.get("state")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self.external_data = self._parse_callback_url(
                    user_input[CONF_CALLBACK_URL]
                )
            except InvalidCallbackURLError as err:
                errors["base"] = err.error_key
            else:
                return await self.async_step_creation()

        return self.async_show_form(
            step_id="auth_manual",
            data_schema=STEP_AUTH_MANUAL_SCHEMA,
            description_placeholders={"authorize_url": self._authorize_url},
            errors=errors,
        )

    def _parse_callback_url(self, raw_url: str) -> dict[str, Any]:
        """
        Turn the pasted callback URL into OAuth2 external data.

        Returns a mapping shaped like the one the OAuth callback view hands the
        flow: the authorization code plus the decoded state.

        Raises:
            InvalidCallbackURLError: If the URL is unusable, carries an
                authorization error, lacks a code, or does not match the state
                issued for this flow.

        """
        try:
            query = URL(raw_url.strip()).query
        except ValueError as err:
            raise InvalidCallbackURLError(ERROR_INVALID_CALLBACK_URL) from err

        if "error" in query:
            raise InvalidCallbackURLError(ERROR_AUTHORIZE_REJECTED)

        if not (code := query.get("code")):
            raise InvalidCallbackURLError(ERROR_INVALID_CALLBACK_URL)

        if not self._expected_state or query.get("state") != self._expected_state:
            raise InvalidCallbackURLError(ERROR_STATE_MISMATCH)

        return {
            "code": code,
            "state": {
                "flow_id": self.flow_id,
                "redirect_uri": self.flow_impl.redirect_uri,
            },
        }

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
        if mode == "sensor":
            detail = config.get("entity_id", "?")
        elif mode == "static":
            unit = _EXPECTED_UNIT.get(meter["serviceType"], "")
            detail = f"{config.get('price')} {unit}".strip()
        else:
            detail = "Off"
        return f"{service} meter {serial}: {detail}"

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the cost source (sensor / static / off) for one meter."""
        serial = self._current_serial
        meter = self._meters[serial]
        service_type = str(meter["serviceType"])
        errors: dict[str, str] = {}

        if user_input is not None:
            mode = user_input["mode"]
            entity_id = user_input.get("entity_id")
            price = user_input.get("price")
            if mode == "sensor":
                if not entity_id:
                    errors["base"] = "entity_required"
                elif not await self._entity_usable_for_price(entity_id):
                    errors["base"] = "no_price_data"
                else:
                    self._cost_meters[serial] = {
                        "mode": "sensor",
                        "entity_id": entity_id,
                    }
                    return await self.async_step_init()
            elif mode == "static":
                if price is None:
                    errors["base"] = "price_required"
                elif not isfinite(price) or price <= 0:
                    errors["base"] = "invalid_price"
                else:
                    self._cost_meters[serial] = {"mode": "static", "price": price}
                    return await self.async_step_init()
            else:
                self._cost_meters[serial] = {"mode": "off"}
                return await self.async_step_init()

        defaults = (
            user_input if user_input is not None else self._cost_meters.get(serial, {})
        )
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
            price = float(state.state)
        except (ValueError, TypeError):
            return False
        return isfinite(price)
