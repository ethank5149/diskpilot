import { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'
import { fmtBytes, fmtDate, fileIcon } from '../utils.js'

function ConfirmDialog({ path, action, onConfirm, onCancel }) {
  return (
    <div className="overlay" onClick={onCancel}>
      <div className="dialog" onClick={e => e.stopPropagation()}>
        <h3>{action === 'trash' ? '🗑 Move to Trash' : '⚠ Permanently Delete'}</h3>
        <p>{path}</p>
        {action === 'delete' && (
          <p style={{ color: 'var(--danger)', fontSize: 12, marginTop: -12 }}>This cannot be undone.</p>
        )}
        <div className="dialog-btns">
          <button className="btn-ghost" onClick={onCancel}>Cancel</button>
          <button
            className={action === 'delete' ? 'btn-danger' : 'btn-primary'}
            onClick={onConfirm}
          >
            {action === 'trash' ? 'Move to Trash' : 'Delete Forever'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function FileList({ path, onNavigate, onRefresh }) {
  const [items, setItems]     = useState([])
  const [total, setTotal]     = useState(0)
  const [sort, setSort]       = useState('size')
  const [loading, setLoading] = useState(false)
  const [confirm, setConfirm] = useState(null) // { path, action }
  const [maxSize, setMaxSize] = useState(1)

  const load = useCallback(async (p, s) => {
    setLoading(true)
    try {
      const res = await api.ls(p, s ?? sort, 0, 200)
      setItems(res.items)
      setTotal(res.total)
      const mx = res.items.reduce((m, r) => Math.max(m, r.size ?? 0), 1)
      setMaxSize(mx)
    } finally {
      setLoading(false)
    }
  }, [sort])

  useEffect(() => { load(path, sort) }, [path, sort])

  const handleSort = col => {
    const newSort = col === sort ? (col === 'size' ? 'name' : 'size') : col
    setSort(newSort)
  }

  const doAction = async () => {
    if (!confirm) return
    try {
      if (confirm.action === 'trash') {
        await api.trashMove(confirm.path)
      } else {
        await api.deletePerm(confirm.path)
      }
      setConfirm(null)
      load(path, sort)
      onRefresh?.()
    } catch (e) {
      alert('Error: ' + e.message)
      setConfirm(null)
    }
  }

  const th = (col, label) => (
    <th onClick={() => handleSort(col)}>
      {label}
      <span className="sort-arrow">{sort === col ? '↓' : ''}</span>
    </th>
  )

  if (loading && items.length === 0) {
    return <div className="empty"><div className="spinner" /></div>
  }

  return (
    <div className="file-list">
      <table className="file-table">
        <thead>
          <tr>
            <th style={{ width: '100%' }} onClick={() => handleSort('name')}>
              Name {sort === 'name' ? '↓' : ''}
            </th>
            {th('size', 'Size')}
            <th style={{ width: 80 }}></th>
            {th('mtime', 'Modified')}
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map(item => (
            <tr key={item.path}>
              <td>
                <div
                  className="file-name"
                  onClick={() => item.is_dir ? onNavigate(item.path, item.name) : null}
                  title={item.path}
                >
                  <span className="file-icon">{fileIcon(item.name, item.is_dir)}</span>
                  <span className="file-name-text truncate">{item.name}</span>
                  {item.is_dir && <span style={{ color: 'var(--text-mute)', fontSize: 11, marginLeft: 4 }}>
                    {item.cnt?.toLocaleString()} files
                  </span>}
                </div>
              </td>
              <td className="file-size">{fmtBytes(item.size)}</td>
              <td className="size-bar-cell">
                <div className="size-bar-wrap">
                  <div
                    className="size-bar-fill"
                    style={{ width: `${Math.round((item.size / maxSize) * 100)}%` }}
                  />
                </div>
              </td>
              <td className="file-mtime">{fmtDate(item.mtime)}</td>
              <td>
                <div className="file-actions">
                  <button
                    className="btn-icon btn-sm"
                    title="Move to Trash"
                    onClick={() => setConfirm({ path: item.path, action: 'trash' })}
                  >🗑</button>
                  <button
                    className="btn-icon btn-sm"
                    title="Delete permanently"
                    onClick={() => setConfirm({ path: item.path, action: 'delete' })}
                    style={{ color: 'var(--danger)' }}
                  >✕</button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {total > items.length && (
        <div style={{ padding: '10px 14px', color: 'var(--text-dim)', fontSize: 12 }}>
          Showing {items.length} of {total.toLocaleString()} items
        </div>
      )}
      {confirm && (
        <ConfirmDialog
          path={confirm.path}
          action={confirm.action}
          onConfirm={doAction}
          onCancel={() => setConfirm(null)}
        />
      )}
    </div>
  )
}
