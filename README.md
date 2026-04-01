# Duke Energy Custom Integration

A custom component to sync historical electricity usage for Duke Energy customers.

## Crash0verride Edition
This is a fork of [hunterjm](https://github.com/hunterjm/ha-dukeenergy)'s work with my own improvements: gas readings, and average temperature readings. Soon, polling timing improvements. I will open pull requests to implement features upstream as previous ones are merged.

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


