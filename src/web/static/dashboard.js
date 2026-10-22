/*
 * Jinora dashboard. Vanilla JS, no build step, no external libraries.
 * It only displays what the API returns (stored scores); it never computes a score.
 * SECURITY: ad text is third-party content. Every value is inserted with textContent / text nodes through el();
 * this file never assigns innerHTML or builds markup from data, and the page is served with a strict CSP.
 */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);

  /** Creates an element. props: class, text, dataset, on<event> handlers, anything else becomes an attribute. */
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') node[key] = value;
      else if (key === 'style') continue; // inline styles are blocked by the CSP; use classes
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); return node; };

  const FEATURE_LABELS = {
    automatizacion: 'Automatización', agendamiento: 'Agendamiento', gestion_leads_crm: 'Gestión de leads / CRM',
    mensajes_masivos_campanas: 'Mensajes masivos y campañas', atencion_omnicanal: 'Atención omnicanal',
    agentes_ia: 'Agentes conversacionales', pagos_ventas_en_chat: 'Pagos y ventas en chat',
    integraciones_api: 'Integraciones / API', otro: 'Otro',
  };
  const FORMAT_LABELS = {
    ugc: 'UGC', demo_pantalla: 'Demo en pantalla', talking_head: 'Persona hablando', animacion: 'Animación / motion graphics',
    testimonial: 'Testimonial', mixto: 'Mixto', otro: 'Otro', captura_producto: 'Captura de producto', chat_whatsapp: 'Chat de mensajería',
    persona_rostro: 'Persona / rostro', meme: 'Meme', infografia: 'Infografía', mockup_dispositivo: 'Mockup de dispositivo',
    ilustracion: 'Ilustración', foto_stock: 'Foto de stock',
  };
  const MEDIA_LABELS = { video: 'Video', imagen: 'Imagen', carrusel: 'Carrusel', dco: 'DCO' };
  const AI_MENTION_LABELS = { ninguna: 'ninguna', mencionada: 'mencionada', central: 'central' };
  const STATUS_LABELS = {
    pending_evaluation: 'en cola', evaluated: 'evaluado', scored: 'con puntaje final', discovered: 'descubierto', filtered: 'filtrado',
    downloaded: 'descargado', needs_manual_review: 'revisión manual', inactive_data_only: 'inactivo (solo datos)',
  };
  const label = (map, key) => (key && map[key]) || key || '—';
  const fmt = (n, digits = 1) => (n === null || n === undefined || Number.isNaN(Number(n)) ? '—' : Number(n).toFixed(digits));
  const mmss = (sec) => `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;
  function safeHttp(url) {
    try { const u = new URL(url); return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : null; } catch { return null; }
  }

  const state = { view: 'ranking', selected: null, queueTimer: null, pollTimers: new Set() };

  // ---------------------------------------------------------------- API helpers
  async function api(path, options) {
    const response = await fetch(path, options);
    let body = null;
    try { body = await response.json(); } catch { /* not JSON */ }
    if (!response.ok) {
      const detail = body && body.detail;
      throw new Error(typeof detail === 'string' ? detail : `Error ${response.status}`);
    }
    return body;
  }
  function notify(message, isError = false) {
    const box = $('#notice');
    box.textContent = message;
    box.className = isError ? 'notice error' : 'notice';
    box.hidden = !message;
    if (message && !isError) setTimeout(() => { if (box.textContent === message) box.hidden = true; }, 5000);
  }

  // ---------------------------------------------------------------- tabs
  function showView(name) {
    state.view = name;
    for (const tab of document.querySelectorAll('.tab')) tab.setAttribute('aria-selected', String(tab.dataset.view === name));
    for (const view of document.querySelectorAll('.view')) view.hidden = view.id !== `view-${name}`;
    if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
    clearInterval(state.queueTimer);
    if (name === 'competitors') loadCompetitors();
    if (name === 'queue') { loadQueue(); state.queueTimer = setInterval(loadQueue, 20000); }
  }

  // ---------------------------------------------------------------- filters
  const FILTERS = [
    ['pillar', 'Pilar'], ['country', 'País'], ['competitor', 'Competidor'], ['format', 'Formato'], ['language', 'Idioma'], ['has_voiceover', 'Voz'],
  ];
  function filterQuery() {
    const params = new URLSearchParams();
    for (const [key] of FILTERS) {
      const select = $(`#f-${key}`);
      if (select && select.value) params.set(key, select.value);
    }
    return params;
  }
  async function buildFilters() {
    const facets = await api('/api/filters');
    const options = {
      pillar: facets.pillars.map((v) => [v, v]),
      country: facets.countries.map((v) => [v, v]),
      competitor: facets.competitors.map((v) => [v, v]),
      format: facets.formats.map((v) => [v, label(FORMAT_LABELS, v)]),
      language: facets.languages.map((v) => [v, v]),
      has_voiceover: [['true', 'Con voz'], ['false', 'Sin voz']],
    };
    const form = clear($('#filters'));
    for (const [key, text] of FILTERS) {
      const select = el('select', { id: `f-${key}`, 'aria-label': text, onchange: loadRanking },
        el('option', { value: '', text: 'Todos' }), options[key].map(([value, name]) => el('option', { value, text: name })));
      form.append(el('label', {}, text, select));
    }
    form.append(el('button', { type: 'button', class: 'btn', text: 'Limpiar', onclick: () => {
      for (const [key] of FILTERS) $(`#f-${key}`).value = '';
      loadRanking();
    } }));
    form.onsubmit = (event) => event.preventDefault();
  }

  // ---------------------------------------------------------------- ranking
  function scoreLine(row) {
    return el('div', { class: 'scores' },
      el('span', { class: 'score-big', text: fmt(row.jinora_score), title: 'jinora_score (guardado)' }),
      el('span', { class: 'score-small', text: `creativo ${fmt(row.creative_score, 0)} · mercado ${fmt(row.market_score)}` }));
  }
  function renderRow(row, unevaluated) {
    const thumb = row.thumbnail_url
      ? el('img', { class: 'thumb', src: row.thumbnail_url, alt: '', loading: 'lazy' })
      : el('div', { class: 'thumb empty', text: 'sin imagen' });
    const chips = el('div', { class: 'meta' },
      row.format ? el('span', { class: 'chip', text: label(FORMAT_LABELS, row.format) }) : el('span', { class: 'chip', text: label(MEDIA_LABELS, row.media_format) }),
      row.variants > 1 ? el('span', { class: 'chip accent', text: `x ${row.variants} variantes` }) : null,
      el('span', { class: 'chip', text: `${row.days_active} d${row.is_active ? ' activo' : ''}` }),
      unevaluated ? el('span', { class: 'chip', text: label(STATUS_LABELS, row.status) }) : null,
      unevaluated && row.can_enqueue ? el('span', { class: 'chip good', text: 'se puede encolar' }) : null,
      row.rubric_outdated ? el('span', { class: 'chip warn', text: 'rúbrica anterior', title: 'Evaluado con una versión de la rúbrica distinta a la vigente' }) : null);
    const button = el('button', { class: 'row', type: 'button', dataset: { adId: row.ad_id }, onclick: () => selectAd(row.ad_id) },
      thumb,
      el('div', { class: 'who' }, el('strong', { text: row.competitor || row.page_name || row.ad_id }),
        el('span', { class: 'muted small', text: row.headline || row.page_name || '' })),
      chips,
      unevaluated ? el('div', { class: 'scores' }, el('span', { class: 'chip', text: 'Sin evaluar' })) : scoreLine(row));
    return button;
  }
  async function loadRanking() {
    const params = filterQuery();
    params.set('limit', '100');
    const hasFilters = [...params.keys()].some((k) => k !== 'limit');
    let data;
    try { data = await api(`/api/ads/top?${params}`); } catch (error) { notify(error.message, true); return; }
    const list = clear($('#ranking-list'));
    $('#ranking-head').textContent = data.total
      ? `${data.total} creativo${data.total === 1 ? '' : 's'} evaluado${data.total === 1 ? '' : 's'} (un renglón por creativo; los puntajes son los guardados)`
      : '';
    if (!data.total) {
      list.append(hasFilters
        ? el('div', { class: 'empty-state', text: 'Ningún anuncio evaluado coincide con los filtros.' })
        : el('div', { class: 'empty-state' }, 'Aún no hay anuncios evaluados; mira la pestaña Cola.', el('br'),
            el('button', { class: 'btn', type: 'button', text: 'Ir a Cola', onclick: () => showView('queue') })));
    }
    for (const row of data.items) list.append(renderRow(row, false));
    const title = $('#unevaluated-title');
    const pending = clear($('#unevaluated-list'));
    title.hidden = !data.unevaluated.total;
    title.textContent = `Sin evaluar (${data.unevaluated.total})`;
    for (const row of data.unevaluated.items) pending.append(renderRow(row, true));
    if (state.selected) markSelected(state.selected);
  }
  function markSelected(adId) {
    for (const row of document.querySelectorAll('.row')) row.removeAttribute('aria-current');
    for (const row of document.querySelectorAll(`.row[data-ad-id="${CSS.escape(adId)}"]`)) row.setAttribute('aria-current', 'true');
  }

  // ---------------------------------------------------------------- detail
  async function selectAd(adId) {
    state.selected = adId;
    markSelected(adId);
    const panel = clear($('#detail'));
    panel.append(el('p', { class: 'muted', text: 'Cargando…' }));
    try { renderDetail(await api(`/api/ads/${encodeURIComponent(adId)}`)); } catch (error) { clear(panel).append(el('p', { text: error.message })); }
    if (window.matchMedia('(max-width: 900px)').matches) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function dimensionBlock(dim, player) {
    const pct = dim.score === null || dim.score === undefined ? 0 : Math.max(0, Math.min(100, (dim.score / dim.max) * 100));
    const bar = el('div', { class: 'bar', role: 'meter', 'aria-label': dim.label, 'aria-valuemin': 0, 'aria-valuemax': dim.max, 'aria-valuenow': dim.score ?? 0 },
      el('span'));
    bar.firstChild.style.width = `${pct}%`; // CSSOM assignment is allowed by the CSP (inline style attributes are not)
    const evidence = el('ul', { class: 'evidence' });
    for (const ev of dim.evidence) {
      let item;
      if (ev.seconds !== null && ev.seconds !== undefined && player) {
        item = el('button', { type: 'button', class: 'ev-btn', title: 'Saltar a este momento del video', onclick: () => {
          player.currentTime = ev.seconds;
          player.play().catch(() => {});
          player.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        } }, el('span', { class: 'ev-time', text: ev.timestamp || mmss(ev.seconds) }), el('span', { class: 'muted small', text: ev.source ? `${ev.source}: ` : '' }), ev.quote || '');
      } else {
        item = el('div', { class: 'ev-plain' }, ev.timestamp ? el('span', { class: 'ev-time', text: ev.timestamp }) : null,
          el('span', { class: 'muted small', text: ev.source ? `${ev.source}: ` : '' }), ev.quote || '');
      }
      evidence.append(el('li', {}, item));
    }
    return el('div', { class: 'dim' },
      el('div', { class: 'dim-head' }, el('span', { text: dim.label }), el('span', { text: `${dim.score ?? '—'}/${dim.max}` })),
      bar,
      dim.anchor_level ? el('span', { class: 'chip', text: `nivel ${dim.anchor_level}` }) : null,
      el('p', { text: dim.reasoning || '' }), evidence);
  }
  function renderDetail(d) {
    const panel = clear($('#detail'));
    const ad = d.ad, ev = d.evaluation;
    panel.append(el('h2', { text: ad.competitor || ad.page_name || ad.id }));
    panel.append(el('div', { class: 'tags' },
      el('span', { class: 'chip', text: ad.pillar }),
      el('span', { class: 'chip', text: label(STATUS_LABELS, ad.status) }),
      el('span', { class: 'chip', text: `${ad.days_active} días${ad.is_active ? ' · activo' : ' · inactivo'}` }),
      ...ad.countries.map((c) => el('span', { class: 'chip', text: c })),
      ev && ev.rubric_outdated ? el('span', { class: 'chip warn', text: 'rúbrica anterior', title: `Evaluado con la rúbrica ${ev.rubric_version}; vigente: ${ev.rubric_current}` }) : null));

    // media player
    let player = null;
    if (d.media.video_url) {
      player = el('video', { class: 'media', controls: true, preload: 'metadata', playsinline: true, src: d.media.video_url, poster: d.media.image_url || null });
      panel.append(player);
    } else if (d.media.image_url) {
      panel.append(el('img', { class: 'media', src: d.media.image_url, alt: 'Creativo del anuncio' }));
    } else {
      panel.append(el('p', { class: 'muted', text: 'Este anuncio no tiene archivo local.' }));
    }

    // stored scores
    panel.append(el('div', { class: 'score-row' },
      ...[['jinora_score', d.market.jinora_score, 1], ['Creativo', ev ? ev.creative_score : null, 0], ['Mercado', d.market.market_score, 1]].map(([name, value, digits]) =>
        el('div', { class: 'score-box' }, el('span', { class: 'label', text: name }), el('span', { class: 'value', text: fmt(value, digits) })))));

    if (!ev) {
      panel.append(el('div', { class: 'empty-state' }, 'Este anuncio todavía no tiene evaluación.', el('br'), enqueueControl(ad.id, d)));
    } else {
      panel.append(el('h3', { text: 'Propuesta de valor' }), el('p', { text: ev.value_proposition || '—' }));
      panel.append(el('div', { class: 'tags', 'aria-label': 'Etiquetas' },
        ...ev.feature_focus.map((t) => el('span', { class: 'chip accent', text: label(FEATURE_LABELS, t) }))));
      panel.append(el('dl', { class: 'kv' },
        el('dt', { text: 'Formato' }), el('dd', { text: label(FORMAT_LABELS, ev.format) }),
        el('dt', { text: 'Idioma' }), el('dd', { text: ev.language || '—' }),
        el('dt', { text: 'Voz' }), el('dd', { text: ev.has_voiceover === null ? '—' : (ev.has_voiceover ? 'con voz' : 'sin voz') }),
        el('dt', { text: 'Mención de IA (dato descriptivo)' }), el('dd', { text: label(AI_MENTION_LABELS, ev.ai_mention) }),
        el('dt', { text: 'Nicho' }), el('dd', { text: ev.target_niche || '—' }),
        el('dt', { text: 'Evaluación' }), el('dd', { text: `${ev.model} · prompt ${ev.prompt_version} · rúbrica ${ev.rubric_version}` })));
      if (ev.hook_description) panel.append(el('h3', { text: 'Los primeros segundos' }), el('p', { text: ev.hook_description }));

      panel.append(el('h3', { text: 'Desglose (sobre 25)' }));
      for (const dim of ev.dimensions) panel.append(dimensionBlock(dim, player));

      panel.append(el('div', { class: 'callouts' },
        callout('Qué copiar', ev.what_to_model), callout('Qué superar', ev.what_to_beat), callout('Idea para Cosmiq', ev.cosmiq_adaptation_idea)));

      if (ev.transcript && ev.transcript.length) {
        const list = el('ul', { class: 'evidence' });
        for (const seg of ev.transcript) {
          const text = seg.text || '';
          list.append(el('li', {}, player && typeof seg.start_s === 'number'
            ? el('button', { type: 'button', class: 'ev-btn', onclick: () => { player.currentTime = seg.start_s; player.play().catch(() => {}); } },
                el('span', { class: 'ev-time', text: mmss(seg.start_s) }), text)
            : el('div', { class: 'ev-plain', text })));
        }
        panel.append(el('details', {}, el('summary', { text: 'Transcripción' }), list));
      }
    }

    // market signal
    const used = d.market.market_components && d.market.market_components.used;
    if (used && Object.keys(used).length) {
      const rows = Object.entries(used).map(([name, c]) => el('tr', {}, el('td', { text: name }), el('td', { text: String(JSON.stringify(c.raw)) }),
        el('td', { text: fmt(c.value, 2) }), el('td', { text: fmt(c.effective_weight, 2) })));
      panel.append(el('details', {}, el('summary', { text: 'Señal de mercado (componentes guardados)' }),
        el('table', {}, el('thead', {}, el('tr', {}, ...['componente', 'dato', 'valor 0-1', 'peso'].map((h) => el('th', { text: h })))), el('tbody', {}, rows)),
        d.market.market_components.missing && d.market.market_components.missing.length
          ? el('p', { class: 'muted small', text: `Sin dato (excluidos): ${d.market.market_components.missing.join(', ')}` }) : null));
    }
    if (d.variants.length > 1) {
      panel.append(el('details', {}, el('summary', { text: `Variantes del creativo (${d.variants.length})` }),
        el('ul', { class: 'evidence' }, d.variants.map((v) => el('li', {}, el('button', { type: 'button', class: 'ev-btn', onclick: () => selectAd(v.ad_id) },
          `${v.page_name || v.ad_id} · ${v.days_active} d${v.is_current ? ' (actual)' : ''}`))))));
    }
    // ad copy (third-party text, shown as plain text)
    const link = safeHttp(ad.link_url);
    panel.append(el('details', {}, el('summary', { text: 'Texto del anuncio' }),
      ad.headline ? el('p', {}, el('strong', { text: ad.headline })) : null,
      ad.body_text ? el('div', { class: 'copy-text', text: ad.body_text }) : null,
      ad.cta_text ? el('p', { class: 'muted small', text: `CTA: ${ad.cta_text}` }) : null,
      link ? el('p', {}, el('a', { href: link, target: '_blank', rel: 'noopener noreferrer nofollow', text: 'Abrir destino del anuncio' })) : null));
  }
  function callout(title, text) {
    return el('div', { class: 'callout' }, el('h3', { text: title }), el('p', { text: text || '—' }));
  }

  // ---------------------------------------------------------------- enqueue analysis + job polling
  function enqueueControl(adId, detail) {
    const statusLine = el('p', { class: 'job-status muted small', role: 'status' });
    const button = el('button', { type: 'button', class: 'btn-primary', text: 'Encolar análisis' });
    if (detail && detail.can_enqueue === false) {
      button.disabled = true;
      statusLine.textContent = detail.enqueue_reason ? `No se puede encolar: ${detail.enqueue_reason}.` : '';
    }
    button.onclick = async () => {
      button.disabled = true;
      statusLine.textContent = 'Encolando…';
      try {
        const queued = await api(`/api/evaluate/${encodeURIComponent(adId)}`, { method: 'POST' });
        pollJob(queued.job_id, statusLine, () => { loadRanking(); selectAd(adId); });
      } catch (error) {
        statusLine.textContent = error.message;
        button.disabled = false;
      }
    };
    return el('div', {}, button, statusLine);
  }
  const JOB_LABELS = { pending: 'en cola, esperando al worker', running: 'en curso', done: 'listo', failed: 'falló' };
  function pollJob(jobId, statusLine, onDone, attempt = 0) {
    api(`/api/jobs/${jobId}`).then((job) => {
      const detail = job.status === 'failed' && job.error ? `: ${job.error}` : '';
      statusLine.textContent = `Job #${job.id}: ${JOB_LABELS[job.status] || job.status}${detail}`;
      if (job.status === 'done') { onDone(); return; }
      if (job.status === 'failed' || attempt > 200) return;
      const timer = setTimeout(() => { state.pollTimers.delete(timer); pollJob(jobId, statusLine, onDone, attempt + 1); }, 3000);
      state.pollTimers.add(timer);
    }).catch((error) => { statusLine.textContent = error.message; });
  }

  // ---------------------------------------------------------------- competitors
  function histogram(timeline) {
    const max = Math.max(1, ...timeline.map((m) => m.ads));
    const cols = timeline.map((m) => {
      const bar = el('span', { title: `${m.month}: ${m.ads} anuncios, ${m.evaluated} evaluados${m.avg_creative_score !== null ? `, creativo promedio ${m.avg_creative_score}` : ''}` });
      bar.style.height = `${Math.max(3, (m.ads / max) * 100)}%`;
      return el('div', { class: 'col' }, bar);
    });
    return el('div', {}, el('div', { class: 'histogram', role: 'img', 'aria-label': 'Anuncios por mes de inicio' }, cols),
      el('div', { class: 'hist-labels' }, el('span', { text: timeline.length ? timeline[0].month : '' }), el('span', { text: timeline.length > 1 ? timeline[timeline.length - 1].month : '' })));
  }
  async function loadCompetitors() {
    const grid = clear($('#competitors-grid'));
    let data;
    try { data = await api('/api/competitors'); } catch (error) { notify(error.message, true); return; }
    if (!data.length) { grid.append(el('div', { class: 'empty-state', text: 'Todavía no hay anuncios asociados a competidores del catálogo.' })); return; }
    for (const c of data) {
      const formats = { ...Object.fromEntries(Object.entries(c.media_formats).map(([k, v]) => [label(MEDIA_LABELS, k), v])),
        ...Object.fromEntries(Object.entries(c.creative_formats).map(([k, v]) => [label(FORMAT_LABELS, k), v])) };
      const aiText = Object.entries(c.ai_mention).map(([k, v]) => `${label(AI_MENTION_LABELS, k)} ${v}`).join(' · ');
      grid.append(el('article', { class: 'card' },
        el('h2', { text: c.name }),
        el('div', { class: 'tags' }, el('span', { class: 'chip', text: c.pillar || '—' }), el('span', { class: 'chip', text: `prioridad ${c.priority}` })),
        el('div', { class: 'stat-grid' }, ...[['Activos', c.active_ads], ['Evaluados', c.evaluated], ['Pendientes', c.pending], ['Creativo prom.', fmt(c.avg_creative_score)], ['jinora prom.', fmt(c.avg_jinora_score)]]
          .map(([l, n]) => el('div', { class: 'stat' }, el('div', { class: 'n', text: n }), el('div', { class: 'l', text: l })))),
        el('h3', { text: 'Anuncios por mes de inicio' }), histogram(c.timeline),
        el('h3', { text: 'Formatos' }), el('div', { class: 'tags' }, Object.entries(formats).map(([k, v]) => el('span', { class: 'chip', text: `${k} · ${v}` }))),
        el('h3', { text: 'Ángulos recurrentes' }),
        c.recurring_angles.length ? el('div', { class: 'tags' }, c.recurring_angles.map((a) => el('span', { class: 'chip accent', text: `${label(FEATURE_LABELS, a.tag)} · ${a.count}` })))
          : el('p', { class: 'muted small', text: 'Sin evaluaciones todavía.' }),
        aiText ? el('p', { class: 'muted small', text: `Mención de IA (dato descriptivo): ${aiText}` }) : null,
        el('button', { type: 'button', class: 'btn', text: 'Ver en el ranking', onclick: async () => {
          showView('ranking');
          $('#f-competitor').value = c.name;
          loadRanking();
        } })));
    }
  }

  // ---------------------------------------------------------------- queue
  function statCard(title, big, ...lines) {
    return el('article', { class: 'card' }, el('h3', { text: title }), el('div', { class: 'big', text: big }),
      ...lines.map((line) => el('p', { class: 'muted small', text: line })));
  }
  async function loadQueue() {
    let s;
    try { s = await api('/api/queue/status'); } catch (error) { notify(error.message, true); return; }
    const t = s.totals, q = s.quota;
    const stats = clear($('#queue-stats'));
    stats.append(
      statCard('En cola', String(t.queue), `${t.queue_video} videos (Gemini) · ${t.queue_static} estáticos (Claude)`, `${t.pending_without_media} pendientes sin archivo local`),
      statCard('Evaluados', String(t.evaluated), `${t.unevaluated} sin evaluar de ${t.total_ads}`, `${t.creative_groups} grupos de creativos`),
      statCard('Solicitudes de hoy', String(q.requests_today), `éxitos ${q.success_today} · fallos ${q.failed_today}`, `límite ${q.rpd}, reserva ${q.reserve}, útiles ${q.usable}`,
        q.exhausted ? 'Cuota diaria agotada: se reanuda a medianoche (hora del Pacífico)' : 'Cuota activa'),
      statCard('Días estimados para vaciar', String(s.estimate.days_to_clear), `${s.estimate.queue_video} videos / (RPD − reserva) = ${s.estimate.queue_video} / ${s.estimate.usable_rpd}`),
      statCard('Clave de Gemini', q.key_fingerprint, 'Solo los primeros 8 caracteres del hash; nunca la clave'),
      statCard('Claude (estáticos)', `$${fmt(s.claude.spent_usd, 4)}`, `tope $${fmt(s.claude.budget_usd, 2)}`),
      statCard('Excluidos de la cola', String(t.rejected + t.out_of_catalog), `rejected ${t.rejected} · out_of_catalog ${t.out_of_catalog}`),
      statCard('Ventana preferida', s.window.inside ? 'dentro' : 'fuera', `${s.window.preferred_hours} (hora de Lima)`));
    $('#queue-updated').textContent = `Actualizado ${new Date().toLocaleTimeString()}`;
    const list = clear($('#jobs-list'));
    if (!s.recent_jobs.length) { list.append(el('p', { class: 'muted', text: 'Todavía no hay trabajos en la cola.' })); return; }
    list.append(el('table', {}, el('thead', {}, el('tr', {}, ...['#', 'Tipo', 'Estado', 'Anuncio', 'Creado', 'Error'].map((h) => el('th', { text: h })))),
      el('tbody', {}, s.recent_jobs.map((j) => el('tr', {}, el('td', { text: j.id }), el('td', { text: j.type }), el('td', { text: label(JOB_LABELS, j.status) }),
        el('td', { text: j.ad_id || '—' }), el('td', { text: (j.created_at || '').replace('T', ' ').slice(0, 16) }), el('td', { text: j.error || '' }))))));
  }

  // ---------------------------------------------------------------- init
  async function init() {
    for (const tab of document.querySelectorAll('.tab')) tab.addEventListener('click', () => showView(tab.dataset.view));
    $('#queue-refresh').addEventListener('click', loadQueue);
    try { await buildFilters(); } catch (error) { notify(error.message, true); }
    await loadRanking();
    const wanted = location.hash.replace('#', '');
    if (['ranking', 'competitors', 'queue'].includes(wanted)) showView(wanted);
  }
  init();
})();
