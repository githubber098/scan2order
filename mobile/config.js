/**
 * config.js - Central config for the scan2order mobile app.
 *
 * Update BACKEND_URL to your Render deployment URL before building.
 * In dev, point at your local server: 'http://192.168.x.x:8000'
 */

export const BACKEND_URL = 'https://scan2order.reysen.net'

/**
 * Attach the user_id header and content-type to every API call.
 * Pass userId (string) - will be skipped if null/empty.
 */
export function apiHeaders(userId, extra = {}) {
  const h = { 'Content-Type': 'application/json', ...extra }
  if (userId) h['X-User-ID'] = userId
  return h
}

/**
 * Lightweight API helper. Throws on network error; returns parsed JSON.
 * Non-2xx responses are still returned (caller checks .error).
 */
export async function api(path, opts = {}, userId = null) {
  const url = BACKEND_URL + path
  const res = await fetch(url, {
    ...opts,
    headers: { ...apiHeaders(userId), ...(opts.headers || {}) },
  })
  return res.json()
}
