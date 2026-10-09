// Overview page: greeting + stat cards + awaiting approvals + targets table.
import { useEffect, useState } from 'react'
import { listActiveSessions } from '../api.js'
import { StatusPill } from '../components/ui.jsx'

function greeting() {
  const h = new Date().getHours()
  if (h < 12) return 'Good morning'
  if (h < 18) return 'Good afternoon'
  return 'Good evening'
}

function StatCard({ label, value, sub, accent }) {
  return (
    <div className={`rounded-xl border border-[#E9E1D9] bg-white p-5 shadow-[0_1px_3px_rgba(70,70,70,0.06)] ${accent ? 'border-t-2 !border-t-[#A35E47]' : ''}`}>
      <div className="flex items-center justify-between">
        <p className="text-[11px] font-bold tracking-[0.12em] text-[#9C9A9A]">{label}</p>
      </div>
      <p className="mt-3 font-mono text-[32px] font-medium leading-none text-[#1C1A18]">{value}</p>
      <p className="mt-3 text-[12px] text-[#9C9A9A]">{sub}</p>
    </div>
  )
}

function statusFor(t) {
  if (t.status === 'running') return 'Running'
  if (t.status === 'done') return 'Ready'
  if (t.status === 'queued') return 'Ready'
  return t.status || 'Ready'
}

function automationLabel(t) {
  if (t.automation_level === 'standard') return 'Full auto'
  if (t.automation_level === 'aggressive') return 'Full auto'
  if (t.automation_level === 'read-only') return 'Manual'
  return t.automation_level || '—'
}

