'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { AgentBody, BodyManager, validateConfig, authorized, loopbackRequest, configureMovement, createApi } = require('../bridge.cjs')
class Vec3 {
  constructor (x, y, z) { Object.assign(this, { x, y, z }) }
  offset (x, y, z) { return new Vec3(this.x + x, this.y + y, this.z + z) }
}
function fixture (options = {}) {
  const bot = new EventEmitter()
  const blocks = new Map(); const items = []
  const key = p => [Math.floor(p.x), Math.floor(p.y), Math.floor(p.z)].join(',')
  const put = (x, y, z, name = 'oak_log') => {
    const b = { position: new Vec3(x, y, z), name, type: name === 'oak_log' ? 1 : 2, boundingBox: 'block', drops: [1] }
    blocks.set(key(b.position), b); return b
  }
  bot.game = { gameMode: 'survival', dimension: 'overworld' }
  bot.username = 'AI_AM'; bot.health = options.healthPending ? undefined : 20; bot.food = 20; bot.oxygenLevel = 20
  bot.entity = { id: 1, position: new Vec3(0, 64, 0), yaw: 0, pitch: 0, height: 1.8 }
  bot.entities = {}; bot.players = {}; bot.time = { day: 0, timeOfDay: 1000, isDay: true }
  bot.inventory = { items: () => items.filter(i => i.count > 0) }
  bot.registry = { items: { 1: { id: 1, name: 'oak_log' } }, itemsByName: { oak_planks: { id: 2 }, stick: { id: 3 } }, blocksByName: { oak_planks: { id: 2 } } }
  bot.blockAt = p => blocks.get(key(p)) || { name: 'air', type: 0, boundingBox: 'empty', position: new Vec3(Math.floor(p.x), Math.floor(p.y), Math.floor(p.z)) }
  bot.findBlocks = ({ matching, count }) => {
    const distance = p => Math.hypot(p.x - bot.entity.position.x, p.y - bot.entity.position.y, p.z - bot.entity.position.z)
    return [...blocks.values()].filter(matching).map(block => block.position)
      .sort((left, right) => distance(left) - distance(right)).slice(0, count)
  }
  bot.canSeeBlock = () => true; bot.canDigBlock = () => true
  bot.recipesFor = id => id === 2 && items.some(i => i.name === 'oak_log' && i.count > 0) ? [{ id }] : []
  bot.look = async (yaw, pitch) => { bot.entity.yaw = yaw; bot.entity.pitch = pitch }
  bot.lookAt = async p => { const e = bot.entity.position.offset(0, 1.62, 0); bot.entity.yaw = Math.atan2(-(p.x - e.x), -(p.z - e.z)); bot.entity.pitch = Math.atan2(p.y - e.y, Math.hypot(p.x - e.x, p.z - e.z)) }
  bot.equip = async item => { bot.heldItem = item }
  bot.consume = async () => { bot.heldItem.count--; bot.food = Math.min(20, bot.food + 5) }
  bot.dig = async b => { blocks.delete(key(b.position)); items.push({ name: 'oak_log', type: 1, count: 1 }) }
  bot.craft = async () => { items.find(i => i.name === 'oak_log').count--; items.push({ name: 'oak_planks', count: 4, type: 2 }) }
  bot.chat = message => { bot.sent = (bot.sent || []).concat(message); bot.emit('chat', bot.username, message) }
  bot.stopDigging = () => {}; bot.deactivateItem = () => {}; bot.clearControlStates = () => { bot.cleared = (bot.cleared || 0) + 1; bot.controls = {} }
  bot.setControlState = (name, value) => { bot.controls ||= {}; bot.controls[name] = value }
  bot.quit = () => {}; bot.loadPlugin = () => {}
  bot.pathfinder = { setGoal: () => {}, bestHarvestTool: () => null, setMovements: value => { bot.movements = value }, goto: async goal => { bot.entity.position = new Vec3(goal.x, goal.y, goal.z) } }
  const deps = { Vec3, goals: { GoalNear: class { constructor (x, y, z, radius) { Object.assign(this, { x, y, z, radius }) } } }, Movements: class {}, pathfinder: () => {} }
  let clock = 100000
  const body = new AgentBody(bot, deps, { id: 'ai-am', username: 'AI_AM', viewer_port: 8767 }, { reflexTimer: false, clock: () => clock, session: 'test', ...options })
  bot.emit('spawn')
  const request = (skill, target = '', id = 'a1') => ({ action_id: id, skill, target, observation_id: body.observe().observation_id, timeout_ms: 1000 })
  const settle = async () => { const active = body.active; if (active) await active.promise }
  return { bot, body, items, put, request, settle, deps, advance: n => { clock += n } }
}
const config = () => ({ server_host: '127.0.0.1', server_port: 25565, bridge_port: 8766, version: '1.21.4', agents: [{ id: 'ai-am', username: 'AI_AM', viewer_port: 8767 }] })

