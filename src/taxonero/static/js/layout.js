/* ============================== movable dividers ============================== */
/* Layout preferences, specific to this browser: share of the left column (information and article) next to
   the map (%), height of the block (px).
   Mouse or finger to move, keyboard arrows, double click to go back to the default setting. */
const LAYOUT_DEFAULT = { split: 45, blockh: 780 };
const LAYOUT = { ...LAYOUT_DEFAULT };
try { Object.assign(LAYOUT, JSON.parse(localStorage.getItem('layout') || '{}')); } catch (e) {}
const saveLayout = () => { try { localStorage.setItem('layout', JSON.stringify(LAYOUT)); } catch (e) {} };
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const separator = (cls, orient) => `<div class="${cls}" tabindex="0" role="separator" aria-orientation="${orient}" title="${esc(t('layout.drag'))}"></div>`;
/* opts: start() -> state at the start of the gesture; move(dx, dy, state); step(direction) for the keyboard; reset(); done(). */
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
/* On the left the information (the .sphero header, moved here by bindMid) and the article below; on the right the map over
   the whole height: bottom of the map and bottom of the article aligned, the article text scrolls in its box. */
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
