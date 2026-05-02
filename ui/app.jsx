/* PDF Scholar — frontend conectado al bridge Python (pywebview).
 *
 * El bridge se expone como `window.pywebview.api.<metodo>(...)` y devuelve
 * Promises. El backend empuja eventos llamando a `window.__pdfBridge.on*`.
 */

const { useState, useEffect, useRef, useCallback } = React;

const PARAMS = ['autores', 'año', 'crossref título', 'tipo', 'heurísticas'];

const LOG_INIT = [
  { t: 'inf', s: 'PDF Scholar listo. Sube archivos para comenzar.' },
];

// ── Helpers ──────────────────────────────────────────────────────────────────
const apiReady = () =>
  new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) return resolve();
    window.addEventListener('pywebviewready', () => resolve(), { once: true });
  });

async function call(method, ...args) {
  await apiReady();
  return window.pywebview.api[method](...args);
}

// ── PDF page (real render via bridge) ────────────────────────────────────────
function PDFPage({ fileId, page, onLoaded }) {
  const [src, setSrc] = useState(null);

  useEffect(() => {
    let cancel = false;
    setSrc(null);
    if (fileId == null) return;
    call('render_page', fileId, page, 900).then((data) => {
      if (!cancel) {
        setSrc(data || null);
        onLoaded && onLoaded(!!data);
      }
    });
    return () => { cancel = true; };
  }, [fileId, page]);

  if (!src) {
    return (
      <div style={{
        width: '100%', height: '100%',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: '#f3ede4',
      }}>
        <div style={{ width: 18, height: 18, borderRadius: '50%',
          border: '2px solid rgba(180,165,140,0.4)',
          borderTopColor: 'rgba(199,154,67,0.85)',
          animation: 'spin-slow 0.9s linear infinite' }} />
      </div>
    );
  }

  return (
    <img src={src} alt="" draggable={false}
      style={{ display: 'block', width: '100%', height: 'auto', userSelect: 'none' }} />
  );
}

