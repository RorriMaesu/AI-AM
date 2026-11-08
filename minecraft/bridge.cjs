'use strict'
// The body executes named skills. It never evaluates model-produced JavaScript.
// All networking is loopback. No bridge state or authentication cache is written.
const http = require('node:http')
const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')

const SKILLS = Object.freeze(['look', 'navigate', 'collect', 'craft', 'eat', 'equip', 'place', 'chat', 'wait', 'flee', 'give'])
const HOSTILES = new Set(['zombie', 'husk', 'drowned', 'skeleton', 'stray', 'creeper', 'spider', 'cave_spider', 'witch', 'pillager', 'vindicator', 'phantom', 'slime', 'magma_cube'])
const FOODS = new Set(['apple', 'bread', 'baked_potato', 'carrot', 'golden_carrot', 'cooked_beef', 'cooked_porkchop', 'cooked_chicken', 'cooked_mutton', 'cooked_rabbit', 'cooked_cod', 'cooked_salmon', 'melon_slice', 'sweet_berries', 'glow_berries', 'potato', 'beetroot'])
const CROPS = new Set(['wheat', 'carrots', 'potatoes', 'beetroots', 'sweet_berry_bush'])
const LIVE = new Set(['running'])
const clone = value => JSON.parse(JSON.stringify(value))
const pos = p => ({ x: Math.round(p.x * 100) / 100, y: Math.round(p.y * 100) / 100, z: Math.round(p.z * 100) / 100 })
const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z)
const blockId = p => 'block:' + [Math.floor(p.x), Math.floor(p.y), Math.floor(p.z)].join(',')
const finite = n => typeof n === 'number' && Number.isFinite(n)
const error = (message, status = 400) => Object.assign(new Error(message), { status })
const angle = n => Math.atan2(Math.sin(n), Math.cos(n))
function assert (value, message, status) { if (!value) throw error(message, status) }

function validateConfig (input) {
  assert(input && typeof input === 'object' && !Array.isArray(input), 'Configuration must be an object')
  assert(['127.0.0.1', '::1'].includes(input.server_host || '127.0.0.1'), 'Minecraft server must be a loopback IP literal')
  const port = (n, fallback) => {
    n = n === undefined ? fallback : n
    assert(Number.isInteger(n) && n > 0 && n <= 65535, 'Invalid TCP port')
    return n
  }
  assert(input.version === undefined || input.version === '1.21.4', 'This pilot pins Minecraft Java 1.21.4')
  assert(Array.isArray(input.agents) && input.agents.length >= 1 && input.agents.length <= 2, 'Configure one or two agents')
  const ids = new Set(); const names = new Set(); const ports = new Set()
  const serverPort = port(input.server_port, 25565)
  const bridgePort = port(input.bridge_port, 8766)
  ports.add(serverPort); assert(!ports.has(bridgePort), 'Ports must be distinct'); ports.add(bridgePort)
  const agents = input.agents.map((a, index) => {
    assert(a && /^[a-z][a-z0-9_-]{0,31}$/.test(a.id), 'Invalid agent id')
    assert(typeof a.username === 'string' && /^[A-Za-z0-9_]{1,16}$/.test(a.username), 'Invalid Minecraft username')
    assert(!ids.has(a.id) && !names.has(a.username.toLowerCase()), 'Agent ids and usernames must be unique')
    ids.add(a.id); names.add(a.username.toLowerCase())
    const viewerPort = port(a.viewer_port, 8767 + index)
    assert(!ports.has(viewerPort), 'Ports must be distinct'); ports.add(viewerPort)
    return { id: a.id, username: a.username, viewer_port: viewerPort }
  })
  return { server_host: input.server_host || '127.0.0.1', server_port: serverPort, version: '1.21.4', bridge_port: bridgePort, agents }
}
function inventory (bot) {
  const result = {}
  for (const item of bot.inventory.items()) result[item.name] = (result[item.name] || 0) + item.count
  return result
}
function inventoryDelta (before, after) {
  return Object.fromEntries([...new Set([...Object.keys(before), ...Object.keys(after)])].map(k => [k, (after[k] || 0) - (before[k] || 0)]).filter(([, n]) => n !== 0))
}
function checkSignal (signal) { if (signal.aborted) throw error('Action interrupted', 409) }
function delay (ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(error('Action interrupted', 409))
    const finish = () => { signal?.removeEventListener('abort', abort); resolve() }
    const timer = setTimeout(finish, ms)
    const abort = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(error('Action interrupted', 409)) }
    signal?.addEventListener('abort', abort, { once: true })
  })
}
async function until (predicate, timeout, signal) {
  const deadline = Date.now() + timeout
  do {
    checkSignal(signal)
    if (predicate()) return
    await delay(50, signal)
  } while (Date.now() < deadline)
  throw error('Server confirmation was not observed', 409)
}

