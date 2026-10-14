'use strict'

const { test, describe, beforeEach, afterEach } = require('node:test')
const assert = require('node:assert/strict')
const { startFakeBkshading, stopFakeBkshading, ppCam6, offlineCam, unreachableCam } = require('./testing/fake-bkshading')

/**
 * Load index.js with the Companion SDK's runtime pieces replaced: the real
 * InstanceBase needs an IPC channel to a running Companion. The enums,
 * regexes and combineRgb are the real ones from @companion-module/base.
 */
function loadModule() {
	const real = require('@companion-module/base')
	const basePath = require.resolve('@companion-module/base')
	const captured = {}
	class InstanceBase {
		constructor() {
			this.logs = []
			this.statuses = []
			this.variables = {}
			this.variableDefs = []
			this.feedbackChecks = 0
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
		setFeedbackDefinitions(defs) {
			this.feedbacks = defs
		}
		setVariableDefinitions(defs) {
			this.variableDefs = defs
		}
		setVariableValues(values) {
			Object.assign(this.variables, values)
		}
		checkFeedbacks() {
			this.feedbackChecks++
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
		assert.equal(mod.captured.cls, mod.BkshadingSimpleInstance)
		assert.deepEqual(mod.captured.upgrades, [])
	})

	test('varId keeps only letters, digits and _', () => {
		assert.equal(mod.varId('cam6'), 'cam6')
		assert.equal(mod.varId('handheld-1'), 'handheld_1')
	})

	test('cameraValues shows "-" for a camera that is not online', () => {
		assert.deepEqual(mod.cameraValues(ppCam6()), { iso: '1600', fnumber: 'f/4.8', kelvin: '5000K', tint: '0', online: 'on', label: 'Cam 6' })
		const off = mod.cameraValues(offlineCam('cam4'))
		assert.equal(off.online, 'off')
		assert.equal(off.iso, '-')
		assert.equal(mod.cameraValues(undefined).label, '-')
	})
})