// ── Loading overlay (orchestrates analysis lifecycle) ────────────────────────
function LoadingOverlay({ files, doneIds, errorIds }) {
  const [revealed, setRevealed] = useState([]);

  useEffect(() => {
    files.forEach((_, i) => {
      const t = setTimeout(() => setRevealed((p) => p.includes(i) ? p : [...p, i]), i * 220);
      return () => clearTimeout(t);
    });
  }, [files.length]);

  const total    = files.length;
  const finished = doneIds.size + errorIds.size;
  const pct      = total === 0 ? 0 : Math.round((finished / total) * 100);
  const allDone  = total > 0 && finished >= total;

  return (
    <div className="loading-overlay" onClick={(e) => e.stopPropagation()}>
      <div className="loading-modal">

        <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:18, marginBottom: 24 }}>
          <div style={{ position:'relative', width:72, height:72 }}>
            <svg className="spinner-ring" width="72" height="72" viewBox="0 0 72 72" style={{ position:'absolute', top:0, left:0 }}>
              <circle cx="36" cy="36" r="33" fill="none"
                stroke="rgba(199,154,67,0.12)" strokeWidth="1.5"/>
              <circle cx="36" cy="36" r="33" fill="none"
                stroke="rgba(199,154,67,0.55)" strokeWidth="1.5"
                strokeDasharray="28 180" strokeLinecap="round"
                className="spinner-dash"/>
            </svg>
            <svg className="spinner-ring-rev" width="52" height="52" viewBox="0 0 52 52"
              style={{ position:'absolute', top:10, left:10 }}>
              <circle cx="26" cy="26" r="22" fill="none"
                stroke="rgba(199,154,67,0.08)" strokeWidth="1"/>
              <circle cx="26" cy="26" r="22" fill="none"
                stroke="rgba(199,154,67,0.3)" strokeWidth="1"
                strokeDasharray="14 124" strokeLinecap="round"/>
            </svg>
            <div className="breathe-dot" style={{
              position:'absolute', top:'50%', left:'50%',
              transform:'translate(-50%,-50%)',
              width:14, height:14, borderRadius:'50%',
              background:'radial-gradient(circle at 35% 35%, rgba(225,185,90,0.9), rgba(199,154,67,0.6))',
              boxShadow:'0 0 12px rgba(199,154,67,0.5), 0 0 4px rgba(199,154,67,0.8)',
            }}/>
          </div>

          <div style={{ textAlign:'center' }}>
            <div style={{ fontSize:13, fontWeight:500, color:'rgba(218,228,248,0.82)', marginBottom:4, letterSpacing:'0.01em' }}>
              {allDone ? 'Listo' : 'Procesando metadatos'}
            </div>
            <div style={{ fontSize:11, color:'rgba(145,158,195,0.45)', fontWeight:300 }}>
              {allDone ? `${finished}/${total} completado` : `${pct}% completado`}
            </div>
          </div>

          <div style={{ width:'100%', height:2, borderRadius:1, background:'rgba(255,255,255,0.05)', overflow:'hidden' }}>
            <div style={{
              height:'100%', borderRadius:1,
              background:'linear-gradient(90deg, rgba(199,154,67,0.5), rgba(225,185,90,0.9))',
              width:`${pct}%`,
              transition:'width 0.4s cubic-bezier(0.4,0,0.2,1)',
              boxShadow:'0 0 8px rgba(199,154,67,0.4)',
            }}/>
          </div>
        </div>

        <div style={{ display:'flex', flexDirection:'column', overflowY:'auto', minHeight:0 }}>
          {files.map((f, i) => revealed.includes(i) && (
            <div key={f.id} className="file-row">
              <div style={{
                width:28, height:34, borderRadius:3, flexShrink:0,
                background: errorIds.has(f.id) ? 'rgba(215,85,85,0.07)' : 'rgba(199,154,67,0.07)',
                border: errorIds.has(f.id) ? '0.5px solid rgba(215,85,85,0.22)' : '0.5px solid rgba(199,154,67,0.18)',
                display:'flex', alignItems:'center', justifyContent:'center',
              }}>
                <svg width="12" height="14" viewBox="0 0 12 14" fill="none">
                  <rect x="0.5" y="0.5" width="11" height="13" rx="1.5" stroke={errorIds.has(f.id) ? 'rgba(215,85,85,0.55)' : 'rgba(199,154,67,0.5)'} strokeWidth="0.8"/>
                  <path d="M2.5 4.5h7M2.5 6.5h7M2.5 8.5h4.5" stroke={errorIds.has(f.id) ? 'rgba(215,85,85,0.55)' : 'rgba(199,154,67,0.5)'} strokeWidth="0.7" strokeLinecap="round"/>
                </svg>
              </div>
              <div style={{ flex:1, minWidth:0 }}>
                <div style={{ fontSize:10, color:'rgba(145,158,195,0.4)', fontFamily:'Roboto Mono,monospace', marginBottom:2, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{f.orig}</div>
                <div className={(doneIds.has(f.id) || errorIds.has(f.id)) ? '' : 'shimmer-text'}
                  style={{
                    fontSize:11,
                    color: errorIds.has(f.id) ? 'rgba(215,85,85,0.85)'
                         : doneIds.has(f.id) ? 'rgba(200,210,235,0.75)'
                         : undefined,
                    overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap',
                    transition:'color 0.3s'
                  }}>
                  {errorIds.has(f.id) ? `⚠ ${f.error || 'error'}`
                    : doneIds.has(f.id) ? (f.name && f.name.length > 56 ? f.name.slice(0, 56) + '…' : f.name)
                    : 'Analizando…'}
                </div>
              </div>
              {doneIds.has(f.id) && (
                <div className="tick-appear" style={{
                  width:20, height:20, borderRadius:'50%', flexShrink:0,
                  background:'rgba(72,195,130,0.12)',
                  border:'0.5px solid rgba(72,195,130,0.3)',
                  display:'flex', alignItems:'center', justifyContent:'center',
                }}>
                  <svg width="10" height="8" viewBox="0 0 10 8" fill="none">
                    <path d="M1 4l2.8 3L9 1" stroke="rgba(72,195,130,0.9)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </div>
              )}
              {errorIds.has(f.id) && (
                <div className="tick-appear" style={{
                  width:20, height:20, borderRadius:'50%', flexShrink:0,
                  background:'rgba(215,85,85,0.12)',
                  border:'0.5px solid rgba(215,85,85,0.3)',
                  display:'flex', alignItems:'center', justifyContent:'center',
                  color:'rgba(215,85,85,0.9)', fontSize:11, fontWeight:600,
                }}>!</div>
              )}
            </div>
          ))}
        </div>

      </div>
    </div>
  );
}

