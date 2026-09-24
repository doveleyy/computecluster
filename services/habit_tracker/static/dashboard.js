"use strict";
const config = window.habitConfig;
const $ = selector => document.querySelector(selector);
const money = new Intl.NumberFormat('en-SG', {style:'currency',currency:config.currency});
const format = cents => money.format(cents / 100);
const clamp = ratio => Math.max(0, Math.min(1, ratio));
let budget = null, resetTimer = null, loading = false, saving = false;
$('#user').textContent = config.displayName;
for (const selector of ['#open-water', '#open-water-art']) $(selector).href = `${config.basePath}/water`;
for (const selector of ['#open-budget', '#open-budget-art']) $(selector).href = `${config.basePath}/budget`;

async function request(path, options = {}) {
  const response = await fetch(`${config.basePath}/api${path}`, {
    ...options, headers: {'Content-Type':'application/json'}, cache:'no-store'
  });
  if (!response.ok) {
    let detail = `Could not load Habit Tracker (${response.status}).`;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response.json();
}

function progress(selector, ratio, description) {
  const percent = Math.round(clamp(ratio) * 100);
  $(selector).setAttribute('aria-valuenow', String(percent));
  $(selector).setAttribute('aria-valuetext', description);
  $(`${selector} span`).style.width = `${percent}%`;
}

function renderWater(water) {
  const ratio = water.total_ml / water.goal_ml;
  $('#water-total').textContent = `${water.total_ml.toLocaleString()} ml`;
  $('#water-target').textContent = `of ${water.goal_ml.toLocaleString()} ml`;
  $('#water-status').textContent = ratio >= 1 ? 'Goal reached' : `${Math.round(ratio * 100)}% today`;
  $('#water-liquid').setAttribute('transform', `translate(0 ${32 - Math.round(clamp(ratio) * 16) * 2})`);
  const remainder = Math.max(0, water.goal_ml - water.total_ml);
  $('#water-detail').textContent = remainder ? `${remainder.toLocaleString()} ml to go. One glass at a time.` : 'Daily goal reached. Your extra drinks are still recorded.';
  progress('#water-progress', ratio, `${water.total_ml} of ${water.goal_ml} millilitres`);
}

function renderSavings(summary) {
  budget = summary;
  const goal = summary.savings_goal_cents;
  const balance = summary.fund_balance_cents;
  const ratio = goal ? balance / goal : 0;
  $('#fund-total').textContent = format(balance);
  $('#fund-total').classList.toggle('negative', balance < 0);
  $('#goal-trigger').disabled = false;
  $('#goal-trigger').textContent = goal ? `of ${format(goal)} · edit goal` : 'Set a savings goal ↗';
  $('#savings-status').textContent = !goal ? 'No goal yet' : ratio >= 1 ? 'Goal reached' : `${Math.round(clamp(ratio) * 100)}% saved`;
  $('#pig-fill').setAttribute('transform', `translate(0 ${32 - Math.round(clamp(ratio) * 16) * 2})`);
  const pending = summary.pending_surplus_cents ? ` ${format(summary.pending_surplus_cents)} pending from today.` : '';
  $('#savings-detail').textContent = (goal ? balance >= goal ? 'You reached your target. Make something good of it.' : `${format(goal - balance)} until your goal.` : 'Give your savings something to aim for.') + pending;
  progress('#savings-progress', ratio, goal ? `${format(balance)} of ${format(goal)}` : 'No savings goal set');
}

async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const [water, summary] = await Promise.all([request('/water/today'),request('/budget/summary')]);
    renderWater(water); renderSavings(summary);
    $('#today').textContent = `${water.date} · ${config.timezone}`;
    $('#error').hidden = true;
    clearTimeout(resetTimer);
    resetTimer = setTimeout(refresh, Math.max(1000, new Date(summary.next_reset_at).getTime() - Date.now() + 1000));
  } catch (error) {
    $('#error span').textContent = error.message;
    $('#error').hidden = false;
    $('#water-status').textContent = 'Refresh needed';
    $('#savings-status').textContent = 'Refresh needed';
  } finally { loading = false; }
}

function editGoal(editing) {
  $('#goal-form').hidden = !editing;
  $('#goal-trigger').hidden = editing;
  $('#goal-message').textContent = '';
  if (editing) {
    $('#goal-input').value = budget.savings_goal_cents ? (budget.savings_goal_cents / 100).toFixed(2) : '';
    $('#goal-clear').hidden = budget.savings_goal_cents === null;
    $('#goal-input').focus();
  }
}

async function saveGoal(target) {
  if (saving) return;
  saving = true;
  $('#goal-form').querySelectorAll('button,input').forEach(node => node.disabled = true);
  try {
    await request('/budget/savings-goal', {method:'PUT', body:JSON.stringify({target_cents:target})});
    editGoal(false); await refresh();
  } catch (error) { $('#goal-message').textContent = error.message; }
  finally {
    saving = false;
    $('#goal-form').querySelectorAll('button,input').forEach(node => node.disabled = false);
  }
}

$('#goal-trigger').addEventListener('click', () => editGoal(true));
$('#goal-cancel').addEventListener('click', () => editGoal(false));
$('#goal-clear').addEventListener('click', () => saveGoal(null));
$('#goal-form').addEventListener('submit', event => {
  event.preventDefault();
  saveGoal(Math.round(Number($('#goal-input').value) * 100));
});
$('#goal-input').addEventListener('keydown', event => { if (event.key === 'Escape' && !saving) editGoal(false); });
$('#retry').addEventListener('click', refresh);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
window.addEventListener('pageshow', event => { if (event.persisted) refresh(); });
refresh();
