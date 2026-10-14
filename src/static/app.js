"use strict";

const list = document.getElementById("departures");
const updated = document.getElementById("updated");
const message = document.getElementById("message");
let lastDepartures = [];
let lastUpdatedAt = null;
let lastWasStale = false;

function valueAt(object, path) {
  return path.split(".").reduce((value, key) => value == null ? undefined : value[key], object);
}

function firstValue(object, paths) {
  for (const path of paths) {
    const value = valueAt(object, path);
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return "";
}

function unwrapDepartures(data) {
  let rows = Array.isArray(data) ? data : firstValue(data, ["departures", "items", "results"]);
  if (!Array.isArray(rows) && Array.isArray(data?.features)) {
    rows = data.features.map(feature => feature.properties || feature);
  }
  if (!Array.isArray(rows)) return [];
  return rows.flatMap(row => {
    if (Array.isArray(row?.departures)) {
      const stopName = firstValue(row, ["stop.name", "stop_name", "name"]);
      return row.departures.map(departure => ({ ...departure, _boardStopName: stopName }));
    }
    return [row?.properties || row];
  });
}

function getDepartureTime(item) {
  const candidate = firstValue(item, [
    "departure_timestamp.predicted", "arrival_timestamp.predicted",
    "departure_timestamp.scheduled", "arrival_timestamp.scheduled",
    "departureTime", "departure_time", "time"
  ]);
  if (typeof candidate === "number") return new Date(candidate < 1e12 ? candidate * 1000 : candidate);
  return candidate ? new Date(candidate) : null;
}

function render() {
  list.replaceChildren();
  const now = Date.now();
  const departures = lastDepartures
    .map(item => ({ item, date: getDepartureTime(item) }))
    .filter(entry => entry.date && Number.isFinite(entry.date.getTime()))
    .sort((a, b) => a.date - b.date);

  if (!departures.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = lastDepartures.length
      ? "Departure times were not available in the response."
      : "No upcoming departures were returned.";
    list.append(empty);
  }

  for (const { item, date } of departures.slice(0, 40)) {
    const row = document.createElement("li");
    row.className = "departure";

    const route = document.createElement("span");
    route.className = "line";
    route.textContent = String(firstValue(item, ["route.short_name", "route.shortName", "route.name", "route_id", "routeId"]) || "—");

    const destination = document.createElement("span");
    destination.className = "destination";
    destination.append(document.createTextNode(String(firstValue(item, ["trip.headsign", "trip_headsign", "headsign", "direction", "destination"]) || "Destination not provided")));
    const stopName = firstValue(item, ["_boardStopName", "stop.name", "stop_name"]);
    if (stopName) {
      const stop = document.createElement("small");
      stop.className = "stop-name";
      stop.textContent = String(stopName);
      destination.append(stop);
    }

    const time = document.createElement("span");
    time.className = "time";
    const minutes = Math.ceil((date.getTime() - now) / 60000);
    const relative = document.createElement("strong");
    relative.textContent = minutes <= 0 ? "now" : `${minutes} min`;
    const exact = document.createElement("span");
    exact.textContent = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(date);
    time.append(relative, exact);
    row.append(route, destination, time);
    list.append(row);
  }

  list.setAttribute("aria-busy", "false");
  if (lastUpdatedAt) {
    const date = new Date(lastUpdatedAt);
    updated.textContent = `Updated ${Number.isFinite(date.getTime()) ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "recently"}${lastWasStale ? " · showing saved data" : ""}`;
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/departures", { cache: "no-store", headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Departure service unavailable");
    lastDepartures = unwrapDepartures(payload.data);
    lastUpdatedAt = payload.updatedAt;
    lastWasStale = Boolean(payload.stale);
    message.textContent = lastWasStale ? "Golemio is temporarily unavailable; showing the last successful update." : "";
    render();
  } catch (error) {
    message.textContent = error.message || "Could not load departures. Retrying in one minute.";
    list.setAttribute("aria-busy", "false");
    if (lastDepartures.length) render();
    else {
      list.replaceChildren();
      const empty = document.createElement("li");
      empty.className = "empty";
      empty.textContent = "Waiting for departure data…";
      list.append(empty);
    }
    if (!lastUpdatedAt) updated.textContent = "Unable to connect to the departure service";
  }
}

refresh();
window.setInterval(refresh, 60_000);
window.setInterval(render, 15_000);
