// Pure helpers for session tool-call display + download. No JSX, no React.

export function argsText(args) {
  if (args === undefined || args === null || args === '') return ''
  return typeof args === 'string' ? args : JSON.stringify(args, null, 2)
}

export function downloadText(filename, text) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

// Full session log for manual follow-up: every attempted tool call with its
// exact name, args and command, so the operator can rerun steps locally.
export function sessionLogText(log) {
  const lines = [
    `# Agent session #${log.run.id} — ${log.run.provider || ''}/${log.run.model || 'default'} (${log.run.autonomy_mode}, ${log.run.status})`,
    '',
  ]
  for (const a of log.actions || []) {
    lines.push(`## #${a.seq} tool=${a.tool || '(no tool name)'} [${a.status}]`)
    if (a.thought) lines.push(`thought: ${a.thought}`)
    if (a.command) lines.push(`command: ${a.command}`)
    if (a.args) lines.push(`args: ${a.args}`)
    if (a.output) lines.push(`output:\n${a.output}`)
    if (a.error) lines.push(`error: ${a.error}`)
    lines.push('')
  }
  return lines.join('\n')
}
