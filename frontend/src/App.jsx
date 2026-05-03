import { useState, useEffect, useCallback, useRef } from 'react'
import { api } from './api.js'
import { fmtBytes } from './utils.js'
import TreeMap   from './components/TreeMap.jsx'
import Sunburst  from './components/Sunburst.jsx'
import FileList  from './components/FileList.jsx'
import SkippedLog from './components/SkippedLog.jsx'
import DupePanel  from './components/DupePanel.jsx'
import TrashPanel from './components/TrashPanel.jsx'

const VIEWS  = ['treemap', 'sunburst', 'list']
const PANELS = ['skipped', 'dupes', 'trash']

export default function App() {
  // ── scan state ──────────────────────────────────────────
  const [scan, setScan]       = useState({ status: 'idle' })
  const pollRef               = useRef(null)

  // ── tree / nav state ────────────────────────────────────
  const [treeData, setTreeData]   = useState(null)
  const [crumbs, setCrumbs]       = useState([{ name: 'root', path: '__root__' }])
  const [loading, setLoading]     = useState(false)

  // ── view / panel state ──────────────────────────────────
  const [view, setView]       = useState('treemap')
  const [panel, setPanel]     = useState(null)

  // ── counts for badges ───────────────────────────────────
  const [skipCount, setSkipCount]   = useState(0)
  const [trashCount, setTrashCount] = useState(0)
  const [dupeState, setDupeState]   = useState({ status: 'idle' })

  // ── poll scan status ────────────────────────────────────
  useEffect(() => {
    pollRef.current = setInterval(async () => {
      try {
        const s = await api.scanStatus()
        setScan(s)
        if (s.status === 'complete' && !treeData) {
          loadTree('__root__', [{ name: 'root', path: '__root__' }])
        }
      } catch (_) {}
    }, 800)
    return () => clearInterval(pollRef.current)
  }, [treeData])

  // ── initial mounts check ─────────────────────────────────
  useEffect(() => {
    api.scanStatus().then(setScan).catch(() => {})
  }, [])

  const loadTree = useCallback(async (path, newCrumbs) => {
    setLoading(true)
    try {
      const data = await api.getTree(path, 3)
      setTreeData(data)
      if (newCrumbs) setCrumbs(newCrumbs)
    } finally {
      setLoading(false)
    }
  }, [])

  const navigateTo = useCallback((path, name) => {
    const existingIdx = crumbs.findIndex(c => c.path === path)
    let newCrumbs
    if (existingIdx >= 0) {
      newCrumbs = crumbs.slice(0, existingIdx + 1)
    } else {
      newCrumbs = [...crumbs, { name, path }]
    }
    loadTree(path, newCrumbs)
  }, [crumbs, loadTree])

  const startScan = async () => {
    setTreeData(null)
    setCrumbs([{ name: 'root', path: '__root__' }])
    await api.scanStart()
    setScan({ status: 'scanning' })
  }

  const currentPath = crumbs[crumbs.length - 1]?.path ?? '__root__'

  // ── refresh badge counts when panel opens ──────────────
  useEffect(() => {
    if (panel === 'skipped') {
      api.skipped().then(r => setSkipCount(r.length)).catch(() => {})
    }
    if (panel === 'trash') {
      api.trashList().then(r => setTrashCount(r.length)).catch(() => {})
    }
  }, [panel])

  useEffect(() => {
    if (scan.status === 'complete') {
      api.skipped().then(r => setSkipCount(r.length)).catch(() => {})
    }
  }, [scan.status])

  // ── status bar text ────────────────────────────────────
  const statusText = () => {
    if (scan.status === 'scanning') {
      return `Scanning: ${scan.current?.split('/').slice(-2).join('/') ?? '...'} — ${scan.files?.toLocaleString()} files, ${fmtBytes(scan.bytes)}`
    }
    if (scan.status === 'complete') {
      return `Indexed ${scan.files?.toLocaleString()} files · ${scan.dirs?.toLocaleString()} dirs · ${fmtBytes(scan.bytes)} · ${scan.elapsed}s`
    }
    return 'Click Scan to analyse your storage'
  }

  return (
    <div className="app">
      {/* ── Header ── */}
      <header className="header">
        <div className="header-logo">🖥 Disk<span>Pilot</span></div>
        <div className={`header-status ${scan.status === 'scanning' ? 'scanning' : ''}`}>
          {scan.status === 'scanning' && <span className="spinner" style={{ marginRight: 8 }} />}
          {statusText()}
        </div>
        <button
          className="btn-primary"
          onClick={startScan}
          disabled={scan.status === 'scanning'}
        >
          {scan.status === 'scanning' ? 'Scanning…' : 'Scan'}
        </button>
      </header>

      {scan.status === 'scanning' && (
        <div className="progress-bar">
          <div className="progress-fill indeterminate" />
        </div>
      )}

      <div className="app-body">
        {/* ── Sidebar ── */}
        <aside className="sidebar">
          <div className="sidebar-section">
            <div className="sidebar-label">Views</div>
            {VIEWS.map(v => (
              <div
                key={v}
                className={`sidebar-item ${view === v ? 'active' : ''}`}
                onClick={() => setView(v)}
              >
                {v === 'treemap' ? '▦' : v === 'sunburst' ? '◉' : '≡'}&nbsp;
                {v.charAt(0).toUpperCase() + v.slice(1)}
              </div>
            ))}
          </div>

          <div className="sidebar-section" style={{ flex: 1 }}>
            <div className="sidebar-label">Panels</div>
            <div
              className={`sidebar-item ${panel === 'skipped' ? 'active' : ''}`}
              onClick={() => setPanel(panel === 'skipped' ? null : 'skipped')}
            >
              ⚠ Skipped
              {skipCount > 0 && <span className="sidebar-badge warn">{skipCount}</span>}
            </div>
            <div
              className={`sidebar-item ${panel === 'dupes' ? 'active' : ''}`}
              onClick={() => setPanel(panel === 'dupes' ? null : 'dupes')}
            >
              🔍 Duplicates
              {dupeState.groups > 0 && <span className="sidebar-badge info">{dupeState.groups}</span>}
            </div>
            <div
              className={`sidebar-item ${panel === 'trash' ? 'active' : ''}`}
              onClick={() => setPanel(panel === 'trash' ? null : 'trash')}
            >
              🗑 Trash
              {trashCount > 0 && <span className="sidebar-badge">{trashCount}</span>}
            </div>
          </div>
        </aside>

        {/* ── Main ── */}
        <div className="main">
          {/* breadcrumb */}
          <nav className="breadcrumb">
            {crumbs.map((c, i) => (
              <span key={c.path}>
                {i < crumbs.length - 1
                  ? <span className="breadcrumb-seg" onClick={() => navigateTo(c.path, c.name)}>{c.name}</span>
                  : <span className="breadcrumb-cur">{c.name}</span>
                }
                {i < crumbs.length - 1 && <span className="breadcrumb-sep"> / </span>}
              </span>
            ))}
            {loading && <span className="spinner" style={{ marginLeft: 8 }} />}
          </nav>

          {/* view tabs */}
          <div className="view-tabs">
            {VIEWS.map(v => (
              <button key={v} className={`view-tab ${view === v ? 'active' : ''}`} onClick={() => setView(v)}>
                {v === 'treemap' ? '▦ Treemap' : v === 'sunburst' ? '◉ Sunburst' : '≡ List'}
              </button>
            ))}
            {treeData && (
              <span style={{ marginLeft: 'auto', color: 'var(--text-dim)', fontSize: 12, alignSelf: 'center' }}>
                {fmtBytes(treeData.size)} · {treeData.cnt?.toLocaleString()} files
              </span>
            )}
          </div>

          {/* main content */}
          <div className="content">
            {!treeData && scan.status !== 'scanning' && (
              <div className="empty">
                <div className="empty-icon">🖥</div>
                <div className="empty-title">No data yet</div>
                <div className="empty-sub">Click <strong>Scan</strong> to index your storage</div>
              </div>
            )}
            {!treeData && scan.status === 'scanning' && (
              <div className="empty">
                <div className="spinner" style={{ width: 32, height: 32, borderWidth: 3 }} />
                <div className="empty-title">Scanning…</div>
                <div className="empty-sub mono" style={{ fontSize: 11 }}>
                  {scan.files?.toLocaleString()} files · {fmtBytes(scan.bytes)}
                </div>
              </div>
            )}
            {treeData && view === 'treemap' && (
              <TreeMap data={treeData} onNavigate={navigateTo} />
            )}
            {treeData && view === 'sunburst' && (
              <Sunburst data={treeData} onNavigate={navigateTo} />
            )}
            {treeData && view === 'list' && (
              <FileList
                path={currentPath}
                treeData={treeData}
                onNavigate={navigateTo}
                onRefresh={() => loadTree(currentPath, null)}
              />
            )}
          </div>

          {/* panel */}
          {panel && (
            <div className="panel">
              {panel === 'skipped' && <SkippedLog onClose={() => setPanel(null)} />}
              {panel === 'dupes'   && <DupePanel  onClose={() => setPanel(null)} dupeState={dupeState} setDupeState={setDupeState} />}
              {panel === 'trash'   && <TrashPanel onClose={() => setPanel(null)} onRefresh={() => {}} setTrashCount={setTrashCount} />}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
