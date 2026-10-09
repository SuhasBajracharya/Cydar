// Helpers over the markdown bodies the agent writes (see
// agent/loop.py render_report_markdown): `### [SEVERITY] Cn: title`
// finding headings. Pure functions, no JSX.

const RANK = { info: 0, low: 1, medium: 2, high: 3, critical: 4 }

export function reportSeverities(body) {
  const found = []
  for (const m of String(body || '').matchAll(/^###\s*\[([^\]]+)\]/gim)) {
    const sev = m[1].trim().toLowerCase()
    if (sev in RANK) found.push(sev)
  }
  return found
}

export function reportSeverity(body) {
  const sevs = reportSeverities(body)
  if (sevs.length === 0) return null
  return sevs.sort((a, b) => RANK[b] - RANK[a])[0]
}

export const SEV_BORDER = {
  info: 'border-l-[#9C9A9A]',
  low: 'border-l-[#7A8CA3]',
  medium: 'border-l-[#C9A227]',
  high: 'border-l-[#A35E47]',
  critical: 'border-l-[#A33A2E]',
}
