'use strict'

const { test, describe } = require('node:test')
const assert = require('node:assert/strict')
const { stepChoice, fNumberFromAv, snapToChoice, sliderValue, displayValues, planStep, StepError, KELVIN_SLIDER, TINT_SLIDER } = require('./steps')
const { ppCam6 } = require('../testing/fake-bkshading')

const ISO = [25, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000]

describe('stepChoice (mirror of bkshading mapping::step_choice)', () => {
	test('"+" is the first choice strictly above, "-" the last strictly below', () => {
		assert.equal(ISO[stepChoice(1600, ISO, 1)], 2000)
		assert.equal(ISO[stepChoice(1600, ISO, -1)], 1250)
	})

	test('an off-grid value lands on the grid in the pressed direction', () => {
		assert.equal(ISO[stepChoice(1500, ISO, 1)], 1600)
		assert.equal(ISO[stepChoice(1500, ISO, -1)], 1250)
	})

	test('the ends stay put', () => {
		assert.equal(stepChoice(2000, ISO, 1), ISO.length - 1)
		assert.equal(stepChoice(25, ISO, -1), 0)
		assert.equal(stepChoice(10, ISO, -1), 0) // below the grid
		assert.equal(stepChoice(99999, ISO, 1), ISO.length - 1)
	})

	test('no choices -> null, one choice -> 0', () => {
		assert.equal(stepChoice(5, [], 1), null)
		assert.equal(stepChoice(5, [8], -1), 0)
	})
})

describe('aperture helpers', () => {
	test('fNumberFromAv inverts AV = 2*log2(N)', () => {
		for (const f of [2.8, 4.8, 8, 16]) assert.ok(Math.abs(fNumberFromAv(2 * Math.log2(f)) - f) < 1e-9)
		assert.equal(fNumberFromAv(null), null)
		assert.equal(fNumberFromAv(undefined), null)
		assert.equal(fNumberFromAv(Number.NaN), null)
	})

	test('snapToChoice absorbs floating-point noise only', () => {
		assert.equal(snapToChoice(4.7999999, [4.5, 4.8, 5.2]), 4.8)
		assert.equal(snapToChoice(4.65, [4.5, 4.8, 5.2]), 4.65)
	})
})

describe('displayValues', () => {
	test('formats the pp cam6 reading', () => {
		assert.deepEqual(displayValues(ppCam6().state.params), { iso: '1600', fnumber: 'f/4.8', kelvin: '5000K', tint: '0' })
	})

	test('unknown values show "-"', () => {
		assert.deepEqual(displayValues(null), { iso: '-', fnumber: '-', kelvin: '-', tint: '-' })
		assert.deepEqual(displayValues({ iso: null, apertureAv: null, kelvin: null, tint: null }), { iso: '-', fnumber: '-', kelvin: '-', tint: '-' })
	})
})

