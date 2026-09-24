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
const MAX_ITEMS = 1024

class ResolumeError extends Error {
	constructor(message) {
		super(message)
		this.name = 'ResolumeError'
	}
}

function normalise(name) {
	return String(name).trim().toLowerCase()
}

/**
 * The names a column can be addressed by. Arena shows "#" in a name as the
 * column number ("Column #" is displayed as "Column 3"), while REST returns
 * the raw template, so both spellings are accepted.
 */
function columnAliases(rawName, index) {
	const raw = String(rawName)
	const shown = raw.replace(/#/g, String(index))
	return shown === raw ? [raw] : [raw, shown]
}

/** 'exact' | 'loose' | undefined: how column `index` (raw name `rawName`) matches `wanted`. */
function columnMatch(rawName, index, wanted) {
	const w = String(wanted).trim()
	if (!w) return undefined
	const aliases = columnAliases(rawName, index)
	if (aliases.includes(w)) return 'exact'
	const lw = normalise(w)
	return aliases.some((a) => normalise(a) === lw) ? 'loose' : undefined
}

/**
 * 1-based index of `wanted` in `names` (index 0 = column 1). An exact match
 * anywhere wins over a case-insensitive one; within each pass the first
 * column wins. Empty names never match.
 */
function findColumnIndex(names, wanted) {
	for (const kind of ['exact', 'loose']) {
		for (let i = 0; i < names.length; i++) {
			if (columnMatch(names[i], i + 1, wanted) === kind) return i + 1
		}
	}
	return undefined
}

/**
 * A named list in Arena that the module can address by name:
 * the composition's columns, a layer group's columns, or the decks.
 */
function columnsOf(group) {
	return group
		? { key: `columns:${group}`, path: `/composition/layergroups/${group}/columns`, what: 'column', where: `layer group ${group}`, verb: 'connect' }
		: { key: 'columns:0', path: '/composition/columns', what: 'column', where: 'the composition', verb: 'connect' }
}
const DECKS = { key: 'decks', path: '/composition/decks', what: 'deck', where: 'the deck list', verb: 'select' }

/** Release the socket of a response whose body we do not need. */
async function discardBody(res) {
	try {
		await res.body?.cancel()
	} catch {
		// the body is irrelevant; nothing to do
	}
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
		/** list key -> { list, names } (names: raw, index 0 = item 1) */
		this.cache = new Map()
		/** list key -> sequence of the latest press; an older press never overrides a newer one */
		this.pressSeq = new Map()
		/** list key -> promise chain: trigger POSTs of one list go out one after another, in press order */
		this.postChains = new Map()
	}

	failure(method, path, err) {
		const reason = err && err.name === 'TimeoutError' ? `no answer within ${this.timeoutMs} ms` : errorText(err)
		return new ResolumeError(`${method} ${this.baseUrl}/api/v1${path} failed: ${reason}`)
	}

	/** One request; `read` consumes the response (inside the same timeout and error mapping). */
	async request(method, path, read) {
		const url = `${this.baseUrl}/api/v1${path}`
		try {
			const res = await this.fetch(url, { method, signal: AbortSignal.timeout(this.timeoutMs) })
			return await read(res)
		} catch (err) {
			if (err instanceof ResolumeError) throw err
			throw this.failure(method, path, err)
		}
	}

	async product() {
		return this.request('GET', '/product', async (res) => {
			if (!res.ok) {
				await discardBody(res)
				throw new ResolumeError(`GET /product answered HTTP ${res.status}`)
			}
			return res.json()
		})
	}

	/** Raw name of one item of `list`, or undefined when it does not exist. */
	async itemName(list, index) {
		return this.request('GET', `${list.path}/${index}`, async (res) => {
			if (res.status === 404) {
				await discardBody(res)
				return undefined
			}
			if (!res.ok) {
				await discardBody(res)
				throw new ResolumeError(`GET ${list.what} ${index} of ${list.where} answered HTTP ${res.status}`)
			}
			const item = await res.json()
			return item && item.name && typeof item.name.value === 'string' ? item.name.value : ''
		})
	}

	/** Re-read every name of `list`. */
	async refreshNames(list) {
		const names = []
		for (let index = 1; index <= MAX_ITEMS; index++) {
			const name = await this.itemName(list, index)
			if (name === undefined) break
			names.push(name)
		}
		this.cache.set(list.key, { list, names })
		return names
	}

	/** Every list used so far (for background refresh). */
	knownLists() {
		return [...this.cache.values()].map((entry) => entry.list)
	}

	/**
	 * 1-based index of the item called `name`. A cached index is re-checked
	 * against that single item first (one tiny request), so renaming or
	 * moving things in Arena is picked up without waiting for a rescan.
	 * Known limit: if the cached item still matches, a better match added
	 * elsewhere (an exact spelling, or an earlier duplicate) is only seen after
	 * the next background refresh (every 30 s, see index.js).
	 */
	async resolve(list, name) {
		const cached = this.cache.get(list.key)
		const cachedIndex = cached && findColumnIndex(cached.names, name)
		if (cachedIndex !== undefined) {
			const current = await this.itemName(list, cachedIndex)
			if (current !== undefined && columnMatch(current, cachedIndex, name)) return cachedIndex
		}
		const names = await this.refreshNames(list)
		if (names.length === 0) {
			throw new ResolumeError(`${list.where} has no ${list.what}s or does not exist`)
		}
		const index = findColumnIndex(names, name)
		if (index === undefined) {
			throw new ResolumeError(`No ${list.what} named "${name}" in ${list.where} (${names.length} ${list.what}s checked)`)
		}
		return index
	}

	/** POST .../{index}/connect|select (Arena answers 204). */
	async trigger(list, index) {
		const path = `${list.path}/${index}/${list.verb}`
		await this.request('POST', path, async (res) => {
			await discardBody(res)
			if (!res.ok) throw new ResolumeError(`POST ${path} answered HTTP ${res.status}`)
		})
	}

	/**
	 * Resolve and trigger. If a newer press on the same list started while
	 * this one was still resolving its name, this press is dropped
	 * (`superseded: true`) so the operator's LAST press always wins, even if
	 * that newer press then fails. POSTs of one list are sent in press order;
	 * columns and decks queue independently.
	 * @returns {Promise<{index: number, superseded: boolean}>}
	 */
	async triggerByName(list, name) {
		const seq = (this.pressSeq.get(list.key) || 0) + 1
		this.pressSeq.set(list.key, seq)
		const index = await this.resolve(list, name)
		const chain = this.postChains.get(list.key) || Promise.resolve()
		const send = chain.then(async () => {
			if (seq !== this.pressSeq.get(list.key)) return { index, superseded: true }
			await this.trigger(list, index)
			return { index, superseded: false }
		})
		this.postChains.set(
			list.key,
			send.catch(() => {}),
		)
		return send
	}

	// Columns (group 0 = the composition's own columns)
	columnName(index, group = 0) {
		return this.itemName(columnsOf(group), index)
	}
	refreshColumnNames(group = 0) {
		return this.refreshNames(columnsOf(group))
	}
	resolveColumn(name, group = 0) {
		return this.resolve(columnsOf(group), name)
	}
	connectColumn(index, group = 0) {
		return this.trigger(columnsOf(group), index)
	}
	connectColumnByName(name, group = 0) {
		return this.triggerByName(columnsOf(group), name)
	}

	// Decks
	selectDeckByName(name) {
		return this.triggerByName(DECKS, name)
	}
}

function errorText(err) {
	if (!err) return 'unknown error'
	const cause = err.cause && (err.cause.code || err.cause.message)
	return cause ? `${err.message} (${cause})` : err.message
}

module.exports = { ResolumeClient, ResolumeError, findColumnIndex, columnsOf, DECKS, DEFAULT_TIMEOUT_MS }
