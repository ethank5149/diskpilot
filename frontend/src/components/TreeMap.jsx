import { useEffect, useRef, useState } from 'react'
import * as d3 from 'd3'
import { fmtBytes, mountColor } from '../utils.js'

// Add synthetic "[files]" remainder leaf so directory sizes are accurate
function withRemainders(node) {
  if (!node.children?.length) return node
  const childSum = node.children.reduce((s, c) => s + (c.size ?? 0), 0)
  const rem = node.size - childSum
  const children = node.children.map(withRemainders)
  if (rem > 1024 * 512) {
    children.push({ name: '[other files]', path: node.path + '/__rem__', size: rem, is_dir: false, _rem: true })
  }
  return { ...node, children }
}

function getTopColor(d) {
  let n = d
  while (n.depth > 1 && n.parent) n = n.parent
  return n.depth === 1 ? mountColor(n.data.name) : '#4a5568'
}

export default function TreeMap({ data, onNavigate }) {
  const wrapRef = useRef(null)
  const svgRef  = useRef(null)
  const [tooltip, setTooltip] = useState(null)

  useEffect(() => {
    if (!data || !svgRef.current || !wrapRef.current) return
    const { width, height } = wrapRef.current.getBoundingClientRect()
    if (width < 10 || height < 10) return

    const svg = d3.select(svgRef.current)
      .attr('width', width).attr('height', height)
    svg.selectAll('*').remove()

    const prepared = withRemainders(data)
    const root = d3.hierarchy(prepared)
      .sum(d => (!d.children?.length) ? Math.max(0, d.size ?? 0) : 0)
      .sort((a, b) => b.value - a.value)

    d3.treemap()
      .size([width, height])
      .paddingOuter(4)
      .paddingTop(18)
      .paddingInner(2)
      .round(true)(root)

    // ── directory header rects ──
    const dirs = svg.selectAll('g.dp-dir')
      .data(root.descendants().filter(d => d.depth > 0 && d.children?.length))
      .join('g').attr('class', 'dp-dir')
      .attr('transform', d => `translate(${d.x0},${d.y0})`)

    dirs.append('rect')
      .attr('width', d => Math.max(0, d.x1 - d.x0))
      .attr('height', d => Math.min(18, Math.max(0, d.y1 - d.y0)))
      .attr('fill', d => getTopColor(d))
      .attr('fill-opacity', 0.25)
      .attr('rx', 2)
      .style('cursor', 'pointer')
      .on('click', (_, d) => onNavigate(d.data.path, d.data.name))

    dirs.append('text')
      .attr('x', 5).attr('y', 13)
      .attr('font-size', '11px').attr('font-weight', '600').attr('fill', '#e6edf3')
      .attr('pointer-events', 'none')
      .text(d => {
        const w = d.x1 - d.x0
        if (w < 30) return ''
        const label = `📁 ${d.data.name}`
        return label.slice(0, Math.floor(w / 7))
      })

    // ── leaf rects ──
    const leaves = svg.selectAll('g.dp-leaf')
      .data(root.leaves())
      .join('g').attr('class', 'dp-leaf')
      .attr('transform', d => `translate(${d.x0},${d.y0})`)

    leaves.append('rect')
      .attr('width', d => Math.max(0, d.x1 - d.x0))
      .attr('height', d => Math.max(0, d.y1 - d.y0))
      .attr('fill', d => d.data._rem ? '#2d333b' : getTopColor(d))
      .attr('fill-opacity', d => d.data._rem ? 0.4 : 0.75)
      .attr('rx', 2)
      .style('cursor', d => (d.data.is_dir && !d.data._rem) ? 'pointer' : 'default')
      .on('click', (_, d) => {
        if (d.data.is_dir && !d.data._rem) onNavigate(d.data.path, d.data.name)
      })
      .on('mousemove', (e, d) => setTooltip({ x: e.clientX, y: e.clientY, d: d.data }))
      .on('mouseleave', () => setTooltip(null))

    leaves.append('text')
      .attr('x', 4).attr('y', 14)
      .attr('font-size', '11px').attr('fill', '#e6edf3').attr('pointer-events', 'none')
      .text(d => {
        const w = d.x1 - d.x0, h = d.y1 - d.y0
        if (w < 32 || h < 16) return ''
        return d.data.name.slice(0, Math.floor(w / 7))
      })

    leaves.append('text')
      .attr('x', 4).attr('y', 27)
      .attr('font-size', '10px').attr('fill', '#8b949e').attr('pointer-events', 'none')
      .text(d => {
        const w = d.x1 - d.x0, h = d.y1 - d.y0
        if (w < 50 || h < 32) return ''
        return fmtBytes(d.data.size)
      })

  }, [data, onNavigate])

  return (
    <div ref={wrapRef} className="treemap-wrap">
      <svg ref={svgRef} style={{ display: 'block', width: '100%', height: '100%' }} />
      {tooltip && (
        <div className="treemap-tooltip" style={{ left: tooltip.x + 14, top: tooltip.y - 14 }}>
          <div className="tt-name">{tooltip.d._rem ? '[other files]' : tooltip.d.name}</div>
          <div className="tt-size">{fmtBytes(tooltip.d.size)}</div>
          {tooltip.d.is_dir && <div className="tt-count">{tooltip.d.cnt?.toLocaleString()} files</div>}
        </div>
      )}
    </div>
  )
}
