/* ============================== suggestions de recherche ============================== */
/* Sous la barre de recherche, pendant la frappe : espèces et rangs (règne… genre) dont un nom commun (toutes
   langues) ou le nom scientifique correspond, sans tenir compte des accents ni des majuscules. Début de nom
   d'abord, puis début de mot, puis n'importe où. Flèches pour choisir, Entrée pour ouvrir, Échap pour fermer ;
   Entrée sans choix : tous les résultats, sur la page Espèces. Lieux, appareils et fichiers : demandés au serveur. */
const SUG = { items: [], sel: -1, src: null, entries: [], req: 0 };
const fold = s => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
async function sugEntries() {
  if (!SP.all.length) { try { SP.all = await api('/api/species'); } catch (e) { return []; } }
  if (SUG.src === SP.all) return SUG.entries;
  const ranks = new Map();   // « rang/nom » -> espèces
  SP.all.forEach(s => HIGHER_RANKS.forEach(r => { const n = s.taxon?.lineage?.[r];
    if (n) ranks.set(`${r}/${n}`, [...(ranks.get(`${r}/${n}`) || []), s]); }));
  const missing = [...new Set([...ranks.keys()].map(k => k.split('/')[1]))].filter(n => !(SP.higher || {})[n]);
  if (missing.length) {
    try { SP.higher = { ...(SP.higher || {}), ...await api('/api/higher-taxa', { method: 'POST', body: JSON.stringify({ names: missing }) }) }; } catch (e) {}
  }
  const species = SP.all.map(s => { const tx = s.taxon || {};
    return { href: `species/${s.taxon_key}`, title: shortName(tx), sub: common(tx) ? tx.scientific_name : '', cover: s.cover, box: s.cover_box,
             n: s.n, hay: [tx.scientific_name, common(tx)], other: Object.values(tx.vernacular || {}), kind: 'sp' }; });
  const higher = [...ranks.entries()].map(([k, list]) => { const [rank, name] = k.split('/'), { main, sci } = higherNames(name);
    const top = [...list].sort((a, b) => b.n - a.n)[0];
    return { href: `${rank}/${encodeURIComponent(name)}`, title: main, sub: `${sci ? `${sci} · ` : ''}${rankName(rank)} · ${t('count.species_short', { n: list.length })}`,
             cover: top.cover, box: top.cover_box, n: list.length, hay: [name, higherCommon(name)], other: Object.values(SP.higher?.[name]?.vernacular || {}), kind: 'rank' }; });
  SUG.src = SP.all; SUG.entries = [...species, ...higher];
  return SUG.entries;
}
/* Langue de l'interface et nom scientifique : début de nom (3), début de mot (2), n'importe où (1). Autres
   langues : début de nom ou de mot seulement, classés après (sinon « ren » sortait d'abord le raton laveur). */
