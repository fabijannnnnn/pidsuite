#!/usr/bin/env python3
"""Small LAN departure board. Run with: python3 server.py"""

from __future__ import annotations

import json
import os
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
API_URL = "https://api.golemio.cz/v2/pid/departureboards"
CACHE_SECONDS = 60
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
API_TIMEOUT_SECONDS = 12


def load_local_env() -> None:
    """Read a simple .env file without adding a dependency; environment wins."""
    env_file = BASE_DIR / ".env"
    if not env_file.is_file():
        return
    try:
        if os.name == "posix" and env_file.stat().st_mode & 0o077:
            print(
                "Warning: .env is readable by group/others; run: chmod 600 .env",
                file=sys.stderr,
            )
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key.replace("_", "").isalnum():
                os.environ.setdefault(key, value)
    except OSError:
        print("Warning: could not read .env", file=sys.stderr)


load_local_env()


def parse_clock_setting(name: str, default: str) -> int:
    value = os.environ.get(name, default).strip()
    try:
        hour_text, minute_text = value.split(":", maxsplit=1)
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as exc:
        raise SystemExit(f"{name} must use HH:MM format") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise SystemExit(f"{name} must be a valid 24-hour time")
    return hour * 60 + minute


FETCH_START_MINUTE = parse_clock_setting("FETCH_START", "06:00")
FETCH_END_MINUTE = parse_clock_setting("FETCH_END", "18:00")
if FETCH_START_MINUTE >= FETCH_END_MINUTE:
    raise SystemExit("FETCH_START must be earlier than FETCH_END on the same day")


def fetch_window_is_open() -> bool:
    local_now = datetime.now().astimezone()
    minute_of_day = local_now.hour * 60 + local_now.minute
    return FETCH_START_MINUTE <= minute_of_day < FETCH_END_MINUTE


def configured_identifiers() -> list[tuple[str, str]]:
    stop_start = os.environ.get("STOP_START", "").strip()
    if stop_start:
        if "," in stop_start:
            raise ValueError("STOP_START must contain one GTFS stop ID")
        return [("ids[]", stop_start)]

    # Backward compatibility for existing configurations; STOP_START is preferred.
    stop_type = os.environ.get("STOP_TYPE", "").strip()
    allowed = {"ids", "cisIds", "aswIds", "names"}
    if stop_type not in allowed:
        raise ValueError("Set STOP_START to a GTFS stop ID")
    values = [
        value.strip()
        for value in os.environ.get("STOP_VALUES", "").split(",")
        if value.strip()
    ]
    if not values:
        raise ValueError("STOP_START must contain a GTFS stop ID")
    if len(values) > 100:
        raise ValueError("STOP_VALUES may contain at most 100 stops")
    return [(f"{stop_type}[]", value) for value in values]


def configured_tram_numbers() -> set[str]:
    return {
        value.strip()
        for value in os.environ.get("TRAM_NUMBERS", "").split(",")
        if value.strip()
    }


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        char for char in decomposed if not unicodedata.combining(char)
    ).strip()


def route_number(item: dict) -> str:
    route = item.get("route")
    if isinstance(route, dict):
        value = route.get("short_name") or route.get("shortName")
        if value is not None:
            return str(value).strip()
    value = item.get("route_short_name") or item.get("line")
    return str(value).strip() if value is not None else ""


def destination_name(item: dict) -> str:
    trip = item.get("trip")
    if isinstance(trip, dict) and trip.get("headsign"):
        return str(trip["headsign"]).strip()
    for key in ("trip_headsign", "headsign", "direction", "destination"):
        if item.get(key):
            return str(item[key]).strip()
    return ""


def filter_departure_item(
    item, selected_numbers: set[str], selected_destinations: set[str]
):
    if not isinstance(item, dict):
        return None

    if isinstance(item.get("properties"), dict):
        filtered_properties = filter_departure_item(
            item["properties"], selected_numbers, selected_destinations
        )
        if filtered_properties is None:
            return None
        return {**item, "properties": filtered_properties}

    departures = item.get("departures")
    if isinstance(departures, list):
        filtered = [
            result
            for departure in departures
            if (
                result := filter_departure_item(
                    departure, selected_numbers, selected_destinations
                )
            )
            is not None
        ]
        return {**item, "departures": filtered} if filtered else None

    if selected_numbers and route_number(item) not in selected_numbers:
        return None
    if selected_destinations:
        heading = normalize_text(destination_name(item))
        if not any(destination in heading for destination in selected_destinations):
            return None
    return item


def filter_departure_payload(payload):
    selected_numbers = configured_tram_numbers()
    selected_destinations = {
        normalize_text(value)
        for value in os.environ.get("STOP_DESTINATION", "").split(",")
        if value.strip()
    }
    if not selected_numbers and not selected_destinations:
        return payload

    if isinstance(payload, list):
        return [
            result
            for item in payload
            if (
                result := filter_departure_item(
                    item, selected_numbers, selected_destinations
                )
            )
            is not None
        ]

    if isinstance(payload, dict):
        for key in ("features", "departures", "items", "results"):
            if isinstance(payload.get(key), list):
                return {
                    **payload,
                    key: [
                        result
                        for item in payload[key]
                        if (
                            result := filter_departure_item(
                                item, selected_numbers, selected_destinations
                            )
                        )
                        is not None
                    ],
                }
    return payload


