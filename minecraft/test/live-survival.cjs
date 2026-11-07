'use strict'
// Hosted integration test. Uses actual TCP game packets and the production body
// bridge. Flying Squid is an MIT protocol implementation, not Mojang's server;
// passing this test does not establish vanilla survival competence or LLM skill.
// Run after installing flying-squid@1.12.0 and socket.io-client@4.8.1 for testing:
//   node minecraft/test/live-survival.cjs
// Optional existing private server: AIAM_TEST_EXISTING_SERVER=1 and
// AIAM_TEST_SERVER_PORT=25565. No JAR download, EULA acceptance, or server setup.
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const { Vec3 } = require('vec3')
const { BodyManager, createApi, createViewer } = require('../bridge.cjs')
const { io } = require('socket.io-client')
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z)
const snapshot = p => ({ x: p.x, y: p.y, z: p.z })
const report = {
  test: 'minecraft_live_protocol_smoke', source_commit: process.env.GITHUB_SHA || null,
  started_at: new Date().toISOString(), game_version: '1.21.4', model_inference: false,
  status: 'running', assertions: [], skill_receipts: [], capabilities: {}, server_errors: [],
  limitations: [
    'Flying Squid is an independent Minecraft-protocol implementation, not the Mojang Java server.',
    'The fixture uses Survival mode, a deterministic flat world, and one deliberately placed resource block.',
    'No LLM participates; this validates the body transport and observable game effects.',
    'Collect/craft capabilities are reported independently from mandatory spawn, movement and stop assertions.'
  ]
}
let manager; let api; let gameServer; let viewer; let cameraClient; let heartbeat; let timeout; let failed = false
function mark (name, data = {}) { report.assertions.push({ name, ...data }); console.log('LIVE_ASSERT ' + name) }
function serializeError (err) { return String(err?.stack || err).slice(0, 2400) }
async function poll (predicate, milliseconds, description) {
  const deadline = Date.now() + milliseconds
  while (Date.now() < deadline) {
    const result = await predicate()
    if (result) return result
    await sleep(100)
  }
  throw new Error('Timed out: ' + description)
}
async function main () {
  const existing = process.env.AIAM_TEST_EXISTING_SERVER === '1'
  report.provider = existing ? 'operator_supplied_server' : 'flying-squid'
  let gamePort = Number(process.env.AIAM_TEST_SERVER_PORT || 25565)
  if (!existing) {
    globalThis.isMocha = true // upstream test flag: do not swallow uncaught server errors
    const squid = require('flying-squid')
    report.provider_version = require('flying-squid/package.json').version
    assert.equal(report.provider_version, '1.12.0', 'CI must pin the server implementation')
    gameServer = squid.createMCServer({
      host: '127.0.0.1', port: 0, version: '1.21.4', 'online-mode': false,
      'max-players': 4, 'max-entities': 100, gameMode: 0, difficulty: 1,
      generation: { name: 'superflat', options: { seed: 2116746182, middleThickness: 3 } },
      worldFolder: undefined, logging: false, noConsoleOutput: true, 'view-distance': 2,
      'everybody-op': false, kickTimeout: 30000, plugins: {}, modpe: false,
      'player-list-text': { header: { text: 'AI-AM integration test' }, footer: { text: 'Protocol smoke only' } }
    })
    gameServer.on('error', err => report.server_errors.push(serializeError(err)))
    gameServer.on('clientError', (_client, err) => report.server_errors.push(serializeError(err)))
    await gameServer.waitForReady(20000)
    gamePort = gameServer.listeningPort
    // Only fixture creation uses authority over server state. The tested agent
    // remains non-operator Survival and must discover references through observe().
    gameServer.getSpawnPoint = async () => new Vec3(0.5, 5, 0.5)
    const log = gameServer.registry.blocksByName.oak_log
    await gameServer.setBlock(gameServer.overworld, new Vec3(0, 5, -5), log.defaultState ?? log.minStateId)
    gameServer.setTime(1000)
    mark('fresh_flat_world_ready', { game_port: gamePort, memory_world: true })
  } else {
    assert(Number.isInteger(gamePort) && gamePort > 0 && gamePort <= 65535)
    report.limitations.push('Existing server is used unchanged; no fixture injection and no collect/craft mutation without explicit AIAM_TEST_MUTATIONS=1.')
  }

  const config = {
    server_host: '127.0.0.1', server_port: gamePort, version: '1.21.4', bridge_port: 8766,
    agents: [
      { id: 'ai-am', username: 'AIAM_Test', viewer_port: 8767 },
      { id: 'mira', username: 'Mira_Test', viewer_port: 8768 }
    ]
  }
  manager = new BodyManager(config)
  const token = crypto.randomBytes(32).toString('hex')
  api = createApi(manager, token)
  await new Promise((resolve, reject) => { api.once('error', reject); api.listen(config.bridge_port, '127.0.0.1', resolve) })
  const base = 'http://127.0.0.1:' + config.bridge_port
  async function request (agent, endpoint, method = 'GET', body = undefined) {
    const response = await fetch(base + '/agents/' + agent + '/' + endpoint, {
      method, headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(25000)
    })
    const value = await response.json()
    if (!response.ok) throw new Error(endpoint + ' HTTP' + response.status + ': ' + JSON.stringify(value))
    return value
  }
  async function observe (id = 'ai-am') { return request(id, 'observation') }
  const bodies = [...manager.agents.values()]
  await poll(() => bodies.every(body => body.connected && body.alive), 30000, 'both real Minecraft clients spawned and alive')
  for (const body of bodies) {
    assert.equal(body.bot.version, '1.21.4')
    assert.equal(body.bot.game.gameMode, 'survival')
    await poll(() => body.bot.world?.getColumnAt(body.bot.entity.position), 10000,
      'spawn terrain decoded from actual chunk packets for ' + body.config.id)
  }
  if (gameServer) {
    const fixtureLog = new Vec3(0, 5, -5)
    await poll(() => bodies.every(body => body.bot.blockAt(fixtureLog)?.name === 'oak_log'),
      10000, 'both players received the fixture log through the game connection')
  }
  mark('two_real_clients_spawned_in_survival', { agents: bodies.map(b => ({ id: b.config.id, version: b.bot.version, position: snapshot(b.bot.entity.position) })) })
  assert.equal((await observe()).enabled, false, 'Joining must not automatically enable the body')
  assert((await observe()).health > 0, 'Health must reach the bridge after spawn')
  viewer = createViewer(manager, 'ai-am', 8767)
  await new Promise((resolve, reject) => { viewer.once('error', reject); viewer.listen(8767, '127.0.0.1', resolve) })
  const viewerUrl = 'http://127.0.0.1:8767'
  for (const asset of ['/', '/index.js', '/worker.js', '/textures/1.21.4.png', '/blocksStates/1.21.4.json']) {
    const response = await fetch(viewerUrl + asset, { signal: AbortSignal.timeout(5000) })
    assert(response.ok, 'Viewer asset missing: ' + asset)
    assert((await response.arrayBuffer()).byteLength > 0, 'Viewer asset empty: ' + asset)
  }
  const camera = { version: null, chunks: 0, position: null }
  cameraClient = io(viewerUrl, { autoConnect: false, reconnection: false, transports: ['websocket'], timeout: 5000 })
  cameraClient.on('version', version => { camera.version = version })
  cameraClient.on('loadChunk', chunk => { if (chunk?.chunk) camera.chunks++ })
  cameraClient.on('position', view => { camera.position = view.pos })
  cameraClient.connect()
  await poll(() => camera.version === '1.21.4' && camera.chunks && camera.position, 10000, 'observer receives actual chunks and pose')
  mark('observer_served_assets_and_streamed_world', { chunks_received: camera.chunks })
  await request('ai-am', 'start', 'POST', {})
  await request('mira', 'start', 'POST', {})
  let heartbeatError = null
  heartbeat = setInterval(() => {
    Promise.all(['ai-am', 'mira'].map(id => request(id, 'heartbeat', 'POST', {}))).catch(err => { heartbeatError = err })
  }, 2000)
  let actionSequence = 0
  async function act (skill, target = '', id = 'ai-am', timeoutMs = 15000) {
    if (heartbeatError) throw heartbeatError
    const observation = await observe(id)
    const action = { action_id: 'live-' + (++actionSequence), skill, target, observation_id: observation.observation_id, timeout_ms: timeoutMs }
    const before = snapshot(manager.get(id).bot.entity.position)
    await request(id, 'actions', 'POST', action)
    const receipt = await poll(async () => {
      const r = await request(id, 'actions/' + action.action_id)
      return r.status !== 'running' ? r : null
    }, timeoutMs + 5000, skill + ' terminal receipt')
    await poll(() => !manager.get(id).active, 3000, skill + ' releases body lock')
    const after = snapshot(manager.get(id).bot.entity.position)
    const entry = { agent: id, action, before, after, receipt }
    report.skill_receipts.push(entry)
    return entry
  }

  let lookPackets = 0
  if (gameServer) {
    const player = gameServer.getPlayer('AIAM_Test')
    for (const event of ['look', 'position_look']) player._client.on(event, () => { lookPackets++ })
  }
  let observation
  let destination
  for (let turn = 0; turn < 4 && !destination; turn++) {
    const result = await act('look')
    assert.equal(result.receipt.status, 'succeeded', JSON.stringify(result.receipt))
    observation = await observe()
    destination = gameServer
      ? observation.visible.find(block => block.id === 'block:0,5,-5' && block.name === 'oak_log')
      : observation.visible.find(block => block.distance > 3 && block.distance < 9)
  }
  if (gameServer) await poll(() => lookPackets > 0, 3000, 'look packets received by real server')
  mark('look_skill_crossed_real_protocol', { server_look_packets: gameServer ? lookPackets : null })
  assert(destination, 'Looking around did not reveal a navigation reference')
  const navigation = await act('navigate', destination.id)
  assert.equal(navigation.receipt.status, 'succeeded', JSON.stringify(navigation.receipt))
  assert(distance(navigation.before, navigation.after) > 0.4, 'Bot position must actually change')
  if (gameServer) {
    await poll(() => distance(gameServer.getPlayer('AIAM_Test').position, manager.get('ai-am').bot.entity.position) < 1, 3000, 'server and client movement agree')
    navigation.server_position = snapshot(gameServer.getPlayer('AIAM_Test').position)
  }
  await poll(() => camera.position && distance(camera.position, navigation.after) < 1, 4000, 'camera follows actual movement')
  mark('navigation_changed_authoritative_position', { before: navigation.before, after: navigation.after, server_position: navigation.server_position || null, observer_position: camera.position })
  const retry = await request('ai-am', 'actions', 'POST', navigation.action)
  assert.equal(retry.status, navigation.receipt.status)
  assert.equal(manager.get('ai-am').active, null, 'Identical action retry must not replay motion')
  mark('terminal_action_retry_did_not_reexecute')

  // Do not quietly redefine an incomplete server's behavior as vanilla success.
  // These attempts use only the production skill; no injected inventory results.
  if (!existing || process.env.AIAM_TEST_MUTATIONS === '1') {
    observation = await observe()
    const collectTarget = observation.visible.find(b => b.name === 'oak_log')
    if (collectTarget) {
      const collected = await act('collect', collectTarget.id, 'ai-am', 12000)
      report.capabilities.collect = { status: collected.receipt.status, reason: collected.receipt.reason, inventory_delta: collected.receipt.inventory_delta || null }
      if (collected.receipt.status === 'succeeded') {
        assert((collected.receipt.inventory_delta?.oak_log || 0) > 0)
        if (gameServer) assert.notEqual((await gameServer.overworld.getBlock(new Vec3(collectTarget.position.x, collectTarget.position.y, collectTarget.position.z))).name, 'oak_log')
        const available = await observe()
        if (available.recipes.includes('oak_planks')) {
          const crafted = await act('craft', 'oak_planks', 'ai-am', 12000)
          report.capabilities.craft = { status: crafted.receipt.status, reason: crafted.receipt.reason, inventory_delta: crafted.receipt.inventory_delta || null }
          if (crafted.receipt.status === 'succeeded') assert((crafted.receipt.inventory_delta?.oak_planks || 0) > 0)
        } else report.capabilities.craft = { status: 'not_available', reason: 'No actual oak_planks recipe after collect' }
      } else report.capabilities.craft = { status: 'not_attempted', reason: 'Collect did not produce verified log inventory' }
    } else report.capabilities.collect = { status: 'not_available', reason: 'No visible log after navigation' }
  } else report.capabilities.collect = { status: 'not_attempted', reason: 'Existing-server mutations not enabled' }

  const waitObservation = await observe('mira')
  const waitAction = { action_id: 'live-stop-check', skill: 'wait', target: '', observation_id: waitObservation.observation_id, timeout_ms: 5000 }
  await request('mira', 'actions', 'POST', waitAction)
  await request('mira', 'stop', 'POST', {})
  const stopped = await request('mira', 'actions/' + waitAction.action_id)
  assert.equal(stopped.status, 'cancelled')
  assert.equal((await observe('mira')).enabled, false)
  mark('stop_cancelled_real_connected_body')
  // Stop heartbeats before the explicit lease test; observer GET must not renew it.
  clearInterval(heartbeat); heartbeat = null
  await request('ai-am', 'heartbeat', 'POST', {})
  await poll(async () => !(await observe()).enabled, 18000, 'lease expiry disables a live body')
  mark('read_only_observation_did_not_extend_supervisor_lease')

  if (process.env.AIAM_TEST_REQUIRE_COLLECT === '1') assert.equal(report.capabilities.collect?.status, 'succeeded', 'Strict collect capability requested')
  if (process.env.AIAM_TEST_REQUIRE_CRAFT === '1') assert.equal(report.capabilities.craft?.status, 'succeeded', 'Strict craft capability requested')
  assert.equal(report.server_errors.length, 0, 'Server emitted errors; inspect captured diagnostics')
  report.status = 'passed'
}

async function cleanup () {
  clearTimeout(timeout); clearInterval(heartbeat)
  cameraClient?.disconnect()
  if (viewer) await new Promise(resolve => viewer.close(resolve))
  try { manager?.close() } catch (_) {}
  await sleep(500) // allow protocol compression streams to drain before teardown
  if (api) { api.closeAllConnections(); await new Promise(resolve => api.close(resolve)) }
  if (gameServer) {
    gameServer.stopTickInterval?.()
    try { await Promise.race([gameServer.quit(), sleep(2000)]) } catch (err) { report.cleanup_error = serializeError(err) }
  }
}
async function finish (err) {
  if (failed) return
  failed = true
  if (err) { report.status = 'failed'; report.error = serializeError(err) }
  await cleanup()
  report.finished_at = new Date().toISOString()
  console.log('AIAM_LIVE_SMOKE ' + JSON.stringify(report))
  process.exit(report.status === 'passed' ? 0 : 1)
}
process.once('uncaughtException', err => { void finish(err) })
process.once('unhandledRejection', err => { void finish(err) })
timeout = setTimeout(() => { void finish(new Error('Live integration exceeded120seconds')) }, 120000)
main().then(() => finish(), err => finish(err))
