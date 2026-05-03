const BASE = ''

async function req(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } }
  if (body) opts.body = JSON.stringify(body)
  const r = await fetch(BASE + path, opts)
  if (!r.ok) {
    const err = await r.json().catch(() => ({}))
    throw new Error(err.detail || `HTTP ${r.status}`)
  }
  return r.json()
}

export const api = {
  scanStart:    ()         => req('POST', '/api/scan/start'),
  scanStatus:   ()         => req('GET',  '/api/scan/status'),
  getTree:      (path, d)  => req('GET',  `/api/tree?path=${encodeURIComponent(path)}&depth=${d ?? 3}`),
  ls:           (path, sort, offset, limit) =>
    req('GET', `/api/ls?path=${encodeURIComponent(path)}&sort=${sort ?? 'size'}&offset=${offset ?? 0}&limit=${limit ?? 200}`),
  biggest:      (root)     => req('GET', `/api/biggest?root=${encodeURIComponent(root ?? '')}&limit=200`),
  skipped:      ()         => req('GET',  '/api/skipped'),
  trashMove:    (path)     => req('POST', '/api/trash/move',    { path }),
  trashRestore: (path)     => req('POST', '/api/trash/restore', { path }),
  trashEmpty:   ()         => req('POST', '/api/trash/empty'),
  trashList:    ()         => req('GET',  '/api/trash/list'),
  deletePerm:   (path)     => req('POST', '/api/delete',        { path }),
  dupsStart:    ()         => req('POST', '/api/dups/start'),
  dupsStatus:   ()         => req('GET',  '/api/dups/status'),
  dupsResults:  ()         => req('GET',  '/api/dups/results'),
  mounts:       ()         => req('GET',  '/api/mounts'),
}
