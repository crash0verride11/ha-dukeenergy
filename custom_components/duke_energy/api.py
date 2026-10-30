"""API for Duke Energy bound to Home Assistant OAuth."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from aiodukeenergy_co import AbstractDukeEnergyAuth, DukeEnergyAuthError
from aiohttp import ClientResponseError

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
        # async_setup_entry makes the same distinction for the initial refresh.
        # Here it covers refreshes during polling, where only a
        # DukeEnergyAuthError reaches the coordinator as a reauth trigger; a
        # busy endpoint (408/429) or an outage propagates as a failed poll.
        try:
            await self._oauth_session.async_ensure_token_valid()
        except ClientResponseError as err:
            if (
                HTTPStatus.BAD_REQUEST <= err.status < HTTPStatus.INTERNAL_SERVER_ERROR
                and err.status
                not in (HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS)
            ):
                msg = "OAuth refresh was rejected"
                raise DukeEnergyAuthError(msg) from err
            raise
        except ValueError as err:
            # oauth.py:_adjust_token_expiry — id_token missing or undecodable.
            msg = "OAuth refresh returned an unusable ID token"
            raise DukeEnergyAuthError(msg) from err
        return self._oauth_session.token["id_token"]
