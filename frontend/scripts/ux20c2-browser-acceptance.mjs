/**
 * UX20-C2/D1/D2 and UX20-E browser acceptance (Node.js 22 + Chrome CDP, no npm browser package).
 * Mock server binds only 127.0.0.1. Never points Vite at a real Management API.
 */
import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:http'
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const sleep = (ms) => new Promise((done) => setTimeout(done, ms))
const evidence = resolve(process.env.CI_BROWSER_ARTIFACTS || join(tmpdir(), 'modelops-ux20c2-browser'))
const samples = { base: '2026-11-18T02:00:00Z', later: '2026-11-18T04:00:00Z' }
const gpu = (index) => ({
  id: 'gpu-' + index, node_id: 'node-ux20c2',
  gpu_uuid: 'GPU-UUID-' + index, device_index: index,
  model_name: 'TEST-GPU-' + index, vram_total_mb: index ? 24576 : 16384,
  compute_capability: '8.6', safety_margin_mb: 1024, status: 'AVAILABLE',
  last_seen_at: samples.base, created_at: samples.base, updated_at: samples.base,
})
const host = () => ({
  sampled_at: samples.base, cpu_utilization_pct: 12.5,
  ram_total_mb: 65536, ram_used_mb: 16384, ram_free_mb: 49152,
  disk_total_mb: 512000, disk_used_mb: 128000, disk_free_mb: 384000,
})
const measured = (index) => ({
  gpu: gpu(index),
  snapshot: {
    sampled_at: index ? samples.later : samples.base,
    vram_total_mb: index ? 24576 : 16384,
    vram_used_mb: index ? 12288 : 4096,
    vram_free_mb: index ? 12288 : 12288,
    gpu_utilization_pct: index ? null : 40,
    memory_utilization_pct: index ? 20 : 0,
    temperature_c: index ? null : 42,
    power_w: index ? null : 80,
  },
})
const resources = () => {
  if (scenario === 'empty') {
    return { node_id: 'node-ux20c2', source: 'persisted_snapshot', host: null, gpus: [] }
  }
  if (scenario === 'partial') {
    return {
      node_id: 'node-ux20c2', source: 'persisted_snapshot', host: null,
      gpus: [{
        gpu: gpu(0),
        snapshot: {
          sampled_at: samples.base, vram_total_mb: 0, vram_used_mb: 0,
          vram_free_mb: 0, gpu_utilization_pct: 0, memory_utilization_pct: null,
          temperature_c: 0, power_w: 0,
        },
      }, { gpu: gpu(1), snapshot: null }],
    }
  }
  const currentHost = host()
  if (refreshed) currentHost.cpu_utilization_pct = 55
  return {
    node_id: 'node-ux20c2', source: 'persisted_snapshot',
    host: currentHost, gpus: [measured(0), measured(1)],
  }
}
const node = () => ({
  id: 'node-ux20c2', name: 'gpu-node-mock', hostname: 'mock.local',
  agent_base_url: 'http://never-called.invalid', environment: 'test',
  region: 'isolated', status: 'ONLINE', last_heartbeat_at: samples.base,
  cpu_model: 'Mock CPU', ram_total_mb: 65536, disk_total_mb: 512000,
  labels_json: null, created_at: samples.base, updated_at: samples.base,
  gpus: resources().gpus.map((item) => item.gpu),
})

let scenario = 'rich'
let readFailure = false
let postFailure = false
let refreshed = false
let operationReadFailure = false
let operationDetailReadFailure = false
let dashboardDeploymentsFailure = false
const unexpectedApiPaths = []
const requests = []
const exceptions = []
const unexpectedRemoteRequests = []
const passes = []
let server, vite, chrome, websocket, chromeProfile

