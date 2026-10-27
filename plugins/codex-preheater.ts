/**
 * Codex Quota Preheater — OpenCode Server Auto-Mount Plugin (v1.4.0)
 *
 * 行為規範：
 * 1. 只要 OpenCode Server 啟動即自動掛載於背景，無需使用者手動呼叫。
 * 2. 固定在每個整點後一分（HH:01:00）執行一次檢查。
 * 3. 若讀到 5 小時額度目前已使用（< 100% 剩餘）且有下次刷新時間（resetAtMs），
 *    自動排程在「下次刷新後 1 分鐘內」（+15 秒與 +45 秒雙重保底）觸發檢查並預熱。
 * 4. 所有計時器獨立追蹤於 Set 中，重新載入或停止時完整清除，絕不殘留孤兒計時器。
 * 5. 相容所有 OpenCode 環境（原生 auth.json、oc-codex-multi-auth 帳號檔），
 *    支援 OAuth Refresh Token 輪替（Token Rotation）原子回寫，不依賴任何過期磁碟快照。
 * 6. 實際預熱透過 Python 核心腳本 `codex_preheater.py` 執行，發送前強制重新即時查詢並驗證 5h 剩餘 == 100%。
 */

import { spawn } from "node:child_process"
import { appendFile, mkdir, readFile, rename, stat, unlink, writeFile } from "node:fs/promises"
import { homedir } from "node:os"
import { dirname, join } from "node:path"

const PLUGIN_ID = "codex-preheater-server"
const FIVE_HOURS_MINUTES = 300
const DEFAULT_MODEL = "auto"

const OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
const OPENAI_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
const CHATGPT_WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
const JWT_AUTH_CLAIM = "https://api.openai.com/auth"

interface LimitEntry {
  name?: string
  windowMinutes?: number
  usedPercent?: number
  leftPercent?: number
  resetAtMs?: number | null
  notStarted?: boolean
}

interface AccountQuota {
  index?: number
  isActive?: boolean
  planType?: string
  limits?: LimitEntry[]
}

interface LimitsPayload {
  totalAccounts?: number
  activeIndex?: number
  accounts?: AccountQuota[]
}

interface OAuthCredential {
  accessToken: string
  refreshToken?: string
  expiresAtMs?: number
  accountId?: string
  organizationId?: string
  sourcePath?: string
  sourceKind?: "multi_auth" | "native_auth"
  accountIndex?: number
}

interface RefreshedTokenResult {
  accessToken: string
  refreshToken?: string
  expiresAtMs?: number
}

declare global {
  // eslint-disable-next-line no-var
  var __codexPreheaterDaemon:
    | {
        stop: () => void
      }
    | undefined
}

function getLogFilePath(): string {
  const home = homedir()
  const base =
    process.platform === "win32"
      ? process.env.LOCALAPPDATA || join(home, "AppData", "Local")
      : process.env.XDG_STATE_HOME || join(home, ".local", "state")
  return join(base, "codex-preheater", "server.log")
}

async function logLine(message: string): Promise<void> {
  try {
    const logPath = getLogFilePath()
    await mkdir(dirname(logPath), { recursive: true })
    const ts = new Date().toLocaleString("sv-SE", { hour12: false })
    await appendFile(logPath, `[${ts}] [opencode-plugin] ${message}\n`, "utf-8")
  } catch {
    // 忽略日誌寫入失敗，不影響 OpenCode Server 運行
  }
}

export function msUntilNextHourPlusOneMinute(now = new Date()): number {
  const target = new Date(now.getTime())
  target.setMinutes(1, 0, 0)
  if (target.getTime() <= now.getTime()) {
    target.setHours(target.getHours() + 1, 1, 0, 0)
  }
  return Math.max(1_000, target.getTime() - now.getTime())
}

function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const parts = token.split(".")
    if (parts.length !== 3) return {}
    const payloadB64 = parts[1].replace(/-/g, "+").replace(/_/g, "/")
    const padded = payloadB64 + "=".repeat((4 - (payloadB64.length % 4)) % 4)
    const json = Buffer.from(padded, "base64").toString("utf-8")
    const parsed = JSON.parse(json)
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