function sugScore(e, q) {
  const word = new RegExp(`[\\s'’,(-]${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`);
  const of = h => { const f = fold(h); return !f ? 0 : f.startsWith(q) ? 3 : word.test(f) ? 2 : f.includes(q) ? 1 : 0; };
  let best = Math.max(0, ...e.hay.map(of));
  for (const h of e.other) { const s = of(h); if (s >= 2) best = Math.max(best, s - 1.5); }   // 1,5 ou 0,5
  return best;
}
async function showSuggestions() {
  const q = fold($('#gsearch').value.trim()), req = ++SUG.req, box = $('#sugbox');
  if (q.length < 2) { box.hidden = true; SUG.items = []; SUG.sel = -1; return; }
  const raw = $('#gsearch').value.trim();
  const [all, more] = await Promise.all([sugEntries(), api(`/api/search?q=${encodeURIComponent(raw)}`).catch(() => ({ places: [], cameras: [], files: [] }))]);
  if (req !== SUG.req) return;
  const ranked = all.map(e => ({ e, s: sugScore(e, q) })).filter(x => x.s).sort((a, b) => b.s - a.s || b.e.n - a.e.n).map(x => x.e);
  const sp = ranked.filter(e => e.kind === 'sp').slice(0, 6), hi = ranked.filter(e => e.kind === 'rank').slice(0, 4);
  const others = [
    ...more.places.map(p => ({ href: `photos?filter=all&${p.city ? `city=${encodeURIComponent(p.city)}` : `country=${encodeURIComponent(p.country)}`}`, title: p.city || p.country, sub: `${p.city ? `${p.country || ''} · ` : ''}${t('count.photos', { n: p.n })}`, cover: p.cover, kind: 'place' })),
    ...more.cameras.map(c => ({ href: `photos?filter=all&camera=${encodeURIComponent(c.camera)}`, title: c.camera, sub: t('count.photos', { n: c.n }), cover: c.cover, kind: 'camera' })),
    ...more.files.map(f => ({ href: `photos?filter=all&photo=${encodeURIComponent(f.asset_id)}`, title: f.file_name, sub: '', cover: f.asset_id, kind: 'file' })),
  ];
  const all_ = { href: `species?q=${encodeURIComponent(raw)}`, title: t('search.all'), kind: 'all' };
  SUG.items = [...sp, ...hi, ...others, ...(sp.length + hi.length ? [all_] : [])]; SUG.sel = -1;
  const row = (e, i) => `<a class="sug" href="#${e.href}" data-i="${i}"><img class="${e.kind}" src="/api/thumb/${e.cover}?size=thumbnail" style="${focus(e.box)}" alt="">
    <span><b>${esc(e.title)}</b>${e.sub ? `<small>${esc(e.sub)}</small>` : ''}</span></a>`;
  const section = (head, kind) => { const list = SUG.items.map((e, i) => [e, i]).filter(([e]) => e.kind === kind);
    return list.length ? `<div class="sughead">${head}</div>${list.map(([e, i]) => row(e, i)).join('')}` : ''; };
  box.innerHTML = !SUG.items.length ? `<div class="sugnone">${t('search.none')}</div>`
    : section(t('nav.species'), 'sp') + section(t('search.ranks'), 'rank') + section(t('search.places'), 'place')
      + section(t('search.cameras'), 'camera') + section(t('search.files'), 'file')
      + (SUG.items.at(-1).kind === 'all' ? `<a class="sug sugall" href="#${all_.href}" data-i="${SUG.items.length - 1}"><span><b>${esc(all_.title)}</b></span></a>` : '');
  box.hidden = false;
}
function sugMove(d) {
  const n = SUG.items.length; if (!n) return;
  SUG.sel = SUG.sel + d < -1 ? n - 1 : SUG.sel + d >= n ? -1 : SUG.sel + d;   // -1 : aucun choix (texte tapé)
  $$('#sugbox .sug').forEach(a => a.classList.toggle('sel', +a.dataset.i === SUG.sel));
  $('#sugbox .sug.sel')?.scrollIntoView({ block: 'nearest' });
}
function sugClose() { $('#sugbox').hidden = true; SUG.sel = -1; }
(() => {
  const input = $('#gsearch');
  input.insertAdjacentHTML('afterend', '<div id="sugbox" class="sugbox" hidden></div>');
  input.setAttribute('autocomplete', 'off');
  input.addEventListener('input', showSuggestions);
  input.addEventListener('focus', showSuggestions);
  input.addEventListener('blur', () => setTimeout(sugClose, 150));
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && SUG.sel < 0 && input.value.trim()) {   // tous les résultats
      location.hash = `species?q=${encodeURIComponent(input.value.trim())}`; input.value = ''; sugClose(); input.blur(); return;
    }
    if ($('#sugbox').hidden) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); sugMove(e.key === 'ArrowDown' ? 1 : -1); }
    else if (e.key === 'Escape') sugClose();
    else if (e.key === 'Enter' && SUG.sel >= 0) { e.preventDefault();
      location.hash = SUG.items[SUG.sel].href; input.value = ''; sugClose(); input.blur(); }
  });
  $('#sugbox').addEventListener('mousedown', e => e.preventDefault());   // garder le focus jusqu'au clic
  $('#sugbox').addEventListener('click', e => { if (e.target.closest('.sug')) { input.value = ''; sugClose(); input.blur(); } });
})();
