'use strict'

/**
 * One +/- step of a camera parameter, computed exactly like the bkshading
 * web panel (zbynekdrlik/camera-box bkshading/service/web/app.js), so a
 * Companion press and a panel tap move the camera the same way.
 *
 * - ISO and aperture step ONE camera choice (`stepChoice`, the JS mirror of
 *   the proto `mapping::step_choice`): "+" = the first choice strictly above
 *   the current value, "-" = the last strictly below; an off-grid value lands
 *   on the grid. ISO is sent as the absolute `iso`, aperture as
 *   `apertureNorm = idx/(n-1)` (the inverse of the relay's
 *   norm_to_choice_index over the same list).
 * - White balance steps KELVIN_STEP K and tint TINT_STEP, clamped to the
 *   panel's slider ranges.
 *
 * Pure: no I/O, no Companion import.
 */

const KELVIN_STEP = 100
const TINT_STEP = 1
/** The panel's sliders (web/index.html): min, max and the slider's own step grid. */
const KELVIN_SLIDER = { min: 2500, max: 10000, grid: 50 }
const TINT_SLIDER = { min: -50, max: 50, grid: 1 }
/**
 * apertureAv -> f-number goes through floating point (2^(av/2)), so a lens
 * sitting exactly on a stop reads back a hair off it: on the pp cam6 list
 * f/4.5, 6.2, 10, 12, 17, 19, 25 and 27 come back just below the stop and
 * f/3.5, 4.8, 6.7, 7.3, 14, 15 and 21 just above. Within this distance of a
 * choice the value counts as that choice, or "+" (resp. "-") would "step" to
 * the same f-number and the press would do nothing. The bkshading panel
 * itself has that dead press (it compares the raw value); this module
 * deliberately does not copy the bug.
 */
const SNAP_EPSILON = 0.005

const PARAMS = ['iso', 'aperture', 'kelvin', 'tint']

class StepError extends Error {
	constructor(message) {
		super(message)
		this.name = 'StepError'
	}
}

/** Index of the next choice for a step, or null when there are no choices. Ascending choices. */
function stepChoice(current, choices, dir) {
	const n = choices.length
	if (n === 0) return null
	if (n === 1) return 0
	if (dir > 0) {
		const i = choices.findIndex((c) => c > current)
		return i === -1 ? n - 1 : i
	}
	for (let i = n - 1; i >= 0; i--) if (choices[i] < current) return i
	return 0
}

/** The f-number for bkshading's `apertureAv` (AV = 2*log2(N)), or null. */
function fNumberFromAv(av) {
	return typeof av === 'number' && Number.isFinite(av) ? Math.sqrt(Math.pow(2, av)) : null
}

/** `value`, replaced by the choice it is within SNAP_EPSILON of (floating-point noise). */
function snapToChoice(value, choices) {
	for (const c of choices) if (Math.abs(c - value) <= SNAP_EPSILON) return c
	return value
}

function clamp(value, min, max) {
	return Math.min(max, Math.max(min, value))
}

/**
 * Where the panel's slider sits for a camera value: an <input type=range>
 * clamps to min..max and snaps to its step grid (counted from min). The panel
 * steps WB and tint from that position, so this module does too.
 */
function sliderValue(value, { min, max, grid }) {
	const snapped = min + Math.round((clamp(value, min, max) - min) / grid) * grid
	return snapped > max ? snapped - grid : snapped
}

function formatFNumber(f) {
	return `f/${f.toFixed(1)}`
}

/** Display strings for a camera's current params (`-` when unknown). */
function displayValues(params) {
	const p = params || {}
	const f = fNumberFromAv(p.apertureAv)
	return {
		iso: typeof p.iso === 'number' ? String(p.iso) : '-',
		fnumber: f === null ? '-' : formatFNumber(f),
		kelvin: typeof p.kelvin === 'number' ? `${p.kelvin}K` : '-',
		tint: typeof p.tint === 'number' ? String(p.tint) : '-',
	}
}

function numericChoices(list) {
	return Array.isArray(list) ? list.filter((v) => typeof v === 'number' && Number.isFinite(v)) : []
}

/**
 * Plan one step of `param` in direction `dir` (+1 / -1) for a camera's
 * bkshading `state` ({ online, params, caps }).
 *
 * Returns { patch, params, label, unchanged }:
 *   patch     the body for PUT /api/cameras/:id/params
 *   params    the params the camera will have once it applies the patch
 *   label     the new value as shown on the button
 *   unchanged true when the value is already at its end (no PUT needed)
 * Throws StepError with a readable reason when the step cannot be made.
 */
function planStep(state, param, dir) {
	if (!PARAMS.includes(param)) throw new StepError(`unknown parameter "${param}"`)
	if (dir !== 1 && dir !== -1) throw new StepError(`direction must be +1 or -1, got ${dir}`)
	if (!state) throw new StepError('the camera relay is not reachable')
	if (!state.online) throw new StepError('the camera is offline (relay up, no camera on USB)')
	const params = state.params || {}
	const caps = state.caps || {}

	if (param === 'iso') {
		const choices = numericChoices(caps.isoChoices)
		if (choices.length < 2) throw new StepError('the camera reports no ISO choices')
		if (typeof params.iso !== 'number') throw new StepError('the camera reports no current ISO')
		const value = choices[stepChoice(params.iso, choices, dir)]
		return { patch: { iso: value }, params: { ...params, iso: value }, label: String(value), unchanged: value === params.iso }
	}

	if (param === 'aperture') {
		const choices = numericChoices(caps.fNumberChoices)
		if (choices.length < 2) throw new StepError('the camera reports no f-number choices')
		const current = fNumberFromAv(params.apertureAv)
		if (current === null) throw new StepError('the camera reports no current aperture')
		const snapped = snapToChoice(current, choices)
		const idx = stepChoice(snapped, choices, dir)
		const target = choices[idx]
		const norm = idx / (choices.length - 1)
		return {
			patch: { apertureNorm: norm },
			params: { ...params, apertureAv: 2 * Math.log2(target), apertureNorm: norm },
			label: formatFNumber(target),
			unchanged: target === snapped,
		}
	}

	const [key, step, slider, suffix] = param === 'kelvin' ? ['kelvin', KELVIN_STEP, KELVIN_SLIDER, 'K'] : ['tint', TINT_STEP, TINT_SLIDER, '']
	const current = params[key]
	if (typeof current !== 'number') throw new StepError(`the camera reports no current ${key === 'kelvin' ? 'white balance' : 'tint'}`)
	const from = sliderValue(current, slider)
	const value = clamp(from + dir * step, slider.min, slider.max)
	// At the slider's end the panel disables the button: nothing is sent, even
	// when the camera itself sits outside the slider range (e.g. 2000 K).
	return { patch: { [key]: value }, params: { ...params, [key]: value }, label: `${value}${suffix}`, unchanged: value === from }
}

module.exports = {
	PARAMS,
	KELVIN_STEP,
	TINT_STEP,
	KELVIN_SLIDER,
	TINT_SLIDER,
	StepError,
	stepChoice,
	fNumberFromAv,
	snapToChoice,
	sliderValue,
	displayValues,
	planStep,
}
