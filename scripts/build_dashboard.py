#!/usr/bin/env python3
"""Render the standalone HTML dashboard.

The payload is inlined rather than fetched. A dashboard that needs a web server
to show its own numbers is one more thing between a reviewer and the result --
this one opens from a file, offline, on any machine.

    python scripts/build_dashboard.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "data" / "out" / "bi" / "dashboard_data.json"
OUTPUT = ROOT / "dashboard.html"

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hourglass — authorisation utilisation</title>
<style>
  :root{
    color-scheme: light;
    --page:#f9f9f7; --surface:#fcfcfb; --raised:#ffffff;
    --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
    --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
    --blue:#2a78d6; --blue-300:#6da7ec; --blue-550:#1c5cab; --blue-100:#cde2fb;
    --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --critical:#d03b3b;
    --wash:rgba(11,11,11,.035);
  }
  :root[data-theme="dark"]{
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19; --raised:#212120;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --blue:#3987e5; --blue-300:#5598e7; --blue-550:#86b6ef; --blue-100:#184f95;
    --wash:rgba(255,255,255,.05);
  }
  @media (prefers-color-scheme: dark){
    :root:where(:not([data-theme="light"])){
      color-scheme: dark;
      --page:#0d0d0d; --surface:#1a1a19; --raised:#212120;
      --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
      --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
      --blue:#3987e5; --blue-300:#5598e7; --blue-550:#86b6ef; --blue-100:#184f95;
      --wash:rgba(255,255,255,.05);
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--page);color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:15px;line-height:1.55}
  .wrap{max-width:1180px;margin:0 auto;padding:32px 22px 80px}
  header.top{display:flex;justify-content:space-between;align-items:flex-start;
    gap:20px;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:20px}
  h1{font-size:23px;margin:0 0 4px;letter-spacing:-.015em}
  .sub{color:var(--ink-2);font-size:13.5px;margin:0}
  .meta{font-size:12px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
  h2{font-size:16px;margin:36px 0 4px;letter-spacing:-.005em}
  .h2sub{font-size:13px;color:var(--ink-2);margin:0 0 14px}
  .banner{margin:18px 0 0;padding:12px 16px;border-radius:10px;font-size:13.5px;
    border:1px solid var(--border);border-left:3px solid var(--warn);background:var(--surface)}
  .banner b{color:var(--ink)}
  .synthetic{border-left-color:var(--blue)}
  .hero{display:flex;gap:26px;align-items:flex-end;flex-wrap:wrap;margin:22px 0 4px}
  .hero-fig{font-size:56px;font-weight:600;letter-spacing:-.03em;line-height:1}
  .hero-meta{font-size:13.5px;color:var(--ink-2);padding-bottom:6px}
  .pill{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.05em;
    text-transform:uppercase;padding:3px 9px;border-radius:5px;border:1px solid}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:20px 0 0}
  .kpi{background:var(--surface);border:1px solid var(--border);border-radius:11px;padding:14px 16px}
  .kpi .lab{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
    font-weight:600;margin:0 0 6px}
  .kpi .val{font-size:26px;font-weight:600;letter-spacing:-.02em;line-height:1.1;
    font-variant-numeric:tabular-nums}
  .kpi .note{font-size:12.5px;color:var(--ink-2);margin:5px 0 0}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin:14px 0}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  @media (max-width:860px){.grid2{grid-template-columns:1fr}}
  /* The at-risk table has eight columns and cannot usefully compress to a
     phone. Scrolling it inside its own card is the lesser evil; without this
     it widens the document and every other block scrolls sideways with it. */
  .card.scroll-x{overflow-x:auto}
  .card.scroll-x table{min-width:660px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--grid)}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600}
  td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  tbody tr:hover{background:var(--wash)}
  .chk{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-bottom:1px solid var(--grid)}
  .chk:last-child{border-bottom:0}
  .chk .sev{flex:none;width:52px;font-size:10.5px;font-weight:700;letter-spacing:.04em;
    padding:2px 0;text-align:center;border-radius:4px;border:1px solid;margin-top:2px}
  .chk .body{flex:1;min-width:0}
  .chk .nm{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;font-weight:600}
  .chk .ms{font-size:12.5px;color:var(--ink-2);margin:2px 0 0}
  .ok{color:var(--good);border-color:var(--good)}
  .warnc{color:var(--warn);border-color:var(--warn)}
  .blockc{color:var(--critical);border-color:var(--critical)}
  .mutedc{color:var(--muted);border-color:var(--border)}
  .toggle{position:fixed;top:12px;right:12px;background:var(--surface);color:var(--ink-2);
    border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:12px;
    cursor:pointer;font-family:inherit;z-index:40}
  .tip{position:fixed;pointer-events:none;background:var(--raised);border:1px solid var(--border);
    border-radius:8px;padding:7px 10px;font-size:12.5px;box-shadow:0 4px 16px rgba(0,0,0,.16);
    opacity:0;transition:opacity .09s;z-index:60;max-width:250px}
  .tip.on{opacity:1}
  .tip b{display:block;margin-bottom:2px}
  .foot{margin-top:36px;padding-top:18px;border-top:1px solid var(--border);
    font-size:12.5px;color:var(--ink-2)}
  code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
    background:var(--wash);padding:1px 5px;border-radius:4px}
</style>
</head>
<body>
<button class="toggle" id="themeBtn">Dark</button>
<div class="wrap">

<header class="top">
  <div>
    <h1>Authorisation utilisation</h1>
    <p class="sub">Authorised therapy hours against hours actually delivered, by payer, discipline and centre.</p>
  </div>
  <div class="meta" id="meta"></div>
</header>

<div class="banner synthetic">
  <b>All data on this page is synthetic.</b> It was produced by a seeded generator in this
  repository. No real patient information was used to build it and none is present in it.
</div>

<div id="gateBanner"></div>

<div class="hero">
  <div>
    <div class="hero-fig" id="heroFig">—</div>
    <div class="sub" style="margin-top:4px" id="heroSub">delivery pace against the operating floor</div>
  </div>
  <div class="hero-meta" id="heroMeta"></div>
</div>

<div class="kpis" id="kpis"></div>

<h2>What needs attention this week</h2>
<p class="h2sub" id="atRiskSub"></p>
<div class="card scroll-x">
  <table>
    <thead><tr>
      <th>Client</th><th>Service</th><th>Payer</th><th>Contract</th>
      <th class="n">Expires in</th><th class="n">Authorised</th>
      <th class="n">Delivered</th><th class="n">Hours at risk</th>
    </tr></thead>
    <tbody id="atRiskBody"></tbody>
  </table>
</div>

<div class="grid2">
  <div>
    <h2>Pace by payer</h2>
    <p class="h2sub">Weighted by authorised units. The line marks the 90% floor.</p>
    <div class="card"><div id="chartPayer"></div></div>
  </div>
  <div>
    <h2>Pace by discipline</h2>
    <p class="h2sub">Same measure, cut by service line.</p>
    <div class="card"><div id="chartDiscipline"></div></div>
  </div>
</div>

<h2>Hours delivered by month</h2>
<p class="h2sub" id="monthlySub">Completed sessions only. Cancellations and no-shows are excluded.</p>
<div class="card"><div id="chartMonthly"></div></div>

<h2>Measure coverage by month</h2>
<p class="h2sub" id="coverageSub"></p>
<div class="card"><div id="chartCoverage"></div></div>

<h2>What the missing unit of measure would have cost</h2>
<div class="card" id="spreadCard"></div>

<h2>Data quality gate</h2>
<p class="h2sub" id="qualitySub"></p>
<div class="card" id="checks"></div>

<div class="foot" id="foot"></div>
</div>

<div class="tip" id="tip"></div>

<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
(function(){
  const D = JSON.parse(document.getElementById('payload').textContent);
  const $ = id => document.getElementById(id);
  const pct = (v, d=1) => (v*100).toFixed(d) + '%';
  const num = (v, d=0) => Number(v).toLocaleString(undefined,
      {minimumFractionDigits:d, maximumFractionDigits:d});

  /* ---------- theme ---------- */
  const btn = $('themeBtn');
  const cur = () => document.documentElement.getAttribute('data-theme')
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const sync = () => { btn.textContent = cur()==='dark' ? 'Light' : 'Dark'; };
  btn.onclick = () => { document.documentElement.setAttribute('data-theme',
      cur()==='dark'?'light':'dark'); sync(); drawAll(); };
  sync();
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  /* ---------- tooltip ---------- */
  const tip = $('tip');
  function showTip(e, html){
    tip.innerHTML = html; tip.classList.add('on');
    let x = e.clientX + 13, y = e.clientY + 13;
    if (x + 260 > innerWidth) x = e.clientX - 262;
    if (y + 90 > innerHeight) y = e.clientY - 92;
    tip.style.left = x+'px'; tip.style.top = y+'px';
  }
  const hideTip = () => tip.classList.remove('on');

  /* ---------- header ---------- */
  const m = D.meta;
  $('meta').innerHTML =
      `as of <b>${m.as_of}</b><br>run <code>${m.run_id}</code> · hourglass ${m.code_version}` +
      `<br>lake backend: ${m.lake_backend}`;

  const h = D.headline;
  const status = h.pace >= 1.0 ? ['Over-delivered','warnc']
               : h.pace >= 0.90 ? ['On track','ok']
               : h.pace >= 0.75 ? ['Behind','warnc'] : ['At risk','blockc'];
  $('heroFig').textContent = pct(h.pace);
  $('heroSub').textContent = `delivery pace against the ${pct(h.floor,0)} operating floor`;
  $('heroFig').style.color = h.pace >= 0.90 ? css('--good')
                            : h.pace >= 0.75 ? css('--warn') : css('--critical');
  $('heroMeta').innerHTML =
      `<span class="pill ${status[1]}">${status[0]}</span><br><br>` +
      `<b>${num(h.units_delivered)}</b> units delivered against ` +
      `<b>${num(h.expected_units_to_date)}</b> expected by today<br>` +
      `across ${num(h.active_authorizations)} open authorisations ` +
      `(${num(h.closed_authorizations)} closed at ${pct(h.closed_utilization)})`;

  /* ---------- KPIs ---------- */
  const q = D.quality, uomCheck = q.results.find(r => r.name === 'uom_resolution_coverage');
  const coverage = uomCheck ? uomCheck.observed : 1;
  const uomExcluded = uomCheck ? uomCheck.affected_rows : 0;
  const kpis = [
    ['Hours at risk', num(h.at_risk_hours), `${h.at_risk_count} authorisations expiring within 30 days`],
    ['Children affected', num(h.at_risk_children), 'have approved hours about to expire unused'],
    ['Unused hours, all open auths', num(h.hours_unused), 'authorised but not yet delivered'],
    ['Measure coverage', pct(coverage,1), `${num(uomExcluded)} sessions excluded — unit of measure unknown`],
  ];
  $('kpis').innerHTML = kpis.map(k =>
    `<div class="kpi"><p class="lab">${k[0]}</p><div class="val">${k[1]}</div>
     <p class="note">${k[2]}</p></div>`).join('');

  /* ---------- gate banner ---------- */
  const acks = Object.entries(q.acknowledged || {});
  if (acks.length){
    $('gateBanner').innerHTML =
      `<div class="banner"><b>Published with an acknowledged blocking failure.</b><br>` +
      acks.map(([k,v]) => `<code>${k}</code> — ${v}`).join('<br>') +
      `<br><span style="color:var(--muted)">Rule set v${q.ruleset_version} ` +
      `(<code>${q.ruleset_hash}</code>). Recorded in the run log.</span></div>`;
  }

  /* ---------- at-risk table ---------- */
  $('atRiskSub').textContent =
    `${h.at_risk_count} authorisations covering ${h.at_risk_children} children expire within 30 days ` +
    `with at least a quarter of their hours undelivered. ` +
    `The ${Math.min(15, D.at_risk.length)} expiring soonest are shown, most urgent first.`;
  $('atRiskBody').innerHTML = D.at_risk.slice(0,15).map(r => `
    <tr>
      <td><code>${r.client_id}</code></td>
      <td>${r.service_code} · ${r.discipline}</td>
      <td>${r.payer_name}</td>
      <td>${r.contract_type === 'value_based'
            ? '<span class="pill ok" style="font-size:10px">value-based</span>'
            : '<span class="pill mutedc" style="font-size:10px">fee-for-service</span>'}</td>
      <td class="n">${r.days_to_expiry === 0 ? 'today' : r.days_to_expiry + (r.days_to_expiry === 1 ? ' day' : ' days')}</td>
      <td class="n">${num(r.hours_authorized,1)} h</td>
      <td class="n">${num(r.hours_delivered,1)} h</td>
      <td class="n"><b>${num(r.hours_unused,1)} h</b></td>
    </tr>`).join('');

  /* ---------- charts ---------- */
  function barChart(hostId, rows, labelKey, valueKey, opts){
    opts = opts || {};
    const host = $(hostId);
    const W = Math.max(300, host.clientWidth || 520);
    const clip = t => String(t).length > 22 ? String(t).slice(0,21) + '\u2026' : String(t);
    const longest = Math.max(...rows.map(r => clip(r[labelKey]).length));
    const rowH = 30, m = {t:16, r:56, b:26, l:Math.min(opts.labelWidth || 190, longest*6.6 + 14)};
    const H = m.t + m.b + rows.length*rowH;
    const pw = W - m.l - m.r;
    // The floor participates in the scale. Left out, a floor above every bar
    // lands outside the plot area and its label renders on top of the axis.
    // Round the top of the scale up to a fifth, so the four quarter-ticks land
    // on whole percents (0/25/50/75/100) instead of 24/49/73/97.
    const raw = Math.max(opts.max || 0, opts.floor || 0, ...rows.map(r => r[valueKey])) * 1.02 || 1;
    const max = Math.ceil(raw*5)/5;
    const X = v => (v/max)*pw;
    const s = [`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${opts.aria||''}">`];

    // A narrow plot cannot carry five tick labels without them running together.
    (pw < 240 ? [0,0.5,1] : [0,0.25,0.5,0.75,1]).forEach(f => {
      const gx = m.l + pw*f;
      s.push(`<line x1="${gx}" y1="${m.t}" x2="${gx}" y2="${m.t+rows.length*rowH-8}" stroke="${css('--grid')}" stroke-width="1"/>`);
      s.push(`<text x="${gx}" y="${m.t+rows.length*rowH+12}" fill="${css('--muted')}" font-size="10.5" text-anchor="middle" font-family="system-ui">${pct(max*f,0)}</text>`);
    });

    rows.forEach((r,i) => {
      const y = m.t + i*rowH, bh = 15, by = y + (rowH-bh)/2 - 4;
      const v = r[valueKey], w = Math.max(2, X(v));
      const below = v < (opts.floor ?? 0.9);
      const fill = below ? css('--blue-300') : css('--blue');
      s.push(`<text x="${m.l-10}" y="${by+bh-3}" fill="${css('--ink')}" font-size="12" text-anchor="end" font-family="system-ui">${clip(r[labelKey])}</text>`);
      s.push(`<rect class="bar" data-i="${i}" x="${m.l}" y="${by}" width="${w}" height="${bh}" rx="4" fill="${fill}" style="cursor:pointer"/>`);
      // Parked in the right-hand gutter rather than chased along the bar end.
      // Trailing the bar puts the label wherever the value happens to land,
      // which is on top of the floor line for any value near the floor.
      s.push(`<text x="${W-m.r+8}" y="${by+bh-3}" fill="${css('--muted')}" font-size="11.5" font-family="system-ui" font-variant-numeric="tabular-nums">${pct(v)}</text>`);
    });

    if (opts.floor){
      const fx = m.l + X(opts.floor);
      s.push(`<line x1="${fx}" y1="${m.t-2}" x2="${fx}" y2="${m.t+rows.length*rowH-6}" stroke="${css('--critical')}" stroke-width="2" stroke-dasharray="4 3"/>`);
      s.push(`<text x="${fx}" y="${m.t-5}" fill="${css('--critical')}" font-size="10" text-anchor="middle" font-family="system-ui">floor ${pct(opts.floor,0)}</text>`);
    }
    s.push('</svg>');
    host.innerHTML = s.join('');
    host.querySelectorAll('.bar').forEach(el => {
      const r = rows[+el.dataset.i];
      el.addEventListener('mousemove', e => showTip(e,
        `<b>${r[labelKey]}</b>pace ${pct(r[valueKey])}<br>` +
        `${num(r.units_delivered)} of ${num(r.units_authorized)} units<br>` +
        `${num(r.hours_unused)} hours unused · ${num(r.authorizations)} auths`));
      el.addEventListener('mouseleave', hideTip);
    });
  }

  function lineChart(hostId, rows, xKey, yKey, opts){
    opts = opts || {};
    const host = $(hostId);
    const W = Math.max(320, host.clientWidth || 900);
    const m = {t:16, r:20, b:34, l:56}, H = opts.height || 240;
    const pw = W-m.l-m.r, ph = H-m.t-m.b;
    const vals = rows.map(r => r[yKey]);
    // An auto-scaled top of 18,829 gives gridlines at 4,707 and 9,414. Round it
    // up to a 1/2/5 x 10^n step so the four ticks are numbers a reader can hold.
    const niceCeil = v => {
      if (!(v > 0)) return 1;
      const p = Math.pow(10, Math.floor(Math.log10(v)));
      return [1,2,2.5,5,10].map(k => k*p).find(c => c >= v - 1e-9) || 10*p;
    };
    const lo = opts.min !== undefined ? opts.min : Math.min(...vals)*0.92;
    const hi = opts.max !== undefined ? opts.max : niceCeil(Math.max(...vals)*1.06);
    const X = i => m.l + (rows.length===1 ? pw/2 : (i/(rows.length-1))*pw);
    const Y = v => m.t + ph - ((v-lo)/(hi-lo))*ph;
    const s = [`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${opts.aria||''}">`];

    for (let k=0;k<=4;k++){
      const v = lo + (hi-lo)*k/4, gy = Y(v);
      s.push(`<line x1="${m.l}" y1="${gy}" x2="${W-m.r}" y2="${gy}" stroke="${css('--grid')}" stroke-width="1"/>`);
      s.push(`<text x="${m.l-9}" y="${gy+4}" fill="${css('--muted')}" font-size="10.5" text-anchor="end" font-family="system-ui">${opts.fmt ? opts.fmt(v) : num(v)}</text>`);
    }
    rows.forEach((r,i) => s.push(`<text x="${X(i)}" y="${m.t+ph+18}" fill="${css('--muted')}" font-size="10.5" text-anchor="middle" font-family="system-ui">${String(r[xKey]).slice(2)}</text>`));

    if (opts.reference !== undefined){
      const ry = Y(opts.reference);
      s.push(`<line x1="${m.l}" y1="${ry}" x2="${W-m.r}" y2="${ry}" stroke="${css('--critical')}" stroke-width="1.5" stroke-dasharray="4 3"/>`);
      s.push(`<text x="${W-m.r}" y="${ry-5}" fill="${css('--critical')}" font-size="10.5" text-anchor="end" font-family="system-ui">${opts.referenceLabel||''}</text>`);
    }
    if (opts.markIndex !== undefined && opts.markIndex >= 0){
      const mx = X(opts.markIndex);
      s.push(`<line x1="${mx}" y1="${m.t}" x2="${mx}" y2="${m.t+ph}" stroke="${css('--warn')}" stroke-width="2"/>`);
      s.push(`<text x="${mx+6}" y="${m.t+ph-8}" fill="${css('--warn')}" font-size="10.5" font-family="system-ui">${opts.markLabel||''}</text>`);
    }

    s.push(`<path d="${rows.map((r,i)=>`${i?'L':'M'}${X(i)},${Y(r[yKey])}`).join(' ')}" fill="none" stroke="${css('--blue')}" stroke-width="2" stroke-linejoin="round"/>`);
    rows.forEach((r,i) => s.push(`<circle class="pt" data-i="${i}" cx="${X(i)}" cy="${Y(r[yKey])}" r="4.5" fill="${css('--blue')}" stroke="${css('--surface')}" stroke-width="2" style="cursor:pointer"/>`));
    s.push('</svg>');
    host.innerHTML = s.join('');
    host.querySelectorAll('.pt').forEach(el => {
      const r = rows[+el.dataset.i];
      el.addEventListener('mousemove', e => showTip(e, opts.tip ? opts.tip(r) : `<b>${r[xKey]}</b>${num(r[yKey])}`));
      el.addEventListener('mouseleave', hideTip);
    });
  }

  const covRows = D.monthly.map(r => ({...r}));
  const dropIdx = (() => {
    let worst = -1, worstDrop = 0;
    for (let i=1;i<covRows.length;i++){
      const d = covRows[i-1].uom_coverage - covRows[i].uom_coverage;
      if (d > worstDrop){ worstDrop = d; worst = i; }
    }
    return worst;
  })();

  function drawAll(){
    barChart('chartPayer', D.by_payer, 'payer_name', 'pace',
      {floor: h.floor, labelWidth: 168, aria:'Delivery pace by payer'});
    barChart('chartDiscipline', D.by_discipline, 'discipline', 'pace',
      {floor: h.floor, labelWidth: 116, aria:'Delivery pace by discipline'});
    lineChart('chartMonthly', D.monthly, 'year_month', 'hours_delivered',
      {min:0, height:230, aria:'Hours delivered by month',
       tip: r => `<b>${r.year_month}</b>${num(r.hours_delivered)} hours<br>${num(r.sessions)} completed sessions`});
    lineChart('chartCoverage', covRows, 'year_month', 'uom_coverage',
      {min:0.85, max:1.0, height:230, fmt: v => pct(v,1),
       reference: 0.99, referenceLabel: 'coverage floor 99%',
       markIndex: dropIdx, markLabel: 'source change',
       aria:'Unit-of-measure coverage by month',
       tip: r => `<b>${r.year_month}</b>${pct(r.uom_coverage,1)} of sessions usable<br>${num(r.sessions)} sessions`});
  }

  // The last month on the chart stops at the as-of date. Unlabelled, its drop
  // reads as a collapse in delivery rather than a month that is not over.
  (function(){
    const last = D.monthly[D.monthly.length-1];
    if (last && String(m.as_of).slice(0,7) === last.year_month){
      $('monthlySub').innerHTML +=
        ` <b>${last.year_month} is a partial month</b> — data runs to ${m.as_of}, ` +
        `so the final point is not comparable with the ones before it.`;
    }
  })();

  $('coverageSub').innerHTML =
    'Share of sessions whose duration could be interpreted. The step in ' +
    `<b>${dropIdx>=0 ? covRows[dropIdx].year_month : 'n/a'}</b> is the source-system change, ` +
    'not a change in clinical practice — see <code>docs/ANOMALY.md</code>.';

  /* ---------- spread ---------- */
  const c = D.comparison;
  $('spreadCard').innerHTML = `
    <p style="margin:0 0 12px;font-size:13.5px;color:var(--ink-2)">
      ${num(c.affected_sessions)} completed sessions arrived with a duration but no unit of
      measure &mdash; the part of the ${num(uomExcluded)} excluded sessions where a guess was
      even possible. Both available assumptions are defensible and they differ by roughly a
      factor of fifteen. Neither raises an error.</p>
    <table>
      <thead><tr><th>Approach</th><th class="n">Hours delivered</th><th>Consequence</th></tr></thead>
      <tbody>
        <tr><td>Assume the value is <b>minutes</b></td><td class="n">${num(c.if_assumed_minutes_hours)}</td>
            <td>Understates delivery. Looks plausible.</td></tr>
        <tr><td>Assume the value is <b>units</b></td><td class="n">${num(c.if_assumed_units_hours)}</td>
            <td>Overstates delivery. Also looks plausible.</td></tr>
        <tr style="background:var(--wash)"><td><b>Exclude and publish the coverage</b></td>
            <td class="n"><b>${num(c.resolved_hours)}</b></td>
            <td>Correct over ${pct(coverage,1)} of sessions, and says so.</td></tr>
      </tbody>
    </table>
    <p style="margin:12px 0 0;font-size:13.5px;color:var(--ink-2)">
      The gap between the two guesses is <b>${num(c.spread_hours)} hours</b>
      (${pct(c.spread_relative,1)} of delivered volume). That is the cost of picking one
      and not writing it down.</p>`;

  /* ---------- checks ---------- */
  const sevClass = {BLOCK:'blockc', WARN:'warnc', INFO:'mutedc'};
  const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : many}`;
  $('qualitySub').innerHTML =
    `${q.summary.total} checks · ${q.summary.passed} passed · ` +
    `${plural(q.summary.block, 'blocking failure', 'blocking failures')} · ` +
    `${plural(q.summary.warn, 'warning', 'warnings')} · ` +
    `rule set v${q.ruleset_version} <code>${q.ruleset_hash}</code>`;
  $('checks').innerHTML = q.results.map(r => `
    <div class="chk">
      <div class="sev ${r.passed ? 'ok' : sevClass[r.severity]}">${r.passed ? 'PASS' : r.severity}</div>
      <div class="body"><div class="nm">${r.name}</div><p class="ms">${r.message}</p></div>
    </div>`).join('');

  // Naming the S3 bucket on a run that never touched S3 reads as a claim the
  // run cannot support. Show the path the data actually took.
  const lakeText = m.lake_backend === 's3'
    ? `lake <code>s3://${m.bucket}</code>`
    : `lake: local filesystem (S3 target <code>s3://${m.bucket}</code> not used on this run)`;
  $('foot').innerHTML =
    `Generated ${m.generated_at_utc} · ${lakeText}` +
    ` · every figure recomputed from the warehouse on each run. ` +
    `The Power BI semantic model in <code>bi/measures.dax</code> reproduces these ` +
    `same measures from the same exports.`;

  drawAll();
  addEventListener('resize', drawAll);
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { sync(); drawAll(); });
})();
</script>
</body>
</html>
"""


def main() -> int:
    if not PAYLOAD.exists():
        print(f"No payload at {PAYLOAD}. Run the pipeline first.")
        return 2
    payload = PAYLOAD.read_text(encoding="utf-8")
    # Guard the closing script tag; a "</script>" inside JSON would end the block.
    payload = payload.replace("</", "<\\/")
    OUTPUT.write_text(TEMPLATE.replace("__PAYLOAD__", payload), encoding="utf-8")
    print(f"wrote {OUTPUT}  ({OUTPUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
