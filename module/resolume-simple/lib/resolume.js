'use strict'

/**
 * Minimal Resolume Arena REST client.
 *
 * Deliberately never downloads the whole composition and never opens the
 * WebSocket: on a big composition Arena pushes the full 14+ MB JSON to every
 * WebSocket client after each column change, which stalls a module for ~10 s
 * (companion-updater#10). Every call here touches a single ~2 KB resource.
 */

const DEFAULT_TIMEOUT_MS = 2000
const MAX_COLUMNS = 1024

class ResolumeError extends Error {
	constructor(message) {
		super(message)
		this.name = 'ResolumeError'
	}
}

function normalise(name) {
	return String(name).trim().toLowerCase()
}

function namesMatch(actual, wanted) {
	return actual === wanted || normalise(actual) === normalise(wanted)
}

/** 1-based index of `wanted` in `names`: exact match first, then case-insensitive. */
function findColumnIndex(names, wanted) {
	const exact = names.indexOf(wanted)
	if (exact >= 0) return exact + 1
	const w = normalise(wanted)
	const loose = names.findIndex((n) => normalise(n) === w)
	return loose >= 0 ? loose + 1 : undefined
}

function describeTarget(group) {
	return group ? `layer group ${group}` : 'the composition'
}

class ResolumeClient {
	/**
	 * @param {object} opts
	 * @param {string} opts.baseUrl e.g. http://10.77.9.201:8090
	 * @param {number} [opts.timeoutMs]
	 * @param {typeof fetch} [opts.fetchImpl]
	 */
	constructor({ baseUrl, timeoutMs = DEFAULT_TIMEOUT_MS, fetchImpl = globalThis.fetch }) {
		this.baseUrl = baseUrl.replace(/\/+$/, '')
		this.timeoutMs = timeoutMs
		this.fetch = fetchImpl
		/** group number (0 = composition) -> column names, index 0 = column 1 */
		this.columnNames = new Map()
	}

	async request(method, path) {
		const url = `${this.baseUrl}/api/v1${path}`
		try {
			return await this.fetch(url, { method, signal: AbortSignal.timeout(this.timeoutMs) })
		} catch (err) {
			const reason = err && err.name === 'TimeoutError' ? `no answer within ${this.timeoutMs} ms` : errorText(err)
			throw new ResolumeError(`${method} ${url} failed: ${reason}`)
		}
	}

	async product() {
		const res = await this.request('GET', '/product')
		if (!res.ok) throw new ResolumeError(`GET /product answered HTTP ${res.status}`)
		return res.json()
	}

	columnsPath(group) {
		return group ? `/composition/layergroups/${group}/columns` : '/composition/columns'
	}

	/** Name of one column, or undefined when the column does not exist. */
	async columnName(index, group = 0) {
		const res = await this.request('GET', `${this.columnsPath(group)}/${index}`)
		if (res.status === 404) return undefined
		if (!res.ok) throw new ResolumeError(`GET column ${index} of ${describeTarget(group)} answered HTTP ${res.status}`)
		const column = await res.json()
		return column && column.name && typeof column.name.value === 'string' ? column.name.value : ''
	}

	/** Re-read every column name of the composition (group 0) or of one layer group. */
	async refreshColumnNames(group = 0) {
		const names = []
		for (let index = 1; index <= MAX_COLUMNS; index++) {
			const name = await this.columnName(index, group)
			if (name === undefined) break
			names.push(name)
		}
		this.columnNames.set(group, names)
		return names
	}

	/**
	 * 1-based index of the column called `name`. A cached index is re-checked
	 * against that single column first (one tiny request), so renaming or
	 * moving columns in Arena is picked up without a periodic rescan.
	 */
	async resolveColumn(name, group = 0) {
		const cached = this.columnNames.get(group)
		const cachedIndex = cached && findColumnIndex(cached, name)
		if (cachedIndex !== undefined) {
			const current = await this.columnName(cachedIndex, group)
			if (current !== undefined && namesMatch(current, name)) return cachedIndex
		}
		const names = await this.refreshColumnNames(group)
		const index = findColumnIndex(names, name)
		if (index === undefined) {
			throw new ResolumeError(`No column named "${name}" in ${describeTarget(group)} (${names.length} columns checked)`)
		}
		return index
	}

	/** Short click on a column's connect button (Arena answers 204). */
	async connectColumn(index, group = 0) {
		const path = `${this.columnsPath(group)}/${index}/connect`
		const res = await this.request('POST', path)
		if (!res.ok) throw new ResolumeError(`POST ${path} answered HTTP ${res.status}`)
	}

	async connectColumnByName(name, group = 0) {
		const index = await this.resolveColumn(name, group)
		await this.connectColumn(index, group)
		return index
	}
}

function errorText(err) {
	if (!err) return 'unknown error'
	const cause = err.cause && (err.cause.code || err.cause.message)
	return cause ? `${err.message} (${cause})` : err.message
}

module.exports = { ResolumeClient, ResolumeError, findColumnIndex, DEFAULT_TIMEOUT_MS }
