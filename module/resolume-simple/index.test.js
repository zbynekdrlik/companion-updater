'use strict'

const { test, describe, beforeEach, afterEach } = require('node:test')
const assert = require('node:assert/strict')
const { startFakeArena, stopFakeArena, ALLOWED_REQUEST } = require('./testing/fake-arena')

/**
 * Load index.js with the Companion SDK's runtime pieces replaced: the real
 * InstanceBase needs an IPC channel to a running Companion. The enums and
 * regexes are the real ones from @companion-module/base.
 */
function loadModule() {
	const real = require('@companion-module/base')
	const basePath = require.resolve('@companion-module/base')
	const captured = {}
	class InstanceBase {
		constructor() {
			this.logs = []
			this.statuses = []
			this.osc = []
		}
		log(level, message) {
			this.logs.push([level, message])
		}
		updateStatus(status, message) {
			this.statuses.push([status, message])
		}
		setActionDefinitions(defs) {
			this.actions = defs
		}
		oscSend(...args) {
			this.osc.push(args)
		}
	}
	const saved = require.cache[basePath]
	require.cache[basePath] = {
		id: basePath,
		filename: basePath,
		loaded: true,
		exports: { ...real, InstanceBase, runEntrypoint: (cls, upgrades) => Object.assign(captured, { cls, upgrades }) },
	}
	delete require.cache[require.resolve('./index.js')]
	try {
		return { ...require('./index.js'), captured, InstanceStatus: real.InstanceStatus }
	} finally {
		require.cache[basePath] = saved
	}
}

const context = { parseVariablesInString: async (s) => s }

describe('index.js wiring', () => {
	const mod = loadModule()

	test('registers the instance class with Companion', () => {
		assert.equal(mod.captured.cls, mod.ResolumeSimpleInstance)
		assert.deepEqual(mod.captured.upgrades, [])
	})

	test('parseLayerGroup accepts only whole numbers >= 0', () => {
		assert.equal(mod.parseLayerGroup(2), 2)
		assert.equal(mod.parseLayerGroup('0'), 0)
		assert.equal(mod.parseLayerGroup(' 12 '), 12)
		for (const bad of ['', 'abc', '2.5', '-1', null, undefined]) assert.equal(mod.parseLayerGroup(bad), undefined, String(bad))
	})
})

