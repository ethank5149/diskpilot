import { useEffect, useState, useRef } from 'react'
import { api } from '../api.js'
import { fmtBytes } from '../utils.js'

export default function ScanModal({ onClose }) {
  const [scan, setScan] = useState({ status: 'idle' })
  const pollRef = useRef(null)

  useEffect(() => {
    const poll = setInterval(async () => {
      try {
        const s = await api.scanStatus()
        setScan(s)
        if (s.status === 'complete' || s.status === 'aborted') {
          clearInterval(poll)
          setTimeout(onClose, 500)
        }
      } catch (_) {}
    }, 400)
    pollRef.current = poll
    return () => clearInterval(pollRef.current)
  }, [onClose])

  const abortScan = async () => {
    try { await api.scanAbort() }
    catch (_) {}
  }

  const isScanning = scan.status === 'scanning' || scan.status === 'aborting'
  const pct = scan.dirs > 0 ? Math.min(99, Math.round((scan.dirs / (scan.dirs + 10)) * 100)) : 0

  return (
    <div className="overlay">
      <div className="scan-modal">
        <div className="scan-modal-header">
          <span className="scan-modal-title">
            {isScanning ? '🔍 Scanning Storage' : 'Scan Complete'}
          </span>
          {!isScanning && <button className="btn-icon" onClick={onClose}>✕</button>}
        </div>

        <div className="scan-modal-body">
          {/* Large current path indicator */}
          {isScanning && (
            <div className="scan-current-path" title={scan.current}>
              <span className="scan-current-label">Scanning:</span>
              <span className="scan-current-value mono">
                {scan.current?.split('/').slice(-2).join('/') || '...'}
              </span>
            </div>
          )}

          {/* Stats grid */}
          <div className="scan-stats">
            <div className="scan-stat">
              <span className="scan-stat-val">{scan.dirs?.toLocaleString() || 0}</span>
              <span className="scan-stat-label">Directories</span>
            </div>
            <div className="scan-stat">
              <span className="scan-stat-val">{scan.files?.toLocaleString() || 0}</span>
              <span className="scan-stat-label">Files</span>
            </div>
            <div className="scan-stat">
              <span className="scan-stat-val">{fmtBytes(scan.bytes || 0)}</span>
              <span className="scan-stat-label">Scanned</span>
            </div>
            <div className="scan-stat">
              <span className="scan-stat-val">{scan.aggregated?.toLocaleString() || 0}</span>
              <span className="scan-stat-label">Aggregated</span>
            </div>
          </div>

          {/* Progress bar */}
          {isScanning && (
            <div className="scan-progress-wrap">
              <div className="scan-progress-bar">
                <div className="scan-progress-fill" style={{ width: pct + '%' }} />
              </div>
              <div className="scan-progress-meta">
                <span className="spinner" style={{ width: 12, height: 12, borderWidth: 2 }} />
                <span className="mono" style={{ fontSize: 11 }}>
                  {scan.status === 'aborting' ? 'Aborting…' : `Elapsed: ${scan.elapsed?.toFixed(1) || 0}s`}
                </span>
              </div>
            </div>
          )}

          {/* Completion message */}
          {scan.status === 'complete' && (
            <div className="scan-complete">
              <span className="scan-complete-icon">✓</span>
              <span>Scan completed in {scan.elapsed?.toFixed(1)}s</span>
            </div>
          )}

          {/* Aborted message */}
          {scan.status === 'aborted' && (
            <div className="scan-aborted">
              <span className="scan-aborted-icon">⏹</span>
              <span>Scan was aborted</span>
            </div>
          )}
        </div>

        {isScanning && (
          <div className="scan-modal-footer">
            <button className="btn-danger" onClick={abortScan}>
              {scan.status === 'aborting' ? 'Aborting…' : '⏹ Abort Scan'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
