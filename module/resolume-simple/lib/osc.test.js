'use strict'

const { test } = require('node:test')
const assert = require('node:assert/strict')
const { buildOscArgs, validateOscPath } = require('./osc')

test('no argument', () => {
	assert.deepEqual(buildOscArgs('none', 'ignored'), [])
})

test('integer argument', () => {
	assert.deepEqual(buildOscArgs('i', ' 42 '), [{ type: 'i', value: 42 }])
	assert.deepEqual(buildOscArgs('i', '-3'), [{ type: 'i', value: -3 }])
	assert.throws(() => buildOscArgs('i', '4.5'), /not a whole number/)
	assert.throws(() => buildOscArgs('i', ''), /not a whole number/)
})

test('float argument', () => {
	assert.deepEqual(buildOscArgs('f', '0.25'), [{ type: 'f', value: 0.25 }])
	assert.deepEqual(buildOscArgs('f', '1'), [{ type: 'f', value: 1 }])
	assert.throws(() => buildOscArgs('f', 'abc'), /not a number/)
	assert.throws(() => buildOscArgs('f', ''), /not a number/)
})

test('string argument keeps the text as typed', () => {
	assert.deepEqual(buildOscArgs('s', ' Hello '), [{ type: 's', value: ' Hello ' }])
})

test('unknown type is rejected', () => {
	assert.throws(() => buildOscArgs('x', '1'), /Unknown OSC argument type "x"/)
})

test('OSC path must start with a slash', () => {
	assert.equal(validateOscPath(' /composition/columns/2/connect '), '/composition/columns/2/connect')
	assert.throws(() => validateOscPath('composition/x'), /must start with "\/"/)
	assert.throws(() => validateOscPath(''), /must start with "\/"/)
})
