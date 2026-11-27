# Duke Energy Custom Integration

A custom component to sync historical electricity usage for Duke Energy customers.

## Crash0verride Edition
This is a fork of [hunterjm](https://github.com/hunterjm/ha-dukeenergy)'s work with my own improvements, in order of addition: 
- Gas meter readings
- Average temperature readings — 1 per Duke account, as meters at the same location have the same temperature reading.
- Polling timing improvements — up to three semi-random polls over a narrower window, see FAQ
- Cost statistics — configure a price and the integration calculates a running total, see configuration below
- Account and meter entities:
  - Account entities: Update progress (`Status`), when the integration last polled (`Last updated`) and will next poll (`Next scheduled update`), `Cost last bill cycle`, `Cost this time last year`, `Bill balance`, `Bill due date`, `Bill status`
  - Meter entities: When each meter last received new data (`Last changed`), `Usage last bill cycle`, `Usage this bill cycle`,  `Usage this time last year`
- Authentication: both manual sign-in without an extension, and a new generic extension, HA OAuth Assist, for Chrome and Firefox
- Background first usage history retrieval — devices appear right away and the `Status` entity tracks the download
- Privacy — the entry title is a masked email and logs no longer show your email, address, or sign-in tokens

### AI Transparency
Within the last year I've more heavily used AI to work on this integration. I'm a code-savvy, Python-literate amateur who knows enough to spot when Claude is being a complete idiot, writing some convoluted insanity, or, typically, just making bad judgment calls. I review each line of the code committed and consistently find areas to improve, challenge, or completely re-think. I try to add a human touch to the release notes for changes or features I considered, investigated, tested, captured traffic for, and spent time on. I let AI write some of the more tedious fix notes and backports from other forks, making sure appropriate credit is added. AI is not where I started, but I wouldn't have had the time to get past adding gas support without it.

## Why?

In November 2025, Duke Energy migrated their API authentication to use Auth0 which broke the existing core integration. In order to get around this, we needed to build a custom Chrome extension that captured the OAuth callback from the mobile app flow to restore functionality. Because of the extensive and limited configuration options, it was decided that this integration would be better served as a custom integration than to try and put it back in core.

## Install
> [!IMPORTANT]
> A browser extension is recommended for easy authentication with Duke Energy, but a manual option is now available, instructions below.

### Add the Integration

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=crash0verride11&repository=ha-dukeenergy&category=integration)

### HA OAuth Assist (Chrome and Firefox extensions)

This new extension is effectively the same as the original, but generic, designed for use across multiple authentication platforms. Future extension development will be focused on this one moving forward.

