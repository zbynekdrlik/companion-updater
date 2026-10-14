'use strict'

const { test, describe, beforeEach, afterEach } = require('node:test')
const assert = require('node:assert/strict')
const { BkshadingClient, BkshadingError } = require('./client')
const { ShadingController } = require('./controller')
const { StepError } = require('./steps')
const { startFakeBkshading, stopFakeBkshading, ppCam6, offlineCam, unreachableCam } = require('../testing/fake-bkshading')

describe('BkshadingClient against a fake bkshading', () => {
	let server
	let state
	let client

	beforeEach(async () => {
		state = { cameras: [ppCam6(), offlineCam('cam4'), unreachableCam('cam1')] }
		server = await startFakeBkshading(state)
		client = new BkshadingClient({ baseUrl: `http://127.0.0.1:${server.address().port}`, pollTimeoutMs: 10000, writeTimeoutMs: 10000 })
	})

	afterEach(() => stopFakeBkshading(server))

	test('cameras() returns the aggregate', async () => {
		const agg = await client.cameras()
		assert.equal(agg.version, '1.7.0-dev.785')
		assert.deepEqual(
			agg.cameras.map((c) => c.id),
			['cam6', 'cam4', 'cam1'],
		)
	})

	test('setParams PUTs JSON to /api/cameras/:id/params', async () => {
		await client.setParams('cam6', { iso: 2000 })
		assert.deepEqual(state.writes, [{ id: 'cam6', patch: { iso: 2000 } }])
		assert.ok(state.requests.includes('PUT /api/cameras/cam6/params'))
	})

	test('an HTTP error carries the status and the service text', async () => {
		await assert.rejects(client.setParams('nope', { iso: 100 }), (e) => e instanceof BkshadingError && /HTTP 404: no camera 'nope'/.test(e.message))
		await assert.rejects(client.setParams('cam1', { iso: 100 }), /HTTP 502: relay cam1 unreachable/)
	})

	test('a service that never answers fails with a timeout, not a hang', async () => {
		state.hang = true
		const quick = new BkshadingClient({ baseUrl: `http://127.0.0.1:${server.address().port}`, pollTimeoutMs: 200 })
		await assert.rejects(quick.cameras(), /no answer within 200 ms/)
	})

	test('a dead port fails with the connection error', async () => {
		const dead = new BkshadingClient({ baseUrl: 'http://127.0.0.1:1', pollTimeoutMs: 2000 })
		await assert.rejects(dead.cameras(), (e) => e instanceof BkshadingError && /GET http:\/\/127\.0\.0\.1:1\/api\/cameras failed/.test(e.message))
	})
})

