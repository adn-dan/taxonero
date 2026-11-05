/* Visionneuse : rectangles (Annotorious) et zoom (Panzoom).
   - Annotorious dessine, sélectionne, déplace et redimensionne les rectangles, dans le repère en pixels de
     l'image affichée (vignette, aperçu ou original : les détections, normalisées 0..1, y sont converties à
     chaque chargement).
   - Panzoom zoome le calque #pz (photo, rectangles, étiquettes) : molette vers le curseur, pincement, clic à la
     souris (zoom ×3 ou retour), double appui au doigt, touches Z, + et -. Zoomé, un glisser déplace la photo ; il
     est laissé à Annotorious en mode dessin (B), et sur les poignées du rectangle sélectionné.
   - Clic sur un autre rectangle que le sélectionné : il devient le sujet affiché, sans zoom.
   - Tracer une boîte demande le mode dessin (B ou icône), à la souris comme au doigt.
   Les étiquettes (nom du taxon) sont un calque HTML au-dessus, de taille constante à l'écran. */
const HANDLES = '.a9s-handle, .a9s-shape-handle, .a9s-edge-handle, .a9s-corner-handle, .a9s-touch-handle';
const anno = Annotorious.createImageAnnotator($('#photo'), {
  drawingEnabled: false, drawingMode: 'drag', disableUndoRedoKeys: true, theme: 'dark',
});
const pz = Panzoom($('#pz'), { minScale: 1, maxScale: 8, contain: 'outside', excludeClass: 'pz-skip', cursor: '' });

let drawing = false;   // mode dessin (B ou icône) : une seule boîte, puis retour au mode normal
const zoomed = () => pz.getScale() > 1.001;
function syncDraw() {
  const on = drawing;
  if (anno.isDrawingEnabled() !== on) anno.setDrawingEnabled(on);
  $('#frame').classList.toggle('pz-skip', on);
}
function enableDraw() { drawing = true; $('#frame').classList.add('drawing'); $('#drawhint').classList.add('on'); syncDraw(); }
function stopDraw() {
  drawing = false; anno.cancelDrawing();
  $('#frame').classList.remove('drawing'); $('#drawhint').classList.remove('on'); syncDraw();
}

/* -- rectangles ------------------------------------------------------------ */
const natural = () => { const img = $('#photo'); return [img.naturalWidth, img.naturalHeight]; };
function toAnno(d) {
  const [W, H] = natural(), x = d.x * W, y = d.y * H, w = d.w * W, h = d.h * H;
  return { id: String(d.id), bodies: [], target: { annotation: String(d.id), selector: { type: 'RECTANGLE',
    geometry: { x, y, w, h, bounds: { minX: x, minY: y, maxX: x + w, maxY: y + h } } } } };
}
function fromGeometry(g) {
  const [W, H] = natural(), c = v => Math.min(1, Math.max(0, v));
  const x = c(g.x / W), y = c(g.y / H);
  return { x, y, w: Math.min(1 - x, g.w / W), h: Math.min(1 - y, g.h / H) };
}
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
function boxStyle(a) {
  const k = cur()?.detections.findIndex(d => String(d.id) === a.id), d = cur()?.detections[k];
  const color = d?.status === 'confirmed' ? css('--box-ok') : d?.status === 'rejected' ? 'rgba(255,255,255,.55)' : css('--box');
  // Épaisseur divisée par le zoom : sous une transformation CSS, Firefox agrandit aussi les traits (vector-effect
  // non-scaling-stroke ne la compense pas).
  const width = k === V.sel ? 3 : d && d.w * d.h > 0.98 ? 1 : 2;
  return { stroke: color, strokeWidth: width / pz.getScale(), fill: color, fillOpacity: 0 };
}
function drawBoxes() {
  // Image pas encore affichée (taille 0 à l'ouverture) : Annotorious calculerait NaN ; redessiné à son chargement.
  const a = cur(); if (!a || V.opening || !natural()[0] || !$('#photo').clientWidth) return;
  anno.setStyle(a2 => boxStyle(a2));   // nouvelle fonction : redessin avec la sélection à jour
  anno.setAnnotations(a.detections.map(toAnno), true);
  const d = curDet();
  if (d) anno.setSelected(String(d.id)); else anno.cancelSelected();
  drawLabels();
}
function drawLabels() {
  const a = cur(); if (!a) return;
  $('#labels').innerHTML = a.detections.map((d, k) => {
    const inside = d.w * d.h > 0.98 || d.y < 0.07;
    const pos = inside ? `left:${d.x * 100}%;top:${d.y * 100}%` : `left:${d.x * 100}%;bottom:${(1 - d.y) * 100}%`;
    return `<span class="vlbl ${d.status}${k === V.sel ? ' sel' : ''}${inside ? ' in' : ''}" style="${pos}">${subjectIcon(d)} ${esc(shortName(d.taxon))}${d.status === 'confirmed' ? ' ✓' : ''}</span>`;
  }).join('');
}
// Après la mise en page : Annotorious relit la taille de l'image à l'image suivante (sinon échelle NaN un instant).
$('#photo').addEventListener('load', () => { if (V.open) requestAnimationFrame(() => requestAnimationFrame(() => { if (V.open) drawBoxes(); })); });

