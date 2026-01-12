"""Constants for the Duke Energy integration."""

DOMAIN = "duke_energy"

# Auth0 OAuth2 configuration for Duke Energy
OAUTH2_AUTHORIZE = "https://login.duke-energy.com/authorize"
OAUTH2_TOKEN = "https://login.duke-energy.com/oauth/token"
OAUTH2_CLIENT_ID = "uB67shrSSodJrNKTvZ0cWyAE9VRqkFwI"

# Scopes required for Duke Energy API access
OAUTH2_SCOPES = ["openid", "profile", "email", "offline_access"]

# Auth0 client identifier (base64 encoded client info for mobile app)
AUTH0_CLIENT = "eyJuYW1lIjoiQXV0aDAuQW5kcm9pZCIsImVudiI6eyJhbmRyb2lkIjoiMzUifSwidmVyc2lvbiI6IjMuOC4wIn0="

# Mobile app redirect URI - required by Duke Energy Auth0 config
MOBILE_REDIRECT_URI = (
    "cma-prod://login.duke-energy.com/android/"
    "com.dukeenergy.customerapp.release/callback"
)