JSONPayload = list[object] | dict[str, object]


class InvalidGolemioResponse(TypeError):
    """Raised when the upstream JSON has an unexpected top-level type."""


cache_lock = threading.Lock()
_cache: JSONPayload | None = None
_cache_time = 0.0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward the API token if the upstream unexpectedly redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_golemio() -> JSONPayload:
    token = os.environ.get("GOLEMIO_API_TOKEN", "").strip()
    if not token:
        raise ValueError("GOLEMIO_API_TOKEN is not configured")
    params = configured_identifiers()
    params.extend(
        [
            ("minutesAfter", "180"),
            ("limit", "1000"),
            ("order", "real"),
            ("preferredTimezone", "Europe/Prague"),
        ]
    )
    url = API_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "X-Access-Token": token,
            "Accept": "application/json",
            "User-Agent": "Local-PID-Departure-Board/1.0",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(
        NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    request_started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    print(
        f"Golemio request started at {request_started_at}", file=sys.stderr, flush=True
    )
    with opener.open(request, timeout=API_TIMEOUT_SECONDS) as response:
        content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise ValueError("Golemio response exceeded the configured size limit")
        payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, (list, dict)):
        raise InvalidGolemioResponse("Unexpected Golemio response")
    return cast(JSONPayload, payload)


def get_departures(force_refresh: bool = False) -> tuple[int, dict]:
    global _cache, _cache_time, _cache_time_wall
    with cache_lock:
        if not fetch_window_is_open():
            return 200, {
                "data": _cache if _cache is not None else [],
                "updatedAt": _cache_time_wall or None,
                "stale": _cache is not None,
                "paused": True,
            }
        now = time.monotonic()
        if (
            not force_refresh
            and _cache is not None
            and now - _cache_time < CACHE_SECONDS
        ):
            return 200, {"data": _cache, "updatedAt": _cache_time_wall, "stale": False}
        try:
            payload = fetch_golemio()
            _cache = payload
            _cache_time = time.monotonic()
            _cache_time_wall = datetime.now().astimezone().isoformat(timespec="seconds")
            print(
                f"Golemio departures refreshed at {_cache_time_wall}",
                file=sys.stderr,
                flush=True,
            )
            return 200, {"data": _cache, "updatedAt": _cache_time_wall, "stale": False}
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            InvalidGolemioResponse,
        ) as exc:
            # Do not send upstream errors, URLs, or configuration values to clients.
            print(f"Golemio refresh failed ({type(exc).__name__})", file=sys.stderr)
            if _cache is not None:
                return 200, {
                    "data": _cache,
                    "updatedAt": _cache_time_wall,
                    "stale": True,
                }
            return 502, {
                "error": "Could not retrieve departures. Check server configuration and connection."
            }


_cache_time_wall = ""


def refresh_departures_periodically(stop_event: threading.Event) -> None:
    get_departures(force_refresh=True)
    while not stop_event.is_set():
        seconds_to_next_minute = 60 - (time.time() % 60)
        if stop_event.wait(seconds_to_next_minute):
            break
        get_departures(force_refresh=True)


class BoardHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(
            error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
        ):
            return
        super().handle_error(request, client_address)


class BoardHandler(BaseHTTPRequestHandler):
    server_version = "LocalDepartureBoard"
    sys_version = ""

    def log_message(self, format, *args):
        # Keep request URLs and any query values out of server logs.
        return

    def send_bytes(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            self.serve_static("index.html", "text/html; charset=utf-8")
        elif path == "/static/style.css":
            self.serve_static("style.css", "text/css; charset=utf-8")
        elif path == "/static/app.js":
            self.serve_static("app.js", "text/javascript; charset=utf-8")
        elif path == "/healthz":
            self.send_bytes(200, b"ok", "text/plain; charset=utf-8")
        elif path == "/api/departures":
            status, payload = get_departures()
            if status == 200:
                payload["data"] = filter_departure_payload(payload["data"])
            self.send_bytes(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
        else:
            self.send_bytes(404, b"Not found", "text/plain; charset=utf-8")

    def serve_static(self, name: str, content_type: str) -> None:
        try:
            content = (STATIC_DIR / name).read_bytes()
        except OSError:
            self.send_bytes(404, b"Not found", "text/plain; charset=utf-8")
            return
        self.send_bytes(200, content, content_type)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("PORT must be an integer from 1 to 65535")
    server = BoardHTTPServer((host, port), BoardHandler)
    server.daemon_threads = True
    refresh_stop = threading.Event()
    refresh_thread = threading.Thread(
        target=refresh_departures_periodically,
        args=(refresh_stop,),
        name="golemio-refresh",
        daemon=True,
    )
    refresh_thread.start()
    print(f"Departure board listening on http://{host}:{port}")
    print("Find this phone's Wi-Fi IP address to open the board from another device.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping departure board.")
    finally:
        refresh_stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
