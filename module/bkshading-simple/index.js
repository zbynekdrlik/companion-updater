'use strict'

const { InstanceBase, InstanceStatus, Regex, combineRgb, runEntrypoint } = require('@companion-module/base')
const { BkshadingClient } = require('./lib/client')
const { ShadingController } = require('./lib/controller')
const { PARAMS, displayValues } = require('./lib/steps')

/** bkshading's own pump refreshes every ~2 s; polling faster shows nothing new. */
const POLL_INTERVAL_MS = 2000
const DEFAULT_PORT = 8770

const PARAM_LABELS = { iso: 'ISO', aperture: 'Clona (aperture)', kelvin: 'WB (Kelvin)', tint: 'Tint' }
const VALUE_KEYS = ['iso', 'fnumber', 'kelvin', 'tint']

/** A camera id as a Companion variable name part: letters, digits and _ only. */
function varId(id) {
	return String(id).replace(/[^A-Za-z0-9_]/g, '_')
}

/** The values shown for one camera ('-' for anything unknown or offline). */
function cameraValues(cam) {
	const online = Boolean(cam && cam.reachable && cam.state && cam.state.online)
	const values = online ? displayValues(cam.state.params) : displayValues(null)
	return { ...values, online: online ? 'on' : 'off', label: cam ? String(cam.label ?? cam.id) : '-' }
}

class BkshadingSimpleInstance extends InstanceBase {
	constructor(internal) {
		super(internal)
		this.client = null
		this.controller = null
		this.pollTimer = null
		this.pollRun = null
		this.pollTimeoutMs = undefined // library defaults (tests shorten them)
		this.writeTimeoutMs = undefined
		this.pollFailures = 0
		this.lastHealth = null
		this.knownIds = ''
		/**
		 * Every camera id seen since the module started. Their variables stay
		 * defined (showing '-' / off) when a camera drops out of bkshading or
		 * bkshading is down, so a button never shows a stale live value.
		 */
		this.knownIdList = []
		this.selected = null
	}

	async init(config) {
		this.config = config
		this.selected = String(config.defaultCamera || '').trim() || null
		this.setActionDefinitions(this.actionDefinitions([]))
		this.setFeedbackDefinitions(this.feedbackDefinitions())
		this.defineVariables([])
		this.start()
	}

	async configUpdated(config) {
		this.config = config
		this.start()
	}

	async destroy() {
		this.stop()
		this.client = null
	}

	getConfigFields() {
		return [
			{
				type: 'textinput',
				id: 'host',
				label: 'bkshading host (DNS name, no http:// or port), e.g. strih.lan',
				width: 6,
				default: 'strih.lan',
				regex: Regex.HOSTNAME,
			},
			{
				type: 'number',
				id: 'port',
				label: 'bkshading port',
				width: 3,
				default: DEFAULT_PORT,
				min: 1,
				max: 65535,
			},
			{
				type: 'textinput',
				id: 'defaultCamera',
				label: 'Camera selected at startup (bkshading id, e.g. cam1)',
				width: 3,
				default: 'cam1',
			},
		]
	}

	start() {
		this.stop()
		const host = String(this.config.host || '').trim()
		if (!host) {
			this.client = null
			this.controller = null
			this.updateStatus(InstanceStatus.BadConfig, 'Set the bkshading host')
			return
		}
		this.client = new BkshadingClient({
			baseUrl: `http://${host}:${this.config.port || DEFAULT_PORT}`,
			pollTimeoutMs: this.pollTimeoutMs,
			writeTimeoutMs: this.writeTimeoutMs,
		})
		this.controller = new ShadingController({ client: this.client })
		this.controller.select(this.selected)
		this.pollFailures = 0
		this.lastHealth = null
		this.updateStatus(InstanceStatus.Connecting)
		this.poll()
		this.pollTimer = setInterval(() => this.poll(), POLL_INTERVAL_MS)
	}

	stop() {
		if (this.pollTimer) clearInterval(this.pollTimer)
		this.pollTimer = null
	}

	/** One GET /api/cameras; never throws. A poll already running for the same client is shared. */
	poll() {
		const client = this.client
		if (!client) return Promise.resolve()
		if (this.pollRun && this.pollRun.client === client) return this.pollRun.promise
		const promise = this.runPoll(client).finally(() => {
			if (this.pollRun && this.pollRun.client === client) this.pollRun = null
		})
		this.pollRun = { client, promise }
		return promise
	}

