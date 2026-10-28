// Provider/model/step controls rendered as a labeled grid inside
// the Agent control card. Run mode is chosen by the Run buttons, not here.
// Props: providers, provider, model, onChange(providerId, modelId),
// maxIter, onMaxIter(...), disabled.
//
// The Model field is a searchable combobox: curated ids from
// GET /api/providers/ plus the live list from
// GET /api/providers/<id>/models/ (proxied server-side so the key never
// reaches the browser). The live list is fetched lazily on first open per
// provider and cached in-memory for the session.
import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchProviderModels } from '../api.js'
import { labelCls } from '../lib/styles.js'

const CUSTOM_MODEL = '__custom__'
const RENDER_CAP = 200
const EMPTY_MODELS = []

export default function ModelPicker({
  providers, provider, model, onChange,
  maxIter, onMaxIter, disabled,
}) {
  const spec = providers.find((p) => p.id === provider)
  const effective = model || spec?.default_model || ''

  // Live-model cache: { [pid]: { models: [], error: null|string, loaded: bool } }
  const [remote, setRemote] = useState({})
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const boxRef = useRef(null)
  const searchRef = useRef(null)

  const curated = useMemo(() => spec?.models || EMPTY_MODELS, [spec])
  const live = remote[provider]?.models || EMPTY_MODELS
  const liveLoaded = !!remote[provider]?.loaded
  const liveError = remote[provider]?.error || null

  // Combined list: default first, then curated, then live-only extras.
  const combined = useMemo(() => {
    const seen = new Set()
    const out = []
    const push = (m) => { if (m && !seen.has(m)) { seen.add(m); out.push(m) } }
    if (spec?.default_model) push(spec.default_model)
    for (const m of curated) push(m)
    for (const m of live) push(m)
    // Keep a saved custom id selectable even if absent from every list.
    if (effective && !seen.has(effective)) push(effective)
    return out
  }, [spec, curated, live, effective])

  const inList = combined.includes(effective)
  const selectValue = inList ? effective : (effective ? CUSTOM_MODEL : (spec?.default_model || ''))
  const showCustomInput = selectValue === CUSTOM_MODEL

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return combined
    return combined.filter((m) => m.toLowerCase().includes(q))
  }, [combined, query])
  const capped = filtered.slice(0, RENDER_CAP)

  async function loadLive(refresh = false) {
    if (!spec?.configured || loading) return
    if (!refresh && remote[provider]?.loaded) return
    try {
      setLoading(true)
      const res = await fetchProviderModels(provider, refresh)
      setRemote((prev) => ({
        ...prev,
        [provider]: { models: res.models || [], error: res.error || null, loaded: true },
      }))
    } catch (err) {
      setRemote((prev) => ({
        ...prev,
        [provider]: { models: [], error: err.message, loaded: true },
      }))
    } finally {
      setLoading(false)
    }
  }

  function toggleOpen() {
    if (disabled) return
    const next = !open
    setOpen(next)
    if (next) {
      setQuery('')
      loadLive(false)
    }
  }

  function pickProvider(pid) {
    const next = providers.find((p) => p.id === pid)
    const known = [...(next?.models || []), ...(remote[pid]?.models || [])]
    const keep = next && (known.includes(effective) || !effective) ? effective : ''
    setOpen(false)
    onChange(pid, keep)
  }

  function pickModel(mid) {
    setOpen(false)
    onChange(provider, mid === CUSTOM_MODEL ? effective : mid)
  }

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return undefined
    function onDown(e) {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false)
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onDown)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onDown)
    }
  }, [open])

  useEffect(() => {
    if (open) searchRef.current?.focus()
  }, [open])

  // Reset the open panel when the provider changes.
  useEffect(() => { setOpen(false); setQuery('') }, [provider])

  const selCls = 'w-full appearance-none rounded-lg border border-[#E2DAD2] bg-white px-3 py-2 pr-8 text-[13px] font-medium text-[#1C1A18] focus:border-[#A35E47] focus:outline-none disabled:opacity-50'
  const btnCls = 'flex w-full items-center justify-between gap-2 rounded-lg border border-[#E2DAD2] bg-white px-3 py-2 text-left text-[13px] font-medium text-[#1C1A18] focus:border-[#A35E47] focus:outline-none disabled:opacity-50'

  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <label className="block">
        <span className={labelCls}>Provider</span>
        <span className="relative mt-1.5 block">
          <select className={selCls} value={provider} disabled={disabled} onChange={(e) => pickProvider(e.target.value)}>
            {providers.map((p) => (
              <option key={p.id} value={p.id} disabled={!p.configured}>
                {p.name}{p.configured ? '' : ' (no key)'}
              </option>
            ))}
          </select>
          <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[#9C9A9A]">▾</span>
        </span>
      </label>
      <div className="block">
        <span className={labelCls}>
          Model{liveLoaded && live.length > 0 ? ` (${combined.length})` : ''}
        </span>
        <div ref={boxRef} className="relative mt-1.5">
          <button type="button" className={btnCls} disabled={disabled} onClick={toggleOpen} aria-expanded={open} aria-haspopup="listbox">
            <span className="min-w-0 flex-1 truncate font-mono text-[12px]">
              {showCustomInput ? 'Custom…' : (effective || 'Select model…')}
            </span>
            <span className="shrink-0 text-[#9C9A9A]">{open ? '▴' : '▾'}</span>
          </button>
          {open && (
            <div className="absolute z-30 mt-1.5 w-full min-w-[260px] rounded-lg border border-[#E2DAD2] bg-white shadow-lg">
              <div className="border-b border-[#F0EAE1] p-2">
                <input
                  ref={searchRef}
                  className="w-full rounded-md border border-[#E2DAD2] bg-[#FAF8F6] px-2.5 py-1.5 text-[12px] focus:border-[#A35E47] focus:outline-none"
                  placeholder={`Search ${combined.length || ''} models…`.trim()}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
              </div>
              <div className="max-h-60 overflow-y-auto p-1 nice-scroll" role="listbox">
                {loading && !liveLoaded && (
                  <p className="px-2.5 py-2 text-[12px] text-[#9C9A9A]">Loading live models…</p>
                )}
                {capped.map((m) => (
                  <button
                    key={m}
                    type="button"
                    role="option"
                    aria-selected={m === effective}
                    onClick={() => pickModel(m)}
                    className={`flex w-full items-center justify-between gap-2 rounded-md px-2.5 py-1.5 text-left font-mono text-[12px] hover:bg-[#F7F3EF] ${m === effective ? 'bg-[#F5E6DE] font-semibold text-[#A35E47]' : 'text-[#1C1A18]'}`}
                  >
                    <span className="min-w-0 flex-1 truncate">
                      {m}{m === spec?.default_model ? ' ★' : ''}{m === effective && m !== spec?.default_model ? ' ✓' : ''}
                    </span>
                  </button>
                ))}
                {filtered.length === 0 && !loading && (
                  <p className="px-2.5 py-2 text-[12px] text-[#9C9A9A]">No matches — pick Custom… below to type any id.</p>
                )}
                <button
                  type="button"
                  onClick={() => pickModel(CUSTOM_MODEL)}
                  className={`mt-1 w-full rounded-md border-t border-[#F0EAE1] px-2.5 py-1.5 text-left text-[12px] font-semibold hover:bg-[#F7F3EF] ${showCustomInput ? 'text-[#A35E47]' : 'text-[#464646]'}`}
                >
                  Custom… (type any model id)
                </button>
              </div>
              <div className="flex items-center justify-between gap-2 border-t border-[#F0EAE1] px-2.5 py-1.5 text-[11px] text-[#9C9A9A]">
                <span>
                  {loading ? 'Fetching…' : liveError ? liveError : `Showing ${capped.length} of ${filtered.length}${filtered.length !== combined.length ? ` (${combined.length} total)` : ''}`}
                </span>
                {liveLoaded && (
                  <button type="button" className="font-semibold text-[#A35E47] hover:underline" onClick={() => loadLive(true)}>
                    Refresh
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
      <label className="block">
        <span className={labelCls}>Max steps</span>
        <input
          className="mt-1.5 w-full rounded-lg border border-[#E2DAD2] bg-white px-3 py-2 text-[13px] font-medium focus:border-[#A35E47] focus:outline-none disabled:opacity-50"
          type="number" min={1} max={100} value={maxIter} disabled={disabled}
          onChange={(e) => onMaxIter(e.target.value)}
        />
      </label>
      {showCustomInput && (
        <label className="block sm:col-span-2 xl:col-span-4">
          <span className={labelCls}>Custom model id</span>
          <input
            className="mt-1.5 w-full rounded-lg border border-[#E2DAD2] bg-white px-3 py-2 text-[13px] focus:border-[#A35E47] focus:outline-none"
            placeholder="Type any model id…" disabled={disabled}
            value={effective === CUSTOM_MODEL ? '' : effective}
            onChange={(e) => onChange(provider, e.target.value)}
          />
        </label>
      )}
      {spec && !spec.configured && spec.requires_key !== false && (
        <p className="mt-2 text-[12px] text-[#7A5B1F] sm:col-span-3">
          <span className="font-semibold">{spec.name}</span> has no API key. Add it in the Providers page to use this provider.
        </p>
      )}
      {spec && spec.requires_key === false && (
        <p className="mt-2 text-[12px] text-[#9C9A9A] sm:col-span-3">{spec.help || 'Local model — no API key needed.'}</p>
      )}
    </div>
  )
}