function extractAccountIdFromToken(token: string): string | undefined {
  const payload = decodeJwtPayload(token)
  const authClaim = payload[JWT_AUTH_CLAIM]
  if (authClaim && typeof authClaim === "object") {
    const cid = (authClaim as Record<string, unknown>).chatgpt_account_id
    if (typeof cid === "string" && cid.trim()) return cid.trim()
  }
  for (const key of ["chatgpt_account_id", "account_id", "accountId"]) {
    const val = payload[key]
    if (typeof val === "string" && val.trim()) return val.trim()
  }
  return undefined
}

async function refreshOpenAIOAuthToken(refreshToken: string): Promise<RefreshedTokenResult | null> {
  try {
    const body = new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: refreshToken,
      client_id: OPENAI_OAUTH_CLIENT_ID,
    })
    const resp = await fetch(OPENAI_OAUTH_TOKEN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    })
    if (!resp.ok) return null
    const data = (await resp.json()) as Record<string, unknown>
    if (typeof data.access_token !== "string" || !data.access_token.trim()) {
      return null
    }
    const newRefresh =
      typeof data.refresh_token === "string" && data.refresh_token.trim()
        ? data.refresh_token.trim()
        : refreshToken
    const expiresIn = Number(data.expires_in)
    const expiresAtMs =
      Number.isFinite(expiresIn) && expiresIn > 0
        ? Math.round(Date.now() + expiresIn * 1000)
        : undefined
    return {
      accessToken: data.access_token.trim(),
      refreshToken: newRefresh,
      expiresAtMs,
    }
  } catch {
    return null
  }
}

async function persistRefreshedCredential(
  cred: OAuthCredential,
  refreshed: RefreshedTokenResult
): Promise<void> {
  if (!cred.sourcePath || !cred.sourceKind) return
  let tempPath = ""
  try {
    const raw = await readFile(cred.sourcePath, "utf-8")
    const data = JSON.parse(raw) as Record<string, unknown>
    if (!data || typeof data !== "object") return
    let updated = false

    if (cred.sourceKind === "multi_auth") {
      const accounts = Array.isArray(data.accounts) ? data.accounts : []
      const idx = cred.accountIndex ?? -1
      if (idx >= 0 && idx < accounts.length && accounts[idx] && typeof accounts[idx] === "object") {
        const acc = accounts[idx] as Record<string, unknown>
        if ("accessToken" in acc || !("access" in acc)) {
          acc.accessToken = refreshed.accessToken
        } else {
          acc.access = refreshed.accessToken
        }
        if (refreshed.refreshToken) {
          if ("refreshToken" in acc || !("refresh" in acc)) {
            acc.refreshToken = refreshed.refreshToken
          } else {
            acc.refresh = refreshed.refreshToken
          }
        }
        if (refreshed.expiresAtMs && refreshed.expiresAtMs > 0) {
          if ("expiresAt" in acc || !("expires" in acc)) {
            acc.expiresAt = refreshed.expiresAtMs
          } else {
            acc.expires = refreshed.expiresAtMs
          }
        }
        updated = true
      }
    } else if (cred.sourceKind === "native_auth") {
      const openai = data.openai as Record<string, unknown> | undefined
      if (openai && typeof openai === "object" && openai.type === "oauth") {
        if ("access" in openai || !("accessToken" in openai)) {
          openai.access = refreshed.accessToken
        } else {
          openai.accessToken = refreshed.accessToken
        }
        if (refreshed.refreshToken) {
          if ("refresh" in openai || !("refreshToken" in openai)) {
            openai.refresh = refreshed.refreshToken
          } else {
            openai.refreshToken = refreshed.refreshToken
          }
        }
        if (refreshed.expiresAtMs && refreshed.expiresAtMs > 0) {
          if ("expires" in openai || !("expiresAt" in openai)) {
            openai.expires = refreshed.expiresAtMs
          } else {
            openai.expiresAt = refreshed.expiresAtMs
          }
        }
        updated = true
      }
    }

    if (updated) {
      tempPath = `${cred.sourcePath}.${process.pid}.${Date.now()}.tmp`
      await writeFile(tempPath, JSON.stringify(data, null, 2), "utf-8")
      await rename(tempPath, cred.sourcePath)
    }
  } catch {
    if (tempPath) {
      try {
        await unlink(tempPath)
      } catch {
        // ignore cleanup error
      }
    }
  }
}

