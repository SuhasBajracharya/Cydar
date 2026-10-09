// Provider/model/step controls rendered as a labeled grid inside
// the Agent control card. Run mode is chosen by the Run buttons, not here.
// Props: providers, provider, model, onChange(providerId, modelId),
// maxIter, onMaxIter(...), disabled.
import { CopyBtn } from './ui.jsx'
import { labelCls } from '../lib/styles.js'

const CUSTOM_MODEL = '__custom__'

export default function ModelPicker({
  providers, provider, model, onChange,
  maxIter, onMaxIter, disabled,
}) {
  const spec = providers.find((p) => p.id === provider)
  const effective = model || spec?.default_model || ''
  const selectValue = spec && spec.models.includes(effective) ? effective : (effective ? CUSTOM_MODEL : (spec?.default_model || ''))

  function pickProvider(pid) {
    const next = providers.find((p) => p.id === pid)
    const keep = next && next.models.includes(effective) ? effective : ''
    onChange(pid, keep)
  }

  const selCls = 'w-full appearance-none rounded-lg border border-[#E2DAD2] bg-white px-3 py-2 pr-8 text-[13px] font-medium text-[#1C1A18] focus:border-[#A35E47] focus:outline-none disabled:opacity-50'

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
      <label className="block">
        <span className={labelCls}>Model</span>
        <span className="relative mt-1.5 block">
          <select
            className={selCls} disabled={disabled}
            value={selectValue}
            onChange={(e) => onChange(provider, e.target.value === CUSTOM_MODEL ? effective : e.target.value)}
          >
            {spec?.default_model && <option value={spec.default_model}>{spec.default_model} (default)</option>}
            {(spec?.models || []).filter((m) => m !== spec?.default_model).map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
            <option value={CUSTOM_MODEL}>Custom…</option>
          </select>
          <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[#9C9A9A]">▾</span>
        </span>
      </label>
      <label className="block">
        <span className={labelCls}>Max steps</span>
        <input
          className="mt-1.5 w-full rounded-lg border border-[#E2DAD2] bg-white px-3 py-2 text-[13px] font-medium focus:border-[#A35E47] focus:outline-none disabled:opacity-50"
          type="number" min={1} max={100} value={maxIter} disabled={disabled}
          onChange={(e) => onMaxIter(e.target.value)}
        />
      </label>
      {selectValue === CUSTOM_MODEL && (
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
        <p className="mt-2 flex flex-wrap items-center gap-2 text-[12px] text-[#7A5B1F] sm:col-span-3">
          <span>
            <span className="font-semibold">{spec.name}</span> has no API key. Add this line to <span className="font-mono">.env</span> (repo root), restart Django, then refresh:
          </span>
          <code className="rounded-md border border-[#E2DAD2] bg-[#FAF7F4] px-2 py-1 font-mono text-[11px] text-[#464646]">{spec.key_env}=paste-key-here</code>
          <CopyBtn text={`${spec.key_env}=paste-key-here`} label="Copy line" />
        </p>
      )}
      {spec && spec.requires_key === false && (
        <p className="mt-2 text-[12px] text-[#9C9A9A] sm:col-span-3">{spec.help || 'Local model — no API key needed.'}</p>
      )}
    </div>
  )
}