	async runPoll(client) {
		const controller = this.controller
		const startedAt = controller.now()
		try {
			const aggregate = await client.cameras()
			if (client !== this.client) return
			controller.applyAggregate(aggregate, startedAt)
			this.pollFailures = 0
			if (this.lastHealth !== 'ok') {
				const online = controller.order.filter((id) => controller.isOnline(id))
				this.log(
					'info',
					`Connected to bkshading ${aggregate.version ?? '?'} at ${client.baseUrl}: cameras ${controller.order.join(', ') || 'none'}; online ${online.join(', ') || 'none'}`,
				)
			}
			this.lastHealth = 'ok'
			this.updateStatus(InstanceStatus.Ok)
			this.refreshDefinitionsIfCamerasChanged()
			this.publish()
		} catch (err) {
			if (client !== this.client) return
			this.pollFailures++
			// One missed poll is not an outage; the values stay as they were.
			if (this.pollFailures < 2 && this.lastHealth === 'ok') {
				this.log('debug', `Poll missed once (${err.message}); not reporting yet`)
				return
			}
			if (this.lastHealth !== err.message) this.log('warn', `bkshading not reachable: ${err.message}`)
			this.lastHealth = err.message
			this.updateStatus(InstanceStatus.ConnectionFailure, err.message)
			controller.clear()
			this.publish()
		}
	}

	/** Camera list changed (first poll, or bkshading config changed): new dropdowns + variables. */
	refreshDefinitionsIfCamerasChanged() {
		const ids = this.controller.order
		const key = ids.join(',')
		if (key === this.knownIds) return
		this.knownIds = key
		this.knownIdList = [...new Set([...this.knownIdList, ...ids])]
		this.setActionDefinitions(this.actionDefinitions(ids))
		this.defineVariables(this.knownIdList)
		this.log('debug', `Camera list: ${key || 'empty'}`)
	}

	defineVariables(ids) {
		const defs = [
			{ variableId: 'camera', name: 'Selected camera (bkshading id)' },
			{ variableId: 'camera_label', name: 'Selected camera name' },
			{ variableId: 'online', name: 'Selected camera online (on/off)' },
			{ variableId: 'iso', name: 'Selected camera ISO' },
			{ variableId: 'fnumber', name: 'Selected camera aperture (f-number)' },
			{ variableId: 'kelvin', name: 'Selected camera white balance' },
			{ variableId: 'tint', name: 'Selected camera tint' },
			{ variableId: 'service_version', name: 'bkshading service version' },
		]
		for (const id of ids) {
			const v = varId(id)
			defs.push(
				{ variableId: `${v}_online`, name: `${id} online (on/off)` },
				...VALUE_KEYS.map((k) => ({ variableId: `${v}_${k}`, name: `${id} ${k}` })),
			)
		}
		this.setVariableDefinitions(defs)
	}

	/** Push every variable and re-check every feedback. */
	publish() {
		const controller = this.controller
		if (!controller) return
		const selected = controller.selected
		const sel = cameraValues(selected ? controller.camera(selected) : undefined)
		const values = {
			camera: selected || '-',
			camera_label: selected && controller.camera(selected) ? sel.label : selected || '-',
			online: sel.online,
			iso: sel.iso,
			fnumber: sel.fnumber,
			kelvin: sel.kelvin,
			tint: sel.tint,
			service_version: controller.serviceVersion ?? '-',
		}
		// A camera that vanished from the answer (or bkshading is down) shows '-'/'off',
		// never its last values as if it were still live.
		for (const id of new Set([...this.knownIdList, ...controller.order])) {
			const v = varId(id)
			const c = cameraValues(controller.camera(id))
			values[`${v}_online`] = c.online
			for (const k of VALUE_KEYS) values[`${v}_${k}`] = c[k]
		}
		this.setVariableValues(values)
		this.checkFeedbacks()
	}

	/** '' or an unset camera option means "the selected camera". */
	async cameraOption(raw, context) {
		const text = (await context.parseVariablesInString(String(raw ?? ''))).trim()
		return text || this.controller?.selected || ''
	}

	async selectCameraAction(options, context) {
		const id = (await context.parseVariablesInString(String(options.camera ?? ''))).trim()
		if (!id) {
			this.log('error', 'Select camera: no camera id set')
			return
		}
		this.selected = id
		if (!this.controller) {
			this.log('error', `Select camera ${id}: bkshading host is not configured`)
			return
		}
		this.controller.select(id)
		const cam = this.controller.camera(id)
		const state = !cam ? 'not in bkshading' : this.controller.isOnline(id) ? 'online' : cam.reachable ? 'camera offline' : 'relay not reachable'
		this.log('info', `Selected camera ${id}${cam && cam.label ? ` (${cam.label})` : ''}: ${state}`)
		this.publish()
	}

