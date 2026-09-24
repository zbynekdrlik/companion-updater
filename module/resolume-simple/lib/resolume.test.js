'use strict'

const { test, describe, beforeEach, afterEach } = require('node:test')
const assert = require('node:assert/strict')
const http = require('node:http')
const { ResolumeClient, ResolumeError, findColumnIndex } = require('./resolume')
const { startFakeArena, stopFakeArena, ALLOWED_REQUEST } = require('../testing/fake-arena')

describe('findColumnIndex', () => {
	test('prefers an exact match anywhere, then falls back to case-insensitive', () => {
		const names = ['BLANK', 'ytfast', 'YTFAST', '5MIN']
		assert.equal(findColumnIndex(names, 'YTFAST'), 3)
		assert.equal(findColumnIndex(names, 'ytfast'), 2)
		assert.equal(findColumnIndex(names, '5min'), 4)
		assert.equal(findColumnIndex(names, ' 5MIN '), 4)
		assert.equal(findColumnIndex(names, 'KOSIK'), undefined)
	})

	test('"#" in a name also matches the column number Arena shows for it', () => {
		const names = ['Blank', 'ytfast', 'Kosik #', 'Column #', 'Column #']
		assert.equal(findColumnIndex(names, 'Kosik #'), 3)
		assert.equal(findColumnIndex(names, 'kosik 3'), 3)
		assert.equal(findColumnIndex(names, 'Column 5'), 5)
		assert.equal(findColumnIndex(names, 'Column 4'), 4)
	})

	test('an empty name never matches, not even an unnamed column', () => {
		assert.equal(findColumnIndex(['', 'x'], ''), undefined)
		assert.equal(findColumnIndex(['', 'x'], '   '), undefined)
	})
})