describe('ResolumeSimpleInstance against a fake Arena', () => {
	const mod = loadModule()
	let server
	let state
	let instance

	beforeEach(async () => {
		state = {
			columns: { 0: ['BLANK', 'YTFAST'], 2: ['Blank', 'ytfast', 'Kosik #'] },
			decks: ['NewLevel', 'Amazing Grace', 'Oceans'],
		}
		server = await startFakeArena(state)
		instance = new mod.ResolumeSimpleInstance({})
		await instance.init({ host: '127.0.0.1', restPort: server.address().port, oscPort: 7002 })
	})

	afterEach(async () => {
		await instance.destroy()
		await stopFakeArena(server)
	})

	test('defines the three actions; the layer group defaults to 2', () => {
		assert.deepEqual(Object.keys(instance.actions).sort(), ['connect_column_by_name', 'select_deck_by_name', 'send_osc'])
		const group = instance.actions.connect_column_by_name.options.find((o) => o.id === 'group')
		assert.equal(group.default, 2)
		const value = instance.actions.send_osc.options.find((o) => o.id === 'value')
		assert.equal(typeof value.isVisibleExpression, 'string')
		assert.equal(value.isVisible, undefined)
	})

	test('becomes Ok once Arena answers and logs the connection once', async () => {
		await instance.checkHealth()
		await instance.checkHealth()
		assert.equal(instance.statuses.at(-1)[0], mod.InstanceStatus.Ok)
		assert.equal(instance.logs.filter(([l, m]) => l === 'info' && m.startsWith('Connected to Arena 7.27.1')).length, 1)
	})

	test('the connect action connects the named column in layer group 2, case-insensitively', async () => {
		await instance.actions.connect_column_by_name.callback({ options: { name: 'YTFAST', group: 2 } }, context)
		assert.deepEqual(state.connected, [{ group: 2, index: 2 }])
		for (const r of state.requests) assert.match(r, ALLOWED_REQUEST)
	})

	test('an invalid layer group is refused with an error, never falling back to the composition', async () => {
		await instance.actions.connect_column_by_name.callback({ options: { name: 'YTFAST', group: '' } }, context)
		assert.equal(state.connected.length, 0)
		assert.ok(instance.logs.some(([l, m]) => l === 'error' && /layer group "" is not a whole number/.test(m)))
	})

	test('an unknown column is logged as an error', async () => {
		await instance.actions.connect_column_by_name.callback({ options: { name: 'NOPE', group: 2 } }, context)
		assert.ok(instance.logs.some(([l, m]) => l === 'error' && /No column named "NOPE" in layer group 2/.test(m)))
	})

	test('the deck action selects the deck named by a variable (AbleSet song name)', async () => {
		const ctx = { parseVariablesInString: async (s) => s.replace('$(AbleSet:activeSongName)', 'oceans') }
		await instance.actions.select_deck_by_name.callback({ options: { name: '$(AbleSet:activeSongName)' } }, ctx)
		assert.deepEqual(state.selectedDecks, [3])
		for (const r of state.requests) assert.match(r, ALLOWED_REQUEST)
	})

	test('an empty song name selects nothing and is logged', async () => {
		const ctx = { parseVariablesInString: async () => '' }
		await instance.actions.select_deck_by_name.callback({ options: { name: '$(AbleSet:activeSongName)' } }, ctx)
		assert.equal(state.selectedDecks, undefined)
		assert.ok(instance.logs.some(([l, m]) => l === 'error' && /no deck name set/.test(m)))
	})

	test('an unknown deck name is logged as an error', async () => {
		await instance.actions.select_deck_by_name.callback({ options: { name: 'Nope Song' } }, context)
		assert.ok(instance.logs.some(([l, m]) => l === 'error' && /No deck named "Nope Song" in the deck list \(3 decks checked\)/.test(m)))
	})

	test('the background refresh warms group 2 and every list used, and stops for a replaced client', async () => {
		await instance.checkHealth()
		const client = instance.client
		assert.deepEqual(client.knownLists().map((l) => l.key), ['columns:2']) // warmed on the first healthy check
		await instance.actions.select_deck_by_name.callback({ options: { name: 'Oceans' } }, context)
		state.requests.length = 0
		instance.lastNamesRefresh = 0
		await instance.refreshNamesIfDue(client)
		assert.ok(state.requests.includes('GET /api/v1/composition/layergroups/2/columns/1'))
		assert.ok(state.requests.includes('GET /api/v1/composition/decks/1'))
		state.requests.length = 0
		instance.lastNamesRefresh = 0
		instance.client = null // what destroy() does
		await instance.refreshNamesIfDue(client)
		assert.deepEqual(state.requests, [])
	})

	test('a superseded press is logged as skipped, not as an error', async () => {
		instance.client.connectColumnByName = async () => ({ index: 2, superseded: true })
		await instance.actions.connect_column_by_name.callback({ options: { name: 'ytfast', group: 2 } }, context)
		assert.ok(instance.logs.some(([l, m]) => l === 'info' && /Skipped column "ytfast" \(#2, group 2\): superseded by a newer press/.test(m)))
		assert.ok(!instance.logs.some(([l]) => l === 'error'))
	})

	test('send_osc sends through Companion with typed arguments', async () => {
		await instance.actions.send_osc.callback({ options: { path: '/composition/layers/29/clips/4/video/opacity', type: 'f', value: '0' } }, context)
		assert.deepEqual(instance.osc, [['127.0.0.1', 7002, '/composition/layers/29/clips/4/video/opacity', [{ type: 'f', value: 0 }]]])
	})

	test('send_osc with a bad value logs an error and sends nothing', async () => {
		await instance.actions.send_osc.callback({ options: { path: '/x', type: 'i', value: 'abc' } }, context)
		assert.equal(instance.osc.length, 0)
		assert.ok(instance.logs.some(([l, m]) => l === 'error' && /not a whole number/.test(m)))
	})

	test('an unreachable Arena turns the status red and is logged once, not on every check', async () => {
		const wedged = await startFakeArena({ columns: {}, hang: true })
		try {
			instance.requestTimeoutMs = 100
			await instance.configUpdated({ host: '127.0.0.1', restPort: wedged.address().port, oscPort: 7002 })
			await instance.checkHealth() // joins the check started by configUpdated
			await instance.checkHealth()
			assert.equal(instance.statuses.at(-1)[0], mod.InstanceStatus.ConnectionFailure)
			assert.match(instance.statuses.at(-1)[1], /no answer within 100 ms/)
			assert.equal(instance.logs.filter(([l]) => l === 'warn').length, 1)
		} finally {
			await instance.destroy()
			await stopFakeArena(wedged)
		}
	})

	test('without a host the status is BadConfig and actions do nothing', async () => {
		await instance.configUpdated({ host: '', restPort: 8090, oscPort: 7000 })
		assert.equal(instance.statuses.at(-1)[0], mod.InstanceStatus.BadConfig)
		await instance.actions.connect_column_by_name.callback({ options: { name: 'YTFAST', group: 2 } }, context)
		assert.equal(state.connected.length, 0)
	})
})
