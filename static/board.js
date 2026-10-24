import * as maplibregl from "./vendor/maplibre-gl.mjs";

maplibregl.setWorkerUrl(new URL("./vendor/maplibre-gl-worker.mjs", import.meta.url).href);

const POLL_INTERVAL_MS = 2000;
const URGENCY_ORDER = { EMERGENCY: 0, PRIORITY: 1, WELFARE: 2, ROUTINE: 3 };
const markerByCode = new Map();
const elements = {
  banner: document.getElementById("banner"),
  mockBadge: document.getElementById("mock-badge"),
  mapMessage: document.getElementById("map-message"),
  mapEmpty: document.getElementById("map-empty"),
  mapSummary: document.getElementById("map-summary"),
  fitMap: document.getElementById("fit-map"),
  rows: document.getElementById("rows"),
  ticketCount: document.getElementById("ticket-count"),
  lastUpdated: document.getElementById("last-updated"),
  emergencyPanel: document.getElementById("emergency-panel"),
  emergencyCards: document.getElementById("emergency-cards"),
};

let map;
let mapReady = false;
let mapStyleLoaded = false;
let initialFitDone = false;
let latestTickets = [];
let lastRenderedTickets = "";
let mockMode = false;

function coordinates(ticket) {
  const latitude = ticket.latitude;
  const longitude = ticket.longitude;
  if (typeof latitude !== "number" || typeof longitude !== "number") return null;
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
  if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) return null;
  return [longitude, latitude];
}

function showMapMessage(message) {
  elements.mapMessage.textContent = message;
  elements.mapMessage.hidden = false;
}

function formatTime(value) {
  if (!value) return "unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "unknown time" : date.toLocaleString();
}

function makePopup(ticket) {
  const container = document.createElement("div");
  const title = document.createElement("h3");
  title.textContent = `${ticket.ticket_code} · ${ticket.urgency}`;
  container.append(title);

  for (const [label, value] of [
    ["Node", ticket.node_id],
    ["Report", ticket.what_happened || ticket.raw_text],
    ["Reported place", ticket.location || "Not specified"],
    ["Sender GPS received", formatTime(ticket.position_received_at)],
  ]) {
    const line = document.createElement("p");
    const strong = document.createElement("strong");
    strong.textContent = `${label}: `;
    line.append(strong, document.createTextNode(String(value || "")));
    container.append(line);
  }

  const note = document.createElement("p");
  note.className = "popup-note";
  note.textContent = "This pin is the sender radio's last known position, not a verified incident location.";
  container.append(note);
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
    const signature = JSON.stringify([
      ticket.latitude, ticket.longitude, ticket.urgency, ticket.status,
      ticket.what_happened, ticket.raw_text, ticket.location, ticket.position_received_at,
    ]);
    const previous = markerByCode.get(ticket.ticket_code);
    if (previous?.signature === signature) continue;
    previous?.marker.remove();

    const pin = document.createElement("button");
    pin.type = "button";
    pin.className = `ticket-marker ${ticket.urgency.toLowerCase()}`;
    pin.setAttribute("aria-label", `Show ${ticket.ticket_code} on map`);
    const popup = new maplibregl.Popup({ offset: 25, maxWidth: "320px" })
      .setDOMContent(makePopup(ticket));
    const marker = new maplibregl.Marker({ element: pin, anchor: "bottom" })
      .setLngLat(coordinates(ticket))
      .setPopup(popup)
      .addTo(map);
    markerByCode.set(ticket.ticket_code, { marker, signature });
  }

  if (located.length && !initialFitDone) {
    fitLocatedTickets();
    initialFitDone = true;
  }
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
  map.fitBounds(bounds, { padding: 70, maxZoom: 13, duration: 700 });
}

