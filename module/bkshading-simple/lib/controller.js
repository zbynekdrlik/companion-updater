'use strict'

const { planStep, displayValues, StepError } = require('./steps')

/** bkshading wire key reported in `notApplied` for each step parameter. */
const WIRE_KEY = { iso: 'iso', aperture: 'apertureNorm', kelvin: 'kelvin', tint: 'tint' }
/** displayValues() key for each step parameter. */
const DISPLAY_KEY = { iso: 'iso', aperture: 'fnumber', kelvin: 'kelvin', tint: 'tint' }

/**
 * The module's view of bkshading: the latest camera list, which camera the
 * operator selected, and the writes.
 *
 * Writes to ONE camera go out strictly one after another, in press order, and
 * each step is computed when its turn comes, from the value the previous write
 * set. So three fast "+" presses are three steps (never three times the same
 * target), and the relay never gets two gphoto2 writes at once. No press is
 * dropped. Different cameras do not wait for each other.
 */
class ShadingController {
	/**
	 * @param {object} opts
	 * @param {{ setParams: (id: string, patch: object) => Promise<unknown> }} opts.client
	 * @param {() => number} [opts.now] a MONOTONIC clock: "did this poll leave
	 *   before that write ended?" must not flip when NTP steps the wall clock.
	 */
	constructor({ client, now = () => performance.now() }) {
		this.client = client
		this.now = now
		/** id -> camera view from GET /api/cameras (params patched by our own writes) */
		this.cameras = new Map()
		this.order = []
		this.serviceVersion = null
		this.selected = null
		/** id -> promise chain of that camera's writes */
		this.chains = new Map()
		/** id -> number of writes queued or running */
		this.busy = new Map()
		/** id -> time the last write to that camera finished */
		this.lastWriteDone = new Map()
	}

	/**
	 * Take a fresh GET /api/cameras answer. `pollStartedAt` (from `now()`) is
	 * when that GET was sent. For a camera written to after it, or still being
	 * written to, the answer may predate the write:
	 * - the params our own write set are kept, so a slow poll can never roll a
	 *   step back (everything else - online, caps, notApplied - is taken);
	 * - a "relay not reachable" answer is ignored: the relay is busy with our
	 *   write (its read waits for the write burst, and the service gives up on
	 *   a relay poll after 1.5 s), and taking it would fail the queued presses.
	 */
	applyAggregate(aggregate, pollStartedAt) {
		this.serviceVersion = aggregate.version ?? null
		const next = new Map()
		for (const cam of aggregate.cameras) {
			if (!cam || typeof cam.id !== 'string') continue
			const own = this.cameras.get(cam.id)
			const stale = (this.busy.get(cam.id) || 0) > 0 || (this.lastWriteDone.get(cam.id) ?? -Infinity) >= pollStartedAt
			if (stale && own && own.reachable && own.state && !(cam.reachable && cam.state)) {
				next.set(cam.id, own)
			} else if (stale && own && own.state && cam.state) {
				next.set(cam.id, { ...cam, state: { ...cam.state, params: own.state.params } })
			} else {
				next.set(cam.id, cam)
			}
		}
		this.cameras = next
		this.order = [...next.keys()]
	}

	/** Forget the camera list (the service is not reachable). The selection is kept. */
	clear() {
		this.cameras = new Map()
		this.order = []
	}

	camera(id) {
		return this.cameras.get(id)
	}

	select(id) {
		this.selected = id
	}

	/** True when the camera is in bkshading, its relay answers and the camera is on USB. */
	isOnline(id) {
		const cam = this.cameras.get(id)
		return Boolean(cam && cam.reachable && cam.state && cam.state.online)
	}

	/** True when bkshading says the camera ignored the last write of `param`. */
	notApplied(id, param) {
		const cam = this.cameras.get(id)
		const list = cam && cam.state && Array.isArray(cam.state.notApplied) ? cam.state.notApplied : []
		return list.includes(WIRE_KEY[param])
	}

	/**
	 * Queue one step of `param` (+1 / -1) for camera `id`.
	 * Resolves to { from, label, unchanged, patch, ms } once the camera has the
	 * new value (`from` and `ms` are measured when the step's turn came, not
	 * when it was queued), or rejects with a StepError (cannot step) /
	 * BkshadingError (write failed).
	 */
	step(id, param, dir) {
		this.busy.set(id, (this.busy.get(id) || 0) + 1)
		const run = (this.chains.get(id) || Promise.resolve()).then(() => this.runStep(id, param, dir))
		const done = run.finally(() => {
			this.busy.set(id, this.busy.get(id) - 1)
			this.lastWriteDone.set(id, this.now())
		})
		// The chain must survive a failed step: the next press still runs.
		this.chains.set(
			id,
			done.catch(() => {}),
		)
		return done
	}

	async runStep(id, param, dir) {
		const cam = this.cameras.get(id)
		if (!cam) {
			const known = this.order.length ? this.order.join(', ') : 'none'
			throw new StepError(`camera "${id}" is not in bkshading (cameras: ${known})`)
		}
		if (!cam.reachable) throw new StepError(`the relay of ${id} is not reachable`)
		const started = this.now()
		const plan = planStep(cam.state, param, dir)
		const from = displayValues(cam.state.params)[DISPLAY_KEY[param]]
		if (plan.unchanged) return { from, label: plan.label, unchanged: true, ms: 0 }
		await this.client.setParams(id, plan.patch)
		// bkshading reflects a write in its very next GET; mirror that now so the
		// next queued step starts from the new value.
		const current = this.cameras.get(id)
		if (current && current.state) {
			this.cameras.set(id, { ...current, state: { ...current.state, params: plan.params } })
		}
		return { from, label: plan.label, unchanged: false, patch: plan.patch, ms: Math.round(this.now() - started) }
	}
}

module.exports = { ShadingController, WIRE_KEY }
