export function fmtBytes(b) {
  if (b == null) return '—'
  if (b < 1024)           return b + ' B'
  if (b < 1024 ** 2)      return (b / 1024).toFixed(1) + ' KB'
  if (b < 1024 ** 3)      return (b / 1024 ** 2).toFixed(1) + ' MB'
  if (b < 1024 ** 4)      return (b / 1024 ** 3).toFixed(2) + ' GB'
  return (b / 1024 ** 4).toFixed(2) + ' TB'
}

export function fmtDate(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function fileIcon(name, isDir) {
  if (isDir) return '📁'
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  const map = {
    mp4:'🎬', mkv:'🎬', avi:'🎬', mov:'🎬', wmv:'🎬', m4v:'🎬', ts:'🎬', webm:'🎬',
    mp3:'🎵', flac:'🎵', aac:'🎵', ogg:'🎵', wav:'🎵',
    jpg:'🖼', jpeg:'🖼', png:'🖼', gif:'🖼', webp:'🖼', heic:'🖼', tiff:'🖼',
    zip:'📦', tar:'📦', gz:'📦', bz2:'📦', xz:'📦', rar:'📦', '7z':'📦',
    pdf:'📄', doc:'📄', docx:'📄', txt:'📄', md:'📄',
    iso:'💿', img:'💿',
    db:'🗃', sql:'🗃', sqlite:'🗃',
    py:'📝', js:'📝', ts:'📝', sh:'📝', yaml:'📝', yml:'📝', json:'📝',
  }
  return map[ext] ?? '📄'
}

// Mount-point colors (Tableau10 palette)
const PALETTE = [
  '#4e79a7','#f28e2b','#e15759','#76b7b2','#59a14f',
  '#edc948','#b07aa1','#ff9da7','#9c755f','#bab0ac'
]
const _colorMap = new Map()
export function mountColor(name) {
  if (!_colorMap.has(name)) _colorMap.set(name, PALETTE[_colorMap.size % PALETTE.length])
  return _colorMap.get(name)
}