function mapWhamWindow(window: unknown, nowSec = Date.now() / 1000): LimitEntry | null {
  if (!window || typeof window !== "object") return null
  const w = window as Record<string, unknown>
  const lengthSec = Number(w.limit_window_seconds)
  const used = Number(w.used_percent)
  if (!Number.isFinite(lengthSec) || lengthSec <= 0 || !Number.isFinite(used)) return null

  const windowMinutes = Math.max(1, Math.ceil(lengthSec / 60))
  const resetAfter = typeof w.reset_after_seconds === "number" ? w.reset_after_seconds : undefined
  const resetAt = typeof w.reset_at === "number" ? w.reset_at : undefined
  const remainingSec =
    resetAfter !== undefined ? resetAfter : resetAt !== undefined ? resetAt - nowSec : undefined

  const notStarted =
    used === 0 && remainingSec !== undefined && remainingSec >= lengthSec - 1

  let resetAtMs: number | null = null
  if (!notStarted) {
    if (resetAt !== undefined && resetAt > 0) {
      resetAtMs = Math.round(resetAt * 1000)
    } else if (resetAfter !== undefined && resetAfter > 0) {
      resetAtMs = Math.round((nowSec + resetAfter) * 1000)
    }
  }

  return {
    name: windowMinutes === 300 ? "5h limit" : `${windowMinutes}m limit`,
    windowMinutes,
    usedPercent: used,
    leftPercent: Math.max(0, Math.min(100, Math.round(100 - used))),
    resetAtMs,
    notStarted,
  }
}

async function discoverOpenCodeCredentials(): Promise<OAuthCredential[]> {
  const home = homedir()
  const credentials: OAuthCredential[] = []

  // 1. Multi-auth JSON files (若使用者有安裝 oc-codex-multi-auth)
  const multiAuthPaths = [
    join(home, ".opencode", "oc-codex-multi-auth-accounts.json"),
    join(home, ".opencode", "openai-codex-accounts.json"),
    join(home, ".config", "opencode", "oc-codex-multi-auth-accounts.json"),
  ]
  for (const path of multiAuthPaths) {
    try {
      const raw = await readFile(path, "utf-8")
      const data = JSON.parse(raw) as Record<string, unknown>
      const accounts = Array.isArray(data.accounts) ? data.accounts : []
      if (accounts.length === 0) continue
      const byFamily = data.activeIndexByFamily as Record<string, unknown> | undefined
      const rawActive =
        byFamily && typeof byFamily.codex === "number"
          ? byFamily.codex
          : typeof data.activeIndex === "number"
            ? data.activeIndex
            : 0
      const activeIdx = rawActive >= 0 && rawActive < accounts.length ? rawActive : 0
      const acc = accounts[activeIdx] as Record<string, unknown> | undefined
      if (acc && acc.enabled !== false) {
        credentials.push({
          accessToken: String(acc.accessToken || acc.access || ""),
          refreshToken: typeof (acc.refreshToken || acc.refresh) === "string"
            ? String(acc.refreshToken || acc.refresh)
            : undefined,
          expiresAtMs: typeof (acc.expiresAt || acc.expires) === "number"
            ? Number(acc.expiresAt || acc.expires)
            : undefined,
          accountId: typeof acc.accountId === "string" ? acc.accountId : undefined,
          organizationId: typeof acc.organizationId === "string" ? acc.organizationId : undefined,
          sourcePath: path,
          sourceKind: "multi_auth",
          accountIndex: activeIdx,
        })
        break
      }
    } catch {
      continue
    }
  }

  // 2. OpenCode 原生 auth.json (~/.local/share/opencode/auth.json 或 LOCALAPPDATA/opencode/auth.json)
  const nativeAuthPaths = [
    process.env.XDG_DATA_HOME ? join(process.env.XDG_DATA_HOME, "opencode", "auth.json") : "",
    join(home, ".local", "share", "opencode", "auth.json"),
    process.env.LOCALAPPDATA ? join(process.env.LOCALAPPDATA, "opencode", "auth.json") : "",
  ].filter(Boolean)

  for (const path of nativeAuthPaths) {
    try {
      const raw = await readFile(path, "utf-8")
      const data = JSON.parse(raw) as Record<string, unknown>
      const openai = data.openai as Record<string, unknown> | undefined
      if (openai && openai.type === "oauth") {
        credentials.push({
          accessToken: String(openai.access || openai.accessToken || ""),
          refreshToken: typeof (openai.refresh || openai.refreshToken) === "string"
            ? String(openai.refresh || openai.refreshToken)
            : undefined,
          expiresAtMs: typeof (openai.expires || openai.expiresAt) === "number"
            ? Number(openai.expires || openai.expiresAt)
            : undefined,
          accountId: typeof openai.accountId === "string" ? openai.accountId : undefined,
          organizationId: typeof openai.organizationId === "string" ? openai.organizationId : undefined,
          sourcePath: path,
          sourceKind: "native_auth",
        })
        break
      }
    } catch {
      continue
    }
  }

  return credentials
}