test('configuration permits only private loopback version-pinned distinct agents', () => {
  assert.equal(validateConfig(config()).version, '1.21.4')
  for (const host of ['localhost', '0.0.0.0', '192.168.1.2', '127.0.0.1.attacker.test']) assert.throws(() => validateConfig({ ...config(), server_host: host }))
  assert.throws(() => validateConfig({ ...config(), version: '26.1' }))
  assert.throws(() => validateConfig({ ...config(), agents: [{ id: 'a', username: 'A', viewer_port: 8766 }] }))
  assert.throws(() => validateConfig({ ...config(), agents: [config().agents[0], config().agents[0]] }))
})
test('initial health arriving after spawn makes the connected body ready', () => {
  const { bot, body } = fixture({ healthPending: true })
  assert.equal(body.connected, true)
  assert.equal(body.alive, false)
  bot.health = 20
  bot.emit('health')
  assert.equal(body.observe().alive, true)
  assert.equal(body.start().enabled, true)
  bot.emit('end', 'test disconnected')
  assert.equal(body.observe().alive, false)
})

test('API requires exact token, loopback peer, legitimate Host and same origin', () => {
  const req = { socket: { remoteAddress: '127.0.0.1' }, headers: { host: '127.0.0.1:8766', authorization: 'Bearer secret' } }
  assert(authorized(req, 'secret')); assert(!authorized(req, 'secre'))
  assert(loopbackRequest(req, 8766))
  req.headers.origin = 'https://evil.example'; assert(!loopbackRequest(req, 8766))
  delete req.headers.origin; req.headers.host = 'evil.example:8766'; assert(!loopbackRequest(req, 8766))
  req.headers.host = '127.0.0.1:8766'; req.socket.remoteAddress = '192.168.1.5'; assert(!loopbackRequest(req, 8766))
})
test('observation hides behind-camera and occluded blocks and entities', () => {
  const { body, bot, put } = fixture()
  put(0, 65, -3, 'oak_log'); put(0, 65, 3, 'oak_log'); put(0, 65, -5, 'iron_ore')
  bot.entities[2] = { id: 2, position: new Vec3(0, 64, -6), name: 'zombie', height: 1.8 }
  const observation = body.observe()
  assert(observation.visible.some(b => b.id === 'block:0,65,-3'))
  assert(!observation.visible.some(b => b.id === 'block:0,65,3' || b.name === 'iron_ore'))
  assert.equal(observation.nearby.length, 0)
  assert.equal(observation.connected, true); assert.equal(observation.enabled, false)
})
test('nearby terrain cannot crowd a visible log out of the resource search', () => {
  const { body, put } = fixture()
  for (let x = -6; x <= 6; x++) {
    for (let z = -6; z <= 6; z++) {
      put(x, 62, z, 'dirt')
      put(x, 63, z, 'dirt')
    }
  }
  put(0, 65, -10, 'oak_log')
  assert(body.observe().visible.some(block => block.id === 'block:0,65,-10'))
})
test('chat is bounded and proximity-scoped including stable source ids', () => {
  const { body, bot } = fixture()
  bot.players.Mira = { entity: { position: new Vec3(2, 64, 0) } }
  bot.players.Far = { entity: { position: new Vec3(200, 64, 0) } }
  bot.emit('chat', 'Far', 'hidden distant speech')
  for (let i = 0; i < 12; i++) bot.emit('chat', 'Mira', 'nearby' + i)
  const chats = body.observe().chat
  assert.equal(chats.length, 8); assert.equal(chats[0].text, 'nearby4')
  assert.equal(new Set(chats.map(c => c.id)).size, 8)
})
test('paused body rejects action and slash chat cannot become server command', () => {
  const { body, request, bot } = fixture()
  assert.throws(() => body.startAction(request('chat', 'hello')), /not running/)
  body.start()
  assert.throws(() => body.startAction(request('chat', ' /op AI_AM')), /server commands/)
  assert.equal(bot.sent, undefined)
})
test('server echoed chat is recorded exactly once under action retry', async () => {
  const { body, request, bot, settle } = fixture(); body.start()
  const req = request('chat', 'Hello Mira')
  body.startAction(req); await settle()
  assert.equal(body.getAction(req.action_id).status, 'succeeded')
  body.stop()
  assert.equal(body.startAction(req).status, 'succeeded')
  assert.deepEqual(bot.sent, ['Hello Mira'])
  assert.throws(() => body.startAction({ ...req, target: 'different' }), /reused/)
})
test('stale observation cannot authorize a new action', () => {
  const { body, request, advance } = fixture(); body.start()
  const req = request('look'); advance(180001); body.start()
  assert.throws(() => body.startAction(req), /stale/)
})
test('old observation reference is revalidated against changed world', async () => {
  const { body, request, put, settle } = fixture(); body.start()
  put(0, 65, -3)
  const req = request('collect', 'block:0,65,-3')
  put(0, 65, -3, 'stone')
  body.startAction(req); await settle()
  assert.equal(body.getAction(req.action_id).status, 'failed')
  assert.match(body.getAction(req.action_id).reason, /changed/)
})
test('look rotates body and confirms heading', async () => {
  const { body, request, settle, bot } = fixture(); body.start()
  body.startAction(request('look')); await settle()
  assert.equal(body.getAction('a1').status, 'succeeded', body.getAction('a1').reason)
  assert(Math.abs(bot.entity.yaw - Math.PI / 2) < 0.01)
})
test('craft verifies actual inventory delta and exposes available recipe only', async () => {
  const { body, items, request, settle } = fixture(); body.start()
  items.push({ name: 'oak_log', type: 1, count: 1 })
  assert.deepEqual(body.observe().recipes, ['oak_planks'])
  body.startAction(request('craft', 'oak_planks')); await settle()
  const result = body.getAction('a1')
  assert.equal(result.status, 'succeeded')
  assert.deepEqual(result.inventory_delta, { oak_log: -1, oak_planks: 4 })
})
test('eat requires food increase and consumed inventory', async () => {
  const { body, bot, items, request, settle } = fixture(); body.start()
  bot.food = 10; items.push({ name: 'bread', type: 4, count: 2 })
  body.startAction(request('eat', 'bread')); await settle()
  assert.equal(body.getAction('a1').status, 'succeeded', body.getAction('a1').reason)
  assert.equal(bot.food, 15); assert.equal(items[0].count, 1)
})
test('cancellation prevents any subsequent primitive after pending equip settles', async () => {
  const { body, bot, items, request, settle } = fixture(); body.start()
  bot.food = 10; items.push({ name: 'bread', type: 4, count: 2 })
  let resolveEquip; let consumed = false
  bot.equip = () => new Promise(resolve => { resolveEquip = resolve })
  bot.consume = async () => { consumed = true }
  body.startAction(request('eat', 'bread'))
  await Promise.resolve(); await Promise.resolve()
  body.stop()
  assert.equal(body.getAction('a1').status, 'cancelled')
  resolveEquip(); await settle()
  assert.equal(consumed, false); assert.equal(body.active, null)
})
test('death stops actions and reflexes without automatically respawning', async () => {
  const { body, bot, request, settle } = fixture(); body.start()
  let respawns = 0; bot.respawn = () => { respawns++ }
  body.startAction(request('wait')); bot.health = 0; bot.emit('death'); await settle()
  assert.equal(body.enabled, false); assert.equal(body.alive, false); assert(body.observe().death)
  assert.equal(respawns, 0); assert.equal(body.getAction('a1').status, 'cancelled')
})
test('lost supervisor lease stops body and observation reads never renew it', () => {
  const { body, advance, bot } = fixture(); body.start()
  const lease = body.leaseUntil
  advance(14000); body.observe(); assert.equal(body.leaseUntil, lease)
  advance(1001); body.reflexTick()
  assert.equal(body.enabled, false); assert(bot.cleared > 0)
  assert.throws(() => body.heartbeat(), /not running/)
})
test('heartbeat renews only an already enabled body', () => {
  const { body, advance } = fixture()
  assert.throws(() => body.heartbeat())
  body.start(); advance(10000); body.heartbeat(); advance(10000); body.reflexTick()
  assert.equal(body.enabled, true)
})
test('oxygen reflex is explicitly labeled and stop releases its control state', () => {
  const { body, bot } = fixture(); body.start()
  bot.oxygenLevel = 3; bot.entity.isInWater = true
  body.reflexTick()
  assert.equal(bot.controls.jump, true); assert.equal(body.reflexes[0].kind, 'surface')
  body.stop(); body.reflexTick()
  assert.equal(bot.controls.jump, undefined); assert.equal(body.emergency, false)
})
test('disconnect marks interrupted mutation outcome unknown', async () => {
  const { body, bot, request, settle } = fixture(); body.start()
  bot.chat = () => { bot.emit('end', 'socket_lost') }
  body.startAction(request('chat', 'hello')); await settle()
  assert.equal(body.getAction('a1').status, 'outcome_unknown')
  assert.equal(body.enabled, false)
})
test('movement configuration prohibits hidden mining or building and risky jumps', () => {
  const { bot, deps } = fixture()
  configureMovement(bot, deps); bot.emit('spawn')
  assert.equal(bot.movements.canDig, false); assert.equal(bot.movements.allowParkour, false)
  assert.equal(bot.movements.allow1by1towers, false); assert.equal(bot.movements.maxDropDown, 1)
})
test('manager creates separate offline bodies and disables automatic respawn', () => {
  const made = []
  const { deps } = fixture()
  deps.createBot = options => { const f = fixture(); made.push(options); return f.bot }
  const manager = new BodyManager({ ...config(), agents: [...config().agents, { id: 'mira', username: 'Mira', viewer_port: 8768 }] }, deps)
  assert.equal(manager.agents.size, 2)
  assert.equal(made[0].auth, 'offline'); assert.equal(made[0].respawn, false)
  assert.notEqual(manager.get('ai-am').records, manager.get('mira').records)
  manager.close()
})

