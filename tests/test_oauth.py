"""Tests for the Duke Energy OAuth2 implementation."""

from __future__ import annotations

import time

import jwt
import pytest

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import (
    LocalOAuth2ImplementationWithPkce,
)

from custom_components.duke_energy.oauth import DukeEnergyOAuth2Implementation

from .conftest import _make_id_token


def _auth0_token(id_token: str | None) -> dict:
    """Build an Auth0 token response whose access_token outlives the id_token."""
    token = {
        "access_token": "test-access-token",
        "refresh_token": "test-refresh-token",
        "token_type": "Bearer",
        "expires_in": 86400,
        "expires_at": time.time() + 86400,
    }
    if id_token is not None:
        token["id_token"] = id_token
    return token


def _assert_expires_with_id_token(token: dict, id_token: str) -> None:
    """Assert the token expiry was moved to the id_token's exp claim."""
    exp = jwt.decode(id_token, options={"verify_signature": False})["exp"]
    assert token["expires_at"] == float(exp)
    assert token["expires_in"] == pytest.approx(1800, abs=5)


async def test_refresh_token_expires_with_id_token(hass: HomeAssistant) -> None:
    """A refreshed token expires with its id_token, not the longer access_token.

    The id_token is what gets exchanged for a Duke Energy API token, so HA
    must refresh before it lapses.
    """
    id_token = _make_id_token(expires_in=1800)
    implementation = DukeEnergyOAuth2Implementation(hass)

    with patch.object(
        LocalOAuth2ImplementationWithPkce,
        "async_refresh_token",
        new_callable=AsyncMock,
        return_value=_auth0_token(id_token),
    ):
        token = await implementation.async_refresh_token(_auth0_token(None))

    _assert_expires_with_id_token(token, id_token)


async def test_initial_token_expires_with_id_token(hass: HomeAssistant) -> None:
    """The token from the sign-in code exchange gets the same adjustment."""
    id_token = _make_id_token(expires_in=1800)
    implementation = DukeEnergyOAuth2Implementation(hass)

    with patch.object(
        LocalOAuth2ImplementationWithPkce,
        "async_resolve_external_data",
        new_callable=AsyncMock,
        return_value=_auth0_token(id_token),
    ):
        token = await implementation.async_resolve_external_data({"code": "abc"})

    _assert_expires_with_id_token(token, id_token)


@pytest.mark.parametrize(
    "id_token",
    [
        None,
        "not-a-jwt",
        jwt.encode({"sub": "test-user-id"}, "secret", algorithm="HS256"),
    ],
    ids=["missing", "undecodable", "no-exp"],
)
async def test_unusable_id_token_raises(
    hass: HomeAssistant, id_token: str | None
) -> None:
    """An id_token that cannot set the expiry raises ValueError (setup → reauth)."""
    implementation = DukeEnergyOAuth2Implementation(hass)

    with (
        patch.object(
            LocalOAuth2ImplementationWithPkce,
            "async_refresh_token",
            new_callable=AsyncMock,
            return_value=_auth0_token(id_token),
        ),
        pytest.raises(ValueError, match="id_token"),
    ):
        await implementation.async_refresh_token(_auth0_token(None))