describe('ResolumeClient against a fake Arena', () => {
	let server
	let state
	let client

	beforeEach(async () => {
		state = {
			columns: {
				0: ['BLANK', 'YTFAST', '5MIN', '1MIN', 'KOSIK'],
				2: ['Blank', 'ytfast', '5min', 'Kosik #'],
			},
			decks: ['NewLevel', 'NewLevel 2', 'GoodFest SNV'],
		}
		server = await startFakeArena(state)
		client = new ResolumeClient({ baseUrl: `http://127.0.0.1:${server.address().port}/`, timeoutMs: 300 })
	})

	afterEach(async () => {
		await stopFakeArena(server)
	})

	test('product() returns Arena version info', async () => {
		const p = await client.product()
		assert.equal(p.name, 'Arena')
		assert.equal(p.major, 7)
	})

	test('connects a composition column by name with one POST to the right index', async () => {
		assert.deepEqual(await client.connectColumnByName('5MIN'), { index: 3, superseded: false })
		assert.deepEqual(state.connected, [{ group: 0, index: 3 }])
		assert.ok(state.requests.includes('POST /api/v1/composition/columns/3/connect'))
	})

	test('only ever calls the tiny endpoints (never the whole composition)', async () => {
		await client.product()
		await client.connectColumnByName('KOSIK')
		await client.connectColumnByName('kosik 4', 2)
		await assert.rejects(client.connectColumnByName('NOPE'))
		for (const r of state.requests) assert.match(r, ALLOWED_REQUEST)
	})

	test('matches names case-insensitively when there is no exact match', async () => {
		assert.equal((await client.connectColumnByName('kosik')).index, 5)
	})

	test('connects a layer-group column by name, including a "#" name', async () => {
		assert.equal((await client.connectColumnByName('YTFAST', 2)).index, 2)
		assert.equal((await client.connectColumnByName('Kosik #', 2)).index, 4)
		assert.deepEqual(state.connected, [
			{ group: 2, index: 2 },
			{ group: 2, index: 4 },
		])
		assert.ok(state.requests.includes('POST /api/v1/composition/layergroups/2/columns/4/connect'))
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
		assert.equal((await client.connectColumnByName('KOSIK')).index, 2)
		assert.deepEqual(state.connected.at(-1), { group: 0, index: 2 })
	})

	test('picks up a cached column that no longer exists (404) by rescanning', async () => {
		await client.connectColumnByName('KOSIK') // cached as column 5
		state.columns[0] = ['KOSIK', 'BLANK'] // column 5 is gone
		assert.equal((await client.connectColumnByName('KOSIK')).index, 1)
	})

	test('the LAST press wins when an earlier press is still resolving its name', async () => {
		await client.connectColumnByName('BLANK') // warm the cache; 5MIN will need a slow rescan
		state.connected.length = 0
		state.columns[0] = ['BLANK', 'YTFAST', 'XX', '1MIN', 'KOSIK', '5MIN'] // 5MIN moved: forces a rescan
		state.delayMs = (method, url) => (method === 'GET' && /\/columns\/6$/.test(url) ? 150 : 0)
		const first = client.connectColumnByName('5MIN') // slow: rescans up to column 6
		await new Promise((r) => setTimeout(r, 20))
		const second = client.connectColumnByName('BLANK') // fast: cached
		assert.deepEqual(await second, { index: 1, superseded: false })
		assert.deepEqual(await first, { index: 6, superseded: true })
		assert.deepEqual(state.connected, [{ group: 0, index: 1 }])
	})

	test('selects a deck by name, case-insensitively, with one POST', async () => {
		assert.deepEqual(await client.selectDeckByName('goodfest snv'), { index: 3, superseded: false })
		assert.deepEqual(state.selectedDecks, [3])
		assert.ok(state.requests.includes('POST /api/v1/composition/decks/3/select'))
		for (const r of state.requests) assert.match(r, ALLOWED_REQUEST)
	})

	test('deck and column presses do not supersede each other', async () => {
		const [col, deck] = await Promise.all([client.connectColumnByName('5MIN'), client.selectDeckByName('NewLevel 2')])
		assert.equal(col.superseded, false)
		assert.equal(deck.superseded, false)
		assert.deepEqual(state.connected, [{ group: 0, index: 3 }])
		assert.deepEqual(state.selectedDecks, [2])
	})

	test('the last press wins even when that newer press then fails: nothing is sent', async () => {
		await client.connectColumnByName('BLANK')
		state.connected.length = 0
		state.columns[0] = ['BLANK', 'YTFAST', 'XX', '1MIN', 'KOSIK', '5MIN'] // forces a slow rescan for 5MIN
		state.delayMs = (method, url) => (method === 'GET' && /\/columns\/6$/.test(url) ? 150 : 0)
		const first = client.connectColumnByName('5MIN')
		await new Promise((r) => setTimeout(r, 20))
		const second = client.connectColumnByName('NO SUCH COLUMN')
		await assert.rejects(second, /No column named "NO SUCH COLUMN"/)
		assert.deepEqual(await first, { index: 6, superseded: true })
		assert.deepEqual(state.connected, [])
	})

	test('a failed deck select is reported', async () => {
		state.connectStatus = 500
		await assert.rejects(client.selectDeckByName('NewLevel'), /POST \/composition\/decks\/1\/select answered HTTP 500/)
	})

	test('a slow deck select does not hold up a column connect', async () => {
		state.delayMs = (method, url) => (method === 'POST' && /\/decks\/\d+\/select$/.test(url) ? 250 : 0)
		const order = []
		const deck = client.selectDeckByName('NewLevel').then(() => order.push('deck'))
		await new Promise((r) => setTimeout(r, 30))
		const col = client.connectColumnByName('5MIN').then(() => order.push('column'))
		await Promise.all([deck, col])
		assert.deepEqual(order, ['column', 'deck'])
	})

	test('knownLists() lists every list used so far', async () => {
		assert.deepEqual(client.knownLists(), [])
		await client.connectColumnByName('5min', 2)
		await client.selectDeckByName('NewLevel')
		assert.deepEqual(client.knownLists().map((l) => l.key).sort(), ['columns:2', 'decks'])
	})

	test('an unknown name throws a ResolumeError naming the column and sends no POST', async () => {
		await assert.rejects(client.connectColumnByName('NOPE'), (err) => {
			assert.ok(err instanceof ResolumeError)
			assert.match(err.message, /No column named "NOPE" in the composition \(5 columns checked\)/)
			return true
		})
		assert.equal(state.connected.length, 0)
	})

	test('a layer group that does not exist is reported as such', async () => {
		await assert.rejects(client.connectColumnByName('YTFAST', 7), /layer group 7 has no columns or does not exist/)
	})

	test('an HTTP error on connect is reported, not swallowed', async () => {
		state.connectStatus = 500
		await assert.rejects(client.connectColumnByName('YTFAST'), /answered HTTP 500/)
	})

	test('a failed POST does not block later presses', async () => {
		state.connectStatus = 500
		await assert.rejects(client.connectColumnByName('YTFAST'))
		state.connectStatus = 204
		assert.equal((await client.connectColumnByName('5MIN')).superseded, false)
		assert.deepEqual(state.connected, [{ group: 0, index: 3 }])
	})

	test('a wedged Arena fails fast with a timeout instead of hanging the action', async () => {
		state.hang = true
		const started = Date.now()
		await assert.rejects(client.product(), /GET http:\/\/127\.0\.0\.1:\d+\/api\/v1\/product failed: no answer within 300 ms/)
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
