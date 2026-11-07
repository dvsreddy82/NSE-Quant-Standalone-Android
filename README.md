# NSE Quant Pro — standalone Android app

Everything the NSE Quant Terminal desktop app does, on the phone, **without a Mac**.

| | Companion app (`NSE-Quant-Terminal-Android`) | **This app (standalone)** |
|---|---|---|
| Data and analysis | computed by the Mac app, fetched over Wi-Fi / Tailscale | computed **on the phone** |
| Shoonya | the Mac's session | the phone's own session (market hours) |
| Storage | the Mac's MongoDB | SQLite in the app's private storage |
| Size | ~10 MB | ~90 MB (Python, numpy, pandas inside) |

Both can be installed side by side (different app ids). **Shoonya allows one live session per login**: while this
phone is connected, the Mac app's live feed drops, and the other way round — run one of them live at a time.

## How it works
The analysis engines are the desktop app's own Python modules, copied unchanged into
`app/src/main/python` by `tools/sync_engines.sh` and run on the phone by
[Chaquopy](https://chaquo.com/chaquopy/) (Python 3.11, numpy 1.26, pandas 2.1):
option chain and option-selling analysis, writer risk monitor, book risk (Greeks, stress test,
expiry odds, account limits), FII/DII positioning and the evening setup, BANKNIFTY constituents
and weights, market news, global markets, level-break alerts and the 15-minute direction calls.

`engine.py` replaces the desktop app's Qt shell: one background loop on the same schedule
(ticker 1 s · NSE snapshot 15 s when Shoonya isn't streaming · chains 5 min · risk 3 min ·
trades 30 s while connected · BANKNIFTY 5 min · news 2/10 min · global 5 min · levels every
5-min close · direction calls 9:30–3:15 every 15 min · FII/DII 4:30–11 pm · EOD after 6:30 pm).
`phone_api.py` answers the UI with the same JSON the companion app fetched from the Mac
(`api_routes.py`, shared with the desktop app), so the screens are the companion app's.

Data sources: Shoonya (read-only, 9:00 am – 3:40 pm only), NSE (snapshot, option chain, FII/DII,
participant OI, index archive), Yahoo (candles, global markets), RSS (news).

## Screens
Home (direction calls, alerts newest first, news, positions, indices) · BANKNIFTY monitor ·
Option chain · **Writer risk** (threat matrix, alerts, prohibitions, safe zones, leg guidance,
your book and account limits) · FII/DII · Global markets · Settings (Shoonya sign-in, status-bar
icon, engine activity). Plus the BANKNIFTY home-screen widget and the Market monitor (live price
in the status bar / lock screen and notifications for every alert).

## Set up on the phone
1. Install `dist/NSE-Quant-Pro-1.0.0-debug.apk` (allow "install unknown apps"), or
   `adb install -r dist/NSE-Quant-Pro-1.0.0-debug.apk`.
2. Open **NSE Quant Pro → Settings** (gear). Enter your Shoonya **User ID**, **Client ID** and
   **Secret code** (trade.shoonya.com → Profile → API Key) and tap **Save**. The redirect URL
   registered there must be `http://127.0.0.1:8765/shoonya/callback` (the same as the Mac app).
3. In market hours tap **Sign in to Shoonya**: Shoonya's page opens in the browser; sign in with
   your password and TOTP there. Shoonya redirects to `127.0.0.1:8765`, which this app answers,
   and it connects. If the browser shows an error page instead, paste its address into the box.
   Today's session is reused automatically for the rest of the day.
4. On Home turn on **Market monitor** (keeps the engine running in the background) and allow
   notifications; in battery settings let the app run in the background.

The first start downloads about two years of daily history and today's data (a minute or two);
everything is then kept in the phone's SQLite database for offline use.

## Build
JDK 17, Android SDK 35, and a **Python 3.11** on the build machine (Chaquopy compiles and installs
the Python packages with it; default `/opt/homebrew/bin/python3.11`, override with
`CHAQUOPY_BUILD_PYTHON`).
```bash
tools/sync_engines.sh            # copy the latest engines from the desktop repo
./gradlew assembleDebug          # → app/build/outputs/apk/debug/app-debug.apk
```

Read-only: the app never places orders. Analytics, not investment advice.
