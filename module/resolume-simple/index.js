'use strict'

const { InstanceBase, InstanceStatus, Regex, runEntrypoint } = require('@companion-module/base')
const { ResolumeClient, columnsOf } = require('./lib/resolume')
const { buildOscArgs, validateOscPath } = require('./lib/osc')

const HEALTH_INTERVAL_MS = 5000
/** Column names are re-read this often in the background, so presses rarely need a scan. */
const NAMES_REFRESH_MS = 30000
const DEFAULT_LAYER_GROUP = 2

/** Layer group option → integer >= 0, or undefined when it is not a valid group number. */
function parseLayerGroup(raw) {
	const text = String(raw ?? '').trim()
	if (!/^\d+$/.test(text)) return undefined
	return Number(text)
}

class ResolumeSimpleInstance extends InstanceBase {
	constructor(internal) {
		super(internal)
		this.client = null
		this.healthTimer = null
		/** The health check currently running: { client, promise } */
		this.healthRun = null
		this.requestTimeoutMs = undefined // library default
		this.lastHealth = null
		this.lastNamesRefresh = 0
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
		this.client = null
	}

	getConfigFields() {
		return [
			{
				type: 'textinput',
				id: 'host',
				label: 'Resolume host (IP or name, no http:// or port)',
				width: 6,
				default: '',
				regex: Regex.HOSTNAME,
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
		this.client = new ResolumeClient({
			baseUrl: `http://${host}:${this.config.restPort || 8090}`,
			timeoutMs: this.requestTimeoutMs,
		})
		this.lastHealth = null
		this.lastNamesRefresh = 0
		this.updateStatus(InstanceStatus.Connecting)
		this.checkHealth()
		this.healthTimer = setInterval(() => this.checkHealth(), HEALTH_INTERVAL_MS)
	}

	stop() {
		if (this.healthTimer) clearInterval(this.healthTimer)
		this.healthTimer = null
	}

	/**
	 * Status reflects whether Arena's webserver answers; logs only on changes.
	 * Never throws. A check already running for the same client is shared.
	 */
	checkHealth() {
		const client = this.client
		if (!client) return Promise.resolve()
		if (this.healthRun && this.healthRun.client === client) return this.healthRun.promise
		const promise = this.runHealthCheck(client).finally(() => {
			if (this.healthRun && this.healthRun.client === client) this.healthRun = null
		})
		this.healthRun = { client, promise }
		return promise
	}

	async runHealthCheck(client) {
		try {
			const p = await client.product()
			if (client !== this.client) return
			if (this.lastHealth !== 'ok') {
				this.log('info', `Connected to ${p.name} ${p.major}.${p.minor}.${p.micro} at ${client.baseUrl}`)
			}
			this.lastHealth = 'ok'
			this.updateStatus(InstanceStatus.Ok)
			await this.refreshNamesIfDue(client)
		} catch (err) {
			if (client !== this.client) return
			if (this.lastHealth !== err.message) this.log('warn', `Resolume not reachable: ${err.message}`)
			this.lastHealth = err.message
			this.updateStatus(InstanceStatus.ConnectionFailure, err.message)
		}
	}

	/** Keep the name cache warm for every list already used, plus the default layer group. */
	async refreshNamesIfDue(client) {
		if (Date.now() - this.lastNamesRefresh < NAMES_REFRESH_MS) return
		this.lastNamesRefresh = Date.now()
		const lists = new Map([[columnsOf(DEFAULT_LAYER_GROUP).key, columnsOf(DEFAULT_LAYER_GROUP)]])
		for (const list of client.knownLists()) lists.set(list.key, list)
		for (const list of lists.values()) {
			if (client !== this.client) return // destroyed or reconfigured meanwhile
			try {
				await client.refreshNames(list, () => client === this.client)
			} catch (err) {
				this.log('debug', `Refreshing ${list.what} names of ${list.where} failed: ${err.message}`)
			}
		}
	}

	async connectColumnAction(options, context) {
		const name = (await context.parseVariablesInString(String(options.name ?? ''))).trim()
		const group = parseLayerGroup(options.group ?? DEFAULT_LAYER_GROUP)
		if (!name) {
			this.log('error', 'Connect column by name: no column name set')
			return
		}
		if (group === undefined) {
			this.log('error', `Connect column "${name}": layer group "${options.group}" is not a whole number >= 0`)
			return
		}
		const client = this.client
		if (!client) {
			this.log('error', `Connect column "${name}": Resolume host is not configured`)
			return
		}
		const started = Date.now()
		try {
			const { index, superseded } = await client.connectColumnByName(name, group)
			const where = `"${name}" (#${index}, ${group ? `group ${group}` : 'composition'})`
			if (superseded) {
				this.log('info', `Skipped column ${where}: superseded by a newer press`)
			} else {
				this.log('debug', `Connected column ${where} in ${Date.now() - started} ms`)
			}
		} catch (err) {
			this.log('error', `Connect column "${name}" (${group ? `group ${group}` : 'composition'}) failed: ${err.message}`)
		}
	}

	async selectDeckAction(options, context) {
		const name = (await context.parseVariablesInString(String(options.name ?? ''))).trim()
		if (!name) {
			this.log('error', 'Select deck by name: no deck name set (an empty variable?)')
			return
		}
		const client = this.client
		if (!client) {
			this.log('error', `Select deck "${name}": Resolume host is not configured`)
			return
		}
		const started = Date.now()
		try {
			const { index, superseded } = await client.selectDeckByName(name)
			if (superseded) {
				this.log('info', `Skipped deck "${name}" (#${index}): superseded by a newer press`)
			} else {
				this.log('debug', `Selected deck "${name}" (#${index}) in ${Date.now() - started} ms`)
			}
		} catch (err) {
			this.log('error', `Select deck "${name}" failed: ${err.message}`)
		}
	}

	async sendOscAction(options, context) {
		const host = String(this.config.host || '').trim()
		if (!host) {
			this.log('error', 'Send OSC: Resolume host is not configured')
			return
		}
		try {
			const path = validateOscPath(await context.parseVariablesInString(String(options.path ?? '')))
			const value = await context.parseVariablesInString(String(options.value ?? ''))
			const args = buildOscArgs(options.type, value)
			const port = Number(this.config.oscPort || 7000)
			// UDP: fire-and-forget, Arena never confirms. Logged so a wrong port can be traced.
			this.log('debug', `OSC -> ${host}:${port} ${path} ${JSON.stringify(args)}`)
			this.oscSend(host, port, path, args)
		} catch (err) {
			this.log('error', `Send OSC failed: ${err.message}`)
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
						label: 'Column name (upper/lower case ignored)',
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
						step: 1,
					},
				],
				callback: (action, context) => this.connectColumnAction(action.options, context),
			},
			select_deck_by_name: {
				name: 'Select deck by name',
				description: 'Finds the deck by its name and selects it (Arena confirms). Variables allowed, e.g. $(AbleSet:activeSongName).',
				options: [
					{
						type: 'textinput',
						id: 'name',
						label: 'Deck name (upper/lower case ignored)',
						default: '',
						useVariables: true,
					},
				],
				callback: (action, context) => this.selectDeckAction(action.options, context),
			},
			send_osc: {
				name: 'Send OSC',
				description: 'Sends one OSC message to Resolume over UDP (no confirmation).',
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
						isVisibleExpression: "$(options:type) != 'none'",
					},
				],
				callback: (action, context) => this.sendOscAction(action.options, context),
			},
		}
	}
}

module.exports = { ResolumeSimpleInstance, parseLayerGroup }

runEntrypoint(ResolumeSimpleInstance, [])
