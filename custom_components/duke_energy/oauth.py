"""OAuth2 implementation for Duke Energy."""

import secrets

from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import (
    LocalOAuth2ImplementationWithPkce,
)

from .const import (
    AUTH0_CLIENT,
    MOBILE_REDIRECT_URI,
    OAUTH2_AUTHORIZE,
    OAUTH2_CLIENT_ID,
    OAUTH2_SCOPES,
    OAUTH2_TOKEN,
)


class DukeEnergyOAuth2Implementation(LocalOAuth2ImplementationWithPkce):
    """Duke Energy OAuth2 implementation using mobile app credentials."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the Duke Energy OAuth2 implementation."""
        super().__init__(
            hass,
            domain="duke_energy",
            client_id=OAUTH2_CLIENT_ID,
            client_secret="",  # PKCE flow doesn't need a client secret
            authorize_url=OAUTH2_AUTHORIZE,
            token_url=OAUTH2_TOKEN,
        )

    @property
    def name(self) -> str:
        """Return the name of the implementation."""
        return "Duke Energy"

    @property
    def redirect_uri(self) -> str:
        """Return the redirect URI for Duke Energy mobile app OAuth flow."""
        return MOBILE_REDIRECT_URI

    @property
    def extra_authorize_data(self) -> dict:
        """Extra data for the authorize request."""
        data = {
            "scope": " ".join(OAUTH2_SCOPES),
            "auth0Client": AUTH0_CLIENT,
            "nonce": secrets.token_urlsafe(32),
        }
        data.update(super().extra_authorize_data)
        return data
