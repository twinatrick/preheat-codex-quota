# Preheat Codex Quota

這是一個專門在 **ChatGPT 訂閱的 Codex 五小時額度為 100% 剩餘（`usedPercent == 0`）** 時，根據**當前正在運行/使用的 CLI 介面（OpenCode 或 Codex）**，在通過 Python 程式碼強制額度檢查後自動送出一次極小請求以提前啟動 5 小時刷新視窗的技能與 Server 自動掛載工具。

---

## 核心機制與安全防護

1. **Server 自動掛載（無須手動呼叫）**
   - **OpenCode 介面**：透過 OpenCode Server 插件 (`plugins/codex-preheater.ts`) 自動掛載於 OpenCode 背景服務。只要 OpenCode Server 在運行狀態就會自動排程守衛；沒開 OpenCode 就不執行。
   - **Codex CLI 介面**：透過 Codex MCP Server (`codex_preheater.py mcp-server`) 掛載於 `~/.codex/config.toml`。只有啟動 `codex` CLI 時才會隨之啟動；關閉 `codex` 即自動結束。
2. **只針對當前使用的 CLI 介面與當前啟用帳號觸發**
   - 在 **OpenCode** 內：只讀取 OpenCode 當前啟用帳號 (`isActive == true`) 的額度，並只透過 `opencode run` 發送預熱請求，絕不觸發 `codex` CLI。
   - 在 **Codex CLI** 內：只透過 `codex app-server` 讀取額度，並只透過 `codex exec` 發送預熱請求，絕不觸發 `opencode` CLI。
   - 若未在任一 CLI 環境中使用，Python 程式碼直接攔截拒絕執行。
3. **程式碼雙重強制檢查 5 小時 100% 額度**
   - 在 `check_once()` 與底層發送函式 (`ping_opencode()` / `ping_codex()`) 內強制執行 `_assert_quota_eligible()`：
     - 必須為 5 小時訂閱視窗（`duration_minutes == 300`，Free 免費方案或週/月額度一律排除）
     - 必須滿足 `used_percent == 0` 且 `remaining_percent == 100`
     - 未通過檢查一律在程式碼層級強制拋出例外攔截，絕不發送請求。
4. **精準排程（整點後一分 + 刷新後 1 分鐘內搶先預熱）**
   - **整點後一分固定巡檢（`HH:01:00`）**：每小時固定在 `01分00秒`（例如 `14:01:00`、`15:01:00`）自動巡檢一次。
   - **刷新後 1 分鐘內自動預熱（`resetAt + 15s` 與 `+ 45s` 保底）**：當偵測到目前 5 小時額度已有使用量且取得下次刷新時間 `resetAt` 時，自動排程在刷新時間過後 **15 秒**（以及 **45 秒**保底）立即檢查並在回到 100% 時瞬間完成預熱。
5. **直接吃系統 `PATH` 與 280 分鐘防重複鎖**
   - 自動透過 `shutil.which` 解析系統 `PATH` 下的 `opencode` 或 `codex` 執行檔，不寫死路徑。
   - 發送前先透過跨平台檔案鎖寫入狀態檔（預設防重複間隔 280 分鐘，且同一 `resets_at` 視窗絕不重複發送）。

---

## 目錄結構

```text
preheat-codex-quota/
├── README.md                      # 完整安裝、自動掛載與排程說明
├── SKILL.md                       # Agent Skill 規範與指令說明
├── agents/
│   └── openai.yaml                # 介面描述與預設提示設定
├── assets/
│   └── icon.svg                   # 技能圖示
├── plugins/
│   └── codex-preheater.ts         # OpenCode Server 自動掛載插件
└── scripts/
    ├── codex_preheater.py         # 核心預熱與額度檢查腳本（含 Codex MCP Server 模式）
    └── test_preheater.py          # 18 項自動化單元測試
```

---

## 安裝與自動掛載步驟

### 1. 安裝 Skill 本體
直接透過 Git Clone 安裝至 `~/.agents/skills/preheat-codex-quota`：
```powershell
git clone https://github.com/twinatrick/preheat-codex-quota.git "$HOME\.agents\skills\preheat-codex-quota"
```

### 2. 掛載 OpenCode Server 插件（自動隨 OpenCode Server 啟動）
將 `plugins/codex-preheater.ts` 複製到 `~/.config/opencode/plugins/`：
```powershell
New-Item -ItemType Directory -Force -Path "$HOME\.config\opencode\plugins" | Out-Null
Copy-Item -Path "$HOME\.agents\skills\preheat-codex-quota\plugins\codex-preheater.ts" -Destination "$HOME\.config\opencode\plugins\codex-preheater.ts" -Force
```

### 3. 掛載 Codex CLI MCP Server（自動隨 Codex CLI 啟動）
在 `~/.codex/config.toml` 加入以下設定：
```toml
[mcp_servers.codex_preheater]
command = "python"
args = ["C:/Users/<你的使用者名稱>/.agents/skills/preheat-codex-quota/scripts/codex_preheater.py", "mcp-server"]
```

---

## 狀態與日誌位置

- **OpenCode Server 排程與預熱日誌**：
  - Windows：`%LOCALAPPDATA%\codex-preheater\server.log`（即 `C:\Users\<User>\AppData\Local\codex-preheater\server.log`）
  - Linux / macOS：`~/.local/state/codex-preheater/server.log`
- **防重複狀態檔**：
  - Windows：`%LOCALAPPDATA%\codex-preheater\state.json`
  - Linux / macOS：`~/.local/state/codex-preheater/state.json`

---

## 手動測試與單元測試指令

```powershell
# 執行 18 項單元測試
python -m unittest -v scripts/test_preheater.py

# 在 OpenCode 環境手動演練檢查（不實際發送）
python scripts/codex_preheater.py once --dry-run --opencode-limits-file <limits.json>

# 在 Codex CLI 環境手動檢查狀態
python scripts/codex_preheater.py status
```
