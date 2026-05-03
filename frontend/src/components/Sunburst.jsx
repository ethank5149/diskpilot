import { useEffect, useRef, useState } from 'react'
import * as d3 from 'd3'
import { fmtBytes, mountColor } from '../utils.js'

export default function Sunburst({ data, onNavigate }) {
  const wrapRef  = useRef(null)
  const svgRef   = useRef(null)
  const [center, setCenter] = useState(null)

  useEffect(() => {
    if (!data || !svgRef.current || !wrapRef.current) return
    const { width, height } = wrapRef.current.getBoundingClientRect()
    const R = Math.min(width, height) / 2 - 10
    if (R < 50) return

    const svg = d3.select(svgRef.current)
      .attr('viewBox', `${-width / 2} ${-height / 2} ${width} ${height}`)
      .attr('width', width).attr('height', height)
    svg.selectAll('*').remove()

    const root = d3.hierarchy(data)
      .sum(d => (!d.children?.length) ? Math.max(0, d.size ?? 0) : 0)
      .sort((a, b) => b.value - a.value)

    d3.partition().size([2 * Math.PI, R])(root)
    root.each(d => { d.current = { x0: d.x0, x1: d.x1, y0: d.y0, y1: d.y1 } })

    const arc = d3.arc()
      .startAngle(d => d.x0).endAngle(d => d.x1)
      .innerRadius(d => d.y0).outerRadius(d => d.y1 - 2)
      .padAngle(0.005).padRadius(R / 2)

    const arcVisible = d =>
      d.y1 <= R && d.y0 >= 0 && d.x1 > d.x0

    function getColor(d) {
      let n = d; while (n.depth > 1 && n.parent) n = n.parent
      return n.depth === 1 ? mountColor(n.data.name) : '#4a5568'
    }

    const g = svg.append('g')

    const path = g.selectAll('path')
      .data(root.descendants().slice(1))
      .join('path')
      .attr('fill', d => { const c = d3.color(getColor(d)); if (c) c.opacity = 0.7 + 0.3 * (1 - d.depth / 6); return c ?? getColor(d) })
      .attr('fill-opacity', d => arcVisible(d.current) ? 0.8 : 0)
      .attr('pointer-events', d => arcVisible(d.current) ? 'auto' : 'none')
      .attr('d', d => arc(d.current))
      .style('cursor', d => d.data.is_dir ? 'pointer' : 'default')

    const label = g.selectAll('text')
      .data(root.descendants().slice(1))
      .join('text')
      .attr('pointer-events', 'none')
      .attr('text-anchor', 'middle')
      .attr('fill', '#e6edf3')
      .attr('font-size', '11px')
      .attr('fill-opacity', d => {
        const a = d.current
        return arcVisible(a) && (a.x1 - a.x0) > 0.08 ? 1 : 0
      })
      .attr('transform', d => {
        const a = d.current
        const x = ((a.x0 + a.x1) / 2 * 180 / Math.PI) - 90
        const y = (a.y0 + a.y1) / 2
        return `rotate(${x}) translate(${y}, 0) rotate(${x < 0 ? 180 : 0})`
      })
      .text(d => {
        const a = d.current
        const span = a.x1 - a.x0
        if (span < 0.1) return ''
        return d.data.name.slice(0, Math.floor(span * 18))
      })

    // Center circle — shows current node info
    const centerG = svg.append('g').attr('class', 'center-info')
    centerG.append('circle')
      .attr('r', root.children?.[0]?.y0 ?? R * 0.25)
      .attr('fill', 'var(--surface)')
      .attr('stroke', 'var(--border)')
      .attr('stroke-width', 1)
      .style('cursor', 'pointer')
      .on('click', () => {
        if (root.parent) onNavigate(root.parent.data.path || '__root__', root.parent.data.name || 'root')
      })

    setCenter({ name: data.name, size: data.size, cnt: data.cnt })

    // click to zoom
    function clicked(_, p) {
      if (!p.data.is_dir) return
      onNavigate(p.data.path, p.data.name)
    }

    path.on('click', clicked)

    // tooltip
    path.on('mousemove', function(e, d) {
      d3.select(this).attr('fill-opacity', 1)
      setCenter({ name: d.data.name, size: d.data.size, cnt: d.data.cnt, hover: true })
    }).on('mouseleave', function() {
      d3.select(this).attr('fill-opacity', 0.8)
      setCenter({ name: data.name, size: data.size, cnt: data.cnt })
    })

  }, [data, onNavigate])

  return (
    <div ref={wrapRef} className="sunburst-wrap">
      <svg ref={svgRef} style={{ display: 'block' }} />
      {center && (
        <div style={{
          position: 'absolute', top: '50%', left: '50%',
          transform: 'translate(-50%, -50%)',
          textAlign: 'center', pointerEvents: 'none',
          maxWidth: 90
        }}>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 2, wordBreak: 'break-word' }}>
            {center.name}
          </div>
          <div style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--mono)', color: 'var(--accent)' }}>
            {fmtBytes(center.size)}
          </div>
          {center.cnt != null && (
            <div style={{ fontSize: 10, color: 'var(--text-mute)' }}>
              {center.cnt.toLocaleString()} files
            </div>
          )}
        </div>
      )}
    </div>
  )
}
