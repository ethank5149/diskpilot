import { useState, useEffect, useRef } from 'react'
import { api } from '../api.js'
import { fmtBytes } from '../utils.js'

export default function DupePanel({ onClose, dupeState, setDupeState }) {
  const [groups, setGroups] = useState([])
  const pollRef = useRef(null)

  useEffect(() => {
    if (dupeState.status === 'complete') {
      api.dupsResults().then(setGroups).catch(() => {})
    }
  }, [dupeState.status])

  useEffect(() => {
    if (dupeState.status === 'hashing') {
      pollRef.current = setInterval(async () => {
        try {
          const s = await api.dupsStatus()
          setDupeState(s)
          if (s.status === 'complete') {
            clearInterval(pollRef.current)
            api.dupsResults().then(setGroups).catch(() => {})
          }
        } catch (_) {}
      }, 800)
    }
    return () => clearInterval(pollRef.current)
  }, [dupeState.status, setDupeState])

  const startScan = async () => {
    await api.dupsStart()
    setDupeState({ status: 'hashing', done: 0, total: 0, groups: 0 })
  }

  const trashOne = async (path) => {
    try {
      await api.trashMove(path)
      const s = await api.dupsResults()
      setGroups(s)
    } catch (e) { alert('Error: ' + e.message) }
  }

  const totalWasted = groups.reduce((s, g) => s + g.size * (g.count - 1), 0)
  const pct = dupeState.total > 0 ? Math.round((dupeState.done / dupeState.total) * 100) : 0

  return (
    <>
      <div className="panel-header">
        <span className="panel-title">
          🔍 Duplicates
          {dupeState.status === 'complete' && groups.length > 0 && (
            <span style={{ color: 'var(--warning)', marginLeft: 8, fontWeight: 400, fontSize: 12 }}>
              {groups.length} groups · {fmtBytes(totalWasted)} reclaimable
            </span>
          )}
        </span>
        {dupeState.status === 'idle' || dupeState.status === 'complete' ? (
          <button className="btn-ghost btn-sm" onClick={startScan}>
            {dupeState.status === 'complete' ? 'Re-scan' : 'Scan for Duplicates'}
          </button>
        ) : (
          <span style={{ fontSize: 12, color: 'var(--accent)' }}>
            <span className="spinner" style={{ marginRight: 6 }} />
            {dupeState.done?.toLocaleString()} / {dupeState.total?.toLocaleString()} ({pct}%)
          </span>
        )}
        <button className="btn-icon" onClick={onClose}>✕</button>
      </div>

      <div className="panel-body">
        {dupeState.status === 'hashing' && (
          <div style={{ padding: '8px 14px' }}>
            <div style={{ background: 'var(--surface3)', borderRadius: 2, height: 4, overflow: 'hidden' }}>
              <div style={{ height: '100%', background: 'var(--accent)', width: pct + '%', transition: 'width .3s' }} />
            </div>
            <div style={{ fontSize: 11, color: 'var(--text-mute)', marginTop: 4, fontFamily: 'var(--mono)' }}>
              {dupeState.current?.split('/').slice(-1)[0]}
            </div>
          </div>
        )}

        {dupeState.status === 'idle' && (
          <div style={{ padding: '12px 14px', color: 'var(--text-dim)', fontSize: 13 }}>
            Run a duplicate scan to find files with identical content.
          </div>
        )}

        {groups.map(g => (
          <div key={g.hash} className="dup-group">
            <div className="dup-header">
              <span className="dup-size">{fmtBytes(g.size)}</span>
              <span className="dup-savings">× {g.count} — save {fmtBytes(g.size * (g.count - 1))}</span>
            </div>
            {g.paths.map((p, i) => (
              <div key={p} className="dup-path">
                <span style={{ color: i === 0 ? 'var(--success)' : 'var(--text-dim)', flexShrink: 0, fontSize: 10 }}>
                  {i === 0 ? '✓ keep' : '  copy'}
                </span>
                <span className="truncate" title={p}>{p}</span>
                {i > 0 && (
                  <div className="dup-actions">
                    <button className="btn-icon btn-sm" title="Move to Trash" onClick={() => trashOne(p)}>🗑</button>
                  </div>
                )}
              </div>
            ))}
          </div>
        ))}

        {dupeState.status === 'complete' && groups.length === 0 && (
          <div style={{ padding: '12px 14px', color: 'var(--success)', fontSize: 13 }}>
            ✓ No duplicates found.
          </div>
        )}
      </div>
    </>
  )
}
