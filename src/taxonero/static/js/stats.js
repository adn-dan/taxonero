/* ============================== page Statistiques ============================== */
/* Graphiques en SVG natif. Règles (skill dataviz) : barres <= 24 px, 4 px arrondis côté valeur,
   2 px d'intervalle entre segments, lignes 2 px, points >= 8 px sans anneau (contour au survol),
   texte toujours en couleur d'encre, légende dès deux séries, infobulle sur chaque marque. */
/* data-tip : HTML de l'infobulle, échappé une seconde fois pour l'attribut (le navigateur le décode, puis
   innerHTML l'interprète) : les noms venus des données y sont donc échappés deux fois. */
function tipOn(el) {
  el.querySelectorAll('[data-tip]').forEach(m => {
    m.onmousemove = e => { const t = $('#tip'); t.innerHTML = m.dataset.tip; t.style.display = 'block';
      t.style.left = `${Math.min(e.clientX + 12, innerWidth - t.offsetWidth - 8)}px`; t.style.top = `${e.clientY + 14}px`; };
    m.onmouseleave = () => { $('#tip').style.display = 'none'; };
  });
}
// Maximum « rond » et pair pour que la graduation du milieu reste un entier (on compte des espèces, des photos).
const niceMax = v => { const p = Math.pow(10, Math.floor(Math.log10(Math.max(1, v)))); const m = [1, 2, 3, 4, 5, 6, 8, 10].map(k => k * p).find(k => k >= v) || v; return Math.max(2, m % 2 ? m + 1 : m); };

