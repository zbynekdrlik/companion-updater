'use strict'

const { test, describe, beforeEach, afterEach } = require('node:test')
const assert = require('node:assert/strict')
const { BkshadingClient, BkshadingError, WRITE_TIMEOUT_MS } = require('./client')
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

	test('a write waits longer than the service waits for the relay (10 s)', () => {
		// camera-box bkshading/service/src/aggregator.rs write_client: 10 s total. A write the
		// service completes must never be reported here as failed (the next step would resend it).
		assert.ok(WRITE_TIMEOUT_MS > 10000, `WRITE_TIMEOUT_MS=${WRITE_TIMEOUT_MS}`)
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
		const startedAt = ctl.now()
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
		const startedAt = ctl.now() - 1000 // this GET left before the write below
		const stale = await client.cameras() // still says 1600 (the fake answers instantly)
		stale.cameras[0].state.params.iso = 1600
		await ctl.step('cam6', 'iso', 1)
		ctl.applyAggregate(stale, startedAt)
		assert.equal(ctl.camera('cam6').state.params.iso, 2000)
		const next = await ctl.step('cam6', 'iso', 1)
		assert.equal(next.label, '2500')
	})

	test('a poll that leaves DURING a write and answers the old value after it cannot roll it back', async () => {
		state.delayMs = (method) => (method === 'PUT' ? 80 : 0)
		const press = ctl.step('cam6', 'iso', 1)
		await new Promise((r) => setTimeout(r, 20)) // the PUT is in flight now
		const startedAt = ctl.now()
		const prePoll = await client.cameras() // answers 1600: the fake applies the PUT only when it answers
		assert.equal(prePoll.cameras[0].state.params.iso, 1600)
		await press
		ctl.applyAggregate(prePoll, startedAt)
		assert.equal(ctl.camera('cam6').state.params.iso, 2000)
	})

	test('a stale poll still brings in online, caps and notApplied; only the params are held', async () => {
		const startedAt = ctl.now()
		const stale = await client.cameras()
		await ctl.step('cam6', 'iso', 1)
		stale.cameras[0].state.notApplied = ['apertureNorm']
		stale.cameras[0].state.caps = { ...stale.cameras[0].state.caps, isoChoices: [100, 2000, 4000] }
		ctl.applyAggregate(stale, startedAt)
		const cam = ctl.camera('cam6')
		assert.equal(cam.state.params.iso, 2000)
		assert.deepEqual(cam.state.notApplied, ['apertureNorm'])
		assert.deepEqual(cam.state.caps.isoChoices, [100, 2000, 4000])
		const next = await ctl.step('cam6', 'iso', 1)
		assert.equal(next.label, '4000', 'the next step uses the new caps')
	})

	test('a relay reported unreachable while our write runs does not fail the queued presses', async () => {
		state.delayMs = (method) => (method === 'PUT' ? 80 : 0)
		const first = ctl.step('cam6', 'iso', 1)
		const second = ctl.step('cam6', 'iso', 1)
		await new Promise((r) => setTimeout(r, 20))
		// The relay is busy with the write burst; the service's 1.5 s poll gives up on it.
		ctl.applyAggregate({ version: 'x', cameras: [{ ...ppCam6(), reachable: false, state: null }] }, ctl.now())
		assert.deepEqual(
			(await Promise.all([first, second])).map((r) => r.label),
			['2000', '2500'],
		)
	})

	test('an unreachable relay reported after the writes ended is taken', async () => {
		await ctl.step('cam6', 'iso', 1)
		await new Promise((r) => setTimeout(r, 5))
		ctl.applyAggregate({ version: 'x', cameras: [{ ...ppCam6(), reachable: false, state: null }] }, ctl.now())
		assert.equal(ctl.camera('cam6').reachable, false)
		await assert.rejects(ctl.step('cam6', 'iso', 1), /relay of cam6 is not reachable/)
	})

	test('each queued step reports the value it stepped from, measured when its turn came', async () => {
		state.delayMs = (method) => (method === 'PUT' ? 100 : 0)
		const results = await Promise.all([ctl.step('cam6', 'iso', 1), ctl.step('cam6', 'iso', 1), ctl.step('cam6', 'iso', 1)])
		assert.deepEqual(
			results.map((r) => `${r.from}->${r.label}`),
			['1600->2000', '2000->2500', '2500->3200'],
		)
		// Each write took ~100 ms; timed from the press, the 2nd and 3rd would be ~200 and ~300.
		for (const r of results) assert.ok(Number.isInteger(r.ms) && r.ms >= 90 && r.ms < 190, `ms=${r.ms}`)
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
