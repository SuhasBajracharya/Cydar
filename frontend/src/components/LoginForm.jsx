// Login gate. Props: onLoggedIn(username), setError(message).
import { useState } from 'react'
import { login } from '../api.js'

export default function LoginForm({ onLoggedIn, setError }) {
  const [username, setUsername] = useState('security@acme.co')
  const [password, setPassword] = useState('password123')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    try {
      const data = await login(username, password)
      onLoggedIn(data.username)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen flex-col bg-white text-[#1C1A18]">
      <main className="flex flex-1 items-center justify-center px-4 py-16">
        <div className="w-full max-w-[430px] rounded-2xl bg-black p-8 text-white shadow-2xl">
          <h1 className="flex flex-wrap items-center gap-x-3 gap-y-2 text-[26px] font-medium">
            Sign in to
            <img src="/cydar-logo.png" alt="Cydar" className="h-9 w-auto object-contain" />
          </h1>
          <p className="mt-1.5 text-[13px] leading-relaxed text-[#9C9A9A]">
            Continue autonomous security research across your approved targets.
          </p>
          <form onSubmit={submit} className="mt-6 space-y-4">
            <div>
              <label className="mb-1.5 block text-[11px] font-semibold tracking-wide text-[#B9B5B1]">Username</label>
              <input
                className="w-full rounded-lg border border-[#333] bg-[#171717] px-3 py-2.5 text-sm font-semibold text-white placeholder:text-[#666] focus:border-[#A35E47] focus:outline-none focus:ring-2 focus:ring-[#A35E47]/30"
                placeholder="security@acme.co" value={username}
                onChange={(e) => setUsername(e.target.value)} autoComplete="username"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-[11px] font-semibold tracking-wide text-[#B9B5B1]">Password</label>
              <input
                className="w-full rounded-lg border border-[#333] bg-[#171717] px-3 py-2.5 text-sm text-white placeholder:text-[#666] focus:border-[#A35E47] focus:outline-none focus:ring-2 focus:ring-[#A35E47]/30"
                type="password" placeholder="••••••••••" value={password}
                onChange={(e) => setPassword(e.target.value)} autoComplete="current-password"
              />
            </div>
            <button
              className="w-full rounded-lg bg-[#A35E47] py-3 text-sm font-semibold text-white hover:bg-[#8E513D] disabled:opacity-60"
              disabled={busy} type="submit"
            >
              {busy ? 'Signing in…' : 'Login  →'}
            </button>
          </form>
          <p className="mt-5 text-center text-[11px] text-[#8E8A86]">
            <span className="mr-1.5 rounded border border-[#333] px-1.5 py-0.5 text-[9px] font-bold text-[#A35E47]">DEV</span>
            Use any credentials to access the workspace
          </p>
        </div>
      </main>
      <footer className="flex items-center justify-center pb-6">
        <p className="text-[10px] tracking-[0.16em] text-[#9C9A9A]">PRIVATE BETA · BUILT FOR RESPONSIBLE DISCLOSURE</p>
      </footer>
    </div>
  )
}