describe('BkshadingSimpleInstance against a fake bkshading', () => {
	const mod = loadModule()
	let server
	let state
	let instance

	beforeEach(async () => {
		state = { cameras: [ppCam6(), offlineCam('cam4'), unreachableCam('handheld-1')] }
		server = await startFakeBkshading(state)
		instance = new mod.BkshadingSimpleInstance({})
		instance.pollTimeoutMs = 10000 // behaviour, not timing: a loaded box must not flake these
		instance.writeTimeoutMs = 10000
		await instance.init({ host: '127.0.0.1', port: server.address().port, defaultCamera: 'cam6' })
		await instance.poll()
	})

	afterEach(async () => {
		await instance.destroy()
		await stopFakeBkshading(server)
	})

	test('first poll: status ok, one "Connected" line, variables for the selected camera and per camera', () => {
		assert.deepEqual(instance.statuses.at(-1), [mod.InstanceStatus.Ok, undefined])
		const connected = instance.logs.filter(([, m]) => m.startsWith('Connected to bkshading'))
		assert.equal(connected.length, 1)
		assert.match(connected[0][1], /1\.7\.0-dev\.785 .*cameras cam6, cam4, handheld-1; online cam6/)
		assert.equal(instance.variables.camera, 'cam6')
		assert.equal(instance.variables.camera_label, 'Cam 6')
		assert.equal(instance.variables.iso, '1600')
		assert.equal(instance.variables.fnumber, 'f/4.8')
		assert.equal(instance.variables.kelvin, '5000K')
		assert.equal(instance.variables.tint, '0')
		assert.equal(instance.variables.online, 'on')
		assert.equal(instance.variables.cam4_online, 'off')
		assert.equal(instance.variables.handheld_1_iso, '-')
		assert.ok(instance.variableDefs.some((d) => d.variableId === 'cam6_kelvin'))
	})

	test('the camera dropdown lists bkshading cameras', () => {
		const choices = instance.actions.select_camera.options[0].choices.map((c) => c.id)
		assert.deepEqual(choices, ['cam6', 'cam4', 'handheld-1'])
	})

	test('ISO + on the selected camera writes 2000 and shows it', async () => {
		await instance.actions.step.callback({ options: { param: 'iso', direction: 'up', camera: '' } }, context)
		assert.deepEqual(state.writes, [{ id: 'cam6', patch: { iso: 2000 } }])
		assert.equal(instance.variables.iso, '2000')
		assert.equal(instance.variables.cam6_iso, '2000')
		assert.ok(instance.logs.some(([l, m]) => l === 'info' && m.startsWith('cam6 iso +: 1600 -> 2000')))
	})

	test('each column steps its own parameter', async () => {
		const step = (param, direction) => instance.actions.step.callback({ options: { param, direction, camera: '' } }, context)
		await step('aperture', 'up')
		await step('kelvin', 'down')
		await step('tint', 'up')
		assert.equal(state.writes.length, 3)
		assert.equal(instance.variables.fnumber, 'f/5.2')
		assert.equal(instance.variables.kelvin, '4900K')
		assert.equal(instance.variables.tint, '1')
	})

	test('select_camera switches what the steps and variables act on', async () => {
		await instance.actions.select_camera.callback({ options: { camera: 'cam4' } }, context)
		assert.equal(instance.variables.camera, 'cam4')
		assert.equal(instance.variables.online, 'off')
		assert.equal(instance.variables.iso, '-')
		assert.ok(instance.logs.some(([, m]) => m === 'Selected camera cam4 (Cam 4): camera offline'))
		await instance.actions.step.callback({ options: { param: 'iso', direction: 'up', camera: '' } }, context)
		assert.deepEqual(state.writes, [])
		assert.ok(instance.logs.some(([l, m]) => l === 'error' && /cam4 iso \+ failed: the camera is offline/.test(m)))
	})

	test('selecting a camera bkshading does not have logs why and writes nothing', async () => {
		await instance.actions.select_camera.callback({ options: { camera: 'cam2' } }, context)
		assert.equal(instance.variables.camera, 'cam2')
		assert.equal(instance.variables.camera_label, 'cam2')
		assert.ok(instance.logs.some(([, m]) => m === 'Selected camera cam2: not in bkshading'))
		await instance.actions.step.callback({ options: { param: 'kelvin', direction: 'down', camera: '' } }, context)
		assert.ok(instance.logs.some(([, m]) => /cam2 kelvin - failed: camera "cam2" is not in bkshading/.test(m)))
		assert.deepEqual(state.writes, [])
	})

	test('an explicit camera option overrides the selection', async () => {
		await instance.actions.select_camera.callback({ options: { camera: 'cam4' } }, context)
		await instance.actions.step.callback({ options: { param: 'tint', direction: 'down', camera: 'cam6' } }, context)
		assert.deepEqual(state.writes, [{ id: 'cam6', patch: { tint: -1 } }])
	})

	test('feedbacks: online, selected, not applied', async () => {
		const fb = (id, options) => instance.feedbacks[id].callback({ options }, context)
		assert.equal(await fb('camera_online', { camera: '' }), true)
		assert.equal(await fb('camera_online', { camera: 'cam4' }), false)
		assert.equal(await fb('camera_selected', { camera: 'cam6' }), true)
		assert.equal(await fb('camera_selected', { camera: 'cam4' }), false)
		assert.equal(await fb('not_applied', { param: 'aperture', camera: '' }), false)
		state.cameras[0].state.notApplied = ['apertureNorm']
		await instance.poll()
		assert.equal(await fb('not_applied', { param: 'aperture', camera: '' }), true)
	})

	test('status turns red only after two failed polls; values are cleared then', async () => {
		instance.client.pollTimeoutMs = 200
		state.hang = true
		await instance.poll()
		assert.deepEqual(instance.statuses.at(-1), [mod.InstanceStatus.Ok, undefined])
		assert.equal(instance.variables.iso, '1600', 'one missed poll keeps the values')
		await instance.poll()
		assert.equal(instance.statuses.at(-1)[0], mod.InstanceStatus.ConnectionFailure)
		assert.ok(instance.logs.some(([l, m]) => l === 'warn' && /^bkshading not reachable: GET .* no answer within 200 ms/.test(m)))
		assert.equal(instance.variables.iso, '-')
		assert.equal(instance.variables.online, 'off')
	})

	test('no host -> BadConfig, and actions log instead of throwing', async () => {
		await instance.configUpdated({ host: '', port: 8770 })
		assert.equal(instance.statuses.at(-1)[0], mod.InstanceStatus.BadConfig)
		await instance.actions.step.callback({ options: { param: 'iso', direction: 'up', camera: '' } }, context)
		assert.ok(instance.logs.some(([l, m]) => l === 'error' && /bkshading host is not configured/.test(m)))
	})
})
