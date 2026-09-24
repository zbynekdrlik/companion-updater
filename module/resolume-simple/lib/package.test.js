'use strict'

const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const root = path.join(__dirname, '..')
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'))
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'companion', 'manifest.json'), 'utf8'))

test('manifest and package.json versions are bumped together', () => {
	assert.equal(manifest.version, pkg.version)
})

test('manifest apiVersion matches the pinned @companion-module/base', () => {
	assert.equal(manifest.runtime.apiVersion, pkg.dependencies['@companion-module/base'])
})

test('manifest entrypoint points at the real main file', () => {
	assert.equal(path.resolve(root, 'companion', manifest.runtime.entrypoint), path.resolve(root, pkg.main))
	assert.ok(fs.existsSync(path.join(root, pkg.main)))
})

test('manifest is not a prerelease and has the fields Companion requires', () => {
	assert.equal(manifest.isPrerelease, false)
	for (const key of ['id', 'name', 'shortname', 'version', 'runtime', 'manufacturer', 'products']) {
		assert.ok(manifest[key], `manifest.${key} missing`)
	}
	assert.equal(manifest.runtime.api, 'nodejs-ipc')
})