1. Install the extension
  - Chrome / Edge / other Chromium browsers:
    - Install [OAuth Assist for HA](https://chromewebstore.google.com/detail/oauth-assist-for-ha/mdoleppdlojmeeogmmocmjnkemecaofh) from the Chrome Web Store
  - Firefox (store listing is under review; install manually until it's approved):
    - Download the Firefox zip from the [latest release](https://github.com/crash0verride11/ha-oauth-assist/releases/latest/) page and extract it
    - Go to about:debugging → Load Temporary Add-on and select a file in the extracted extension/ folder
    > A manually loaded extension is tied to the folder location on your computer and may 'disappear' if you move the folder.
2. In a new tab, click the extension's icon to open its popup
3. Select `Manage providers...`
4. Download and import the [duke_ha-oauth-assist.json](https://github.com/crash0verride11/ha-dukeenergy/blob/ha-dukeenergy-aio/resources/duke_ha-oauth-assist.json) provider profile
5. Enable the Duke provider profile
6. Start the authentication process in Home Assistant

### Legacy Chrome Extension
The previous Duke Energy-specific extension will continue to work for the time being and is available for [download here](https://github.com/crash0verride11/aiodukeenergy/releases/download/latest/chrome-extension.zip), however I will no longer be maintaining it.

### Safari Extension
>Safari support requires a free developer account. I intend to eventually publish a Safari version of HA OAuth Assist to replace this integration-specific extension.

1. Download the .zip of the [latest release](https://github.com/crash0verride11/aiodukeenergy/releases/latest) from aiodukeenergy-co, uncompress, and find the safari-extension folder.
2. Open the project
3. Set your free developer account as the Team in both Targets
4. Build
5. Enable the extension in Safari settings.
6. Build the extension again.
7. Enable access to all or the specific URL in the extension settings.
8. Try to authenticate.

### Manual Authentication
> On non-iOS devices, manually configuring the browser's user agent is required.

1. Make the integration identify as an iPhone. Duke rejects desktop browsers after authentication:
   - **Firefox:** `about:config` → add a **String** named
     `general.useragent.override` with the iPhone value below. Firefox has no
     per-site override, so reset the pref when you are done.
   - **Safari Desktop:** enable the Develop menu, then
     **Develop → User Agent → Safari — iOS 18 — iPhone**.
   - **Chrome/Edge:** DevTools (`F12`) → device toolbar (`Ctrl+Shift+M`) →
     iPhone.
2. Start the integration (or reauthentication), choose **Guided manual
   sign-in**, and open the sign-in link in that tab. Sign in.
3. Duke lands on a "Not Found" page whose address ends in `?code=…&state=…`.
4. Copy the **entire** address and paste it into **Callback URL**.
5. If the page says `access_denied`, the browser still looked like a desktop;
   repeat from step 1 with a fresh sign-in link.

Keep the setup form open until you submit the URL. Treat that URL as a sign-in credential and keep it out of issue reports and screenshots. You can reset your User-Agent when you are done.

iPhone User-Agent:

```
Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1
```

#### If the sign-in fails

| What you see | Cause | What to do |
| --- | --- | --- |
| `access_denied` / "An error has occurred" instead of a code | Duke's login still saw a desktop browser | Apply the mobile User-Agent, then start a fresh sign-in |
| "Not Found" page with `code` and `state` in the address | This is the expected landing page | Copy the whole address into **Callback URL** |
| No `code` in the address | Sign-in did not complete | Reopen the current setup form's sign-in link with the mobile User-Agent applied |
| "That URL belongs to a different sign-in attempt" | The pasted URL came from a previous sign-in attempt | Open the sign-in link from the current form again and paste the new address |

## Configuration

### Per meter cost statistics

For external statistics, such as those the integration makes, Home Assistant only allows adding a running total of costs to the Energy dashboard. I've added a configuration window, available after setup, to select an entity tracking price or static price that the integration can use to calculate a running total cost statistic for you.

#### Setup

- On the integration page click the gear icon to open the configuration window
- Select each meter you want to track and select a price source
- Select `Save and apply`
- Backfilling historical data
    - Enabling recalculates cost for all stored usage of enabled meters, with a waiting screen, no reload needed. Usage before your price sensor's first recorded price gets the earliest price. A static price (or a `number`/`input_number`) reprices everything at the current rate, replacing any earlier rates
    - Disabled keeps existing costs. New costs are filled in as far back as your price sensor's history goes, or from the start of the current bill cycle (up to 30 days) with a static price
- Turning a meter's cost off keeps its history. Turning it back on fills the gap from a price sensor's history, or picks up at the current bill cycle with a static price
- If your price sensor is unavailable for a while, costs catch up once it's back

## FAQ

### I successfully added the integration, where is my data?

The integration downloads three years of data from Duke and Home Assistant needs time to calculate statistics based on your data. You can check the `Status` entity on any of your account devices to see if the integration is 'Fetching', 'Recording', or if data addition is 'Finished'.

### I don't see any usage or cost entities?

Only external statistics are created with a name such as `duke_energy:gas_333333_energy_consumption`. Since we only know historical data, we are not able to set a known current state. While it is possible to create entities with permanent `unknown` states this does not allow cost calculation (which is calculated from a current state) nor are they usable by additional dashboard card types.

### When is new data retrieved?

#### 09:00, 14:00, and 19:00
* +/- a random interval up to 2 hours
  * The integration SHA-256 hashes "{entry_id}:{YYYY-MM-DD}", takes the first 8 hex chars as an integer, maps it into [-2hr, +2hr] in milliseconds, and stores it as a timedelta.
* Stops polling after data retrieved successfully for all meters
* Consistent timing across restarts, different for every user, different every day
* Consistent offsets across intervals
