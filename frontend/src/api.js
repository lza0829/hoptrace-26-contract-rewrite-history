async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  waybills: () => req('/api/waybills'),
  timeline: (id) => req(`/api/waybills/${encodeURIComponent(id)}/timeline`),
  quarantine: () => req('/api/quarantine'),
  sidecar: () => req('/api/sidecar'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  contract: () => req('/api/contract'),
  contractAdd: (event_code, proj_text) =>
    req('/api/contract/codes', { method: 'POST', body: JSON.stringify({ event_code, proj_text }) }),
  contractRemove: (code) => req(`/api/contract/codes/${encodeURIComponent(code)}`, { method: 'DELETE' }),
  contractProof: (batchId) => req(`/api/contract/proof?batch_id=${encodeURIComponent(batchId)}`),
  proofs: (waybillId) =>
    req(waybillId ? `/api/status-proofs?waybill_id=${encodeURIComponent(waybillId)}` : '/api/status-proofs'),
}