function accumulationSVG(obs, taxa, W = 540) {
  // Première observation de chaque taxon -> courbe en escalier du nombre d'espèces cumulé.
  const first = new Map();
  obs.filter(o => o.taken_at).forEach(o => { if (!first.has(o.taxon_key)) first.set(o.taxon_key, o.taken_at); });
  const pts = [...first.entries()].map(([k, d]) => ({ k, d: new Date(d) })).sort((a, b) => a.d - b.d);
  if (pts.length < 2) return `<div class="sub">${t('stats.not_enough')}</div>`;
  const H = 220, ml = 34, mr = 14, mt = 16, mb = 26;   // largeur réelle du bloc, texte à taille fixe
  const t0 = +pts[0].d, t1 = +pts[pts.length - 1].d || t0 + 1, ymax = niceMax(pts.length);
  const x = d => ml + (W - ml - mr) * ((+d - t0) / Math.max(1, t1 - t0)), y = n => H - mb - (H - mt - mb) * (n / ymax);
  let path = `M${x(pts[0].d)},${y(0)}`;
  pts.forEach((p, i) => { path += `L${x(p.d)},${y(i)}L${x(p.d)},${y(i + 1)}`; });
  const years = []; for (let yr = new Date(t0).getFullYear() + 1; yr <= new Date(t1).getFullYear(); yr++) years.push(yr);
  const step = Math.max(1, Math.ceil(years.length / Math.max(2, Math.floor((W - ml - mr) / 60))));   // ~60 px par année affichée
  const grid = [0, ymax / 2, ymax].map(v => `<line x1="${ml}" x2="${W - mr}" y1="${y(v)}" y2="${y(v)}" class="gl"/><text x="${ml - 6}" y="${y(v) + 4}" class="ax" text-anchor="end">${v}</text>`).join('');
  const xt = years.filter((_, i) => i % step === 0).map(yr => { const xx = x(new Date(`${yr}-01-01`)); return `<line x1="${xx}" x2="${xx}" y1="${H - mb}" y2="${H - mb + 4}" class="gl"/><text x="${xx}" y="${H - 8}" class="ax" text-anchor="middle">${yr}</text>`; }).join('');
  // Chaque point prend la couleur du groupe de l'espèce.
  const dots = pts.map((p, i) => { const tx = taxa[p.k], g = groupOf(tx);
    return `<circle cx="${x(p.d)}" cy="${y(i + 1)}" r="4.5" class="dotm" style="fill:${g.c}" data-href="species/${p.k}" data-tip="${esc(`<b>${esc(shortName(tx))}</b><br>${g.icon} ${esc(g.name)}<br>${fmtDate(p.d, {})} · ${esc(t('stats.nth_species', { i: i + 1 }))}`)}"/>`; }).join('');
  const last = pts.length;
  const used = groupsFor(pts.map(p => taxa[p.k])).filter(g => pts.some(p => groupOf(taxa[p.k]) === g));
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${esc(t('stats.accumulation'))}">${grid}${xt}
    <path d="${path}" class="area" /><path d="${path}" class="line"/>${dots}
    <text x="${x(pts[last - 1].d) - 6}" y="${y(last) - 10}" class="lab" text-anchor="end">${t('count.species', { n: last })}</text></svg>
    <div class="legend">${groupLegend(used)}</div>`;
}

function seasonSVG(obs, taxa, W = 540) {
  // Observations par mois de l'année (toutes années), empilées par groupe.
  const months = Array.from({ length: 12 }, () => ({}));
  obs.filter(o => o.taken_at).forEach(o => { const m = new Date(o.taken_at).getUTCMonth(), g = groupOf(taxa[o.taxon_key]).id; months[m][g] = (months[m][g] || 0) + 1; });
  const tot = months.map(m => Object.values(m).reduce((a, b) => a + b, 0)), ymax = niceMax(Math.max(1, ...tot));
  const H = 220, ml = 28, mr = 6, mt = 14, mb = 26, slot = (W - ml - mr) / 12, bw = Math.min(24, slot * 0.62);
  const y = v => (H - mt - mb) * (v / ymax);
  const names = Array.from({ length: 12 }, (_, i) => new Date(2001, i, 1).toLocaleDateString(LANG, { month: 'short' }).replace('.', ''));
  let h = [0, ymax / 2, ymax].map(v => `<line x1="${ml}" x2="${W - mr}" y1="${H - mb - y(v)}" y2="${H - mb - y(v)}" class="gl"/><text x="${ml - 6}" y="${H - mb - y(v) + 4}" class="ax" text-anchor="end">${v}</text>`).join('');
  months.forEach((m, i) => {
    let base = H - mb; const cx = ml + slot * i + (slot - bw) / 2;
    const segs = GROUPS.filter(g => m[g.id]);   // groupes déjà créés par groupOf ci-dessus
    segs.forEach((g, j) => {
      const hh = y(m[g.id]), top = j === segs.length - 1;
      const yy = base - hh + (j ? 0 : 0);
      h += top ? `<path d="M${cx},${base}V${yy + 4}Q${cx},${yy} ${cx + 4},${yy}H${cx + bw - 4}Q${cx + bw},${yy} ${cx + bw},${yy + 4}V${base}Z"`
               : `<rect x="${cx}" y="${yy}" width="${bw}" height="${Math.max(0, hh - 2)}"`;
      h += ` fill="${g.c}" data-href="species?group=${g.id}&month=${i + 1}" data-tip="${esc(`<b>${names[i]}</b> · ${g.icon} ${esc(g.name)} : ${t('count.observations', { n: m[g.id] })}`)}"/>`;
      base -= hh;
    });
    h += `<text x="${cx + bw / 2}" y="${H - 8}" class="ax" text-anchor="middle"${tot[i] ? ` data-href="species?month=${i + 1}"` : ''}>${names[i]}</text>`;
    if (tot[i]) h += `<text x="${cx + bw / 2}" y="${H - mb - y(tot[i]) - 5}" class="ax" text-anchor="middle">${tot[i]}</text>`;
  });
  const used = GROUPS.filter(g => months.some(m => m[g.id]));
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${esc(t('stats.per_month'))}">${h}</svg>
    <div class="legend">${groupLegend(used)}</div>`;
}