function focusTicket(code) {
  const entry = markerByCode.get(code);
  if (!entry || !mapReady) return;
  const position = entry.marker.getLngLat();
  map.flyTo({ center: [position.lng, position.lat], zoom: Math.max(map.getZoom(), 12), essential: true });
  if (!entry.marker.getPopup().isOpen()) entry.marker.togglePopup();
  document.querySelector(".map-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function addCell(row, value, className) {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = value == null ? "" : String(value);
  row.append(cell);
  return cell;
}

function renderRows(tickets) {
  const fragment = document.createDocumentFragment();
  for (const ticket of tickets) {
    const row = document.createElement("tr");
    addCell(row, ticket.ticket_code, "ticket-code");

    const urgencyCell = addCell(row, ticket.urgency, `urgency ${ticket.urgency.toLowerCase()}`);
    if (ticket.raised_by_keyword) {
      const tag = document.createElement("span");
      tag.className = "small-note";
      tag.textContent = "keyword raised";
      urgencyCell.append(tag);
    }
    addCell(row, ticket.node_id, "node-id");
    addCell(row, ticket.what_happened || ticket.raw_text || "");
    addCell(row, ticket.location || "Not specified");
    addCell(row, ticket.injured_count);
    addCell(row, ticket.status);

    const mapCell = document.createElement("td");
    if (coordinates(ticket)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "locate-button";
      button.textContent = "Locate sender";
      button.addEventListener("click", () => focusTicket(ticket.ticket_code));
      mapCell.append(button);
    } else {
      const label = document.createElement("span");
      label.className = "unlocated";
      label.textContent = "No radio GPS";
      mapCell.append(label);
    }
    row.append(mapCell);
    fragment.append(row);
  }
  elements.rows.replaceChildren(fragment);
}

function emergencyScript(ticket) {
  const report = ticket.what_happened || ticket.raw_text || "Details not provided";
  const place = ticket.location || "Location not provided";
  const people = Number.isInteger(ticket.injured_count)
    ? `The report mentions ${ticket.injured_count} injured ${ticket.injured_count === 1 ? "person" : "people"}.`
    : "The number of injured people is unknown.";
  return `I am calling from a field coordination point. We received an emergency report, ticket ${ticket.ticket_code}. ${report}. The reported location is ${place}. ${people} The reporting radio node is ${ticket.node_id}. This is an unverified field report. Please advise and send assistance.`;
}

function renderEmergencyCards(tickets) {
  const emergencies = tickets.filter((ticket) => ticket.urgency === "EMERGENCY" && ticket.status === "open");
  elements.emergencyPanel.hidden = emergencies.length === 0;
  const fragment = document.createDocumentFragment();

  for (const ticket of emergencies) {
    const card = document.createElement("article");
    card.className = "emergency-card";

    const heading = document.createElement("div");
    heading.className = "emergency-card-heading";
    const title = document.createElement("h3");
    title.textContent = `Prepare 911 call · ${ticket.ticket_code}`;
    const received = document.createElement("span");
    received.textContent = `Received ${formatTime(ticket.received_at)}`;
    heading.append(title, received);

    const facts = document.createElement("p");
    facts.className = "emergency-facts";
    facts.textContent = `${ticket.location || "Location not provided"} · ${ticket.what_happened || ticket.raw_text || "Details not provided"}`;

    const scriptLabel = document.createElement("p");
    scriptLabel.className = "script-label";
    scriptLabel.textContent = "Read this script to the dispatcher";
    const script = document.createElement("blockquote");
    script.className = "call-script";
    script.textContent = emergencyScript(ticket);

    const actionRow = document.createElement("div");
    actionRow.className = "emergency-action-row";
    const note = document.createElement("p");
    note.className = "human-call-note";
    note.textContent = "You must place the call yourself.";
    const calledButton = document.createElement("button");
    calledButton.type = "button";
    calledButton.className = "called-button";
    calledButton.textContent = "I called 911";
    calledButton.addEventListener("click", () => markEmergencyAssigned(ticket.ticket_code, calledButton));
    actionRow.append(note, calledButton);

    card.append(heading, facts, scriptLabel, script, actionRow);
    fragment.append(card);
  }
  elements.emergencyCards.replaceChildren(fragment);
}

async function markEmergencyAssigned(ticketCode, button) {
  button.disabled = true;
  try {
    const response = await fetch(`/api/tickets/${encodeURIComponent(ticketCode)}/status?status=assigned`, {
      method: "POST",
    });
    if (!response.ok) throw new Error("Could not update ticket status");
    button.textContent = "Marked assigned";
    await poll();
  } catch (error) {
    button.disabled = false;
    button.textContent = "I called 911";
    elements.lastUpdated.textContent = "Could not update ticket status";
    console.error("Ticket status update failed", error);
  }
}

function updateBoard(tickets) {
  latestTickets = [...tickets].sort((a, b) =>
    (URGENCY_ORDER[a.urgency] ?? 99) - (URGENCY_ORDER[b.urgency] ?? 99) ||
    String(b.received_at).localeCompare(String(a.received_at)));
  const located = latestTickets.filter((ticket) => coordinates(ticket)).length;
  elements.ticketCount.textContent = String(latestTickets.length);
  elements.mapSummary.textContent = `${located} of ${latestTickets.length} tickets have sender GPS · ${latestTickets.length - located} unlocated`;
  elements.mapEmpty.hidden = located > 0;
  elements.mapEmpty.textContent = mockMode
    ? "No mock tickets have GPS coordinates. Check the ticket fixture."
    : "No radio GPS positions yet. Run with FIRETOWER_MOCK=1 to preview the North Georgia demo points.";
  elements.fitMap.disabled = !located || !mapReady;
  syncMarkers(latestTickets);
  renderEmergencyCards(latestTickets);

  const signature = JSON.stringify(latestTickets);
  if (signature !== lastRenderedTickets) {
    renderRows(latestTickets);
    lastRenderedTickets = signature;
  }
  elements.lastUpdated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

async function poll() {
  try {
    const [statusRes, ticketsRes] = await Promise.all([
      fetch("/api/status", { cache: "no-store" }),
      fetch("/api/tickets", { cache: "no-store" }),
    ]);
    if (!statusRes.ok || !ticketsRes.ok) throw new Error("Board API unavailable");
    const [status, tickets] = await Promise.all([statusRes.json(), ticketsRes.json()]);
    if (!Array.isArray(tickets)) throw new Error("Ticket API returned invalid data");

    elements.banner.textContent = status.online ? "ONLINE" : "OFFLINE · LOCAL TRIAGE";
    elements.banner.className = `badge ${status.online ? "badge-online" : "badge-offline"}`;
    mockMode = Boolean(status.mock);
    elements.mockBadge.hidden = !status.mock;
    updateBoard(tickets);
  } catch (error) {
    elements.banner.textContent = "BOARD CONNECTION LOST";
    elements.banner.className = "badge badge-offline";
    elements.lastUpdated.textContent = "Could not refresh tickets";
    console.error("Board poll failed", error);
  }
}

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
      if (!mapStyleLoaded) showMapMessage("Map tiles need an internet connection. Ticket pins remain available.");
      console.warn("Map resource failed", event.error);
    });
    window.setTimeout(() => {
      if (!mapStyleLoaded) showMapMessage("Map tiles are taking too long to load. Ticket pins remain available.");
    }, 15000);
  } catch (error) {
    console.error("Map setup failed", error);
    showMapMessage("Map unavailable in this browser. Tickets remain below.");
  }
}

elements.fitMap.addEventListener("click", fitLocatedTickets);
poll().finally(startMap);
window.setInterval(poll, POLL_INTERVAL_MS);
