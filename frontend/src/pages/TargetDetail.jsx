// Target detail page: header + Agent / Reports / Timeline tabs.
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  createFinding,
  deleteFinding,
  deleteRun,
  getTarget,
  listActiveSessions,
  listFindings,
  listProviders,
  listReports,
  listRuns,
  updateTarget,
} from '../api.js'
import { CopyBtn, Modal, SevPill, StatusPill } from '../components/ui.jsx'
import Markdown from '../components/Markdown.jsx'
import { SEV_BORDER, reportSeverity } from '../lib/report.js'
import { btnDanger, btnSecondary, card, input } from '../lib/styles.js'
import TargetForm from '../components/TargetForm.jsx'
import AgentPanel from '../components/AgentPanel.jsx'

const TABS = [
  ['agent', 'Agent'],
  ['reports', 'Reports'],
  ['timeline', 'Timeline'],
]

export default function TargetDetail({ targetId, onEdited, onOpenTarget, setError }) {
  const [target, setTarget] = useState(null)
  const [tab, setTab] = useState('agent')
  const [runs, setRuns] = useState([])
  const [findings, setFindings] = useState([])
  const [reports, setReports] = useState([])
  const [activeSessions, setActiveSessions] = useState([])
  const [openReport, setOpenReport] = useState(null)
  const [repFilter, setRepFilter] = useState('all') // all|confirmed|leads
  const [tlFilter, setTlFilter] = useState('all') // all|runs|findings|reports
  const [openTl, setOpenTl] = useState(null) // timeline row id or null
  const [showEdit, setShowEdit] = useState(false)
  const [findingTitle, setFindingTitle] = useState('')
  const [findingSeverity, setFindingSeverity] = useState('')
  const [findingDetail, setFindingDetail] = useState('')
  const [providers, setProviders] = useState([])
  const [selProvider, setSelProvider] = useState('deepseek')
  const [selModel, setSelModel] = useState('')
  const pickerInit = useRef(false)

  const load = useCallback(async () => {
    try {
      const [t, r, f, p, rep] = await Promise.all([getTarget(targetId), listRuns(targetId), listFindings(targetId), listProviders(), listReports(targetId)])
      setTarget(t); setRuns(r); setFindings(f); setProviders(p); setReports(rep)
      if (!pickerInit.current) {
        pickerInit.current = true
        const saved = p.find((x) => x.id === (t.provider || 'deepseek'))
        const initial = saved?.configured ? saved.id : (p.find((x) => x.configured)?.id || 'deepseek')
        setSelProvider(initial)
        setSelModel(initial === t.provider ? (t.model || '') : '')
      }
    } catch (err) {
      setError(err.message)
    }
  }, [targetId, setError])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const all = await listActiveSessions()
        if (!cancelled) setActiveSessions(all.filter((s) => s.target_id !== targetId))
      } catch { /* keep last known */ }
    }
    poll()
    const timer = setInterval(poll, 5000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [targetId])

  async function addFinding(e) {
    e.preventDefault()
    if (!findingTitle.trim()) { setError('Give the finding a title.'); return }
    try {
      const f = await createFinding(targetId, { title: findingTitle, severity: findingSeverity || 'info', detail: findingDetail })
      setFindings((prev) => [f, ...prev])
      setFindingTitle(''); setFindingDetail(''); setFindingSeverity('')
    } catch (err) {
      setError(err.message)
    }
  }

  async function removeRun(runId) {
    if (!window.confirm(`Delete plan run #${runId}?`)) return
    try {
      await deleteRun(targetId, runId)
      setRuns((prev) => prev.filter((r) => r.id !== runId))
    } catch (err) {
      setError(err.message)
    }
  }

  async function removeFinding(findingId) {
    if (!window.confirm('Delete this finding?')) return
    try {
      await deleteFinding(targetId, findingId)
      setFindings((prev) => prev.filter((f) => f.id !== findingId))
    } catch (err) {
      setError(err.message)
    }
  }

  function exportAll() {
    const txt = reports.map((r) => `# ${r.title}\n\n${r.body}`).join('\n\n---\n\n')
    const blob = new Blob([txt], { type: 'text/markdown' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `reports-${target?.domain || targetId}.md`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  if (!target) return <p className="text-sm text-[#9C9A9A]">Loading…</p>

  const leadsCount = reports.reduce((n, r) => n + (r.leads_count || 0), 0)
  const confirmedCount = reports.reduce((n, r) => n + (r.findings_count || 0), 0) || findings.length

  const timelineItems = [
    ...runs.map((r) => ({ kind: 'run', at: new Date(r.created_at), r })),
    ...findings.map((f) => ({ kind: 'finding', at: new Date(f.created_at), f })),
    ...reports.map((rep) => ({ kind: 'report', at: new Date(rep.created_at), rep })),
  ].sort((a, b) => b.at - a.at)
  const tlCounts = {
    runs: runs.length,
    findings: findings.length,
    reports: reports.length,
  }
  const tlShown = timelineItems.filter((it) => tlFilter === 'all' || it.kind === tlFilter.slice(0, -1))
  const repShown = reports.filter((rep) => {
    if (repFilter === 'confirmed') return (rep.findings_count || 0) > 0
    if (repFilter === 'leads') return (rep.findings_count || 0) === 0
    return true
  })

  return (
    <div className="space-y-0">
      {/* header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-bold tracking-[0.16em] text-[#9C9A9A]">TARGETS <span className="mx-1.5">/</span> TARGET DETAIL</p>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-[#F5E6DE]">
              <svg width="17" height="17" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5.5" stroke="#A35E47" strokeWidth="1.4" /><circle cx="8" cy="8" r="1.8" stroke="#A35E47" strokeWidth="1.4" /></svg>
            </span>
            <h1 className="font-mono text-[24px] font-semibold tracking-tight text-[#1C1A18]">{target.domain}</h1>
            <StatusPill status={target.status === 'done' ? 'Ready' : target.status === 'queued' ? 'Ready' : 'Running'} />
          </div>
          <p className="mt-2 pl-[52px] text-[12px] text-[#9C9A9A]">
            Program <span className="font-semibold text-[#464646]">Acme Private BBP</span>
            <span className="mx-2">Scope <span className="font-mono font-semibold text-[#464646]">*.{target.domain}</span></span>
            {target.auth_configured
              ? <span className="mr-1.5 rounded-full border border-[#E2DAD2] bg-white px-2 py-0.5 font-medium text-[#464646]">Session auth</span>
              : <span className="mr-1.5 rounded-full border border-[#E2DAD2] bg-white px-2 py-0.5 font-medium text-[#9C9A9A]">No session</span>}
            <span className="rounded-full bg-[#E9EFE7] px-2 py-0.5 font-medium text-[#3F5A3C]">In scope</span>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className={`${btnSecondary} !rounded-lg`} onClick={() => setShowEdit(true)}>✎ Edit target</button>
        </div>
      </div>

      {/* tabs */}
      <div className="mt-5 flex gap-6 border-b border-[#E9E1D9]">
        {TABS.map(([id, label]) => (
          <button
            key={id} type="button" onClick={() => setTab(id)}
            className={`relative pb-3 text-[13px] font-medium ${tab === id ? 'text-[#A35E47]' : 'text-[#9C9A9A] hover:text-[#464646]'}`}
          >
            {label}
            {id === 'reports' && reports.length > 0 && (
              <span className="ml-1.5 rounded-full bg-[#F3EEE9] px-1.5 py-0.5 text-[10px] font-bold text-[#464646]">{reports.length}</span>
            )}
            {tab === id && <span className="absolute inset-x-0 -bottom-px h-0.5 bg-[#A35E47]" />}
          </button>
        ))}
      </div>

      {showEdit && (
        <Modal title={`Edit ${target.domain}`} onClose={() => setShowEdit(false)}>
          <TargetForm
            initial={target}
            submitText="Save changes"
            setError={setError}
            onSubmit={async (payload) => {
              const t = await updateTarget(targetId, payload)
              setTarget(t); setShowEdit(false); onEdited()
            }}
          />
        </Modal>
      )}

      <div className="pt-5">
        {tab === 'agent' && (
          <AgentPanel
            targetId={targetId}
            providers={providers} provider={selProvider} model={selModel}
            onProviderChange={(p, m) => { setSelProvider(p); setSelModel(m) }}
            runs={runs}
            activeElsewhere={activeSessions}
            onOpenTarget={onOpenTarget}
            setError={setError}
            reload={load}
            leadsCount={leadsCount}
            confirmedCount={confirmedCount}
          />
        )}

        {tab === 'reports' && (
          <div className="mx-auto max-w-[880px]">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <h2 className="text-[17px] font-semibold">Agent reports</h2>
                <p className="mt-0.5 text-[12px] text-[#9C9A9A]">Structured findings generated from completed runs</p>
              </div>
              {reports.length > 0 && (
                <button type="button" className={btnSecondary} onClick={exportAll}>⭳ Export all</button>
              )}
            </div>
            {reports.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {[['all', `All (${reports.length})`],
                  ['confirmed', `Confirmed (${reports.filter((r) => (r.findings_count || 0) > 0).length})`],
                  ['leads', `Leads only (${reports.filter((r) => (r.findings_count || 0) === 0).length})`],
                ].map(([id, label]) => (
                  <button
                    key={id} type="button" onClick={() => setRepFilter(id)}
                    className={`rounded-full px-3 py-1 text-xs font-medium ${repFilter === id ? 'bg-black text-white' : 'border border-[#E2DAD2] bg-white text-[#464646] hover:bg-[#F7F3EF]'}`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            )}
            {reports.length === 0 && (
              <p className="mt-4 rounded-xl border border-[#E9E1D9] bg-white p-6 text-center text-sm text-[#9C9A9A]">
                No reports yet — finish an autonomous session to get a bounty-ready report.
              </p>
            )}
            {reports.length > 0 && repShown.length === 0 && (
              <p className="mt-4 rounded-xl border border-[#E9E1D9] bg-white p-6 text-center text-sm text-[#9C9A9A]">
                No reports match this filter.
              </p>
            )}
            <ol className="mt-4 space-y-4">
              {repShown.map((rep, i) => {
                const sev = reportSeverity(rep.body) || ((rep.findings_count || 0) > 0 ? 'high' : 'info')
                return (
                  <li key={`report-${rep.id}`} className={`overflow-hidden rounded-xl border border-[#E9E1D9] border-l-4 bg-white shadow-[0_1px_3px_rgba(70,70,70,0.06)] ${SEV_BORDER[sev] || SEV_BORDER.info}`}>
                    <button
                      type="button" onClick={() => setOpenReport(openReport === rep.id ? null : rep.id)}
                      className="flex w-full items-center gap-4 p-4 text-left"
                    >
                      <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-[#E9E1D9] bg-[#FAF7F4] font-mono text-[11px] font-bold text-[#A35E47]">
                        R{i + 1}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <SevPill severity={sev} />
                          <span className="text-[11px] text-[#9C9A9A]">{new Date(rep.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, {new Date(rep.created_at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}</span>
                        </span>
                        <span className="mt-1 block truncate text-[14px] font-semibold text-[#1C1A18]">{rep.title}</span>
                        <span className="mt-0.5 block text-[12px] text-[#9C9A9A]">{rep.findings_count} confirmed · {rep.leads_count || 0} leads</span>
                      </span>
                      <span className="text-[#9C9A9A]">{openReport === rep.id ? '▴' : '▾'}</span>
                    </button>
                    {openReport === rep.id && (
                      <div className="border-t border-[#F0EAE4] px-5 py-4">
                        <div className="mb-3 flex justify-end">
                          <CopyBtn text={rep.body} label="⧉ Copy markdown" />
                        </div>
                        <Markdown text={rep.body} />
                      </div>
                    )}
                  </li>
                )
              })}
            </ol>
          </div>
        )}

        {tab === 'timeline' && (
          <div className="grid items-start gap-4 xl:grid-cols-[1fr_300px]">
            <div className="rounded-xl border border-[#E9E1D9] bg-white shadow-[0_1px_3px_rgba(70,70,70,0.06)]">
              <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
                <div>
                  <h2 className="text-[15px] font-semibold">Activity timeline</h2>
                  <p className="mt-0.5 text-[12px] text-[#9C9A9A]">Runs, findings, reports, and manual updates</p>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {[['all', `All (${timelineItems.length})`],
                    ['runs', `Runs (${tlCounts.runs})`],
                    ['findings', `Findings (${tlCounts.findings})`],
                    ['reports', `Reports (${tlCounts.reports})`],
                  ].map(([id, label]) => (
                    <button
                      key={id} type="button" onClick={() => setTlFilter(id)}
                      className={`rounded-full px-3 py-1 text-xs font-medium ${tlFilter === id ? 'bg-black text-white' : 'border border-[#E2DAD2] bg-white text-[#464646] hover:bg-[#F7F3EF]'}`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <ol className="divide-y divide-[#F3EEE9] px-5">
                {tlShown.length === 0 && <li className="py-6 text-sm text-[#9C9A9A]">{timelineItems.length === 0 ? 'Nothing yet — run the agent to generate activity.' : 'Nothing matches this filter.'}</li>}
                {tlShown.map((item) => {
                  const rowId = item.kind === 'run' ? `run-${item.r.id}` : item.kind === 'finding' ? `finding-${item.f.id}` : `report-${item.rep.id}`
                  const open = openTl === rowId
                  const toggle = () => setOpenTl(open ? null : rowId)
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
                        {item.kind === 'finding' && (
                          <>
                            <div className="flex items-start justify-between gap-2">
                              <button type="button" className="text-left text-[13px] font-semibold hover:underline" onClick={toggle}>
                                <SevPill severity={item.f.severity} /> <span className="ml-1">{item.f.title}</span>
                                <span className="ml-1.5 text-[#9C9A9A]">{open ? '▴' : '▾'}</span>
                              </button>
                              <button type="button" className={`${btnDanger} shrink-0`} onClick={() => removeFinding(item.f.id)}>Delete</button>
                            </div>
                            {open && item.f.detail && <p className="mt-1 whitespace-pre-wrap text-[12px] leading-relaxed text-[#77716C]">{item.f.detail}</p>}
                            {open && <p className="mt-1 text-[11px] text-[#9C9A9A]">Logged {item.at.toLocaleString()}</p>}
                          </>
                        )}
                        {item.kind === 'run' && (
                          <>
                            <div className="flex items-start justify-between gap-2">
                              <button type="button" className="text-left text-[13px] font-semibold text-[#1C1A18] hover:underline" onClick={toggle}>
                                {item.r.kind === 'agent' ? `Agent session #${item.r.id} · ${item.r.status}` : `Plan run #${item.r.id}`}
                                <span className="ml-1.5 text-[#9C9A9A]">{open ? '▴' : '▾'}</span>
                              </button>
                              <button type="button" className={`${btnDanger} shrink-0`} onClick={() => removeRun(item.r.id)}>Delete</button>
                            </div>
                            <p className="mt-0.5 text-[12px] text-[#9C9A9A]">
                              {item.r.kind === 'agent'
                                ? `${item.r.autonomy_mode === 'auto' ? 'Fully auto' : 'Semi-auto'} · ${item.r.provider || 'deepseek'}/${item.r.model || 'default'} · ${item.r.iterations} steps`
                                : item.r.output?.slice(0, 120)}
                            </p>
                            {open && item.r.status === 'error' && <p className="mt-1 text-[12px] text-[#96382B]">Failed: {item.r.error}</p>}
                            {open && item.r.status !== 'error' && item.r.output && (
                              <pre className="nice-scroll mt-1.5 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-[#F0EAE4] bg-[#FAF7F4] p-2.5 font-mono text-[11px] leading-relaxed text-[#464646]">{item.r.output}</pre>
                            )}
                          </>
                        )}
                        {item.kind === 'report' && (
                          <>
                            <div className="flex items-start justify-between gap-2">
                              <button type="button" className="text-left text-[13px] font-semibold text-[#1C1A18] hover:underline" onClick={toggle}>
                                {item.rep.title}
                                <span className="ml-1.5 text-[#9C9A9A]">{open ? '▴' : '▾'}</span>
                              </button>
                            </div>
                            <p className="mt-0.5 text-[12px] text-[#9C9A9A]">{item.rep.findings_count} confirmed · {item.rep.leads_count || 0} leads</p>
                            {open && (
                              <div className="mt-1.5">
                                <Markdown text={item.rep.body} />
                                <button
                                  type="button" className="mt-2 text-xs font-semibold text-[#A35E47] hover:underline"
                                  onClick={() => { setTab('reports'); setOpenReport(item.rep.id) }}
                                >
                                  Open in Reports →
                                </button>
                              </div>
                            )}
                          </>
                        )}
                      </div>
                      <span className="shrink-0 font-mono text-[10px] text-[#B9B5B1]">
                        {item.at.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })} · {item.at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}
                      </span>
                    </li>
                  )
                })}
              </ol>
            </div>

            <form onSubmit={addFinding} className={`${card} !p-5`}>
              <p className="text-[11px] font-bold tracking-[0.12em] text-[#9C9A9A]">MANUAL ENTRY</p>
              <h3 className="mt-3 text-[15px] font-semibold">Add finding</h3>
              <p className="text-[12px] text-[#9C9A9A]">Capture a lead outside an agent run.</p>
              <label className="mt-4 block text-[11px] font-bold tracking-wide text-[#464646]">Title</label>
              <input className={`${input} mt-1.5`} placeholder="Finding title" value={findingTitle} onChange={(e) => setFindingTitle(e.target.value)} />
              <label className="mt-3 block text-[11px] font-bold tracking-wide text-[#464646]">Severity</label>
              <select className={`${input} mt-1.5`} value={findingSeverity} onChange={(e) => setFindingSeverity(e.target.value)}>
                <option value="">Select severity</option>
                <option value="info">info</option><option value="low">low</option><option value="medium">medium</option>
                <option value="high">high</option><option value="critical">critical</option>
              </select>
              <label className="mt-3 block text-[11px] font-bold tracking-wide text-[#464646]">Evidence</label>
              <textarea className={`${input} mt-1.5`} rows={4} placeholder="Add context, steps, or evidence…" value={findingDetail} onChange={(e) => setFindingDetail(e.target.value)} />
              <button className="mt-4 w-full rounded-lg bg-[#A35E47] py-2.5 text-sm font-semibold text-white hover:bg-[#8E513D]" type="submit">
                Add finding →
              </button>
            </form>
          </div>
        )}
      </div>
    </div>
  )
}