function hbars(rows, unit = '') {
  // Barres horizontales : libellé et valeur en encre, marque colorée à côté.
  const m = Math.max(1, ...rows.map(r => r.v));
  return rows.map(r => `<div class="hbar" ${r.tip ? `data-tip="${esc(r.tip)}"` : ''}${r.href ? ` data-href="${esc(r.href)}" role="link" tabindex="0"` : ''}><span class="k">${r.k}</span>
    <span class="t"><i style="width:${r.v / m * 100}%${r.c ? `;background:${r.c}` : ''}"></i></span><span class="v">${r.v}${unit}</span></div>`).join('') || '<span class="sub">—</span>';
}

let statsMap, statsRO;
async function loadStats() {
  const [{ observations: obs, taxa }, s, rv] = await Promise.all([api('/api/observations'), api('/api/status'), api('/api/review-stats')]);
  const root = $('#stats');
  if (!obs.length) { root.innerHTML = `<div class="empty">${t('stats.empty')}</div>`; return; }
  const species = new Set(obs.map(o => o.taxon_key));
  const photos = new Set(obs.map(o => o.asset_id));
  const countries = new Map(), cities = new Set(), days = new Set();
  obs.forEach(o => { if (o.country) { const c = countries.get(o.country) || { p: new Set(), s: new Set() }; c.p.add(o.asset_id); c.s.add(o.taxon_key); countries.set(o.country, c); }
                     if (o.city) cities.add(`${o.city}|${o.country}`); if (o.taken_at) days.add(o.taken_at.slice(0, 10)); });
  const threatened = [...species].filter(k => THREATENED.includes(taxa[k]?.iucn)).length;
  const year = new Date().getFullYear();
  const firstSeen = new Map(); obs.forEach(o => { if (o.taken_at && !firstSeen.has(o.taxon_key)) firstSeen.set(o.taxon_key, o.taken_at); });
  const newThisYear = [...firstSeen.values()].filter(d => new Date(d).getFullYear() === year).length;
  // Espèces les plus photographiées
  const perSp = new Map(); obs.forEach(o => perSp.set(o.taxon_key, (perSp.get(o.taxon_key) || 0) + 1));
  const top = [...perSp.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10)
    .map(([k, v]) => ({ k: esc(shortName(taxa[k])), v, c: groupOf(taxa[k]).c, href: `species/${k}` }));
  const byGroup = groupsFor([...species].map(k => taxa[k])).map(g => ({ g, k: `${g.icon} ${esc(g.name)}`, v: [...species].filter(k => groupOf(taxa[k]) === g).length, c: g.c, href: `species?group=${g.id}` })).filter(r => r.v);
  const iucnRows = IUCN_CATS.map(k => ({ k: esc(iucnName(k)), v: [...species].filter(x => (taxa[x]?.iucn || 'NOT_EVALUATED') === k).length, c: IUCN_COLOR[k], href: `species?iucn=${k}` })).filter(r => r.v);
  const ctry = [...countries.entries()].sort((a, b) => b[1].p.size - a[1].p.size).slice(0, 8)
    .map(([c, v]) => ({ k: esc(c), v: v.p.size, tip: `${esc(c)} : ${t('count.photos', { n: v.p.size })}, ${t('count.species', { n: v.s.size })}`, href: `species?country=${encodeURIComponent(c)}` }));
  const gear = new Map(); obs.forEach(o => { const k = [o.make, o.model].filter(Boolean).join(' '); const g = gear.get(k) || { p: new Set(), s: new Set(), lens: new Set() };
    g.p.add(o.asset_id); g.s.add(o.taxon_key); if (o.lens) g.lens.add(o.lens); gear.set(k, g); });
  const reviewed = rv.kept + rv.corrected;
  const agree = reviewed ? Math.round(rv.kept / reviewed * 100) : null;
  const totalDet = rv.auto + rv.user + rv.pending + rv.rejected;
  const seg = (v, c, name, filter) => v ? `<span style="flex:${v};background:${c}" data-tip="${esc(`${name} : ${v}`)}" data-href="photos?filter=${filter}"></span>` : '';
  root.innerHTML = `
    <div class="kpis wide">
      <div class="card kpi hero" data-href="species" role="link" tabindex="0"><b>${fmtN(species.size)}</b><span>${t('stats.kpi_species', { n: species.size })}</span></div>
      <div class="card kpi" data-href="photos?filter=identified" role="link" tabindex="0"><b>${fmtN(photos.size)}</b><span>${t('stats.kpi_photos', { n: photos.size })}</span></div>
      <div class="card kpi"${newThisYear ? ` data-href="species?year=${year}" role="link" tabindex="0"` : ''}><b>${fmtN(newThisYear)}</b><span>${t('stats.kpi_new', { n: newThisYear, year: String(year) })}</span></div>
      <div class="card kpi"${threatened ? ' data-href="species?iucn=threatened" role="link" tabindex="0"' : ''}><b>${fmtN(threatened)}</b><span>${t('stats.kpi_threatened', { n: threatened })}</span></div>
      <div class="card kpi" data-href="photos?filter=identified" role="link" tabindex="0"><b>${fmtN(days.size)}</b><span>${t('stats.kpi_days', { n: days.size })}</span></div>
      <div class="card kpi" data-href="photos?filter=identified" role="link" tabindex="0"><b>${fmtN(cities.size)}</b><span>${t('stats.kpi_places', { n: cities.size, countries: t('count.countries', { n: countries.size }) })}</span></div>
    </div>
    <div class="sgrid">
      <section class="card"><h3>${t('stats.accumulation')}</h3><p class="sub">${t('stats.accumulation_sub')}</p><div class="chartbox" id="ch-acc"></div></section>
      <section class="card"><h3>${t('stats.season')}</h3><p class="sub">${t('stats.season_sub')}</p><div class="chartbox" id="ch-season"></div></section>
      <section class="card"><h3>${t('stats.top')}</h3>${hbars(top, '')}</section>
      <section class="card"><h3>${t('stats.by_group')}</h3>${hbars(byGroup)}</section>
      <section class="card maprow"><h3>${t('stats.map')}</h3><p class="sub">${t('stats.map_sub')}</p><div id="statsmap"></div>
        <div class="legend">${groupLegend(byGroup.map(r => r.g))}</div></section>
      <section class="card"><h3>${t('iucn.long_title')}</h3><p class="sub">${t('stats.iucn_sub')}</p>${hbars(iucnRows)}</section>
      <section class="card"><h3>${t('stats.countries')}</h3><p class="sub">${t('stats.countries_sub')}</p>${hbars(ctry)}</section>
      <section class="card"><h3>${t('stats.review')}</h3>
        <div class="stackbar">${seg(rv.auto, 'var(--g-aves)', t('det.confirmed_auto'), 'auto')}${seg(rv.user, 'var(--g-insecta)', t('det.confirmed_user'), 'user')}${seg(rv.pending, 'var(--g-reptilia)', t('det.pending'), 'pending')}${seg(rv.rejected, 'var(--g-other)', t('filter.rejected'), 'rejected')}</div>
        <div class="legend"><span><i class="sw" style="background:var(--g-aves)"></i>${t('stats.leg_auto')} <b>${fmtN(rv.auto)}</b></span><span><i class="sw" style="background:var(--g-insecta)"></i>${t('stats.leg_user')} <b>${fmtN(rv.user)}</b></span>
          <span><i class="sw" style="background:var(--g-reptilia)"></i>${t('det.pending')} <b>${fmtN(rv.pending)}</b></span><span><i class="sw" style="background:var(--g-other)"></i>${t('filter.rejected')} <b>${fmtN(rv.rejected)}</b></span></div>
        <p>${t('stats.auto_share', { pct: fmtPct(totalDet ? rv.auto / totalDet : 0) })}
          ${reviewed ? ` ${t('stats.kept', { n: reviewed, pct: fmtPct(agree / 100), corrections: t('count.corrections', { n: rv.corrected }) })}` : ''}
          ${rv.audits ? ` ${t('stats.audits', { ok: rv.audits_ok, n: rv.audits, pct: fmtPct(rv.audits_ok / rv.audits) })}` : ''}
          ${rv.manual_boxes ? ` ${t('stats.manual_boxes', { n: rv.manual_boxes })}` : ''}
          ${rv.deleted ? ` ${t('stats.deleted', { n: rv.deleted })}` : ''}</p>
        <p class="sub">${t('stats.mode', { mode: `<b>${esc(tOr(`set.auto_confirm.${s.auto_confirm}`, s.auto_confirm))}</b>`, thr: s.threshold != null ? fmtPct(s.threshold) : '—', done: s.calibration.reviews, n: s.calibration.min_reviews })}</p></section>
      <section class="card"><h3>${t('stats.gear')}</h3><table>${[...gear.entries()].sort((a, b) => b[1].p.size - a[1].p.size).map(([k, g]) =>
        `<tr data-href="species?camera=${encodeURIComponent(k)}" role="link" tabindex="0"><td>${esc(k || t('stats.unknown_camera'))}<br><small>${esc([...g.lens].join(', ') || '')}</small></td><td>${t('count.photos', { n: g.p.size })}<br><small>${t('count.species', { n: g.s.size })}</small></td></tr>`).join('')}</table></section>
    </div>`;
  // Graphiques SVG redessinés à la largeur réelle de leur bloc (et à chaque redimensionnement).
  const charts = [['#ch-acc', w => accumulationSVG(obs, taxa, w)], ['#ch-season', w => seasonSVG(obs, taxa, w)]];
  const drawCharts = () => { charts.forEach(([sel, fn]) => { const el = $(sel); if (el) { el.innerHTML = fn(Math.max(280, el.clientWidth)); tipOn(el); } }); };
  drawCharts();
  statsRO?.disconnect();
  let rt; statsRO = new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(drawCharts, 120); });
  charts.forEach(([sel]) => $(sel) && statsRO.observe($(sel)));
  tipOn(root);
  // Clic (ou Entrée) sur un élément d'un graphique : page des espèces ou des photos, filtrée en conséquence.
  root.onclick = e => { const el = e.target.closest('[data-href]'); if (el) { $('#tip').style.display = 'none'; location.hash = el.dataset.href; } };
  root.onkeydown = e => { if (e.key === 'Enter') e.target.closest?.('[data-href]')?.click(); };
  if (statsMap) { statsMap.remove(); statsMap = null; }
  statsMap = L.map('statsmap', { ...MAP_TOUCH, worldCopyJump: true, scrollWheelZoom: true }).addLayer(baseLayer());
  const pts = obs.filter(o => o.lat != null);
  const css = getComputedStyle(document.documentElement);
  pts.forEach(o => { const t = taxa[o.taxon_key], g = groupOf(t);
    L.circleMarker([o.lat, o.lon], { radius: 6, stroke: false,
      fillColor: css.getPropertyValue(g.cv).trim(), fillOpacity: 1 })
      .bindPopup(`<a href="#species/${o.taxon_key}?photo=${encodeURIComponent(o.asset_id)}"><img src="/api/thumb/${o.asset_id}?size=thumbnail" style="width:170px;border-radius:8px"></a><br><a href="#species/${o.taxon_key}">${taxName(t)}</a><br><small>${esc([o.city, o.country].filter(Boolean).join(', '))}</small>`).addTo(statsMap); });
  if (pts.length) statsMap.fitBounds(pts.map(o => [o.lat, o.lon]), { padding: [30, 30], maxZoom: 8 }); else statsMap.setView([46.6, 2.4], 4);
  setTimeout(() => statsMap.invalidateSize(), 60);
}
