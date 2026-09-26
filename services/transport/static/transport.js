(() => {
  "use strict";
  const config = window.TRANSPORT_CONFIG;
  const base = config.basePath || "";
  const $ = (id) => document.getElementById(id);
  const state = { buses: [], arrivals: new Map() };

  $("today").textContent = new Date(`${config.today}T12:00:00`).toLocaleDateString("en-SG", {
    weekday: "long", day: "numeric", month: "long"
  });

  async function api(path, options = {}) {
    const response = await fetch(`${base}${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) }
    });
    if (!response.ok) {
      let message = "Something went wrong";
      try { message = (await response.json()).detail || message; } catch (_) { /* response was not JSON */ }
      throw new Error(message);
    }
    return response.status === 204 ? null : response.json();
  }

  function option(value, label) {
    const item = document.createElement("option");
    item.value = value;
    item.textContent = label;
    return item;
  }

  function fill(select, items, placeholder, map) {
    select.replaceChildren(option("", placeholder), ...items.map(map));
    select.disabled = items.length === 0;
  }

  function snapshotText(snapshot) {
    if (!snapshot) return "No timetable saved yet.";
    const when = new Date(snapshot.refreshed_at).toLocaleString("en-SG", { dateStyle: "medium", timeStyle: "short" });
    return `Saved locally · refreshed ${when}`;
  }

  async function loadTrain() {
    const data = await api("/api/train");
    $("train-snapshot").textContent = snapshotText(data.snapshot);
    fill($("line"), data.lines, data.lines.length ? "Choose line" : "Refresh first", (line) => option(line.short_name, `${line.short_name} · ${line.long_name}`));
  }

  $("line").addEventListener("change", async (event) => {
    fill($("station"), [], "Select a direction", () => null);
    $("find-train").disabled = true;
    if (!event.target.value) return fill($("direction"), [], "Select a line", () => null);
    const data = await api(`/api/train/directions?line=${encodeURIComponent(event.target.value)}`);
    fill($("direction"), data.directions, "Choose direction", (direction) => option(JSON.stringify(direction), `Towards ${direction.headsign}`));
  });

  $("direction").addEventListener("change", async (event) => {
    $("find-train").disabled = true;
    if (!event.target.value) return fill($("station"), [], "Select a direction", () => null);
    const direction = JSON.parse(event.target.value);
    const query = new URLSearchParams({ line: $("line").value, direction_id: direction.direction_id, headsign: direction.headsign });
    const data = await api(`/api/train/stations?${query}`);
    fill($("station"), data.stations, "Choose station", (station) => option(station.stop_code, `${station.stop_code} · ${station.stop_name}`));
  });

  $("station").addEventListener("change", (event) => { $("find-train").disabled = !event.target.value; });

  $("find-train").addEventListener("click", async () => {
    const direction = JSON.parse($("direction").value);
    const query = new URLSearchParams({ line: $("line").value, direction_id: direction.direction_id, headsign: direction.headsign, stop_code: $("station").value });
    const result = $("train-result");
    try {
      const data = await api(`/api/train/last?${query}`);
      result.classList.remove("empty");
      const time = document.createElement("div");
      time.className = "train-time";
      time.textContent = data.time;
      const copy = document.createElement("div");
      copy.className = "train-copy";
      const station = document.createElement("strong");
      station.textContent = data.stop_name;
      const directionText = document.createElement("span");
      directionText.textContent = `${data.line} towards ${data.headsign}`;
      copy.append(station, directionText);
      if (data.day_offset) {
        const offset = document.createElement("span");
        offset.textContent = "after midnight · following day";
        copy.append(offset);
      }
      result.replaceChildren(time, copy);
    } catch (error) {
      result.classList.add("empty"); result.textContent = error.message;
    }
  });

  $("refresh-trains").addEventListener("click", async (event) => {
    event.target.disabled = true; event.target.textContent = "Refreshing…";
    try { const data = await api("/api/train/refresh", { method: "POST" }); $("train-snapshot").textContent = snapshotText(data.snapshot); $("train-snapshot").classList.remove("error"); await loadTrain(); }
    catch (error) { $("train-snapshot").textContent = error.message; $("train-snapshot").classList.add("error"); }
    finally { event.target.disabled = false; event.target.textContent = "Refresh timetable"; }
  });

  function renderBuses() {
    const list = $("bus-list");
    if (!state.buses.length) { list.innerHTML = '<div class="bus-empty">Save a stop and bus to keep it here.</div>'; return; }
    list.replaceChildren(...state.buses.map((bus) => {
      const row = document.createElement("div"); row.className = "bus-row";
      const minutes = state.arrivals.get(bus.id);
      const primary = minutes && minutes[0] !== null ? (minutes[0] === 0 ? "Arr" : `${minutes[0]} min`) : "—";
      const later = minutes ? minutes.slice(1).filter((value) => value !== null).map((value) => value === 0 ? "Arr" : `${value}`).join(" · ") : "refresh";
      row.innerHTML = `<div class="bus-number"></div><div class="bus-place"><strong></strong><span></span></div><div class="arrivals"><strong></strong><br><span></span></div><button class="remove" aria-label="Remove saved bus">×</button>`;
      row.querySelector(".bus-number").textContent = bus.service_no;
      row.querySelector(".bus-place strong").textContent = bus.stop_name;
      row.querySelector(".bus-place span").textContent = `Stop ${bus.stop_code}`;
      row.querySelector(".arrivals strong").textContent = primary;
      row.querySelector(".arrivals span").textContent = later;
      row.querySelector(".remove").addEventListener("click", async () => { await api(`/api/buses/${bus.id}`, { method: "DELETE" }); state.buses = state.buses.filter((item) => item.id !== bus.id); renderBuses(); });
      return row;
    }));
  }

  async function loadBuses() { state.buses = (await api("/api/buses")).buses; renderBuses(); }

  $("bus-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    try {
      const saved = await api("/api/buses", { method: "POST", body: JSON.stringify(Object.fromEntries(form)) });
      state.buses.push(saved); event.target.reset(); renderBuses();
    } catch (error) { $("bus-snapshot").textContent = error.message; $("bus-snapshot").classList.add("error"); }
  });

  $("refresh-buses").addEventListener("click", async (event) => {
    event.target.disabled = true; event.target.textContent = "Refreshing…";
    try {
      const data = await api("/api/buses/refresh", { method: "POST" });
      state.arrivals = new Map(data.buses.map((bus) => [bus.id, bus.minutes]));
      $("bus-snapshot").textContent = `Updated ${new Date(data.observed_at).toLocaleTimeString("en-SG", { hour: "2-digit", minute: "2-digit" })}`;
      $("bus-snapshot").classList.remove("error"); renderBuses();
    } catch (error) { $("bus-snapshot").textContent = error.message; $("bus-snapshot").classList.add("error"); }
    finally { event.target.disabled = false; event.target.textContent = "Refresh arrivals"; }
  });

  Promise.all([loadTrain(), loadBuses()]).catch((error) => { $("bus-snapshot").textContent = error.message; });
})();