// Clic sur un rectangle : il devient le sujet affiché dans le panneau.
anno.on('selectionChanged', sel => {
  const k = sel[0] ? cur()?.detections.findIndex(d => String(d.id) === sel[0].id) : -1;
  if (k >= 0 && k !== V.sel) { V.sel = k; anno.setStyle(a => boxStyle(a)); drawLabels(); drawInfo(); }
});

// Nouvelle boîte tracée : identifiée par le serveur, puis sélectionnée.
anno.on('createAnnotation', async created => {
  const box = fromGeometry(created.target.selector.geometry);
  stopDraw();
  if (box.w < 0.02 || box.h < 0.02) { anno.removeAnnotation(created.id); return; }
  if (!V.info) toggleInfo();
  $('#vi-body').innerHTML = `<div class="empty">${t('det.identifying')}</div>`;
  const before = new Set(cur().detections.map(x => x.id));
  try {
    const a = await api(`/api/assets/${cur().asset_id}/boxes`, { method: 'POST', body: JSON.stringify(box) });
    V.list[V.i] = { ...cur(), ...a }; V.touched.add(V.i);
    const made = a.detections.find(x => !before.has(x.id));
    if (made) pushUndo({ asset: cur().asset_id, det: made.id, created: true });   // U supprime la boîte créée
    V.sel = Math.max(0, a.detections.findIndex(x => x === made));
  } catch (e) { fail(e); }
  drawViewer();
});

/* Rectangle déplacé ou redimensionné : à la fin du geste, nouvelle identification du recadrage. */
let editing = null, press = null;
$('#frame').addEventListener('pointerdown', e => {
  if (e.button !== 0) return;
  if (e.pointerType === 'mouse') press = { x: e.clientX, y: e.clientY, sel: V.sel, target: e.target };
  else if (doubleTap(e)) { e.stopPropagation(); e.preventDefault(); return; }
  // Hors du rectangle sélectionné, Annotorious consacrerait ce glisser à le désélectionner : on le fait avant,
  // pour que le glisser trace une boîte.
  if (anno.isDrawingEnabled() && !e.target.closest(HANDLES)) anno.cancelSelected();
  if (!e.target.closest(HANDLES) || drawing) return;
  const d = curDet(); if (!d) return;
  editing = { id: d.id, before: { x: d.x, y: d.y, w: d.w, h: d.h } };
  $('#frame').classList.add('pz-skip');   // Panzoom ne déplace pas la photo pendant ce geste
}, true);
window.addEventListener('pointerup', ev => {
  const e = editing, p = press; editing = press = null;
  if (e) setTimeout(() => commitEdit(e));   // après Annotorious, qui finit de mettre le rectangle à jour
  syncDraw();
  if (p && !drawing && Math.hypot(ev.clientX - p.x, ev.clientY - p.y) < 5) clickPhoto(ev, p);
});
/* Clic à la souris : autre rectangle -> il devient le sujet ; sinon zoom ×3 à cet endroit, ou retour à la photo
   entière. Le second clic d'un double clic est ignoré : il annulerait aussitôt le zoom. */
