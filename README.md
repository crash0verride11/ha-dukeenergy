# Duke Energy Custom Integration

A custom component to sync historical electricity usage for Duke Energy customers.

## Crash0verride Edition
This is a fork of [hunterjm](https://github.com/hunterjm/ha-dukeenergy)'s work with my own improvements, in order of addition: 
- Gas meter readings
- Average temperature readings — 1 per duke account, as meters at the same location have the same temperature reading.
- Polling timing improvements — up to three semi-random polls over a narrower window, see FAQ
- Cost statistics — configure a price and the integration calculates a running total
- Account and meter entities:
  - Account entities: When the integration last polled (`Last updated`), `Cost last bill cycle`, `Cost this time last year`
  - Meter entities: When each meter last received new data (`Last changed`), `Usage last bill cycle`, `Usage this bill cycle`,  `Usage this time last year`

## Why?

In November 2025, Duke Energy migrated their API authentication to use Auth0 which broke the existing core integration. In order to get around this, we needed to build a custom chrome extension that captured the OAuth callback from the mobile app flow to restore functionality. Because of the extensive and limited configuration options, it was decided that this integration would be better served as a custom integration than to try and put it back in core.

## Install
> [!IMPORTANT]
> A browser extension is required to successfully authenticate with Duke Energy. Do not skip this step!

### Chrome Extension
> The chrome extension is tied to the folder location on your computer

1. Download the latest chrome extension from the aiodukeenergy release page [here](https://github.com/crash0verride11/ha-dukeenergy/releases/latest/download/chrome-extension.zip).
2. Extract the folder from zip if downloaded.
3. In Google Chrome, visit [chrome://extensions/](chrome://extensions/). In Edge, visit [edge://extensions/](edge://extensions/).
4. Enable `Developer mode` in the top right.
5. Click `Load unpacked` and select the extracted extension.
6. Add [this repository](https://my.home-assistant.io/redirect/hacs_repository/?owner=crash0verride11&repository=ha-dukeenergy&category=integration) to HACS and install.
7. Restart Home Assistant
8. If you already had the core integration installed, it should prompt you to re-authenticate. Otherwise, add the integration from Devices and Services.

### Safari Extension
>Safari support using the built-in xcode conversion method. Requires a free developer account.

1. Download the latest safari extension from the release page [here](https://github.com/crash0verride11/ha-dukeenergy/releases/latest/download/safari-extension.zip).
2. Open the project
3. Set your free developer account as the Team in both Targets
4. Build
5. Enable the extension in Safari settings/
6. Build the extension again.
7. Enable access to all or specific URL in the extension settings.
8. Try to authenticate.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=crash0verride11&repository=ha-dukeenergy&category=integration)


## Configuration

### Per meter cost statistics

For external statistics, such as those the integration makes, home assistant only allows adding a running total of costs to the energy dashboard. I've added a configuration window, available after setup, to select an entity tracking price or static price that the integration can use to calculate a running total cost statistic for you.

#### Setup

- On the integration page click the gear icon to open the configuration window
- Select each meter you want to track and select a price source
- Select `Save and apply`
- Backfilling historical data
    - Enabled will calculate a running cost for all available usage, based on either the price at the time and earlier price available, or the set static price
    - Disabled will only use the newly set price for new usage running cost calculations

## FAQ

### I successfully added the integration, where is my data?

The integration downloads three years of data from duke and home assistant needs time to calculate statistics based on your data. Wait a while and try again.

### I don't see any usage or cost entities?

Only external statistics are created. Since we only know historical data, these could never have a known current state.

### When is new data retrieved?

#### 09:00, 14:00, and 19:00
* +/- a random interval up to 2 hours
  * The integration SHA-256 hashes "{entry_id}:{YYYY-MM-DD}", takes the first 8 hex chars as an integer, maps it into [-2hr, +2hr] in milliseconds, and stores it as a timedelta.
* Stops polling after data retrieved successfully for all meters
* consistent timing across restarts, different for every user, different every day
* consistent offsets across intervals