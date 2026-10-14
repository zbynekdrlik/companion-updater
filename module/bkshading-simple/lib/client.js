'use strict'

/**
 * Minimal client for the bkshading service API (zbynekdrlik/camera-box,
 * bkshading/service/src/http.rs):
 *   GET /api/cameras               -> { version, cameras: [CameraView] }
 *   PUT /api/cameras/:id/params    <- SetRequest (absolute values)  -> { ok: true }
 * The service forwards a PUT to the camera's relay and answers 502 with the
 * relay's error text when the relay or camera cannot take it, 404 for an
 * unknown camera id.
 */

/**
 * Measured on companion-pp, 2026-10-14 (companion-updater#22): a PUT of one
 * ISO value answered in 0.40 s and the next GET already showed it. The
 * service itself waits up to 10 s for the relay on a write (aggregator.rs
 * `write_client`), and a write it completes must never be reported here as
 * failed: the next queued step would then start from the old value and send
 * the same target again. So the write timeout is the service's 10 s plus a
 * margin.
 */
const POLL_TIMEOUT_MS = 3000
const WRITE_TIMEOUT_MS = 12000

class BkshadingError extends Error {
	constructor(message) {
		super(message)
		this.name = 'BkshadingError'
	}
}

function errorText(err) {
	if (!err) return 'unknown error'
	const cause = err.cause && (err.cause.code || err.cause.message)
	return cause ? `${err.message} (${cause})` : err.message
}

class BkshadingClient {
	/**
	 * @param {object} opts
	 * @param {string} opts.baseUrl e.g. http://strih.lan:8770
	 * @param {number} [opts.pollTimeoutMs]
	 * @param {number} [opts.writeTimeoutMs]
	 * @param {typeof fetch} [opts.fetchImpl]
	 */
	constructor({ baseUrl, pollTimeoutMs = POLL_TIMEOUT_MS, writeTimeoutMs = WRITE_TIMEOUT_MS, fetchImpl = globalThis.fetch }) {
		this.baseUrl = baseUrl.replace(/\/+$/, '')
		this.pollTimeoutMs = pollTimeoutMs
		this.writeTimeoutMs = writeTimeoutMs
		this.fetch = fetchImpl
	}

	async request(method, path, body, timeoutMs) {
		const url = `${this.baseUrl}${path}`
		let res
		try {
			const init = { method, signal: AbortSignal.timeout(timeoutMs) }
			if (body !== undefined) {
				init.headers = { 'Content-Type': 'application/json' }
				init.body = JSON.stringify(body)
			}
			res = await this.fetch(url, init)
		} catch (err) {
			const reason = err && err.name === 'TimeoutError' ? `no answer within ${timeoutMs} ms` : errorText(err)
			throw new BkshadingError(`${method} ${url} failed: ${reason}`)
		}
		let text
		try {
			text = await res.text()
		} catch (err) {
			throw new BkshadingError(`${method} ${url}: reading the answer failed: ${errorText(err)}`)
		}
		if (!res.ok) {
			const detail = text.trim().slice(0, 300)
			throw new BkshadingError(`${method} ${url} answered HTTP ${res.status}${detail ? `: ${detail}` : ''}`)
		}
		try {
			return JSON.parse(text)
		} catch {
			throw new BkshadingError(`${method} ${url} answered something that is not JSON: ${text.slice(0, 120)}`)
		}
	}

	/** The service's aggregate: { version, cameras: [...] }. */
	async cameras() {
		const body = await this.request('GET', '/api/cameras', undefined, this.pollTimeoutMs)
		if (!body || !Array.isArray(body.cameras)) {
			throw new BkshadingError(`GET ${this.baseUrl}/api/cameras: no "cameras" list in the answer`)
		}
		return body
	}

	/** Write absolute params to one camera. */
	async setParams(id, patch) {
		return this.request('PUT', `/api/cameras/${encodeURIComponent(id)}/params`, patch, this.writeTimeoutMs)
	}
}

module.exports = { BkshadingClient, BkshadingError, POLL_TIMEOUT_MS, WRITE_TIMEOUT_MS }
