"use strict";

const config = window.habitConfig;
const api = path => `${config.basePath}/api/budget${path}`;
const state = {kind:"daily_spend",category:"other",summary:null,selectedDate:null,editingBudget:false,resetTimer:null};
const categoryLabels = Object.fromEntries(config.categories.map(category => [category.value, category.label]));
const categorised = new Set(config.categorisedKinds);
const money = new Intl.NumberFormat("en-SG", {style:"currency",currency:config.currency,minimumFractionDigits:2});
const labels = {daily_spend:"Daily spending",fund_redemption:"Fund redemption",fund_contribution:"Fund contribution"};
const help = {daily_spend:"Counts against today’s allowance. Any remaining amount settles into the fund at midnight.",fund_redemption:"Pays directly from the sinking fund without changing today’s allowance.",fund_contribution:"Adds money directly to the sinking fund and records where it came from."};
document.querySelector("#user").textContent = config.displayName;
document.querySelector("#timezone").textContent = config.timezone;
const format = cents => money.format(cents / 100);
const signed = cents => `${cents >= 0 ? "+" : "−"}${format(Math.abs(cents))}`;
const localDate = value => new Date(`${value}T12:00:00`);
function isoDate(value) { const year = value.getFullYear(); const month = String(value.getMonth() + 1).padStart(2, "0"); const day = String(value.getDate()).padStart(2, "0"); return `${year}-${month}-${day}`; }
function moveDate(value, offset) { const day = localDate(value); day.setDate(day.getDate() + offset); return isoDate(day); }
function displayDate(value) { return localDate(value).toLocaleDateString([], {weekday:"long",day:"numeric",month:"long",year:"numeric"}); }

