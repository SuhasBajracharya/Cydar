// Providers management page matching user's wireframe:
// - Header with "API Providers", search bar, "+ Add" button, and "Refresh" button
// - Single card layout per line (only configured providers are shown!)
// - Each card has: Provider title, status, API key input (or status), Base URL, Curated models,
//   Remote discovery button, Test connection button, Remove button, Save button.
// - "+ Add" opens a modal listing all available unconfigured providers to add.
import { useCallback, useEffect, useState } from 'react'
import {
  deleteProviderConfig,
  fetchProviderModels,
  listProviders,
  saveProviderConfig,
  testProviderKey,
} from '../api.js'
import { Modal } from '../components/ui.jsx'
import { btn, btnSecondary, card, input, labelCls, miniBtn } from '../lib/styles.js'

export default function Providers({ setError }) {
  const [allProviders, setAllProviders] = useState([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [showAddModal, setShowAddModal] = useState(false)
  const [selectedAddId, setSelectedAddId] = useState('')

  // Per-card state
  const [inputs, setInputs] = useState({}) // { [pid]: { api_key, base_url } }
  const [saving, setSaving] = useState({}) // { [pid]: bool }
  const [testing, setTesting] = useState({}) // { [pid]: bool }
  const [fetching, setFetching] = useState({}) // { [pid]: bool }
  const [testResults, setTestResults] = useState({}) // { [pid]: { success, detail } }
  const [remoteModels, setRemoteModels] = useState({}) // { [pid]: { models, error, open } }

  const load = useCallback(async () => {
    try {
      setLoading(true)
      const data = await listProviders()
      setAllProviders(data)

      const initInputs = {}
      for (const p of data) {
        initInputs[p.id] = {
          api_key: '',
          base_url: p.base_url || p.default_url || '',
        }
      }
      setInputs((prev) => ({ ...initInputs, ...prev }))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [setError])

  useEffect(() => {
    load()
  }, [load])

  function handleInputChange(pid, field, value) {
    setInputs((prev) => ({
      ...prev,
      [pid]: {
        ...prev[pid],
        [field]: value,
      },
    }))
  }

  async function handleSave(pid) {
    const pInput = inputs[pid] || {}
    const pSpec = allProviders.find((p) => p.id === pid)
    if (!pInput.api_key && pSpec?.requires_key !== false && !pSpec?.configured) {
      setError(`Please enter an API key for ${pSpec?.name || pid}`)
      return
    }

    try {
      setSaving((prev) => ({ ...prev, [pid]: true }))
      await saveProviderConfig(pid, {
        api_key: pInput.api_key,
        base_url: pInput.base_url,
      })
      // Clear key input after successful save (write-only)
      setInputs((prev) => ({
        ...prev,
        [pid]: {
          ...prev[pid],
          api_key: '',
        },
      }))
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving((prev) => ({ ...prev, [pid]: false }))
    }
  }

  async function handleRemove(pid) {
    const pSpec = allProviders.find((p) => p.id === pid)
    if (!window.confirm(`Remove ${pSpec?.name || pid}?`)) {
      return
    }
    try {
      setSaving((prev) => ({ ...prev, [pid]: true }))
      await deleteProviderConfig(pid)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving((prev) => ({ ...prev, [pid]: false }))
    }
  }

  async function handleAddProvider(pid) {
    const pInput = inputs[pid] || {}
    const pSpec = allProviders.find((p) => p.id === pid)
    if (!pInput.api_key && pSpec?.requires_key !== false) {
      setError(`Please enter an API key to add ${pSpec?.name || pid}`)
      return
    }
    try {
      setSaving((prev) => ({ ...prev, [pid]: true }))
      await saveProviderConfig(pid, {
        api_key: pInput.api_key || '',
        base_url: pInput.base_url || '',
      })
      setShowAddModal(false)
      setSelectedAddId('')
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving((prev) => ({ ...prev, [pid]: false }))
    }
  }

  async function handleTest(pid) {
    try {
      setTesting((prev) => ({ ...prev, [pid]: true }))
      setTestResults((prev) => ({ ...prev, [pid]: null }))
      const res = await testProviderKey(pid)
      setTestResults((prev) => ({ ...prev, [pid]: res }))
    } catch (err) {
      setTestResults((prev) => ({
        ...prev,
        [pid]: { success: false, detail: err.message },
      }))
    } finally {
      setTesting((prev) => ({ ...prev, [pid]: false }))
    }
  }

  async function handleFetchModels(pid) {
    try {
      setFetching((prev) => ({ ...prev, [pid]: true }))
      const res = await fetchProviderModels(pid)
      setRemoteModels((prev) => ({
        ...prev,
        [pid]: {
          models: res.models || [],
          error: res.error,
          open: true,
        },
      }))
    } catch (err) {
      setRemoteModels((prev) => ({
        ...prev,
        [pid]: {
          models: [],
          error: err.message,
          open: true,
        },
      }))
    } finally {
      setFetching((prev) => ({ ...prev, [pid]: false }))
    }
  }

  function toggleModelsList(pid) {
    setRemoteModels((prev) => ({
      ...prev,
      [pid]: {
        ...prev[pid],
        open: !prev[pid]?.open,
      },
    }))
  }

  // ONLY show configured providers in the main list!
  const configuredProviders = allProviders.filter((p) => p.configured)
  // Providers that can still be added via modal
  const unconfiguredProviders = allProviders.filter((p) => !p.configured)

  // Search filter applied over configured providers
  const filtered = configuredProviders.filter((p) => {
    if (!search.trim()) return true
    const q = search.toLowerCase()
    return p.name.toLowerCase().includes(q) || p.id.toLowerCase().includes(q)
  })

  return (
    <div className="space-y-6">
      {/* Top Header matching wireframe: [API Providers] [Search Provider] [+ Add] [Refresh] */}
      <div className="flex flex-col gap-4 border-b border-[#E9E1D9] pb-5 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-[#1C1A18]">API Providers</h1>
          <p className="mt-1 text-sm text-[#777]">
            Active LLM API providers configured in the database.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          {/* Search Provider Input */}
          <div className="relative min-w-[200px]">
            <input
              type="text"
              placeholder="Search Provider…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className={`${input} py-1.5 pl-8 text-xs`}
            />
            <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-xs text-[#9C9A9A]">
              🔍
            </span>
            {search && (
              <button
                type="button"
                onClick={() => setSearch('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-[#9C9A9A] hover:text-[#1C1A18]"
              >
                ✕
              </button>
            )}
          </div>

          {/* + Add Button */}
          <button
            type="button"
            onClick={() => {
              setSelectedAddId(unconfiguredProviders[0]?.id || '')
              setShowAddModal(true)
            }}
            className={btn}
          >
            + Add
          </button>

          {/* Refresh Button */}
          <button
            type="button"
            onClick={load}
            disabled={loading}
            className={btnSecondary}
          >
            Refresh
          </button>
        </div>
      </div>

      {/* Main Content: Single card layout per line */}
      {loading && allProviders.length === 0 ? (
        <div className="flex h-48 items-center justify-center">
          <p className="text-sm text-[#9C9A9A]">Loading providers…</p>
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-xl border border-dashed border-[#DED7CE] p-12 text-center">
          <p className="text-base font-medium text-[#464646]">
            {configuredProviders.length === 0
              ? 'No API providers configured yet'
              : 'No matching providers found'}
          </p>
          <p className="mt-1 text-xs text-[#8E8A86]">
            {configuredProviders.length === 0
              ? 'Click "+ Add" in the top bar to configure an API key for DeepSeek, OpenRouter, NVIDIA NIM, or another provider.'
              : 'Try clearing your search query.'}
          </p>
          {configuredProviders.length === 0 && (
            <button
              type="button"
              onClick={() => {
                setSelectedAddId(unconfiguredProviders[0]?.id || '')
                setShowAddModal(true)
              }}
              className={`${btn} mt-4`}
            >
              + Add Provider
            </button>
          )}
        </div>
      ) : (
        <div className="space-y-4">
          {filtered.map((p) => {
            const currentInput = inputs[p.id] || { api_key: '', base_url: '' }
            const isSaving = saving[p.id]
            const isTesting = testing[p.id]
            const isFetching = fetching[p.id]
            const testRes = testResults[p.id]
            const fetched = remoteModels[p.id]

            return (
              /* Single full-width card layout per line matching wireframe */
              <div key={p.id} className={`${card} w-full space-y-4`}>
                {/* Card Title & Top Info Bar */}
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#F0EAE1] pb-3">
                  <div className="flex items-center gap-3">
                    <h2 className="text-lg font-bold text-[#1C1A18]">{p.name}</h2>
                    {p.requires_key === false ? (
                      <span className="inline-flex items-center gap-1 rounded-full bg-[#E9EFE7] px-2.5 py-0.5 text-xs font-semibold text-[#3F5A3C]">
                        <span className="h-1.5 w-1.5 rounded-full bg-[#3F5A3C]" /> Local
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 rounded-full bg-[#E9EFE7] px-2.5 py-0.5 text-xs font-semibold text-[#3F5A3C]">
                        <span className="h-1.5 w-1.5 rounded-full bg-[#3F5A3C]" /> Configured
                      </span>
                    )}
                  </div>

                  {/* Actions: Remove and Save matching wireframe */}
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => handleRemove(p.id)}
                      disabled={isSaving}
                      className="rounded-lg border border-[#EFC5BE] bg-[#FDF0EE] px-3 py-1.5 text-xs font-semibold text-[#96382B] hover:bg-[#FBE5E1] disabled:opacity-50"
                    >
                      Remove
                    </button>
                    <button
                      type="button"
                      onClick={() => handleSave(p.id)}
                      disabled={isSaving}
                      className={btn}
                    >
                      {isSaving ? 'Saving…' : 'Save'}
                    </button>
                  </div>
                </div>

                {/* Form fields in single card layout */}
                <div className="grid gap-4 sm:grid-cols-2">
                  {/* API Key */}
                  {p.requires_key !== false && (
                    <div>
                      <label className="block">
                        <span className={labelCls}>API Key</span>
                        <input
                          type="password"
                          className={`${input} mt-1`}
                          placeholder="•••••••••••••••••••••••• (leave blank to keep current)"
                          value={currentInput.api_key}
                          onChange={(e) => handleInputChange(p.id, 'api_key', e.target.value)}
                        />
                      </label>
                    </div>
                  )}

                  {/* Base URL */}
                  <div>
                    <label className="block">
                      <span className={labelCls}>Base URL</span>
                      <input
                        type="text"
                        className={`${input} mt-1 font-mono text-xs`}
                        placeholder={p.default_url}
                        value={currentInput.base_url}
                        onChange={(e) => handleInputChange(p.id, 'base_url', e.target.value)}
                      />
                    </label>
                  </div>
                </div>

                {/* Curated Models */}
                <div>
                  <span className={labelCls}>Curated Models ({p.models?.length || 0})</span>
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {p.models && p.models.length > 0 ? (
                      p.models.map((m) => (
                        <span
                          key={m}
                          className={`rounded border border-[#E9E1D9] bg-[#FAF8F6] px-2 py-0.5 font-mono text-[11px] ${
                            m === p.default_model ? 'border-[#A35E47]/40 font-semibold text-[#A35E47]' : 'text-[#555]'
                          }`}
                        >
                          {m} {m === p.default_model && '★'}
                        </span>
                      ))
                    ) : (
                      <span className="text-[11px] italic text-[#9C9A9A]">No preset models</span>
                    )}
                  </div>
                </div>

                {/* Secondary tools: Test connection & Live model discovery */}
                <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[#F0EAE1] bg-[#FAF8F6] p-2.5">
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      className={miniBtn}
                      disabled={isTesting}
                      onClick={() => handleTest(p.id)}
                    >
                      {isTesting ? 'Testing…' : 'Test connection'}
                    </button>
                    <button
                      type="button"
                      className={miniBtn}
                      disabled={isFetching}
                      onClick={() => handleFetchModels(p.id)}
                    >
                      {isFetching ? 'Fetching…' : 'Fetch live models'}
                    </button>
                  </div>

                  {testRes && (
                    <span
                      className={`text-xs font-medium ${
                        testRes.success ? 'text-[#2F6B28]' : 'text-[#96382B]'
                      }`}
                    >
                      {testRes.success ? '✓' : '✗'} {testRes.detail}
                    </span>
                  )}
                </div>

                {/* Remote models expanded drawer */}
                {fetched && (
                  <div className="rounded-lg border border-[#E2DAD2] bg-white p-3 text-xs">
                    {fetched.error ? (
                      <p className="text-[#96382B]">{fetched.error}</p>
                    ) : (
                      <div>
                        <div className="flex items-center justify-between text-[11px] text-[#777]">
                          <span>Found {fetched.models.length} accessible model(s) on provider</span>
                          <button
                            type="button"
                            onClick={() => toggleModelsList(p.id)}
                            className="font-medium text-[#A35E47] hover:underline"
                          >
                            {fetched.open ? 'Hide' : 'Show all'}
                          </button>
                        </div>
                        {fetched.open && (
                          <div className="mt-2 max-h-40 overflow-y-auto rounded border border-[#E2DAD2] bg-[#FAF8F6] p-2 font-mono text-[11px] nice-scroll">
                            {fetched.models.map((m) => (
                              <div key={m} className="rounded px-1.5 py-0.5 text-[#333] hover:bg-white">
                                {m}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Add Provider Modal */}
      {showAddModal && (
        <Modal title="Add API Provider" onClose={() => setShowAddModal(false)}>
          <div className="space-y-4">
            <p className="text-xs text-[#777]">
              Select a provider from the registry and supply its API key.
            </p>

            {unconfiguredProviders.length === 0 ? (
              <p className="py-4 text-center text-xs text-[#9C9A9A]">
                All supported providers are already configured!
              </p>
            ) : (
              <div className="space-y-3.5">
                <div>
                  <label className="block">
                    <span className={labelCls}>Provider</span>
                    <select
                      className={`${input} mt-1`}
                      value={selectedAddId}
                      onChange={(e) => setSelectedAddId(e.target.value)}
                    >
                      {unconfiguredProviders.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                {selectedAddId && (() => {
                  const pSpec = unconfiguredProviders.find((p) => p.id === selectedAddId)
                  const currentInput = inputs[selectedAddId] || { api_key: '', base_url: '' }

                  return (
                    <>
                      {pSpec?.requires_key !== false && (
                        <div>
                          <label className="block">
                            <span className={labelCls}>API Key</span>
                            <input
                              type="password"
                              className={`${input} mt-1`}
                              placeholder="Enter API key…"
                              value={currentInput.api_key}
                              onChange={(e) => handleInputChange(selectedAddId, 'api_key', e.target.value)}
                            />
                          </label>
                        </div>
                      )}

                      <div>
                        <label className="block">
                          <span className={labelCls}>Base URL (Optional override)</span>
                          <input
                            type="text"
                            className={`${input} mt-1 font-mono text-xs`}
                            placeholder={pSpec?.default_url}
                            value={currentInput.base_url}
                            onChange={(e) => handleInputChange(selectedAddId, 'base_url', e.target.value)}
                          />
                        </label>
                      </div>

                      <div className="flex justify-end gap-2 pt-2">
                        <button
                          type="button"
                          className={btnSecondary}
                          onClick={() => setShowAddModal(false)}
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          className={btn}
                          disabled={saving[selectedAddId]}
                          onClick={() => handleAddProvider(selectedAddId)}
                        >
                          {saving[selectedAddId] ? 'Adding…' : 'Add Provider'}
                        </button>
                      </div>
                    </>
                  )
                })()}
              </div>
            )}
          </div>
        </Modal>
      )}
    </div>
  )
}