class AgentBody {
  constructor (bot, deps, config, options = {}) {
    this.bot = bot; this.deps = deps; this.config = config
    this.clock = options.clock || Date.now
    this.session = options.session || crypto.randomUUID()
    this.connected = false; this.alive = false; this.enabled = false
    this.records = options.records || new Map()
    this.observations = new Map(); this.sequence = 0; this.active = null
    this.chats = []; this.reflexes = []; this.death = null; this.lastError = null
    this.leaseUntil = 0; this.lastReflex = 0; this.emergency = false; this.closed = false
    this.lastProgress = null; this.timer = null
    bot.on('spawn', () => { this.connected = true; this.alive = bot.health > 0; this.observations.clear() })
    // Mineflayer emits the initial spawn before assigning health from the same packet.
    bot.on('health', () => { if (this.connected) this.alive = bot.health > 0 })
    bot.on('death', () => {
      // A player that logs in with zero health can die before the first spawn event.
      this.connected = true
      this.alive = false; this.death = { id: 'death:' + this.session + ':' + ++this.sequence, at: this.clock() }
      this.stop('death')
    })
    bot.on('end', reason => {
      this.connected = false; this.alive = false; this.enabled = false; this.observations.clear()
      this.interrupt('connection ended: ' + String(reason).slice(0, 120), 'outcome_unknown')
    })
    bot.on('kicked', reason => { this.lastError = 'kicked: ' + String(reason).slice(0, 200) })
    bot.on('error', err => { this.lastError = String(err.message || err).slice(0, 200) })
    bot.on('chat', (username, message) => {
      if (typeof message !== 'string' || typeof username !== 'string') return
      const entity = username === bot.username ? bot.entity : bot.players?.[username]?.entity
      // Hearing is proximity-scoped; a global chat packet does not give omniscience.
      if (!entity?.position || !bot.entity?.position || distance(entity.position, bot.entity.position) > 16) return
      this.chats.push({ id: 'chat:' + this.session + ':' + ++this.sequence, username, text: message.slice(0, 256), at: this.clock(), channel: 'nearby_chat' })
      this.chats = this.chats.slice(-8)
    })
    if (options.reflexTimer !== false) {
      this.timer = setInterval(() => { try { this.reflexTick() } catch (err) { this.lastError = err.message } }, 250)
      this.timer.unref()
    }
  }