// ── PDF viewer with zoom/pan + animated pill toolbar ─────────────────────────
function PDFViewer({ file, page }) {
  const [zoom, setZoom]         = useState(1);
  const [mode, setMode]         = useState('select');
  const [offset, setOffset]     = useState({ x: 0, y: 0 });
  const [pillOpen, setPillOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const dragStart = useRef(null);
  const wrapRef   = useRef(null);
  const pillTimer = useRef(null);

  const openPill  = () => { clearTimeout(pillTimer.current); setPillOpen(true); };
  const closePill = () => { pillTimer.current = setTimeout(() => setPillOpen(false), 900); };

  const clamp = (val, mn, mx) => Math.min(mx, Math.max(mn, val));
  const doZoom = (delta) => setZoom((z) => clamp(+(z + delta).toFixed(2), 0.4, 3));

  const onMouseDown = (e) => {
    if (mode !== 'pan') return;
    e.preventDefault();
    dragStart.current = { x: e.clientX - offset.x, y: e.clientY - offset.y };
    setDragging(true);
  };
  const onMouseMove = (e) => {
    if (!dragging || !dragStart.current) return;
    setOffset({ x: e.clientX - dragStart.current.x, y: e.clientY - dragStart.current.y });
  };
  const onMouseUp = () => { setDragging(false); dragStart.current = null; };

  const onWheel = (e) => { e.preventDefault(); doZoom(e.deltaY < 0 ? 0.1 : -0.1); };

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  });

  useEffect(() => { setZoom(1); setOffset({ x:0, y:0 }); setMode('select'); }, [file?.id, page]);

  if (!file || file.error) return (
    <div className="pdf-empty">
      <svg width="26" height="34" viewBox="0 0 26 34" fill="none">
        <rect x="1" y="1" width="24" height="32" rx="2" stroke="rgba(175,188,218,0.16)" strokeWidth="1.1"/>
        <path d="M6 11h14M6 16h14M6 21h9" stroke="rgba(175,188,218,0.16)" strokeWidth="0.9" strokeLinecap="round"/>
        <path d="M17 1v9h8" stroke="rgba(175,188,218,0.16)" strokeWidth="1.1"/>
      </svg>
      <div style={{ fontSize:10.5, fontWeight:300, color:'rgba(150,165,200,0.22)', textAlign:'center', lineHeight:1.65 }}>
        {file?.error ? '⚠ Error al analizar' : <>Sube un PDF<br/>para ver la<br/>vista previa</>}
      </div>
    </div>
  );

  const pct = Math.round(zoom * 100);

  return (
    <div
      ref={wrapRef}
      onMouseEnter={openPill}
      onMouseLeave={closePill}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      style={{
        position: 'relative',
        width: '100%',
        aspectRatio: '0.707',
        overflow: 'hidden',
        borderRadius: 6,
        background: 'rgba(255,255,255,0.018)',
        border: '0.5px solid rgba(255,255,255,0.06)',
        cursor: mode === 'pan' ? (dragging ? 'grabbing' : 'grab') : 'default',
        userSelect: 'none',
      }}
    >
      <div style={{
        position: 'absolute',
        top: '50%', left: '50%',
        transform: `translate(calc(-50% + ${offset.x}px), calc(-50% + ${offset.y}px)) scale(${zoom})`,
        transformOrigin: 'center center',
        transition: dragging ? 'none' : 'transform 0.06s ease-out',
        width: '88%',
        borderRadius: 4,
        overflow: 'hidden',
        boxShadow: `
          0 0 0 0.5px rgba(180,165,140,0.35),
          0 2px 8px rgba(0,0,0,0.4),
          0 8px 28px rgba(0,0,0,0.55),
          0 16px 48px rgba(0,0,0,0.35),
          inset 0 1px 0 rgba(255,255,255,0.55),
          inset 1px 0 0 rgba(255,255,255,0.12),
          inset -1px 0 0 rgba(0,0,0,0.08)
        `,
      }}>
        <div style={{
          position: 'absolute', inset: 0, zIndex: 2, pointerEvents: 'none',
          background: `
            linear-gradient(180deg, rgba(255,255,255,0.18) 0%, transparent 6%, transparent 94%, rgba(0,0,0,0.06) 100%),
            linear-gradient(90deg, rgba(255,255,255,0.08) 0%, transparent 4%, transparent 96%, rgba(0,0,0,0.04) 100%)
          `,
          borderRadius: 4,
        }} />
        <div style={{ background: '#f3ede4', borderRadius: 4 }}>
          <PDFPage fileId={file.id} page={page} />
        </div>
      </div>

      <div
        className={`zoom-pill ${pillOpen ? 'expanded' : 'collapsed'}`}
        onMouseEnter={openPill}
        onMouseLeave={closePill}
      >
        {pillOpen ? (
          <>
            <button className="zpill-btn" onClick={() => doZoom(-0.15)} title="Alejar">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <circle cx="6" cy="6" r="4.5" stroke="currentColor" strokeWidth="1.2"/>
                <path d="M4 6h4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
                <path d="M9.5 9.5L12.5 12.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              </svg>
            </button>
            <span className="zoom-pct">{pct}%</span>
            <div className="zpill-sep" />
            <button className="zpill-btn" onClick={() => doZoom(0.15)} title="Acercar">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <circle cx="6" cy="6" r="4.5" stroke="currentColor" strokeWidth="1.2"/>
                <path d="M4 6h4M6 4v4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
                <path d="M9.5 9.5L12.5 12.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              </svg>
            </button>
            <div className="zpill-sep" />
            <button
              className={`zpill-btn${mode === 'pan' ? ' active-mode' : ''}`}
              onClick={() => setMode((m) => m === 'pan' ? 'select' : 'pan')}
              title="Mover"
            >
              <svg width="15" height="15" viewBox="0 0 15 15" fill="none">
                <path d="M6 2.5V8M6 2.5C6 1.67 6.67 1 7.5 1S9 1.67 9 2.5V7M6 2.5C6 1.67 5.33 1 4.5 1S3 1.67 3 2.5V8.5M9 7C9 6.17 9.67 5.5 10.5 5.5S12 6.17 12 7V9.5C12 11.43 10.43 13 8.5 13H7C5.34 13 3.92 11.92 3.3 10.42L2 7.5C1.73 6.84 2.05 6.08 2.72 5.84C3.36 5.61 4.06 5.92 4.32 6.54L4.5 7" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </button>
          </>
        ) : (
          <button className="zpill-btn" style={{ width:36, height:36 }}
            onClick={openPill} title="Herramientas de zoom">
            <svg width="15" height="15" viewBox="0 0 15 15" fill="none">
              <circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" strokeWidth="1.2"/>
              <path d="M4.5 6.5h4M6.5 4.5v4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              <path d="M10 10L13 13" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}

// ── Console ──────────────────────────────────────────────────────────────────
function Console({ logs }) {
  const r = useRef(null);
  useEffect(() => { if (r.current) r.current.scrollTop = r.current.scrollHeight; }, [logs]);
  const cls = (t) => t === 'ok' ? 'l-ok' : t === 'err' ? 'l-err' : t === 'warn' ? 'l-warn' : 'l-inf';
  return (
    <div ref={r} className="console-wrap">
      {logs.map((l, i) => (
        <div key={i} className={cls(l.t)}
          style={{ paddingLeft: l.s.startsWith('  ') ? 12 : 0 }}>{l.s}</div>
      ))}
    </div>
  );
}

// ── File item ────────────────────────────────────────────────────────────────
function FileItem({ f, sel, onSel, onSave, onRm, onChange }) {
  return (
    <div className={`gl-item ${sel ? 'sel' : ''}`}
      style={{ padding:'10px 12px', marginBottom:6, cursor:'pointer' }}
      onClick={() => onSel(f.id)}
    >
      <div style={{ display:'flex', alignItems:'center', gap:6, marginBottom:6 }}>
        <span className={`src-badge${f.error ? ' err' : ''}`}>{f.error ? 'error' : (f.src || '...')}</span>
        <span style={{ fontSize:9.5, color:'rgba(145,158,192,0.32)', fontFamily:'Roboto Mono,monospace', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', flex:1 }}>{f.orig}</span>
      </div>
      <div style={{ display:'flex', alignItems:'center', gap:8 }}>
        <input className={`finput${f.saved ? ' done' : ''}${f.error ? ' err' : ''}`}
          value={f.error ? `⚠ ${f.error}` : (f.name || 'Analizando…')}
          disabled={!!f.error || !f.name}
          onChange={(e) => onChange(f.id, e.target.value)}
          onClick={(e) => e.stopPropagation()}
        />
        {!f.saved && !f.error && f.name && (
          <button className="btn-save" onClick={(e) => { e.stopPropagation(); onSave(f.id); }}>Guardar</button>
        )}
        {f.saved && <span className="saved-chip">✓ GUARDADO</span>}
        <button className="rm-btn" onClick={(e) => { e.stopPropagation(); onRm(f.id); }}>×</button>
      </div>
    </div>
  );
}

// ── App ──────────────────────────────────────────────────────────────────────
function App() {
  const [files,    setFiles]    = useState([]);
  const [selId,    setSelId]    = useState(null);
  const [logs,     setLogs]     = useState(LOG_INIT);
  const [tab,      setTab]      = useState('preview');
  const [pg,       setPg]       = useState(1);
  const [leftPct,  setLeftPct]  = useState(60);
  const [rightW,   setRightW]   = useState(285);
  const [logH,     setLogH]     = useState(130);
  const [busy,     setBusy]     = useState(false);
  const [overlay,  setOverlay]  = useState(null);  // { files: [...] }
  const [doneIds,  setDoneIds]  = useState(() => new Set());
  const [errorIds, setErrorIds] = useState(() => new Set());
  const [pageText, setPageText] = useState('');
  const [env,      setEnv]      = useState({ ocrAvailable:false, thumbAvailable:false, maxFiles:50 });

  const dragH = useRef(false);
  const dragV = useRef(false);
  const dragR = useRef(false);
  const cRef  = useRef(null);

  const sel   = files.find((f) => f.id === selId) || null;
  const saved = files.filter((f) => f.saved).length;
  const total = files.length;
  const hasFiles = total > 0;

  // ── Bridge wiring (events from Python) ─────────────────────────────────────
  useEffect(() => {
    window.__pdfBridge = {
      onLog: (entry) => {
        setLogs((p) => [...p.slice(-300), { t: entry.level, s: entry.msg }]);
      },
      onProgress: (p) => {
        if (p.status === 'analyzing') return;
        if (p.status === 'done') {
          setFiles((prev) => prev.map((f) => f.id === p.id
            ? { ...f, name: p.name, src: p.src, pageCount: p.pageCount, error: '' }
            : f));
          setDoneIds((prev) => { const n = new Set(prev); n.add(p.id); return n; });
        }
        if (p.status === 'error') {
          setFiles((prev) => prev.map((f) => f.id === p.id ? { ...f, error: p.error } : f));
          setErrorIds((prev) => { const n = new Set(prev); n.add(p.id); return n; });
        }
        if (p.status === 'saved') {
          setFiles((prev) => prev.map((f) => f.id === p.id ? { ...f, saved: true } : f));
        }
      },
      onDone: () => {
        setBusy(false);
        // close overlay shortly after, leaving room for the user to read it
        setTimeout(() => setOverlay(null), 700);
      },
    };

    apiReady().then(async () => {
      const e = await call('env');
      setEnv(e);
      setLogs((p) => [
        ...p,
        { t: 'inf', s: `Límite: ${e.maxFiles} archivos por sesión.` },
        ...(e.ocrAvailable ? [{ t: 'inf', s: 'OCR activo.' }]
                           : [{ t: 'warn', s: 'OCR inactivo. Instalar pytesseract + Tesseract para activarlo.' }]),
      ]);
    });
  }, []);

  // ── Resize drag ─────────────────────────────────────────────────────────────
  useEffect(() => {
    const mv = (e) => {
      if (dragH.current && cRef.current) {
        const r = cRef.current.getBoundingClientRect();
        setLeftPct(Math.min(80, Math.max(20, ((e.clientX - r.left) / (r.width - rightW - 1)) * 100)));
      }
      if (dragV.current && cRef.current) {
        const r = cRef.current.getBoundingClientRect();
        setLogH(Math.min(280, Math.max(55, r.bottom - e.clientY)));
      }
      if (dragR.current && cRef.current) {
        const r = cRef.current.getBoundingClientRect();
        setRightW(Math.min(420, Math.max(180, r.right - e.clientX)));
      }
    };
    const up = () => { dragH.current = false; dragV.current = false; dragR.current = false; };
    window.addEventListener('mousemove', mv);
    window.addEventListener('mouseup', up);
    return () => { window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up); };
  }, [rightW]);

  // ── Selected file → fetch page text for "Texto" tab ─────────────────────────
  useEffect(() => {
    if (!sel || sel.error) { setPageText(''); return; }
    call('get_text', sel.id, pg).then((t) => setPageText(t || ''));
  }, [sel?.id, pg]);

  // ── Actions ─────────────────────────────────────────────────────────────────
  const upload = async () => {
    if (busy) return;
    const paths = await call('pick_files');
    if (!paths || !paths.length) return;
    const newItems = await call('analyze', paths);
    if (!newItems || !newItems.length) return;
    setBusy(true);
    setFiles((prev) => [...prev, ...newItems]);
    if (selId == null && newItems[0]) setSelId(newItems[0].id);
    setOverlay({ files: newItems });
    setDoneIds((p) => { const n = new Set(p); newItems.forEach((x) => n.delete(x.id)); return n; });
    setErrorIds((p) => { const n = new Set(p); newItems.forEach((x) => n.delete(x.id)); return n; });
  };

  const saveAll = async () => {
    const r = await call('save_all');
    if (r && r.saved) setFiles((prev) => prev.map((f) => f.saved || (!f.error && f.name) ? { ...f, saved: true } : f));
  };

  const clear = async () => {
    await call('clear_all');
    setFiles([]); setSelId(null); setDoneIds(new Set()); setErrorIds(new Set());
    setLogs((p) => [...p, { t: 'inf', s: 'Cola limpiada.' }]);
  };

  const saveOne = async (id) => {
    const r = await call('save_one', id);
    if (r && r.saved) setFiles((prev) => prev.map((f) => f.id === id ? { ...f, saved: true } : f));
  };

  const removeOne = async (id) => {
    await call('remove_file', id);
    setFiles((prev) => {
      const next = prev.filter((f) => f.id !== id);
      if (selId === id) setSelId(next[0]?.id ?? null);
      return next;
    });
    setDoneIds((p) => { const n = new Set(p); n.delete(id); return n; });
    setErrorIds((p) => { const n = new Set(p); n.delete(id); return n; });
  };

  const changeName = (id, v) => {
    setFiles((prev) => prev.map((f) => f.id === id ? { ...f, name: v, saved: false } : f));
    call('update_name', id, v);
  };

  const pick = (id) => { setSelId(id); setPg(1); };

  // ── Tweaks ──────────────────────────────────────────────────────────────────
  const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
    "showConsole": true,
  }/*EDITMODE-END*/;
  const [tw, setTweak] = (window.useTweaks || ((d) => [d, () => {}]))(TWEAK_DEFAULTS);

  const TOP_H   = 50;
  const PARAM_H = 34;

  const totalPages = sel?.pageCount || 1;

  return (
    <div style={{ height:'100vh', display:'flex', flexDirection:'column', overflow:'hidden', userSelect:'none' }}>

      {/* TOP BAR */}
      <div className="gl-top" style={{ height:TOP_H, flexShrink:0, display:'flex', alignItems:'center', padding:'0 16px', gap:8 }}>

        <div style={{
          width:30, height:30, borderRadius:8, flexShrink:0,
          background:'linear-gradient(145deg, rgba(199,154,67,0.13), rgba(199,154,67,0.06))',
          border:'0.5px solid rgba(199,154,67,0.22)',
          boxShadow:'inset 0 1px 0 rgba(199,154,67,0.14)',
          display:'flex', alignItems:'center', justifyContent:'center',
        }}>
          <svg width="14" height="17" viewBox="0 0 14 17" fill="none">
            <rect x="1" y="1" width="12" height="15" rx="1.5" stroke="rgba(199,154,67,0.65)" strokeWidth="1"/>
            <path d="M3 5.5h8M3 8.5h8M3 11.5h5" stroke="rgba(199,154,67,0.65)" strokeWidth="0.85" strokeLinecap="round"/>
          </svg>
        </div>

        <div style={{ display:'flex', flexDirection:'column', justifyContent:'center', marginRight:4 }}>
          <span style={{ fontSize:14, fontWeight:700, letterSpacing:'-0.02em', color:'rgba(228,234,248,0.92)', lineHeight:1.15 }}>PDF Scholar</span>
          <div style={{ display:'flex', gap:10 }}>
            {['Archivo','Editar','Documentos'].map((m) => (
              <span key={m} style={{ fontSize:10, color:'rgba(145,158,195,0.38)', cursor:'pointer', fontWeight:300, letterSpacing:'0.015em', transition:'color 0.15s' }}
                onMouseEnter={(e) => e.target.style.color = 'rgba(195,208,238,0.65)'}
                onMouseLeave={(e) => e.target.style.color = 'rgba(145,158,195,0.38)'}
              >{m}</span>
            ))}
          </div>
        </div>

        <div style={{ width:'0.5px', height:26, background:'rgba(255,255,255,0.06)', margin:'0 6px' }} />

        <button className="btn-primary" onClick={upload} disabled={busy}>
          <svg width="11" height="11" viewBox="0 0 11 11" fill="none">
            <path d="M5.5 1.5v5.5M3 4l2.5-2.5L8 4M1.5 9.5h8" stroke="rgba(20,14,0,0.8)" strokeWidth="1.35" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
          Subir PDFs
        </button>

        <button className="btn-ghost" onClick={saveAll} disabled={!hasFiles || busy}>Guardar todos</button>
        <button className="btn-ghost danger" onClick={clear} disabled={!hasFiles || busy}>Limpiar cola</button>

        <div style={{ flex:1 }} />

        {hasFiles && (
          <div style={{ display:'flex', gap:6, alignItems:'center' }}>
            <span style={{ fontSize:11, color:'rgba(199,154,67,0.72)', fontWeight:400 }}>{total} analizados</span>
            <span style={{ color:'rgba(255,255,255,0.12)', fontSize:12 }}>·</span>
            <span style={{ fontSize:11, color:'rgba(72,195,130,0.68)', fontWeight:400 }}>{saved} guardados</span>
          </div>
        )}

        <div style={{
          display:'flex', alignItems:'center', gap:5,
          padding:'4px 9px', borderRadius:5,
          background: env.ocrAvailable ? 'rgba(72,195,130,0.07)' : 'rgba(220,170,70,0.07)',
          border: env.ocrAvailable ? '0.5px solid rgba(72,195,130,0.22)' : '0.5px solid rgba(220,170,70,0.22)',
          marginLeft:6,
        }}>
          <span style={{
            width:5, height:5, borderRadius:'50%',
            background: env.ocrAvailable ? 'rgba(72,195,130,0.85)' : 'rgba(220,170,70,0.85)',
          }}/>
          <span style={{ fontSize:9, fontWeight:500, letterSpacing:'0.07em',
            color: env.ocrAvailable ? 'rgba(72,195,130,0.78)' : 'rgba(220,170,70,0.78)' }}>
            {env.ocrAvailable ? 'OCR' : 'OCR OFF'}
          </span>
        </div>

      </div>

      <div className="gline" />

      {/* PARAM BAR */}
      <div style={{ height:PARAM_H, flexShrink:0, borderBottom:'0.5px solid rgba(255,255,255,0.035)' }}>
        <div className="pbar">
          {PARAMS.map((p,i) => (
            <React.Fragment key={i}>
              <span className="ptag">{p}</span>
              {i < PARAMS.length-1 && <span className="parr">→</span>}
            </React.Fragment>
          ))}
        </div>
      </div>

      {/* MAIN */}
      <div ref={cRef} style={{ flex:1, display:'flex', minHeight:0, overflow:'hidden' }}>

        {/* LEFT: queue */}
        <div style={{ flex:1, minWidth:200, display:'flex', flexDirection:'column', overflow:'hidden' }}>
          <div style={{ padding:'7px 14px 4px', borderBottom:'0.5px solid rgba(255,255,255,0.035)', display:'flex', alignItems:'center', justifyContent:'space-between', flexShrink:0 }}>
            <span className="mlabel">Cola</span>
            {hasFiles && <span style={{ fontSize:9.5, color:'rgba(145,158,195,0.28)' }}>{total} archivo{total!==1?'s':''}</span>}
          </div>
          {hasFiles && (
            <div style={{ padding:'3px 14px 4px', borderBottom:'0.5px solid rgba(255,255,255,0.025)', flexShrink:0 }}>
              <span style={{ fontSize:9, color:'rgba(145,158,195,0.22)', fontWeight:300, letterSpacing:'0.02em' }}>Nombre sugerido para renombrar</span>
            </div>
          )}
          <div style={{ flex:1, overflowY:'auto', padding: hasFiles ? '10px 12px' : 0 }}>
            {!hasFiles ? (
              <div style={{ display:'flex', flexDirection:'column', alignItems:'center', justifyContent:'center', height:'100%', gap:14, color:'rgba(145,158,195,0.22)' }}>
                <svg width="38" height="46" viewBox="0 0 38 46" fill="none">
                  <rect x="1.5" y="1.5" width="35" height="43" rx="3" stroke="rgba(175,188,218,0.13)" strokeWidth="1.2"/>
                  <path d="M9 15h20M9 21h20M9 27h13" stroke="rgba(175,188,218,0.13)" strokeWidth="1" strokeLinecap="round"/>
                  <path d="M25 1.5v11h11" stroke="rgba(175,188,218,0.13)" strokeWidth="1.2"/>
                </svg>
                <div style={{ textAlign:'center', lineHeight:1.85 }}>
                  <div style={{ fontSize:12.5, fontWeight:400, color:'rgba(180,192,228,0.28)' }}>La cola está vacía.</div>
                  <div style={{ fontSize:11, fontWeight:300, color:'rgba(145,158,195,0.2)' }}>
                    Haz clic en <span style={{ color:'rgba(199,154,67,0.48)', fontWeight:500 }}>Subir PDFs</span> para empezar.
                  </div>
                </div>
              </div>
            ) : files.map((f) => (
              <FileItem key={f.id} f={f} sel={f.id===selId}
                onSel={pick} onSave={saveOne} onRm={removeOne} onChange={changeName}
              />
            ))}
          </div>
        </div>

        {/* H divider */}
        <div className="div-h" onMouseDown={() => { dragR.current = true; }} />

        {/* RIGHT PANEL */}
        <div style={{ width:rightW, minWidth:180, flexShrink:0, display:'flex', flexDirection:'column', overflow:'hidden', borderLeft:'0.5px solid rgba(255,255,255,0.04)', background:'rgba(0,0,0,0.12)' }}>
          <div style={{ padding:'7px 10px 6px', borderBottom:'0.5px solid rgba(255,255,255,0.035)', display:'flex', alignItems:'center', gap:3, flexShrink:0 }}>
            <button className={`tab${tab==='preview'?' on':''}`} onClick={() => setTab('preview')}>Vista Previa</button>
            <button className={`tab${tab==='text'?' on':''}`}    onClick={() => setTab('text')}>Texto</button>
            {sel && <span style={{ flex:1 }}/>}
            {sel && <span style={{ fontSize:9, color:'rgba(145,158,195,0.4)', fontFamily:'Roboto Mono,monospace', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap', maxWidth:120 }}>{sel.orig.slice(0, 18)}{sel.orig.length>18?'…':''}</span>}
          </div>

          {tab==='preview' ? (
            <div style={{ flex:1, padding:12, display:'flex', flexDirection:'column', gap:8, overflow:'hidden' }}>
              <div style={{ flex:1, position:'relative', minHeight:0 }}>
                <PDFViewer file={sel} page={pg} />
              </div>
              {sel && !sel.error && (
                <div style={{ display:'flex', alignItems:'center', justifyContent:'center', gap:5, flexShrink:0 }}>
                  <button className="pg-btn" onClick={() => setPg((p) => Math.max(1, p-1))} disabled={pg<=1}>‹</button>
                  <span style={{ fontSize:10, color:'rgba(145,158,195,0.38)', minWidth:62, textAlign:'center', fontVariantNumeric:'tabular-nums' }}>pág. {pg} / {totalPages}</span>
                  <button className="pg-btn" onClick={() => setPg((p) => Math.min(totalPages, p+1))} disabled={pg>=totalPages}>›</button>
                </div>
              )}
              {sel && (
                <div style={{ fontSize:9, color:'rgba(145,158,195,0.2)', textAlign:'center', fontFamily:'Roboto Mono,monospace', letterSpacing:'0.02em', flexShrink:0, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{sel.orig}</div>
              )}
            </div>
          ) : (
            <div style={{ flex:1, display:'flex', flexDirection:'column', padding:12, gap:6, overflow:'hidden' }}>
              <span className="mlabel">Texto extraído · pág. {pg}</span>
              <textarea className="txtarea" value={pageText || (sel ? '(sin texto)' : 'Sin selección')} readOnly />
            </div>
          )}
        </div>
      </div>

      {/* CONSOLE */}
      {tw.showConsole && (
        <>
          <div className="div-v" onMouseDown={() => { dragV.current = true; }} />
          <div style={{ height:logH, flexShrink:0, display:'flex', flexDirection:'column', overflow:'hidden', background:'rgba(0,0,0,0.22)', borderTop:'0.5px solid rgba(255,255,255,0.035)' }}>
            <div style={{ padding:'4px 14px', borderBottom:'0.5px solid rgba(255,255,255,0.028)', display:'flex', alignItems:'center', justifyContent:'space-between', flexShrink:0 }}>
              <span className="mlabel">Consola</span>
              <button onClick={() => setLogs(LOG_INIT)} style={{ background:'transparent', border:'none', color:'rgba(145,158,195,0.28)', fontSize:10, cursor:'pointer', fontFamily:'Roboto,sans-serif', letterSpacing:'0.04em', transition:'color 0.15s' }}
                onMouseEnter={(e) => e.target.style.color = 'rgba(195,208,238,0.58)'}
                onMouseLeave={(e) => e.target.style.color = 'rgba(145,158,195,0.28)'}
              >Limpiar</button>
            </div>
            <Console logs={logs} />
          </div>
        </>
      )}

      {/* STATUS BAR */}
      <div className="sbar">
        <div className={`sbar-dot${busy ? ' busy' : ''}`} />
        <span className="sbar-txt">{hasFiles ? `${total} PDFs en cola · ${saved} guardados` : 'Sin archivos en cola'}</span>
        <div style={{ flex:1 }} />
        {sel && <span className="sbar-txt" style={{ fontFamily:'Roboto Mono,monospace' }}>{sel.orig}</span>}
        <div style={{ width:'0.5px', height:11, background:'rgba(255,255,255,0.06)' }} />
        <span className="sbar-txt">modo: renombrar</span>
      </div>

      {/* TWEAKS */}
      {window.TweaksPanel && (
        <window.TweaksPanel title="Tweaks">
          <window.TweakSection label="Interfaz" />
          <window.TweakToggle label="Mostrar consola" value={tw.showConsole} onChange={(v) => setTweak('showConsole', v)} />
        </window.TweaksPanel>
      )}

      {/* LOADING OVERLAY */}
      {overlay && <LoadingOverlay files={overlay.files} doneIds={doneIds} errorIds={errorIds} />}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
