'use strict'

const http = require('node:http')

/**
 * A real HTTP server shaped like the bkshading service API
 * (camera-box bkshading/service/src/http.rs, v1.7.0-dev.785), for tests.
 *
 *   GET /api/cameras              -> { version, cameras } as they were when the
 *                                    request ARRIVED (a delayed answer is old)
 *   PUT /api/cameras/:id/params   -> applies the absolute values like the relay
 *                                    projection does, answers { ok: true };
 *                                    404 unknown camera, 502 relay down
 *
 * state: { cameras: [CameraView], requests: [], writes: [],
 *          hang?: bool, putStatus?: number, delayMs?: (method, url) => ms }
 */
function startFakeBkshading(state) {
	state.requests = state.requests || []
	state.writes = state.writes || []
	const server = http.createServer((req, res) => {
		let body = ''
		req.on('data', (chunk) => (body += chunk))
		req.on('end', () => {
			state.requests.push(`${req.method} ${req.url}`)
			if (state.hang) return // never answer: a wedged service
			// The service builds its answer from what it knows NOW; a slow answer is old news.
			const snapshot = req.method === 'GET' ? JSON.stringify({ version: '1.7.0-dev.785', cameras: state.cameras }) : null
			const delay = state.delayMs ? state.delayMs(req.method, req.url) : 0
			setTimeout(() => answer(state, req, res, body, snapshot), delay)
		})
	})
	return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)))
}

function json(res, status, value) {
	res.writeHead(status, { 'Content-Type': 'application/json' })
	res.end(JSON.stringify(value))
}

function answer(state, req, res, body, snapshot) {
	if (req.method === 'GET' && req.url === '/api/cameras') {
		res.writeHead(200, { 'Content-Type': 'application/json' })
		return res.end(snapshot)
	}
	const m = req.url.match(/^\/api\/cameras\/([^/]+)\/params$/)
	if (m && req.method === 'PUT') {
		const id = decodeURIComponent(m[1])
		const cam = state.cameras.find((c) => c.id === id)
		if (!cam) {
			res.writeHead(404, { 'Content-Type': 'text/plain' })
			return res.end(`no camera '${id}'`)
		}
		if (state.putStatus && state.putStatus !== 200) {
			res.writeHead(state.putStatus, { 'Content-Type': 'text/plain' })
			return res.end('relay cam: connection refused')
		}
		if (!cam.reachable || !cam.state) {
			res.writeHead(502, { 'Content-Type': 'text/plain' })
			return res.end(`relay ${id} unreachable`)
		}
		const patch = JSON.parse(body)
		state.writes.push({ id, patch })
		const p = cam.state.params
		if (patch.iso !== undefined) p.iso = patch.iso
		if (patch.kelvin !== undefined) p.kelvin = patch.kelvin
		if (patch.tint !== undefined) p.tint = patch.tint
		if (patch.apertureNorm !== undefined) {
			const choices = cam.state.caps.fNumberChoices
			const idx = Math.round(patch.apertureNorm * (choices.length - 1))
			p.apertureNorm = patch.apertureNorm
			p.apertureAv = 2 * Math.log2(choices[idx])
		}
		return json(res, 200, { ok: true })
	}
	res.writeHead(404)
	res.end()
}

function stopFakeBkshading(server) {
	return new Promise((resolve) => {
		server.closeAllConnections?.()
		server.close(() => resolve())
	})
}

/** cam6 on companion-pp as bkshading reported it on 2026-10-14 (companion-updater#22). */
function ppCam6() {
	return {
		id: 'cam6',
		label: 'Cam 6',
		transport: 'cambox-relay',
		hasPreview: true,
		previewLive: true,
		reachable: true,
		grabFps: 60,
		grabFpsDesync: false,
		fpsSync: 'mismatch',
		fpsAlignSettable: false,
		state: {
			online: true,
			camera: 'USB PTP Class Camera',
			params: {
				apertureAv: 4.526068811667588,
				apertureNorm: 0.2307692307692308,
				iso: 1600,
				kelvin: 5000,
				tint: 0,
				shutter: 83,
				fps100: 5000,
				sensorFps100: 6000,
				focusDistance: 0,
			},
			caps: {
				isoChoices: [25, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000, 2500, 3200, 4000, 5000, 6400, 8000, 10000, 12800, 16000],
				fNumberChoices: [2.8, 3.2, 3.5, 3.7, 4.0, 4.5, 4.8, 5.2, 5.6, 6.2, 6.7, 7.3, 8.0, 8.7, 9.5, 10.0, 11.0, 12.0, 14.0, 15.0, 16.0, 17.0, 19.0, 21.0, 22.0, 25.0, 27.0],
				shutterChoices: [50, 60, 80, 100, 120, 125, 160, 200, 240, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000],
				fpsChoices: [],
				fpsMin: 5,
				fpsMax: 60,
				kelvinMin: 2500,
				kelvinMax: 10000,
			},
			fpsSupported: true,
			captureFps: 60,
			version: '1.7.0-dev.755',
			notApplied: [],
		},
	}
}

/** A camera whose relay is up but has no camera on USB (pp cam4 on 2026-10-14). */
function offlineCam(id) {
	return {
		id,
		label: `Cam ${id.replace(/\D/g, '')}`,
		transport: 'cambox-relay',
		reachable: true,
		state: { online: false, camera: null, params: { apertureAv: null, apertureNorm: null, iso: null, kelvin: null, tint: null }, caps: null, notApplied: [] },
	}
}

/** A camera whose relay does not answer (snv cameras on 2026-10-14). */
function unreachableCam(id) {
	return { id, label: `Cam ${id.replace(/\D/g, '')}`, transport: 'cambox-relay', reachable: false, state: null }
}

module.exports = { startFakeBkshading, stopFakeBkshading, ppCam6, offlineCam, unreachableCam }