  vector (p) { return new this.deps.Vec3(p.x, p.y, p.z) }
  eye () {
    const p = this.bot.entity.position
    return { x: p.x, y: p.y + (this.bot.entity.height || 1.8) * 0.9, z: p.z }
  }
  visiblePoint (p, ignoreBlock = null) {
    const eye = this.eye(); const d = distance(eye, p)
    if (d > 12 || d < 0.01) return d < 0.01
    const dx = p.x - eye.x; const dy = p.y - eye.y; const dz = p.z - eye.z
    const yaw = Math.atan2(-dx, -dz)
    if (Math.abs(angle(yaw - (this.bot.entity.yaw || 0))) > Math.PI * 0.39) return false
    if (Math.abs(angle(Math.atan2(dy, Math.hypot(dx, dz)) - (this.bot.entity.pitch || 0))) > Math.PI * 0.32) return false
    // Collision-ray test deliberately does not reveal the hidden ray's contents.
    for (let t = 0.25; t < d; t += 0.25) {
      const point = { x: eye.x + dx * t / d, y: eye.y + dy * t / d, z: eye.z + dz * t / d }
      if (ignoreBlock && blockId(point) === ignoreBlock) continue
      const block = this.bot.blockAt(this.vector(point))
      if (!block || block.boundingBox === 'block') return false
    }
    return true
  }
  visibleBlocks () {
    const bot = this.bot
    const resource = block => block && (
      CROPS.has(block.name) ||
      /_(log|stem|wood|ore|bed|planks)$/.test(block.name) ||
      ['crafting_table', 'furnace', 'chest', 'hay_block'].includes(block.name)
    )
    const terrain = block => block && (
      ['dirt', 'grass_block', 'stone', 'cobblestone', 'deepslate', 'sand', 'gravel'].includes(block.name) ||
      /_leaves$/.test(block.name)
    )
    const inView = block => {
      const p = block.position
      const center = { x: p.x + .5, y: p.y + .5, z: p.z + .5 }
      return this.visiblePoint(center, blockId(p)) && (CROPS.has(block.name) || bot.canSeeBlock(block))
    }
    // Apply line of sight before the search cap so nearby unseen terrain does
    // not exhaust the search before it reaches a visible resource.
    const resources = bot.findBlocks({ matching: resource, useExtraInfo: inView, maxDistance: 12, count: 18 })
    const surroundings = bot.findBlocks({ matching: terrain, useExtraInfo: inView, maxDistance: 12, count: 18 })
    const result = []
    for (const p of resources.concat(surroundings).slice(0, 18)) {
      const block = bot.blockAt(p)
      if (!block) continue
      const age = CROPS.has(block.name) ? { age: block.getProperties?.().age ?? null } : {}
      result.push({
        id: blockId(p), name: block.name, ...age, position: pos(p),
        distance: Math.round(distance(bot.entity.position, p) * 10) / 10
      })
    }
    return result
  }
  nearbyEntities () {
    return Object.values(this.bot.entities || {}).filter(e => {
      if (!e?.position || e.id === this.bot.entity.id) return false
      return this.visiblePoint({ x: e.position.x, y: e.position.y + (e.height || 1) / 2, z: e.position.z })
    }).sort((a, b) => distance(this.bot.entity.position, a.position) - distance(this.bot.entity.position, b.position))
      .slice(0, 8).map(e => ({ id: 'entity:' + e.id, name: e.name || e.type || 'unknown', username: e.username || null, position: pos(e.position), distance: Math.round(distance(this.bot.entity.position, e.position) * 10) / 10 }))
  }
  craftingTable (visible) {
    const entry = visible.find(b => b.name === 'crafting_table' && b.distance <= 4)
    return entry ? this.bot.blockAt(this.vector(entry.position)) : null
  }
  recipes (visible) {
    const table = this.craftingTable(visible)
    const names = ['oak_planks', 'spruce_planks', 'birch_planks', 'jungle_planks', 'acacia_planks', 'dark_oak_planks', 'mangrove_planks', 'cherry_planks', 'bamboo_planks', 'stick', 'crafting_table', 'wooden_pickaxe', 'wooden_axe', 'stone_pickaxe', 'stone_axe', 'furnace', 'torch', 'wheat', 'bread', 'bowl', 'white_bed']
    return names.filter(name => {
      const item = this.bot.registry.itemsByName[name]
      return item && this.bot.recipesFor(item.id, null, 1, table).length > 0
    }).slice(0, 12)
  }
  observe () {
    const ready = this.connected && this.alive && this.bot.entity?.position
    const visible = ready ? this.visibleBlocks() : []
    const nearby = ready ? this.nearbyEntities() : []
    const observationId = 'obs:' + this.session + ':' + ++this.sequence
    const snapshot = { at: this.clock(), visible, nearby }
    this.observations.set(observationId, snapshot)
    for (const [id, observation] of this.observations) {
      if (this.clock() - observation.at > 180000 || this.observations.size > 256) this.observations.delete(id)
    }
    const inv = ready ? inventory(this.bot) : {}
    return {
      id: this.config.id, observation_id: observationId, observed_at: snapshot.at, body_session: this.session,
      connected: this.connected, alive: this.alive, enabled: this.enabled, lease_expires_at: this.leaseUntil,
      dimension: this.bot.game?.dimension || null, game_mode: this.bot.game?.gameMode || null,
      health: ready ? this.bot.health : 0, food: ready ? this.bot.food : 0,
      oxygen: ready ? this.bot.oxygenLevel : null,
      position: ready ? pos(this.bot.entity.position) : null,
      inventory: Object.entries(inv).slice(0, 16).map(([name, count]) => ({ name, count })),
      visible, nearby, chat: clone(this.chats), time: ready ? { day: Number(this.bot.time?.day || 0), time_of_day: Number(this.bot.time?.timeOfDay || 0), is_day: Boolean(this.bot.time?.isDay) } : null,
      actions: [...SKILLS], recipes: ready ? this.recipes(visible) : [],
      active_action: this.active ? this.publicAction(this.active) : null,
      reflexes: clone(this.reflexes.slice(-6)), death: this.death, error: this.lastError,
      viewer_url: 'http://127.0.0.1:' + this.config.viewer_port + '/',
      perception: 'Structured, range/FOV/occlusion-filtered observations; body navigation uses local collision geometry.'
    }
  }
  publicAction (record) { const { controller, fingerprint, promise, ...value } = record; return clone(value) }
  getAction (id) {
    const record = this.records.get(id)
    assert(record, 'Unknown action id', 404)
    return this.publicAction(record)
  }
  start () {
    assert(this.connected && this.alive, 'Agent must be connected and alive', 409)
    assert(this.bot.game?.gameMode === 'survival', 'This experiment requires Survival game mode', 409)
    this.enabled = true; this.leaseUntil = this.clock() + 15000
    return { enabled: true, alive: true, connected: true }
  }
  heartbeat () {
    if (this.enabled && this.clock() > this.leaseUntil) this.stop('supervisor_lease_expired')
    assert(this.enabled && this.connected && this.alive, 'Agent is not running', 409)
    this.leaseUntil = this.clock() + 15000
    return { enabled: true, lease_expires_at: this.leaseUntil }
  }
  stop (reason = 'operator_stop') {
    this.enabled = false; this.leaseUntil = 0; this.emergency = false
    this.interrupt(reason)
    this.release()
    return { enabled: false, active_action: this.active ? this.publicAction(this.active) : null }
  }
  release () {
    try { this.bot.pathfinder.setGoal(null) } catch (_) {}
    try { this.bot.stopDigging() } catch (_) {}
    try { this.bot.deactivateItem() } catch (_) {}
    try { this.bot.clearControlStates() } catch (_) {}
  }
  interrupt (reason, status = 'cancelled') {
    if (!this.active || !LIVE.has(this.active.status)) return
    const record = this.active
    record.status = status; record.success = false; record.reason = reason; record.finished_at = this.clock()
    record.may_have_effects = Boolean(record.effect_started)
    record.controller.abort()
    this.release()
    // Do not release the per-body lock until the old asynchronous skill settles.
  }
  ensure (record) {
    checkSignal(record.controller.signal)
    if (this.clock() > this.leaseUntil) this.stop('supervisor_lease_expired')
    assert(this.enabled && this.connected && this.alive, 'Agent is stopped, disconnected, or dead', 409)
    if (this.bot.game?.gameMode !== 'survival') { this.stop('survival_mode_required'); throw error('This experiment requires Survival game mode', 409) }
  }
  assertBlock (target, observation) {
    const known = observation.visible.find(b => b.id === target)
    assert(known, 'Target was not in the supplied observation')
    const block = this.bot.blockAt(this.vector(known.position))
    assert(block && block.name === known.name && this.visiblePoint({ x: known.position.x + 0.5, y: known.position.y + 0.5, z: known.position.z + 0.5 }, known.id) && (CROPS.has(block.name) || this.bot.canSeeBlock(block)), 'Target is no longer visible or has changed', 409)
    return block
  }
  assertEntity (target, observation) {
    const known = observation.nearby.find(e => e.id === target)
    assert(known, 'Entity was not in the supplied observation')
    const entity = this.bot.entities[Number(target.slice(7))]
    assert(entity && (!known.username || entity.username === known.username) && this.visiblePoint({ x: entity.position.x, y: entity.position.y + (entity.height || 1) / 2, z: entity.position.z }), 'Entity is no longer visible', 409)
    return entity
  }
  startAction (request, source = 'mind') {
    assert(request && typeof request === 'object', 'Action body required')
    assert(typeof request.action_id === 'string' && /^[A-Za-z0-9:_-]{1,128}$/.test(request.action_id), 'Invalid action id')
    assert(SKILLS.includes(request.skill), 'Unsupported skill')
    const target = request.target === undefined ? '' : request.target
    assert(typeof target === 'string' && target.length <= 128 && !/[\x00-\x1f\x7f]/.test(target), 'Target must be a short single-line string')
    const timeout = request.timeout_ms === undefined ? 30000 : request.timeout_ms
    assert(Number.isInteger(timeout) && timeout >= 1000 && timeout <= 60000, 'timeout_ms must be1000..60000')
    const fingerprint = JSON.stringify([request.skill, target, request.observation_id, timeout])
    const previous = this.records.get(request.action_id)
    if (previous) {
      assert(previous.fingerprint === fingerprint, 'Action id reused with different parameters', 409)
      return this.publicAction(previous)
    }
    if (this.enabled && this.clock() > this.leaseUntil) this.stop('supervisor_lease_expired')
    assert(this.enabled && this.connected && this.alive, 'Agent is not running', 409)
    assert(!this.active, 'Body is busy or cancelling its previous action', 409)
    assert(!this.emergency || source === 'reflex', 'Survival reflex is active', 409)
    assert(this.records.size < 4096, 'Action ledger capacity reached; restart the bridge between experiments', 409)
    const observation = this.observations.get(request.observation_id)
    assert(observation && this.clock() - observation.at <= 180000, 'Unknown or stale observation', 409)
    if (['look', 'wait'].includes(request.skill)) assert(!target, 'This skill has no target')
    if (request.skill === 'chat') assert(target.trim() && !target.trimStart().startsWith('/'), 'Chat cannot execute server commands')
    const record = {
      id: request.action_id, skill: request.skill, target, status: 'running', success: false,
      reason: '', source, observation_id: request.observation_id, started_at: this.clock(), finished_at: null,
      timeout_ms: timeout, effect_started: false, may_have_effects: false,
      inventory_before: inventory(this.bot), fingerprint, controller: new AbortController()
    }
    this.records.set(record.id, record); this.active = record
    const timer = setTimeout(() => this.interrupt('action_timeout', record.effect_started ? 'outcome_unknown' : 'failed'), timeout)
    record.promise = Promise.resolve().then(() => this.perform(record, observation)).then(result => {
      if (record.status === 'running') { record.status = 'succeeded'; record.success = true; record.result = result; record.reason = result.reason || 'verified' }
    }).catch(err => {
      if (record.status === 'running') {
        record.status = record.effect_started ? 'outcome_unknown' : 'failed'
        record.reason = String(err.message || err).slice(0, 240)
      }
    }).finally(() => {
      clearTimeout(timer)
      record.finished_at = record.finished_at || this.clock()
      record.final_position = this.bot.entity?.position ? pos(this.bot.entity.position) : null
      record.inventory_after = inventory(this.bot)
      record.inventory_delta = inventoryDelta(record.inventory_before, record.inventory_after)
      record.may_have_effects = record.effect_started && !record.success
      this.release(); this.lastProgress = null
      if (this.active === record) this.active = null
    })
    return this.publicAction(record)
  }

