# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A custom Home Assistant integration for Duke Energy. It uses the `aiodukeenergy-co` library (published to PyPI, import name `aiodukeenergy_co`) for all API communication. It is declared in `manifest.json` `requirements` (so HA installs it) and pinned in `pyproject.toml`'s dev group for tests. The library's dev source lives at `../aiodukeenergy` (sibling of this repo).

The repo follows the standard GitHub/HACS layout: the integration lives under `custom_components/duke_energy/`. Standalone example scripts live in `docs/`. Filenames referenced below (`coordinator.py`, `oauth.py`, `api.py`, etc.) are all relative to `custom_components/duke_energy/`.

## Architecture

### Two-layer auth

Duke Energy requires **two token exchanges** before any API call:

1. **Auth0 → HA OAuth session** (`oauth.py` + `config_flow.py`): Uses `LocalOAuth2ImplementationWithPkce` with Duke Energy's Auth0 tenant (`login.duke-energy.com`). The `id_token` expires in 30 minutes (much sooner than the `access_token`), so `oauth.py:_adjust_token_expiry` overrides the token's `expires_at` to match the `id_token`'s `exp` claim, ensuring HA refreshes before the id_token expires.

2. **Auth0 id_token → Duke Energy API token** (`aiodukeenergy_co/duke_auth.py:AbstractDukeEnergyAuth._exchange_for_duke_token`): Every API request to `api-v2.cma.duke-energy.app` requires a separate Duke Energy bearer token obtained by POSTing the Auth0 id_token to `/login/auth-token`. This token is cached in `AbstractDukeEnergyAuth`.

The HA-specific bridge is `api.py:DukeEnergyAuth`, which subclasses `AbstractDukeEnergyAuth` and implements `async_get_id_token()` by delegating to the HA `OAuth2Session`.

### Data flow

`coordinator.py:DukeEnergyCoordinator` is the heart of the integration. It:
- Uses `DataUpdateCoordinator` but with `update_interval=None` — scheduling is manual.
- Polls at 9am, 2pm, and 7pm ET (all in `America/New_York`, see `_DUKE_TZ`), with a per-user ±2-hour random offset derived from the config entry ID + date via SHA-256 to spread load.
- Writes **external statistics** directly into the HA recorder. The statistics have no backing entities (a carrier-entity redesign was built and abandoned — branch `entities-unknown`, kept for reference only).
- Statistic IDs follow the pattern `duke_energy:{type}_{serial}_energy_consumption` and `duke_energy:account_{srcAcctId}_temperature`.
- Separately, `sensor.py` creates one **account device** (Last updated, bill-cycle cost, and billing/payment sensors) and one **meter device** per supported meter via_device (Last changed, bill-cycle usage sensors), fed by `get_monthly_usage()` / `get_billing_payment_info()` — these are ordinary state sensors, unrelated to the statistics.
- Statistics persist across unload/reload/restart; they are cleared from the recorder only when the config entry is removed (`__init__.py:async_remove_entry`, which clears all `source == DOMAIN` statistics).

### aiodukeenergy-co library

External PyPI dependency (import name `aiodukeenergy_co`; dev source at `../aiodukeenergy`). Key modules:
- `dukeenergy.py` — `DukeEnergy` API client: `get_accounts()`, `get_meters()`, `get_energy_usage()`. Accounts and meters are cached in-memory; pass `fresh=True` to bypass.
- `duke_auth.py` — Abstract/concrete auth classes. The `AbstractDukeEnergyAuth.request()` method auto-injects the DE bearer token.
- `auth0.py` — Standalone Auth0 PKCE client (used for non-HA contexts like `docs/electric_example.py`/`docs/gas_example.py`).

### Statistic details

- Electric meters: hourly intervals, `UnitOfEnergy.KILO_WATT_HOUR`, `StatisticMeanType.NONE`, has_sum=True.
- Gas meters: daily intervals, `UnitOfVolume.CENTUM_CUBIC_FEET`, `StatisticMeanType.NONE`, has_sum=True.
- Temperature: daily (noon), `UnitOfTemperature.FAHRENHEIT`, `StatisticMeanType.ARITHMETIC`, has_sum=False. One temperature stat per account (not per meter), registered only for the first meter seen for each `accountNumber`.
- Daily consumption stats are registered at noon (start + 12h) to better represent when usage occurred.

## Development

### Running in HA

Copy or symlink `custom_components/duke_energy/` into your HA config's `custom_components/` directory and restart.

### Linting

```bash
uvx ruff check custom_components/
uvx ruff format custom_components/
```

### Testing

Tests live in `tests/` and use `pytest-homeassistant-custom-component` for HA fixtures (`hass`, `recorder_mock`, `MockConfigEntry`, etc.).

```bash
uv run pytest tests/
```

Or without a project venv (ephemeral):

```bash
PYTHONPATH=. uvx --with "pytest-homeassistant-custom-component>=0.13.0" --with "freezegun>=1.5" --with "PyJWT>=2.8" --with "aiodukeenergy-co==1.2.0" pytest tests/
```

Key mocking notes:
- `aiodukeenergy_co.DukeEnergy` is instantiated in `__init__.py:async_setup_entry` — mock it at `custom_components.duke_energy.DukeEnergy`.
- `OAuth2Session.async_ensure_token_valid` must be patched to avoid real token refresh in tests.
- The `mock_config_entry` fixture does **not** call `add_to_hass` — tests do this themselves.

## Key constraints

- All service areas are `America/New_York`. If Duke Energy ever expands, timezone detection by service address would be needed.
- Duke Energy hourly data is available up to ~3 years back; `_async_get_energy_usage` fetches in 30-day windows walking backward.
- A `ClientError` from the API during pagination is treated as "no more data" (normal termination), not an error.
- Config entry version is 2 (version 1 entries trigger reauth via `async_migrate_entry`).
