"use strict";

const config = window.habitConfig;
const api = path => `${config.basePath}/api/study${path}`;
const state = {active:null, endClock:0, startClock:0, durationMs:0, completing:false, busy:false, todayDate:null, selectedDate:null, calendarMonth:null, calendarDays:[]};
const message = document.querySelector("#message");
document.querySelector("#user").textContent = config.displayName;

async function request(path, options = {}) {
  const response = await fetch(api(path), {headers:{"Content-Type":"application/json"}, ...options});
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}
function show(text, error = false) { message.textContent = text; message.style.color = error ? "var(--danger)" : "var(--muted)"; }
function duration(seconds) {
  if (seconds < 60) return `${seconds} sec`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  const hours = Math.floor(minutes / 60);
  const parts = [];
  if (hours) parts.push(`${hours} hr`);
  if (minutes % 60 || !hours) parts.push(`${minutes % 60} min`);
  if (remainder) parts.push(`${remainder} sec`);
  return parts.join(" ");
}
function clockText(milliseconds) {
  const seconds = Math.ceil(Math.max(0, milliseconds) / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
function busy(value) {
  state.busy = value;
  document.querySelectorAll("button,input").forEach(element => { element.disabled = value; });
  if (!value && state.calendarMonth) renderCalendar();
}
function localDate(value) { return new Date(`${value}T12:00:00`); }
function monthKey(value) { return value.slice(0, 7); }
function shiftMonth(value, offset) { const [year, month] = value.split("-").map(Number); const shifted = new Date(year, month - 1 + offset, 1, 12); return `${shifted.getFullYear()}-${String(shifted.getMonth() + 1).padStart(2, "0")}`; }
function lastDayOfMonth(value) { const [year, month] = value.split("-").map(Number); return new Date(year, month, 0, 12).getDate(); }
function displayDate(value) { return localDate(value).toLocaleDateString([], {weekday:"long",day:"numeric",month:"long",year:"numeric"}); }
function focusMinutes(seconds) { return seconds < 60 ? "<1 min" : `${Math.floor(seconds / 60)} min`; }
function renderCalendar() {
  const month = state.calendarMonth;
  const [year, number] = month.split("-").map(Number);
  const firstWeekday = (new Date(year, number - 1, 1, 12).getDay() + 6) % 7;
  const daysInMonth = lastDayOfMonth(month);
  const summaries = new Map(state.calendarDays.map(day => [day.date, day.total_seconds]));
  const maximum = Math.max(0, ...state.calendarDays.map(day => day.total_seconds));
  document.querySelector("#calendar-month").textContent = localDate(`${month}-01`).toLocaleDateString([], {month:"long",year:"numeric"});
  document.querySelector("#previous-month").disabled = state.busy;
  document.querySelector("#next-month").disabled = state.busy || month >= monthKey(state.todayDate);
  const selectedSeconds = summaries.get(state.selectedDate) || 0;
  document.querySelector("#selected-date").textContent = displayDate(state.selectedDate);
  document.querySelector("#selected-total").textContent = selectedSeconds ? duration(selectedSeconds) : "0 min";
  const root = document.querySelector("#focus-calendar"); root.replaceChildren();
  for (let index = 0; index < firstWeekday; index++) { const spacer = document.createElement("span"); spacer.className = "calendar-spacer"; spacer.setAttribute("aria-hidden", "true"); root.append(spacer); }
  for (let day = 1; day <= daysInMonth; day++) {
    const date = `${month}-${String(day).padStart(2, "0")}`;
    const seconds = summaries.get(date) || 0;
    const button = document.createElement("button"); button.type = "button"; button.dataset.date = date;
    button.className = `calendar-day${seconds ? " focused" : ""}${date === state.selectedDate ? " selected" : ""}${date === state.todayDate ? " today" : ""}`;
    button.disabled = state.busy || date > state.todayDate;
    button.setAttribute("aria-pressed", String(date === state.selectedDate));
    button.setAttribute("aria-label", `${displayDate(date)}: ${seconds ? focusMinutes(seconds) + " saved" : date > state.todayDate ? "future day" : "no focus saved"}`);
    if (seconds) button.style.setProperty("--focus-level", `${Math.round(12 + 26 * seconds / maximum)}%`);
    const numberLabel = document.createElement("span"); numberLabel.textContent = day;
    const timeLabel = document.createElement("span"); timeLabel.className = "calendar-minutes"; timeLabel.textContent = seconds ? focusMinutes(seconds).replace(" min", "m") : "";
    button.append(numberLabel, timeLabel); root.append(button);
  }
  for (let index = firstWeekday + daysInMonth; index < 42; index++) { const spacer = document.createElement("span"); spacer.className = "calendar-spacer"; spacer.setAttribute("aria-hidden", "true"); root.append(spacer); }
}
async function loadCalendar(month, selectedDate) {
  const lastDay = lastDayOfMonth(month);
  const end = month === monthKey(state.todayDate) ? state.todayDate : `${month}-${String(lastDay).padStart(2, "0")}`;
  const days = Number(end.slice(-2));
  const data = await request(`/history?days=${days}&end=${end}`);
  state.calendarMonth = month; state.calendarDays = data.days; state.selectedDate = selectedDate;
  renderCalendar();
}
async function changeMonth(offset) {
  if (state.busy) return;
  const month = shiftMonth(state.calendarMonth, offset);
  const day = Math.min(Number(state.selectedDate.slice(-2)), lastDayOfMonth(month));
  const candidate = `${month}-${String(day).padStart(2, "0")}`;
  busy(true);
  try { await loadCalendar(month, candidate > state.todayDate ? state.todayDate : candidate); }
  catch (error) { show(error.message, true); }
  finally { busy(false); }
}
function renderRecent(sessions) {
  const root = document.querySelector("#recent"); root.replaceChildren();
  if (!sessions.length) { const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "No saved focus sessions yet."; root.append(empty); return; }
  for (const session of sessions) {
    const row = document.createElement("div"); row.className = "recent-row";
    const main = document.createElement("div");
    const name = document.createElement("div"); name.className = "recent-name"; name.textContent = session.activity;
    const meta = document.createElement("div"); meta.className = "recent-meta";
    const ended = new Date(session.ended_at).toLocaleString([], {day:"numeric",month:"short",hour:"2-digit",minute:"2-digit",timeZone:config.timezone});
    meta.textContent = `${session.status === "completed" ? "Completed" : "Stopped"} · ${ended}`;
    const time = document.createElement("div"); time.className = "recent-time"; time.textContent = duration(session.elapsed_seconds);
    main.append(name, meta); row.append(main, time); root.append(row);
  }
}
function tick() {
  if (!state.active) return;
  const remaining = Math.max(0, state.endClock - performance.now());
  document.querySelector("#countdown").textContent = clockText(remaining);
  document.querySelector("#timer-fill").style.width = `${Math.min(100, remaining / state.durationMs * 100)}%`;
  if (remaining === 0 && !state.completing) {
    state.completing = true;
    refresh().then(() => { if (!state.active) show("Focus session completed."); }).catch(error => show(error.message, true)).finally(() => { state.completing = false; });
  }
}
function render(data) {
  state.active = data.active;
  state.todayDate = data.date;
  if (!state.selectedDate) state.selectedDate = data.date;
  document.querySelector("#today-date").textContent = `Today · ${new Date(`${data.date}T12:00:00`).toLocaleDateString([], {day:"numeric",month:"long"})}`;
  document.querySelector("#today-total").textContent = duration(data.today_seconds);
  document.querySelector("#focus-heading").textContent = data.active ? "Focus in progress." : "Settle in and study.";
  document.querySelector("#focus-intro").textContent = data.active ? "Your session keeps going if you leave this page." : "Pick one activity and a focus length. Your session continues if you leave this page.";
  document.querySelector("#start-form").hidden = Boolean(data.active);
  document.querySelector("#active-session").hidden = !data.active;
  renderRecent(data.recent);
  if (data.active) {
    document.querySelector("#active-activity").textContent = data.active.activity;
    state.startClock = performance.now();
    state.endClock = state.startClock + Math.max(0, Date.parse(data.active.planned_end_at) - Date.parse(data.server_now));
    state.durationMs = data.active.duration_minutes * 60000;
    tick();
  }
}
async function refresh() { render(await request("/summary")); await loadCalendar(state.calendarMonth || monthKey(state.selectedDate), state.selectedDate); }
async function transition(action) {
  if (!state.active || state.busy) return;
  const sessionId = state.active.id;
  busy(true);
  try {
    await request(`/sessions/${encodeURIComponent(sessionId)}/${action}`, {method:"POST"});
    await refresh();
    show(action === "stop" ? "Focus time saved." : "Session cancelled.");
  } catch (error) {
    await refresh().catch(() => {});
    show(error.message, true);
  } finally { busy(false); }
}
document.querySelector("#start-form").addEventListener("submit", async event => {
  event.preventDefault(); if (state.busy) return;
  const activity = document.querySelector("#activity").value.trim();
  const durationMinutes = Number(document.querySelector("#duration").value);
  busy(true);
  try {
    await request("/sessions", {method:"POST",body:JSON.stringify({activity,duration_minutes:durationMinutes})});
    await refresh(); show("Focus session started.");
  } catch (error) { await refresh().catch(() => {}); show(error.message, true); }
  finally { busy(false); }
});
document.querySelector("#stop").addEventListener("click", () => transition("stop"));
document.querySelector("#cancel").addEventListener("click", () => transition("cancel"));
document.querySelector("#focus-calendar").addEventListener("click", event => { const day = event.target.closest("[data-date]"); if (!day || day.disabled) return; state.selectedDate = day.dataset.date; renderCalendar(); });
document.querySelector("#previous-month").addEventListener("click", () => changeMonth(-1));
document.querySelector("#next-month").addEventListener("click", () => changeMonth(1));
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh().catch(error => show(error.message, true)); });
window.addEventListener("pageshow", event => { if (event.persisted) refresh().catch(error => show(error.message, true)); });
setInterval(tick, 250);
refresh().catch(error => show(error.message, true));
