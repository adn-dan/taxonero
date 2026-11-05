/* ============================== parois déplaçables ============================== */
/* Préférences de mise en page, propres à ce navigateur : part de la colonne de gauche (infos et article) à côté
   de la carte (%), hauteur du bloc (px).
   Souris ou doigt pour déplacer, flèches du clavier, double-clic pour revenir au réglage par défaut. */
const LAYOUT_DEFAULT = { split: 45, blockh: 780 };
const LAYOUT = { ...LAYOUT_DEFAULT };
try { Object.assign(LAYOUT, JSON.parse(localStorage.getItem('layout') || '{}')); } catch (e) {}
const saveLayout = () => { try { localStorage.setItem('layout', JSON.stringify(LAYOUT)); } catch (e) {} };
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const separator = (cls, orient) => `<div class="${cls}" tabindex="0" role="separator" aria-orientation="${orient}" title="${esc(t('layout.drag'))}"></div>`;
/* opts : start() -> état au début du geste ; move(dx, dy, état) ; step(sens) pour le clavier ; reset() ; done(). */
function splitter(handle, opts) {
  if (!handle) return;
  handle.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    e.preventDefault(); handle.setPointerCapture(e.pointerId); handle.classList.add('drag');
    const x0 = e.clientX, y0 = e.clientY, st = opts.start();
    const move = ev => opts.move(ev.clientX - x0, ev.clientY - y0, st);
    const up = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up); handle.classList.remove('drag'); opts.done?.(); saveLayout(); };
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up); handle.addEventListener('pointercancel', up);
  });
  handle.addEventListener('keydown', e => {
    const d = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
    if (d) { e.preventDefault(); opts.step(d); opts.done?.(); saveLayout(); }
  });
  handle.addEventListener('dblclick', () => { opts.reset(); opts.done?.(); saveLayout(); });
}
/* À gauche les infos (l'en-tête .sphero, déplacé ici par bindMid) et l'article dessous ; à droite la carte sur
   toute la hauteur : bas de la carte et bas de l'article alignés, le texte de l'article défile dans sa boîte. */
function midHTML(id, hint) {
  return `<div class="spmidc"><div class="spmid">
      <section class="card spwiki">${wikiCardHTML(id)}</section>${separator('vsplit', 'vertical')}${mapCardHTML(id, hint)}
    </div>${separator('hsplit', 'horizontal')}</div>`;
}
function applyMid() {
  $$('.spmid').forEach(m => { m.style.setProperty('--split', `${LAYOUT.split}%`); m.style.setProperty('--blockh', `${LAYOUT.blockh}px`); });
}
function bindMid(P, root) {
  const mid = root.querySelector('.spmid'), refit = () => requestAnimationFrame(() => { if (P.map?.getContainer().isConnected) P.map.invalidateSize(); });
  const hero = [...root.querySelectorAll('.sphero')].find(h => !h.closest('.spmid'));
  if (hero) mid.prepend(hero);
  applyMid();
  splitter(root.querySelector('.vsplit'), {
    start: () => ({ split: LAYOUT.split, w: mid.getBoundingClientRect().width }),
    move: (dx, dy, s) => { LAYOUT.split = clamp(s.split + dx / s.w * 100, 20, 75); applyMid(); },
    step: d => { LAYOUT.split = clamp(LAYOUT.split + d * 5, 20, 75); applyMid(); },
    reset: () => { LAYOUT.split = LAYOUT_DEFAULT.split; applyMid(); }, done: refit });
  splitter(root.querySelector('.hsplit'), {
    start: () => ({ blockh: mid.getBoundingClientRect().height }),
    move: (dx, dy, s) => { LAYOUT.blockh = Math.round(clamp(s.blockh + dy, 480, 1600)); applyMid(); },
    step: d => { LAYOUT.blockh = Math.round(clamp(mid.getBoundingClientRect().height + d * 40, 480, 1600)); applyMid(); },
    reset: () => { LAYOUT.blockh = LAYOUT_DEFAULT.blockh; applyMid(); }, done: refit });
}