  async step (record, operation) {
    this.ensure(record)
    // An interrupted in-flight primitive is observed to settlement before another
    // skill may own the body. No subsequent primitive runs after cancellation.
    const result = await operation()
    this.ensure(record)
    return result
  }
  async go (record, point, radius = 1) {
    this.lastProgress = { position: pos(this.bot.entity.position), at: this.clock() }
    record.effect_started = true
    await this.step(record, () => this.bot.pathfinder.goto(new this.deps.goals.GoalNear(Math.floor(point.x), Math.floor(point.y), Math.floor(point.z), radius)))
    assert(distance(this.bot.entity.position, point) <= radius + 1.6, 'Navigation did not reach target', 409)
    this.lastProgress = null
  }
  async perform (record, observation) {
    const bot = this.bot; const target = record.target; const signal = record.controller.signal
    this.ensure(record)
    if (record.skill === 'wait') { await delay(2000, signal); this.ensure(record); return { waited_ms: 2000 } }
    if (record.skill === 'look') {
      const yaw = angle(bot.entity.yaw + Math.PI / 2)
      record.effect_started = true
      await this.step(record, () => bot.look(yaw, 0, true))
      assert(Math.abs(angle(bot.entity.yaw - yaw)) < 0.1, 'Look was not applied')
      return { yaw: bot.entity.yaw }
    }
    if (record.skill === 'navigate' || record.skill === 'collect') {
      const block = this.assertBlock(target, observation)
      await this.go(record, block.position, 2)
      if (record.skill === 'navigate') return { position: pos(bot.entity.position) }
      await this.step(record, () => bot.lookAt(block.position.offset(0.5, 0.5, 0.5)))
      this.assertBlock(target, observation)
      const tool = bot.pathfinder.bestHarvestTool(block)
      if (tool) await this.step(record, () => bot.equip(tool, 'hand'))
      assert(bot.canDigBlock(block), 'Block cannot be dug')
      const drops = (block.drops || []).map(id => bot.registry.items[id]?.name).filter(Boolean)
      if (!drops.length) drops.push(block.name)
      record.effect_started = true
      await this.step(record, () => bot.dig(block))
      const current = bot.blockAt(block.position)
      assert(current && current.type !== block.type, 'Block removal was not confirmed')
      await this.go(record, block.position, 1)
      await until(() => drops.some(name => (inventory(bot)[name] || 0) > (record.inventory_before[name] || 0)), 4000, signal)
      return { removed: target, collected: inventoryDelta(record.inventory_before, inventory(bot)) }
    }
    if (record.skill === 'craft') {
      assert(this.recipes(this.visibleBlocks()).includes(target), 'Recipe is not currently available')
      const item = bot.registry.itemsByName[target]
      const table = this.craftingTable(this.visibleBlocks())
      const recipe = bot.recipesFor(item.id, null, 1, table)[0]
      assert(recipe, 'Required materials or crafting table missing')
      record.effect_started = true
      await this.step(record, () => bot.craft(recipe, 1, table))
      await until(() => (inventory(bot)[target] || 0) > (record.inventory_before[target] || 0), 2500, signal)
      return { crafted: target, delta: inventoryDelta(record.inventory_before, inventory(bot)) }
    }
    if (record.skill === 'eat' || record.skill === 'equip') {
      const item = bot.inventory.items().find(i => target ? i.name === target : FOODS.has(i.name))
      assert(item && (record.skill !== 'eat' || FOODS.has(item.name)), 'Requested item is not available or edible')
      record.effect_started = true
      await this.step(record, () => bot.equip(item, 'hand'))
      if (record.skill === 'equip') { assert(bot.heldItem?.name === item.name, 'Held item not confirmed'); return { equipped: item.name } }
      assert(bot.food < 20, 'Hunger is already full')
      const beforeFood = bot.food; record.effect_started = true
      await this.step(record, () => bot.consume())
      await until(() => bot.food > beforeFood && (inventory(bot)[item.name] || 0) < (record.inventory_before[item.name] || 0), 2500, signal)
      return { ate: item.name, food: bot.food }
    }
    if (record.skill === 'place') {
      const match = /^([a-z0-9_]+)@(block:-?\d+,-?\d+,-?\d+)$/.exec(target)
      assert(match, 'Use item_name@block:x,y,z')
      const reference = this.assertBlock(match[2], observation)
      const item = bot.inventory.items().find(i => i.name === match[1])
      assert(item && bot.registry.blocksByName[item.name], 'Inventory item is not a placeable block')
      const above = reference.position.offset(0, 1, 0); const dest = bot.blockAt(above)
      assert(dest && dest.boundingBox === 'empty' && dest.name === 'air', 'Placement requires empty air above reference')
      assert(distance(bot.entity.position, above) > 0.9, 'Cannot place inside own body')
      await this.go(record, reference.position, 2)
      await this.step(record, () => bot.lookAt(reference.position.offset(0.5, 0.5, 0.5)))
      this.assertBlock(match[2], observation)
      await this.step(record, () => bot.equip(item, 'hand'))
      record.effect_started = true
      await this.step(record, () => bot.placeBlock(reference, this.vector({ x: 0, y: 1, z: 0 })))
      await until(() => bot.blockAt(above)?.name === item.name && (inventory(bot)[item.name] || 0) < (record.inventory_before[item.name] || 0), 2500, signal)
      return { placed: item.name, position: pos(above) }
    }
    if (record.skill === 'chat') {
      const sequence = this.sequence
      this.ensure(record); record.effect_started = true
      bot.chat(target)
      await until(() => this.chats.some(m => m.username === bot.username && m.text === target && Number(m.id.split(':').at(-1)) > sequence), 3000, signal)
      return { said: target, evidence: 'server_chat_echo' }
    }
    if (record.skill === 'give') {
      const match = /^([a-z0-9_]+)@(entity:\d+)$/.exec(target)
      assert(match, 'Use item_name@entity:id')
      const entity = this.assertEntity(match[2], observation)
      assert(entity.username && distance(bot.entity.position, entity.position) <= 3, 'Recipient must be a visible player within3blocks')
      const item = bot.inventory.items().find(i => i.name === match[1])
      assert(item, 'Item not in inventory')
      record.effect_started = true
      await this.step(record, () => bot.lookAt(entity.position.offset(0, 1, 0)))
      // Record actual collector; dropping an item is not a verified transfer.
      let collected = false
      const existingEntities = new Set(Object.keys(bot.entities || {}).map(Number))
      const candidateDrops = new Set()
      const onDrop = entity => {
        if (!existingEntities.has(entity.id) && entity.position && distance(entity.position, bot.entity.position) <= 3 && entity.getDroppedItem?.()?.name === item.name) candidateDrops.add(entity.id)
      }
      const onCollect = (collector, dropped) => {
        const stack = dropped?.getDroppedItem?.()
        if (collector?.id === entity.id && candidateDrops.has(dropped?.id) && stack?.name === item.name) collected = true
      }
      bot.on('playerCollect', onCollect); bot.on('entitySpawn', onDrop); bot.on('entityUpdate', onDrop)
      try {
        this.ensure(record); record.effect_started = true
        await this.step(record, () => bot.toss(item.type, item.metadata, 1))
        await until(() => collected && (inventory(bot)[item.name] || 0) === (record.inventory_before[item.name] || 0) - 1, 4000, signal)
      } finally { bot.removeListener('playerCollect', onCollect); bot.removeListener('entitySpawn', onDrop); bot.removeListener('entityUpdate', onDrop) }
      return { item: item.name, recipient: entity.username, count: 1,
        evidence: 'matching_pickup_and_inventory_decrease', exact_drop_owner_confirmed: false,
        reason: 'Inventory decreased and the recipient picked up a matching item.' }
    }
    if (record.skill === 'flee') {
      const selected = target || observation.nearby.find(e => HOSTILES.has(e.name))?.id
      assert(selected, 'No observed threat to flee')
      const entity = this.assertEntity(selected, observation)
      const p = pos(bot.entity.position); const dx = p.x - entity.position.x; const dz = p.z - entity.position.z
      const length = Math.hypot(dx, dz) || 1
      const goal = { x: p.x + dx / length * 4, y: p.y, z: p.z + dz / length * 4 }
      await this.go(record, goal, 1)
      assert(distance(bot.entity.position, entity.position) > distance(p, entity.position), 'Distance from threat did not increase')
      return { fled_from: selected, position: pos(bot.entity.position) }
    }
    throw error('Unsupported skill')
  }

