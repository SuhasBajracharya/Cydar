// App shell + routing. View state ({dashboard} | {timeline} | {detail})
// is the router; every screen lives in pages/ and every widget in components/.
import { useCallback, useEffect, useState } from 'react'
import {
  createTarget,
  deleteTarget,
  getStats,
  listTargets,
  logout,
  me,
  updateTarget,
} from './api.js'
import { ErrorBanner, Logo, Modal } from './components/ui.jsx'
import LoginForm from './components/LoginForm.jsx'
import TargetForm from './components/TargetForm.jsx'
import Dashboard from './pages/Dashboard.jsx'
import TargetDetail from './pages/TargetDetail.jsx'
import Timeline from './pages/Timeline.jsx'

export default function App() {
  const [user, setUser] = useState(null)
  const [checking, setChecking] = useState(true)
  const [targetsOpen, setTargetsOpen] = useState(true)
  const [targets, setTargets] = useState([])
  const [stats, setStats] = useState(null)
  const [view, setView] = useState({ name: 'dashboard' })
  const [error, setError] = useState('')
  const [showNew, setShowNew] = useState(false)
  const [editing, setEditing] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const [t, s] = await Promise.all([listTargets(), getStats()])
      setTargets(t); setStats(s)
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    me().then((d) => { setUser(d.username); refresh() }).catch(() => {}).finally(() => setChecking(false))
  }, [refresh])

  async function handleLogout() {
    await logout().catch(() => {})
    setUser(null); setTargets([]); setStats(null); setView({ name: 'dashboard' })
  }

  async function saveTarget(id, payload) {
    if (id) {
      await updateTarget(id, payload)
      setEditing(null)
    } else {
      const t = await createTarget(payload)
      setShowNew(false)
      setView({ name: 'detail', id: t.id })
    }
    await refresh()
  }

  async function removeTarget(id) {
    await deleteTarget(id)
    await refresh()
  }

  if (checking) return <p className="p-8 text-sm text-[#9C9A9A]">Loading…</p>
  if (!user) {
    return (
      <div>
        <LoginForm onLoggedIn={(u) => { setUser(u); refresh() }} setError={setError} />
        {error && <p className="fixed bottom-4 left-1/2 -translate-x-1/2 rounded-lg bg-[#A35E47] px-4 py-2 text-sm text-white">{error}</p>}
      </div>
    )
  }

  const displayName = user.includes('@') ? user.split('@')[0].replace(/[._-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) : user

  return (
    <div className="flex min-h-screen bg-[#FAF8F6]">
      {/* sidebar */}
      <aside className="hidden w-[228px] shrink-0 flex-col bg-black text-[#B9B5B1] md:flex">
        <div className="px-5 pb-6 pt-6">
          <Logo />
          <p className="mt-5 text-[12px] leading-relaxed text-[#8E8A86]">
            Autonomous recon <span className="text-[#A35E47]">→</span><br />PoC <span className="text-[#9C9A9A]">→</span> report
          </p>
        </div>
        <nav className="flex-1 space-y-1 px-3">
          <button
            type="button"
            onClick={() => setView({ name: 'dashboard' })}
            className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-left text-sm font-medium ${view.name === 'dashboard' ? 'bg-[#232323] text-white' : 'text-[#8E8A86] hover:bg-[#171717] hover:text-white'}`}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><rect x="1.5" y="1.5" width="5" height="5" rx="1" stroke="currentColor" /><rect x="9.5" y="1.5" width="5" height="5" rx="1" stroke="currentColor" /><rect x="1.5" y="9.5" width="5" height="5" rx="1" stroke="currentColor" /><rect x="9.5" y="9.5" width="5" height="5" rx="1" stroke="currentColor" /></svg>
            Overview
          </button>
          <button
            type="button"
            onClick={() => setTargetsOpen((o) => !o)}
            className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-left text-sm font-medium ${view.name === 'detail' ? 'bg-[#232323] text-white' : 'text-[#8E8A86] hover:bg-[#171717] hover:text-white'}`}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5.5" stroke="currentColor" /><circle cx="8" cy="8" r="1.8" stroke="currentColor" /></svg>
            Targets
            <span className="ml-auto rounded-full border border-[#333] px-2 text-[11px] text-[#8E8A86]">{targets.length}</span>
            <span className="text-[10px] text-[#777]">{targetsOpen ? '▾' : '▸'}</span>
          </button>
          {targetsOpen && (
            <div className="max-h-64 space-y-0.5 overflow-y-auto px-2 py-1 nice-scroll">
              <button
                type="button"
                onClick={() => setView({ name: 'dashboard' })}
                className={`block w-full rounded-md px-2 py-1.5 text-left text-[11px] font-semibold ${view.name === 'dashboard' ? 'text-white' : 'text-[#777] hover:text-white'}`}
              >
                All targets →
              </button>
              {targets.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setView({ name: 'detail', id: t.id })}
                  className={`block w-full truncate rounded-md px-2 py-1.5 text-left font-mono text-[11px] ${view.name === 'detail' && view.id === t.id ? 'bg-[#A35E47]/20 text-white' : 'text-[#777] hover:text-white'}`}
                >
                  {t.domain}
                </button>
              ))}
              {targets.length === 0 && <p className="px-2 py-1 text-[11px] text-[#555]">No targets yet.</p>}
            </div>
          )}
          <button
            type="button"
            onClick={() => setView({ name: 'timeline' })}
            className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-left text-sm font-medium ${view.name === 'timeline' ? 'bg-[#232323] text-white' : 'text-[#8E8A86] hover:bg-[#171717] hover:text-white'}`}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6" stroke="currentColor" /><path d="M8 4.5V8l2.5 1.5" stroke="currentColor" strokeLinecap="round" /></svg>
            Timeline
          </button>
        </nav>
        <div className="space-y-4 px-5 py-5">
          <div className="border-t border-[#222] pt-4">
            <p className="flex items-center gap-2 text-[12px] font-semibold text-white">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-[#A35E47]" /> Agent online
            </p>
            <p className="mt-0.5 pl-3.5 text-[11px] text-[#777]">All systems nominal</p>
          </div>
          <div className="flex items-center gap-2.5 border-t border-[#222] pt-4">
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-[#464646] text-[11px] font-bold text-white">
              {displayName.slice(0, 2).toUpperCase()}
            </span>
            <div className="min-w-0 flex-1 leading-tight">
              <p className="truncate text-[12px] font-semibold text-white">{displayName}</p>
              <p className="text-[11px] text-[#777]">Security Lead</p>
            </div>
            <button type="button" onClick={handleLogout} title="Log out" className="text-[#777] hover:text-white">⏻</button>
          </div>
        </div>
      </aside>

      {/* main */}
      <div className="min-w-0 flex-1">
        <header className="border-b border-[#E9E1D9] bg-white px-4 py-3 md:hidden">
          <div className="flex items-center justify-between">
            <span className="flex items-center gap-2 rounded-md bg-black px-2 py-1 text-sm font-bold text-white"><img src="/cydar-logo.png" alt="Cydar" className="h-6 w-auto object-contain" /> Cydar</span>
            <span className="text-xs text-[#464646]">{user} <button type="button" className="ml-2 font-medium text-[#A35E47] hover:underline" onClick={handleLogout}>Log out</button></span>
          </div>
        </header>
        <main className="mx-auto max-w-[1200px] p-4 md:p-8">
          <ErrorBanner message={error} onClear={() => setError('')} />
          {view.name === 'dashboard' && (
            <Dashboard
              targets={targets} stats={stats} refresh={refresh} setError={setError}
              userName={displayName}
              onOpen={(id) => setView({ name: 'detail', id })}
              onNew={() => setShowNew(true)}
              onEdit={(t) => setEditing(t)}
              onDelete={removeTarget}
            />
          )}
          {view.name === 'timeline' && (
            <Timeline
              setError={setError}
              onOpenTarget={(id) => setView({ name: 'detail', id })}
            />
          )}
          {view.name === 'detail' && (
            <TargetDetail
              key={view.id}
              targetId={view.id} setError={setError}
              onBack={() => { setView({ name: 'dashboard' }); refresh() }}
              onEdited={refresh}
              onOpenTarget={(id) => setView({ name: 'detail', id })}
            />
          )}
        </main>
      </div>

      {showNew && (
        <Modal title="Add target" onClose={() => setShowNew(false)}>
          <TargetForm submitText="Add target" setError={setError} onSubmit={(p) => saveTarget(null, p)} />
        </Modal>
      )}
      {editing && (
        <Modal title={`Edit ${editing.domain}`} onClose={() => setEditing(null)}>
          <TargetForm initial={editing} submitText="Save changes" setError={setError} onSubmit={(p) => saveTarget(editing.id, p)} />
        </Modal>
      )}
    </div>
  )
}
