import { spawn } from "node:child_process"
import { appendFileSync, existsSync, mkdirSync, readdirSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { pathToFileURL } from "node:url"

const PLUGIN_ID = "codex-preheater"
const SCRIPT_PATH = join(
  homedir(),
  ".agents",
  "skills",
  "preheat-codex-quota",
  "scripts",
  "codex_preheater.py",
)

interface PreheaterDaemonState {
  hourlyTimer?: ReturnType<typeof setTimeout>
  resetTimer15?: ReturnType<typeof setTimeout>
  resetTimer45?: ReturnType<typeof setTimeout>
  initTimer?: ReturnType<typeof setTimeout>
  scheduledResetAtMs?: number
  running?: boolean
  stop: () => void
}

const GLOBAL_KEY = "__codexPreheaterServerDaemon"

function getLogFilePath(): string {
  const base = process.env.LOCALAPPDATA || join(homedir(), ".local", "state")
  const dir = join(base, "codex-preheater")
  try {
    mkdirSync(dir, { recursive: true })
  } catch {
    // ignore
  }
  return join(dir, "server.log")
}

function logServer(message: string): void {
  try {
    const ts = new Date().toLocaleString("sv-SE", { hour12: false })
    appendFileSync(getLogFilePath(), `[${ts}] [opencode-server] ${message}\n`, "utf-8")
  } catch {
    // ignore
  }
}

export function msUntilNextHourPlusOneMinute(nowMs = Date.now()): number {
  const now = new Date(nowMs)
  const target = new Date(nowMs)
  target.setMinutes(1, 0, 0)
  if (target.getTime() <= now.getTime()) {
    target.setHours(target.getHours() + 1, 1, 0, 0)
  }
  return Math.max(1000, target.getTime() - now.getTime())
}

function findMultiAuthDistDir(): string | null {
  const npmCacheRoot = join(
    process.env.LOCALAPPDATA
      ? join(homedir(), ".cache", "opencode", "npm", "oc-codex-multi-auth@latest")
      : join(homedir(), ".cache", "opencode", "npm", "oc-codex-multi-auth@latest"),
  )
  if (!existsSync(npmCacheRoot)) return null
  const versions = readdirSync(npmCacheRoot)
    .filter((name) => /^\d+$/.test(name))
    .sort((a, b) => Number(b) - Number(a))
  for (const ver of versions) {
    const candidate = join(npmCacheRoot, ver, "node_modules", "oc-codex-multi-auth", "dist")
    if (existsSync(join(candidate, "lib", "codex-usage.js"))) {
      return candidate
    }
  }
  return null
}

async function fetchActiveOpenCodeAccountLimits(): Promise<{
  limitsJson: string
  fiveHourUsedPercent?: number
  fiveHourResetAtMs?: number | null
  notStarted?: boolean
} | null> {
  const distDir = findMultiAuthDistDir()
  if (!distDir) {
    logServer("找不到 oc-codex-multi-auth dist 目錄，跳過本次檢查。")
    return null
  }

  const storageMod = await import(pathToFileURL(join(distDir, "lib", "storage.js")).href)
  const usageMod = await import(pathToFileURL(join(distDir, "lib", "codex-usage.js")).href)
  const configMod = await import(pathToFileURL(join(distDir, "lib", "config.js")).href)

  const storage = await storageMod.loadAccounts()
  if (!storage || !Array.isArray(storage.accounts) || storage.accounts.length === 0) {
    return null
  }

  const rawActive =
    typeof storage.activeIndexByFamily?.codex === "number"
      ? storage.activeIndexByFamily.codex
      : typeof storage.activeIndex === "number"
        ? storage.activeIndex
        : 0
  const activeIndex =
    rawActive >= 0 && rawActive < storage.accounts.length ? rawActive : 0
  const account = storage.accounts[activeIndex]
  if (!account || account.enabled === false) {
    return null
  }

  const credentials = await usageMod.ensureCodexUsageAccessToken({
    storage,
    account,
  })
  const accountId = usageMod.resolveCodexUsageAccountId({
    account,
    accessToken: credentials.accessToken,
  })
  if (!accountId) {
    return null
  }

  const rawUsage = await usageMod.fetchCodexUsage({
    accountId,
    accessToken: credentials.accessToken,
    organizationId: account.organizationId,
  })
  const quotaDisplay = configMod.getQuotaDisplay(configMod.loadPluginConfig())
  const parsed = usageMod.parseCodexUsagePayload(rawUsage, quotaDisplay)

  const fiveHour = Array.isArray(parsed.limits)
    ? parsed.limits.find((item: any) => Number(item?.windowMinutes) === 300)
    : undefined

  const payload = {
    totalAccounts: storage.accounts.length,
    activeIndex: activeIndex + 1,
    accounts: [
      {
        index: activeIndex + 1,
        zeroBasedIndex: activeIndex,
        isActive: true,
        planType: parsed.planType ?? null,
        limits: parsed.limits ?? [],
      },
    ],
  }

  return {
    limitsJson: JSON.stringify(payload),
    fiveHourUsedPercent:
      typeof fiveHour?.usedPercent === "number" ? fiveHour.usedPercent : undefined,
    fiveHourResetAtMs:
      typeof fiveHour?.resetAtMs === "number" ? fiveHour.resetAtMs : null,
    notStarted: Boolean(fiveHour?.notStarted),
  }
}

function runPythonPreheater(limitsJson: string): Promise<{ code: number; output: string }> {
  return new Promise((resolve) => {
    const child = spawn(
      "python",
      [SCRIPT_PATH, "once", "--opencode-limits-json", limitsJson],
      {
        env: {
          ...process.env,
          OPENCODE: "1",
          PYTHONUTF8: "1",
        },
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      },
    )
    let out = ""
    child.stdout?.on("data", (chunk) => {
      out += chunk.toString("utf-8")
    })
    child.stderr?.on("data", (chunk) => {
      out += chunk.toString("utf-8")
    })
    child.on("error", (err) => {
      resolve({ code: 1, output: String(err) })
    })
    child.on("close", (code) => {
      resolve({ code: code ?? 1, output: out.trim() })
    })
  })
}

function ensureDaemonMounted(): void {
  const g = globalThis as Record<string, unknown>
  const prev = g[GLOBAL_KEY] as PreheaterDaemonState | undefined
  if (prev && typeof prev.stop === "function") {
    prev.stop()
  }

  const state: PreheaterDaemonState = {
    running: false,
    stop() {
      if (state.initTimer) clearTimeout(state.initTimer)
      if (state.hourlyTimer) clearTimeout(state.hourlyTimer)
      if (state.resetTimer15) clearTimeout(state.resetTimer15)
      if (state.resetTimer45) clearTimeout(state.resetTimer45)
    },
  }
  g[GLOBAL_KEY] = state

  function scheduleRefreshTimers(resetAtMs: number): void {
    const now = Date.now()
    if (resetAtMs <= now) return
    if (state.scheduledResetAtMs === resetAtMs) return

    if (state.resetTimer15) clearTimeout(state.resetTimer15)
    if (state.resetTimer45) clearTimeout(state.resetTimer45)
    state.scheduledResetAtMs = resetAtMs

    const delay15 = Math.max(1000, resetAtMs - now + 15_000)
    const delay45 = Math.max(2000, resetAtMs - now + 45_000)
    const targetTimeStr = new Date(resetAtMs + 15_000).toLocaleString("sv-SE", {
      hour12: false,
    })
    logServer(`已排程下次 5h 額度刷新後 1 分鐘內自動預熱：${targetTimeStr} (+15s 與 +45s 保底)`)

    state.resetTimer15 = setTimeout(() => {
      void executeCheck("reset+15s")
    }, delay15)
    state.resetTimer15.unref?.()

    state.resetTimer45 = setTimeout(() => {
      void executeCheck("reset+45s")
    }, delay45)
    state.resetTimer45.unref?.()
  }

  function scheduleNextHourlyPlusOne(): void {
    if (state.hourlyTimer) clearTimeout(state.hourlyTimer)
    const delay = msUntilNextHourPlusOneMinute()
    const nextAt = new Date(Date.now() + delay).toLocaleString("sv-SE", {
      hour12: false,
    })
    logServer(`已排程下一次整點後一分固定巡檢：${nextAt}`)
    state.hourlyTimer = setTimeout(() => {
      void executeCheck("hourly-HH:01").finally(() => {
        scheduleNextHourlyPlusOne()
      })
    }, delay)
    state.hourlyTimer.unref?.()
  }

  async function executeCheck(reason: string): Promise<void> {
    if (state.running) return
    state.running = true
    try {
      const info = await fetchActiveOpenCodeAccountLimits()
      if (!info) {
        logServer(`[${reason}] 未讀取到有效 OpenCode 啟用帳號額度。`)
        return
      }
      const res = await runPythonPreheater(info.limitsJson)
      const oneLine = res.output.replace(/\r?\n/g, " | ")
      logServer(`[${reason}] 檢查結果 (exit=${res.code}): ${oneLine}`)

      if (
        typeof info.fiveHourUsedPercent === "number" &&
        info.fiveHourUsedPercent > 0 &&
        typeof info.fiveHourResetAtMs === "number" &&
        info.fiveHourResetAtMs > Date.now()
      ) {
        scheduleRefreshTimers(info.fiveHourResetAtMs)
      }
    } catch (err) {
      logServer(`[${reason}] 檢查異常: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      state.running = false
    }
  }

  logServer("OpenCode 預熱守衛已掛載於 Server。")
  scheduleNextHourlyPlusOne()
  state.initTimer = setTimeout(() => {
    void executeCheck("server-mount")
  }, 2000)
  state.initTimer.unref?.()
}

export const CodexPreheaterOpenCodePlugin = async () => {
  ensureDaemonMounted()
  return {}
}

export default {
  id: PLUGIN_ID,
  async setup() {
    ensureDaemonMounted()
  },
  async server() {
    return CodexPreheaterOpenCodePlugin()
  },
}
