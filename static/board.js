import * as maplibregl from "./vendor/maplibre-gl.mjs";

maplibregl.setWorkerUrl(new URL("./vendor/maplibre-gl-worker.mjs", import.meta.url).href);

const POLL_INTERVAL_MS = 2000;
const URGENCIES = ["EMERGENCY", "PRIORITY", "WELFARE", "ROUTINE"];
const URGENCY_ORDER = { EMERGENCY: 0, PRIORITY: 1, WELFARE: 2, ROUTINE: 3 };
const STATUS_ORDER = { open: 0, assigned: 1, closed: 2 };
// Open tickets older than these get an amber / red age, so a forgotten report stands out.
const LATE_MINUTES = 10;
const CRITICAL_MINUTES = { EMERGENCY: 5, PRIORITY: 20 };
// Radio check-in board, after SAR personnel-accountability boards: a radio
// silent this long is "quiet", then "overdue".
const QUIET_MINUTES = 15;
const OVERDUE_MINUTES = 30;

const markerByCode = new Map();
const $ = (id) => document.getElementById(id);
const elements = {
  linkStatus: $("link-status"),
  linkLabel: $("link-label"),
  linkDetail: $("link-detail"),
  mockBadge: $("mock-badge"),
  lastUpdated: $("last-updated"),
  mapMessage: $("map-message"),
  mapEmpty: $("map-empty"),
  mapSummary: $("map-summary"),
  fitMap: $("fit-map"),
  ticketList: $("ticket-list"),
  ticketCount: $("ticket-count"),
  queueEmpty: $("queue-empty"),
  filterNote: $("filter-note"),
  search: $("search"),
  emergencyPanel: $("emergency-panel"),
  emergencyHeading: $("emergency-heading"),
  emergencyCards: $("emergency-cards"),
  priorityList: $("priority-list"),
  priorityForm: $("priority-form"),
  priorityNodeId: $("priority-node-id"),
  priorityRole: $("priority-role"),
  toast: $("toast"),
  clockLocal: $("clock-local"),
  clockUtc: $("clock-utc"),
  clockElapsed: $("clock-elapsed"),
  rosterList: $("roster-list"),
  rosterCount: $("roster-count"),
};

let map;
let mapReady = false;
let mapStyleLoaded = false;
let initialFitDone = false;
let latestTickets = [];
let lastQueueSignature = "";
let lastEmergencySignature = "";
let lastPrioritySignature = "";
let mockMode = false;
let priorityNodes = {};
let seenCodes = null;
const freshCodes = new Set();
let selectedCode = null;
const filters = { status: "active", urgency: null, query: "" };
let toastTimer;

// ---------- helpers ----------

function coordinates(ticket) {
  const latitude = ticket.latitude;
  const longitude = ticket.longitude;
  if (typeof latitude !== "number" || typeof longitude !== "number") return null;
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
  if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) return null;
  return [longitude, latitude];
}

// Matches the gateway's FIRETOWER_MAX_POSITION_AGE_SECONDS default. A fix
// further than this from the report is the radio's last known position,
// shown as an approximation.
const APPROX_GPS_MINUTES = 30;

function gpsGapMinutes(ticket) {
  const report = new Date(ticket.received_at).getTime();
  const fix = new Date(ticket.position_received_at).getTime();
  if (Number.isNaN(report) || Number.isNaN(fix)) return null;
  return Math.abs(report - fix) / 60000;
}

// Where a pin came from (tickets.location_source). Older rows have no
// source; their GPS counts as approximate when the fix is far from the report.
function locationSource(ticket) {
  if (ticket.location_source) return ticket.location_source;
  if (!coordinates(ticket)) return null;
  const gap = gpsGapMinutes(ticket);
  return gap != null && gap > APPROX_GPS_MINUTES ? "gps_old" : "gps";
}

function isApproxGps(ticket) {
  const source = locationSource(ticket);
  return source != null && source !== "gps";
}

function formatRadius(meters) {
  if (!Number.isFinite(meters)) return "";
  return meters >= 1000 ? `±${(meters / 1000).toFixed(meters >= 10000 ? 0 : 1)} km` : `±${meters} m`;
}

