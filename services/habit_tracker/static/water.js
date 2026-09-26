"use strict";

const config = window.habitConfig;
const api = path => `${config.basePath}/api/water${path}`;
const state = {today:null,analysisDay:null,selectedDate:null,selectedType:"water",temperature:"normal",sweetness:"regular",analysisBreakdown:"drink_type",editingGoal:false};
const message = document.querySelector("#message");
const typeMap = Object.fromEntries(config.drinkTypes.map(type => [type.value, type.label]));
const sweetened = new Set(config.sweetenedTypes);
const typeColors = {water:"#56d8ff",supplement_water:"#8eeaff",coffee:"#d59b6a",tea:"#8bcf7b",milk:"#eee8d5",protein_shake:"#c6a57a",juice:"#ffb454",soft_drink:"#d68cff",sports_drink:"#62e6b5",alcohol:"#ff7474",other:"#9b9b9b"};
const temperatureColors = {hot:"#ff8b5c",normal:"#8eeaff",iced:"#7aa7ff"};
const temperatureLabels = {hot:"Hot",normal:"Normal",iced:"Iced"};
const sweetnessLabels = {none:"No sugar",less:"Less sweet",regular:"Regular sweet",extra:"Extra sweet"};
document.querySelector("#user").textContent = config.displayName;
document.querySelector("#timezone").textContent = config.timezone;

async function request(path, options = {}) {
  const response = await fetch(api(path), {headers:{"Content-Type":"application/json"}, ...options});
  if (!response.ok) { let detail = `Request failed (${response.status})`; try { detail = (await response.json()).detail || detail; } catch {} throw new Error(detail); }
  return response.status === 204 ? null : response.json();
}
function busy(value) { document.querySelectorAll("button").forEach(button => { button.disabled = value; }); if (!value && state.today) document.querySelector("#next-day").disabled = state.selectedDate >= state.today.date; }
function show(text, isError = false) { message.textContent = text; message.style.color = isError ? "var(--danger)" : "var(--muted)"; }
function kindStyle(type) { return `--kind:${typeColors[type] || typeColors.other}`; }
function localDate(value) { return new Date(`${value}T12:00:00`); }
function isoDate(value) { const year = value.getFullYear(); const month = String(value.getMonth() + 1).padStart(2, "0"); const day = String(value.getDate()).padStart(2, "0"); return `${year}-${month}-${day}`; }
function moveDate(value, offset) { const day = localDate(value); day.setDate(day.getDate() + offset); return isoDate(day); }
function displayDate(value) { return localDate(value).toLocaleDateString([], {weekday:"long",day:"numeric",month:"long",year:"numeric"}); }

