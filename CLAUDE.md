# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A custom Home Assistant integration for Duke Energy. It uses the `aiodukeenergy-co` library (published to PyPI, import name `aiodukeenergy_co`) for all API communication. It is declared in `manifest.json` `requirements` (so HA installs it) and pinned in `pyproject.toml`'s dev group for tests — keep the two on the same version. The library's dev source lives at `../aiodukeenergy` (sibling of this repo).

The repo follows the standard GitHub/HACS layout: the integration lives under `custom_components/duke_energy/`. Filenames referenced below (`coordinator.py`, `oauth.py`, `api.py`, etc.) are all relative to `custom_components/duke_energy/`. `docs/` is gitignored: it holds local-only scratch material (example scripts, CSVs, handoff notes) that is not in a fresh clone.

## Architecture

### Two-layer auth

Duke Energy requires **two token exchanges** before any API call:

1. **Auth0 → HA OAuth session** (`oauth.py` + `config_flow.py`): Uses `LocalOAuth2ImplementationWithPkce` with Duke Energy's Auth0 tenant (`login.duke-energy.com`), posing as the Duke mobile app (its client ID and iOS callback URL). The `id_token` expires in 30 minutes (much sooner than the `access_token`), so `oauth.py:_adjust_token_expiry` overrides the token's `expires_at` to match the `id_token`'s `exp` claim, ensuring HA refreshes before the id_token expires. A missing or unreadable id_token raises `ValueError`, which setup turns into reauth.

2. **Auth0 id_token → Duke Energy API token** (`aiodukeenergy_co/duke_auth.py:AbstractDukeEnergyAuth._exchange_for_duke_token`): Every API request to `api-v2.cma.duke-energy.app` requires a separate Duke Energy bearer token obtained by POSTing the Auth0 id_token to `/login/auth-token`. This token is cached in `AbstractDukeEnergyAuth`.

The HA-specific bridge is `api.py:DukeEnergyAuth`, which subclasses `AbstractDukeEnergyAuth` and implements `async_get_id_token()` by delegating to the HA `OAuth2Session`.

### Sign-in flow

Because the redirect goes to the mobile app's callback URL, HA can't receive it directly. `config_flow.py` opens with a menu (`pick_method`):
- **Browser extension** (`auth`): the Chrome extension (source in `../aiodukeenergy/chrome-extension/`) catches the callback and hands it back to HA automatically.
- **Manual paste** (`auth_manual`): the user opens the authorize URL, signs in, and pastes the resulting callback URL. `_parse_callback_url` checks it for an authorization error, a missing code, and a state that doesn't match the one this flow issued.

Reauth offers the same two paths.

### Setup

`__init__.py:async_setup_entry` validates the token, then blocks only on `DukeEnergyCoordinator.async_discover_meters()` (one `get_meters()` call) so the sensor platform can create devices. The first refresh — potentially a 3-year backfill — runs as a background task, and the account **Status** sensor reports its progress. Token-refresh errors: a 4xx (other than 408/429) or an unusable id_token is an auth failure (reauth); a 5xx, 408, 429 or network error is `ConfigEntryNotReady` (retry).

### Data flow

`coordinator.py:DukeEnergyCoordinator` is the heart of the integration. It:
- Uses `DataUpdateCoordinator` but with `update_interval=None` — scheduling is manual (`_schedule_next_check`).
- Polls at 9am, 2pm, and 7pm ET (all in `America/New_York`, see `_DUKE_TZ`), with a per-user ±2-hour random offset derived from the config entry ID + date via SHA-256 to spread load. All three slots in a day share the same offset. A poll counts as up to date only when every supported meter returned a reading for yesterday; then the next poll is tomorrow's first slot, otherwise it retries at the next slot. Later same-day polls skip Duke ("already retrieved today") but still price any unpriced usage.
- Tracks an update phase in `status` (`const.STATUS_*`): fetching → recording (statistics queued but not yet committed by the recorder) → complete, or failed.
- Writes **external statistics** directly into the HA recorder. The statistics have no backing entities (a carrier-entity redesign was built and abandoned — branch `entities-unknown`, kept for reference only).
- Statistic IDs follow the patterns `duke_energy:{type}_{serial}_energy_consumption`, `duke_energy:{type}_{serial}_energy_cost`, and `duke_energy:account_{srcAcctId}_temperature`.
- Separately, `sensor.py` creates one **account device** (Status, Last Duke poll, Next Duke poll, cost last bill cycle / last year, bill balance / due date / status) and one **meter device** per supported meter, linked via `via_device` (Last meter change, usage this bill cycle / last bill cycle / last year). These are ordinary state sensors fed by `get_monthly_usage()`, `get_billing_payment_info()` and the MONTHLY billing-cycle graph, unrelated to the statistics.
- Statistics persist across unload/reload/restart. `__init__.py:async_remove_entry` clears all `source == DOMAIN` statistics only when the **last** Duke Energy entry is removed: statistic IDs don't record which login wrote them, so another remaining login's history is kept.

### Cost statistics

Configured per meter in the options flow (`config_flow.py:DukeEnergyOptionsFlow`, stored in `entry.options["cost_meters"]`), in one of three modes:
- **sensor**: a price entity. Prices come from its long-term statistics (hourly means, carried forward across gaps, never backward in time); with no statistics, its current numeric state is used. A state that isn't a finite number prices nothing.
- **static**: a fixed price per unit.
- **off**: no cost statistic.

