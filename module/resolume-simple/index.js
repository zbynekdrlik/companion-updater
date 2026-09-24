'use strict'

const { InstanceBase, InstanceStatus, runEntrypoint } = require('@companion-module/base')
const { ResolumeClient } = require('./lib/resolume')
const { buildOscArgs, validateOscPath } = require('./lib/osc')

const HEALTH_INTERVAL_MS = 5000
const DEFAULT_LAYER_GROUP = 2

class ResolumeSimpleInstance extends InstanceBase {
	constructor(internal) {
		super(internal)
		this.client = null
		this.healthTimer = null
		this.healthInFlight = false
		this.lastHealth = null
	}

	async init(config) {
		this.config = config
		this.setActionDefinitions(this.actionDefinitions())
		this.start()
	}

	async configUpdated(config) {
		this.config = config
		this.start()
	}

	async destroy() {
		this.stop()
	}

	getConfigFields() {
		return [
			{
				type: 'textinput',
				id: 'host',
				label: 'Resolume host (IP or name)',
				width: 6,
				default: '',
			},
			{
				type: 'number',
				id: 'restPort',
				label: 'Webserver port (Preferences > Webserver)',
				width: 3,
				default: 8090,
				min: 1,
				max: 65535,
			},
			{
				type: 'number',
				id: 'oscPort',
				label: 'OSC input port (Preferences > OSC)',
				width: 3,
				default: 7000,
				min: 1,
				max: 65535,
			},
		]
	}

	start() {
		this.stop()
		const host = String(this.config.host || '').trim()
		if (!host) {
			this.client = null
			this.updateStatus(InstanceStatus.BadConfig, 'Set the Resolume host')
			return
		}
		this.client = new ResolumeClient({ baseUrl: `http://${host}:${this.config.restPort || 8090}` })
		this.lastHealth = null
		this.updateStatus(InstanceStatus.Connecting)
		this.checkHealth()
		this.healthTimer = setInterval(() => this.checkHealth(), HEALTH_INTERVAL_MS)
	}

	stop() {
		if (this.healthTimer) clearInterval(this.healthTimer)
		this.healthTimer = null
	}

	/** Status reflects whether Arena's webserver answers; logs only on changes. */
	async checkHealth() {
		if (this.healthInFlight || !this.client) return
		this.healthInFlight = true
		const client = this.client
		try {
			const p = await client.product()
			if (client !== this.client) return
			if (this.lastHealth !== 'ok') {
				this.log('info', `Connected to ${p.name} ${p.major}.${p.minor}.${p.micro} at ${client.baseUrl}`)
			}
			this.lastHealth = 'ok'
			this.updateStatus(InstanceStatus.Ok)
		} catch (err) {
			if (client !== this.client) return
			if (this.lastHealth !== err.message) this.log('warn', `Resolume not reachable: ${err.message}`)
			this.lastHealth = err.message
			this.updateStatus(InstanceStatus.ConnectionFailure, err.message)
		} finally {
			this.healthInFlight = false
		}
	}

	actionDefinitions() {
		return {
			connect_column_by_name: {
				name: 'Connect column by name',
				description: 'Finds the column by its name in the given layer group and connects it (Arena confirms).',
				options: [
					{
						type: 'textinput',
						id: 'name',
						label: 'Column name',
						default: '',
						useVariables: true,
					},
					{
						type: 'number',
						id: 'group',
						label: 'Layer group (0 = whole composition)',
						default: DEFAULT_LAYER_GROUP,
						min: 0,
						max: 999,
					},
				],
				callback: async (action, context) => {
					const name = (await context.parseVariablesInString(String(action.options.name ?? ''))).trim()
					const group = Number(action.options.group ?? DEFAULT_LAYER_GROUP)
					if (!name) {
						this.log('error', 'Connect column by name: no column name set')
						return
					}
					if (!this.client) {
						this.log('error', `Connect column "${name}": Resolume host is not configured`)
						return
					}
					const started = Date.now()
					try {
						const index = await this.client.connectColumnByName(name, group)
						this.log('debug', `Connected column "${name}" (#${index}, group ${group}) in ${Date.now() - started} ms`)
					} catch (err) {
						this.log('error', `Connect column "${name}" (group ${group}) failed: ${err.message}`)
					}
				},
			},
			send_osc: {
				name: 'Send OSC',
				description: 'Sends one OSC message to Resolume over UDP.',
				options: [
					{
						type: 'textinput',
						id: 'path',
						label: 'OSC path',
						default: '/composition/columns/1/connect',
						useVariables: true,
					},
					{
						type: 'dropdown',
						id: 'type',
						label: 'Argument',
						default: 'none',
						choices: [
							{ id: 'none', label: 'None' },
							{ id: 'i', label: 'Integer' },
							{ id: 'f', label: 'Float' },
							{ id: 's', label: 'String' },
						],
					},
					{
						type: 'textinput',
						id: 'value',
						label: 'Value',
						default: '',
						useVariables: true,
						isVisible: (options) => options.type !== 'none',
					},
				],
				callback: async (action, context) => {
					const host = String(this.config.host || '').trim()
					if (!host) {
						this.log('error', 'Send OSC: Resolume host is not configured')
						return
					}
					try {
						const path = validateOscPath(await context.parseVariablesInString(String(action.options.path ?? '')))
						const value = await context.parseVariablesInString(String(action.options.value ?? ''))
						const args = buildOscArgs(action.options.type, value)
						this.oscSend(host, Number(this.config.oscPort || 7000), path, args)
					} catch (err) {
						this.log('error', `Send OSC failed: ${err.message}`)
					}
				},
			},
		}
	}
}

runEntrypoint(ResolumeSimpleInstance, [])
