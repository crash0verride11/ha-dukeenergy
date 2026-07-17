"""Constants for the Duke Energy integration."""

from homeassistant.const import Platform

DOMAIN = "duke_energy"

PLATFORMS = [Platform.SENSOR]

# Cost modes under which a meter's cost is tracked. The sensor platform
# (carrier entity creation) and the coordinator (cost calculation) both gate
# on this tuple: the cost statistic is imported under the carrier's entity
# id, so the two must stay in lockstep.
COST_ENABLED_MODES = ("sensor", "static")


# These schemes are permanent: a carrier's unique id pins its initial entity
# id, and the entity id IS the statistic id the imported history is keyed to.
# Changing a scheme would orphan every existing statistic series.
def consumption_unique_id(service_type: str, serial_number: str) -> str:
    """Return the unique id for a meter's consumption statistics carrier."""
    return f"duke_{service_type.lower()}_{serial_number}_energy_consumption"


def cost_unique_id(service_type: str, serial_number: str) -> str:
    """Return the unique id for a meter's cost statistics carrier."""
    return f"duke_{service_type.lower()}_{serial_number}_total_cost"


def temperature_unique_id(src_acct_id: str) -> str:
    """Return the unique id for an account's temperature statistics carrier."""
    return f"duke_account_{src_acct_id}_temperature"


# Auth0 OAuth2 configuration for Duke Energy
OAUTH2_AUTHORIZE = "https://login.duke-energy.com/authorize"
OAUTH2_TOKEN = "https://login.duke-energy.com/oauth/token"  # noqa: S105
OAUTH2_CLIENT_ID = "PitoKqxMh8thrFF8rRlYGrAs3LbSD2dj"

# Scopes required for Duke Energy API access
OAUTH2_SCOPES = ["openid", "profile", "email", "offline_access"]

# Auth0 client identifier (base64 encoded client info for mobile app)
AUTH0_CLIENT = "eyJuYW1lIjoiQXV0aDAuc3dpZnQiLCJlbnYiOnsiaU9TIjoiMjYuMiIsInN3aWZ0IjoiNi54In0sInZlcnNpb24iOiIyLjEzLjAifQ"  # noqa: E501

# Mobile app redirect URI - required by Duke Energy Auth0 config
MOBILE_REDIRECT_URI = "https://login.duke-energy.com/ios/com.duke-energy.app/callback"
