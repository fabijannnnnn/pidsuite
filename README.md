# Local PID Departure Board

A small Python standard-library web server and browser display. The server makes read-only requests to the fixed Golemio `/v2/pid/departureboards` endpoint. The API token stays in the server's `.env` file and is never included in HTML, JavaScript, or browser requests.

## Configure

Python 3 is required; no third-party Python packages are used.

1. Copy `src/.env.example` to `src/.env`.
2. Edit `src/.env` and set `GOLEMIO_API_TOKEN`, `STOP_START`, and optionally `STOP_DESTINATION`.
   - `STOP_START` is one GTFS stop ID; it selects the departure board returned by Golemio.
   - `STOP_DESTINATION` is optional text matched against the departure destination/headsign. For example, `STOP_DESTINATION=Belárie`. Matching ignores case and Czech accents and checks for the text within the headsign.
   - Optionally set `TRAM_NUMBERS=3,17` to show only departures whose displayed route short name exactly matches one of those values. Leave it empty to show all routes. This is a number filter, not a transport-mode check, so a bus or other route with the same displayed number could also match if returned for that stop.
   - When `STOP_DESTINATION` and `TRAM_NUMBERS` are both set, a departure must match both. `STOP_START` is not a journey-planner origin/destination pair: it asks Golemio for departures at that one stop, and the destination filter uses the departure headsign text.
3. Restrict access to the token file:
   ```sh
   chmod 600 src/.env
   ```

Do not put the token into the HTML page, JavaScript, a URL, or a shared source repository. `.env` is excluded by `.gitignore`.

## Run on a computer

From this directory:

```sh
python3 src/server.py
```

Open `http://127.0.0.1:8000` on the host. Other devices on the same Wi-Fi can use `http://<host-LAN-IP>:8000`. Find the host's local IP in its network settings. Stop the server with Ctrl+C.

## Run on a Samsung Android phone

1. Install Termux from a trusted source (the F-Droid or Termux GitHub distribution; do not mix Termux and add-on sources).
2. In Termux, install Python with `pkg update && pkg install python`.
3. Put this project in Termux's private home directory (for example, `~/pidsuite`), not a publicly shared folder. Create/edit `src/.env` there and run `chmod 600 src/.env`.
4. From `~/pidsuite/src`, start it with `python server.py` (or from `~/pidsuite` with `python src/server.py`).
5. Find the phone's Wi-Fi IP in Android network settings and open `http://<phone-LAN-IP>:8000` on another device connected to the same network.
6. Keep Termux in the foreground or configure Android battery settings to allow it to run. Android may stop background processes to save power; this affects availability.

If port 8000 is occupied, set another `PORT` in `.env` and use that port in the browser address. The server binds to `0.0.0.0` for LAN access; use it only on a trusted private network. The LAN page uses HTTP, so other devices on that network can see the departure display, but the Golemio token is only sent over HTTPS from the phone to Golemio.

## Behavior and security

- The page polls the local server every 60 seconds. The server caches successful Golemio responses for 60 seconds, so multiple display devices share the same upstream request.
- Requests go only to the hard-coded Golemio HTTPS endpoint. Redirects are not followed, to avoid forwarding the token elsewhere.
- The API request times out after 12 seconds. If a refresh fails, the last successful result is served as stale data; before the first success, the local API returns an error without exposing upstream details.
- Only the local read-only board and health-check paths are served. No arbitrary upstream URL or browser-supplied API query is accepted.
- Keep the phone and other display devices on a trusted Wi-Fi network. This simple server does not provide LAN user authentication or TLS for the local page.

## Checks

`http://127.0.0.1:8000/healthz` returns `ok` when the server is running. The API endpoint is `http://127.0.0.1:8000/api/departures` and never returns the configured token.
