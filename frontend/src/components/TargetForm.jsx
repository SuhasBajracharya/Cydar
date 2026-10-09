// Target create/edit form (shared by the New-Target and Edit modals).
// Props: initial (target or undefined), submitText, onSubmit(payload), setError.
import { useEffect, useState } from 'react'
import { signupMailboxStatus } from '../api.js'
import { btn, btnDanger, btnSecondary, input } from '../lib/styles.js'

const AUTOMATION_OPTIONS = [
  ['read-only', 'Read-only recon'],
  ['standard', 'Standard automation'],
  ['aggressive', 'Aggressive (where allowed)'],
]

export default function TargetForm({ initial, submitText, onSubmit, setError }) {
  const [domain, setDomain] = useState(initial?.domain || '')
  const [inScope, setInScope] = useState(initial?.in_scope || '')
  const [outOfScope, setOutOfScope] = useState(initial?.out_of_scope || '')
  const [automationLevel, setAutomationLevel] = useState(initial?.automation_level || 'standard')
  const [scopeRules, setScopeRules] = useState(initial?.scope_rules || '')
  const [authType, setAuthType] = useState(initial?.auth_type || '')
  const [authName, setAuthName] = useState(initial?.auth_name || '')
  const [authValue, setAuthValue] = useState('')
  const [authCheckUrl, setAuthCheckUrl] = useState(initial?.auth_check_url || '')
  const [extraCreds, setExtraCreds] = useState(initial?.auth_credentials || [])
  const [allowSignup, setAllowSignup] = useState(!!initial?.allow_self_registration)
  const [allowInsecureTls, setAllowInsecureTls] = useState(!!initial?.allow_insecure_tls)
  const [mailbox, setMailbox] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    signupMailboxStatus().then(setMailbox).catch(() => setMailbox({ configured: false }))
  }, [])

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    try {
      const payload = {
        domain,
        scope_rules: scopeRules,
        in_scope: inScope,
        out_of_scope: outOfScope,
        automation_level: automationLevel,
        auth_type: authType,
        auth_name: authName,
        auth_check_url: authCheckUrl,
      }
      // Omit an untouched secret so edits don't wipe the stored session:
      // blank + unchanged type + previously configured => backend keeps it.
      if (authValue || !initial?.auth_configured || authType !== (initial?.auth_type || '')) {
        payload.auth_value = authValue
      }
      payload.allow_self_registration = allowSignup
      payload.allow_insecure_tls = allowInsecureTls
      // Extra per-host sessions (replace-all on the backend; blank values
      // keep stored secrets, removed rows are deleted on save).
      payload.auth_credentials = extraCreds.map((c) => {
        const e = { host_pattern: c.host_pattern, auth_type: c.auth_type, auth_name: c.auth_name || '', check_url: c.check_url || '' }
        if (c.id) e.id = c.id
        if (c.auth_value) e.auth_value = c.auth_value
        return e
      })
      await onSubmit(payload)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3">
      <input className={input} placeholder="Domain, e.g. example.com (bare host, no https://)" value={domain} onChange={(e) => setDomain(e.target.value)} />
      <textarea className={input} rows={2} placeholder="In-scope hosts — one per line (commas ok), e.g. admin.example.com" value={inScope} onChange={(e) => setInScope(e.target.value)} />
      <textarea className={input} rows={2} placeholder="Out-of-scope hosts (optional)" value={outOfScope} onChange={(e) => setOutOfScope(e.target.value)} />
      <select className={input} value={automationLevel} onChange={(e) => setAutomationLevel(e.target.value)}>
        {AUTOMATION_OPTIONS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
      </select>
      <textarea className={input} rows={5} placeholder="Paste the program's scope rules here…" value={scopeRules} onChange={(e) => setScopeRules(e.target.value)} />
      <div className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3">
        <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">Session auth (optional)</p>
        <p className="text-xs text-slate-500">
          Log in with your own test account in your browser, then paste the session here.
          Saved on the server and never shown again — tools send it automatically, the model never sees it.
        </p>
        <div className="flex gap-2">
          <select className={input} style={{ maxWidth: 200 }} value={authType} onChange={(e) => setAuthType(e.target.value)}>
            <option value="">No auth</option>
            <option value="bearer">Bearer token</option>
            <option value="header">Custom header</option>
            <option value="cookie">Session cookie</option>
          </select>
          {(authType === 'header' || authType === 'cookie') && (
            <input className={input} placeholder={authType === 'header' ? 'Header name, e.g. X-Session' : 'Cookie name, e.g. sessionid'} value={authName} onChange={(e) => setAuthName(e.target.value)} />
          )}
        </div>
        {authType && (
          <input className={input} type="password" autoComplete="off"
            placeholder={initial?.auth_configured ? 'Session saved ✓ — leave blank to keep, paste to replace' : 'Paste token / session value…'}
            value={authValue} onChange={(e) => setAuthValue(e.target.value)} />
        )}
        <input className={input} placeholder="Check URL that requires login, e.g. https://host/dashboard" value={authCheckUrl} onChange={(e) => setAuthCheckUrl(e.target.value)} />
        <div className="space-y-2 pt-1">
          <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">More host sessions</p>
          <p className="text-xs text-slate-500">
            One session per host/subdomain — different logins or JWTs per host go here
            (host or <span className="font-mono">*.example.com</span>). Removed rows are deleted on save.
          </p>
          {extraCreds.map((c, i) => (
            <div key={c.id || `new-${i}`} className="flex flex-wrap items-center gap-1.5">
              <input className={input} style={{ maxWidth: 170 }} placeholder="host or *.example.com"
                value={c.host_pattern} onChange={(e) => setExtraCreds((prev) => prev.map((x, j) => j === i ? { ...x, host_pattern: e.target.value } : x))} />
              <select className={input} style={{ maxWidth: 130 }} value={c.auth_type}
                onChange={(e) => setExtraCreds((prev) => prev.map((x, j) => j === i ? { ...x, auth_type: e.target.value } : x))}>
                <option value="bearer">Bearer</option>
                <option value="header">Header</option>
                <option value="cookie">Cookie</option>
              </select>
              {(c.auth_type === 'header' || c.auth_type === 'cookie') && (
                <input className={input} style={{ maxWidth: 130 }} placeholder="name"
                  value={c.auth_name || ''} onChange={(e) => setExtraCreds((prev) => prev.map((x, j) => j === i ? { ...x, auth_name: e.target.value } : x))} />
              )}
              <input className={input} style={{ maxWidth: 170 }} type="password" autoComplete="off"
                placeholder={c.configured ? 'Saved ✓ — blank keeps' : 'Paste value…'}
                value={c.auth_value || ''} onChange={(e) => setExtraCreds((prev) => prev.map((x, j) => j === i ? { ...x, auth_value: e.target.value } : x))} />
              <button type="button" className={btnDanger}
                onClick={() => { if (!c.configured || window.confirm(`Delete session for ${c.host_pattern || 'this host'}?`)) setExtraCreds((prev) => prev.filter((_, j) => j !== i)) }}>
                ✕
              </button>
            </div>
          ))}
          <button type="button" className={btnSecondary}
            onClick={() => setExtraCreds((prev) => [...prev, { host_pattern: '', auth_type: 'cookie', auth_name: '', auth_value: '', check_url: '', configured: false }])}>
            + Add host session
          </button>
        </div>
        <label className="flex items-start gap-2 text-xs text-slate-600">
          <input type="checkbox" className="mt-0.5" checked={allowSignup} onChange={(e) => setAllowSignup(e.target.checked)} />
          <span>
            <span className="font-medium">Allow trial signups</span> — the agent may create throwaway accounts
            with my signup mailbox on open registration forms (aggressive runs only).
            {mailbox && (
              <span className={mailbox.configured ? 'text-emerald-700' : 'text-amber-700'}>
                {' '}Mailbox: {mailbox.configured ? `connected (${mailbox.host})` : 'not configured — set SIGNUP_IMAP_* in .env'}
              </span>
            )}
          </span>
        </label>
        <label className="flex items-start gap-2 text-xs text-slate-600">
          <input type="checkbox" className="mt-0.5" checked={allowInsecureTls} onChange={(e) => setAllowInsecureTls(e.target.checked)} />
          <span>
            <span className="font-medium">Allow insecure TLS</span> — skip certificate verification for HTTPS tools
            (THM/HTB labs with self-signed/expired certs only — NOT for production targets).
          </span>
        </label>
      </div>
      <button className={btn} disabled={busy} type="submit">{busy ? 'Saving…' : submitText}</button>
    </form>
  )
}
