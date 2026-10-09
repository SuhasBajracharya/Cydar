// Shared presentational primitives: pills/logo/modal/copy/donut.
import { useState } from 'react'
import { miniBtn, SEV_STYLE, STATUS_STYLE } from '../lib/styles.js'

export function Logo({ className = '' }) {
  return (
    <div className={`flex items-center bg-black ${className}`}>
      <img
        src="/cydar-logo.png"
        alt="Cydar"
        className="h-12 w-auto object-contain"
      />
    </div>
  )
}

export function StatusPill({ status }) {
  const tone = (status || 'ready').toLowerCase()
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border border-[#E2DAD2] px-2.5 py-0.5 text-xs font-medium ${STATUS_STYLE[tone] || 'bg-[#F1ECE6] text-[#464646]'}`}>
      <span className="inline-block h-1.5 w-1.5 rounded-full bg-[#A35E47]" />
      {status}
    </span>
  )
}

export function Pill({ tone, children }) {
  return (
    <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_STYLE[(tone || '').toLowerCase()] || 'bg-[#EDEDED] text-[#464646]'}`}>
      {children}
    </span>
  )
}

export function SevPill({ severity }) {
  return (
    <span className={`inline-block rounded-full border border-black/5 px-2.5 py-0.5 text-xs font-medium capitalize ${SEV_STYLE[(severity || 'info').toLowerCase()] || SEV_STYLE.info}`}>
      {severity}
    </span>
  )
}

export function ErrorBanner({ message, onClear }) {
  if (!message) return null
  return (
    <div className="mb-4 rounded-xl border border-[#EFC5BE] bg-[#FDF0EE] px-4 py-2.5 text-sm text-[#96382B]">
      {message}{' '}
      <button type="button" className="ml-2 font-medium underline" onClick={onClear}>dismiss</button>
    </div>
  )
}

export function Modal({ title, onClose, children }) {
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4 pt-16" onClick={onClose}>
      <div className="w-full max-w-2xl rounded-xl border border-[#E9E1D9] bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-[#1C1A18]">{title}</h2>
          <button type="button" className="text-sm text-[#9C9A9A] hover:text-black" onClick={onClose}>✕ Close</button>
        </div>
        {children}
      </div>
    </div>
  )
}

export function CopyBtn({ text, label }) {
  const [copied, setCopied] = useState(false)
  if (!text) return null
  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <button type="button" className={miniBtn} onClick={copy} title="Copy to run manually on your own machine">
      {copied ? '✓ Copied' : (label || 'Copy')}
    </button>
  )
}

export function Donut({ pct }) {
  const r = 44
  const c = 2 * Math.PI * r
  const v = Math.max(0, Math.min(100, pct || 0))
  return (
    <div className="relative mx-auto h-28 w-28">
      <svg viewBox="0 0 110 110" className="h-28 w-28 -rotate-90">
        <circle cx="55" cy="55" r={r} stroke="#EDE6DF" strokeWidth="10" fill="none" />
        <circle
          cx="55" cy="55" r={r} stroke="#A35E47" strokeWidth="10" fill="none"
          strokeLinecap="round" strokeDasharray={c} strokeDashoffset={c - (c * v) / 100}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-xl font-bold text-[#1C1A18]">{Math.round(v)}%</span>
      </div>
    </div>
  )
}
