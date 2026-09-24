'use strict'

/**
 * Turn the "Send OSC" action's options into Companion oscSend() arguments.
 * type: 'none' | 'i' | 'f' | 's'
 */
function buildOscArgs(type, rawValue) {
	const value = rawValue === undefined || rawValue === null ? '' : String(rawValue).trim()
	switch (type) {
		case 'none':
		case undefined:
		case '':
			return []
		case 'i': {
			if (!/^[-+]?\d+$/.test(value)) throw new Error(`"${value}" is not a whole number`)
			return [{ type: 'i', value: parseInt(value, 10) }]
		}
		case 'f': {
			const n = Number(value)
			if (value === '' || !Number.isFinite(n)) throw new Error(`"${value}" is not a number`)
			return [{ type: 'f', value: n }]
		}
		case 's':
			return [{ type: 's', value: rawValue === undefined || rawValue === null ? '' : String(rawValue) }]
		default:
			throw new Error(`Unknown OSC argument type "${type}"`)
	}
}

function validateOscPath(path) {
	const p = String(path || '').trim()
	if (!p.startsWith('/')) throw new Error(`OSC path must start with "/" (got "${p}")`)
	return p
}

module.exports = { buildOscArgs, validateOscPath }