async function fetchWhamUsageWithCredential(cred: OAuthCredential): Promise<LimitsPayload | null> {
  let token = cred.accessToken
  let currentRefresh = cred.refreshToken
  const nowMs = Date.now()
  if (
    (!token || (cred.expiresAtMs && cred.expiresAtMs > 0 && cred.expiresAtMs <= nowMs + 30_000)) &&
    currentRefresh
  ) {
    const refreshed = await refreshOpenAIOAuthToken(currentRefresh)
    if (refreshed) {
      token = refreshed.accessToken
      currentRefresh = refreshed.refreshToken || currentRefresh
      await persistRefreshedCredential(cred, refreshed)
    }
  }
  if (!token) return null

  let resolvedAccountId = cred.accountId || extractAccountIdFromToken(token)

  const doRequest = async (bearer: string) => {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${bearer}`,
      Accept: "application/json",
      "OpenAI-Beta": "responses=experimental",
      originator: "codex_cli_rs",
      "User-Agent": "codex_cli_rs/0.1.0",
    }
    if (resolvedAccountId) headers["chatgpt-account-id"] = resolvedAccountId
    if (cred.organizationId) headers["openai-organization"] = cred.organizationId
    return fetch(CHATGPT_WHAM_USAGE_URL, { method: "GET", headers })
  }

  try {
    let resp = await doRequest(token)
    if (resp.status === 401 && currentRefresh) {
      const refreshed = await refreshOpenAIOAuthToken(currentRefresh)
      if (!refreshed) return null
      await persistRefreshedCredential(cred, refreshed)
      resolvedAccountId = cred.accountId || extractAccountIdFromToken(refreshed.accessToken)
      resp = await doRequest(refreshed.accessToken)
    }
    if (!resp.ok) return null
    const rawUsage = (await resp.json()) as Record<string, unknown>
    const rateLimit = rawUsage.rate_limit as Record<string, unknown> | undefined
    const limits: LimitEntry[] = []
    if (rateLimit && typeof rateLimit === "object") {
      for (const key of ["primary_window", "secondary_window"]) {
        const mapped = mapWhamWindow(rateLimit[key])
        if (mapped) limits.push(mapped)
      }
    }
    return {
      totalAccounts: 1,
      activeIndex: 1,
      accounts: [
        {
          index: 1,
          isActive: true,
          planType: typeof rawUsage.plan_type === "string" ? rawUsage.plan_type : undefined,
          limits,
        },
      ],
    }
  } catch {
    return null
  }
}

async function fetchLiveOpenCodePayload(): Promise<LimitsPayload | null> {
  const credentials = await discoverOpenCodeCredentials()
  for (const cred of credentials) {
    const payload = await fetchWhamUsageWithCredential(cred)
    if (payload) return payload
  }
  return null
}

function extractActiveFiveHourLimit(payload: LimitsPayload): LimitEntry | null {
  const accounts = payload.accounts
  if (!Array.isArray(accounts) || accounts.length === 0) return null
  const active =
    accounts.find((a) => a && a.isActive === true) ||
    accounts.find((a) => a && a.index === payload.activeIndex)
  if (!active || String(active.planType || "").toLowerCase() === "free") return null
  const limits = active.limits
  if (!Array.isArray(limits)) return null
  for (const item of limits) {
    if (!item || Number(item.windowMinutes) !== FIVE_HOURS_MINUTES) continue
    const used = Number(item.usedPercent)
    if (!Number.isFinite(used) || used < 0 || used > 100) continue
    if (item.leftPercent !== undefined && item.leftPercent !== null) {
      const left = Number(item.leftPercent)
      if (
        !Number.isFinite(left) ||
        left < 0 ||
        left > 100 ||
        (used === 0 && left !== 100) ||
        (left === 100 && used !== 0) ||
        Math.abs(100 - used - left) > 0.51
      ) {
        return null
      }
    }
    return item
  }
  return null
}

function getScriptCandidates(): string[] {
  const home = homedir()
  return [
    join(home, ".agents", "skills", "preheat-codex-quota", "scripts", "codex_preheater.py"),
    join(home, ".config", "opencode", "skills", "preheat-codex-quota", "scripts", "codex_preheater.py"),
  ]
}

async function resolveScriptPath(): Promise<string | null> {
  for (const candidate of getScriptCandidates()) {
    try {
      await stat(candidate)
      return candidate
    } catch {
      continue
    }
  }
  return null
}

async function runPreheatThroughPython(): Promise<void> {
  const scriptPath = await resolveScriptPath()
  if (!scriptPath) {
    await logLine("找不到 codex_preheater.py，略過本次預熱執行。")
    return
  }

  const pythonCmd = process.platform === "win32" ? "python" : "python3"
  // 不傳遞任何舊快照 JSON，強制由 Python 核心向即時 OAuth API 拉取最新額度並驗證
  const args = [scriptPath, "once", "--cli", "opencode", "--model", DEFAULT_MODEL]

  await new Promise<void>((resolve) => {
    const child = spawn(pythonCmd, args, {
      env: {
        ...process.env,
        OPENCODE: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    })

    let stdout = ""
    let stderr = ""
    child.stdout?.on("data", (chunk) => {
      stdout += String(chunk)
    })
    child.stderr?.on("data", (chunk) => {
      stderr += String(chunk)
    })
    child.on("close", async (code) => {
      const summary = (stdout || stderr).trim().replace(/\r?\n/g, " | ")
      await logLine(`Python 預熱執行結束 (exit=${code}): ${summary}`)
      resolve()
    })
    child.on("error", async (err) => {
      await logLine(`Python 預熱啟動失敗: ${err.message}`)
      resolve()
    })
  })
}

function startPreheaterDaemon(): { stop: () => void } {
  let hourlyTimer: ReturnType<typeof setTimeout> | null = null
  const resetTimers = new Set<ReturnType<typeof setTimeout>>()
  let scheduledResetAtMs = 0
  let running = false
  let stopped = false

  const clearResetTimers = () => {
    for (const timer of resetTimers) {
      clearTimeout(timer)
    }
    resetTimers.clear()
  }

  const scheduleNextHourly = () => {
    if (stopped) return
    if (hourlyTimer) {
      clearTimeout(hourlyTimer)
      hourlyTimer = null
    }
    const waitMs = msUntilNextHourPlusOneMinute()
    const nextRunAt = new Date(Date.now() + waitMs).toLocaleTimeString("sv-SE", { hour12: false })
    void logLine(`已排程下次整點後一分檢查（將於 ${nextRunAt} 觸發）。`)
    hourlyTimer = setTimeout(() => {
      hourlyTimer = null
      void runCheck("hourly-HH:01")
      scheduleNextHourly()
    }, waitMs)
    hourlyTimer.unref?.()
  }

  const scheduleResetTrigger = (resetAtMs: number) => {
    if (stopped || !resetAtMs || resetAtMs <= Date.now()) return
    if (scheduledResetAtMs === resetAtMs && resetTimers.size > 0) return

    clearResetTimers()
    scheduledResetAtMs = resetAtMs

    // 在刷新時間點後 +15 秒觸發第一次預熱檢查，+45 秒做雙重保底（確保 1 分鐘內拿到刷新）
    const delay15s = Math.max(1_000, resetAtMs - Date.now() + 15_000)
    const delay45s = Math.max(5_000, resetAtMs - Date.now() + 45_000)
    const triggerTimeStr = new Date(Date.now() + delay15s).toLocaleString("sv-SE", { hour12: false })
    void logLine(`已排程下次 5h 額度刷新後 1 分鐘內自動預熱（將於 ${triggerTimeStr} 觸發）。`)

    const timer15 = setTimeout(() => {
      resetTimers.delete(timer15)
      void runCheck("reset+15s")
    }, delay15s)
    timer15.unref?.()
    resetTimers.add(timer15)

    const timer45 = setTimeout(() => {
      resetTimers.delete(timer45)
      void runCheck("reset+45s-fallback")
    }, delay45s)
    timer45.unref?.()
    resetTimers.add(timer45)
  }

  const runCheck = async (reason: string) => {
    if (stopped || running) return
    running = true
    try {
      const payload = await fetchLiveOpenCodePayload()
      if (!payload) {
        await logLine(
          `[${reason}] OpenCode 未能讀取當前即時 OAuth 額度，不執行預熱。`
        )
        return
      }
      const fiveHour = extractActiveFiveHourLimit(payload)
      if (!fiveHour) {
        await logLine(`[${reason}] 當前啟用帳號無有效 5 小時額度視窗（可能為 Free 方案或額度資料異常），不觸發。`)
        return
      }

      const used = Number(fiveHour.usedPercent ?? 100)
      const left = Number(fiveHour.leftPercent ?? 100 - used)
      const resetAtMs = Number(fiveHour.resetAtMs ?? 0)

      if (used === 0 && left === 100) {
        await logLine(`[${reason}] 偵測到 5 小時額度為 100% 剩餘（0% 已用），呼叫 Python 核心執行即時強制驗證與預熱！`)
        clearResetTimers()
        scheduledResetAtMs = 0
        await runPreheatThroughPython()
      } else {
        await logLine(`[${reason}] 當前 5 小時額度剩餘 ${left}%（已用 ${used}%），未達 100%，不發送預熱。`)
        if (resetAtMs > Date.now()) {
          scheduleResetTrigger(resetAtMs)
        }
      }
    } finally {
      running = false
    }
  }

  void logLine("Codex 預熱守衛已掛載於 OpenCode Server。")
  void runCheck("server-startup")
  scheduleNextHourly()

  return {
    stop: () => {
      stopped = true
      if (hourlyTimer) {
        clearTimeout(hourlyTimer)
        hourlyTimer = null
      }
      clearResetTimers()
    },
  }
}

function ensureDaemonMounted(): void {
  if (globalThis.__codexPreheaterDaemon) {
    globalThis.__codexPreheaterDaemon.stop()
  }
  globalThis.__codexPreheaterDaemon = startPreheaterDaemon()
}

export const CodexPreheaterPlugin = {
  id: PLUGIN_ID,
  async server() {
    ensureDaemonMounted()
    return {}
  },
  async setup() {
    ensureDaemonMounted()
  },
}

export default CodexPreheaterPlugin
