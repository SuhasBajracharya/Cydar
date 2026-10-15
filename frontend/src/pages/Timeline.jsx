// Global timeline: runs, findings, and reports across ALL targets,
// newest first. Rows expand inline; each links back to its target.
// Props: onOpenTarget(id), setError(message).
import { useCallback, useEffect, useState } from 'react'
import {
  deleteFinding,
  deleteRun,
  listTimeline,
} from '../api.js'
import Markdown from '../components/Markdown.jsx'
import { Pagination, SevPill } from '../components/ui.jsx'
import { btnDanger } from '../lib/styles.js'
import { reportSeverity } from '../lib/report.js'

const FILTERS = [
  ['all', 'All'],
  ['runs', 'Runs'],
  ['findings', 'Findings'],
  ['reports', 'Reports'],
]

function fmtTime(iso) {
  const d = new Date(iso)
  return `${d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })} · ${d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}`
}

export default function Timeline({ onOpenTarget, setError }) {
  const [items, setItems] = useState([])
  const [filter, setFilter] = useState('all')
  const [openId, setOpenId] = useState(null)
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const PAGE_SIZE = 10

  const load = useCallback(async () => {
    try {
      const data = await listTimeline()
      const merged = [
        ...(data.runs || []).map((r) => ({ kind: 'run', at: r.created_at, r })),
        ...(data.findings || []).map((f) => ({ kind: 'finding', at: f.created_at, f })),
        ...(data.reports || []).map((rep) => ({ kind: 'report', at: rep.created_at, rep })),
      ].sort((a, b) => new Date(b.at) - new Date(a.at))
      setItems(merged)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [setError])

  useEffect(() => { load() }, [load])

  async function removeRun(targetId, runId) {
    if (!window.confirm(`Delete plan run #${runId}?`)) return
    try {
      await deleteRun(targetId, runId)
      setItems((prev) => prev.filter((it) => !(it.kind === 'run' && it.r.id === runId)))
    } catch (err) {
      setError(err.message)
    }
  }

  async function removeFinding(targetId, findingId) {
    if (!window.confirm('Delete this finding?')) return
    try {
      await deleteFinding(targetId, findingId)
      setItems((prev) => prev.filter((it) => !(it.kind === 'finding' && it.f.id === findingId)))
    } catch (err) {
      setError(err.message)
    }
  }

  const counts = {
    runs: items.filter((it) => it.kind === 'run').length,
    findings: items.filter((it) => it.kind === 'finding').length,
    reports: items.filter((it) => it.kind === 'report').length,
  }
  const shown = items.filter((it) => filter === 'all' || it.kind === filter.slice(0, -1))
  const totalPages = Math.ceil(shown.length / PAGE_SIZE)
  const pagedItems = shown.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-bold tracking-[0.16em] text-[#9C9A9A]">WORKSPACE / TIMELINE</p>
          <h1 className="mt-2 text-[30px] font-medium tracking-tight text-[#1C1A18]">Timeline.</h1>
          <p className="mt-1 text-sm text-[#77716C]">Every run, finding, and report across all targets.</p>
        </div>
        <button type="button" className="rounded-lg border border-[#E2DAD2] bg-white px-4 py-2.5 text-sm font-medium text-[#464646] hover:bg-[#F7F3EF]" onClick={() => { setLoading(true); load() }}>
          Refresh
        </button>
      </div>

      <div className="overflow-hidden rounded-xl border border-[#E9E1D9] bg-white shadow-[0_1px_3px_rgba(70,70,70,0.06)]">
        <div className="flex flex-wrap items-center gap-1.5 border-b border-[#F0EAE4] px-5 py-3">
          {FILTERS.map(([id, label]) => {
            const n = id === 'all' ? items.length : counts[id]
            const active = filter === id
            return (
              <button
                key={id} type="button" onClick={() => { setFilter(id); setPage(1) }}
                className={`rounded-full px-3 py-1 text-xs font-medium ${active ? 'bg-black text-white' : 'border border-[#E2DAD2] bg-white text-[#464646] hover:bg-[#F7F3EF]'}`}
              >
                {label} ({n})
              </button>
            )
          })}
        </div>
        {loading && <p className="px-5 py-8 text-center text-sm text-[#9C9A9A]">Loading…</p>}
        {!loading && shown.length === 0 && (
          <p className="px-5 py-8 text-center text-sm text-[#9C9A9A]">
            {items.length === 0 ? 'Nothing yet — run the agent on any target.' : 'Nothing matches this filter.'}
          </p>
        )}
        <ol className="divide-y divide-[#F3EEE9] px-5">
          {pagedItems.map((item) => {
            const rowId = item.kind === 'run' ? `run-${item.r.id}` : item.kind === 'finding' ? `finding-${item.f.id}` : `report-${item.rep.id}`
            const open = openId === rowId
            const toggle = () => setOpenId(open ? null : rowId)
            return (
              <li key={rowId} className="flex gap-3.5 py-4">
                <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full ${item.kind === 'finding' ? 'bg-[#F5E6DE] text-[#A35E47]' : item.kind === 'report' ? 'bg-[#E9EFE7] text-[#3F5A3C]' : 'bg-[#F3EEE9] text-[#464646]'}`}>
                  {item.kind === 'finding' ? (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6l7-3z" stroke="currentColor" strokeWidth="1.8" /></svg>
                  ) : item.kind === 'report' ? (
                    <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M4 1.5h6l3 3V14.5H4z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" /><path d="M10 1.5v3h3" stroke="currentColor" strokeWidth="1.4" /></svg>
                  ) : (
                    <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M4 2.5v11l8-5.5-8-5.5z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" /></svg>
                  )}
                </span>
                <div className="min-w-0 flex-1">
                  <button
                    type="button"
                    className="font-mono text-[11px] font-semibold text-[#A35E47] hover:underline"
                    onClick={() => onOpenTarget(item.kind === 'run' ? item.r.target_id : item.kind === 'finding' ? item.f.target_id : item.rep.target_id)}
                  >
                    {item.kind === 'run' ? item.r.domain : item.kind === 'finding' ? item.f.domain : item.rep.domain} →
                  </button>
                  {item.kind === 'finding' && (
                    <>
                      <div className="mt-0.5 flex items-start justify-between gap-2">
                        <button type="button" className="text-left text-[13px] font-semibold hover:underline" onClick={toggle}>
                          <SevPill severity={item.f.severity} /> <span className="ml-1">{item.f.title}</span>
                          <span className="ml-1.5 text-[#9C9A9A]">{open ? '▴' : '▾'}</span>
                        </button>
                        <button type="button" className={`${btnDanger} shrink-0`} onClick={() => removeFinding(item.f.target_id, item.f.id)}>Delete</button>
                      </div>
                      {open && item.f.detail && <p className="mt-1 whitespace-pre-wrap text-[12px] leading-relaxed text-[#77716C]">{item.f.detail}</p>}
                    </>
                  )}
                  {item.kind === 'run' && (
                    <>
                      <div className="mt-0.5 flex items-start justify-between gap-2">
                        <button type="button" className="text-left text-[13px] font-semibold text-[#1C1A18] hover:underline" onClick={toggle}>
                          {item.r.kind === 'agent' ? `Agent session #${item.r.id} · ${item.r.status}` : `Plan run #${item.r.id}`}
                          <span className="ml-1.5 text-[#9C9A9A]">{open ? '▴' : '▾'}</span>
                        </button>
                        <button type="button" className={`${btnDanger} shrink-0`} onClick={() => removeRun(item.r.target_id, item.r.id)}>Delete</button>
                      </div>
                      <p className="mt-0.5 text-[12px] text-[#9C9A9A]">
                        {item.r.kind === 'agent'
                          ? `${item.r.autonomy_mode === 'auto' ? 'Fully auto' : 'Semi-auto'} · ${item.r.provider || 'deepseek'}/${item.r.model || 'default'} · ${item.r.iterations} steps`
                          : (item.r.output || '').slice(0, 120)}
                      </p>
                      {open && item.r.status === 'error' && <p className="mt-1 text-[12px] text-[#96382B]">Failed: {item.r.error}</p>}
                      {open && item.r.status !== 'error' && item.r.output && (
                        <pre className="nice-scroll mt-1.5 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-[#F0EAE4] bg-[#FAF7F4] p-2.5 font-mono text-[11px] leading-relaxed text-[#464646]">{item.r.output}</pre>
                      )}
                    </>
                  )}
                  {item.kind === 'report' && (
                    <>
                      <div className="mt-0.5 flex items-start justify-between gap-2">
                        <button type="button" className="text-left text-[13px] font-semibold text-[#1C1A18] hover:underline" onClick={toggle}>
                          {item.rep.title}
                          <span className="ml-1.5 text-[#9C9A9A]">{open ? '▴' : '▾'}</span>
                        </button>
                      </div>
                      <p className="mt-0.5 text-[12px] text-[#9C9A9A]">
                        {(() => { const sev = reportSeverity(item.rep.body); return sev ? <><SevPill severity={sev} /> · </> : null })()}
                        {item.rep.findings_count} confirmed · {item.rep.leads_count || 0} leads
                      </p>
                      {open && (
                        <div className="mt-1.5">
                          <Markdown text={item.rep.body} />
                        </div>
                      )}
                    </>
                  )}
                </div>
                <span className="shrink-0 font-mono text-[10px] text-[#B9B5B1]">{fmtTime(item.at)}</span>
              </li>
            )
          })}
        </ol>
        <Pagination
          page={page}
          totalPages={totalPages}
          totalItems={shown.length}
          pageSize={PAGE_SIZE}
          onPageChange={setPage}
        />
      </div>
    </div>
  )
}