function renderTypes() {
  const root = document.querySelector("#drink-types"); root.innerHTML = "";
  for (const type of config.drinkTypes) { const button = document.createElement("button"); button.type = "button"; button.dataset.type = type.value; button.className = type.value === state.selectedType ? "active" : ""; button.setAttribute("aria-pressed", String(type.value === state.selectedType)); button.style.cssText = kindStyle(type.value); button.textContent = type.label; root.append(button); }
  const enabled = sweetened.has(state.selectedType); const group = document.querySelector("#sweetness-group"); group.classList.toggle("unavailable", !enabled); group.toggleAttribute("inert", !enabled); group.setAttribute("aria-disabled", String(!enabled));
}
function renderAttributes() {
  document.querySelectorAll("[data-temperature]").forEach(button => { const active = button.dataset.temperature === state.temperature; button.style.cssText = `--kind:${temperatureColors[button.dataset.temperature]}`; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
  document.querySelectorAll("[data-sweetness]").forEach(button => { const active = button.dataset.sweetness === state.sweetness; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
}
function renderBreakdown(breakdown, labels, colors) {
  const root = document.querySelector("#analysis-breakdown"); root.innerHTML = "";
  const values = Object.entries(breakdown).filter(([, amount]) => amount > 0).sort((a, b) => b[1] - a[1]);
  if (!values.length) { root.innerHTML = '<span class="empty-mix">No drinks recorded.</span>'; return; }
  const maximum = values[0][1];
  for (const [key, amount] of values) { const item = document.createElement("div"); item.className = "breakdown-item"; item.style.cssText = `--kind:${colors[key] || typeColors.other}`; item.innerHTML = `<div class="breakdown-label"><span><span class="dot"></span>${labels[key] || key}</span><strong>${amount.toLocaleString()} ml</strong></div><span class="breakdown-track"><span style="width:${(amount / maximum) * 100}%"></span></span>`; root.append(item); }
}
function renderToday(data) {
  state.today = data;
  document.querySelector("#total").textContent = data.total_ml.toLocaleString(); document.querySelector("#goal-value").textContent = data.goal_ml.toLocaleString();
  if (!state.editingGoal) document.querySelector("#goal-input").value = data.goal_ml;
  document.querySelector("#fill").style.width = `${Math.min(100, (data.total_ml / data.goal_ml) * 100)}%`;
  document.querySelector("#entry-count").textContent = `${data.drinks.length} ${data.drinks.length === 1 ? "entry" : "entries"}`;
  const entries = document.querySelector("#entries"); entries.innerHTML = "";
  if (!data.drinks.length) { entries.innerHTML = '<div class="empty">No drinks recorded yet.</div>'; return; }
  for (const drink of data.drinks) { const row = document.createElement("div"); row.className = "entry"; const localTime = new Date(drink.consumed_at).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}); const details = [temperatureLabels[drink.temperature] || "Normal"]; if (drink.sweetness) details.push(sweetnessLabels[drink.sweetness] || drink.sweetness); row.innerHTML = `<div><div class="entry-amount">${drink.amount_ml} ml <span class="entry-kind" style="${kindStyle(drink.drink_type)}"><span class="dot"></span>${typeMap[drink.drink_type] || "Other"}</span></div><div class="entry-time">${localTime} <span class="entry-details">· ${details.join(" · ")}</span></div></div><button class="undo" data-delete="${drink.id}">UNDO</button>`; entries.append(row); }
}
function renderHistory(data) {
  const root = document.querySelector("#history"); root.innerHTML = "";
  for (const day of data.days.slice(-7)) { const height = Math.min(100, (day.total_ml / day.goal_ml) * 100); const button = document.createElement("button"); button.type = "button"; button.className = `day${day.date === state.selectedDate ? " selected" : ""}`; button.dataset.date = day.date; button.title = `${day.date}: ${day.total_ml} / ${day.goal_ml} ml`; button.innerHTML = `<span class="bar-track"><span class="bar" style="height:${height}%"></span></span><span class="day-name">${localDate(day.date).toLocaleDateString([], {weekday:"narrow"})}</span>`; root.append(button); }
}
function renderAnalysis(data) {
  state.analysisDay = data;
  state.selectedDate = data.date;
  const input = document.querySelector("#analysis-date"); input.value = data.date; input.max = state.today.date;
  document.querySelector("#analysis-date-label").textContent = displayDate(data.date); document.querySelector("#analysis-total").textContent = data.total_ml.toLocaleString();
  document.querySelector("#analysis-progress").textContent = `${data.entry_count} ${data.entry_count === 1 ? "entry" : "entries"} · ${Math.round((data.total_ml / data.goal_ml) * 100)}% of current target`;
  document.querySelector("#next-day").disabled = data.date >= state.today.date;
  document.querySelectorAll("[data-breakdown]").forEach(button => { const active = button.dataset.breakdown === state.analysisBreakdown; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
  if (state.analysisBreakdown === "temperature") renderBreakdown(data.temperature_breakdown_ml, temperatureLabels, temperatureColors);
  else renderBreakdown(data.breakdown_ml, typeMap, typeColors);
}
async function selectDate(value) { const [day, history] = await Promise.all([request(`/day?date=${encodeURIComponent(value)}`), request("/history?days=7")]); renderAnalysis(day); renderHistory(history); }
function editGoal(editing) { state.editingGoal = editing; document.querySelector("#goal-trigger").hidden = editing; document.querySelector("#goal-form").hidden = !editing; if (editing) { const input = document.querySelector("#goal-input"); input.value = state.today.goal_ml; input.focus(); input.select(); } }
async function refresh() { const [today, history] = await Promise.all([request("/today"), request("/history?days=7")]); renderToday(today); if (!state.selectedDate) state.selectedDate = today.date; renderAnalysis(await request(`/day?date=${encodeURIComponent(state.selectedDate)}`)); renderHistory(history); }
async function add(amount) { busy(true); show("Saving…"); const payload = {amount_ml:Number(amount),drink_type:state.selectedType,temperature:state.temperature}; if (sweetened.has(state.selectedType)) payload.sweetness = state.sweetness; try { await request("/drinks", {method:"POST",body:JSON.stringify(payload)}); await refresh(); show(`${typeMap[state.selectedType]} saved.`); } catch (error) { show(error.message, true); } finally { busy(false); } }

document.querySelector("#drink-types").addEventListener("click", event => { const type = event.target.dataset.type; if (!type) return; state.selectedType = type; renderTypes(); });
document.querySelector("#temperature-options").addEventListener("click", event => { const value = event.target.dataset.temperature; if (!value) return; state.temperature = value; renderAttributes(); });
document.querySelector("#sweetness-options").addEventListener("click", event => { const value = event.target.dataset.sweetness; if (!value || !sweetened.has(state.selectedType)) return; state.sweetness = value; renderAttributes(); });
document.querySelector("#analysis-switch").addEventListener("click", event => { const breakdown = event.target.dataset.breakdown; if (!breakdown || !state.analysisDay) return; state.analysisBreakdown = breakdown; renderAnalysis(state.analysisDay); });
document.querySelectorAll("[data-amount]").forEach(button => button.addEventListener("click", () => add(button.dataset.amount)));
document.querySelector("#custom-form").addEventListener("submit", event => { event.preventDefault(); const input = document.querySelector("#custom-amount"); add(input.value).then(() => { input.value = ""; }); });
document.querySelector("#goal-trigger").addEventListener("click", () => editGoal(true)); document.querySelector("#goal-input").addEventListener("keydown", event => { if (event.key === "Escape") editGoal(false); });
document.querySelector("#goal-form").addEventListener("submit", async event => { event.preventDefault(); busy(true); try { await request("/settings", {method:"PUT",body:JSON.stringify({daily_goal_ml:Number(document.querySelector("#goal-input").value)})}); editGoal(false); await refresh(); show("Daily target updated."); } catch (error) { show(error.message, true); } finally { busy(false); } });
document.querySelector("#entries").addEventListener("click", async event => { const id = event.target.dataset.delete; if (!id) return; busy(true); try { await request(`/drinks/${id}`, {method:"DELETE"}); await refresh(); show("Entry removed."); } catch (error) { show(error.message, true); } finally { busy(false); } });
document.querySelector("#history").addEventListener("click", event => { const day = event.target.closest("[data-date]"); if (day) selectDate(day.dataset.date).catch(error => show(error.message, true)); });
document.querySelector("#analysis-date").addEventListener("change", event => selectDate(event.target.value).catch(error => show(error.message, true)));
document.querySelector("#previous-day").addEventListener("click", () => selectDate(moveDate(state.selectedDate, -1)).catch(error => show(error.message, true))); document.querySelector("#next-day").addEventListener("click", () => selectDate(moveDate(state.selectedDate, 1)).catch(error => show(error.message, true))); document.querySelector("#today-day").addEventListener("click", () => selectDate(state.today.date).catch(error => show(error.message, true)));
renderTypes(); renderAttributes(); refresh().catch(error => show(error.message, true));
