'use strict'

const http = require('node:http')

/**
 * A real HTTP server shaped like Arena 7.27's REST API, for tests:
 * one column per GET /api/v1/composition[/layergroups/{g}]/columns/{n}
 * (404 past the last one), 204 on POST .../connect, product info.
 *
 * state: { columns: {group: [names]}, requests: [], connected: [],
 *          hang?: bool, connectStatus?: number, delayMs?: (method, url) => ms }
 */
function startFakeArena(state) {
	state.requests = state.requests || []
	state.connected = state.connected || []
	const server = http.createServer((req, res) => {
		state.requests.push(`${req.method} ${req.url}`)
		if (state.hang) return // never answer: simulates a wedged Arena
		const delay = state.delayMs ? state.delayMs(req.method, req.url) : 0
		setTimeout(() => answer(state, req, res), delay)
	})
	return new Promise((resolve) => {
		server.listen(0, '127.0.0.1', () => resolve(server))
	})
}

function answer(state, req, res) {
	const url = req.url
	if (req.method === 'GET' && url === '/api/v1/product') {
		res.writeHead(200, { 'Content-Type': 'application/json' })
		return res.end(JSON.stringify({ name: 'Arena', major: 7, minor: 27, micro: 1, revision: 15990 }))
	}
	const d = url.match(/^\/api\/v1\/composition\/decks\/(\d+)(\/select)?$/)
	if (d) {
		const index = Number(d[1])
		const decks = state.decks || []
		if (index < 1 || index > decks.length) {
			res.writeHead(404, { 'Content-Type': 'application/json' })
			return res.end('{"error":"Deck not found"}')
		}
		if (d[2] && req.method === 'POST') {
			state.selectedDecks = state.selectedDecks || []
			state.selectedDecks.push(index)
			res.writeHead(204)
			return res.end()
		}
		if (req.method === 'GET' && !d[2]) {
			res.writeHead(200, { 'Content-Type': 'application/json' })
			return res.end(JSON.stringify({ id: 5000 + index, name: { valuetype: 'ParamString', value: decks[index - 1] } }))
		}
	}
	const m = url.match(/^\/api\/v1\/composition(?:\/layergroups\/(\d+))?\/columns\/(\d+)(\/connect)?$/)
	if (m) {
		const group = m[1] ? Number(m[1]) : 0
		const index = Number(m[2])
		const names = state.columns[group]
		if (!names || index < 1 || index > names.length) {
			res.writeHead(404, { 'Content-Type': 'application/json' })
			return res.end('{"error":"Column not found"}')
		}
		if (m[3] && req.method === 'POST') {
			if (state.connectStatus && state.connectStatus !== 204) {
				res.writeHead(state.connectStatus)
				return res.end()
			}
			state.connected.push({ group, index })
			res.writeHead(204)
			return res.end()
		}
		if (req.method === 'GET' && !m[3]) {
			res.writeHead(200, { 'Content-Type': 'application/json' })
			return res.end(JSON.stringify({ id: 1000 + index, name: { valuetype: 'ParamString', value: names[index - 1] } }))
		}
	}
	res.writeHead(404)
	res.end()
}

async function stopFakeArena(server) {
	server.closeAllConnections()
	await new Promise((resolve) => server.close(resolve))
}

/** Only these tiny endpoints may ever be called (never /composition itself). */
const ALLOWED_REQUEST = /^(GET \/api\/v1\/product|GET \/api\/v1\/composition\/decks\/\d+|POST \/api\/v1\/composition\/decks\/\d+\/select|GET \/api\/v1\/composition(\/layergroups\/\d+)?\/columns\/\d+|POST \/api\/v1\/composition(\/layergroups\/\d+)?\/columns\/\d+\/connect)$/

module.exports = { startFakeArena, stopFakeArena, ALLOWED_REQUEST }