let lastClick = 0;
function clickPhoto(e, p) {
  if (!V.open || p.target.closest('.a9s-corner-handle, .a9s-edge-handle')) return;
  const now = Date.now(), repeat = now - lastClick < 350;
  lastClick = now;
  if (repeat) return;
  const r = $('#photo').getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
  const hits = cur().detections.map((d, k) => ({ k, area: d.w * d.h, in: x >= d.x && x <= d.x + d.w && y >= d.y && y <= d.y + d.h }))
    .filter(h => h.in).sort((a, b) => a.area - b.area);
  if (hits[0] && hits[0].k !== p.sel) { V.sel = hits[0].k; drawBoxes(); drawInfo(); return; }
  if (zoomed()) resetZoom(); else zoomAt(e.clientX, e.clientY, 3);
  // Annotorious désélectionne sur un clic hors du rectangle : le sujet affiché reste sélectionné.
  setTimeout(() => { const d = curDet(); if (d) anno.setSelected(String(d.id)); });
}
async function commitEdit({ id, before }) {
  const a = anno.getAnnotationById(String(id)), d = cur()?.detections.find(x => x.id === id);
  if (!a || !d) return;
  const b = fromGeometry(a.target.selector.geometry);
  if (Math.abs(b.x - before.x) + Math.abs(b.y - before.y) + Math.abs(b.w - before.w) + Math.abs(b.h - before.h) < 0.01) return;   // simple clic
  pushUndo({ asset: cur().asset_id, det: d.id, prev: snapshot(d) });
  $('#vi-body').innerHTML = `<div class="empty">${t('det.reidentify')}</div>`;
  try {
    const r = await api(`/api/detections/${d.id}/box`, { method: 'PUT', body: JSON.stringify(b) });
    V.list[V.i] = { ...cur(), ...r }; V.touched.add(V.i);
  } catch (err) { fail(err); }
  V.sel = Math.max(0, cur().detections.findIndex(x => x.id === id)); drawViewer(); loadStatus();
}

/* -- zoom ------------------------------------------------------------------ */
/* Dès le premier zoom, l'original (pleine définition) remplace l'aperçu, si la clé y donne accès. */
const Z = { hi: null };
$('#pz').addEventListener('panzoomchange', e => {
  const z = e.detail.scale;
  $('#pz').style.setProperty('--z', z);
  $('#frame').classList.toggle('zoomed', z > 1.001);
  anno.setStyle(a => boxStyle(a));
  syncDraw();
  if (z > 1.2) loadFull();
});
const zoomAt = (cx, cy, z) => pz.zoomToPoint(Math.min(8, Math.max(1, z)), { clientX: cx, clientY: cy }, { animate: false });
const resetZoom = () => pz.reset({ animate: false });
const zoomCenter = k => { const r = $('#frame').getBoundingClientRect(); zoomAt(r.left + r.width / 2, r.top + r.height / 2, pz.getScale() * k); };
/* Z : zoom sur le sujet sélectionné (centré, il remplit la vue), ou retour à la photo entière. */
function zoomToSubject() {
  const d = curDet();
  if (zoomed() || !d) { zoomed() ? resetZoom() : zoomCenter(3); return; }
  const f = $('#pz'), w = f.offsetWidth, h = f.offsetHeight;
  pz.zoom(Math.min(8, Math.max(1.5, 0.85 / Math.max(d.w, d.h))), { animate: false });
  // Transformation de Panzoom : scale(s) translate(x, y), origine au centre ; déplacement en pixels non zoomés.
  setTimeout(() => pz.pan(w / 2 - (d.x + d.w / 2) * w, h / 2 - (d.y + d.h / 2) * h, { animate: false }));
}
function loadFull() {
  const a = cur(); if (!a || Z.hi === a.asset_id || a.kind === 'VIDEO') return;
  Z.hi = a.asset_id;
  const im = new Image();
  im.onload = () => {
    const img = $('#photo');
    // Proportions différentes de l'aperçu (photo recadrée dans Immich) : les rectangles ne tomberaient plus juste.
    if (img.dataset.id !== a.asset_id || Math.abs(im.naturalWidth / im.naturalHeight / V.ar - 1) > 0.02) return;
    img.dataset.full = a.asset_id; img.src = im.src;
  };
  im.src = `/api/full/${a.asset_id}`;   // en cas d'échec (pas de permission, RAW illisible) : on garde l'aperçu
}
$('.stage').addEventListener('wheel', e => { if (V.open) { e.preventDefault(); zoomAt(e.clientX, e.clientY, pz.getScale() * wheelFactor(e)); } }, { passive: false });
/* Double appui au doigt : zoom ×3 à cet endroit, ou retour. Pas d'événement dblclick : il n'arrive pas
   quand l'appui est intercepté (tracé, déplacement). Renvoie true si l'appui est le second d'un double. */
let lastTap = null;
function doubleTap(e) {
  const now = Date.now(), prev = lastTap;
  lastTap = { t: now, x: e.clientX, y: e.clientY };
  if (!prev || now - prev.t > 350 || Math.hypot(e.clientX - prev.x, e.clientY - prev.y) > 20) return false;
  lastTap = null;
  if (zoomed()) resetZoom(); else zoomAt(e.clientX, e.clientY, 3);
  return true;
}
