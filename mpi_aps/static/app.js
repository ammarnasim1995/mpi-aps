'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const DEMO = ['demo.admin','demo.central','demo.central2','demo.pk_planner','demo.pk_supervisor','demo.procurement','demo.management'];
let AUTH = JSON.parse(sessionStorage.getItem('mpi_auth') || 'null'), META = null, VIEW = 'dashboard';
const NAV = [['dashboard','Executive Control Tower','◧'],['demand','Demand & Orders','☰'],['knitting','Knitting Planning','▤'],['dipping','Dipping Planning','▥'],
 ['packing','Packing Planning','▦'],['schedule','Integrated Schedule','▬'],['capacity','Capacity & Load','▮'],['material','Material & WIP','◫'],
 ['execution','Execution & Performance','◔'],['scenarios','Scenario Planning','⎇'],['exceptions','Exceptions & Bottlenecks','⚠'],
 ['master','Master Data & Validation','✓'],['refresh','Data Refresh','⟳'],['approvals','Plans, Approval & Audit','✎'],['settings','Planning Settings','⚙']];

async function api(path, opts = {}) {
  const h = {'Content-Type':'application/json'};
  if (AUTH?.token) h['Authorization'] = 'Bearer ' + AUTH.token; else if (AUTH?.demo) h['X-Demo-User'] = AUTH.demo;
  const r = await fetch(path, {method: opts.method || 'GET', headers: h, body: opts.body ? JSON.stringify(opts.body) : undefined});
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) { const e = new Error(data?.error?.message || r.statusText); e.status = r.status; e.code = data?.error?.code; throw e; }
  return data;
}
const post = (p, body) => api(p, {method:'POST', body});
const q = () => { const p = new URLSearchParams(); for (const [k,id] of [['plant','f-plant'],['process','f-process'],['date_from','f-from'],['date_to','f-to'],['plan_id','f-plan']]) { const v = $('#'+id).value; if (v) p.set(k, v); } return p; };
const can = p => META?.permissions.includes(p);
const fmt = s => s ? String(s).replace('T',' ').slice(0,16) : '–';
const n0 = x => x == null ? '–' : Number(x).toLocaleString();
const CHIPS = {on_time:['●','ok','On time'], at_risk:['▲','warn','At risk'], late:['✖','bad','Late'], unscheduled:['■','bad','Unscheduled'], data_error:['!','bad','Data error'], fulfilled:['✔','mut','Fulfilled'],
  scheduled:['●','ok','Scheduled'], blocked:['■','bad','Blocked'], high:['✖','bad','High'], medium:['▲','warn','Medium'], error:['✖','bad','Error'], warning:['▲','warn','Warning'], fresh:['●','ok','Fresh'], stale:['▲','warn','Stale'], never_refreshed:['■','bad','Never'],
  draft:['✎','warn','Draft'], approved:['✔','ok','Approved'], published:['★','ok','Published'], superseded:['–','mut','Superseded']};
const chip = s => { const c = CHIPS[s] || ['', 'mut', s]; return `<span class="chip ${c[1]}">${c[0]} ${esc(c[2])}</span>`; };
const toast = (m, bad) => { const d = document.createElement('div'); d.className = 'note ' + (bad ? 'err' : ''); d.textContent = m; $('#content').prepend(d); setTimeout(() => d.remove(), 9000); };

function dialog(title, bodyHtml, okText = 'OK') {
  return new Promise(res => { $('#dlg-title').textContent = title; $('#dlg-body').innerHTML = bodyHtml; $('#dlg-ok').textContent = okText;
    const d = $('#dlg'); d.onclose = () => res(d.returnValue === 'ok' ? Object.fromEntries([...d.querySelectorAll('[name]')].map(e => [e.name, e.type === 'checkbox' ? e.checked : e.value])) : null); d.showModal(); });
}

