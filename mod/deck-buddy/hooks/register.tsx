// claude-buddy's eyes inside Claude Code.
//
// Replaces settings.json hooks + notify.sh + transcript tailing. Every event
// arrives here typed, and the mod writes the same per-session file the daemon
// already watches (~/.deck-guy/sessions/<sid>.json), plus what hooks never
// had: the thinking/responding phase, live metrics, subagents, and the exact
// call a permission prompt is about.
//
// Approve-from-buddy rides on `classic.PermissionRequest`, which fires only
// when Claude Code is about to put the dialog in front of you - after your
// allow rules, and after auto mode's classifier, which settles most `ask`
// verdicts by itself. (Hooking `tool.check` instead put every raw `ask` on the
// buddy, including the ones auto mode would have approved without you.) The
// daemon is asked over a Unix socket whether it wants to hold the prompt (you
// are looking elsewhere) or not (your terminal is focused, the feature is off,
// he is not running). Every failure path falls through to the dialog, so the
// worst case is the normal terminal prompt - never a silent allow or deny.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { BuddyPending } from '../types'

type $T = EngineInterface
type Json = Record<string, unknown>
type Fields = { file_path: string; command: string; pattern: string; url: string; description: string }

const HEARTBEAT_MS = 15_000
const ASK_MS = 2_000                // the daemon's first answer: hold or not
const HOLD_MS = 10 * 60_000         // longest a held prompt waits on the buddy
const REMEMBER_MS = 10 * 60_000     // "allow 10 min" for one exact call
const FIELD_MAX = 300

// Tools whose dialog is the point (a question, a plan): a hook cannot dismiss
// those, so they always go to the terminal.
const PERSONAL = new Set(['AskUserQuestion', 'ExitPlanMode', 'EnterPlanMode', 'ProposeGoal'])

// Opt-in guard. A match is denied with a reason that says how to turn it off.
const GUARD_BASH: [RegExp, string][] = [
  [/\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|--recursive\s+--force|--force\s+--recursive)\b/, 'rm -rf'],
  [/\bgit\s+push\b[^|;&]*\s(--force(?!-with-lease)|-f)\b/, 'git force push'],
  [/\bgit\s+reset\s+--hard\b/, 'git reset --hard'],
  [/\bgit\s+clean\s+-[a-zA-Z]*f/, 'git clean -f'],
  [/\b(mkfs(\.\w+)?|wipefs)\b|\bdd\b[^|;&]*\bof=\/dev\//, 'writing a block device'],
  [/\bchmod\s+(-R\s+)?777\b/, 'chmod 777'],
  [/\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b/, 'piping a download into a shell'],
]
const GUARD_PATH = /(^|\/)(\.env(\.[\w-]+)?|id_(rsa|ed25519|ecdsa)(\.pub)?|\.netrc|credentials(\.json)?)$|(^|\/)\.ssh\//

const pending = atom({ plugin: 'deck-buddy', key: 'pending' } as const, null as BuddyPending | null)

// ------------------------------------------------------------------ the state
type Agent = { id: string; type: string; description: string; since: number; tool: string }

const S = {
  sid: '',
  cwd: '',
  transcript: '',
  mode: '',
  state: 'idle',
  event: '',
  tool: '',
  input: {} as Fields | Record<string, never>,
  phase: '',
  ts: 0,
  promptTs: 0,
  started: 0,
  alert: '',
  alertMsg: '',
  pids: '',
  model: '',
  effort: '',
  context: 0,
  window: 0,
  cost: 0,
  turns: 0,
  pend: null as null | { id: string; tool: string; input: Fields; reason: string; rule: string; since: number; held: boolean },
  agents: new Map<string, Agent>(),
}

let root = ''           // ~/.deck-guy
let buddyDir = ''       // the folder holding guy.py, '' when not found
let guard = false
let approveOn: boolean | null = null
const remembered = new Map<string, number>()

const now = async ($: $T) => (await $.clock.now()) / 1000

// --------------------------------------------------------------- the writer
let writing: Promise<void> | null = null
let dirty = false

function write($: $T): Promise<void> {
  dirty = true
  if (!writing) {
    writing = (async () => {
      while (dirty) {
        dirty = false
        try {
          await writeOnce($)
        } catch {
          // a missing dir or a full disk costs one frame, never the session
        }
      }
      writing = null
    })()
  }
  return writing
}