async function request(path, options = {}) {
  const response = await fetch(api(path), {headers:{"Content-Type":"application/json"}, ...options});
  if (!response.ok) { let detail = `Request failed (${response.status})`; try { detail = (await response.json()).detail || detail; } catch {} throw new Error(detail); }
  return response.status === 204 ? null : response.json();
}
function busy(value) { document.querySelectorAll("button").forEach(button => { button.disabled = value; }); if (!value && state.summary) document.querySelector("#next-day").disabled = state.selectedDate >= state.summary.date; }
function show(text, error = false) { const node = document.querySelector("#message"); node.textContent = text; node.style.color = error ? "var(--danger)" : "var(--muted)"; }
function renderMode() {
  document.querySelectorAll("[data-kind]").forEach(button => { const active = button.dataset.kind === state.kind; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
  document.querySelector("#mode-help").textContent = help[state.kind];
  const enabled = categorised.has(state.kind); const group = document.querySelector("#category-picker"); group.classList.toggle("unavailable", !enabled); group.toggleAttribute("inert", !enabled); group.setAttribute("aria-disabled", String(!enabled));
}
function renderCategories() {
  const root = document.querySelector("#categories"); root.innerHTML = "";
  for (const item of config.categories) { const button = document.createElement("button"); button.type = "button"; button.dataset.category = item.value; const active = item.value === state.category; button.className = active ? "active" : ""; button.setAttribute("aria-pressed", String(active)); button.textContent = item.label; root.append(button); }
}
function renderSummary(summary) {
  state.summary = summary;
  const remaining = document.querySelector("#remaining"); remaining.textContent = format(summary.daily_remaining_cents); remaining.classList.toggle("negative", summary.daily_remaining_cents < 0);
  document.querySelector("#budget-value").textContent = format(summary.daily_budget_cents); document.querySelector("#budget-figure").textContent = format(summary.daily_budget_cents); document.querySelector("#spent").textContent = format(summary.daily_spent_cents);
  const settlement = document.querySelector("#settlement"); settlement.textContent = signed(summary.daily_remaining_cents); settlement.className = summary.daily_remaining_cents < 0 ? "negative" : "positive";
  const fund = document.querySelector("#fund"); fund.textContent = format(summary.fund_balance_cents); fund.classList.toggle("negative", summary.fund_balance_cents < 0); document.querySelector("#pending").textContent = summary.pending_surplus_cents ? `+${format(summary.pending_surplus_cents)}` : "—";
  const ratio = summary.daily_budget_cents ? Math.max(0, summary.daily_remaining_cents) / summary.daily_budget_cents : 0; const fill = document.querySelector("#fill"); fill.style.width = `${Math.min(100, ratio * 100)}%`; fill.classList.toggle("over", summary.daily_remaining_cents < 0); fill.setAttribute("aria-valuenow", String(Math.max(0, summary.daily_remaining_cents))); fill.setAttribute("aria-valuemax", String(summary.daily_budget_cents));
  if (!state.editingBudget) document.querySelector("#budget-input").value = (summary.daily_budget_cents / 100).toFixed(2);
  renderActivity(summary.transactions); scheduleReset(summary.next_reset_at);
}
function renderActivity(transactions) {
  document.querySelector("#activity-count").textContent = `${transactions.length} ${transactions.length === 1 ? "entry" : "entries"}`; const root = document.querySelector("#activity"); root.innerHTML = "";
  if (!transactions.length) { root.innerHTML = '<div class="empty">Nothing recorded today.</div>'; return; }
  for (const item of transactions) { const row = document.createElement("div"); row.className = "entry"; const positive = item.kind === "fund_contribution"; const timestamp = new Date(item.occurred_at).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}); const meta = categorised.has(item.kind) ? `${labels[item.kind]} · ${categoryLabels[item.category] || "Other"} · ${timestamp}` : `${labels[item.kind]} · ${timestamp}`; row.innerHTML = `<div class="entry-main"><div class="entry-name"></div><div class="entry-meta">${meta}</div></div><div class="entry-side"><span class="entry-amount ${positive ? "positive" : ""}">${positive ? "+" : "−"}${format(item.amount_cents)}</span><button class="undo" data-delete="${item.id}">UNDO</button></div>`; row.querySelector(".entry-name").textContent = item.description; root.append(row); }
}
function renderHistory(data) {
  const root = document.querySelector("#history"); root.innerHTML = ""; const days = data.days.slice(-7); const ceiling = Math.max(1, ...days.map(day => Math.max(day.budget_cents, day.spent_cents)));
  for (const day of days) { const height = Math.max(2, Math.min(100, (day.spent_cents / ceiling) * 100)); const button = document.createElement("button"); button.type = "button"; button.className = `day${day.date === state.selectedDate ? " selected" : ""}`; button.dataset.date = day.date; button.title = `${day.date}: spent ${format(day.spent_cents)} of ${format(day.budget_cents)}`; button.innerHTML = `<span class="bar-track"><span class="bar ${day.spent_cents > day.budget_cents ? "over" : ""}" style="height:${height}%"></span></span><span class="day-value">${format(day.spent_cents)}</span><span class="day-name">${localDate(day.date).toLocaleDateString([], {weekday:"narrow"})}</span>`; root.append(button); }
}
function renderDay(data) {
  state.selectedDate = data.date;
  const input = document.querySelector("#analysis-date"); input.value = data.date; input.max = state.summary.date; document.querySelector("#analysis-date-label").textContent = displayDate(data.date);
  document.querySelector("#analysis-budget").textContent = format(data.budget_cents); document.querySelector("#analysis-spent").textContent = format(data.spent_cents);
  const remaining = document.querySelector("#analysis-remaining"); remaining.textContent = format(data.remaining_cents); remaining.className = data.remaining_cents < 0 ? "negative" : "positive"; document.querySelector("#next-day").disabled = data.date >= state.summary.date;
  const root = document.querySelector("#category-breakdown"); root.innerHTML = ""; const values = Object.entries(data.category_breakdown_cents).sort((a, b) => b[1] - a[1]);
  if (!values.length) { root.innerHTML = '<span class="breakdown-row empty-row">No daily spending</span>'; return; }
  for (const [category, amount] of values) { const row = document.createElement("span"); row.className = "breakdown-row"; row.innerHTML = `<span>${categoryLabels[category] || "Other"}</span><strong>${format(amount)}</strong>`; root.append(row); }
}
function renderLedger(data) {
  const root = document.querySelector("#ledger"); root.innerHTML = "";
  if (!data.entries.length) { root.innerHTML = '<div class="empty">No budget activity yet.</div>'; return; }
  for (const item of data.entries) { const row = document.createElement("div"); row.className = "ledger-row"; const positive = item.amount_cents >= 0; const adjustment = item.kind === "daily_budget_adjustment"; const note = adjustment ? `Daily budget ${format(item.previous_amount_cents)} → ${format(item.new_amount_cents)}` : item.description; const when = item.occurred_at ? new Date(item.occurred_at).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}) : item.date; row.innerHTML = `<span class="ledger-note"></span><span class="ledger-amount ${positive ? "positive" : "negative"}">${signed(item.amount_cents)}</span>`; row.querySelector(".ledger-note").textContent = `${when} · ${note}`; root.append(row); }
}
function scheduleReset(nextReset) { clearTimeout(state.resetTimer); const wait = Math.max(1000, new Date(nextReset).getTime() - Date.now() + 1000); state.resetTimer = setTimeout(refresh, Math.min(wait, 2147483647)); }
async function selectDate(value) { const [day, history] = await Promise.all([request(`/day?date=${encodeURIComponent(value)}`), request("/history?days=7")]); renderDay(day); renderHistory(history); }
async function refresh() { const [summary, history, ledger] = await Promise.all([request("/summary"),request("/history?days=7"),request("/ledger?limit=50")]); renderSummary(summary); if (!state.selectedDate) state.selectedDate = summary.date; renderDay(await request(`/day?date=${encodeURIComponent(state.selectedDate)}`)); renderHistory(history); renderLedger(ledger); }
function editBudget(editing) { state.editingBudget = editing; document.querySelector("#budget-trigger").hidden = editing; document.querySelector("#budget-form").hidden = !editing; if (editing) { const input = document.querySelector("#budget-input"); input.value = (state.summary.daily_budget_cents / 100).toFixed(2); input.focus(); input.select(); } }