/* ---------- generic table: sort, search, column visibility, CSV export ---------- */
function mountTable(host, cols, rows, o = {}) {
  let sort = null, dir = 1, term = '', hidden = new Set(cols.filter(c => c.hide).map(c => c.k));
  const draw = () => {
    let r = rows.filter(x => !term || JSON.stringify(x).toLowerCase().includes(term));
    if (sort) r = [...r].sort((a, b) => ((a[sort] ?? '') > (b[sort] ?? '') ? 1 : -1) * dir);
    const vis = cols.filter(c => !hidden.has(c.k));
    host.innerHTML = `<div class="tools"><input placeholder="Search…" value="${esc(term)}" aria-label="Search table"><details><summary>Columns</summary>${cols.map(c => `<label style="flex-direction:row"><input type="checkbox" data-c="${c.k}" ${hidden.has(c.k) ? '' : 'checked'}> ${esc(c.t)}</label>`).join('')}</details><button data-csv>Export CSV</button><span class="mut">${r.length} of ${rows.length} rows</span></div>
    <div class="tw"><table><thead><tr>${vis.map(c => `<th data-k="${c.k}" tabindex="0">${esc(c.t)}${sort === c.k ? (dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('')}</tr></thead>
    <tbody>${r.slice(0, 1500).map((x, i) => `<tr data-i="${rows.indexOf(x)}">${vis.map(c => `<td>${c.f ? c.f(x) : esc(x[c.k] ?? '–')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    host.querySelector('input[placeholder]').oninput = e => { term = e.target.value.toLowerCase(); draw(); host.querySelector('input[placeholder]').focus(); };
    host.querySelectorAll('th').forEach(t => { t.onclick = () => { sort === t.dataset.k ? dir = -dir : (sort = t.dataset.k, dir = 1); draw(); }; });
    host.querySelectorAll('input[data-c]').forEach(c => c.onchange = () => { c.checked ? hidden.delete(c.dataset.c) : hidden.add(c.dataset.c); draw(); host.querySelector('details').open = true; });
    host.querySelector('[data-csv]').onclick = () => { const csv = [vis.map(c => c.t), ...r.map(x => vis.map(c => x[c.k] ?? ''))].map(l => l.map(v => '"' + String(v).replace(/"/g, '""') + '"').join(',')).join('\n'); const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], {type:'text/csv'})); a.download = (o.name || 'export') + '.csv'; a.click(); };
    if (o.onRow) host.querySelectorAll('tbody tr').forEach(t => t.onclick = () => { host.querySelectorAll('tr.sel').forEach(s => s.classList.remove('sel')); t.classList.add('sel'); o.onRow(rows[+t.dataset.i]); });
  };
  draw();
}
const H = (title, sub) => `<h1>${esc(title)}</h1><p class="sub">${sub || ''}</p>`;
const planBanner = d => d.plan ? `<div class="note">Plan #${d.plan.id} “${esc(d.plan.name)}” ${chip(d.plan.status)} ${d.plan_stale ? '<b class="warn">▲ Inputs changed since this plan was generated – regenerate or review.</b>' : ''}</div>` : '';

/* ---------- views ---------- */
const VIEWS = {};
VIEWS.dashboard = async c => {
  const d = await api('/api/dashboard?' + q());
  const k = d.kpis, kp = (l, v, sub, go) => `<div class="card kpi" tabindex="0" data-go="${go || ''}"><div class="l">${l}</div><div class="v">${v}</div><small>${sub || ''}</small></div>`;
  c.innerHTML = H('Executive Control Tower', 'Every KPI drills into its orders. All figures come from the validated planning engine.') + planBanner(d) +
  `<div class="grid">${kp('Open order qty', n0(d.dozens.open) + ' dz', n0(k.open_pairs) + ' pairs', 'all')}${kp('Confirmed (open)', n0(d.dozens.confirmed) + ' dz', 'unconfirmed ' + n0(d.dozens.unconfirmed) + ' dz', 'all')}
  ${kp('Orders on time', k.on_time, 'of ' + k.orders, 'on_time')}${kp('Orders at risk', k.at_risk, 'slack &lt; threshold', 'at_risk')}${kp('Projected late', k.late, 'max delay ' + k.max_delay_days + ' d', 'late')}
  ${kp('Unscheduled', k.unscheduled, n0(k.unscheduled_pairs) + ' pairs', 'unscheduled')}${kp('Data errors', k.data_error, 'orders', 'data_error')}
  ${Object.entries(d.utilization_by_process).map(([p, u]) => kp('Utilisation ' + p, (u ?? '–') + '%', 'next 4 weeks', p)).join('')}
  ${kp('Material', d.material.blocked_ops + ' blocked', d.material.delayed_ops + ' delayed starts', 'material')}${kp('WIP / FG (pairs)', n0(d.wip_fg_pairs.KNIT_WIP + d.wip_fg_pairs.DIP_WIP), 'FG available ' + n0(d.wip_fg_pairs.FG), 'material')}
  ${kp('Plan adherence', 'n/a', 'no actuals source connected')}</div>
  <h2>Projected shipment delays</h2>${d.projected_delays.length ? `<table><tr><th>Order</th><th>Customer</th><th>Days late</th></tr>${d.projected_delays.map(x => `<tr><td><a href="#" data-ord="${esc(x.order)}">${esc(x.order)}</a></td><td>${esc(x.customer)}</td><td>${x.days_late.toFixed(1)}</td></tr>`).join('')}</table>` : '<p class="mut">None.</p>'}
  <h2>Bottleneck resources</h2><div id="bn"></div><h2>Data freshness</h2><div id="fr"></div>`;
  mountTable($('#bn'), [{k:'resource',t:'Resource'},{k:'plant',t:'Plant'},{k:'process',t:'Process'},{k:'utilization_pct',t:'Util % to busy-until'},{k:'late_or_at_risk_orders_limited',t:'Late/at-risk orders limited'},{k:'busy_until',t:'Busy until',f:r => fmt(r.busy_until)}], d.bottlenecks, {name:'bottlenecks'});
  mountTable($('#fr'), freshCols, d.freshness);
  c.querySelectorAll('.kpi[data-go]').forEach(e => e.onclick = () => { const g = e.dataset.go; if (['knitting','dipping','packing'].includes(g)) nav(g); else if (g === 'material') nav('material'); else if (g) { window.__status = g === 'all' ? '' : g; nav('demand'); } });
  c.querySelectorAll('[data-ord]').forEach(a => a.onclick = e => { e.preventDefault(); explain(a.dataset.ord); });
};
const freshCols = [{k:'source',t:'Source'},{k:'state',t:'State',f:r => chip(r.state)},{k:'label',t:'Label'},{k:'age_hours',t:'Age (h)'},{k:'last_attempt',t:'Last attempt',f:r => fmt(r.last_attempt)},{k:'last_success',t:'Last success',f:r => fmt(r.last_success)},
  {k:'critical_warning',t:'Critical?',f:r => r.critical_warning ? `<b class="bad">Yes – affects: ${esc(r.affected_decisions.join(', '))}</b>` : 'No'}];

VIEWS.demand = async c => {
  const p = q(); if (window.__status) p.set('status', window.__status);
  const d = await api('/api/orders?' + p);
  c.innerHTML = H('Demand & Order Management', 'Open quantity = ordered − delivered (converted with material-specific UoM). Priority rank follows the configured rules; overrides need a reason.') +
  `<div class="tools"><label>Status <select id="st"><option value="">All</option>${['on_time','at_risk','late','unscheduled','data_error','fulfilled'].map(s => `<option ${window.__status === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label>${can('import') ? '<button id="imp">Import orders (CSV)…</button>' : ''}<a href="/api/templates/orders.csv" id="tpl">CSV template</a></div><div id="tbl"></div><div id="impres"></div><h2>Consolidated demand by SKU and plant</h2><div id="cons"></div>`;
  $('#st').onchange = e => { window.__status = e.target.value; nav('demand'); };
  $('#tpl').onclick = async e => { e.preventDefault(); const t = await api('/api/templates/orders.csv'); const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([t])); a.download = 'orders_template.csv'; a.click(); };
  mountTable($('#tbl'), [{k:'rank',t:'Rank'},{k:'order',t:'Order'},{k:'customer',t:'Customer'},{k:'sku',t:'SKU'},{k:'family',t:'Family',hide:1},{k:'plant',t:'Plant'},{k:'priority',t:'Cust prio'},{k:'due',t:'Due'},
    {k:'open_pairs',t:'Open (pairs)',f:r => n0(r.open_pairs)},{k:'open_dozens',t:'Open (dz)'},{k:'confirmed_open_pairs',t:'Confirmed open',hide:1},{k:'unconfirmed_open_pairs',t:'Unconfirmed open',hide:1},{k:'fg_used_pairs',t:'FG used',hide:1},
    {k:'status',t:'Status',f:r => chip(r.status)},{k:'completion',t:'Projected completion',f:r => fmt(r.completion)},{k:'slack_days',t:'Slack (d)'},{k:'origin',t:'Data origin'},{k:'reason',t:'Reason',hide:1}], d.orders, {name:'orders', onRow: r => explain(r.order)});
  mountTable($('#cons'), [{k:'sku',t:'SKU'},{k:'plant',t:'Plant'},{k:'orders',t:'Orders'},{k:'open_pairs',t:'Open pairs'},{k:'unallocated_pairs',t:'Unallocated pairs'}], d.consolidated_by_sku);
  if ($('#imp')) $('#imp').onclick = importDialog;
};
async function importDialog() {
  const r = await dialog('Import sales orders (CSV)', `<label>CSV file <input type="file" name="file" accept=".csv" id="ifile"></label><label>Control: expected rows <input name="rows" type="number"></label><label>Control: expected total quantity in PAIRS <input name="qty" type="number" step="any"></label><label style="flex-direction:row"><input type="checkbox" name="rs"> Replace sample orders</label>`, 'Import');
  if (!r) return; const f = $('#ifile').files[0]; if (!f) return toast('Choose a file first.', 1);
  try { const rep = await post('/api/orders/import', {csv: await f.text(), file_name: f.name, control_rows: r.rows, control_qty: r.qty, replace_sample: r.rs});
    $('#impres').innerHTML = `<div class="note"><b>Import ${rep.reconciled ? 'reconciled ✔' : 'NOT reconciled ✖'}</b> – rows ${rep.rows_in_file}, accepted ${rep.accepted}, rejected ${rep.rejected}; pairs in file ${rep.pairs_in_file}, accepted ${rep.pairs_accepted}, rejected ${rep.pairs_rejected}${rep.rows_difference !== undefined ? `; row diff vs control ${rep.rows_difference}` : ''}${rep.qty_difference_pairs !== undefined ? `; qty diff (pairs) ${rep.qty_difference_pairs}` : ''}${rep.qty_note ? '; ' + rep.qty_note : ''}.</div>${rep.rejected_records.length ? `<table><tr><th>Row</th><th>Key</th><th>Errors</th></tr>${rep.rejected_records.map(x => `<tr><td>${x.row}</td><td>${esc(x.key)}</td><td>${esc(x.errors.join('; '))}</td></tr>`).join('')}</table>` : ''}<p class="mut">Regenerate the plan to include new orders.</p>`;
  } catch (e) { toast(e.message, 1); }
}

const procView = proc => async c => {
  const d = await api('/api/planning/' + proc + '?' + q());
  const extra = {knitting:[{k:'gauge',t:'Gauge'},{k:'bed',t:'Bed'}], dipping:[{k:'coating',t:'Coating'}], packing:[{k:'customer_pack',t:'Customer pack'}]}[proc];
  c.innerHTML = H(proc[0].toUpperCase() + proc.slice(1) + ' Planning', 'Hours = quantity ÷ validated rate (net) or ÷ (rate × efficiency) (gross); setup is separate. Rates shown: SAP standard vs demonstrated. ' + esc(d.actuals_note)) +
  '<h2>Resources (all weeks in horizon)</h2><div id="res"></div><h2>Planned operations</h2><div id="ops"></div>';
  mountTable($('#res'), [{k:'resource',t:'Resource'},{k:'plant',t:'Plant'},{k:'gross',t:'Gross h'},{k:'downtime',t:'Downtime h'},{k:'available',t:'Available h (net+OT)'},{k:'scheduled',t:'Scheduled h'},{k:'utilization_pct',t:'Util %'}], d.resources, {name: proc + '_resources'});
  mountTable($('#ops'), [{k:'order',t:'Order'},{k:'sku',t:'SKU'},{k:'plant',t:'Plant'},{k:'resource',t:'Resource'},{k:'recipe',t:'Recipe',hide:1},...extra,{k:'qty_pairs',t:'Qty (pairs)',f:r => n0(r.qty_pairs)},{k:'qty_dozens',t:'Qty (dz)'},{k:'wip_in_pairs',t:'WIP in',hide:1},
    {k:'sap_standard_rate',t:'SAP std rate'},{k:'demonstrated_rate',t:'Demonstrated rate'},{k:'rate_unit',t:'Unit',hide:1},{k:'rate_basis',t:'Basis',hide:1},{k:'run_hours',t:'Run h'},{k:'setup_hours',t:'Setup h'},{k:'start',t:'Start',f:r => fmt(r.start)},{k:'finish',t:'Finish',f:r => fmt(r.finish)},
    {k:'driver',t:'Start limited by'},{k:'status',t:'Status',f:r => chip(r.status)},{k:'blocked_reason',t:'Blocked reason',hide:1},{k:'actual_qty',t:'Actual qty',f:() => '<span class="mut">n/a</span>',hide:1}], d.ops, {name: proc + '_ops', onRow: r => explain(r.order)});
};
['knitting','dipping','packing'].forEach(p => VIEWS[p] = procView(p));

VIEWS.schedule = async c => {
  const d = await api('/api/gantt?' + q()), ppH = +(window.__zoom || 3);
  const t0 = new Date(d.horizon_start), resources = d.resources, rowH = 26, left = 90;
  const fin = d.ops.filter(o => o.finish).map(o => +new Date(o.finish)); const tEnd = Math.max(...fin, +t0 + 14 * 864e5) + 36e5 * 24;
  const hrs = (tEnd - +t0) / 36e5, W = left + hrs * ppH, Hh = resources.length * rowH + 24, X = t => left + (new Date(t) - t0) / 36e5 * ppH, rowOf = id => resources.findIndex(r => r.id === id);
  const col = {knitting:'#2b6cb0', dipping:'#7b3fa0', packing:'#1f8a8a'};
  let svg = `<svg width="${W}" height="${Hh}" id="svg" role="img" aria-label="Production Gantt chart"><defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="#b3261e" stroke-width="2"/></pattern></defs>`;
  for (let h = 0; h < hrs; h += 24) { const x = left + h * ppH, dt = new Date(+t0 + h * 36e5); svg += `<line x1="${x}" y1="0" x2="${x}" y2="${Hh}" stroke="#8884" /><text x="${x + 2}" y="10" font-size="9" fill="currentColor">${dt.toISOString().slice(5, 10)}</text>`; }
  resources.forEach((r, i) => { svg += `<text x="2" y="${24 + i * rowH + 16}" font-size="10" fill="currentColor">${esc(r.id)}</text><line x1="0" x2="${W}" y1="${24 + (i + 1) * rowH}" y2="${24 + (i + 1) * rowH}" stroke="#8883"/>`; });
  d.downtimes.forEach(x => { const i = rowOf(x.resource); if (i >= 0) svg += `<rect x="${X(x.start)}" y="${24 + i * rowH + 2}" width="${Math.max(2, X(x.end) - X(x.start))}" height="${rowH - 4}" fill="url(#hatch)" opacity=".6"><title>${esc(x.kind)}: ${esc(x.reason)}</title></rect>`; });
  d.ops.filter(o => o.start).forEach(o => { const i = rowOf(o.resource); if (i < 0) return; const x = X(o.start), w = Math.max(3, X(o.finish) - x);
    svg += `<g class="bar-g" data-op="${esc(o.id)}" data-row="${i}" data-x="${x}" style="cursor:${d.editable && can('edit_schedule') ? 'grab' : 'pointer'}"><rect x="${x}" y="${24 + i * rowH + 3}" width="${w}" height="${rowH - 6}" rx="3" fill="${col[o.process]}" ${o.manual ? 'stroke="#fff" stroke-width="2" stroke-dasharray="4 2"' : ''} opacity=".92"><title>${esc(o.id)} | ${esc(o.sku)} | ${o.qty} pairs | ${fmt(o.start)} → ${fmt(o.finish)} | run ${o.run_h}h setup ${o.setup_h}h | limited by: ${esc(o.driver)}${o.manual ? ' | MANUAL: ' + esc(o.manual_reason) : ''}</title></rect>${w > 46 ? `<text x="${x + 3}" y="${24 + i * rowH + 17}" font-size="9" fill="#fff" pointer-events="none">${esc(o.order)}${o.manual ? ' ✎' : ''}</text>` : ''}</g>`; });
  c.innerHTML = H('Integrated Production Schedule', `Sales order → knitting → WIP → dipping → cure/quality → packing. Drag a bar to another eligible resource or time; the server validates every change against the same constraints as the auto-scheduler. ${d.editable ? '' : '<b class="warn">This plan is ' + esc(d.plan.status) + ' and read-only – clone it to a draft to edit.</b>'}`) +
  `<div class="tools"><label>Zoom <select id="zoom">${[[1,'Week'],[3,'Days'],[6,'Hours']].map(([v, l]) => `<option value="${v}" ${v == ppH ? 'selected' : ''}>${l}</option>`).join('')}</select></label>${d.editable && can('edit_schedule') ? '<button id="undo">Undo last change</button>' : ''}<span class="legend"><span style="color:#2b6cb0">■ Knitting</span><span style="color:#7b3fa0">■ Dipping</span><span style="color:#1f8a8a">■ Packing</span><span style="color:#b3261e">▨ Downtime</span><span>┅ dashed = manual override</span></span></div>
  <div id="msg"></div><div class="gantt">${svg}</svg></div><h2>Operations that cannot be scheduled</h2><div id="unsch"></div>`;
  $('#zoom').onchange = e => { window.__zoom = e.target.value; nav('schedule'); };
  if ($('#undo')) $('#undo').onclick = async () => { try { await post(`/api/plans/${d.plan.id}/undo`, {}); nav('schedule'); } catch (e) { toast(e.message, 1); } };
  mountTable($('#unsch'), [{k:'id',t:'Operation'},{k:'order',t:'Order'},{k:'process',t:'Process'},{k:'blocked',t:'Reason'}], d.ops.filter(o => !o.start), {name:'unscheduled_ops'});
  const svgEl = $('#svg');
  svgEl.querySelectorAll('.bar-g').forEach(g => g.addEventListener('pointerdown', ev => {
    const op = d.ops.find(o => o.id === g.dataset.op), x0 = ev.clientX, y0 = ev.clientY; let moved = false; g.setPointerCapture(ev.pointerId);
    g.onpointermove = e => { const dx = e.clientX - x0, dy = e.clientY - y0; if (Math.abs(dx) + Math.abs(dy) > 5) moved = true; if (d.editable && can('edit_schedule')) g.setAttribute('transform', `translate(${dx},${dy})`); };
    g.onpointerup = async e => { g.onpointermove = null; const dx = e.clientX - x0, dy = e.clientY - y0; g.removeAttribute('transform');
      if (!moved || !d.editable || !can('edit_schedule')) { if (!moved) explain(op.order); return; }
      const row = Math.max(0, Math.min(resources.length - 1, +g.dataset.row + Math.round(dy / rowH))), res = resources[row];
      const nb = new Date(+new Date(op.start) + dx / ppH * 36e5); nb.setMinutes(0, 0, 0);
      const r = await dialog('Reschedule ' + op.id, `<p>Move to <b>${esc(res.id)}</b>, not before <b>${nb.toISOString().slice(0, 16).replace('T', ' ')}</b> (UTC).</p><label>Reason (mandatory)<textarea name="reason" required></textarea></label>`, 'Validate & apply');
      if (!r || !r.reason.trim()) return;
      try { const out = await post(`/api/plans/${d.plan.id}/move`, {op: op.id, resource: res.id, not_before: nb.toISOString().slice(0, 19), reason: r.reason});
        $('#msg').innerHTML = out.applied ? `<div class="note"><b class="ok">✔ Applied.</b> ${Object.keys(out.impact).length} operation(s) changed (downstream dates recalculated). Late orders now: ${out.kpis.late}.</div>` :
          `<div class="note"><b class="bad">✖ Rejected by constraint validation:</b><ul>${out.violations.map(v => `<li><b>${esc(v.rule)}</b>: ${esc(v.message)}</li>`).join('')}</ul></div>`;
        if (out.applied) setTimeout(() => nav('schedule'), 1200);
      } catch (er) { toast(er.message, 1); } };
  }));
};

VIEWS.capacity = async c => {
  const per = window.__per || 'week', d = await api('/api/capacity?period=' + per + '&' + q()), rows = d.capacity.rows, b = d.capacity.buckets.slice(0, per === 'week' ? 12 : 4);
  const res = [...new Set(rows.map(r => r.resource))], g = {}; rows.forEach(r => g[r.resource + '|' + r.bucket] = r);
  const tot = b.map(k => { const rr = rows.filter(r => r.bucket === k); return {k, cap: rr.reduce((s, r) => s + r.available, 0), sch: rr.reduce((s, r) => s + r.scheduled, 0)}; }), mx = Math.max(1, ...tot.map(t => t.cap));
  const hc = u => u == null ? '' : u >= 100 ? 'background:#b3261e;color:#fff' : u >= 85 ? 'background:#e3b341;color:#000' : u > 0 ? 'background:#9bb7d4;color:#000' : '';
  c.innerHTML = H('Capacity & Load Cockpit', esc(d.capacity.definition)) + `<div class="tools"><label>Horizon <select id="per"><option ${per === 'week' ? 'selected' : ''}>week</option><option ${per === 'month' ? 'selected' : ''}>month</option></select></label></div>
  <h2>Available vs scheduled hours</h2><div class="bar" role="img" aria-label="Capacity versus load">${tot.map(t => `<div title="${t.k}: available ${t.cap.toFixed(0)} h, scheduled ${t.sch.toFixed(0)} h"><i class="cap" style="height:${100 * t.cap / mx}%"></i><i class="sch" style="height:${100 * t.sch / mx}%"></i></div>`).join('')}</div>
  <div class="legend mut"><span>${b.map(esc).join(' · ')}</span></div><p class="legend"><span style="color:#9bb7d4">■ Available (net + overtime)</span><span style="color:var(--acc)">■ Scheduled</span></p>
  <h2>Utilisation heatmap (scheduled ÷ available, %)</h2><div class="tw"><table class="heat"><tr><th>Resource</th>${b.map(k => `<th>${esc(k)}</th>`).join('')}</tr>${res.map(r => `<tr><td>${esc(r)}</td>${b.map(k => { const x = g[r + '|' + k]; return `<td class="h" style="${hc(x?.utilization_pct)}" title="gross ${x?.gross} planned DT ${x?.planned_downtime} unplanned DT ${x?.unplanned_downtime} net ${x?.net} OT ${x?.overtime_available} scheduled ${x?.scheduled} remaining ${x?.remaining}">${x?.utilization_pct ?? '–'}</td>`; }).join('')}</tr>`).join('')}</table></div>
  <p class="mut">Colour is secondary: values are shown; ≥85% amber, ≥100% red. Hover for gross / planned downtime / unplanned downtime / net / overtime / remaining.</p>
  <h2>Rough-cut overload check (infinite capacity)</h2><p class="sub">${esc(d.rough_cut.method)}</p><div id="rc"></div><h2>SKU-level load</h2><div id="sl"></div><h2>Resource/week detail</h2><div id="det"></div>`;
  $('#per').onchange = e => { window.__per = e.target.value; nav('capacity'); };
  mountTable($('#rc'), [{k:'resource',t:'Resource'},{k:'plant',t:'Plant'},{k:'process',t:'Process'},{k:'overloaded',t:'Overloaded?',f:r => r.overloaded ? '<b class="bad">✖ Yes</b>' : '<span class="ok">● No</span>'},{k:'max_cumulative_shortage_h',t:'Max cumulative shortage (h)'},{k:'total_demand_h',t:'Total demand (h)'}], d.rough_cut.resources);
  mountTable($('#sl'), [{k:'sku',t:'SKU'},{k:'process',t:'Process'},{k:'hours',t:'Hours'},{k:'pairs',t:'Pairs'}], d.sku_load);
  mountTable($('#det'), [{k:'resource',t:'Resource'},{k:'bucket',t:'Bucket'},{k:'gross',t:'Gross h'},{k:'planned_downtime',t:'Planned DT'},{k:'unplanned_downtime',t:'Unplanned DT'},{k:'net',t:'Net h'},{k:'overtime_available',t:'OT avail'},{k:'scheduled',t:'Scheduled'},{k:'utilization_pct',t:'Util %'},{k:'remaining',t:'Remaining h'}], rows, {name:'capacity'});
};

VIEWS.material = async c => {
  const d = await api('/api/material?' + q());
  c.innerHTML = H('Material & WIP Readiness', 'Available = stock − reserved − quality-blocked. Shortage rule per material: delay (wait for receipts) or block.') + '<h2>Raw materials</h2><div id="rm"></div><h2>WIP / FG</h2><div id="wip"></div><h2>Blocked operations</h2><div id="bl"></div><h2>Delayed starts</h2><div id="dl"></div>';
  mountTable($('#rm'), [{k:'material',t:'Material'},{k:'plant',t:'Plant'},{k:'unit',t:'Unit'},{k:'stock',t:'Stock'},{k:'reserved',t:'Reserved'},{k:'quality_blocked',t:'Blocked'},{k:'available',t:'Available'},{k:'required_by_plan',t:'Required by plan'},{k:'shortage',t:'Shortage',f:r => +r.shortage > 0 ? `<b class="bad">${esc(r.shortage)}</b>` : r.shortage},{k:'expected_receipts',t:'Receipts',f:r => esc(r.expected_receipts.map(x => x.on + ': ' + x.qty).join('; ') || '–')},{k:'on_shortage_rule',t:'Rule'},{k:'origin',t:'Origin'}], d.raw_materials);
  mountTable($('#wip'), [{k:'sku',t:'SKU'},{k:'plant',t:'Plant'},{k:'stage',t:'Stage'},{k:'qty',t:'Qty (pairs)'},{k:'allocated',t:'Allocated'},{k:'quality_blocked',t:'Quality-blocked'},{k:'available',t:'Available'},{k:'origin',t:'Origin'}], d.wip_fg);
  mountTable($('#bl'), [{k:'order',t:'Order'},{k:'op',t:'Operation'},{k:'reason',t:'Why it cannot be scheduled'}], d.blocked_ops);
  mountTable($('#dl'), [{k:'order',t:'Order'},{k:'op',t:'Operation'},{k:'material_ready',t:'Material ready',f:r => fmt(r.material_ready)},{k:'note',t:'Detail'}], d.delayed_ops);
};

VIEWS.execution = async c => { c.innerHTML = H('Production Execution & Performance') + `<div class="note"><b>Planned – not yet available.</b> Comparing planned vs actual (quantity, hours, efficiency, downtime, rejection, schedule adherence, delay reasons) needs a production-confirmation source (SAP confirmations or uploaded shift reports). None is connected, so this screen deliberately shows no numbers.<br>When a source exists, each figure will be tagged actual / manual / imported / estimate / projection.</div>`; };

const SCN = {overtime:[['plant','Plant','plant'],['process','Process','process'],['resource','Resource (optional)','text'],['hours_per_day','Extra hours/day','number',2],['days','Days','number',28]],
  breakdown:[['resource','Resource','text'],['start_day','Start day offset','number',2],['hours','Duration (h)','number',48]], maintenance:[['resource','Resource','text'],['start_day','Start day offset','number',3],['hours','Duration (h)','number',24]],
  rate_change:[['plant','Plant','plant'],['process','Process','process'],['resource','Resource (optional)','text'],['sku','SKU (optional)','text'],['factor','Rate multiplier','number',1.1]],
  demand:[['pct','Change %','number',10],['sku','SKU (optional)','text'],['customer','Customer (optional)','text'],['order','Order (optional)','text']],
  material_delay:[['material','Material','text'],['plant','Plant (optional)','plant'],['days','Delay (days)','number',7]], move_plant:[['order','Order','text'],['plant','To plant','plant']],
  expedite:[['orders','Orders (comma separated)','text']], lots:[['order','Order','text'],['lots','Number of lots','number',2]]};
VIEWS.scenarios = async c => {
  const list = await api('/api/scenarios'); const changes = [];
  const fld = f => f[2] === 'plant' ? `<label>${f[1]}<select name="${f[0]}"><option value=""></option>${META.plants.map(p => `<option>${p.id}</option>`).join('')}</select></label>` : f[2] === 'process' ? `<label>${f[1]}<select name="${f[0]}"><option value=""></option><option>knitting</option><option>dipping</option><option>packing</option></select></label>` : `<label>${f[1]}<input name="${f[0]}" type="${f[2]}" value="${f[3] ?? ''}" step="any"></label>`;
  c.innerHTML = H('Scenario Planning', 'Scenarios run on a copy of the data. The approved/published plan is never changed by a simulation.') + `<div class="card"><div class="tools"><label>Change type <select id="sct">${Object.keys(SCN).map(k => `<option>${k}</option>`).join('')}</select></label><span id="sff" class="tools"></span><button id="add">Add change</button></div>
  <div id="chg" class="mut">No changes added.</div><div class="tools"><label>Name <input id="sname" value="Scenario"></label><label>Mode <select id="smode"><option value="replan">Re-plan (engine re-optimises)</option><option value="resolve">Keep plan, recompute dates</option></select></label><button class="primary" id="run" ${can('run_scenarios') ? '' : 'disabled'}>Run scenario</button></div></div>
  <div id="sres"></div><h2>Breakdown impact analysis</h2><div class="card"><div class="tools"><label>Resource <select id="bdr">${META.resources.map(r => `<option>${r.id}</option>`).join('')}</select></label><label>From <input type="datetime-local" id="bds"></label><label>To <input type="datetime-local" id="bde"></label><button id="bdgo" ${can('run_scenarios') ? '' : 'disabled'}>Analyse</button></div></div><div id="bdres"></div>
  <h2>Saved scenarios</h2><div id="slist"></div>`;
  const draw = () => { $('#sff').innerHTML = SCN[$('#sct').value].map(fld).join(''); }; $('#sct').onchange = draw; draw();
  const showChg = () => $('#chg').innerHTML = changes.length ? changes.map((x, i) => `<div><code>${esc(JSON.stringify(x))}</code> <button data-d="${i}">remove</button></div>`).join('') : 'No changes added.';
  $('#add').onclick = () => { const t = $('#sct').value, ch = {type: t}; SCN[t].forEach(f => { const el = $('#sff').querySelector(`[name=${f[0]}]`); if (el.value !== '') ch[f[0]] = f[2] === 'number' ? +el.value : el.value; }); if (t === 'expedite') ch.orders = String(ch.orders || '').split(',').map(s => s.trim()).filter(Boolean); changes.push(ch); showChg(); $('#chg').querySelectorAll('[data-d]').forEach(b => b.onclick = () => { changes.splice(+b.dataset.d, 1); showChg(); }); };
  const cmp = (o, host) => host.innerHTML = `<div class="note">${(o.comparison?.notes || o.notes || []).map(esc).join(' ')} Baseline: plan #${o.baseline_plan?.id} ${esc(o.baseline_plan?.status || '')}. ${o.scenario_id ? 'Saved as scenario #' + o.scenario_id + '. <b>Approved plan untouched.</b>' : ''}</div>
    <table><tr><th>KPI</th><th>Baseline</th><th>Scenario</th><th>Δ</th></tr>${Object.entries(o.comparison.kpi_delta).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.baseline}</td><td>${v.scenario}</td><td class="${v.delta > 0 && ['late','at_risk','unscheduled','max_delay_days'].includes(k) ? 'bad' : ''}">${v.delta > 0 ? '+' : ''}${v.delta}</td></tr>`).join('')}</table>
    <h3>Scheduled hours by process</h3><table><tr><th>Process</th><th>Baseline</th><th>Scenario</th></tr>${Object.entries(o.comparison.hours_by_process).map(([k, v]) => `<tr><td>${k}</td><td>${v.baseline}</td><td>${v.scenario}</td></tr>`).join('')}</table>
    <h3>Orders whose completion or status changed</h3><table><tr><th>Order</th><th>Status</th><th>Completion (baseline → scenario)</th><th>Δ days</th></tr>${o.comparison.orders_changed.slice(0, 40).map(r => `<tr><td>${esc(r.order)}</td><td>${chip(r.base_status)} → ${chip(r.scenario_status)}</td><td>${fmt(r.base_completion)} → ${fmt(r.scenario_completion)}</td><td>${r.delta_days ?? '–'}</td></tr>`).join('')}</table>${o.violations?.length ? `<p class="bad">Constraint violations in scenario: ${o.violations.length}</p>` : '<p class="ok">✔ No hard-constraint violations in the scenario schedule.</p>'}`;
  $('#run').onclick = async () => { try { cmp(await post('/api/scenarios/run', {name: $('#sname').value, changes, mode: $('#smode').value}), $('#sres')); } catch (e) { toast(e.message, 1); } };
  $('#bdgo').onclick = async () => { try { const o = await post('/api/scenarios/breakdown', {resource: $('#bdr').value, start: $('#bds').value + ':00', end: $('#bde').value + ':00'});
    $('#bdres').innerHTML = `<div class="note"><b>Orders running on ${esc(o.resource)} during the outage:</b> ${esc(o.orders_on_resource_during_outage.join(', ') || 'none')}</div><h3>If the plan is left unchanged</h3><div id="b1"></div><h3>After engine re-plan</h3><div id="b2"></div>`; cmp({comparison: o.if_plan_unchanged, baseline_plan: {}}, $('#b1')); cmp({comparison: o.after_replan, baseline_plan: {}}, $('#b2')); } catch (e) { toast(e.message, 1); } };
  mountTable($('#slist'), [{k:'id',t:'#'},{k:'name',t:'Name'},{k:'created_by',t:'By'},{k:'created_at',t:'Created',f:r => fmt(r.created_at)},{k:'meta',t:'Changes',f:r => esc((r.meta.changes || []).map(x => x.type).join(', '))}], list.scenarios);
};

VIEWS.exceptions = async c => {
  const d = await api('/api/exceptions?' + q());
  c.innerHTML = H('Exceptions & Bottlenecks', 'Every exception names the affected records and a suggested action.') + `<div id="ex"></div><h2>Bottleneck analysis</h2><div id="bn"></div>`;
  mountTable($('#ex'), [{k:'id',t:'ID'},{k:'severity',t:'Severity',f:r => chip(r.severity)},{k:'category',t:'Category'},{k:'title',t:'Exception'},{k:'detail',t:'Detail',hide:1},{k:'records',t:'Affected',f:r => esc(r.records.slice(0, 6).join(', '))},{k:'action',t:'Suggested action'}], d.exceptions, {name:'exceptions', onRow: r => { if (r.category === 'delivery' || r.category === 'scheduling') explain(r.records[0]); }});
  mountTable($('#bn'), [{k:'resource',t:'Resource'},{k:'plant',t:'Plant'},{k:'process',t:'Process'},{k:'utilization_pct',t:'Util % (to busy-until)'},{k:'late_or_at_risk_orders_limited',t:'Late/at-risk orders limited'},{k:'orders',t:'Orders',f:r => esc(r.orders.join(', '))},{k:'busy_until',t:'Busy until',f:r => fmt(r.busy_until)}], d.bottlenecks);
};

VIEWS.master = async c => {
  const d = await api('/api/validation'); const tab = window.__mtab || 'findings';
  c.innerHTML = H('Master Data & Validation', `${d.summary.errors} errors, ${d.summary.warnings} warnings. Nothing is auto-repaired; similar names are never matched.`) + `<div class="tabs">${[['findings','Findings'],['mapping','SKU → resource mapping'],['rates','Rate comparison']].map(([k, l]) => `<button class="${tab === k ? 'on' : ''}" data-t="${k}">${l}</button>`).join('')}</div><div id="mt"></div>`;
  c.querySelectorAll('[data-t]').forEach(b => b.onclick = () => { window.__mtab = b.dataset.t; nav('master'); });
  if (tab === 'findings') mountTable($('#mt'), [{k:'severity',t:'Severity',f:r => chip(r.severity)},{k:'check',t:'Check'},{k:'code',t:'Code'},{k:'message',t:'Message'},{k:'records',t:'Affected records',f:r => esc(r.records.slice(0, 8).join(', ') + (r.records.length > 8 ? ` … +${r.records.length - 8}` : ''))},{k:'action',t:'Action'}], d.findings, {name:'validation'});
  if (tab === 'mapping') mountTable($('#mt'), [{k:'sku',t:'SKU'},{k:'plant',t:'Plant'},{k:'process',t:'Process'},{k:'status',t:'Status',f:r => r.status === 'ok' ? chip('scheduled') : chip('blocked')},{k:'eligible',t:'Eligible resources (recipe, rate, basis, validation)',f:r => esc(r.eligible.map(e => `${e.resource} [${e.recipe}] ${e.rate} ${e.unit} ${e.basis}/${e.validation}`).join(' | ') || '–')},{k:'rejected',t:'Why not',f:r => esc([...new Set(r.rejected.map(x => x.reason))].join(', '))}], d.mapping, {name:'mapping'});
  if (tab === 'rates') mountTable($('#mt'), [{k:'sku',t:'SKU'},{k:'plant',t:'Plant'},{k:'process',t:'Process'},{k:'resource',t:'Resource'},{k:'recipe',t:'Recipe'},{k:'sap_standard',t:'SAP standard'},{k:'demonstrated',t:'Demonstrated'},{k:'planned',t:'Planned'},{k:'unit',t:'Unit'},{k:'demonstrated_vs_sap_pct',t:'Demo vs SAP %'},{k:'basis',t:'Basis'},{k:'validation',t:'Validation'},{k:'actual',t:'Actual'}], d.rates, {name:'rates'});
};

VIEWS.refresh = async c => {
  const d = await api('/api/refresh');
  c.innerHTML = H('Data Refresh Monitoring', 'Last attempted vs last successful refresh per source. Sample and stale data are never labelled live.') + `<div class="note">SAP integration: <b>${esc(d.sap_integration.status)}</b>. ${esc(d.sap_integration.note)}</div><div id="fr"></div><h2>Import history</h2><div id="hist"></div>`;
  mountTable($('#fr'), [...freshCols, {k:'errors',t:'Errors',f:r => esc((r.errors || []).join('; ') || '–')}, {k:'x',t:'',f:r => r.critical_warning && r.acknowledgment_permitted && can('ack_stale') ? `<button data-ack="${esc(r.source)}">Acknowledge & continue</button>` : ''}], d.freshness);
  c.querySelectorAll('[data-ack]').forEach(b => b.onclick = async () => { const r = await dialog('Acknowledge stale data: ' + b.dataset.ack, '<label>Reason<textarea name="reason"></textarea></label>'); if (!r) return; try { await post('/api/refresh/acknowledge', {source: b.dataset.ack, reason: r.reason}); toast('Acknowledged and audited.'); } catch (e) { toast(e.message, 1); } });
  mountTable($('#hist'), [{k:'id',t:'#'},{k:'source',t:'Source'},{k:'origin',t:'Origin'},{k:'attempted_at',t:'Attempted',f:r => fmt(r.attempted_at)},{k:'success',t:'Success',f:r => r.success ? chip('fresh') : chip('error')},{k:'file_name',t:'File'},{k:'rows',t:'Rows'},{k:'accepted',t:'Accepted'},{k:'rejected',t:'Rejected'},{k:'reconciled',t:'Reconciled',f:r => r.report ? (r.report.reconciled ? '✔' : '✖') : '–'},{k:'user',t:'User'},{k:'errors',t:'Errors',f:r => esc(r.errors.join('; '))}], d.history);
};

VIEWS.approvals = async c => {
  const [pl, au] = await Promise.all([api('/api/plans'), can('view_audit') ? api('/api/audit?limit=300') : {audit: []}]);
  c.innerHTML = H('Plans, Approval & Audit History', 'Draft → Approved → Published. Approved and published plans are immutable; the approver must differ from the author (four-eyes).') + '<h2>Plans and scenarios</h2><div id="pl"></div><h2>Audit trail</h2><div id="au"></div>';
  mountTable($('#pl'), [{k:'id',t:'#'},{k:'name',t:'Name'},{k:'kind',t:'Kind'},{k:'status',t:'Status',f:r => chip(r.status)},{k:'created_by',t:'Author'},{k:'created_at',t:'Created',f:r => fmt(r.created_at)},{k:'approved_by',t:'Approved by'},{k:'published_by',t:'Published by'},
    {k:'a',t:'Actions',f:r => r.kind !== 'plan' ? '' : [r.status === 'draft' && can('approve') ? `<button data-a="approve" data-id="${r.id}">Approve</button>` : '', r.status === 'approved' && can('publish') ? `<button data-a="publish" data-id="${r.id}">Publish</button>` : '', can('edit_schedule') ? `<button data-a="clone" data-id="${r.id}">Clone to draft</button>` : ''].join(' ')}], pl.plans);
  c.querySelectorAll('[data-a]').forEach(b => b.onclick = async () => { const r = await dialog(b.dataset.a + ' plan #' + b.dataset.id, '<label>Reason / comment<textarea name="reason"></textarea></label>'); if (!r) return; try { await post(`/api/plans/${b.dataset.id}/${b.dataset.a}`, {reason: r.reason}); await loadMeta(); nav('approvals'); } catch (e) { toast(e.message, 1); } });
  mountTable($('#au'), [{k:'id',t:'#'},{k:'ts',t:'Time',f:r => fmt(r.ts)},{k:'user',t:'User'},{k:'action',t:'Action'},{k:'entity',t:'Entity'},{k:'previous',t:'Previous'},{k:'new',t:'New'},{k:'reason',t:'Reason'},{k:'approval_status',t:'Plan status'}], au.audit, {name:'audit'});
};

VIEWS.settings = async c => {
  const d = await api('/api/settings');
  c.innerHTML = H('Planning Settings', 'Administrator-controlled. Every change needs a reason and is audited; approved changes re-resolve drafts, never approved/published plans.') + `<div class="note">Units: base unit pairs, 1 dozen = 12 pairs (material-specific factors override). Efficiency hierarchy: resource → plant+process → plant → approved system default. Rates marked gross get the factor; net rates never do.</div>
  <h2>Efficiency factors</h2><div id="ef"></div>${can('settings') ? '<button id="addef">Add / change efficiency factor…</button>' : ''}<h2>Cure / processing rules (dipping → packing)</h2><div id="cu"></div>${can('settings') ? '<button id="addcu">Add / change cure rule…</button>' : ''}<h2>Freshness policies</h2><div id="fp"></div><h2>Planner configuration</h2><pre>${esc(JSON.stringify(d.planner_config, null, 2))}</pre>`;
  mountTable($('#ef'), [{k:'scope',t:'Scope'},{k:'plant',t:'Plant'},{k:'process',t:'Process'},{k:'resource',t:'Resource'},{k:'factor',t:'Factor'},{k:'approval_status',t:'Approval'},{k:'effective_from',t:'Effective from'},{k:'source',t:'Source'}], d.efficiency);
  mountTable($('#cu'), [{k:'coating',t:'Coating'},{k:'sku',t:'SKU'},{k:'recipe',t:'Recipe'},{k:'plant',t:'Plant'},{k:'min_cure_hours',t:'Min cure h'},{k:'additional_processing_hours',t:'Additional h'},{k:'approval_status',t:'Approval'},{k:'source',t:'Source'}], d.cure_rules);
  mountTable($('#fp'), [{k:'source',t:'Source'},{k:'process',t:'Process'},{k:'threshold_hours',t:'Threshold (h)'},{k:'critical',t:'Critical'},{k:'affected_decisions',t:'Affected decisions',f:r => esc(r.affected_decisions.join(', '))},{k:'acknowledgment_permitted',t:'Ack permitted'}], d.freshness_policies);
  const today = new Date().toISOString().slice(0, 10);
  if ($('#addef')) $('#addef').onclick = async () => { const r = await dialog('Efficiency factor', `<label>Scope<select name="scope"><option>resource</option><option>plant_process</option><option>plant</option><option>system_default</option></select></label><label>Plant<input name="plant"></label><label>Process<input name="process"></label><label>Resource<input name="resource"></label><label>Factor (0–1]<input name="factor" value="0.85"></label><label>Source<input name="source" value="planner input"></label><label>Reason<textarea name="reason"></textarea></label>`); if (!r) return;
    try { await post('/api/settings/efficiency', {reason: r.reason, payload: {scope: r.scope, plant: r.plant || null, process: r.process || null, resource: r.resource || null, factor: r.factor, effective_from: today, source: r.source, approval_status: 'approved'}}); nav('settings'); } catch (e) { toast(e.message, 1); } };
  if ($('#addcu')) $('#addcu').onclick = async () => { const r = await dialog('Cure rule', `<label>Coating<input name="coating"></label><label>SKU<input name="sku"></label><label>Plant<input name="plant"></label><label>Min cure hours<input name="h" value="6"></label><label>Source<input name="source" value="process engineering"></label><label>Reason<textarea name="reason"></textarea></label>`); if (!r) return;
    try { await post('/api/settings/cure', {reason: r.reason, payload: {coating: r.coating || null, sku: r.sku || null, plant: r.plant || null, min_cure_hours: r.h, effective_from: today, source: r.source, approval_status: 'approved'}}); nav('settings'); } catch (e) { toast(e.message, 1); } };
};

async function explain(order) {
  try { const e = await api(`/api/orders/${encodeURIComponent(order)}/explain?` + q()); const o = e.order;
    $('#panel-body').innerHTML = `<h2>${esc(o.order)} ${chip(o.status)}</h2><p>${esc(o.customer)} · ${esc(o.sku)} · ${esc(o.plant)} · due ${esc(o.due)}<br>Open ${n0(o.open_pairs)} pairs (${o.open_dozens} dz) · projected completion ${fmt(o.completion)} · slack ${o.slack_days ?? '–'} d</p>
    ${o.reason ? `<div class="note">${esc(o.reason)}</div>` : ''}<h3>Why</h3><ul>${e.narrative.map(l => `<li>${esc(l)}</li>`).join('')}</ul>
    ${e.limiting_chain.length ? `<h3>Limiting chain (backwards from completion)</h3><ol>${e.limiting_chain.map(s => `<li>${esc(s.process)} on ${esc(s.resource)} ${fmt(s.start)} → ${fmt(s.finish)} — limited by <b>${esc(s.driver)}</b></li>`).join('')}</ol>` : ''}
    ${e.alternatives ? `<h3>Alternatives (computed by the engine)</h3><table><tr><th>Action</th><th>New completion</th><th>Status</th><th>Other orders worsened</th></tr>${e.alternatives.map(a => `<tr><td>${esc(a.action)}</td><td>${fmt(a.completion)}</td><td>${chip(a.status)}</td><td>${a.orders_worsened}</td></tr>`).join('')}</table><p class="mut">${esc(e.alternatives_note)}</p>` : ''}
    <h3>Resource selection</h3>${e.ops.map(op => `<p><b>${esc(op.process)}</b> ${esc(op.op)}</p>${op.candidates.length ? `<table><tr><th>Resource</th><th>Recipe</th><th>Finish</th><th>Run h</th><th>Setup h</th><th>Score</th><th></th></tr>${op.candidates.map(x => `<tr><td>${esc(x.resource || '–')}</td><td>${esc(x.recipe || '')}</td><td>${fmt(x.finish)}</td><td>${x.run_hours ?? ''}</td><td>${x.setup_hours ?? ''}</td><td>${x.score ?? ''}</td><td>${x.chosen ? '✔ chosen' : esc(x.reason || '')}</td></tr>`).join('')}</table>` : ''}`).join('')}
    ${can('edit_schedule') ? '<p><button id="ovr">Override priority…</button></p>' : ''}`;
    $('#panel').hidden = false; if ($('#ovr')) $('#ovr').onclick = async () => { const r = await dialog('Override priority for ' + order, '<label>Rank (1 = first)<input name="rank" type="number" value="1"></label><label>Reason (mandatory)<textarea name="reason"></textarea></label>'); if (!r) return; try { await post(`/api/orders/${encodeURIComponent(order)}/priority`, r); toast('Override saved. Regenerate the plan to apply it.'); } catch (e) { toast(e.message, 1); } };
  } catch (e) { toast(e.message, 1); }
}

/* ---------- shell ---------- */
async function loadMeta() {
  META = await api('/api/meta'); $('#who').textContent = `${META.user} (${META.role})`; $('#banner').textContent = META.data_banner;
  const cur = $('#f-plan').value; $('#f-plan').innerHTML = '<option value="">Working plan</option>' + META.plans.filter(p => p.kind === 'plan').map(p => `<option value="${p.id}">#${p.id} ${esc(p.name)} [${p.status}]</option>`).join(''); $('#f-plan').value = cur;
  const cp = $('#f-plant').value; $('#f-plant').innerHTML = '<option value="">All</option>' + META.plants.map(p => `<option value="${p.id}">${p.id} ${p.country}</option>`).join(''); $('#f-plant').value = cp;
  const ex = META.freshness.filter(f => f.critical_warning).length; $('#bellc').textContent = ex;
}
function nav(v) { VIEW = v; document.querySelectorAll('#menu a').forEach(a => a.classList.toggle('on', a.dataset.v === v)); render(); }
async function render() { const c = $('#content'); c.innerHTML = '<p class="mut">Loading…</p>'; try { await VIEWS[VIEW](c); } catch (e) { if (e.status === 401) return showLogin(); c.innerHTML = `<div class="note err">${esc(e.message)}</div>`; } c.focus(); }
function showLogin() { $('#app').hidden = true; $('#login').hidden = false; $('#demo-users').hidden = false; $('#demo').innerHTML = DEMO.map(u => `<option>${u}</option>`).join(''); }
async function start() {
  $('#menu').innerHTML = NAV.map(([k, l, i]) => `<a data-v="${k}" tabindex="0" role="link"><span>${i}</span><span class="lbl">${l}</span></a>`).join('');
  $('#menu').onclick = e => { const a = e.target.closest('a'); if (a) { window.__status = ''; nav(a.dataset.v); } };
  $('#menu').onkeydown = e => { if (e.key === 'Enter') e.target.click(); };
  try { await loadMeta(); $('#login').hidden = true; $('#app').hidden = false; nav(VIEW); } catch (e) { showLogin(); }
}
$('#loginbtn').onclick = async () => { const t = $('#tok').value.trim(); AUTH = t ? {token: t} : {demo: $('#demo').value}; sessionStorage.setItem('mpi_auth', JSON.stringify(AUTH)); try { await loadMeta(); $('#loginerr').textContent = ''; start(); } catch (e) { $('#loginerr').textContent = 'Sign-in failed: ' + e.message; } };
$('#logout').onclick = () => { sessionStorage.removeItem('mpi_auth'); AUTH = null; showLogin(); };
$('#collapse').onclick = () => $('#nav').classList.toggle('c'); $('#panel-x').onclick = () => $('#panel').hidden = true;
$('#bell').onclick = () => nav('exceptions');
['f-plan','f-plant','f-process','f-from','f-to'].forEach(id => $('#' + id).onchange = render);
start();
