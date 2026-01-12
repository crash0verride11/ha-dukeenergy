# Duke Energy Custom Integration

A custom component to sync historical electricity usage for Duke Energy customers.

## Why?

In November 2025, Duke Energy migrated their API authentication to use Auth0 which broke the existing core integration. In order to get around this, we needed to build a custom chrome extension that captured the OAuth callback from the mobile app flow to restore functionality. Because of the extensive and limited configuration options, it was decided that this integration would be better served as a custom integration than to try and put it back in core.

## Install

> [!IMPORTANT]
> All steps below must be performed in the Google Chrome browser on a desktop.
> The chrome extension is required to successfully authenticate with Duke Energy. Do not skip this step!

1. Download the latest chrome extension from the aiodukeenergy release page [here](https://github.com/hunterjm/aiodukeenergy/releases/latest/download/chrome-extension.zip).
2. Extract the folder.
3. In Google Chrome, visit [chrome://extensions/](chrome://extensions/).
4. Enable `Developer mode` in the top right.
5. Click `Load unpacked` and select the extracted extension.
6. Add [this repository](https://my.home-assistant.io/redirect/hacs_repository/?owner=hunterjm&repository=ha-dukeenergy&category=integration) to HACS and install.
7. Restart Home Assistant
8. If you already had the core integration installed, it should prompt you to re-authenticate. Otherwise, add the integration from Devices and Services.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hunterjm&repository=ha-dukeenergy&category=integration)