function gpsLabel(ticket) {
  const radius = formatRadius(ticket.location_radius_m);
  switch (locationSource(ticket)) {
    case "gps_old":
      return `Approx. (old GPS, ${formatDuration(gpsGapMinutes(ticket))} from report)`;
    case "map":
      return `Map lookup of stated place${radius ? ` (${radius})` : ""}`;
    case "radio_area":
      return `Radio area${radius ? ` (${radius})` : ""}`;
    default:
      return "Radio GPS";
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function formatTime(value) {
  if (!value) return "unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "unknown time" : date.toLocaleString([], { hour12: false });
}

// 24-hour HH:MM, the way SAR logs record times.
function clockTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "--:--"
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

// WGS84 lat/lon -> UTM grid reference ("17S 236512E 3832740N"), which ground
// teams read off topo maps. Ignores the Norway/Svalbard zone exceptions.
function toUtm(lat, lon) {
  const a = 6378137;
  const f = 1 / 298.257223563;
  const k0 = 0.9996;
  const e2 = f * (2 - f);
  const ep2 = e2 / (1 - e2);
  const zone = Math.min(60, Math.floor((lon + 180) / 6) + 1);
  const lon0 = (((zone - 1) * 6 - 180 + 3) * Math.PI) / 180;
  const phi = (lat * Math.PI) / 180;
  const n = a / Math.sqrt(1 - e2 * Math.sin(phi) ** 2);
  const t = Math.tan(phi) ** 2;
  const c = ep2 * Math.cos(phi) ** 2;
  const A = Math.cos(phi) * ((lon * Math.PI) / 180 - lon0);
  const m = a * (
    (1 - e2 / 4 - (3 * e2 ** 2) / 64 - (5 * e2 ** 3) / 256) * phi
    - ((3 * e2) / 8 + (3 * e2 ** 2) / 32 + (45 * e2 ** 3) / 1024) * Math.sin(2 * phi)
    + ((15 * e2 ** 2) / 256 + (45 * e2 ** 3) / 1024) * Math.sin(4 * phi)
    - ((35 * e2 ** 3) / 3072) * Math.sin(6 * phi)
  );
  const easting = k0 * n * (A + ((1 - t + c) * A ** 3) / 6
    + ((5 - 18 * t + t * t + 72 * c - 58 * ep2) * A ** 5) / 120) + 500000;
  let northing = k0 * (m + n * Math.tan(phi) * (A ** 2 / 2
    + ((5 - t + 9 * c + 4 * c * c) * A ** 4) / 24
    + ((61 - 58 * t + t * t + 600 * c - 330 * ep2) * A ** 6) / 720));
  if (lat < 0) northing += 10000000;
  const band = "CDEFGHJKLMNPQRSTUVWX"[Math.min(19, Math.max(0, Math.floor((lat + 80) / 8)))];
  return `${zone}${band} ${Math.round(easting)}E ${Math.round(northing)}N`;
}

function minutesSince(value) {
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : Math.max(0, (Date.now() - time) / 60000);
}

function formatAge(value) {
  const minutes = minutesSince(value);
  if (minutes == null) return "—";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${Math.floor(minutes)}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${Math.floor(minutes % 60)}m ago`;
  return new Date(value).toLocaleDateString();
}

function formatDuration(minutes) {
  if (minutes == null) return "—";
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${Math.floor(minutes)}m`;
  return `${Math.floor(minutes / 60)}h ${Math.floor(minutes % 60)}m`;
}

function ageClass(ticket) {
  if (ticket.status !== "open") return "age";
  const minutes = minutesSince(ticket.received_at) ?? 0;
  const critical = CRITICAL_MINUTES[ticket.urgency];
  if (critical != null && minutes >= critical) return "age is-critical";
  if (minutes >= LATE_MINUTES) return "age is-late";
  return "age";
}

function showToast(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.className = `toast${isError ? " is-error" : ""}`;
  elements.toast.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { elements.toast.hidden = true; }, 3200);
}

function showMapMessage(message) {
  elements.mapMessage.textContent = message;
  elements.mapMessage.hidden = false;
}

// ---------- map ----------

function makePopup(ticket) {
  const container = document.createElement("div");
  container.append(el("h3", null, `${ticket.ticket_code} · ${ticket.urgency}`));
  const dispatched = teamIds(ticket.dispatched_teams).map(teamLabel);
  for (const [label, value] of [
    ["Node", ticket.node_id],
    ["Report", ticket.what_happened || ticket.raw_text],
    ["Reported place", ticket.location || "Not specified"],
    ["Pin", gpsLabel(ticket)],
    ...(ticket.location_note ? [["How", ticket.location_note]] : []),
    ...(ticket.position_received_at ? [["Sender GPS received", formatTime(ticket.position_received_at)]] : []),
    ...(dispatched.length ? [["Dispatched", dispatched.join(", ")]] : []),
  ]) {
    const line = el("p");
    line.append(el("strong", null, `${label}: `), document.createTextNode(String(value || "")));
    container.append(line);
  }
  container.append(el("p", "popup-note", isApproxGps(ticket)
    ? "Approximate pin, not a verified incident location. Confirm with the sender."
    : "This pin is the sender radio's last known position, not a verified incident location."));
  return container;
}

function syncMarkers(tickets) {
  if (!mapReady) return;
  const located = tickets.filter((ticket) => coordinates(ticket));
  const present = new Set(located.map((ticket) => ticket.ticket_code));

  for (const [code, entry] of markerByCode) {
    if (!present.has(code)) {
      entry.marker.remove();
      markerByCode.delete(code);
    }
  }

  for (const ticket of located) {
    const isPriorityNode = Boolean(priorityNodes[ticket.node_id]);
    const signature = JSON.stringify([
      ticket.latitude, ticket.longitude, ticket.urgency, ticket.status, isPriorityNode,
      ticket.what_happened, ticket.raw_text, ticket.location, ticket.position_received_at,
      ticket.dispatched_teams, ticket.location_source, ticket.location_radius_m,
    ]);
    const previous = markerByCode.get(ticket.ticket_code);
    if (previous?.signature === signature) continue;
    previous?.marker.remove();

    const pin = document.createElement("button");
    pin.type = "button";
    pin.className = [
      "ticket-marker", ticket.urgency.toLowerCase(), ticket.status,
      isPriorityNode ? "priority-node" : "",
      isApproxGps(ticket) ? "is-approx" : "",
      ticket.ticket_code === selectedCode ? "is-selected" : "",
    ].join(" ");
    pin.title = `${ticket.ticket_code}: ${gpsLabel(ticket)}`;
    pin.setAttribute("aria-label", `${ticket.ticket_code}, ${ticket.urgency}`);
    pin.addEventListener("click", () => selectTicket(ticket.ticket_code, { fromMap: true }));
    const popup = new maplibregl.Popup({ offset: 18, maxWidth: "300px" })
      .setDOMContent(makePopup(ticket));
    const marker = new maplibregl.Marker({ element: pin })
      .setLngLat(coordinates(ticket))
      .setPopup(popup)
      .addTo(map);
    // Higher urgency draws on top when pins overlap.
    pin.style.zIndex = String(10 - (URGENCY_ORDER[ticket.urgency] ?? 4));
    markerByCode.set(ticket.ticket_code, { marker, signature, pin });
  }

  if (located.length && !initialFitDone) {
    fitLocatedTickets();
    initialFitDone = true;
  }
}

// ---------- simulated response units ----------
// Firetower never contacts a department. This only animates the
// coordinator's dispatch record: each dispatched team leaves a pretend
// staging point and reaches the ticket's pin SIM_TRAVEL_SECONDS later.
const SIM_TRAVEL_SECONDS = 180;
const SIM_START_METERS = 2500;
const TEAM_BEARING = { fire: 45, police: 135, ems: 225, rescue: 315 };
const unitMarkers = new Map();
let unitFrame = 0;
let lastRouteTrim = 0;

function offsetPoint([longitude, latitude], meters, bearingDegrees) {
  const bearing = (bearingDegrees * Math.PI) / 180;
  const dLat = (meters * Math.cos(bearing)) / 111320;
  const dLon = (meters * Math.sin(bearing)) / (111320 * Math.cos((latitude * Math.PI) / 180));
  return [longitude + dLon, latitude + dLat];
}

// ---------- facilities (OpenStreetMap, cached by the board) ----------
const TEAM_FACILITY = { fire: "fire", police: "police", ems: "hospital", rescue: "fire" };
const FACILITY_LABELS = { fire: "Fire station", hospital: "Hospital", police: "Police" };
const FACILITY_BADGE = { fire: "F", hospital: "H", police: "P" };
const MAX_STATION_METERS = 40000;
let facilityList = [];
let facilitiesCheckedAt = 0;
const facilityMarkers = new Map();

function metersBetween([lon1, lat1], [lon2, lat2]) {
  const x = ((lon2 - lon1) * Math.PI / 180) * Math.cos(((lat1 + lat2) / 2) * Math.PI / 180);
  const y = (lat2 - lat1) * Math.PI / 180;
  return Math.sqrt(x * x + y * y) * 6371000;
}

function nearestFacility(team, point) {
  const kind = TEAM_FACILITY[team];
  let best = null;
  let bestMeters = MAX_STATION_METERS;
  for (const facility of facilityList) {
    if (facility.kind !== kind) continue;
    const meters = metersBetween([facility.longitude, facility.latitude], point);
    if (meters < bestMeters) {
      best = facility;
      bestMeters = meters;
    }
  }
  return best;
}

async function loadFacilities() {
  // The list changes rarely; the board refreshes its cache in the background.
  if (Date.now() - facilitiesCheckedAt < 60000) return;
  facilitiesCheckedAt = Date.now();
  try {
    const response = await fetch("/api/facilities", { cache: "no-store" });
    if (!response.ok) return;
    facilityList = (await response.json()).facilities || [];
    syncFacilities();
    syncUnits(latestTickets);
  } catch (error) {
    console.error("Facilities load failed", error);
  }
}

function facilityPopup(facility) {
  const container = document.createElement("div");
  container.append(el("h3", null, facility.name));
  for (const [label, value] of [
    ["Type", FACILITY_LABELS[facility.kind] + (facility.emergency_room ? " (emergency room)" : "")],
    ["Address", facility.address],
    ["Phone", facility.phone],
  ]) {
    if (!value) continue;
    const line = el("p");
    line.append(el("strong", null, `${label}: `), document.createTextNode(value));
    container.append(line);
  }
  container.append(el("p", "popup-note",
    "OpenStreetMap data, may be out of date. Firetower does not contact this facility."));
  return container;
}

function syncFacilities() {
  if (!mapReady) return;
  const present = new Set(facilityList.map((facility) => facility.id));
  for (const [id, marker] of facilityMarkers) {
    if (!present.has(id)) {
      marker.remove();
      facilityMarkers.delete(id);
    }
  }
  for (const facility of facilityList) {
    if (facilityMarkers.has(facility.id)) continue;
    const badge = el("div", `facility-marker kind-${facility.kind}`, FACILITY_BADGE[facility.kind]);
    badge.title = `${facility.name} (${FACILITY_LABELS[facility.kind]})`;
    const marker = new maplibregl.Marker({ element: badge })
      .setLngLat([facility.longitude, facility.latitude])
      .setPopup(new maplibregl.Popup({ offset: 12, maxWidth: "260px" }).setDOMContent(facilityPopup(facility)))
      .addTo(map);
    facilityMarkers.set(facility.id, marker);
  }
}

function unitProgress(unit, now = Date.now()) {
  return Math.min(1, Math.max(0, (now - unit.departed) / (SIM_TRAVEL_SECONDS * 1000)));
}

function syncUnits(tickets) {
  if (!mapReady) return;
  const wanted = new Map();
  for (const ticket of tickets) {
    const end = coordinates(ticket);
    const departed = new Date(ticket.dispatched_at).getTime();
    if (!end || ticket.status === "closed" || Number.isNaN(departed)) continue;
    for (const team of teamIds(ticket.dispatched_teams)) {
      const station = nearestFacility(team, end);
      wanted.set(`${ticket.ticket_code}:${team}:${end.join(",")}:${station?.id ?? "staging"}`,
        { ticket, team, end, departed, station });
    }
  }
  for (const [key, unit] of unitMarkers) {
    if (!wanted.has(key)) {
      unit.marker.remove();
      unitMarkers.delete(key);
    }
  }
  for (const [key, info] of wanted) {
    if (unitMarkers.has(key)) continue;
    const badge = el("div", `unit-marker team-${info.team}`, teamLabel(info.team));
    // Leave from the nearest real station of that kind when we know one.
    const start = info.station
      ? [info.station.longitude, info.station.latitude]
      : offsetPoint(info.end, SIM_START_METERS, TEAM_BEARING[info.team] ?? 0);
    const marker = new maplibregl.Marker({ element: badge }).setLngLat(start).addTo(map);
    const unit = {
      marker, badge, start, end: info.end, departed: info.departed,
      code: info.ticket.ticket_code, team: info.team, from: info.station?.name,
    };
    setUnitPath(unit, [start, info.end]);
    unitMarkers.set(key, unit);
    loadStreetRoute(unit);
  }
  syncRoutes();
  if (unitMarkers.size && !unitFrame) unitFrame = requestAnimationFrame(animateUnits);
}

function setUnitPath(unit, path) {
  // Cumulative lengths let the badge move at an even pace along every street.
  const lengths = [0];
  for (let i = 1; i < path.length; i += 1) {
    lengths.push(lengths[i - 1] + metersBetween(path[i - 1], path[i]));
  }
  unit.path = path;
  unit.lengths = lengths;
}

async function loadStreetRoute(unit) {
  const [from, to] = [unit.start, unit.end];
  const query = new URLSearchParams({
    from_lon: from[0], from_lat: from[1], to_lon: to[0], to_lat: to[1],
  });
  try {
    const response = await fetch(`/api/route?${query}`, { cache: "no-store" });
    if (!response.ok) return;
    const route = await response.json();
    if (route.source !== "osrm" || route.coordinates.length < 2) return;
    // The route starts and ends on the nearest road; join it to the exact points.
    setUnitPath(unit, [from, ...route.coordinates, to]);
    unit.roadMinutes = route.duration_s ? route.duration_s / 60 : null;
    syncRoutes();
  } catch (error) {
    console.error("Street route failed", error);
  }
}

function pointAlong(unit, progress) {
  const total = unit.lengths[unit.lengths.length - 1];
  if (!total) return unit.path[unit.path.length - 1];
  const target = total * progress;
  let i = 1;
  while (i < unit.lengths.length - 1 && unit.lengths[i] < target) i += 1;
  const segment = unit.lengths[i] - unit.lengths[i - 1] || 1;
  const t = Math.min(1, Math.max(0, (target - unit.lengths[i - 1]) / segment));
  const [a, b] = [unit.path[i - 1], unit.path[i]];
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
}

function remainingPath(unit, progress) {
  const total = unit.lengths[unit.lengths.length - 1];
  const target = total * progress;
  const ahead = unit.path.filter((_, i) => unit.lengths[i] > target);
  return [pointAlong(unit, progress), ...ahead];
}

function syncRoutes() {
  if (!map?.isStyleLoaded?.()) return;
  const now = Date.now();
  const data = {
    type: "FeatureCollection",
    features: [...unitMarkers.values()]
      .filter((unit) => unitProgress(unit, now) < 1)
      .map((unit) => ({
        type: "Feature",
        properties: { team: unit.team },
        geometry: { type: "LineString", coordinates: remainingPath(unit, unitProgress(unit, now)) },
      })),
  };
  const source = map.getSource("sim-routes");
  if (source) {
    source.setData(data);
    return;
  }
  map.addSource("sim-routes", { type: "geojson", data });
  map.addLayer({
    id: "sim-routes", type: "line", source: "sim-routes",
    layout: { "line-cap": "round" },
    paint: { "line-color": "#c9d4dc", "line-width": 2, "line-dasharray": [1.5, 2], "line-opacity": 0.7 },
  });
}

function animateUnits() {
  unitFrame = 0;
  const now = Date.now();
  let moving = false;
  for (const unit of unitMarkers.values()) {
    // Lights and sirens: constant pace, no stops at intersections or lights.
    const progress = unitProgress(unit, now);
    unit.marker.setLngLat(pointAlong(unit, progress));
    const arrived = progress >= 1;
    if (unit.badge.classList.contains("is-arrived") !== arrived) {
      unit.badge.classList.toggle("is-arrived", arrived);
      syncRoutes();
    }
    const label = teamLabel(unit.team);
    unit.badge.title = arrived
      ? `${label} on scene at ${unit.code} (simulated)`
      : `${label} en route to ${unit.code}${unit.from ? ` from ${unit.from}` : ""}, `
        + `~${formatDuration((1 - progress) * SIM_TRAVEL_SECONDS / 60)} left (simulated`
        + `${unit.roadMinutes ? `; ${formatDuration(unit.roadMinutes)} drive in normal traffic` : ""})`;
    moving ||= !arrived;
  }
  // Trim the drawn route behind each badge about twice a second.
  if (moving && now - lastRouteTrim > 500) {
    lastRouteTrim = now;
    syncRoutes();
  }
  if (moving) unitFrame = requestAnimationFrame(animateUnits);
}

function fitLocatedTickets() {
  if (!mapReady) return;
  const points = latestTickets.map(coordinates).filter(Boolean);
  if (!points.length) return;
  if (points.length === 1) {
    map.flyTo({ center: points[0], zoom: 12, essential: true });
    return;
  }
  const bounds = points.reduce(
    (result, point) => result.extend(point),
    new maplibregl.LngLatBounds(points[0], points[0]),
  );
  map.fitBounds(bounds, { padding: 60, maxZoom: 13, duration: 700 });
}

function focusTicketOnMap(code) {
  const entry = markerByCode.get(code);
  if (!entry || !mapReady) return;
  const position = entry.marker.getLngLat();
  map.flyTo({ center: [position.lng, position.lat], zoom: Math.max(map.getZoom(), 12), essential: true });
  if (!entry.marker.getPopup().isOpen()) entry.marker.togglePopup();
  // The map is sticky beside the queue on wide screens; only scroll when stacked.
  const frame = map.getContainer().getBoundingClientRect();
  if (frame.top < 0 || frame.bottom > window.innerHeight) {
    map.getContainer().scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function selectTicket(code, { fromMap = false } = {}) {
  selectedCode = code;
  for (const row of elements.ticketList.children) {
    row.classList.toggle("is-selected", row.dataset.code === code);
  }
  for (const [markerCode, entry] of markerByCode) {
    entry.pin.classList.toggle("is-selected", markerCode === code);
  }
  if (fromMap) {
    const row = elements.ticketList.querySelector(`[data-code="${CSS.escape(code)}"]`);
    row?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } else {
    focusTicketOnMap(code);
  }
}

// ---------- queue ----------

function matchesFilters(ticket) {
  if (filters.status === "active" && ticket.status === "closed") return false;
  if (filters.status === "open" && ticket.status !== "open") return false;
  if (filters.status === "closed" && ticket.status !== "closed") return false;
  if (filters.urgency && ticket.urgency !== filters.urgency) return false;
  if (filters.query) {
    const haystack = [
      ticket.ticket_code, ticket.node_id, ticket.raw_text, ticket.what_happened,
      ticket.location, priorityNodes[ticket.node_id],
      ticket.clarification_question, ticket.clarification_response,
      ...teamIds(ticket.dispatched_teams).map(teamLabel),
    ].join(" ").toLowerCase();
    if (!haystack.includes(filters.query)) return false;
  }
  return true;
}

function fact(label, value, { missing = "unknown", mono = false } = {}) {
  const wrap = el("div");
  const dd = el("dd", null, value ?? missing);
  if (value == null || value === "") {
    dd.textContent = missing;
    dd.classList.add("missing");
  }
  if (mono) dd.classList.add("mono");
  wrap.append(el("dt", null, label), dd);
  return wrap;
}

function clarificationDetails(ticket) {
  if (!ticket.clarification_question && !ticket.clarification_response) return null;
  const details = el("dl", "clarification-details");
  for (const [label, value] of [
    ["AI follow-up question", ticket.clarification_question],
    ["Sender reply (raw)", ticket.clarification_response],
  ]) {
    if (!value) continue;
    const entry = el("div");
    entry.append(el("dt", null, label), el("dd", null, value));
    details.append(entry);
  }
  return details;
}

function statusActions(ticket) {
  const actions = el("div", "ticket-actions");
  const add = (label, status, extra = "") => {
    const button = el("button", `btn btn-small ${extra}`.trim(), label);
    button.type = "button";
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      setStatus(ticket.ticket_code, status, button);
    });
    actions.append(button);
  };
  if (ticket.status === "open") {
    const suggested = teamIds(ticket.suggested_teams);
    if (suggested.length && !teamIds(ticket.dispatched_teams).length) {
      const button = el("button", "btn btn-small btn-dispatch", `Dispatch ${formatTeamList(suggested)}`);
      button.type = "button";
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        dispatchTeams(ticket.ticket_code, button);
      });
      actions.append(button);
    } else if (!teamIds(ticket.dispatched_teams).length) {
      add("Assign", "assigned");
    }
    add("Close", "closed", "btn-quiet");
  } else if (ticket.status === "assigned") {
    add("Close", "closed");
    add("Reopen", "open", "btn-quiet");
  } else {
    add("Reopen", "open", "btn-quiet");
  }
  if (coordinates(ticket)) {
    const locate = el("button", "btn btn-small btn-quiet", "Map");
    locate.type = "button";
    locate.setAttribute("aria-label", `Show ${ticket.ticket_code} on map`);
    locate.addEventListener("click", (event) => {
      event.stopPropagation();
      selectTicket(ticket.ticket_code);
    });
    actions.append(locate);
  }
  return actions;
}