`_async_update_costs()` runs after the meter loop and prices the **stored** consumption statistics, not the usage a poll just fetched, so usage left unpriced (e.g. while the price entity was unavailable) is caught up once a price exists. A new or re-enabled flat-rate series (static, or an entity without price history) starts at the account's current bill-cycle start, capped at 30 days before the meter's newest consumption; an entity with price history fills back as far as that history goes. Meters switched from off back on are tracked in `options["cost_reenabled"]`. Saving the options applies costs immediately (`async_update_costs`); the optional backfill reprices all stored consumption (`async_rebuild_costs`) behind a progress step. Cost writes are serialized by `_cost_lock`. Cost stats are has_sum=True, `StatisticMeanType.NONE`, in the HA currency.

### Diagnostics

`diagnostics.py` reports the entry (data and options) and the coordinator state: status, poll times, account count, each meter's service type and last update, and statistic counts by type. Tokens, the title, the unique ID and price entity IDs are redacted. Meter serials are replaced by `meter_N` labels, used consistently in both options and coordinator. Statistic IDs and account numbers are left out, because they embed identifiers.

### aiodukeenergy-co library

External PyPI dependency (import name `aiodukeenergy_co`; dev source at `../aiodukeenergy`). Key modules:
- `dukeenergy.py` — `DukeEnergy` API client: `get_accounts()`, `get_meters()`, `get_energy_usage()`, `get_monthly_usage()`, `get_billing_payment_info()`, `get_invoices()`. Accounts and meters are cached in-memory; pass `fresh=True` to bypass.
- `duke_auth.py` — Abstract/concrete auth classes. The `AbstractDukeEnergyAuth.request()` method auto-injects the DE bearer token.
- `exceptions.py` — `DukeEnergyAuthError` (rejected token → reauth) and `DukeEnergyBlockedError` (CDN/WAF refusal before reaching Duke's API → failed poll, retried at the next slot, no reauth). The coordinator maps them in `_translate_auth_errors`.
- `auth0.py` — Standalone Auth0 PKCE client for non-HA contexts.

### Statistic details

- Electric meters: hourly intervals, `UnitOfEnergy.KILO_WATT_HOUR`, `StatisticMeanType.NONE`, has_sum=True.
- Gas meters: daily intervals, `UnitOfVolume.CENTUM_CUBIC_FEET`, `StatisticMeanType.NONE`, has_sum=True.
- Temperature: daily (noon), `UnitOfTemperature.FAHRENHEIT`, `StatisticMeanType.ARITHMETIC`, has_sum=False. One temperature stat per account (not per meter), registered only for the first meter seen for each `accountNumber`.
- Daily consumption stats are registered at noon (start + 12h) to better represent when usage occurred.
- Incremental polls refetch from 30 days before the last stored row. The oldest returned reading's stored row is the baseline: its sum is kept and every later reading is rewritten on top of it, so Duke's corrections replace stored values without restarting the sum. If no stored row overlaps what Duke returns, the sum resumes from the last stored row.

## Development

### Running in HA

Copy or symlink `custom_components/duke_energy/` into your HA config's `custom_components/` directory and restart.

### Linting

```bash
uvx ruff check custom_components/
uvx ruff format custom_components/ tests/
```

`.ruff.toml` selects `ALL`, so a ruff upgrade can surface newly stabilized rules. `_async_update_data` carries `# noqa: PLR0912, PLR0915` for its branch and statement counts.

### Testing

Tests live in `tests/` and use `pytest-homeassistant-custom-component` for HA fixtures (`hass`, `recorder_mock`, `MockConfigEntry`, etc.).

```bash
uv run pytest tests/
```

Or without a project venv (ephemeral):

```bash
PYTHONPATH=. uvx --with "pytest-homeassistant-custom-component>=0.13.0" --with "freezegun>=1.5" --with "PyJWT>=2.8" --with "aiodukeenergy-co==1.3.2" pytest tests/
```

Key fixtures (`tests/conftest.py`):
- `stats_store` — an in-memory stand-in for the recorder's statistics tables. It records inserts and answers `get_last_statistics` / `statistics_during_period`, so tests exercise the real read-modify-write flow; `seed()` preloads rows. Prefer it over `mock_recorder`, which only patches the recorder calls.
- `mock_api`, `mock_api_with_meters` (electric `123`, account `src-1`), `mock_api_with_gas_meter` (gas `456`, account `src-2`) — the mocked `DukeEnergy` client. MONTHLY `get_energy_usage` calls are always answered with billing cycles; override the HOURLY/DAILY data via `return_value` or a per-serial `side_effect`.

Key mocking notes:
- `aiodukeenergy_co.DukeEnergy` is instantiated in `__init__.py:async_setup_entry` — mock it at `custom_components.duke_energy.DukeEnergy`.
- `OAuth2Session.async_ensure_token_valid` must be patched to avoid real token refresh in tests.
- The `mock_config_entry` fixture does **not** call `add_to_hass` — tests do this themselves.
- `auto_enable_custom_integrations` is not autouse, so tests that use `recorder_mock` can have the recorder set up before `hass`. Tests that load the integration must request it (or `enable_custom_integrations`) explicitly.

## Key constraints

- All service areas are `America/New_York`. If Duke Energy ever expands, timezone detection by service address would be needed.
- Duke Energy hourly data is available up to ~3 years back; `_async_get_energy_usage` fetches in 30-day windows walking backward.
- During pagination, a 4xx response (other than 408/429) is treated as "no more data" (normal termination). A 5xx, 408, 429, timeout or network error fails the poll with `UpdateFailed`, and nothing is written for that meter, so a partial backfill is never recorded as complete.
- Config entry version is 2. `async_migrate_entry` moves version 1 entries to version 2 with their data cleared, so setup finds no token and starts reauth.
