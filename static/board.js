const POLL_INTERVAL_MS = 2000;

async function poll() {
  const [statusRes, ticketsRes] = await Promise.all([
    fetch("/api/status"),
    fetch("/api/tickets"),
  ]);
  const status = await statusRes.json();
  const tickets = await ticketsRes.json();

  const banner = document.getElementById("banner");
  banner.textContent = status.online ? "ONLINE" : "OFFLINE — triage still running locally";
  banner.className = status.online ? "online" : "offline";

  const rows = document.getElementById("rows");
  rows.innerHTML = tickets
    .map(
      (t) => `
    <tr>
      <td>${t.ticket_code}</td>
      <td class="${t.urgency}">${t.urgency}${t.raised_by_keyword ? " (keyword)" : ""}</td>
      <td>${t.node_id}</td>
      <td>${t.what_happened ?? ""}</td>
      <td>${t.location ?? ""}</td>
      <td>${t.injured_count ?? ""}</td>
      <td>${t.status}</td>
    </tr>`
    )
    .join("");
}

setInterval(poll, POLL_INTERVAL_MS);
poll();