function renderTicket(ticket) {
  const urgency = ticket.urgency.toLowerCase();
  const item = el("li", `ticket ${urgency} status-${ticket.status}`);
  item.dataset.code = ticket.ticket_code;
  if (ticket.ticket_code === selectedCode) item.classList.add("is-selected");
  if (freshCodes.has(ticket.ticket_code)) item.classList.add("is-new");

  const top = el("div", "ticket-top");
  top.append(el("span", "ticket-code", ticket.ticket_code), el("span", "urgency-chip", ticket.urgency));
  if (ticket.raised_by_keyword) {
    const tag = el("span", "tag tag-keyword", "raised by keyword");
    tag.title = "The keyword safety net raised this above the model's ranking.";
    top.append(tag);
  }
  const role = priorityNodes[ticket.node_id];
  if (role || ticket.priority_floor_applied) {
    top.append(el("span", "tag tag-priority", `priority node${role ? `: ${role}` : ""}`));
  }
  if (ticket.status !== "open") top.append(el("span", `status-chip ${ticket.status}`, ticket.status));
  for (const team of teamIds(ticket.dispatched_teams)) {
    const chip = el("span", `tag tag-team tag-team-${team}`, teamLabel(team));
    chip.title = ticket.dispatched_at ? `Dispatched ${formatTime(ticket.dispatched_at)}` : "Dispatched from this board";
    top.append(chip);
  }
  const age = el("time", ageClass(ticket), `${clockTime(ticket.received_at)} · ${formatAge(ticket.received_at)}`);
  age.dateTime = ticket.received_at || "";
  age.title = formatTime(ticket.received_at);
  age.dataset.age = "";
  top.append(age);

  item.append(top);
  item.append(el("p", "ticket-what", ticket.what_happened || ticket.raw_text || "No details"));
  if (ticket.what_happened && ticket.raw_text) item.append(el("p", "ticket-raw", ticket.raw_text));
  const clarification = clarificationDetails(ticket);
  if (clarification) item.append(clarification);

  const bottom = el("div", "ticket-bottom");
  const facts = el("dl", "facts");
  facts.append(
    fact("Where", ticket.location, { missing: "not given" }),
    fact("Hurt", Number.isInteger(ticket.injured_count) ? ticket.injured_count : null),
    fact("Node", ticket.node_id, { mono: true }),
  );
  if (Number.isInteger(ticket.hop_count)) {
    facts.append(fact("Hops", ticket.hop_count));
  }
  const coords = coordinates(ticket);
  if (coords) {
    const approx = isApproxGps(ticket);
    const grid = fact(approx ? "Approx. grid" : "Grid", toUtm(coords[1], coords[0]), { mono: true });
    grid.title = `${coords[1].toFixed(5)}, ${coords[0].toFixed(5)} (${gpsLabel(ticket)})`;
    if (approx) grid.classList.add("is-approx");
    facts.append(grid);
  } else {
    facts.append(fact("GPS", null, { missing: "none" }));
  }
  bottom.append(facts, statusActions(ticket));
  item.append(bottom);

  item.addEventListener("click", () => selectTicket(ticket.ticket_code));
  return item;
}

