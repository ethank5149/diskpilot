import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { fmtBytes } from '../utils.js'

export default function TrashPanel({ onClose, setTrashCount }) {
  const [items, setItems] = useState([])
  const [busy, setBusy]   = useState(false)

  const load = async () => {
    try {
      const data = await api.trashList()
      setItems(data)
      setTrashCount?.(data.length)
    } catch (_) {}
  }

  useEffect(() => { load() }, [])

  const restore = async (path) => {
    try { await api.trashRestore(path); await load() }
    catch (e) { alert('Error: ' + e.message) }
  }

  const emptyTrash = async () => {
    if (!confirm('Permanently delete all trashed files? This cannot be undone.')) return
    setBusy(true)
    try { await api.trashEmpty(); await load() }
    catch (e) { alert('Error: ' + e.message) }
    finally { setBusy(false) }
  }

  const totalSize = items.reduce((s, i) => s + i.size, 0)

  return (
    <>
      <div className="panel-header">
        <span className="panel-title">
          🗑 Trash ({items.length} files · {fmtBytes(totalSize)})
        </span>
        {items.length > 0 && (
          <button className="btn-danger btn-sm" onClick={emptyTrash} disabled={busy}>
            Empty Trash
          </button>
        )}
        <button className="btn-icon" onClick={onClose}>✕</button>
      </div>
      <div className="panel-body">
        {items.length === 0 && (
          <div style={{ padding: '12px 14px', color: 'var(--text-dim)', fontSize: 13 }}>
            Trash is empty.
          </div>
        )}
        {items.map(item => (
          <div key={item.path} className="trash-item">
            <span className="trash-orig" title={item.original}>{item.original}</span>
            <span className="trash-size">{fmtBytes(item.size)}</span>
            <div className="trash-actions">
              <button
                className="btn-ghost btn-sm"
                onClick={() => restore(item.path)}
                title="Restore to original location"
              >↩ Restore</button>
            </div>
          </div>
        ))}
      </div>
    </>
  )
}
