"use strict";
const config = window.wishlistConfig;
const $ = s => document.querySelector(s);
const money = new Intl.NumberFormat('en-SG', {style:'currency', currency:config.currency});
const fmt = cents => money.format(cents / 100);
$('#user').textContent = config.displayName;
$('#footnote').textContent = 'Prices are read from the shop in its own currency and converted at the published ECB rate, so a change here is either the seller or the exchange rate — never both silently.';

async function api(path, options = {}) {
  const response = await fetch(`${config.basePath}/api${path}`, {
    headers: {'Content-Type': 'application/json'}, cache: 'no-store', ...options
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function show(text, bad = false) {
  const node = $('#message');
  node.textContent = text;
  node.className = bad ? 'message bad' : 'message';
}

function busy(value) { document.querySelectorAll('button').forEach(b => b.disabled = value); }

function render(products) {
  const root = $('#items');
  root.innerHTML = '';
  if (!products.length) { root.innerHTML = '<div class="empty">Nothing tracked yet.</div>'; return; }
  for (const p of products) {
    const item = document.createElement('div');
    item.className = 'item';
    const latest = p.latest;
    const chips = [];
    if (latest) {
      chips.push(latest.in_stock
        ? '<span class="chip in">in stock</span>'
        : '<span class="chip out">sold out</span>');
      if (p.variant_label) {
        chips.push(latest.variant_available === false
          ? `<span class="chip out">size ${p.variant_label} out</span>`
          : `<span class="chip in">size ${p.variant_label}</span>`);
      }
      if (p.target_cents) {
        chips.push(p.met_target
          ? '<span class="chip in">target met</span>'
          : `<span class="chip">target ${fmt(p.target_cents)}</span>`);
      }
      if (latest.fx_rate) chips.push(`<span class="chip">@ ${latest.fx_rate}</span>`);
    } else {
      chips.push('<span class="chip warn">no reading yet</span>');
    }
    const delta = p.change_cents;
    const deltaHtml = !delta ? '' :
      `<div class="delta ${delta > 0 ? 'up' : 'down'}">${delta > 0 ? '+' : '−'}${fmt(Math.abs(delta))} since last check</div>`;
    item.innerHTML = `
      <div>
        <div class="item-name"><a href="${p.url}" target="_blank" rel="noopener noreferrer"></a></div>
        <div class="item-meta">${p.source} · ${p.base_currency} → ${p.display_currency}</div>
        <div class="chips">${chips.join('')}</div>
      </div>
      <div class="item-right">
        <div class="price ${p.met_target ? 'met' : ''}">${latest ? fmt(latest.display_cents) : '—'}</div>
        ${latest ? `<div class="base">${latest.price_cents / 100} ${latest.currency}</div>` : ''}
        ${deltaHtml}
        <div class="row-actions">
          <button data-target="${p.id}">TARGET</button>
          <button data-delete="${p.id}">REMOVE</button>
        </div>
      </div>`;
    item.querySelector('.item-name a').textContent = p.name;
    root.append(item);
  }
}

async function load() {
  try { render((await api('/products')).products); }
  catch (error) { show(error.message, true); }
}

$('#add-form').onsubmit = async event => {
  event.preventDefault();
  const target = $('#target').value;
  const body = {
    url: $('#url').value.trim(),
    variant_label: $('#variant').value.trim() || null,
    target_cents: target ? Math.round(Number(target) * 100) : null
  };
  busy(true); show('Fetching…');
  try {
    const added = await api('/products', {method: 'POST', body: JSON.stringify(body)});
    $('#url').value = ''; $('#variant').value = ''; $('#target').value = '';
    await load();
    show(`Tracking ${added.name}.`);
  } catch (error) { show(error.message, true); }
  finally { busy(false); }
};

$('#refresh').onclick = async () => {
  busy(true); show('Checking every tracked item…');
  try {
    const result = await api('/refresh', {method: 'POST'});
    await load();
    show(`Checked ${result.checked}.` + (result.failed.length ? ` ${result.failed.length} failed.` : ''));
  } catch (error) { show(error.message, true); }
  finally { busy(false); }
};

$('#items').onclick = async event => {
  const remove = event.target.dataset.delete;
  const retarget = event.target.dataset.target;
  if (remove) {
    if (!confirm('Stop tracking this item? Its price history is deleted too.')) return;
    busy(true);
    try { await api(`/products/${remove}`, {method: 'DELETE'}); await load(); show('Removed.'); }
    catch (error) { show(error.message, true); } finally { busy(false); }
  } else if (retarget) {
    const entered = prompt('Target price in ' + config.currency + ' (blank to clear)');
    if (entered === null) return;
    busy(true);
    try {
      await api(`/products/${retarget}/target`, {method: 'PUT',
        body: JSON.stringify({target_cents: entered.trim() ? Math.round(Number(entered) * 100) : null})});
      await load(); show('Target updated.');
    } catch (error) { show(error.message, true); } finally { busy(false); }
  }
};

load();