function renderQueue() {
  const visible = latestTickets.filter(matchesFilters);
  const signature = JSON.stringify([visible, priorityNodes, [...freshCodes]]);
  elements.ticketCount.textContent = String(visible.length);

  const notes = [];
  if (filters.urgency) notes.push(filters.urgency.toLowerCase());
  if (filters.query) notes.push(`“${filters.query}”`);
  elements.filterNote.hidden = notes.length === 0;
  if (notes.length) {
    const clear = el("button", null, "Clear");
    clear.type = "button";
    clear.addEventListener("click", () => {
      filters.urgency = null;
      filters.query = "";
      elements.search.value = "";
      syncFilterControls();
      renderQueue();
    });
    elements.filterNote.replaceChildren(
      document.createTextNode(`Showing ${visible.length} of ${latestTickets.length} · filtered by ${notes.join(", ")}`),
      clear,
    );
  }

  if (signature !== lastQueueSignature) {
    const fragment = document.createDocumentFragment();
    for (const ticket of visible) fragment.append(renderTicket(ticket));
    elements.ticketList.replaceChildren(fragment);
    lastQueueSignature = signature;
  } else {
    // Nothing changed but time passed: refresh only the age labels.
    for (const row of elements.ticketList.children) {
      const ticket = visible.find((t) => t.ticket_code === row.dataset.code);
      const age = row.querySelector("[data-age]");
      if (ticket && age) {
        age.textContent = `${clockTime(ticket.received_at)} · ${formatAge(ticket.received_at)}`;
        age.className = ageClass(ticket);
      }
    }
  }

  elements.queueEmpty.hidden = visible.length > 0;
  elements.queueEmpty.textContent = latestTickets.length
    ? "No tickets match these filters."
    : "No messages yet. Tickets appear here as soon as the gateway triages them.";
}