  reflexTick () {
    if (!this.enabled || !this.connected || !this.alive) return
    if (this.clock() > this.leaseUntil) { this.stop('supervisor_lease_expired'); return }
    if (this.bot.game?.gameMode !== 'survival') { this.stop('survival_mode_required'); return }
    const bot = this.bot
    if (finite(bot.oxygenLevel) && bot.oxygenLevel < 8 && bot.entity?.isInWater) {
      if (!this.emergency) {
        this.interrupt('reflex_low_oxygen')
        this.reflexes.push({ id: 'reflex:' + ++this.sequence, kind: 'surface', at: this.clock() })
      }
      this.emergency = true
      bot.setControlState('jump', true)
      return
    }
    if (this.emergency) { this.emergency = false; bot.clearControlStates() }
    if (this.active && this.lastProgress && this.active.skill !== 'look') {
      if (distance(bot.entity.position, this.lastProgress.position) > 0.5) this.lastProgress = { position: pos(bot.entity.position), at: this.clock() }
      else if (this.clock() - this.lastProgress.at > 8000) this.interrupt('reflex_stuck', 'failed')
    }
    if (this.clock() - this.lastReflex < 5000) return
    const nearby = this.nearbyEntities()
    const threat = nearby.find(e => HOSTILES.has(e.name) && e.distance < 4)
    if (threat && this.active?.skill !== 'flee') {
      if (this.active) { this.interrupt('reflex_nearby_threat'); return }
      const observation = this.observe()
      this.lastReflex = this.clock()
      this.reflexes.push({ id: 'reflex:' + ++this.sequence, kind: 'flee', at: this.clock(), target: threat.id })
      this.startAction({ action_id: 'reflex:' + this.session + ':' + this.sequence, skill: 'flee', target: threat.id, observation_id: observation.observation_id, timeout_ms: 10000 }, 'reflex')
    } else if (!this.active && bot.food <= 14 && bot.inventory.items().some(i => FOODS.has(i.name))) {
      const observation = this.observe()
      this.lastReflex = this.clock()
      this.reflexes.push({ id: 'reflex:' + ++this.sequence, kind: 'eat', at: this.clock() })
      this.startAction({ action_id: 'reflex:' + this.session + ':' + this.sequence, skill: 'eat', target: '', observation_id: observation.observation_id, timeout_ms: 10000 }, 'reflex')
    }
    this.reflexes = this.reflexes.slice(-6)
  }
  close () {
    this.closed = true; this.stop('bridge_shutdown'); clearInterval(this.timer)
    try { this.bot.quit('AI-AM body closed') } catch (_) {}
  }
}


