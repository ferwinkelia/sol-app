/* Sol — adaptador para correr la app de Claude en el servidor propio.
   Imita window.claude.use('db' | 'user' | 'downloads') usando la API del servidor:
   los datos se guardan en la mini PC y se sincronizan entre celulares. */
(function () {
  'use strict';
  const KEY = 'sol_pin';
  let pin = '';
  try { pin = localStorage.getItem(KEY) || ''; } catch (e) {}
  const cache = {};            // colección -> Map(id -> data)
  const listeners = new Set();
  let seq = 0, ready = null, version = null;

  function rid() {
    const a = 'abcdefghijklmnopqrstuvwxyz0123456789';
    let s = ''; const r = crypto.getRandomValues(new Uint8Array(20));
    r.forEach(n => { s += a[n % a.length]; });
    return s;
  }

  async function api(method, path, body) {
    const r = await fetch('api/' + path, {
      method, cache: 'no-store',
      headers: { 'X-PIN': pin, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    let d = {};
    try { d = await r.json(); } catch (e) {}
    if (r.status === 401) {
      try { localStorage.removeItem(KEY); } catch (e) {}
      pin = '';
      throw { code: 'auth', message: d.error || 'PIN incorrecto' };
    }
    if (r.status === 429) throw { code: 'auth', message: d.error || 'Demasiados intentos' };
    if (!r.ok) throw { code: r.status === 400 || r.status === 404 ? 'invalid_argument' : 'unavailable', message: d.error || ('Error ' + r.status) };
    return d;
  }

  function apply(changes) {
    changes.forEach(c => {
      const m = cache[c.col] || (cache[c.col] = new Map());
      if (c.data == null) m.delete(c.id); else m.set(c.id, c.data);
    });
    listeners.forEach(f => { try { f(); } catch (e) { console.error(e); } });
  }

  async function loop() {
    for (;;) {
      try {
        const d = await api('GET', 'sync?since=' + seq + '&wait=1');
        if (d.changes.length) apply(d.changes);
        seq = d.seq;
        if (version && d.version && d.version !== version) { location.reload(); return; }
      } catch (e) {
        if (e && e.code === 'auth') { location.reload(); return; }
        await new Promise(r => setTimeout(r, 4000));
      }
    }
  }
  // al volver a la app, forzar una sincronización rápida
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && seq) api('GET', 'sync?since=' + seq).then(d => { if (d.changes.length) apply(d.changes); seq = Math.max(seq, d.seq); }).catch(() => {});
  });

  function pedirPin(msg) {
    return new Promise(res => {
      const o = document.createElement('div');
      o.setAttribute('role', 'dialog');
      o.style.cssText = 'position:fixed;inset:0;z-index:50;background:#0E151B;color:#E4EDF3;display:flex;align-items:center;justify-content:center;padding:24px;font-family:system-ui,sans-serif';
      o.innerHTML = '<form style="width:100%;max-width:320px;display:flex;flex-direction:column;gap:14px">' +
        '<h1 style="margin:0;font-size:26px">Registro de Sol</h1>' +
        '<label style="display:flex;flex-direction:column;gap:6px;font-size:14px;color:#90A4B4">PIN de la familia' +
        '<input id="solPin" type="password" inputmode="numeric" autocomplete="current-password" required style="font-size:22px;padding:12px 14px;border-radius:14px;border:1px solid #344756;background:#17222B;color:#E4EDF3;text-align:center;letter-spacing:4px"></label>' +
        '<p id="solErr" style="margin:0;color:#F08378;font-size:14px;min-height:18px">' + (msg || '') + '</p>' +
        '<button style="border:none;border-radius:999px;background:#4FB38A;color:#0C1E16;font-size:18px;font-weight:700;min-height:54px">Entrar</button>' +
        '<p style="margin:0;font-size:13px;color:#90A4B4">Se pide una sola vez en cada celular.</p></form>';
      document.body.appendChild(o);
      setTimeout(() => { const i = document.getElementById('solPin'); if (i) i.focus(); }, 50);
      o.querySelector('form').onsubmit = async ev => {
        ev.preventDefault();
        pin = document.getElementById('solPin').value.trim();
        try {
          const d = await api('GET', 'sync?since=0');
          try { localStorage.setItem(KEY, pin); } catch (e) {}
          o.remove(); res(d);
        } catch (e) { document.getElementById('solErr').textContent = e.message || 'No se pudo entrar'; }
      };
    });
  }

  async function init() {
    let d = null;
    if (pin) { try { d = await api('GET', 'sync?since=0'); } catch (e) { d = null; } }
    if (!d) d = await pedirPin();
    version = d.version || null;
    apply(d.changes); seq = d.seq;
    loop();
    return DB;
  }

  function cmp(a, b) {
    if (a === b) return 0;
    if (a === undefined || a === null) return 1;
    if (b === undefined || b === null) return -1;
    return a < b ? -1 : 1;
  }
  const meta = { fromCache: false, hasPendingWrites: false };
  function qsnap(col, field, dir, lim) {
    let arr = [...(cache[col] || new Map()).entries()].map(([id, data]) => ({ id, data }));
    if (field) arr.sort((a, b) => cmp(a.data[field], b.data[field]) * (dir === 'desc' ? -1 : 1));
    if (lim) arr = arr.slice(0, lim);
    const docs = arr.map(x => ({ id: x.id, exists: true, data: () => x.data, metadata: meta }));
    return { docs, size: docs.length, empty: !docs.length, metadata: meta, docChanges: () => [] };
  }
  function dsnap(col, id) {
    const d = cache[col] && cache[col].get(id);
    return { id, exists: !!d, data: () => d, metadata: meta };
  }
  function watch(make, cb) {
    let last = null;
    const f = () => { const s = make(); const k = JSON.stringify(s.docs ? s.docs.map(d => [d.id, d.data()]) : (s.exists ? s.data() : null)); if (k !== last) { last = k; cb(s); } };
    listeners.add(f); setTimeout(f, 0);
    return () => listeners.delete(f);
  }
  function query(col, field, dir, lim) {
    return {
      orderBy: (f, d) => query(col, f, d || 'asc', lim),
      limit: n => query(col, field, dir, n),
      where() { return this; },
      get: async () => qsnap(col, field, dir, lim),
      onSnapshot: (cb) => watch(() => qsnap(col, field, dir, lim), cb)
    };
  }
  function docRef(col, id) {
    return {
      id, path: col + '/' + id,
      get: async () => dsnap(col, id),
      set: async d => { const r = await api('PUT', 'doc/' + col + '/' + id, d); apply([{ col, id, data: r.data }]); },
      update: async d => { const r = await api('PATCH', 'doc/' + col + '/' + id, d); apply([{ col, id, data: r.data }]); },
      delete: async () => { await api('DELETE', 'doc/' + col + '/' + id); apply([{ col, id, data: null }]); },
      onSnapshot: (cb) => watch(() => dsnap(col, id), cb)
    };
  }
  const DB = {
    doc: path => { const p = path.split('/'); return docRef(p[0], p[1]); },
    collection: col => Object.assign(query(col, null, 'asc', 0), {
      path: col,
      doc: id => docRef(col, id || rid()),
      add: async d => { const r = docRef(col, rid()); await r.set(d); return r; }
    })
  };
  const USER = { can: async () => true, isOwner: () => true, canEdit: () => true, id: async () => 'familia', me: async () => ({ id: 'familia', name: '' }) };
  const DOWNLOADS = {
    save: async ({ filename, data }) => {
      const blob = data instanceof Blob ? data : new Blob([data], { type: filename.endsWith('.json') ? 'application/json' : 'text/csv' });
      const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
      return { status: 'saved' };
    }
  };
  window.claude = {
    servidor: true,
    importar: async (json, pisar) => api('POST', 'import', { colecciones: json.colecciones || json, pisar: !!pisar }),
    use: async name => name === 'db' ? (ready || (ready = init())) : name === 'user' ? USER : name === 'downloads' ? DOWNLOADS : null
  };
})();