test('expired lease cannot be renewed after watchdog delay', () => {
  const { body, advance } = fixture(); body.start(); advance(15001)
  assert.throws(() => body.heartbeat(), /not running/)
  assert.equal(body.enabled, false)
})
test('operator stop cancels pending reconnect before it can enable body', async () => {
  const { deps } = fixture()
  const bots = []
  deps.createBot = () => { const { bot } = fixture(); bots.push(bot); return bot }
  const manager = new BodyManager(config(), deps)
  const pending = manager.start('ai-am')
  manager.stop('ai-am')
  bots.at(-1).emit('spawn')
  await assert.rejects(pending, /cancelled/)
  assert.equal(manager.get('ai-am').enabled, false)
  manager.close()
})
test('HTTP start, observation, idempotent action polling, stop and auth are wired', async () => {
  const f = fixture()
  const manager = { config: { bridge_port: 0 }, health: () => ({ ok: true }), get: id => { assert.equal(id, 'ai-am'); return f.body }, start: async () => f.body.start(), stop: () => f.body.stop() }
  const token = 'test_token_with_twenty_four_characters'
  const server = createApi(manager, token)
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = 'http://127.0.0.1:' + server.address().port
  const request = (route, method = 'GET', data) => fetch(base + route, { method, headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, ...(data ? { body: JSON.stringify(data) } : {}) })
  try {
    assert.equal((await fetch(base + '/health')).status, 401)
    assert.equal((await request('/health')).status, 200)
    assert.equal((await request('/agents/ai-am/start', 'POST', {})).status, 200)
    const observation = await (await request('/agents/ai-am/observation')).json()
    const action = { action_id: 'http1', skill: 'look', target: '', observation_id: observation.observation_id }
    assert.equal((await request('/agents/ai-am/actions', 'POST', action)).status, 200)
    await f.settle()
    assert.equal((await (await request('/agents/ai-am/actions/http1')).json()).status, 'succeeded')
    assert.equal((await request('/agents/ai-am/actions', 'POST', action)).status, 200)
    assert.equal((await request('/agents/ai-am/stop', 'POST', {})).status, 200)
    assert.equal(f.body.enabled, false)
    assert.equal((await request('/agents/ai-am/heartbeat', 'POST', {})).status, 409)
    assert.equal((await fetch(base + '/health', { headers: { Authorization: 'Bearer ' + token, Origin: 'https://evil.example' } })).status, 403)
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
})

test('creative mode cannot be enabled and mode changes stop a running action', async () => {
  const { body, bot, request, settle } = fixture()
  bot.game.gameMode = 'creative'; assert.throws(() => body.start(), /Survival/)
  bot.game.gameMode = 'survival'; body.start()
  const req = request('look'); bot.game.gameMode = 'spectator'
  body.startAction(req); await settle()
  assert.equal(body.enabled, false); assert.equal(body.getAction('a1').success, false)
})

test('collect confirms both removed block and acquired server inventory item', async () => {
  const { body, bot, put, request, settle } = fixture(); body.start()
  put(0, 65, -3)
  bot.pathfinder.goto = async () => { bot.entity.position = new Vec3(0, 64, -2) }
  body.startAction(request('collect', 'block:0,65,-3')); await settle()
  assert.equal(body.getAction('a1').status, 'succeeded', body.getAction('a1').reason)
  assert.equal(body.getAction('a1').inventory_delta.oak_log, 1)
  assert.deepEqual(body.getAction('a1').final_position, { x: 0, y: 64, z: -2 })
})
test('place verifies target block and inventory depletion', async () => {
  const { body, bot, put, items, request, settle } = fixture(); body.start()
  put(0, 64, -3, 'stone'); items.push({ name: 'oak_planks', count: 2 })
  bot.pathfinder.goto = async () => { bot.entity.position = new Vec3(0, 64, -2) }
  bot.placeBlock = async reference => { put(reference.position.x, reference.position.y + 1, reference.position.z, bot.heldItem.name); bot.heldItem.count-- }
  body.startAction(request('place', 'oak_planks@block:0,64,-3')); await settle()
  assert.equal(body.getAction('a1').status, 'succeeded', body.getAction('a1').reason)
  assert.equal(body.getAction('a1').inventory_delta.oak_planks, -1)
})
test('give verifies a newly spawned matching item picked up by intended player', async () => {
  const { body, bot, items, request, settle } = fixture(); body.start()
  const recipient = { id: 2, username: 'Mira', name: 'player', height: 1.8, position: new Vec3(0, 64, -2) }
  bot.entities[2] = recipient
  items.push({ name: 'bread', count: 2, type: 4, metadata: null })
  bot.toss = async () => {
    items[0].count--
    const drop = { id: 3, position: new Vec3(0, 64, -1), getDroppedItem: () => ({ name: 'bread' }) }
    bot.emit('entitySpawn', drop); bot.emit('playerCollect', recipient, drop)
  }
  body.startAction(request('give', 'bread@entity:2')); await settle()
  assert.equal(body.getAction('a1').status, 'succeeded', body.getAction('a1').reason)
  assert.equal(body.getAction('a1').result.recipient, 'Mira')
})
test('flee uses observed entity and confirms increasing distance', async () => {
  const { body, bot, request, settle } = fixture(); body.start()
  bot.entities[2] = { id: 2, name: 'zombie', height: 1.8, position: new Vec3(0, 64, -3) }
  body.startAction(request('flee', 'entity:2')); await settle()
  assert.equal(body.getAction('a1').status, 'succeeded', body.getAction('a1').reason)
  assert(bot.entity.position.z > 0)
})

test('visible crops expose growth stage without revealing crops behind walls', () => {
  const { body, bot, put } = fixture()
  const crop = put(1, 64, -3, 'wheat'); crop.boundingBox = 'empty'; crop.getProperties = () => ({ age: 7 })
  put(-1, 65, -2, 'stone'); const hidden = put(-1, 64, -4, 'carrots'); hidden.boundingBox = 'empty'
  bot.canSeeBlock = b => b.boundingBox === 'block'
  const seen = body.observe().visible
  assert(seen.some(b => b.name === 'wheat' && b.age === 7))
  assert(!seen.some(b => b.name === 'carrots'))
})
test('hay blocks and actually available wheat recipes support food crafting', () => {
  const { body, bot, put } = fixture()
  put(0, 65, -3, 'hay_block')
  bot.registry.itemsByName.wheat = { id: 30 }
  bot.recipesFor = id => id === 30 ? [{}] : []
  const observation = body.observe()
  assert(observation.visible.some(b => b.name === 'hay_block'))
  assert(observation.recipes.includes('wheat'))
})