describe('ShadingController', () => {
	let server
	let state
	let client
	let ctl

	async function poll() {
		const startedAt = Date.now()
		ctl.applyAggregate(await client.cameras(), startedAt)
	}

	beforeEach(async () => {
		state = { cameras: [ppCam6(), offlineCam('cam4'), unreachableCam('cam1')] }
		server = await startFakeBkshading(state)
		client = new BkshadingClient({ baseUrl: `http://127.0.0.1:${server.address().port}`, pollTimeoutMs: 10000, writeTimeoutMs: 10000 })
		ctl = new ShadingController({ client })
		await poll()
	})

	afterEach(() => stopFakeBkshading(server))

	test('online / offline / unreachable / unknown cameras', () => {
		assert.equal(ctl.isOnline('cam6'), true)
		assert.equal(ctl.isOnline('cam4'), false)
		assert.equal(ctl.isOnline('cam1'), false)
		assert.equal(ctl.isOnline('cam9'), false)
		assert.deepEqual(ctl.order, ['cam6', 'cam4', 'cam1'])
	})

	test('one step writes the next choice to the camera', async () => {
		const r = await ctl.step('cam6', 'iso', 1)
		assert.equal(r.label, '2000')
		assert.deepEqual(state.writes, [{ id: 'cam6', patch: { iso: 2000 } }])
		assert.equal(ctl.camera('cam6').state.params.iso, 2000)
	})

	test('three fast presses are three steps, sent one after another in order', async () => {
		let inFlight = 0
		let maxInFlight = 0
		const realSet = client.setParams.bind(client)
		client.setParams = async (id, patch) => {
			inFlight++
			maxInFlight = Math.max(maxInFlight, inFlight)
			try {
				return await realSet(id, patch)
			} finally {
				inFlight--
			}
		}
		state.delayMs = (method) => (method === 'PUT' ? 50 : 0)
		const results = await Promise.all([ctl.step('cam6', 'iso', 1), ctl.step('cam6', 'iso', 1), ctl.step('cam6', 'iso', 1)])
		assert.deepEqual(
			results.map((r) => r.label),
			['2000', '2500', '3200'],
		)
		assert.deepEqual(
			state.writes.map((w) => w.patch.iso),
			[2000, 2500, 3200],
		)
		assert.equal(maxInFlight, 1, 'two writes to one camera were in flight at once')
	})

	test('a poll that started before a write cannot roll the step back', async () => {
		const startedAt = Date.now() - 1000 // this GET left before the write below
		const stale = await client.cameras() // still says 1600 (the fake answers instantly)
		stale.cameras[0].state.params.iso = 1600
		await ctl.step('cam6', 'iso', 1)
		ctl.applyAggregate(stale, startedAt)
		assert.equal(ctl.camera('cam6').state.params.iso, 2000)
		const next = await ctl.step('cam6', 'iso', 1)
		assert.equal(next.label, '2500')
	})

	test('a poll that started after the write takes the camera value', async () => {
		await ctl.step('cam6', 'iso', 1)
		state.cameras[0].state.params.iso = 800 // the operator changed it on the camera
		await new Promise((r) => setTimeout(r, 5))
		await poll()
		assert.equal(ctl.camera('cam6').state.params.iso, 800)
	})

	test('a failed step does not block the next press', async () => {
		state.putStatus = 502
		await assert.rejects(ctl.step('cam6', 'kelvin', 1), /HTTP 502/)
		assert.equal(ctl.camera('cam6').state.params.kelvin, 5000, 'a failed write must not move the shown value')
		delete state.putStatus
		const r = await ctl.step('cam6', 'kelvin', 1)
		assert.equal(r.label, '5100K')
	})

	test('steps on cameras that cannot take them are refused with the reason, no write', async () => {
		await assert.rejects(ctl.step('cam4', 'iso', 1), (e) => e instanceof StepError && /camera is offline/.test(e.message))
		await assert.rejects(ctl.step('cam1', 'iso', 1), /relay of cam1 is not reachable/)
		await assert.rejects(ctl.step('cam9', 'iso', 1), /camera "cam9" is not in bkshading \(cameras: cam6, cam4, cam1\)/)
		assert.deepEqual(state.writes, [])
	})

	test('a step already at the end sends nothing', async () => {
		state.cameras[0].state.params.tint = 50
		await poll()
		const r = await ctl.step('cam6', 'tint', 1)
		assert.equal(r.unchanged, true)
		assert.deepEqual(state.writes, [])
	})

	test('notApplied maps step parameters to bkshading wire keys', async () => {
		state.cameras[0].state.notApplied = ['apertureNorm']
		await poll()
		assert.equal(ctl.notApplied('cam6', 'aperture'), true)
		assert.equal(ctl.notApplied('cam6', 'iso'), false)
		assert.equal(ctl.notApplied('cam1', 'aperture'), false)
	})

	test('clear() forgets cameras but keeps the selection', () => {
		ctl.select('cam6')
		ctl.clear()
		assert.equal(ctl.selected, 'cam6')
		assert.equal(ctl.isOnline('cam6'), false)
		assert.deepEqual(ctl.order, [])
	})
})