export default function Dashboard({ targets, stats, onOpen, onNew, onEdit, onDelete, setError, userName }) {
  const [query, setQuery] = useState('')
  const [openMenu, setOpenMenu] = useState(null)
  const [awaiting, setAwaiting] = useState([])

  // Sessions paused for an operator decision, across all targets.
  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const all = await listActiveSessions()
        if (!cancelled) setAwaiting(all.filter((s) => s.has_pending))
      } catch {
        /* keep last known */
      }
    }
    poll()
    const timer = setInterval(poll, 10000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [])

  async function remove(id, domain) {
    setOpenMenu(null)
    if (!window.confirm(`Delete target ${domain} and ALL its runs, scans, findings and reports?`)) return
    try {
      await onDelete(id)
    } catch (err) {
      setError(err.message)
    }
  }

  const q = query.trim().toLowerCase()
  const rows = q ? targets.filter((t) => (t.domain || '').toLowerCase().includes(q)) : targets

  const first = userName ? userName.split(' ')[0] : 'there'

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-bold tracking-[0.16em] text-[#9C9A9A]">WORKSPACE / OVERVIEW</p>
          <h1 className="mt-2 text-[30px] font-medium tracking-tight text-[#1C1A18]">{greeting()}, {first}.</h1>
          <p className="mt-1 text-sm text-[#77716C]">Your agents found {stats ? stats.reports : 0} new leads since yesterday.</p>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className="rounded-lg bg-[#A35E47] px-4 py-2.5 text-sm font-semibold text-white hover:bg-[#8E513D]" onClick={onNew}>
            <span className="mr-1.5 text-base leading-none">+</span> Add target
          </button>
        </div>
      </div>

      {stats && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard label="ACTIVE TARGETS" value={String(stats.targets).padStart(2, '0')} sub={`Across ${stats.targets} program${stats.targets === 1 ? '' : 's'}`} />
          <StatCard label="ACTIVE RUNS" value={String(stats.agent_sessions).padStart(2, '0')} sub={`${awaiting.length} awaiting approval`} accent />
          <StatCard label="CONFIRMED" value={String(stats.findings)} sub="+ this week" />
          <StatCard label="OPEN LEADS" value={String(stats.reports)} sub="High confidence" />
        </div>
      )}

      {awaiting.length > 0 && (
        <div className="overflow-hidden rounded-xl border border-[#D9A08F]/60 bg-white shadow-[0_1px_3px_rgba(70,70,70,0.06)]">
          <div className="border-b border-[#F0EAE4] px-5 py-4">
            <h2 className="flex items-center gap-2 text-[15px] font-semibold text-[#1C1A18]">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-[#A35E47]" />
              Awaiting approval ({awaiting.length})
            </h2>
            <p className="mt-0.5 text-[12px] text-[#9C9A9A]">Sessions paused for your decision — approve to keep them moving</p>
          </div>
          <ul className="divide-y divide-[#F3EEE9]">
            {awaiting.map((s) => (
              <li key={s.run_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3">
                <button type="button" className="font-mono text-[13px] font-semibold text-[#1C1A18] hover:text-[#A35E47] hover:underline" onClick={() => onOpen(s.target_id)}>
                  {s.domain}
                </button>
                <span className="text-[12px] text-[#77716C]">
                  session #{s.run_id} · {s.mode === 'auto' ? 'fully auto' : 'semi-auto'}
                  {s.pending_kind === 'auth'
                    ? <> · login needed{s.pending_host ? <> on <span className="font-mono">{s.pending_host}</span></> : null}</>
                    : <> · wants to run <span className="rounded bg-[#F5E6DE] px-1.5 py-0.5 font-mono text-[11px] font-semibold text-[#8C4A33]">{s.pending_tool || 'a tool'}</span></>}
                </span>
                <button type="button" className="ml-auto text-[12px] font-semibold text-[#A35E47] hover:underline" onClick={() => onOpen(s.target_id)}>
                  Review →
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-[#E9E1D9] bg-white shadow-[0_1px_3px_rgba(70,70,70,0.06)]">
        <div className="flex flex-wrap items-center justify-between gap-3 p-5">
          <div>
            <h2 className="text-[15px] font-semibold text-[#1C1A18]">Targets</h2>
            <p className="mt-0.5 text-[12px] text-[#9C9A9A]">In-scope assets and automation status</p>
          </div>
          <div className="relative">
            <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[#9C9A9A]">⌕</span>
            <input
              value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search targets"
              className="w-64 rounded-lg border border-[#E2DAD2] bg-white py-2 pl-9 pr-3 text-[13px] focus:border-[#A35E47] focus:outline-none"
            />
          </div>
        </div>
        {rows.length === 0 && <p className="px-5 pb-5 text-sm text-[#9C9A9A]">No targets yet — add one to get started.</p>}
        {rows.length > 0 && (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-y border-[#F0EAE4] bg-[#FAF7F4] text-[11px] tracking-[0.08em] text-[#9C9A9A]">
                <th className="px-5 py-3 font-semibold">TARGET</th>
                <th className="px-4 py-3 font-semibold">STATUS</th>
                <th className="px-4 py-3 font-semibold">FINDINGS</th>
                <th className="px-4 py-3 font-semibold">AUTOMATION</th>
                <th className="px-4 py-3 font-semibold">UPDATED</th>
                <th className="w-12 px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr key={t.id} className="border-b border-[#F3EEE9] last:border-0 hover:bg-[#FCFAF8]">
                  <td className="px-5 py-4">
                    <div className="flex items-center gap-3">
                      <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-[#F5E6DE]">
                        <svg width="15" height="15" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5.5" stroke="#A35E47" strokeWidth="1.4" /><circle cx="8" cy="8" r="1.8" stroke="#A35E47" strokeWidth="1.4" /></svg>
                      </span>
                      <span>
                        <button type="button" className="block font-mono text-[13px] font-semibold text-[#1C1A18] hover:text-[#A35E47] hover:underline" onClick={() => onOpen(t.id)}>
                          {t.domain}
                        </button>
                        <span className="block font-mono text-[11px] text-[#9C9A9A]">*.{t.domain}</span>
                      </span>
                    </div>
                  </td>
                  <td className="px-4 py-4"><StatusPill status={statusFor(t)} /></td>
                  <td className="px-4 py-4 font-mono text-[13px] font-semibold">{t.findings_count}</td>
                  <td className="px-4 py-4 text-[13px] text-[#464646]">{automationLabel(t)}</td>
                  <td className="px-4 py-4 text-[12px] text-[#9C9A9A]">
                    {t.status === 'running' ? '2m ago' : new Date(t.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
                  </td>
                  <td className="relative px-4 py-4 text-right">
                    <button type="button" className="rounded-md px-2 py-1 text-base tracking-widest text-[#464646] hover:bg-[#F3EEE9]" onClick={() => setOpenMenu(openMenu === t.id ? null : t.id)}>•••</button>
                    {openMenu === t.id && (
                      <div className="absolute right-2 top-10 z-20 w-36 overflow-hidden rounded-lg border border-[#E9E1D9] bg-white shadow-xl">
                        <button type="button" className="block w-full px-3 py-2 text-left text-[13px] hover:bg-[#FAF7F4]" onClick={() => { setOpenMenu(null); onOpen(t.id) }}>Open</button>
                        <button type="button" className="block w-full px-3 py-2 text-left text-[13px] hover:bg-[#FAF7F4]" onClick={() => { setOpenMenu(null); onEdit(t) }}>Edit</button>
                        <button type="button" className="block w-full px-3 py-2 text-left text-[13px] text-[#A33A2E] hover:bg-[#FDF0EE]" onClick={() => remove(t.id, t.domain)}>Delete</button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