	async stepAction(options, context) {
		const param = String(options.param)
		const dir = options.direction === 'down' ? -1 : 1
		const sign = dir > 0 ? '+' : '-'
		if (!this.controller) {
			this.log('error', `Step ${param} ${sign}: bkshading host is not configured`)
			return
		}
		const id = await this.cameraOption(options.camera, context)
		if (!id) {
			this.log('error', `Step ${param} ${sign}: no camera selected`)
			return
		}
		try {
			const result = await this.controller.step(id, param, dir)
			if (result.unchanged) {
				this.log('info', `${id} ${param} ${sign}: already at the end (${result.label})`)
			} else {
				this.log('info', `${id} ${param} ${sign}: ${result.from} -> ${result.label} (${JSON.stringify(result.patch)}, ${result.ms} ms)`)
			}
		} catch (err) {
			this.log('error', `${id} ${param} ${sign} failed: ${err.message}`)
		}
		this.publish()
	}

	actionDefinitions(ids) {
		const cameraChoices = ids.map((id) => ({ id, label: id }))
		return {
			select_camera: {
				name: 'Select camera',
				description: 'Makes this camera the one the +/- steps and the "selected camera" variables act on.',
				options: [
					{
						type: 'dropdown',
						id: 'camera',
						label: 'Camera (bkshading id)',
						default: ids[0] || 'cam1',
						choices: cameraChoices,
						allowCustom: true,
					},
				],
				callback: (action, context) => this.selectCameraAction(action.options, context),
			},
			step: {
				name: 'Step ISO / aperture / WB / tint',
				description: 'One step, exactly like the bkshading panel: ISO and aperture move to the next camera choice, WB by 100 K, tint by 1.',
				options: [
					{
						type: 'dropdown',
						id: 'param',
						label: 'Parameter',
						default: 'iso',
						choices: PARAMS.map((p) => ({ id: p, label: PARAM_LABELS[p] })),
					},
					{
						type: 'dropdown',
						id: 'direction',
						label: 'Direction',
						default: 'up',
						choices: [
							{ id: 'up', label: '+ (up)' },
							{ id: 'down', label: '- (down)' },
						],
					},
					{
						type: 'textinput',
						id: 'camera',
						label: 'Camera (empty = the selected camera)',
						default: '',
						useVariables: true,
					},
				],
				callback: (action, context) => this.stepAction(action.options, context),
			},
		}
	}

	feedbackDefinitions() {
		const cameraOption = {
			type: 'textinput',
			id: 'camera',
			label: 'Camera (empty = the selected camera)',
			default: '',
			useVariables: true,
		}
		return {
			camera_online: {
				type: 'boolean',
				name: 'Camera online',
				description: 'True when bkshading reaches the camera (relay up, camera on USB).',
				defaultStyle: { bgcolor: combineRgb(0, 120, 0), color: combineRgb(255, 255, 255) },
				options: [cameraOption],
				callback: async (feedback, context) => {
					if (!this.controller) return false
					return this.controller.isOnline(await this.cameraOption(feedback.options.camera, context))
				},
			},
			camera_selected: {
				type: 'boolean',
				name: 'Camera is the selected one',
				defaultStyle: { bgcolor: combineRgb(0, 80, 160), color: combineRgb(255, 255, 255) },
				options: [{ ...cameraOption, label: 'Camera (bkshading id)', default: 'cam1' }],
				callback: async (feedback, context) => {
					if (!this.controller) return false
					const id = (await context.parseVariablesInString(String(feedback.options.camera ?? ''))).trim()
					return Boolean(id) && id === this.controller.selected
				},
			},
			not_applied: {
				type: 'boolean',
				name: 'Write not applied by the camera',
				description: 'True when bkshading reports that the camera ignored the last write of this parameter.',
				defaultStyle: { bgcolor: combineRgb(160, 0, 0), color: combineRgb(255, 255, 255) },
				options: [
					{
						type: 'dropdown',
						id: 'param',
						label: 'Parameter',
						default: 'aperture',
						choices: PARAMS.map((p) => ({ id: p, label: PARAM_LABELS[p] })),
					},
					cameraOption,
				],
				callback: async (feedback, context) => {
					if (!this.controller) return false
					return this.controller.notApplied(await this.cameraOption(feedback.options.camera, context), String(feedback.options.param))
				},
			},
		}
	}
}

module.exports = { BkshadingSimpleInstance, varId, cameraValues, POLL_INTERVAL_MS }

runEntrypoint(BkshadingSimpleInstance, [])
