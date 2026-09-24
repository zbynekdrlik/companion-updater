'use strict'

const { test, describe, beforeEach, afterEach } = require('node:test')
const assert = require('node:assert/strict')
const http = require('node:http')
const { ResolumeClient, ResolumeError, findColumnIndex } = require('./resolume')

/**
 * A real HTTP server shaped like Arena 7.27's REST API: one column per
 * GET /api/v1/composition/columns/{n} (404 past the last one), 204 on connect.
 */
function startFakeArena(state) {
	const server = http.createServer((req, res) => {
		state.requests.push(`${req.method} ${req.url}`)
		if (state.hang) return // never answer: simulates a wedged Arena
		const url = req.url
		let m
		if (req.method === 'GET' && url === '/api/v1/product') {
			res.writeHead(200, { 'Content-Type': 'application/json' })
			return res.end(JSON.stringify({ name: 'Arena', major: 7, minor: 27, micro: 1, revision: 15990 }))
		}
		if ((m = url.match(/^\/api\/v1\/composition(?:\/layergroups\/(\d+))?\/columns\/(\d+)(\/connect)?$/))) {
			const group = m[1] ? Number(m[1]) : 0
			const index = Number(m[2])
			const names = state.columns[group] || []
			if (index < 1 || index > names.length) {
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
	})
	return new Promise((resolve) => {
		server.listen(0, '127.0.0.1', () => resolve(server))
	})
}

describe('findColumnIndex', () => {
	test('prefers an exact match, then falls back to case-insensitive', () => {
		const names = ['BLANK', 'ytfast', 'YTFAST', '5MIN']
		assert.equal(findColumnIndex(names, 'YTFAST'), 3)
		assert.equal(findColumnIndex(names, 'ytfast'), 2)
		assert.equal(findColumnIndex(names, '5min'), 4)
		assert.equal(findColumnIndex(names, ' 5MIN '), 4)
		assert.equal(findColumnIndex(names, 'KOSIK'), undefined)
	})
})

describe('ResolumeClient against a fake Arena', () => {
	let server
	let state
	let client

	beforeEach(async () => {
		state = {
			requests: [],
			connected: [],
			hang: false,
			connectStatus: 204,
			columns: {
				0: ['BLANK', 'YTFAST', '5MIN', '1MIN', 'KOSIK'],
				2: ['Blank', 'ytfast', '5min', 'Kosik #'],
			},
		}
		server = await startFakeArena(state)
		const { port } = server.address()
		client = new ResolumeClient({ baseUrl: `http://127.0.0.1:${port}/`, timeoutMs: 300 })
	})

	afterEach(async () => {
		server.closeAllConnections()
		await new Promise((resolve) => server.close(resolve))
	})

	test('product() returns Arena version info', async () => {
		const p = await client.product()
		assert.equal(p.name, 'Arena')
		assert.equal(p.major, 7)
	})

	test('connects a composition column by name with one POST to the right index', async () => {
		const index = await client.connectColumnByName('5MIN')
		assert.equal(index, 3)
		assert.deepEqual(state.connected, [{ group: 0, index: 3 }])
		assert.ok(state.requests.includes('POST /api/v1/composition/columns/3/connect'))
	})

	test('never requests the whole composition', async () => {
		await client.connectColumnByName('KOSIK')
		assert.equal(
			state.requests.filter((r) => r === 'GET /api/v1/composition' || r === 'GET /api/v1/composition/').length,
			0,
		)
	})

	test('matches names case-insensitively when there is no exact match', async () => {
		assert.equal(await client.connectColumnByName('kosik'), 5)
	})

	test('connects a layer-group column by name', async () => {
		const index = await client.connectColumnByName('5min', 2)
		assert.equal(index, 3)
		assert.deepEqual(state.connected, [{ group: 2, index: 3 }])
		assert.ok(state.requests.includes('POST /api/v1/composition/layergroups/2/columns/3/connect'))
	})

	test('a second press reuses the cache: one GET to re-check the column, then the POST', async () => {
		await client.connectColumnByName('1MIN')
		state.requests.length = 0
		await client.connectColumnByName('1MIN')
		assert.deepEqual(state.requests, ['GET /api/v1/composition/columns/4', 'POST /api/v1/composition/columns/4/connect'])
	})

	test('picks up columns moved in Arena after the cache was built', async () => {
		await client.connectColumnByName('KOSIK')
		state.columns[0] = ['BLANK', 'KOSIK', 'YTFAST', '5MIN', '1MIN'] // KOSIK moved from 5 to 2
		const index = await client.connectColumnByName('KOSIK')
		assert.equal(index, 2)
		assert.deepEqual(state.connected.at(-1), { group: 0, index: 2 })
	})

	test('an unknown name throws a ResolumeError naming the column and sends no POST', async () => {
		await assert.rejects(client.connectColumnByName('NOPE'), (err) => {
			assert.ok(err instanceof ResolumeError)
			assert.match(err.message, /No column named "NOPE" in the composition \(5 columns checked\)/)
			return true
		})
		assert.equal(state.connected.length, 0)
	})

	test('an HTTP error on connect is reported, not swallowed', async () => {
		state.connectStatus = 500
		await assert.rejects(client.connectColumnByName('YTFAST'), /answered HTTP 500/)
	})

	test('a wedged Arena fails fast with a timeout instead of hanging the action', async () => {
		state.hang = true
		const started = Date.now()
		await assert.rejects(client.product(), /no answer within 300 ms/)
		assert.ok(Date.now() - started < 2000)
	})

	test('an unreachable Arena is reported with the underlying error', async () => {
		const closed = http.createServer()
		await new Promise((resolve) => closed.listen(0, '127.0.0.1', resolve))
		const { port } = closed.address()
		await new Promise((resolve) => closed.close(resolve)) // nothing listens on `port` now
		const dead = new ResolumeClient({ baseUrl: `http://127.0.0.1:${port}`, timeoutMs: 500 })
		await assert.rejects(dead.connectColumnByName('YTFAST'), (err) => {
			assert.ok(err instanceof ResolumeError)
			assert.match(err.message, new RegExp(`GET http://127\\.0\\.0\\.1:${port}/api/v1/composition/columns/1 failed: .*ECONNREFUSED`))
			return true
		})
	})
})