function syncFilterControls() {
  for (const button of document.querySelectorAll(".segmented button")) {
    button.setAttribute("aria-pressed", String(button.dataset.status === filters.status));
  }
  for (const button of document.querySelectorAll(".stat[data-urgency]")) {
    button.setAttribute("aria-pressed", String(button.dataset.urgency === filters.urgency));
  }
}

async function setStatus(ticketCode, status, button) {
  if (button) button.disabled = true;
  try {
    const response = await fetch(
      `/api/tickets/${encodeURIComponent(ticketCode)}/status?status=${encodeURIComponent(status)}`,
      { method: "POST" },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    showToast(`${ticketCode} marked ${status}`);
    await poll();
  } catch (error) {
    if (button) button.disabled = false;
    showToast(`Could not update ${ticketCode}`, true);
    console.error("Ticket status update failed", error);
  }
}

// ---------- summary ----------

function renderSummary() {
  const open = latestTickets.filter((ticket) => ticket.status === "open");
  for (const urgency of URGENCIES) {
    const count = open.filter((ticket) => ticket.urgency === urgency).length;
    $(`count-${urgency}`).textContent = String(count);
    if (urgency === "EMERGENCY") {
      document.querySelector(".stat-emergency").classList.toggle("has-items", count > 0);
    }
  }
  const hurt = latestTickets
    .filter((ticket) => ticket.status !== "closed" && Number.isInteger(ticket.injured_count))
    .reduce((sum, ticket) => sum + ticket.injured_count, 0);
  $("count-hurt").textContent = String(hurt);
  const oldest = open.reduce((max, ticket) => Math.max(max, minutesSince(ticket.received_at) ?? 0), -1);
  $("count-oldest").textContent = oldest < 0 ? "—" : formatDuration(oldest);
}

// ---------- dispatch cards ----------
// Labels match dispatch.TEAM_LABELS. Team choice happens on the server.

const TEAM_LABELS = { fire: "Fire", police: "Police", ems: "EMS", rescue: "Rescue" };

function teamIds(value) {
  if (Array.isArray(value)) return value.filter(Boolean);
  if (typeof value === "string" && value.trim()) {
    return value.split(",").map((part) => part.trim()).filter(Boolean);
  }
  return [];
}

function teamLabel(team) {
  return TEAM_LABELS[team] || team;
}

function formatTeamList(teams) {
  const labels = teams.map(teamLabel);
  if (labels.length <= 1) return labels[0] || "team";
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")}, and ${labels[labels.length - 1]}`;
}

async function dispatchTeams(ticketCode, button) {
  if (button) button.disabled = true;
  try {
    const response = await fetch(
      `/api/tickets/${encodeURIComponent(ticketCode)}/dispatch`,
      { method: "POST" },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json();
    const sent = formatTeamList(teamIds(body.dispatched_teams));
    showToast(`${ticketCode} dispatched: ${sent}. The sender gets a radio message.`);
    await poll();
  } catch (error) {
    if (button) button.disabled = false;
    showToast(`Could not dispatch ${ticketCode}`, true);
    console.error("Dispatch failed", error);
  }
}

function renderEmergencyCards() {
  // Oldest undispatched emergency first.
  const emergencies = latestTickets
    .filter((ticket) => (
      ticket.urgency === "EMERGENCY"
      && ticket.status === "open"
      && teamIds(ticket.dispatched_teams).length === 0
    ))
    .sort((a, b) => String(a.received_at).localeCompare(String(b.received_at)));
  elements.emergencyPanel.hidden = emergencies.length === 0;
  document.title = emergencies.length ? `(${emergencies.length}) Firetower` : "Firetower";
  elements.emergencyHeading.textContent = emergencies.length === 1
    ? "1 emergency report waiting for dispatch"
    : `${emergencies.length} emergency reports waiting for dispatch`;

  const signature = JSON.stringify(emergencies);
  if (signature === lastEmergencySignature) {
    for (const waiting of elements.emergencyCards.querySelectorAll("[data-waiting]")) {
      waiting.textContent = `Waiting ${formatDuration(minutesSince(waiting.dataset.waiting))}`;
    }
    return;
  }
  lastEmergencySignature = signature;

  const fragment = document.createDocumentFragment();
  for (const ticket of emergencies) {
    const card = el("article", "emergency-card");
    const teams = teamIds(ticket.suggested_teams);

    const heading = el("div", "emergency-card-heading");
    const title = el("h3");
    title.append(el("span", "code", ticket.ticket_code), document.createTextNode(" · Dispatch"));
    const waiting = el("span", "waiting", `Waiting ${formatDuration(minutesSince(ticket.received_at))}`);
    waiting.dataset.waiting = ticket.received_at || "";
    waiting.title = `Received ${formatTime(ticket.received_at)}`;
    heading.append(title, waiting);

    const facts = el("div", "emergency-facts");
    for (const [label, value] of [
      ["Where", ticket.location || "not given"],
      ["Hurt", Number.isInteger(ticket.injured_count) ? ticket.injured_count : "unknown"],
      ["Node", ticket.node_id],
      ...(coordinates(ticket) ? [
        [gpsLabel(ticket), `${coordinates(ticket)[1].toFixed(5)}, ${coordinates(ticket)[0].toFixed(5)}`],
        ["Grid", toUtm(coordinates(ticket)[1], coordinates(ticket)[0])],
      ] : []),
    ]) {
      const span = el("span");
      span.append(el("b", null, label), document.createTextNode(String(value)));
      facts.append(span);
    }

    const dispatchBox = el("div", "dispatch-box");
    const clarification = clarificationDetails(ticket);
    if (clarification) dispatchBox.append(clarification);
    dispatchBox.append(el("p", "dispatch-label", "Teams chosen from this report"));
    const chips = el("div", "team-list");
    for (const team of teams) {
      chips.append(el("span", `team-chip team-${team}`, teamLabel(team)));
    }
    dispatchBox.append(chips, el("p", "dispatch-note",
      "Saved on this board, and the sender is told by radio who is coming. No call is placed."));

    const actions = el("div", "emergency-actions");
    const send = el("button", "btn btn-dispatch", `Dispatch ${formatTeamList(teams)}`);
    send.type = "button";
    send.addEventListener("click", () => dispatchTeams(ticket.ticket_code, send));
    actions.append(send);

    card.append(heading, facts, dispatchBox, actions);
    fragment.append(card);
  }
  elements.emergencyCards.replaceChildren(fragment);
}

// ---------- radio check-in ----------

function renderRoster() {
  const byNode = new Map();
  for (const ticket of latestTickets) {
    const entry = byNode.get(ticket.node_id) || { nodeId: ticket.node_id, count: 0, last: null };
    entry.count += 1;
    if (!entry.last || String(ticket.received_at) > String(entry.last.received_at)) entry.last = ticket;
    byNode.set(ticket.node_id, entry);
  }
  // Longest silence first: the radio nobody has heard from is the one to chase.
  const entries = [...byNode.values()].sort((a, b) =>
    String(a.last.received_at).localeCompare(String(b.last.received_at)));
  elements.rosterCount.textContent = String(entries.length);

  const fragment = document.createDocumentFragment();
  if (!entries.length) fragment.append(el("li", "priority-empty", "No radios heard yet."));
  for (const entry of entries) {
    const silent = minutesSince(entry.last.received_at) ?? 0;
    const state = silent >= OVERDUE_MINUTES ? "is-overdue" : silent >= QUIET_MINUTES ? "is-quiet" : "";
    const item = el("li", `roster-item ${state}`.trim());
    const who = el("div");
    who.append(el("span", "roster-node", entry.nodeId));
    const role = priorityNodes[entry.nodeId];
    const meta = `${entry.count} msg · last ${entry.last.urgency.toLowerCase()}${role ? ` · ${role}` : ""}`;
    who.append(el("span", "roster-meta", meta));
    const heard = el("div", "roster-heard", clockTime(entry.last.received_at));
    heard.append(el("small", null, silent >= OVERDUE_MINUTES ? `overdue · ${formatDuration(silent)}` : formatAge(entry.last.received_at)));
    item.append(el("span", "roster-dot"), who, heard);
    item.title = `Last message ${entry.last.ticket_code}`;
    fragment.append(item);
  }
  elements.rosterList.replaceChildren(fragment);
}

function tickClock() {
  const now = new Date();
  elements.clockLocal.textContent = now.toLocaleTimeString([], { hour12: false });
  elements.clockUtc.textContent = `${now.toISOString().slice(11, 16)}Z`;
  const first = latestTickets.reduce((min, ticket) => {
    const time = new Date(ticket.received_at).getTime();
    return Number.isNaN(time) ? min : Math.min(min, time);
  }, Infinity);
  if (Number.isFinite(first)) {
    // Units spelled out: next to clocks with seconds, "00:21" read as 21 seconds.
    const minutes = Math.max(0, Math.floor((now.getTime() - first) / 60000));
    const hours = Math.floor(minutes / 60);
    elements.clockElapsed.textContent = hours
      ? `T+ ${hours}h ${String(minutes % 60).padStart(2, "0")}m`
      : `T+ ${minutes}m`;
    elements.clockElapsed.title = `First report at ${clockTime(new Date(first))}`;
  }
}

// ---------- priority nodes ----------

function renderPriorityNodes() {
  const signature = JSON.stringify(priorityNodes);
  if (signature === lastPrioritySignature) return;
  lastPrioritySignature = signature;

  const fragment = document.createDocumentFragment();
  const entries = Object.entries(priorityNodes).sort(([a], [b]) => a.localeCompare(b));
  if (!entries.length) {
    fragment.append(el("li", "priority-empty", "None yet. Add shelters, responders or households with medical needs."));
  }
  for (const [nodeId, role] of entries) {
    const item = el("li", "priority-item");
    const label = el("span");
    label.append(el("code", null, nodeId), el("span", "priority-role", role));
    const remove = el("button", "btn btn-small btn-quiet", "Remove");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove priority node ${nodeId}`);
    remove.addEventListener("click", () => removePriorityNode(nodeId));
    item.append(label, remove);
    fragment.append(item);
  }
  elements.priorityList.replaceChildren(fragment);
}

async function addPriorityNode(event) {
  event.preventDefault();
  const nodeId = elements.priorityNodeId.value.trim();
  const role = elements.priorityRole.value.trim();
  if (!nodeId || !role) return;
  try {
    const response = await fetch(
      `/api/priority_nodes/${encodeURIComponent(nodeId)}?role=${encodeURIComponent(role)}`,
      { method: "POST" },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    elements.priorityNodeId.value = "";
    elements.priorityRole.value = "";
    showToast(`${nodeId} is now a priority node`);
    await poll();
  } catch (error) {
    showToast("Could not add priority node", true);
    console.error("Add priority node failed", error);
  }
}

async function removePriorityNode(nodeId) {
  try {
    const response = await fetch(`/api/priority_nodes/${encodeURIComponent(nodeId)}`, { method: "DELETE" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    showToast(`${nodeId} removed from priority nodes`);
    await poll();
  } catch (error) {
    showToast("Could not remove priority node", true);
    console.error("Remove priority node failed", error);
  }
}

// ---------- polling ----------

function setLinkStatus(state, label, detail) {
  elements.linkStatus.className = `link-status is-${state}`;
  elements.linkLabel.textContent = label;
  elements.linkDetail.textContent = detail;
}

function trackFreshTickets(tickets) {
  const codes = tickets.map((ticket) => ticket.ticket_code);
  if (seenCodes) {
    for (const code of codes) {
      if (!seenCodes.has(code)) {
        freshCodes.add(code);
        window.setTimeout(() => freshCodes.delete(code), 2500);
      }
    }
  }
  seenCodes = new Set(codes);
}

function updateBoard(tickets) {
  trackFreshTickets(tickets);
  // Urgency first, then open before assigned before closed, then newest.
  latestTickets = [...tickets].sort((a, b) =>
    (URGENCY_ORDER[a.urgency] ?? 99) - (URGENCY_ORDER[b.urgency] ?? 99) ||
    (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) ||
    String(b.received_at).localeCompare(String(a.received_at)));

  const located = latestTickets.filter((ticket) => coordinates(ticket)).length;
  elements.mapSummary.textContent = `${located} of ${latestTickets.length} with sender GPS`;
  elements.mapEmpty.hidden = located > 0;
  elements.mapEmpty.textContent = mockMode
    ? "No mock tickets have GPS coordinates. Check the ticket fixture."
    : "No radio GPS positions yet. Tickets without GPS stay in the queue.";
  elements.fitMap.disabled = !located || !mapReady;

  syncMarkers(latestTickets);
  syncUnits(latestTickets);
  loadFacilities();
  renderSummary();
  renderEmergencyCards();
  renderQueue();
  renderRoster();
  tickClock();
  elements.lastUpdated.textContent = `Updated ${clockTime(new Date())}`;
}

async function poll() {
  try {
    const [statusRes, ticketsRes, priorityRes] = await Promise.all([
      fetch("/api/status", { cache: "no-store" }),
      fetch("/api/tickets", { cache: "no-store" }),
      fetch("/api/priority_nodes", { cache: "no-store" }),
    ]);
    if (!statusRes.ok || !ticketsRes.ok || !priorityRes.ok) throw new Error("Board API unavailable");
    const [status, tickets, priority] = await Promise.all([
      statusRes.json(), ticketsRes.json(), priorityRes.json(),
    ]);
    if (!Array.isArray(tickets)) throw new Error("Ticket API returned invalid data");

    if (status.online) {
      setLinkStatus("online", "Online", "Cloud tier available · triage stays local");
    } else {
      setLinkStatus("offline", "Offline", "Triage running on this laptop");
    }
    mockMode = Boolean(status.mock);
    elements.mockBadge.hidden = !status.mock;
    priorityNodes = priority && typeof priority === "object" ? priority : {};
    renderPriorityNodes();
    updateBoard(tickets);
  } catch (error) {
    setLinkStatus("lost", "Board disconnected", "Is board.py running?");
    elements.lastUpdated.textContent = "Could not refresh";
    console.error("Board poll failed", error);
  }
}

// ---------- map setup ----------

function startMap() {
  try {
    if (!window.mlcontour) throw new Error("Contour library unavailable");
    const dem = new window.mlcontour.DemSource({
      url: "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp",
      encoding: "terrarium",
      maxzoom: 12,
      worker: true,
    });
    dem.setupMaplibre(maplibregl);

    const firstLocated = latestTickets.find((ticket) => coordinates(ticket));

    map = new maplibregl.Map({
      container: "map",
      style: "https://tiles.openfreemap.org/styles/liberty",
      center: firstLocated ? coordinates(firstLocated) : [-98.5, 39.8],
      zoom: firstLocated ? 12 : 3.3,
      dragRotate: false,
      pitchWithRotate: false,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    mapReady = true;
    elements.fitMap.disabled = !latestTickets.some((ticket) => coordinates(ticket));
    syncMarkers(latestTickets);
    syncFacilities();
    syncUnits(latestTickets);

    map.on("load", () => {
      mapStyleLoaded = true;
      try {
        const layers = map.getStyle().layers;
        const firstLine = layers.find((layer) => layer.type === "line" || layer.type === "symbol")?.id;
        const firstLabel = layers.find((layer) => layer.type === "symbol")?.id;
        map.addSource("firetower-dem", {
          type: "raster-dem",
          tiles: [dem.sharedDemProtocolUrl],
          encoding: "terrarium",
          tileSize: 512,
          maxzoom: 12,
          attribution: '<a href="https://mapterhorn.com/attribution" target="_blank" rel="noopener">© Mapterhorn</a>',
        });
        map.addLayer({
          id: "firetower-hillshade",
          type: "hillshade",
          source: "firetower-dem",
          paint: {
            "hillshade-exaggeration": 0.35,
            "hillshade-shadow-color": "#415760",
            "hillshade-highlight-color": "#fff4d7",
          },
        }, firstLine);

        map.addSource("firetower-contours", {
          type: "vector",
          tiles: [dem.contourProtocolUrl({
            thresholds: { 8: [500, 2500], 10: [200, 1000], 12: [100, 500], 14: [20, 100] },
            elevationKey: "ele",
            levelKey: "level",
            contourLayer: "contours",
            buffer: 1,
            overzoom: 2,
          })],
          maxzoom: 17,
        });
        map.addLayer({
          id: "firetower-contour-lines",
          type: "line",
          source: "firetower-contours",
          "source-layer": "contours",
          paint: {
            "line-color": "#966a3c",
            "line-opacity": 0.75,
            "line-width": ["match", ["get", "level"], 1, 1.4, 0.65],
          },
        }, firstLabel);
        map.addLayer({
          id: "firetower-contour-labels",
          type: "symbol",
          source: "firetower-contours",
          "source-layer": "contours",
          filter: [">", ["get", "level"], 0],
          layout: {
            "symbol-placement": "line",
            "text-size": 10,
            "text-field": ["concat", ["number-format", ["get", "ele"], {}], " m"],
            "text-font": ["Noto Sans Regular"],
          },
          paint: { "text-color": "#76512e", "text-halo-color": "#fffaf0", "text-halo-width": 1.2 },
        }, firstLabel);
      } catch (error) {
        console.error("Terrain layers failed", error);
        showMapMessage("Base map loaded; terrain layers are unavailable.");
      }
      if (elements.mapMessage.textContent === "Loading map…") elements.mapMessage.hidden = true;
    });
    map.on("error", (event) => {
      if (!mapStyleLoaded) showMapMessage("Map tiles need internet. Pins and the queue still work.");
      console.warn("Map resource failed", event.error);
    });
    window.setTimeout(() => {
      if (!mapStyleLoaded) showMapMessage("Map tiles are slow to load. Pins and the queue still work.");
    }, 15000);
  } catch (error) {
    console.error("Map setup failed", error);
    showMapMessage("Map unavailable in this browser. The queue still works.");
  }
}

// ---------- wiring ----------

for (const button of document.querySelectorAll(".segmented button")) {
  button.addEventListener("click", () => {
    filters.status = button.dataset.status;
    syncFilterControls();
    renderQueue();
  });
}
for (const button of document.querySelectorAll(".stat[data-urgency]")) {
  button.addEventListener("click", () => {
    filters.urgency = filters.urgency === button.dataset.urgency ? null : button.dataset.urgency;
    syncFilterControls();
    renderQueue();
  });
}
elements.search.addEventListener("input", () => {
  filters.query = elements.search.value.trim().toLowerCase();
  renderQueue();
});
document.addEventListener("keydown", (event) => {
  const typing = event.target instanceof HTMLInputElement;
  if (event.key === "/" && !typing) {
    event.preventDefault();
    elements.search.focus();
  } else if (event.key === "Escape" && event.target === elements.search) {
    elements.search.value = "";
    filters.query = "";
    elements.search.blur();
    renderQueue();
  }
});
elements.fitMap.addEventListener("click", fitLocatedTickets);
elements.priorityForm.addEventListener("submit", addPriorityNode);
$("overdue-minutes").textContent = String(OVERDUE_MINUTES);
tickClock();
window.setInterval(tickClock, 1000);
poll().finally(startMap);
window.setInterval(poll, POLL_INTERVAL_MS);