async function writeOnce($: $T) {
  if (!S.sid || !root) return
  const t = await now($)
  const doc = {
    // `source` first: notify.sh reads only the first line to see the mod owns
    // this session and steps aside.
    source: 'mod',
    session_id: S.sid,
    state: S.state,
    event: S.event,
    tool: S.tool,
    cwd: S.cwd,
    transcript_path: S.transcript,
    permission_mode: S.mode,
    ts: S.ts || t,
    prompt_ts: S.promptTs || S.ts || t,
    session_started: S.started || t,
    heartbeat: t,
    alert: S.alert,
    alert_msg: S.alertMsg,
    pids: S.pids,
    input: S.input,
    phase: S.phase,
    metrics: {
      model: S.model, effort: S.effort, context: S.context,
      window: S.window, cost: S.cost, turns: S.turns,
    },
    pending: S.pend,
    agents: [...S.agents.values()],
  }
  // One line on purpose: `"source":"mod"` must be on the first line.
  await $.fs.write(`${root}/sessions/${S.sid}.json`, JSON.stringify(doc) + '\n')
}

// ------------------------------------------------------------------ helpers
function str(v: unknown): string {
  return typeof v === 'string' ? v.slice(0, FIELD_MAX) : ''
}

/** The fields the daemon's `describe()` ranks, from any tool input. */
function fields(input: unknown): Fields {
  const i = (input && typeof input === 'object' ? input : {}) as Json
  return {
    file_path: str(i.file_path) || str(i.notebook_path) || str(i.path),
    command: str(i.command),
    pattern: str(i.pattern),
    url: str(i.url),
    description: str(i.description),
  }
}

function target(f: Fields): string {
  return f.command || f.file_path || f.pattern || f.url || f.description || ''
}

/** The tool's own arguments out of a tool.call envelope. */
function argsOf(e: unknown): Json {
  const { tool: _t, tool_use_id: _i, consent: _c, agentId: _a, requestMeta: _r, ...rest } = e as Json
  return rest
}

async function stamp($: $T, event: string) {
  S.event = event
  S.ts = await now($)
}

function clearAlert() {
  S.alert = ''
  S.alertMsg = ''
}

function setAlert(kind: string, msg: string) {
  S.alert = kind
  S.alertMsg = msg.slice(0, FIELD_MAX)
}

