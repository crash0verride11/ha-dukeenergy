"""The Duke Energy integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiodukeenergy_co import DukeEnergy
from homeassistant.components.recorder import (
    get_instance,  # pyright: ignore[reportPrivateImportUsage]
)
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow

from .api import DukeEnergyAuth
from .const import DOMAIN, PLATFORMS
from .coordinator import DukeEnergyConfigEntry, DukeEnergyCoordinator
from .oauth import DukeEnergyOAuth2Implementation

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: DukeEnergyConfigEntry) -> bool:
    """Set up Duke Energy from a config entry."""
    # Register our OAuth implementation
    config_entry_oauth2_flow.async_register_implementation(
        hass,
        DOMAIN,
        DukeEnergyOAuth2Implementation(hass),
    )

    # Check if this is an old entry that needs reauth
    if not entry.data.get("token"):
        msg = "Authentication method has changed. Please reauthenticate."
        raise ConfigEntryAuthFailed(msg)

    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    try:
        await session.async_ensure_token_valid()
    except Exception as err:
        raise ConfigEntryAuthFailed from err

    auth = DukeEnergyAuth(aiohttp_client.async_get_clientsession(hass), session)
    client = DukeEnergy(auth)

    coordinator = DukeEnergyCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_migrate_entry(
    hass: HomeAssistant, entry: DukeEnergyConfigEntry
) -> bool:
    """Cannot migrate without reauth."""
    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry, data={}, minor_version=1, version=2
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DukeEnergyConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant, _entry: DukeEnergyConfigEntry
) -> None:
    """
    Clear this integration's external statistics when it is removed.

    Statistics persist across reloads and restarts (see the coordinator's
    _on_unload); they are only removed here, when the user deletes the entry.
    """
    instance = get_instance(hass)
    all_stats = await instance.async_add_executor_job(list_statistic_ids, hass)
    stat_ids = [
        stat["statistic_id"] for stat in all_stats if stat.get("source") == DOMAIN
    ]
    if stat_ids:
        instance.async_clear_statistics(stat_ids)