function loadDependencies () {
  const mineflayer = require('mineflayer')
  const pathfinder = require('mineflayer-pathfinder')
  return { createBot: mineflayer.createBot, ...pathfinder, Vec3: require('vec3').Vec3 }
}
function configureMovement (bot, deps) {
  bot.loadPlugin(deps.pathfinder)
  bot.once('spawn', () => {
    const movement = new deps.Movements(bot)
    movement.canDig = false; movement.allow1by1towers = false; movement.allowParkour = false
    movement.allowSprinting = false; movement.maxDropDown = 1; movement.infiniteLiquidDropdownDistance = false
    movement.scafoldingBlocks = []
    movement.entitiesToAvoid = new Set(HOSTILES)
    bot.pathfinder.setMovements(movement)
    bot.pathfinder.searchRadius = 24; bot.pathfinder.thinkTimeout = 1500; bot.pathfinder.tickTimeout = 10
  })
}
class BodyManager {
  constructor (config, dependencies) {
    this.config = validateConfig(config); this.deps = dependencies || loadDependencies()
    this.agents = new Map(); this.pendingStarts = new Map()
    for (const agent of this.config.agents) this.connect(agent)
  }
  connect (config) {
    const previous = this.agents.get(config.id)
    previous?.close()
    const bot = this.deps.createBot({
      host: this.config.server_host, port: this.config.server_port, version: this.config.version,
      username: config.username, auth: 'offline', respawn: false, checkTimeoutInterval: 30000,
      hideErrors: true, logErrors: false
    })
    const body = new AgentBody(bot, this.deps, config, { records: previous?.records })
    configureMovement(bot, this.deps)
    this.agents.set(config.id, body)
    return body
  }
  get (id) { const body = this.agents.get(id); assert(body, 'Unknown agent', 404); return body }
  start (id) {
    if (this.pendingStarts.has(id)) return this.pendingStarts.get(id).promise
    const pending = { cancelled: false }
    pending.promise = this.startBody(id, pending).finally(() => { if (this.pendingStarts.get(id) === pending) this.pendingStarts.delete(id) })
    this.pendingStarts.set(id, pending)
    return pending.promise
  }
  stop (id) {
    const pending = this.pendingStarts.get(id)
    if (pending) pending.cancelled = true
    return this.get(id).stop()
  }
  async startBody (id, pending) {
    let body = this.get(id)
    if (!body.connected || body.active?.controller.signal.aborted) body = this.connect(body.config)
    const deadline = Date.now() + 15000
    let respawnRequested = false
    while ((!body.connected || !body.alive) && Date.now() < deadline && !pending.cancelled) {
      // The death signal can arrive after connect() returns. Request once, only
      // after Mineflayer has processed the death and before enabling controls.
      if (body.connected && body.death && !respawnRequested) {
        respawnRequested = true
        body.bot.respawn()
      }
      if (body.connected && body.alive) break
      await delay(100)
    }
    assert(!pending.cancelled, 'Start cancelled by operator', 409)
    assert(body.connected && body.alive, 'Minecraft connection or respawn did not complete', 503)
    return body.start()
  }
  health () {
    return { ok: true, service: 'ai-am-minecraft-body', protocol: 1, version: this.config.version,
      agents: [...this.agents.values()].map(b => ({ id: b.config.id, connected: b.connected, alive: b.alive, enabled: b.enabled, error: b.lastError })) }
  }
  close () { for (const pending of this.pendingStarts.values()) pending.cancelled = true; for (const body of this.agents.values()) body.close() }
}
function loopbackRequest (req, port, checkOrigin = true) {
  if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(req.socket.remoteAddress)) return false
  if (req.headers.host !== '127.0.0.1:' + port && req.headers.host !== 'localhost:' + port) return false
  if (checkOrigin && req.headers.origin && !['http://127.0.0.1:' + port, 'http://localhost:' + port].includes(req.headers.origin)) return false
  return true
}
function authorized (req, token) {
  const actual = Buffer.from(req.headers.authorization || '')
  const expected = Buffer.from('Bearer ' + token)
  return actual.length === expected.length && crypto.timingSafeEqual(actual, expected)
}
async function readJson (req) {
  const chunks = []; let size = 0
  for await (const chunk of req) {
    size += chunk.length
    assert(size <= 8192, 'Request exceeds8KB', 413)
    chunks.push(chunk)
  }
  try {
    const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')
    assert(parsed && typeof parsed === 'object' && !Array.isArray(parsed), 'JSON object required')
    return parsed
  } catch (err) { throw error(err.status ? err.message : 'Invalid JSON') }
}
function json (res, status, payload) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' })
  res.end(JSON.stringify(payload))
}
function createApi (manager, token) {
  assert(typeof token === 'string' && token.length >= 24 && token.length <= 256 && !/[\r\n]/.test(token), 'AIAM_MINECRAFT_TOKEN must contain24..256characters')
  let port = manager.config.bridge_port
  const server = http.createServer(async (req, res) => {
    try {
      assert(loopbackRequest(req, port), 'Loopback origin and Host required', 403)
      assert(authorized(req, token), 'Authentication required', 401)
      const url = new URL(req.url, 'http://127.0.0.1:' + port)
      assert(!url.search, 'Query parameters are not accepted')
      if (req.method === 'GET' && url.pathname === '/health') return json(res, 200, manager.health())
      const match = /^\/agents\/([a-z][a-z0-9_-]{0,31})\/(observation|actions|stop|start|heartbeat)(?:\/([A-Za-z0-9:_-]{1,128}))?$/.exec(url.pathname)
      assert(match, 'Unknown route', 404)
      const body = manager.get(match[1])
      if (req.method === 'GET' && match[2] === 'observation' && !match[3]) return json(res, 200, body.observe())
      if (req.method === 'GET' && match[2] === 'actions' && match[3]) return json(res, 200, body.getAction(match[3]))
      if (req.method === 'POST' && !match[3]) {
        assert((req.headers['content-type'] || '').split(';')[0] === 'application/json', 'Content-Type must be application/json', 415)
        const input = await readJson(req)
        if (match[2] === 'actions') return json(res, 200, body.startAction(input))
        if (match[2] === 'heartbeat') return json(res, 200, body.heartbeat())
        if (match[2] === 'stop') return json(res, 200, manager.stop(match[1]))
        if (match[2] === 'start') return json(res, 200, await manager.start(match[1]))
      }
      throw error('Method not allowed', 405)
    } catch (err) { if (!res.headersSent) json(res, err.status || 500, { error: err.status ? err.message : 'Bridge operation failed' }) }
  })
  server.on('listening', () => { port = server.address().port })
  server.requestTimeout = 20000; server.headersTimeout = 10000; server.keepAliveTimeout = 5000
  return server
}
function createViewer (manager, agentId, port) {
  // The upstream mineflayer viewer binds on all interfaces. Build its read-only
  // WorldView transport on our own loopback server instead; no blockClicked handler.
  const express = require('express')
  const { Server } = require('socket.io')
  const { WorldView } = require('prismarine-viewer/viewer/lib/worldView')
  const { setupRoutes } = require('prismarine-viewer/lib/common')
  const app = express()
  app.use((req, res, next) => {
    if (!loopbackRequest(req, port)) return res.status(403).end('Loopback observer only')
    res.set('Cache-Control', 'no-store')
    next()
  })
  setupRoutes(app, '')
  const server = http.createServer(app)
  const io = new Server(server, { allowRequest: (req, callback) => callback(null, loopbackRequest(req, port)), maxHttpBufferSize: 8192 })
  io.on('connection', socket => {
    const body = manager.get(agentId); const bot = body.bot
    if (!body.connected || !body.alive || !bot.entity?.position) { socket.emit('observer_status', { ready: false, reason: 'Agent is not spawned; reconnect viewer after starting' }); socket.disconnect(true); return }
    socket.emit('version', bot.version)
    const world = new WorldView(bot.world, 4, bot.entity.position, socket)
    socket.removeAllListeners('mouseClick')
    const closeViewer = () => socket.disconnect(true)
    world.init(bot.entity.position).catch(closeViewer)
    const update = () => {
      socket.emit('position', { pos: bot.entity.position, yaw: bot.entity.yaw, addMesh: true })
      world.updatePosition(bot.entity.position).catch(closeViewer)
    }
    bot.on('move', update); world.listenToBot(bot); update()
    const end = () => socket.disconnect(true)
    bot.once('end', end)
    socket.on('disconnect', () => {
      bot.removeListener('move', update); bot.removeListener('end', end); world.removeListenersFromBot(bot)
    })
  })
  const closeHttp = server.close.bind(server)
  server.close = callback => { io.disconnectSockets(true); io.engine.close(); return closeHttp(callback) }
  return server
}
async function main () {
  const index = process.argv.indexOf('--config')
  assert(index >= 0 && process.argv[index + 1], 'Usage: node minecraft/bridge.cjs --config PATH')
  const configPath = path.resolve(process.argv[index + 1])
  assert(process.platform !== 'win32' || !/^c:/i.test(configPath), 'Store the Minecraft configuration on a non-C drive')
  const config = validateConfig(JSON.parse(fs.readFileSync(configPath, 'utf8').replace(/^\uFEFF/, '')))
  const token = process.env.AIAM_MINECRAFT_TOKEN
  assert(token && token.length >= 24, 'Set AIAM_MINECRAFT_TOKEN to a random secret of at least24characters')
  const manager = new BodyManager(config)
  const servers = [createApi(manager, token), ...config.agents.map(a => createViewer(manager, a.id, a.viewer_port))]
  const ports = [config.bridge_port, ...config.agents.map(a => a.viewer_port)]
  try {
    await Promise.all(servers.map((server, i) => new Promise((resolve, reject) => {
      server.once('error', reject)
      server.listen(ports[i], '127.0.0.1', resolve)
    })))
  } catch (err) {
    manager.close(); for (const server of servers) server.close(); throw err
  }
  console.log('AI-AM Minecraft bridge ready on127.0.0.1:' + config.bridge_port + '; agents initially paused')
  const close = () => {
    manager.close()
    for (const server of servers) server.close()
    setTimeout(() => process.exit(0), 1000).unref()
  }
  process.once('SIGINT', close); process.once('SIGTERM', close)
}
module.exports = { SKILLS, AgentBody, BodyManager, validateConfig, createApi, createViewer, loopbackRequest, authorized, inventory, inventoryDelta, configureMovement, main }
if (require.main === module) main().catch(err => { console.error(err.message); process.exit(1) })