describe('planStep on the real pp cam6 state', () => {
	const state = () => ppCam6().state

	test('ISO + / - move one camera choice and send the absolute ISO', () => {
		const up = planStep(state(), 'iso', 1)
		assert.deepEqual(up.patch, { iso: 2000 })
		assert.equal(up.label, '2000')
		assert.equal(up.params.iso, 2000)
		assert.equal(up.unchanged, false)
		assert.deepEqual(planStep(state(), 'iso', -1).patch, { iso: 1250 })
	})

	test('aperture + from f/4.8 goes to f/5.2 and sends apertureNorm = idx/(n-1)', () => {
		const s = state()
		const n = s.caps.fNumberChoices.length
		const up = planStep(s, 'aperture', 1)
		assert.deepEqual(up.patch, { apertureNorm: 7 / (n - 1) })
		assert.equal(up.label, 'f/5.2')
		assert.ok(Math.abs(fNumberFromAv(up.params.apertureAv) - 5.2) < 1e-9)
		const down = planStep(s, 'aperture', -1)
		assert.deepEqual(down.patch, { apertureNorm: 5 / (n - 1) })
		assert.equal(down.label, 'f/4.5')
	})

	test('aperture reading 4.7999999 still steps (not a dead press)', () => {
		const s = state()
		s.params.apertureAv = 2 * Math.log2(4.7999999)
		assert.equal(planStep(s, 'aperture', 1).label, 'f/5.2')
		assert.equal(planStep(s, 'aperture', -1).label, 'f/4.5')
	})

	test('WB steps 100 K and tint 1, clamped to the panel ranges', () => {
		assert.deepEqual(planStep(state(), 'kelvin', 1).patch, { kelvin: 5100 })
		assert.equal(planStep(state(), 'kelvin', -1).label, '4900K')
		assert.deepEqual(planStep(state(), 'tint', 1).patch, { tint: 1 })
		assert.deepEqual(planStep(state(), 'tint', -1).patch, { tint: -1 })
		const s = state()
		s.params.kelvin = 10000
		s.params.tint = -50
		assert.equal(planStep(s, 'kelvin', 1).unchanged, true)
		assert.equal(planStep(s, 'tint', -1).unchanged, true)
		s.params.kelvin = 2550
		assert.deepEqual(planStep(s, 'kelvin', -1).patch, { kelvin: 2500 })
	})

	test('every real f-number stop steps to its neighbours (AV round-trip noise included)', () => {
		// f/4.5 reads back just below its stop and f/4.8 just above: compared raw,
		// "+" from f/4.5 and "-" from f/4.8 would resend the same stop.
		const choices = ppCam6().state.caps.fNumberChoices
		for (let i = 0; i < choices.length; i++) {
			const s = state()
			s.params.apertureAv = 2 * Math.log2(choices[i])
			const up = planStep(s, 'aperture', 1)
			const down = planStep(s, 'aperture', -1)
			assert.equal(up.label, `f/${choices[Math.min(i + 1, choices.length - 1)].toFixed(1)}`, `+ from f/${choices[i]}`)
			assert.equal(down.label, `f/${choices[Math.max(i - 1, 0)].toFixed(1)}`, `- from f/${choices[i]}`)
			assert.equal(up.unchanged, i === choices.length - 1)
			assert.equal(down.unchanged, i === 0)
		}
	})

	test('WB steps from where the panel slider sits: snapped to 50 K and clamped', () => {
		const s = state()
		s.params.kelvin = 5627 // the slider shows 5650
		assert.deepEqual(planStep(s, 'kelvin', 1).patch, { kelvin: 5750 })
		assert.deepEqual(planStep(s, 'kelvin', -1).patch, { kelvin: 5550 })
		s.params.kelvin = 2000 // below the slider: the panel's "-" is disabled
		assert.equal(planStep(s, 'kelvin', -1).unchanged, true)
		assert.deepEqual(planStep(s, 'kelvin', 1).patch, { kelvin: 2600 })
		s.params.kelvin = 12000
		assert.equal(planStep(s, 'kelvin', 1).unchanged, true)
		assert.deepEqual(planStep(s, 'kelvin', -1).patch, { kelvin: 9900 })
	})

	test('sliderValue mirrors an <input type=range>', () => {
		assert.equal(sliderValue(5627, KELVIN_SLIDER), 5650)
		assert.equal(sliderValue(5624, KELVIN_SLIDER), 5600)
		assert.equal(sliderValue(1000, KELVIN_SLIDER), 2500)
		assert.equal(sliderValue(99999, KELVIN_SLIDER), 10000)
		assert.equal(sliderValue(-80, TINT_SLIDER), -50)
		assert.equal(sliderValue(2.4, TINT_SLIDER), 2)
	})

	test('at the top ISO choice "+" is reported unchanged', () => {
		const s = state()
		s.params.iso = 16000
		const r = planStep(s, 'iso', 1)
		assert.equal(r.unchanged, true)
		assert.equal(r.label, '16000')
	})
})

describe('planStep refuses with a reason instead of guessing', () => {
	test('no relay state, camera offline, no choices, no current value', () => {
		assert.throws(() => planStep(null, 'iso', 1), (e) => e instanceof StepError && /relay is not reachable/.test(e.message))
		assert.throws(() => planStep({ online: false, params: {} }, 'iso', 1), /camera is offline/)
		const noCaps = ppCam6().state
		noCaps.caps = null
		assert.throws(() => planStep(noCaps, 'iso', 1), /no ISO choices/)
		assert.throws(() => planStep(noCaps, 'aperture', 1), /no f-number choices/)
		const noIso = ppCam6().state
		noIso.params.iso = null
		assert.throws(() => planStep(noIso, 'iso', 1), /no current ISO/)
		const noAv = ppCam6().state
		noAv.params.apertureAv = null
		assert.throws(() => planStep(noAv, 'aperture', 1), /no current aperture/)
		const noWb = ppCam6().state
		noWb.params.kelvin = null
		assert.throws(() => planStep(noWb, 'kelvin', 1), /no current white balance/)
	})

	test('bad parameter or direction', () => {
		assert.throws(() => planStep(ppCam6().state, 'shutter', 1), /unknown parameter/)
		assert.throws(() => planStep(ppCam6().state, 'iso', 0), /direction/)
	})
})