document.querySelector("#modes").addEventListener("click", event => { const kind = event.target.dataset.kind; if (!kind) return; state.kind = kind; renderMode(); });
document.querySelector("#transaction-form").addEventListener("submit", async event => { event.preventDefault(); const amount = Math.round(Number(document.querySelector("#amount").value) * 100); const description = document.querySelector("#description").value; busy(true); show("Saving…"); try { await request("/transactions", {method:"POST",body:JSON.stringify({kind:state.kind,amount_cents:amount,description,category:categorised.has(state.kind) ? state.category : "other"})}); document.querySelector("#amount").value = ""; document.querySelector("#description").value = ""; await refresh(); show(`${labels[state.kind]} saved.`); } catch (error) { show(error.message, true); } finally { busy(false); } });
document.querySelector("#activity").addEventListener("click", async event => { const id = event.target.dataset.delete; if (!id || !confirm("Permanently remove this entry?")) return; busy(true); try { await request(`/transactions/${id}`, {method:"DELETE"}); await refresh(); show("Entry removed."); } catch (error) { show(error.message, true); } finally { busy(false); } });
document.querySelector("#budget-trigger").addEventListener("click", () => editBudget(true)); document.querySelector("#budget-input").addEventListener("keydown", event => { if (event.key === "Escape") editBudget(false); });
document.querySelector("#budget-form").addEventListener("submit", async event => { event.preventDefault(); const amount = Math.round(Number(document.querySelector("#budget-input").value) * 100); busy(true); try { await request("/daily-budget", {method:"PUT",body:JSON.stringify({amount_cents:amount})}); editBudget(false); await refresh(); show("Daily budget updated from today."); } catch (error) { show(error.message, true); } finally { busy(false); } });
document.querySelector("#categories").addEventListener("click", event => { const category = event.target.dataset.category; if (!category) return; state.category = category; renderCategories(); });
document.querySelector("#history").addEventListener("click", event => { const day = event.target.closest("[data-date]"); if (day) selectDate(day.dataset.date).catch(error => show(error.message, true)); });
document.querySelector("#analysis-date").addEventListener("change", event => selectDate(event.target.value).catch(error => show(error.message, true)));
document.querySelector("#previous-day").addEventListener("click", () => selectDate(moveDate(state.selectedDate, -1)).catch(error => show(error.message, true))); document.querySelector("#next-day").addEventListener("click", () => selectDate(moveDate(state.selectedDate, 1)).catch(error => show(error.message, true))); document.querySelector("#today-day").addEventListener("click", () => selectDate(state.summary.date).catch(error => show(error.message, true)));
renderMode(); renderCategories(); refresh().catch(error => show(error.message, true));
