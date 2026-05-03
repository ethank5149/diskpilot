import { useState, useEffect } from 'react'
import { api } from '../api.js'

export default function SkippedLog({ onClose }) {
  const [rows, setRows]   = useState([])
  const [filter, setFilter] = useState('')

  useEffect(() => {
    api.skipped().then(setRows).catch(() => {})
  }, [])

  const visible = filter
    ? rows.filter(r => r.path.toLowerCase().includes(filter.toLowerCase()) ||
                       r.reason.toLowerCase().includes(filter.toLowerCase()))
    : rows

  return (
    <>
      <div className="panel-header">
        <span className="panel-title">⚠ Skipped Paths ({rows.length})</span>
        <input
          placeholder="Filter…"
          value={filter}
          onChange={e => setFilter(e.target.value)}
          style={{
            background: 'var(--surface3)', border: '1px solid var(--border)',
            color: 'var(--text)', borderRadius: 4, padding: '3px 8px', fontSize: 12, width: 180
          }}
        />
        <button className="btn-icon" onClick={onClose}>✕</button>
      </div>
      <div className="panel-body">
        {visible.length === 0 && (
          <div style={{ padding: '12px 14px', color: 'var(--text-dim)', fontSize: 13 }}>
            {rows.length === 0 ? 'No skipped paths.' : 'No matches.'}
          </div>
        )}
        {visible.map((r, i) => (
          <div key={i} className="skip-row">
            <span className="skip-path" title={r.path}>{r.path}</span>
            <span className="skip-reason">{r.reason}</span>
          </div>
        ))}
      </div>
    </>
  )
}