function sendJson(res, status, body) {
  const data = JSON.stringify(body)
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' })
  res.end(data)
}
async function waitUntil(condition, label, timeout = 15000) {
  const deadline = Date.now() + timeout
  let lastError
  while (Date.now() < deadline) {
    try { if (await condition()) return } catch (error) { lastError = error }
    await sleep(80)
  }
  throw new Error('Timed out: ' + label + (lastError ? ' — ' + String(lastError) : ''))
}
function chromeBinary() {
  if (process.env.CHROME_BIN) return process.env.CHROME_BIN
  for (const name of ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']) {
    if (spawnSync('which', [name], { stdio: 'ignore' }).status === 0) return name
  }
  throw new Error('Headless Chrome/Chromium is not available on the runner')
}
async function connectCdp(port) {
  const targets = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json()
  const page = targets.find((target) => target.type === 'page')
  assert.ok(page?.webSocketDebuggerUrl, 'Chrome page target is available')
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail })
  websocket = ws
  let id = 0
  const pending = new Map()
  ws.onmessage = ({ data }) => {
    const message = JSON.parse(String(data))
    if (message.id) {
      const handler = pending.get(message.id)
      if (handler) { pending.delete(message.id); handler(message) }
      return
    }
    if (message.method === 'Runtime.exceptionThrown') {
      exceptions.push(message.params?.exceptionDetails?.text || 'Runtime exception')
    }
    if (message.method === 'Network.requestWillBeSent') {
      const url = message.params?.request?.url || ''
      if (/^https?:/i.test(url) && !url.startsWith('http://127.0.0.1:3000/')) {
        unexpectedRemoteRequests.push(url)
      }
    }
  }
  return {
    call: (method, params = {}) => new Promise((ok, fail) => {
      const requestId = ++id
      const timeout = setTimeout(() => { pending.delete(requestId); fail(new Error('CDP timeout: ' + method)) }, 12000)
      pending.set(requestId, (response) => {
        clearTimeout(timeout)
        if (response.error) fail(new Error(method + ': ' + response.error.message))
        else ok(response.result)
      })
      ws.send(JSON.stringify({ id: requestId, method, params }))
    }),
  }
}
async function evaluate(cdp, expression) {
  const result = await cdp.call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (result.exceptionDetails) {
    throw new Error('Browser JavaScript: ' + (result.result?.description || result.exceptionDetails.text))
  }
  return result.result?.value
}
async function ready(cdp) {
  await waitUntil(async () => evaluate(cdp, "document.querySelector('.node-resource-v2__overview') !== null"), 'Node resource overview')
  await waitUntil(async () => evaluate(cdp, "document.querySelectorAll('.node-resource-v2__gpu-card').length === " + (scenario === 'empty' ? '0' : '2')), 'GPU cards')
}
async function navigate(cdp, tag) {
  await cdp.call('Page.navigate', { url: 'http://127.0.0.1:3000/nodes/node-ux20c2?accept=' + tag })
  await ready(cdp)
}
async function screenshot(cdp, name) {
  const shot = await cdp.call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  await writeFile(join(evidence, name + '.png'), Buffer.from(shot.data, 'base64'))
}
async function check(name, task) {
  await task()
  passes.push(name)
  console.log('PASS ' + name)
}
async function run() {
  await mkdir(evidence, { recursive: true })
  server = createServer((req, res) => {
    const path = new URL(req.url || '/', 'http://127.0.0.1').pathname
    const method = req.method || 'GET'
    requests.push(method + ' ' + path)
    // UX20-E: public read-only API contracts, with a real empty/zero payload.
    // No fallback to production and no made-up observed metrics.
    if (method === 'GET') {
      if (path === '/health') return sendJson(res, 200, { status: 'ok' })
      if (path === '/ready') return sendJson(res, 200, { status: 'ready' })
      if (path === '/api/v1/nodes' || path === '/api/v1/models' ||
          path === '/api/v1/deployments' || path === '/api/v1/endpoints' ||
          path === '/api/v1/clients') {
        if (path === '/api/v1/deployments' && dashboardDeploymentsFailure) {
          return sendJson(res, 503, { error: { message: 'Mock deployments temporarily unavailable' } })
        }
        const query = new URL(req.url || '/', 'http://127.0.0.1').searchParams
        return sendJson(res, 200, {
          items: [], page: Number(query.get('page') || '1'),
          page_size: Number(query.get('page_size') || '20'), total: 0,
        })
      }
      if (path === '/api/v1/observability/invocations/summary') {
        const query = new URL(req.url || '/', 'http://127.0.0.1').searchParams
        return sendJson(res, 200, {
          hours: Number(query.get('hours') || '24'),
          group_by: query.get('group_by') || 'client',
          items: [], window_start: samples.base, window_end: samples.later,
        })
      }
      if (path === '/api/v1/observability/runtime/latest') {
        return sendJson(res, 200, { items: [] })
      }
    }
    if (path === '/api/v1/nodes/node-ux20c2' && method === 'GET') {
      return sendJson(res, 200, node())
    }
    if (path === '/api/v1/nodes/node-ux20c2/resources/latest' && method === 'GET') {
      return readFailure
        ? sendJson(res, 503, { error: { message: 'Mock read unavailable' } })
        : sendJson(res, 200, resources())
    }
    if (path === '/api/v1/nodes/node-ux20c2/resources/refresh' && method === 'POST') {
      if (postFailure) return sendJson(res, 502, { error: { message: 'Mock collection failed' } })
      refreshed = true
      return sendJson(res, 200, resources())
    }
    if (/^\/api\/v1\/operations\/op-(fail-01|manual-02)$/.test(path) && method === 'GET') {
      if (operationDetailReadFailure) {
        return sendJson(res, 503, { error: { message: 'Mock diagnostic read failed' } })
      }
      const isManual = path.endsWith('op-manual-02')
      const id = isManual ? 'op-manual-02' : 'op-fail-01'
      const details = {
        id, operation_type: 'SWITCH',
        status: isManual ? 'MANUAL_INTERVENTION_REQUIRED' : 'FAILED',
        switch_strategy: 'HOT',
        endpoint_alias_id: null,
        source_deployment_id: 'dep-source-01',
        target_deployment_id: 'dep-target-01',
        current_step: null, cancel_requested_at: null,
        retry_of_operation_id: null, requested_by: 'mock-admin',
        request_reason: null, created_at: samples.base,
        started_at: samples.base, finished_at: samples.later,
        error: { code: isManual ? 'MANUAL_REVIEW' : 'OP_FAILED', message: 'Mock operation diagnostic' },
        steps: [
          { id: 'step-v', sequence_no: 1, step_code: 'VALIDATE', status: 'SUCCEEDED',
            attempt_no: 1, started_at: samples.base, finished_at: samples.base,
            created_at: samples.base, error: null },
          { id: 'step-s', sequence_no: 2, step_code: 'START_TARGET',
            status: isManual ? 'SUCCEEDED' : 'FAILED',
            attempt_no: 1, started_at: samples.base, finished_at: samples.later,
            created_at: samples.base,
            error: isManual ? null : { code: 'GPU_OOM', message: 'GPU out of memory' } },
          { id: 'step-f', sequence_no: 3, step_code: 'FINALIZE', status: 'SUCCEEDED',
            attempt_no: 1, started_at: samples.later, finished_at: samples.later,
            created_at: samples.base, error: null },
        ],
      }
      return sendJson(res, 200, details)
    }
    if (path === '/api/v1/operations' && method === 'GET') {
      if (operationReadFailure) return sendJson(res, 503, { error: { message: 'Mock operation read failed' } })
      const query = new URL(req.url || '/', 'http://127.0.0.1').searchParams
      const operations = [
        { id: 'op-fail-01', operation_type: 'SWITCH', status: 'FAILED', switch_strategy: 'HOT',
          endpoint_alias_id: null, source_deployment_id: null, target_deployment_id: null,
          created_at: samples.base },
        { id: 'op-manual-02', operation_type: 'SWITCH',
          status: 'MANUAL_INTERVENTION_REQUIRED', switch_strategy: null,
          endpoint_alias_id: null, source_deployment_id: null, target_deployment_id: null,
          created_at: samples.base },
        { id: 'op-running-03', operation_type: 'SWITCH', status: 'RUNNING', switch_strategy: null,
          endpoint_alias_id: null, source_deployment_id: null, target_deployment_id: null,
          created_at: samples.base },
      ]
      const filtered = operations.filter((op) => {
        if (query.has('status') && query.get('status') !== op.status) return false
        if (query.get('active') === 'true' && op.status !== 'RUNNING') return false
        if (query.get('active') === 'false' && op.status === 'RUNNING') return false
        if (query.has('operation_type') && query.get('operation_type') !== op.operation_type) return false
        return true
      })
      return sendJson(res, 200, { items: filtered, page: 1, page_size: 20, total: filtered.length })
    }
    if (path.startsWith('/api/') || path === '/health' || path === '/ready') {
      unexpectedApiPaths.push(method + ' ' + path)
    }
    return sendJson(res, 404, { error: { message: 'Blocked by isolated mock backend' } })
  })
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok))
  const port = server.address().port
  vite = spawn(process.execPath, ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', '3000', '--strictPort'], {
    cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, VITE_MODELOPS_API_BASE_URL: '', VITE_DEV_PROXY_TARGET: 'http://127.0.0.1:' + port },
  })
  let viteError = ''
  vite.stderr.on('data', (chunk) => { viteError += String(chunk).slice(-2000) })
  await waitUntil(async () => {
    if (vite.exitCode !== null) throw new Error('Vite exited: ' + viteError)
    const response = await fetch('http://127.0.0.1:3000')
    return response.ok
  }, 'isolated Vite')
  chromeProfile = await mkdtemp(join(tmpdir(), 'modelops-browser-'))
  chrome = spawn(chromeBinary(), [
    '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
    '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
    '--disable-extensions', '--disable-sync', '--disable-component-update',
    '--remote-allow-origins=*', '--remote-debugging-port=0',
    '--user-data-dir=' + chromeProfile, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] })
  let chromeLog = ''
  chrome.stderr.on('data', (chunk) => { chromeLog += String(chunk).slice(-3000) })
  let chromePort = 0
  await waitUntil(async () => {
    if (chrome.exitCode !== null) throw new Error('Chrome exited unexpectedly: ' + chromeLog)
    try {
      const contents = await readFile(join(chromeProfile, 'DevToolsActivePort'), 'utf8')
      chromePort = Number(contents.split('\n')[0])
      return chromePort > 0
    } catch { return false }
  }, 'Chrome DevTools port (stderr: ' + chromeLog + ')', 45000)
  const cdp = await connectCdp(chromePort)
  await cdp.call('Page.enable')
  await cdp.call('Runtime.enable')
  await cdp.call('Network.enable')

  await check('rich independent GPU snapshots and timestamps', async () => {
    scenario = 'rich'
    await navigate(cdp, 'rich')
    await waitUntil(async () => evaluate(cdp, "document.body.innerText.includes('4.0 GiB / 16.0 GiB') && document.body.innerText.includes('12.0 GiB / 24.0 GiB')"), 'rich measurements')
    const values = await evaluate(cdp, "Array.from(document.querySelectorAll('.node-resource-v2__gpu-card')).map(el => ({text:el.innerText, sampled:el.querySelector('.sampled-at')?.innerText}))")
    assert.equal(values.length, 2)
    assert.notEqual(values[0].sampled, values[1].sampled, 'Per-GPU timestamps must not be merged')
    assert.ok(values[0].text.includes('42.0 °C'))
    assert.ok(values[1].text.includes('—'))
    assert.ok((await evaluate(cdp, "document.body.innerText")).includes('DB 저장 스냅샷'))
    const countBefore = requests.filter(x => x.includes('POST')).length
    assert.equal(countBefore, 0, 'Viewing snapshots must never collect new data')
    await evaluate(cdp, "document.querySelector('.node-resource-v2__gpu-identity summary').click()")
    assert.equal(await evaluate(cdp, "document.querySelector('.node-resource-v2__gpu-identity').open"), true)
  })

  await check('320/375/768/1024/1280 responsive and light/dark', async () => {
    for (const width of [320, 375, 768, 1024, 1280]) {
      await cdp.call('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false })
      await sleep(120)
      const sizes = await evaluate(cdp, "({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,inner:window.innerWidth})")
      assert.equal(sizes.inner, width)
      assert.ok(sizes.scroll <= sizes.width + 1, width + 'px horizontal overflow: ' + JSON.stringify(sizes))
      if (width === 320) await screenshot(cdp, 'node-320-light')
    }
    await cdp.call('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] })
    assert.equal(await evaluate(cdp, "matchMedia('(prefers-color-scheme: dark)').matches"), true)
    await screenshot(cdp, 'node-1280-dark')
    await cdp.call('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'light' }] })
  })

  await check('missing GPU, measured zero versus null', async () => {
    scenario = 'partial'
    await navigate(cdp, 'partial')
    await waitUntil(async () => evaluate(cdp, "document.body.innerText.includes('1 / 2개 측정')"), 'partial snapshots')
    const states = await evaluate(cdp, "Array.from(document.querySelectorAll('.node-resource-v2__gpu-card')).map(el => ({text:el.innerText, bars:Array.from(el.querySelectorAll('[role=progressbar]')).map(bar => ({label:bar.getAttribute('aria-label'),value:bar.getAttribute('aria-valuenow')}))}))")
    assert.ok(states[0].text.includes('0 MB / 0 MB'))
    assert.ok(states[1].text.includes('미수집'))
    assert.ok(states[0].bars.some(b => b.label.includes('GPU Utilization') && b.value === '0'))
    assert.ok(!states[0].bars.some(b => b.label.includes('VRAM Used')))
    assert.ok(!states[0].bars.some(b => b.label.includes('Memory Utilization')))
  })

  await check('zero registered GPU and host uncollected', async () => {
    scenario = 'empty'
    await navigate(cdp, 'empty')
    await waitUntil(async () => evaluate(cdp, "document.body.innerText.includes('등록된 GPU가 없습니다.')"), 'empty GPU')
    assert.ok((await evaluate(cdp, "document.body.innerText")).includes('Host 자원이 미수집'))
  })

  await check('GET read failure keeps prior data with warning', async () => {
    scenario = 'rich'
    readFailure = false
    refreshed = false
    await navigate(cdp, 'get-ok')
    await waitUntil(async () => evaluate(cdp, "document.body.innerText.includes('12.5%')"), 'GET baseline')
    readFailure = true
    await evaluate(cdp, "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='새로고침').click()")
    await waitUntil(async () => evaluate(cdp, "document.body.innerText.includes('Mock read unavailable') && document.body.innerText.includes('이전에 조회한 스냅샷')"), 'retained GET warning')
    assert.ok((await evaluate(cdp, "document.body.innerText")).includes('12.5%'))
    readFailure = false
  })

  await check('POST is opt-in and routes only to mock server', async () => {
    await evaluate(cdp, "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='리소스 갱신').click()")
    await waitUntil(async () => evaluate(cdp, "document.body.innerText.includes('55.0%')"), 'mock-only resource refresh')
    const postCount = requests.filter((x) => x === 'POST /api/v1/nodes/node-ux20c2/resources/refresh').length
    assert.equal(postCount, 1)
    postFailure = true
    await waitUntil(async () => evaluate(cdp, "Array.from(document.querySelectorAll('button')).some(b => b.textContent.trim() === '리소스 갱신' && !b.disabled)"), 'resource collection button re-enabled')
    await evaluate(cdp, "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='리소스 갱신' && !b.disabled).click()")
    await waitUntil(async () => evaluate(cdp, "document.body.innerText.includes('Mock collection failed') && document.body.innerText.includes('이전에 조회한 스냅샷')"), 'mock POST error')
    assert.ok((await evaluate(cdp, "document.body.innerText")).includes('55.0%'))
    assert.equal(requests.filter((x) => x.startsWith('POST ')).length, 2)
    assert.equal(unexpectedRemoteRequests.length, 0, 'External requests are forbidden: ' + unexpectedRemoteRequests.join(', '))
    assert.deepEqual(exceptions, [], 'No uncaught Chrome exceptions')
  })
  await screenshot(cdp, 'node-final')

  await check('operation triage filters preserve type and never mutate', async () => {
    await cdp.call('Page.navigate', { url: 'http://127.0.0.1:3000/operations?operation_type=SWITCH' })
    await waitUntil(async () => evaluate(cdp,
      "document.querySelector('.operation-triage__options') !== null && document.querySelector('.operation-triage__results')?.textContent.includes('3건')"),
    'operation triage initial result')
    const beforePosts = requests.filter((request) => request.startsWith('POST ')).length
    const scenarios = [
      ['실패', 'status=FAILED', 'op-fail-01'],
      ['진행 중', 'active=true', 'op-running-03'],
      ['수동 확인 필요', 'status=MANUAL_INTERVENTION_REQUIRED', 'op-manual-02'],
      ['전체 작업', '', 'op-fail-01'],
    ]
    for (const [label, queryPart, visibleId] of scenarios) {
      await evaluate(cdp, "Array.from(document.querySelectorAll('.operation-triage__option')).find(el => el.textContent.trim() === " + JSON.stringify(label) + ").click()")
      await waitUntil(async () => evaluate(cdp, "(() => { const chosen = Array.from(document.querySelectorAll('.operation-triage__option')).find(el => el.textContent.trim() === " + JSON.stringify(label) + "); return chosen?.getAttribute('aria-pressed') === 'true' && location.search.includes('operation_type=SWITCH') && " + (queryPart ? "location.search.includes(" + JSON.stringify(queryPart) + ")" : "!location.search.includes('status=') && !location.search.includes('active=')") + " && Array.from(document.querySelectorAll('a')).some(a => a.getAttribute('href') === '/operations/" + visibleId + "'); })()"), label + ' operation filter')
    }
    assert.equal(requests.filter((request) => request.startsWith('POST ')).length, beforePosts)
    assert.ok(!requests.some((request) => /^GET \/api\/v1\/operations\//.test(request)), 'Operation list must avoid row N+1 detail fetches')
    await cdp.call('Emulation.setDeviceMetricsOverride', { width: 320, height: 900, deviceScaleFactor: 1, mobile: false })
    await sleep(100)
    const bounds = await evaluate(cdp, "({viewport:document.documentElement.clientWidth,content:document.documentElement.scrollWidth})")
    assert.ok(bounds.content <= bounds.viewport + 1, 'Operation mobile overflow: ' + JSON.stringify(bounds))
    await screenshot(cdp, 'operations-320-light')
    await cdp.call('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false })
    await cdp.call('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] })
    await screenshot(cdp, 'operations-1280-dark')
  })

  await check('operation GET failure labels retained results without writes', async () => {
    operationReadFailure = true
    await evaluate(cdp, "Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '새로고침').click()")
    await waitUntil(async () => evaluate(cdp,
      "document.body.innerText.includes('Mock operation read failed') && document.querySelector('.operation-triage__results')?.textContent.includes('이전 조회 결과')"),
    'operation stale results')
    assert.equal(requests.filter((request) => request.startsWith('POST /api/v1/operations')).length, 0)
    assert.equal(unexpectedRemoteRequests.length, 0)
    assert.deepEqual(exceptions, [])
  })

  await check('Operation detail persisted diagnostics and local Step filtering', async () => {
    operationReadFailure = false
    operationDetailReadFailure = false
    await cdp.call('Page.navigate', { url: 'http://127.0.0.1:3000/operations/op-fail-01' })
    await waitUntil(async () => evaluate(cdp,
      "document.querySelector('.operation-diagnostics__summary')?.innerText.includes('3건') && document.body.innerText.includes('START_TARGET')"),
    'Operation diagnostics ready')
    assert.equal(await evaluate(cdp, "document.body.innerText.includes('Docker 컨테이너 로그가 아닙니다')"), true)
    const link = await evaluate(cdp, "Array.from(document.querySelectorAll('.operation-diagnostics__links a')).find(a => a.textContent.includes('변경 대상 Runtime'))?.getAttribute('href')")
    assert.equal(link, '/observability/deployments/dep-target-01')
    const beforeDetailFetches = requests.filter(x => x === 'GET /api/v1/operations/op-fail-01').length
    const beforeMutations = requests.filter(x => x.startsWith('POST ')).length
    // React controlled inputs use the native setter to trigger onChange in Chrome.
    await evaluate(cdp, "(() => { const el = document.querySelector('#operation-step-query'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el, 'GPU_OOM'); el.dispatchEvent(new Event('input',{bubbles:true})); })()")
    await waitUntil(async () => evaluate(cdp,
      "document.querySelector('.operation-diagnostics__filter-count')?.innerText.includes('1건 표시')"),
    'Operation client-side keyword filter')
    const rows = await evaluate(cdp, "Array.from(document.querySelectorAll('table tbody')).flatMap(t=>Array.from(t.querySelectorAll('tr')).map(x=>x.innerText))")
    assert.equal(rows.length, 1)
    assert.ok(rows[0].includes('GPU_OOM'))
    await evaluate(cdp, "document.querySelector('.operation-diagnostics__filter-button').click()")
    await waitUntil(async () => evaluate(cdp,
      "document.querySelector('.operation-diagnostics__filter-button')?.getAttribute('aria-pressed') === 'true'"),
    'Operation error-only filter')
    await evaluate(cdp, "(() => { const el = document.querySelector('#operation-step-query'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el, 'nothing-matches'); el.dispatchEvent(new Event('input',{bubbles:true})); })()")
    await waitUntil(async () => evaluate(cdp,
      "document.body.innerText.includes('표시 조건에 맞는 단계가 없습니다')"),
    'Operation empty filtered result')
    assert.equal(requests.filter(x => x === 'GET /api/v1/operations/op-fail-01').length, beforeDetailFetches)
    assert.equal(requests.filter(x => x.startsWith('POST ')).length, beforeMutations)
    assert.ok(!requests.some(x=>x.endsWith('/steps') || x.includes('/logs') || x.includes('/events')))
    await cdp.call('Emulation.setDeviceMetricsOverride', { width: 320, height: 900, deviceScaleFactor: 1, mobile: false })
    await sleep(100)
    const overflow = await evaluate(cdp, "({client:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})")
    assert.ok(overflow.scroll <= overflow.client + 1, 'Operation detail horizontal overflow: '+JSON.stringify(overflow))
    await screenshot(cdp, 'operation-detail-320-light')
    await cdp.call('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false })
    await cdp.call('Emulation.setEmulatedMedia', { features: [{ name:'prefers-color-scheme',value:'dark' }] })
    await screenshot(cdp, 'operation-detail-1280-dark')
  })

  await check('Operation diagnostic GET failure preserves prior data with search', async () => {
    operationDetailReadFailure = true
    await evaluate(cdp, "Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='새로고침').click()")
    await waitUntil(async () => evaluate(cdp,
      "document.body.innerText.includes('Mock diagnostic read failed') && document.querySelector('#operation-step-query')?.value==='nothing-matches'"),
    'Operation detail stale read')
    assert.equal(requests.filter(x => x.startsWith('POST /api/v1/operations')).length, 0)
    assert.equal(unexpectedRemoteRequests.length, 0)
    assert.deepEqual(exceptions, [])
  })

  await check('UX20-E eight primary routes load truthful empty/zero mock data', async () => {
    operationReadFailure = false
    operationDetailReadFailure = false
    dashboardDeploymentsFailure = false
    const routes = [
      ['/dashboard', '대시보드', '운영 요약', '/api/v1/observability/invocations/summary'],
      ['/nodes', '서버 및 GPU', '등록된 서버가 없습니다.', '/api/v1/nodes'],
      ['/models', '등록된 모델 관리', '등록된 모델이 없습니다.', '/api/v1/models'],
      ['/deployments', '모델 배포 관리', '등록된 모델 배포가 없습니다.', '/api/v1/deployments'],
      ['/endpoints', 'API 연결 관리', '등록된 API 연결이 없습니다.', '/api/v1/endpoints'],
      ['/operations', '작업 이력', 'op-fail-01', '/api/v1/operations'],
      ['/observability', '운영 모니터링', 'AI 요청 사용 현황', '/api/v1/observability/runtime/latest'],
      ['/clients', 'AI 사용 시스템', '등록된 AI 사용 시스템이 없습니다.', '/api/v1/clients'],
    ]
    const log = []
    const beforePostCount = requests.filter(x => !x.startsWith('GET ')).length
    for (const [path, heading, evidenceText, api] of routes) {
      const requestsBefore = requests.filter(x => x === 'GET ' + api).length
      const exceptionsBefore = exceptions.length
      await cdp.call('Page.navigate', { url: 'http://127.0.0.1:3000' + path })
      await waitUntil(async () => evaluate(cdp, "(() => { const h = document.querySelector('main h1'); return location.pathname===" + JSON.stringify(path) +
        " && h?.textContent.trim()===" + JSON.stringify(heading) +
        " && document.body.innerText.includes(" + JSON.stringify(evidenceText) + ")" +
        " && !!document.querySelector('a.nav-link--active[href=" + JSON.stringify(path) + "]')" +
        "; })()"), 'UX20-E route ' + path)
      await waitUntil(async () => requests.filter(x => x === 'GET ' + api).length > requestsBefore,
        'UX20-E API called: ' + api)
      for (const width of [320, 375, 768, 1024, 1280]) {
        await cdp.call('Emulation.setDeviceMetricsOverride', {
          width, height: 900, deviceScaleFactor: 1, mobile: false,
        })
        const dims = await evaluate(cdp, "({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})")
        assert.ok(dims.scroll <= dims.width + 1,
          path + ' / ' + width + 'px horizontal overflow: ' + JSON.stringify(dims))
      }
      assert.equal(exceptions.length, exceptionsBefore, path + ' uncaught exceptions')
      log.push({ route: path, heading, evidenceText, api, widths: [320, 375, 768, 1024, 1280] })
      if (path === '/dashboard') {
        const message = await evaluate(cdp, "document.body.innerText")
        assert.ok(message.includes('요청 0건은 100% 성공이 아닙니다'))
        await cdp.call('Emulation.setDeviceMetricsOverride',
          { width: 375, height: 900, deviceScaleFactor: 1, mobile: false })
        await screenshot(cdp, 'integrated-dashboard-375-light')
      }
      if (path === '/observability') {
        await cdp.call('Emulation.setEmulatedMedia',
          { features: [{ name: 'prefers-color-scheme', value: 'dark' }] })
        assert.ok(await evaluate(cdp, "matchMedia('(prefers-color-scheme: dark)').matches"))
        await screenshot(cdp, 'integrated-observability-1280-dark')
        await cdp.call('Emulation.setEmulatedMedia',
          { features: [{ name: 'prefers-color-scheme', value: 'light' }] })
      }
    }
    assert.equal(requests.filter(x => !x.startsWith('GET ')).length, beforePostCount)
    assert.deepEqual(unexpectedApiPaths, [], 'Unhandled API calls: ' + unexpectedApiPaths.join(', '))
    assert.deepEqual(unexpectedRemoteRequests, [])
    await writeFile(join(evidence, 'ux20e-route-matrix.json'), JSON.stringify(log, null, 2))
  })

  await check('UX20-E mobile drawer keyboard focus, route and desktop resize', async () => {
    await cdp.call('Page.navigate', { url: 'http://127.0.0.1:3000/dashboard' })
    await waitUntil(async () => evaluate(cdp, "document.querySelector('main h1')?.textContent === '대시보드'"),
      'UX20-E drawer dashboard')
    await cdp.call('Emulation.setDeviceMetricsOverride',
      { width: 375, height: 900, deviceScaleFactor: 1, mobile: false })
    await evaluate(cdp, "document.querySelector('.shell__menu-toggle').click()")
    await waitUntil(async () => evaluate(cdp,
      "document.querySelector('.shell__menu-toggle')?.getAttribute('aria-expanded')==='true' && !!document.querySelector('.shell__backdrop') && document.body.style.overflow==='hidden'"),
    'UX20-E mobile menu opened')
    await waitUntil(async () => evaluate(cdp,
      "document.querySelector('.shell__nav')?.contains(document.activeElement)"),
    'UX20-E drawer autofocus')
    const firstFocused = await evaluate(cdp,
      "document.activeElement?.getAttribute('aria-label') || document.activeElement?.textContent.trim()")
    assert.ok(firstFocused, 'drawer first focus must exist')
    await cdp.call('Input.dispatchKeyEvent', { type:'keyDown', key:'Tab', code:'Tab', windowsVirtualKeyCode:9 })
    await cdp.call('Input.dispatchKeyEvent', { type:'keyUp', key:'Tab', code:'Tab', windowsVirtualKeyCode:9 })
    assert.equal(await evaluate(cdp, "document.querySelector('.shell__nav')?.contains(document.activeElement)"), true)
    await cdp.call('Emulation.setDeviceMetricsOverride',
      { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false })
    await waitUntil(async () => evaluate(cdp,
      "document.querySelector('.shell__menu-toggle')?.getAttribute('aria-expanded')==='false' && !document.querySelector('.shell__backdrop') && document.body.style.overflow!=='hidden'"),
    'UX20-E desktop resize drawer cleanup')
    await cdp.call('Emulation.setDeviceMetricsOverride',
      { width: 375, height: 900, deviceScaleFactor: 1, mobile: false })
    await evaluate(cdp, "document.querySelector('.shell__menu-toggle').click()")
    await waitUntil(async () => evaluate(cdp, "document.querySelector('.shell__menu-toggle').getAttribute('aria-expanded')==='true'"),
      'UX20-E reopened drawer')
    await evaluate(cdp, "document.querySelector('.shell__nav a[href=\"/nodes\"]').click()")
    await waitUntil(async () => evaluate(cdp,
      "location.pathname==='/nodes' && document.querySelector('.shell__menu-toggle')?.getAttribute('aria-expanded')==='false' && !document.querySelector('.shell__backdrop') && !!document.querySelector('.shell__nav a.nav-link--active[href=\"/nodes\"]')"),
    'UX20-E route change closes drawer')
    await screenshot(cdp, 'integrated-nodes-375-after-menu')
    assert.deepEqual(unexpectedApiPaths, [])
    assert.deepEqual(exceptions, [])
  })

  await check('UX20-E dashboard partial failure is not presented as healthy', async () => {
    dashboardDeploymentsFailure = true
    await cdp.call('Page.navigate', { url: 'http://127.0.0.1:3000/dashboard' })
    await waitUntil(async () => evaluate(cdp,
      "document.body.innerText.includes('Mock deployments temporarily unavailable') && document.body.innerText.includes('운영 요약')"),
    'UX20-E partial dashboard error')
    const body = await evaluate(cdp, "document.body.innerText")
    assert.ok(body.includes('확인 불가'))
    assert.ok(!body.includes('Management API를 사용할 수 없습니다'),
      'Partial error should not be treated as total Management failure')
    dashboardDeploymentsFailure = false
    assert.deepEqual(unexpectedApiPaths, [])
    assert.deepEqual(unexpectedRemoteRequests, [])
    assert.deepEqual(exceptions, [])
  })
}
try {
  await run()
  await writeFile(join(evidence, 'result.json'), JSON.stringify({ status: 'PASS', passes, requests, unexpectedApiPaths, unexpectedRemoteRequests, exceptions }, null, 2))
  console.log('MODELOPS_BROWSER_ACCEPTANCE_PASS (' + passes.length + ' checks)')
} catch (error) {
  console.error(error)
  await mkdir(evidence, { recursive: true })
  await writeFile(join(evidence, 'result.json'), JSON.stringify({ status: 'FAIL', message: String(error), passes, requests, unexpectedApiPaths, unexpectedRemoteRequests, exceptions }, null, 2))
  process.exitCode = 1
} finally {
  websocket?.close()
  chrome?.kill('SIGTERM')
  vite?.kill('SIGTERM')
  server?.closeAllConnections?.()
  server?.close()
  if (chromeProfile) await rm(chromeProfile, { recursive: true, force: true, maxRetries: 3 }).catch(() => {})
}