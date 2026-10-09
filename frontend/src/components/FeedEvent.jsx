// One live agent-session event, Urban Loft timeline style.
// Props: ev (SSE event object), index (1-based step number for B-badges).
import { CopyBtn } from './ui.jsx'
import { argsText } from '../lib/session.js'

function Step({ n, kicker, time, children }) {
  return (
    <div className="flex gap-4 px-5 py-4">
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-[#E2DAD2] bg-white font-mono text-[9px] text-[#9C9A9A]">{n}</span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <p className="text-[10px] font-bold tracking-[0.14em] text-[#A35E47]">{kicker}</p>
          {time && <span className="font-mono text-[10px] text-[#B9B5B1]">{time}</span>}
        </div>
        <div className="mt-1.5">{children}</div>
      </div>
    </div>
  )
}

export default function FeedEvent({ ev, index = 0 }) {
  const n = `B${String(index + 1).padStart(2, '0')}`
  if (ev.type === 'thought') {
    return (
      <Step n={n} kicker="THOUGHT" time={ev.at}>
        <p className="text-[13px] leading-relaxed text-[#464646]">{ev.text}</p>
      </Step>
    )
  }
  if (ev.type === 'tool_call') {
    return (
      <Step n={n} kicker="TOOL CALL" time={ev.at}>
        <div className="overflow-hidden rounded-lg border border-[#E9E1D9]">
          <div className="flex items-center justify-between bg-[#F7F3EF] px-3 py-2">
            <span className="font-mono text-[12px] font-semibold text-[#1C1A18]">{ev.tool || '(no tool name)'}</span>
            <CopyBtn text={ev.command} label="⧉ Copy" />
          </div>
          <div className="bg-white px-3 py-2.5">
            {ev.thought && <p className="mb-1 text-[12px] text-[#77716C]">{ev.thought}</p>}
            <p className="whitespace-pre-wrap font-mono text-[12px] text-[#464646]">{ev.command}</p>
            {argsText(ev.args) && (
              <details className="mt-1 text-xs text-[#9C9A9A]">
                <summary className="cursor-pointer hover:underline">args</summary>
                <pre className="whitespace-pre-wrap font-mono text-[11px]">{argsText(ev.args)}</pre>
              </details>
            )}
          </div>
        </div>
      </Step>
    )
  }
  if (ev.type === 'auth_needed') {
    return (
      <Step n={n} kicker="SESSION" time={ev.at}>
        <div className="rounded-lg border border-[#E8C86A]/60 bg-[#FDF6E3] p-3">
          <p className="text-xs font-semibold text-[#7A5B1F]">Session expired{ev.tool ? ` during ${ev.tool}` : ''}</p>
          <p className="mt-0.5 text-xs text-[#464646]">{ev.detail}</p>
        </div>
      </Step>
    )
  }
  if (ev.type === 'approved') {
    return <p className="px-5 py-1 text-xs text-[#3F5A3C]">✓ Approved <span className="font-mono">{ev.tool}</span>{ev.hint ? ' + guidance sent' : ''}.</p>
  }
  if (ev.type === 'rejected') {
    return (
      <Step n={n} kicker="REJECTED" time={ev.at}>
        <div className="rounded-lg border border-[#E8C86A]/60 bg-[#FDF6E3] p-3">
          <p className="text-xs text-[#7A5B1F]">✕ Rejected <span className="font-mono font-semibold">{ev.tool || '(no tool name)'}</span>{ev.hint ? `: “${ev.hint}”` : ''}</p>
          {argsText(ev.args) && (
            <details className="mt-1 text-xs text-[#77716C]">
              <summary className="cursor-pointer hover:underline">attempted args (rerun manually)</summary>
              <pre className="whitespace-pre-wrap">{argsText(ev.args)}</pre>
            </details>
          )}
        </div>
      </Step>
    )
  }
  if (ev.type === 'malformed') {
    return (
      <Step n={n} kicker="MALFORMED" time={ev.at}>
        <div className="rounded-lg border border-[#EFC5BE] bg-[#FDF0EE] p-3">
          <p className="text-xs font-semibold text-[#96382B]">
            Malformed tool call{ev.tool ? <> (tried: <span className="font-mono">{ev.tool}</span>)</> : ''}
          </p>
          <p className="mt-0.5 text-xs text-[#96382B]">{ev.detail}</p>
          {(ev.payload !== undefined && ev.payload !== null && ev.payload !== '') && (
            <details className="mt-1 text-xs text-[#464646]">
              <summary className="cursor-pointer hover:underline">raw model payload (rerun manually)</summary>
              <pre className="whitespace-pre-wrap">{argsText(ev.payload)}</pre>
              <div className="mt-1"><CopyBtn text={argsText(ev.payload)} label="Copy payload" /></div>
            </details>
          )}
        </div>
      </Step>
    )
  }
  if (ev.type === 'tool_result') {
    return (
      <Step n={n} kicker="RESULT" time={ev.at}>
        <div>
          <div className="flex items-center justify-between">
            <span className="inline-flex items-center gap-1.5 font-mono text-[11px]">
              <span className={`rounded px-1.5 py-0.5 font-semibold ${ev.ok ? 'bg-[#E9EFE7] text-[#3F5A3C]' : 'bg-[#F7D9D4] text-[#96382B]'}`}>
                {ev.ok ? '200 OK' : 'FAIL'}
              </span>
              <span className="text-[#9C9A9A]">18.4 kB · 342 ms</span>
            </span>
            {ev.ok && <CopyBtn text={ev.output} label="⧉ Copy response" />}
          </div>
          {ev.command && <p className="mt-1 font-mono text-[11px] text-[#9C9A9A]">{ev.command}</p>}
          {ev.ok
            ? <p className="mt-1 text-[13px] leading-relaxed text-[#464646]">{ev.output}</p>
            : (
              <div>
                <p className="mt-1 text-xs text-[#96382B]">{ev.error}</p>
                {argsText(ev.args) && (
                  <details className="mt-1 text-xs text-[#77716C]">
                    <summary className="cursor-pointer hover:underline">attempted args (rerun manually)</summary>
                    <pre className="whitespace-pre-wrap">{argsText(ev.args)}</pre>
                  </details>
                )}
              </div>
            )}
        </div>
      </Step>
    )
  }
  if (ev.type === 'report') {
    return <p className="px-5 py-2 text-sm font-semibold text-[#3F5A3C]">Report written — {ev.findings} confirmed, {ev.leads || 0} leads. See Reports tab.</p>
  }
  if (ev.type === 'error') {
    return (
      <Step n={n} kicker="ERROR" time={ev.at}>
        <div className="rounded-lg border border-[#EFC5BE] bg-[#FDF0EE] p-3">
          <p className="text-[12px] font-medium leading-relaxed text-[#96382B]">{ev.detail || 'The session hit an error.'}</p>
        </div>
      </Step>
    )
  }
  if (ev.type === 'done') {
    const tone = ev.status === 'ok' ? 'text-[#3F5A3C]' : 'text-[#96382B]'
    const msg = {
      ok: 'finished — report saved. See Reports tab.',
      stopped: 'stopped by operator.',
      error: 'ended with an error — see Timeline for details.',
      running: 'worker is gone (server restarted?) — session is no longer live.',
    }[ev.status] || `ended (${ev.status}).`
    return (
      <p className={`px-5 py-2 font-mono text-[11px] ${tone}`}>
        ■ Session {msg}
      </p>
    )
  }
  if (ev.type === 'report_rejected' || ev.type === 'budget') {
    return <p className="px-5 py-1 text-xs text-[#7A5B1F]">⚠ {ev.detail}</p>
  }
  if (ev.type === 'pending') {
    return null
  }
  if (ev.type === 'status') {
    return <p className="px-5 py-1 font-mono text-[11px] text-[#B9B5B1]">Session {ev.status}…</p>
  }
  return null
}
