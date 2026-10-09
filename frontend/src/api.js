// Tiny API layer for the SPA: same-origin /api (proxied to Django in dev),
// session cookies + CSRF header on every POST.

function getCsrfToken() {
  const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/)
  return match ? decodeURIComponent(match[1]) : ''
}

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: 'include', // send the Django session cookie
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCsrfToken(),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  })
  let data = null
  try {
    data = await res.json()
  } catch {
    /* non-JSON (shouldn't happen) — error below still surfaces status */
  }
  if (!res.ok) {
    throw new Error(data?.detail || `Request failed (${res.status})`)
  }
  return data
}

export const me = () => api('/auth/me/')
export const login = (username, password) => api('/auth/login/', { method: 'POST', body: { username, password } })
export const logout = () => api('/auth/logout/', { method: 'POST' })

export const listProviders = () => api('/providers/')

export const listTargets = () => api('/targets/')
export const createTarget = (payload) => api('/targets/', { method: 'POST', body: payload })
export const getTarget = (id) => api(`/targets/${id}/`)
export const updateTarget = (id, payload) => api(`/targets/${id}/`, { method: 'PUT', body: payload })
export const deleteTarget = (id) => api(`/targets/${id}/`, { method: 'DELETE' })
export const getStats = () => api('/stats/')
export const listTimeline = () => api('/timeline/')
export const listRuns = (id) => api(`/targets/${id}/runs/`)
export const deleteRun = (targetId, runId) => api(`/targets/${targetId}/runs/${runId}/`, { method: 'DELETE' })
export const listFindings = (id) => api(`/targets/${id}/findings/`)
export const deleteFinding = (targetId, findingId) => api(`/targets/${targetId}/findings/${findingId}/`, { method: 'DELETE' })
export const createFinding = (id, payload) => api(`/targets/${id}/findings/`, { method: 'POST', body: payload })
export const listScans = (id) => api(`/targets/${id}/scans/`)
export const runScan = (id, payload) => api(`/targets/${id}/scans/`, { method: 'POST', body: payload })
export const deleteScan = (targetId, scanId) => api(`/targets/${targetId}/scans/${scanId}/`, { method: 'DELETE' })
export const runPlan = (id, selection = {}) => api(`/targets/${id}/run/`, { method: 'POST', body: selection })

// Autonomous agent sessions (tool-using loop; see backend targets/agent_runtime.py).
export const listActiveSessions = () => api('/agent/active/')
export const signupMailboxStatus = () => api('/signup-mailbox/')
export const startAgent = (id, payload) => api(`/targets/${id}/agent/start/`, { method: 'POST', body: payload })
export const approveAgent = (id, runId, payload) => api(`/targets/${id}/agent/${runId}/approve/`, { method: 'POST', body: payload })
export const stopAgent = (id, runId) => api(`/targets/${id}/agent/${runId}/stop/`, { method: 'POST' })
export const listActions = (id, runId) => api(`/targets/${id}/agent/${runId}/actions/`)
export const listReports = (id) => api(`/targets/${id}/reports/`)

// Generic SSE stream: forwards every `data:` frame to onEvent and resolves
// with the final done frame (or null if the stream just ends or our own
// AbortSignal tears it down — teardown is quiet, not an error).
async function sseStream(url, body, onEvent, signal) {
  let res
  try {
    res = await fetch(url, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      ...(signal ? { signal } : {}),
    })
  } catch (err) {
    if (signal && signal.aborted) return null
    throw err
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null)
    throw new Error(data?.detail || `Stream failed (${res.status})`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let final = null
  for (;;) {
    let done, value
    try {
      ({ done, value } = await reader.read())
    } catch (err) {
      if (signal && signal.aborted) return null
      throw err
    }
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split('\n\n')
    buffer = frames.pop()
    for (const frame of frames) {
      if (frame.startsWith(':')) continue // heartbeat comment
      const line = frame.split('\n').find((l) => l.startsWith('data:'))
      if (!line) continue
      const event = JSON.parse(line.slice(5).trim())
      // Only `done` is terminal. Mid-stream `error` frames (LLM hiccups,
      // refused tools surfaced as errors) are informational — forward them
      // so the live feed can render them instead of going silent.
      if (event.type === 'done') final = event
      else onEvent(event)
    }
  }
  return final
}

export function agentStream(id, runId, onEvent, signal) {
  return sseStream(`/api/targets/${id}/agent/${runId}/events/`, undefined, onEvent, signal)
}

// Streaming run: POSTs to the SSE endpoint and calls onToken(text) per chunk.
// `selection` is {provider, model} (OpenCode-style); omitted fields reuse the
// target's saved choice. Resolves with the final event ({type:'done'|'error', ...}).
export async function runPlanStream(id, onToken, selection = {}) {
  const res = await fetch(`/api/targets/${id}/run-stream/`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
    body: JSON.stringify(selection),
  })
  if (!res.ok) {
    const data = await res.json().catch(() => null)
    throw new Error(data?.detail || `Stream failed (${res.status})`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let final = null
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // SSE frames are separated by a blank line; each frame is `data: {...}`.
    const frames = buffer.split('\n\n')
    buffer = frames.pop()
    for (const frame of frames) {
      const line = frame.split('\n').find((l) => l.startsWith('data:'))
      if (!line) continue
      const event = JSON.parse(line.slice(5).trim())
      if (event.type === 'token') onToken(event.text)
      else final = event
    }
  }
  return final
}
