// Autonomous agent session panel: Agent control card + Live activity feed
// + Run progress rail. Live events over SSE; steps persist as AgentAction rows.
import { useEffect, useRef, useState } from 'react'
import {
  agentStream,
  approveAgent,
  listActions,
  startAgent,
  stopAgent,
} from '../api.js'
import { CopyBtn, Donut } from './ui.jsx'
import { btn, btnSecondary, input } from '../lib/styles.js'
import { argsText, downloadText, sessionLogText } from '../lib/session.js'
import FeedEvent from './FeedEvent.jsx'
import ModelPicker from './ModelPicker.jsx'

// Seconds ticking since a proposal started waiting (local helper, same file
// by convention — see Step in FeedEvent.jsx).
function WaitingTimer({ since }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  const start = typeof since === 'number' ? since : now
  const s = Math.max(0, Math.floor((now - start) / 1000))
  const mm = String(Math.floor(s / 60)).padStart(2, '0')
  const ss = String(s % 60).padStart(2, '0')
  return <span className="font-mono text-[10px] font-semibold text-[#A35E47]">waiting {mm}:{ss}</span>
}

// Args JSON with key/value tinting (no dependency). Falls back to raw text
// when the payload isn't JSON — never crashes on model output.
function JsonArgs({ value }) {
  const raw = argsText(value)
  if (!raw) return null
  let pretty = raw
  try {
    pretty = JSON.stringify(typeof value === 'string' ? JSON.parse(value) : value, null, 2)
  } catch {
    return <pre className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-[#464646]">{raw}</pre>
  }
  const chunks = pretty.split(/("(?:[^"\\]|\\.)*")(\s*:)?/g)
  const nodes = []
  for (let i = 0; i < chunks.length; i += 3) {
    const other = chunks[i]
    const str = chunks[i + 1]
    const colon = chunks[i + 2]
    if (other) nodes.push(<span key={`o${i}`} className="text-[#77716C]">{other}</span>)
    if (str !== undefined) {
      nodes.push(
        <span key={`s${i}`} className={colon ? 'font-semibold text-[#8C4A33]' : 'text-[#3F5A3C]'}>
          {str}
        </span>,
      )
    }
    if (colon) nodes.push(<span key={`c${i}`} className="text-[#77716C]">{colon}</span>)
  }
  return (
    <pre className="nice-scroll max-h-44 overflow-auto whitespace-pre-wrap rounded-lg border border-[#E9E1D9] bg-[#FAF7F4] p-2.5 font-mono text-[11px] leading-relaxed">{nodes}</pre>
  )
}

export default function AgentPanel({
  targetId, providers, provider, model, onProviderChange,
  runs, activeElsewhere, onOpenTarget, setError, reload,
  leadsCount = 0, confirmedCount = 0,
}) {
  const [maxIter, setMaxIter] = useState(50)
  const [runId, setRunId] = useState(null)
  const [live, setLive] = useState(false)
  const [conn, setConn] = useState('idle') // idle|connecting|live|ended
  const [feed, setFeed] = useState([])
  const [pending, setPending] = useState(null)
  const [hint, setHint] = useState('')
  const [deciding, setDeciding] = useState(false)
  const [openLog, setOpenLog] = useState(null)
  const [logs, setLogs] = useState({})
  const feedEnd = useRef(null)
  const streamCtl = useRef(null) // AbortController for the open SSE stream
  const attachedId = useRef(null) // run id with a live (or attempted) stream
  const settledIds = useRef(new Set()) // runs whose stream already terminated

  const agentRuns = runs.filter((r) => r.kind === 'agent')
  const running = agentRuns.find((r) => r.status === 'running') // runs are newest-first
  const current = agentRuns.find((r) => r.id === runId) || running || null

  useEffect(() => {
    feedEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [feed, pending])

  // Leaving the page must close the SSE stream (the worker keeps running
  // server-side). Reset attachedId so a remount re-attaches cleanly.
  useEffect(() => () => {
    attachedId.current = null
    if (streamCtl.current) streamCtl.current.abort()
  }, [])

  function stamp() {
    return new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  }

  function pushEvent(ev) {
    if (ev.type === 'done') return // terminal frame is rendered by attach()
    const stamped = { ...ev, atTs: Date.now(), at: stamp() }
    setFeed((prev) => [...prev, stamped])
    setConn((c) => (c === 'live' ? c : 'live'))
    if (ev.type === 'pending') setPending(stamped)
  }

  // Rebuild a readable feed from persisted AgentAction rows so a returning
  // operator sees full history, then live events append after the seed.
  function feedFromActions(actions) {
    const evs = []
    for (const a of actions || []) {
      if (!a.tool) {
        if (a.output) evs.push({ type: 'thought', text: a.output.slice(0, 500) })
        continue
      }
      evs.push({ type: 'tool_call', tool: a.tool, command: a.command || a.tool, args: a.args || '' })
      if (a.status === 'rejected') {
        evs.push({ type: 'rejected', tool: a.tool, args: a.args || '', hint: (a.error || '').slice(0, 200) })
      } else if (a.status === 'error') {
        evs.push({ type: 'tool_result', tool: a.tool, ok: false, command: a.command, args: a.args || '', error: (a.error || '').slice(0, 500) })
      } else if (a.status === 'note') {
        evs.push({ type: 'thought', text: a.output ? a.output.slice(0, 500) : '' })
      } else {
        evs.push({ type: 'tool_result', tool: a.tool, ok: true, command: a.command, args: a.args || '', output: (a.output || '').slice(0, 4000) })
      }
    }
    return evs
  }

  async function attach(run_id, seed = [], pendingRestore = null) {
    if (attachedId.current === run_id) return // already streaming this run
    attachedId.current = run_id
    if (streamCtl.current) streamCtl.current.abort()
    const ctl = new AbortController()
    streamCtl.current = ctl
    setRunId(run_id); setLive(true); setConn('connecting'); setFeed(seed); setPending(pendingRestore ? { ...pendingRestore, atTs: Date.now() } : null)
    try {
      const final = await agentStream(targetId, run_id, pushEvent, ctl.signal)
      if (final === null && ctl.signal.aborted) return // our own teardown
      const status = (final && final.status) || 'done'
      setConn('ended')
      setFeed((prev) => [...prev, { type: 'done', status, at: stamp() }])
      if (status === 'error') {
        setError('Session ended with an error — see the live log above and Timeline for details.')
      }
    } catch (err) {
      if (!ctl.signal.aborted) {
        setConn('ended')
        setError(err.message)
      }
    } finally {
      if (streamCtl.current === ctl) streamCtl.current = null
      if (!ctl.signal.aborted) {
        settledIds.current.add(run_id) // never auto-attach this run again
        attachedId.current = null
        setLive(false); setPending(null)
        await reload()
      }
    }
  }

  // Re-attach to a session that outlived this component (back-nav/refresh):
  // seed history + pending approval from the DB, then go live.
  async function rehydrate(run_id) {
    try {
      const log = await listActions(targetId, run_id)
      // The operator may have started a fresh session while history loaded.
      if (attachedId.current !== null) return
      setLogs((prev) => ({ ...prev, [run_id]: log }))
      await attach(run_id, feedFromActions(log.actions), log.pending || null)
    } catch (err) {
      setError(err.message)
    }
  }

  async function startWith(nextMode) {
    try {
      const data = await startAgent(targetId, {
        mode: nextMode, max_iterations: Number(maxIter) || 50, provider, model,
      })
      await attach(data.run_id)
    } catch (err) {
      setError(err.message)
    }
  }

  async function decide(decision) {
    if (!runId || !pending) return
    setDeciding(true)
    try {
      await approveAgent(targetId, runId, { decision, hint })
      setPending(null); setHint('')
    } catch (err) {
      const msg = err.message || ''
      if (/already recorded/i.test(msg)) setPending(null)
      // Restored approval went stale (decided elsewhere / worker moved on):
      // drop the card quietly instead of bannering.
      else if (/no action awaiting|no longer running/i.test(msg)) setPending(null)
      else setError(err.message)
    } finally {
      setDeciding(false)
    }
  }

  async function stopRun(run_id) {
    if (!run_id) return
    try {
      await stopAgent(targetId, run_id)
      await reload()
    } catch (err) {
      setError(err.message)
    }
  }

  async function downloadLog(run_id) {
    try {
      const log = await listActions(targetId, run_id)
      downloadText(`agent-session-${run_id}.md`, sessionLogText(log))
    } catch (err) {
      setError(err.message)
    }
  }

  async function toggleLog(run_id) {
    if (openLog === run_id) { setOpenLog(null); return }
    try {
      const log = await listActions(targetId, run_id)
      setLogs((prev) => ({ ...prev, [run_id]: log }))
      setOpenLog(run_id)
    } catch (err) {
      setError(err.message)
    }
  }

  // Rehydrate: a session that outlived this component (back-nav/refresh,
  // StrictMode remount, second tab) keeps running server-side. Seed the
  // feed from persisted actions, restore any pending approval, subscribe.
  // Skips runs already streaming or already seen to termination, so a dead
  // row can never spin a rehydrate loop.
  useEffect(() => {
    const active = runs.filter((r) => r.kind === 'agent' && r.status === 'running')
    if (active.length === 0) return
    if (attachedId.current !== null || settledIds.current.has(active[0].id)) return
    rehydrate(active[0].id)
  })

  const toolCalls = feed.filter((e) => e.type === 'tool_call').length
  const stepsDone = current?.iterations || feed.filter((e) => e.type === 'tool_result').length
  const pct = Math.min(100, Math.round((stepsDone / (Number(maxIter) || 50)) * 100))

  const showApproval = pending && pending.kind !== 'auth'
  const showAuth = pending && pending.kind === 'auth'

  return (
    <div>
      {activeElsewhere.length > 0 && (
        <div className="mb-4 rounded-xl border border-[#E2CFC4] bg-[#FBF1EC] p-3">
          <p className="text-sm font-semibold text-[#8C4A33]">
            AI already running{activeElsewhere.length > 1 ? ` (${activeElsewhere.length} sessions)` : ''}:
          </p>
          <ul className="mt-1 space-y-1">
            {activeElsewhere.map((s) => (
              <li key={s.run_id} className="flex flex-wrap items-center gap-2 text-xs text-[#8C4A33]">
                <span className="font-mono font-medium">{s.domain}</span>
                <span>session #{s.run_id} · {s.mode || 'semi'} · {s.iterations} steps</span>
                <button type="button" className="font-medium underline hover:text-black" onClick={() => onOpenTarget(s.target_id)}>
                  Open target →
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {!live && (
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <button type="button" className={btn} disabled={!!running} onClick={() => startWith('auto')} title={running ? `Session #${running.id} is already running here` : 'Start a fully automatic session'}>
            ▶ Run fully auto
          </button>
          <button type="button" className={btnSecondary} disabled={!!running} onClick={() => startWith('semi')} title={running ? `Session #${running.id} is already running here` : 'Start a session that pauses for approval'}>
            ▶ Run semi-auto
          </button>
          {running && (
            <span className="text-xs text-[#9C9A9A]">
              Session #{running.id} ({running.autonomy_mode || 'semi'}, {running.iterations} steps) is running — one session per target. Stop it to start a new one.
            </span>
          )}
        </div>
      )}

      <div className="grid items-start gap-4 xl:grid-cols-[1fr_250px]">
        {/* left column */}
        <div className="min-w-0 space-y-4">
          <div className="rounded-xl border border-[#E9E1D9] bg-white shadow-[0_1px_3px_rgba(70,70,70,0.06)]">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#F0EAE4] px-5 py-4">
              <div>
                <h3 className="flex items-center gap-2 text-[15px] font-semibold text-[#1C1A18]">
                  Agent control
                  {live && <span className="rounded-full bg-[#F5E6DE] px-2 py-0.5 font-mono text-[10px] font-semibold text-[#8C4A33]">● Live · {String(stepsDone).padStart(2, '0')}:{String(Number(maxIter) || 50)}</span>}
                </h3>
                <p className="mt-0.5 text-[12px] text-[#9C9A9A]">Configure and supervise the current run</p>
              </div>
              <div className="flex items-center gap-2">
                {runId && (
                  <button type="button" className={btnSecondary} onClick={() => downloadLog(runId)}>
                    ⭳ Download log
                  </button>
                )}
                {live && (
                  <button type="button" className="rounded-lg bg-black px-4 py-2 text-sm font-semibold text-white hover:bg-[#2a2a2a]" onClick={() => stopRun(runId)}>
                    ▫ Stop run
                  </button>
                )}
                {!live && running && (
                  <button type="button" className="rounded-lg bg-black px-4 py-2 text-sm font-semibold text-white hover:bg-[#2a2a2a]" onClick={() => stopRun(running.id)}>
                    ▫ Stop session #{running.id}
                  </button>
                )}
              </div>
            </div>
            <div className="px-5 py-4">
              <ModelPicker
                providers={providers} provider={provider} model={model} onChange={onProviderChange}
                maxIter={maxIter} onMaxIter={setMaxIter} disabled={live}
              />
            </div>
          </div>

          <div className="overflow-hidden rounded-xl border border-[#E9E1D9] bg-white shadow-[0_1px_3px_rgba(70,70,70,0.06)]">
            <div className="flex items-center justify-between bg-[#464646] px-5 py-3.5">
              <div>
                <h3 className="flex items-center gap-2 text-[14px] font-semibold text-white">
                  Live activity
                  {conn === 'live' && <span className="rounded-full bg-[#A35E47] px-2 py-0.5 font-mono text-[10px] font-bold text-white">● LIVE</span>}
                  {conn === 'connecting' && <span className="animate-pulse rounded-full bg-[#8a8078] px-2 py-0.5 font-mono text-[10px] font-bold text-white">◌ CONNECTING…</span>}
                  {conn === 'ended' && <span className="rounded-full bg-[#2b2b2b] px-2 py-0.5 font-mono text-[10px] font-bold text-[#B9B5B1]">■ ENDED</span>}
                </h3>
                <p className="font-mono text-[11px] text-[#B9B5B1]">session_{runId || current?.id || '—'} · started {current ? new Date(current.created_at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'}</p>
              </div>
              <span className="flex items-end gap-[3px]" aria-hidden>
                {[8, 14, 6, 12, 9].map((h, i) => (
                  <span key={i} className="w-[3px] rounded-full bg-[#A35E47]" style={{ height: h }} />
                ))}
              </span>
            </div>
            <div className="divide-y divide-[#F3EEE9]">
              {feed.length === 0 && !pending && !running && (
                <p className="px-5 py-8 text-center text-[13px] text-[#9C9A9A]">
                  No activity yet — {live ? 'waiting for the first step…' : 'press Run to start an autonomous session.'}
                </p>
              )}
              {feed.length === 0 && !pending && !live && running && (
                <div className="m-4 rounded-xl border border-[#E2CFC4] bg-[#FBF1EC] p-4">
                  <p className="text-[13px] font-semibold text-[#8C4A33]">
                    Session #{running.id} is still running on this target
                  </p>
                  <p className="mt-1 text-[12px] text-[#77716C]">
                    {running.autonomy_mode === 'auto' ? 'Fully auto' : 'Semi-auto'} · {running.iterations} steps
                    {running.autonomy_mode !== 'auto' && ' — it may be paused waiting for your approval.'}
                    {' '}The live feed should attach automatically — retry it, or stop the session first (one session per target).
                  </p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button
                      type="button" className="rounded-lg bg-[#A35E47] px-4 py-2 text-sm font-semibold text-white hover:bg-[#8E513D]"
                      onClick={() => rehydrate(running.id)}
                    >
                      ↻ Retry live feed
                    </button>
                    <button
                      type="button" className="rounded-lg bg-black px-4 py-2 text-sm font-semibold text-white hover:bg-[#2a2a2a]"
                      onClick={() => stopRun(running.id)}
                    >
                      ▫ Stop session #{running.id}
                    </button>
                  </div>
                </div>
              )}
              {feed.map((ev, i) => <FeedEvent key={i} ev={ev} index={i} />)}
              <div ref={feedEnd} />
            </div>

            {showApproval && (
              <div className="m-4 rounded-xl border border-[#D9A08F]/60 bg-[#FDF6F2] p-4">
                <div className="flex items-start gap-3">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-[#D9A08F]/60 bg-white">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6l7-3z" stroke="#A35E47" strokeWidth="1.8" /></svg>
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <p className="rounded-full bg-[#F5E6DE] px-2 py-0.5 text-[10px] font-bold tracking-wide text-[#8C4A33]">APPROVAL NEEDED</p>
                      <WaitingTimer since={pending.atTs} />
                    </div>
                    <p className="mt-2 text-[13px] font-semibold text-[#1C1A18]">
                      {pending.title || `Run ${pending.tool || '(no tool name)'}?`}
                    </p>
                    {pending.thought && <p className="mt-0.5 text-[12px] text-[#77716C]">{pending.thought}</p>}
                    {pending.command && (
                      <div className="mt-1.5 flex flex-wrap items-center gap-2">
                        <p className="font-mono text-[12px] text-[#464646]">{pending.command}</p>
                        <CopyBtn text={pending.command} label="Copy command" />
                      </div>
                    )}
                    {argsText(pending.args) && (
                      <div className="mt-1.5">
                        <div className="mb-1 flex items-center justify-between">
                          <span className="text-[10px] font-bold tracking-[0.12em] text-[#9C9A9A]">ARGUMENTS</span>
                          <CopyBtn text={argsText(pending.args)} label="Copy args" />
                        </div>
                        <JsonArgs value={pending.args} />
                      </div>
                    )}
                    <textarea
                      className={`${input} mt-2`} rows={2}
                      placeholder="Guide the agent (optional)…"
                      value={hint} onChange={(e) => setHint(e.target.value)}
                    />
                    <div className="mt-2 flex gap-2">
                      <button type="button" className={btn} disabled={deciding} onClick={() => decide('approve')}>Approve once</button>
                      <button type="button" className={btnSecondary} disabled={deciding} onClick={() => decide('reject')}>Reject</button>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {showAuth && (
              <div className="m-4 rounded-xl border border-dashed border-[#C9C2BB] bg-white p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="flex items-center gap-2 text-[13px] font-semibold text-[#1C1A18]">
                    <span className="text-[#9C9A9A]">⚿</span> Authentication required
                  </p>
                  <button type="button" className={btnSecondary} onClick={() => decide('approve')} disabled={deciding}>Refresh session →</button>
                </div>
                <p className="mt-1 text-[12px] text-[#9C9A9A]">{pending.detail || `The host session${pending.host ? ` for ${pending.host}` : ''} has expired.`}</p>
                <textarea
                  className={`${input} mt-2`} rows={2}
                  placeholder="Note for the agent (optional)…"
                  value={hint} onChange={(e) => setHint(e.target.value)}
                />
                <div className="mt-2 flex gap-2">
                  <button type="button" className={btn} disabled={deciding} onClick={() => decide('approve')}>Approve — I pasted it</button>
                  <button type="button" className={btnSecondary} disabled={deciding} onClick={() => decide('skip')}>Skip this host</button>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* right rail */}
        <div className="space-y-4">
          <div className="rounded-xl border border-[#E9E1D9] bg-white p-5 text-center shadow-[0_1px_3px_rgba(70,70,70,0.06)]">
            <p className="text-left text-[11px] font-bold tracking-[0.12em] text-[#9C9A9A]">RUN PROGRESS</p>
            <div className="mt-3">
              <Donut pct={pct} />
              <p className="mt-1 font-mono text-[11px] text-[#9C9A9A]">{stepsDone} / {Number(maxIter) || 50} steps</p>
            </div>
            <div className="mx-4 my-4 h-0.5 overflow-hidden rounded bg-[#F0EAE4]">
              <div className="h-full bg-[#A35E47]" style={{ width: `${pct}%` }} />
            </div>
            <div className="grid grid-cols-2 gap-y-4">
              <div className="border-r border-[#F0EAE4]">
                <p className="text-[10px] text-[#9C9A9A]">Steps</p>
                <p className="font-mono text-[15px] font-semibold">{stepsDone}</p>
              </div>
              <div>
                <p className="text-[10px] text-[#9C9A9A]">Tool calls</p>
                <p className="font-mono text-[15px] font-semibold">{toolCalls}</p>
              </div>
              <div className="border-r border-[#F0EAE4]">
                <p className="text-[10px] text-[#9C9A9A]">Leads</p>
                <p className="font-mono text-[15px] font-semibold">{leadsCount}</p>
              </div>
              <div>
                <p className="text-[10px] text-[#9C9A9A]">Confirmed</p>
                <p className="font-mono text-[15px] font-semibold">{confirmedCount}</p>
              </div>
            </div>
          </div>

          <div className="overflow-hidden rounded-xl border border-[#E9E1D9] bg-white shadow-[0_1px_3px_rgba(70,70,70,0.06)]">
            <div className="flex items-center justify-between px-4 py-3">
              <div>
                <p className="text-[13px] font-semibold">Past sessions</p>
                <p className="text-[11px] text-[#9C9A9A]">Recent agent runs</p>
              </div>
              <span className="rounded-full bg-[#F3EEE9] px-2 py-0.5 font-mono text-[11px] text-[#464646]">{agentRuns.length}</span>
            </div>
            <ul className="divide-y divide-[#F3EEE9]">
              {agentRuns.slice(0, 4).map((r) => (
                <li key={`agent-${r.id}`} className="px-4 py-2.5">
                  <button type="button" className="flex w-full items-center justify-between gap-2 text-left" onClick={() => toggleLog(r.id)}>
                    <span>
                      <span className="block text-[12px] font-semibold text-[#1C1A18]">
                        {new Date(r.created_at).toLocaleDateString('en-GB', { month: 'short', day: 'numeric' })}, {new Date(r.created_at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}
                      </span>
                      <span className="block text-[11px] text-[#9C9A9A]">{r.iterations} steps · {r.autonomy_mode || 'semi'}</span>
                    </span>
                    <span className={`text-[10px] font-medium ${r.status === 'running' ? 'text-[#8C4A33]' : 'text-[#9C9A9A]'}`}>
                      {r.status === 'running' ? '● Running' : r.status} ▾
                    </span>
                  </button>
                  {openLog === r.id && logs[r.id] && (
                    <ol className="mt-2 space-y-1 border-l-2 border-[#F0EAE4] pl-2">
                      {logs[r.id].actions.map((a) => (
                        <li key={`a-${a.id}`} className="text-[11px] text-[#464646]">
                          <span className="font-medium">#{a.seq} {a.tool ? <span className="font-mono">{a.tool}</span> : 'note'}</span>
                          <span> [{a.status}]</span>
                          {a.error && <span className="text-[#96382B]"> — {a.error.slice(0, 120)}</span>}
                        </li>
                      ))}
                      {logs[r.id].actions.length === 0 && <li className="text-[11px] text-[#B9B5B1]">No steps logged yet.</li>}
                    </ol>
                  )}
                </li>
              ))}
              {agentRuns.length === 0 && <li className="px-4 py-3 text-[12px] text-[#9C9A9A]">No sessions yet.</li>}
            </ul>
            {agentRuns.length > 0 && (
              <button type="button" className="block w-full bg-[#FAF7F4] px-4 py-2.5 text-[12px] font-semibold text-[#A35E47] hover:bg-[#F3EEE9]" onClick={() => runId && downloadLog(runId)}>
                View all sessions →
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