// ------------------------------------------------------------ daemon socket
async function call($: $T, path: string, body: Json, ms: number): Promise<Json | null> {
  if (!root) return null
  let timer: { cancel: () => void } | undefined
  const late = new Promise<null>(r => { timer = $.clock.after(ms, () => r(null)) })
  const reply = $.http
    .fetch(`http://buddy${path}`, {
      method: 'POST',
      socketPath: `${root}/buddy.sock`,
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    .then(r => (r.ok ? (JSON.parse(r.text) as Json) : null))
    .catch(() => null)
  try {
    return await Promise.race([reply, late])
  } finally {
    timer?.cancel()
  }
}

/** 'allow' | 'deny' | 'ask' (fall through to the terminal dialog). */
async function askBuddy($: $T, e: { tool: string; id: string; input: unknown; agentId?: string; risk?: string },
                        reason: string, rule: string, signal: AbortSignal): Promise<{ decision: string; remember: boolean }> {
  const id = e.id
  const f = fields(e.input)
  const fallthrough = { decision: 'ask', remember: false }
  const first = await call($, '/ask', {
    session_id: S.sid, id, tool: e.tool, input: f, reason, rule, agent: e.agentId ?? '',
    // what the guard would call it, guard on or off: the card turns red for it
    risk: e.risk ?? '',
  }, ASK_MS)
  if (!first || first.id !== id || first.decision !== 'hold') return fallthrough

  S.pend = { id, tool: e.tool, input: f, reason, rule, since: await now($), held: true }
  setAlert('permission_prompt', `${e.tool} ${target(f)} - waiting on you at the buddy`)
  await write($)
  await update($, pending, () => ({ id, tool: e.tool, target: target(f) }))
  $.ui.status(`🐣 ${e.tool} is waiting on you at the buddy`)

  const cancel = () => { void call($, '/answer', { id, decision: 'ask' }, ASK_MS) }
  signal.addEventListener('abort', cancel)
  try {
    const ans = await call($, '/wait', { id }, HOLD_MS)
    // Only an answer naming this exact call counts; anything else is the dialog.
    if (!ans || ans.id !== id) return fallthrough
    const decision = ans.decision === 'allow' || ans.decision === 'deny' ? ans.decision : 'ask'
    return { decision, remember: decision === 'allow' && ans.remember === true }
  } finally {
    signal.removeEventListener('abort', cancel)
    S.pend = null
    clearAlert()
    $.ui.status(undefined)
    await update($, pending, () => null)
    await write($)
  }
}

function ruleKey(tool: string, input: unknown): string {
  const f = fields(input)
  return `${tool}\u0000${f.command || f.file_path || f.url || f.pattern}`
}

function guardHit(tool: string, input: unknown): string {
  const f = fields(input)
  if (tool === 'Bash' && f.command) {
    for (const [re, what] of GUARD_BASH) if (re.test(f.command)) return what
  }
  if ((tool === 'Edit' || tool === 'Write' || tool === 'NotebookEdit') && GUARD_PATH.test(f.file_path)) {
    return `editing ${f.file_path.split('/').pop()}`
  }
  return ''
}

// --------------------------------------------------------------- the daemon
async function findBuddy($: $T): Promise<string> {
  const home = (await $.env.get('HOME')) ?? ''
  const pinned = await $.env.get('CLAUDE_BUDDY_DIR')
  // The mod ships inside the repo at mod/deck-buddy, so guy.py is two up -
  // from its real path, since a dev folder may reach it through a link.
  const real = await $.fs.stat($.plugin.root, { resolve: true })
    .then(s => s.realPath ?? $.plugin.root).catch(() => $.plugin.root)
  const candidates = [
    pinned ?? '',
    `${real}/../..`,
    `${home}/Desktop/claude-code-buddy`,
    `${home}/Desktop/claude-buddy`,
  ]
  for (const dir of candidates) {
    if (dir && (await $.fs.exists(`${dir}/guy.py`).catch(() => false))) return dir
  }
  return ''
}

async function alive($: $T): Promise<boolean> {
  try {
    const pid = (await $.fs.read(`${buddyDir}/daemon.pid`)).trim()
    if (!/^\d+$/.test(pid)) return false
    return (await $.process.run(['kill', '-0', pid], { timeoutMs: 2_000 })).exitCode === 0
  } catch {
    return false
  }
}

/** Same rules notify.sh had: start him unless he was quit this sitting. */
async function ensureDaemon($: $T, source: string) {
  if (!buddyDir) return
  const display = (await $.env.get('DISPLAY')) || (await $.env.get('WAYLAND_DISPLAY'))
  if (!display) return
  const offMarker = `${root}/off`
  if (source === 'startup' || source === 'resume') {
    await $.process.run(['rm', '-f', offMarker], { timeoutMs: 2_000 }).catch(() => null)
  } else if (await $.fs.exists(offMarker).catch(() => false)) {
    return
  }
  if (await alive($)) return
  // The env strip and GDK_BACKEND=x11 are why notify.sh launched him this way:
  // snap breaks GTK's linking, and GTK3 on native Wayland cannot place him.
  await $.process.run([
    'sh', '-c',
    'exec setsid --fork env -u LD_LIBRARY_PATH -u GTK_PATH -u GIO_MODULE_DIR ' +
      'GDK_BACKEND=x11 python3 "$1/guy.py" >"$1/guy.crash.log" 2>&1 </dev/null',
    'sh', buddyDir,
  ], { timeoutMs: 5_000 }).catch(() => null)
}

/** claude's own pid chain, nearest first - what the daemon matches windows against. */
async function pidChain($: $T): Promise<string> {
  try {
    const r = await $.process.run(['sh', '-c', 'echo $PPID'], { timeoutMs: 2_000 })
    let p = parseInt(r.stdout.trim(), 10)
    const out: number[] = []
    for (let i = 0; i < 10 && p > 1; i++) {
      out.push(p)
      const stat = await $.fs.read(`/proc/${p}/stat`)
      const tail = stat.slice(stat.lastIndexOf(')') + 2).split(' ')
      p = parseInt(tail[1] ?? '0', 10)
    }
    return out.join(',')
  } catch {
    return ''
  }
}

async function refreshApprove($: $T) {
  const r = await call($, '/prefs', {}, ASK_MS)
  approveOn = r && typeof r.approve === 'boolean' ? r.approve : null
}

function statusText(): string {
  const lines = [
    `buddy:    ${buddyDir || 'guy.py not found (set CLAUDE_BUDDY_DIR)'}`,
    `session:  ${S.sid || '?'}  ${S.state}${S.phase ? ` (${S.phase})` : ''}`,
    `approve:  ${approveOn === null ? 'daemon not reachable' : approveOn ? 'on - prompts can be answered from the buddy' : 'off'}`,
    `guard:    ${guard ? 'on' : 'off'}`,
    `subagents: ${S.agents.size}`,
  ]
  return lines.join('\n')
}

// ------------------------------------------------------------------ register
export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const home = (await $.env.get('HOME')) ?? ''
    root = (await $.env.get('DECK_GUY_HOME')) || `${home}/.deck-guy`
    S.cwd = e.cwd
    S.sid = S.sid || (await $.session.id().catch(() => ''))
    S.started = S.started || (await now($))
    S.ts = S.ts || S.started
    buddyDir = await findBuddy($)
    guard = (await $.store.get('guard').catch(() => false)) === true
    S.pids = await pidChain($)

    await $.command.register({
      name: 'buddy',
      description: 'claude-buddy: status, approve on|off, guard on|off, start, wave',
      argumentHint: '[approve on|off | guard on|off | start | wave]',
    })

    $.clock.every(HEARTBEAT_MS, () => { void write($) })
    if (e.isInteractive) await ensureDaemon($, 'startup')
    await refreshApprove($)
    await write($)
    return next(e)
  })

  // Carries what session.start does not: the id after a /clear, the source,
  // the transcript path (still handy for the hook-era fallback).
  on('classic.SessionStart', async ($, e, next) => {
    if (S.sid && S.sid !== e.session_id) {
      S.state = 'ended'
      await write($)
      S.agents.clear()
      S.turns = 0
      S.cost = 0
    }
    S.sid = e.session_id
    S.transcript = e.transcript_path
    S.cwd = e.cwd || S.cwd
    S.mode = e.permission_mode ?? S.mode
    if (e.model) S.model = e.model
    S.started = await now($)
    S.state = 'idle'
    clearAlert()
    await stamp($, 'SessionStart')
    if (e.source === 'resume') await ensureDaemon($, 'resume')
    await write($)
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    S.state = 'ended'
    await write($)
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    S.state = 'working'
    S.phase = 'thinking'
    S.tool = ''
    S.input = {}
    clearAlert()
    await stamp($, 'UserPromptSubmit')
    S.promptTs = S.ts
    await write($)
    return next(e)
  })

  // The one thing hooks could never say: is he thinking, or talking?
  on('turn.step', async function* ($, e, next) {
    if (e.agentId) return yield* next(e)
    S.model = e.model || S.model
    if (e.effort !== undefined) S.effort = String(e.effort)
    for await (const chunk of next(e)) {
      const phase = chunk.kind === 'thinking' ? 'thinking'
        : chunk.kind === 'text' ? 'responding'
        : chunk.kind === 'tool' ? 'tool' : ''
      if (phase && phase !== S.phase) {
        S.phase = phase
        void write($)
      }
      yield chunk
    }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId) return next(e)
    S.state = e.isAborted ? 'idle' : 'jump'
    S.phase = ''
    S.turns += 1
    if (e.reason === 'error') {
      S.state = 'error'
      setAlert('session_failed', 'the turn ended in an error')
    } else {
      clearAlert()
    }
    await stamp($, 'Stop')
    await write($)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    S.context = e.context.tokens ?? S.context
    S.window = e.context.window || S.window
    if (e.cost) S.cost = e.cost.usd
    await write($)
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    const input = fields(argsOf(e))
    if (e.agentId) {
      const a = S.agents.get(e.agentId)
      if (a) {
        a.tool = `${e.tool} ${target(input)}`.trim().slice(0, 80)
        void write($)
      }
      return next(e)
    }
    S.state = 'working'
    S.phase = 'tool'
    S.tool = e.tool
    S.input = input
    if (!S.pend) clearAlert()
    await stamp($, 'PreToolUse')
    await write($)
    const result = await next(e)
    S.phase = 'thinking'
    await stamp($, 'PostToolUse')
    await write($)
    return result
  }).catch(($, e, next) => next(e))

  // Guard and remembered approvals. The buddy itself waits for PermissionRequest.
  on('tool.check', async ($, e, next) => {
    const verdict = await next(e)
    if (!e.tool_use_id) return verdict                  // a query, not a call
    if (guard) {
      const hit = guardHit(e.tool, e.input)
      if (hit) {
        return { decision: 'deny' as const, reason: `claude-buddy guard blocked ${hit}. Ask the user to run /buddy guard off if it is intended.` }
      }
    }
    if (verdict.decision !== 'ask' || PERSONAL.has(e.tool)) return verdict
    // "Allow 10 min" was for this exact call; the person already said yes to it.
    const until = remembered.get(ruleKey(e.tool, e.input))
    if (until !== undefined && until > (await now($))) {
      return { decision: 'allow' as const, reason: 'allowed again from claude-buddy (10 min)' }
    }
    return verdict
  }).catch(($, e, next) => next(e))                  // fail open: the engine's own verdict

  // The dialog is about to open: this is the one moment the person is really
  // being asked. Hold it on the buddy if they are not looking at the terminal.
  on('classic.PermissionRequest', async ($, e, next) => {
    const beneath = await next(e)                    // the person's own settings hooks first
    if (beneath.decision || PERSONAL.has(e.tool_name)) return beneath
    const id = `perm-${crypto.randomUUID()}`
    const risk = guardHit(e.tool_name, e.tool_input)
    const ans = await askBuddy($, { tool: e.tool_name, id, input: e.tool_input, agentId: e.agent_id, risk },
                               '', '', next.signal)
    if (ans.decision === 'allow') {
      if (ans.remember) remembered.set(ruleKey(e.tool_name, e.tool_input), (await now($)) + REMEMBER_MS / 1000)
      return { ...beneath, decision: { behavior: 'allow' as const } }
    }
    if (ans.decision === 'deny') {
      return { ...beneath, decision: { behavior: 'deny' as const, message: 'The user denied this from claude-buddy.' } }
    }
    return beneath
  }).catch(($, e, next) => next(e))                  // fail open: the dialog

  on('agent.spawn', async ($, e, next) => {
    const r = await next(e)
    if (r.agentId) {
      S.agents.set(r.agentId, {
        id: r.agentId, type: e.subagentType, description: e.description.slice(0, 80),
        since: await now($), tool: '',
      })
      await write($)
    }
    return r
  }).catch(($, e, next) => next(e))

  on('classic.SubagentStop', async ($, e, next) => {
    if (S.agents.delete(e.agent_id)) await write($)
    return next(e)
  })

  // Only notifications that mean "I am stuck until you act" are alerts. The
  // end-of-turn "waiting for your input" is Claude being done, not blocked -
  // the jump already said so - and badging it made every finished turn look
  // like a problem.
  const BLOCKING = new Set(['permission_prompt', 'elicitation_dialog'])
  on('classic.Notification', async ($, e, next) => {
    if (!S.pend && BLOCKING.has(e.notification_type)) {
      setAlert(e.notification_type || 'notification', e.message)
      await stamp($, 'Notification')
      await write($)
    }
    return next(e)
  })

  on('classic.PermissionDenied', async ($, e, next) => {
    clearAlert()
    await write($)
    return next(e)
  })

  on('classic.PostToolUseFailure', async ($, e, next) => {
    if (!e.is_interrupt && !e.agent_id) {
      setAlert('tool_failed', `${e.tool_name} failed${e.error ? `: ${e.error}` : ''}`)
      await write($)
    }
    return next(e)
  })

  on('classic.StopFailure', async ($, e, next) => {
    S.state = 'error'
    setAlert('session_failed', String(e.error_details || e.error || 'the session failed'))
    await stamp($, 'StopFailure')
    await write($)
    return next(e)
  })

  // ------------------------------------------------------------- /buddy
  on('command.run', { command: 'buddy' }, async ($, e) => {
    const [verb = '', arg = ''] = e.args.trim().split(/\s+/)
    if (verb === 'guard' && (arg === 'on' || arg === 'off')) {
      guard = arg === 'on'
      await $.store.set('guard', guard)
      return { text: `claude-buddy guard is ${arg}.` }
    }
    if (verb === 'approve' && (arg === 'on' || arg === 'off')) {
      const r = await call($, '/prefs', { approve: arg === 'on' }, ASK_MS)
      if (!r) return { text: 'The buddy is not running, so there is nothing to switch. Try /buddy start.' }
      approveOn = r.approve === true
      return { text: `Answering prompts from the buddy is ${approveOn ? 'on' : 'off'}.` }
    }
    if (verb === 'start') {
      await ensureDaemon($, 'startup')
      return { text: buddyDir ? 'Started the buddy (or he was already up).' : 'Could not find guy.py. Set CLAUDE_BUDDY_DIR.' }
    }
    if (verb === 'wave') {
      const r = await call($, '/cue', { cue: 'wave' }, ASK_MS)
      return { text: r ? '👋' : 'The buddy is not running.' }
    }
    await refreshApprove($)
    return { text: statusText() }
  })

  // ------------------------------------------- band while a prompt is held
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const held = await read($, pending)
    if (!held) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    return (
      <Box>
        <Text color="yellow">🐣 {held.tool} {held.target.slice(0, Math.max(10, e.props.bodyColumns - 40))} </Text>
        <Text dimColor>is waiting on you at the buddy </Text>
        <Button
          key="here"
          label="Answer here"
          hotkey="a"
          onPress={() => { void call($, '/answer', { id: held.id, decision: 'ask' }, ASK_MS) }}
        />
      </Box>
    )
  })
}
