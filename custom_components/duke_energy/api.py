"""API for Duke Energy bound to Home Assistant OAuth."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .aiodukeenergy import AbstractDukeEnergyAuth

if TYPE_CHECKING:
    import aiohttp
    from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

_LOGGER = logging.getLogger(__name__)


class DukeEnergyAuth(AbstractDukeEnergyAuth):
    """Provide Duke Energy authentication tied to an OAuth2 based config entry."""

    def __init__(
        self,
        websession: aiohttp.ClientSession,
        oauth_session: OAuth2Session,
    ) -> None:
        """Initialize Duke Energy auth."""
        super().__init__(websession)
        self._oauth_session = oauth_session

        # If id_token provided, decode to populate email/internal_user_id
        if self._oauth_session.token["id_token"]:
            self._update_user_info_from_token(self._oauth_session.token["id_token"])

    async def async_get_id_token(self) -> str:
        """Return a valid ID token."""
        await self._oauth_session.async_ensure_token_valid()
        return self._oauth_session.token["id_token"]
