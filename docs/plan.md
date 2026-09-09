# LINE 團體記錄助手 — 技術規劃

## Context

`docs/user_stories.md`已定案：把itineraryManager「LINE群組討論 → LLM整理成文件 → 指令式溯源問答」的核心概念做成通用版，讓使用者自訂主題與整理prompt。**最終目標是取代itineraryManager**，不是永久並存——旅遊規劃只會變成這個通用系統底下「客製`organize_prompt`」的其中一種設定。開發期間跟itineraryManager完全平行、不共用資料表，指令名稱直接沿用itineraryManager既有的（`/開始`、`/整理`、`/問`、`/檢查`），開發/過渡期兩邊指令撞名的問題刻意先不處理。

這份文件延續itineraryManager `docs/plan.md`已經驗證過的技術決策（同一組人、同一個作者），不重新查證那些已經有答案的問題，只記錄這個專案獨有的決策跟理由。

---

## 部署拓樸：Cloud Run（運算）＋ Cloudflare D1／R2（資料），不跟itineraryManager共用

**決策沿革**：一開始規劃是「全新Cloudflare Worker，跟itineraryManager一樣的技術棧」，後來因為itineraryManager被Cloudflare Workers `fetch()` handler「約30秒、不可catch的平台級kill」這個限制搞得很痛苦（webhook路徑跟cron路徑要分開設計不同timeout預算、`httpx2`這個Anthropic SDK專屬fork的connect timeout要另外調、GPT-5的`reasoning_effort`要另外注意——這些坑itineraryManager `docs/plan.md`都有記錄），決定重新選型運算平台；資料庫/儲存則維持在Cloudflare，理由見下方。

**最終決策：Google Cloud Run（運算）＋ Cloudflare D1（資料庫，走REST API）＋ Cloudflare R2（照片儲存，不動）＋ Cloud Scheduler（取代Cron Triggers）＋ 全新LINE Channel（跟itineraryManager分開）。**

### 為什麼是Cloud Run，不是繼續用Cloudflare Workers

Cloud Run不是FaaS沙盒，是一般的長時間執行container process（跑FastAPI/uvicorn的Docker image）——沒有另一層「平台自己的隱藏kill」，自己設的timeout就是唯一的timeout，Python的`httpx`/`anthropic` SDK的timeout是真正可以catch的，不會再有Pyodide/WASM那種傳輸層怪異行為，也不需要`httpx2`這種平台專屬fork。

| | Cloudflare Workers（itineraryManager現況） | Cloud Run（這個專案的選擇） |
|---|---|---|
| Timeout | HTTP路徑~30秒，**不可catch**；cron路徑~15分鐘，兩套預算要分開設計 | 最長60分鐘，一個設定值，webhook跟cron可以共用同一套，「現在在哪個預算底下」這整個問題消失 |
| Python執行環境 | Pyodide（WASM），需要vendor相容wheel、`httpx2`這種平台專屬fork | 原生CPython，一般pip安裝的套件都能用，不用管wasm相容性 |
| 免費額度 | 每天10萬請求 | 每月200萬請求、18萬vCPU秒、36萬GiB秒，永久免費（不是12個月試用） |
| 冷啟動 | 幾乎沒有 | scale-to-zero約0.5-2秒，接DB可能3-5秒；介意的話設`min-instances=1`可完全消除（會有一點點小額費用） |

**評估過但排除的其他方案**：
- **AWS Lambda（用Function URL，不是API Gateway）**：次選。Function URL能把完整15分鐘timeout暴露在同步HTTP路徑上，免費額度也是永久的（每月100萬請求＋40萬GB秒）。但終究還是FaaS，15分鐘上限本質上還是平台的隱藏kill，只是設得夠寬鬆幾乎不會撞到——不像Cloud Run是結構性不存在這個問題。
- **Fly.io**：免費層2024年10月已取消，新帳號只剩7天2小時試用，不符合「永久免費」的需求。
- **Vercel／Netlify／Deno Deploy**：查證過，這些都是edge function平台，一樣有很緊的同步timeout（Vercel Hobby預設10秒、Netlify同步function封頂60秒），不會解決根本問題，只是換一個一樣緊的限制。
- **便宜VPS**（例如Hetzner CX22約$4.59/月）：完全沒有平台限制，但要自己管作業系統更新/systemd/TLS，不想要這個維運負擔，列為備案（Cloud Run哪天真的不夠用才考慮）。

### 資料庫：維持Cloudflare D1，不換Turso

原本考慮換成Turso（SQLite相容的代管服務，免費層更大方：5GB儲存／每月5億次讀取／1000萬次寫入），但**最後決定維持D1**，理由是不想為了這個實驗性專案多辦一個新帳號——Cloud Run本身就需要辦一個新的GCP帳號了，不想再加一個Turso帳號。

D1支援在Workers之外用REST API查詢（正式文件記載、有API scope權限控管，不是wrangler內部的野路子）：
```
POST https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query
Authorization: Bearer <API token，D1 Read/Write權限>
```

**D1 REST API vs Turso的優缺點**（記錄討論過程，之後如果D1的延遲真的變成問題，可以回來這裡重新考慮換Turso）：
- 兩者都是「遠端資料庫」，從Cloud Run的角度來看都是網路呼叫、不是local read——這點沒有差別，一開始以為這是D1獨有的缺點，講得不夠精確。
- **Turso唯一真正的優勢**：支援「embedded replica」，可以在Cloud Run那邊放一份本地同步的SQLite副本，讀取幾乎零延遲、只有寫入需要真的打網路同步。D1的REST API沒有這種機制，每次查詢（不管讀或寫）都是一次真正的HTTPS往返。
- **這個差異在這個專案的實際規模下不重要**：小群組、間歇性使用、沒有即時性要求，Cloud Run的timeout又設得很寬——`organize_trip`即使因為改用REST API多了幾個依序查詢的網路延遲，整個流程頂多多個一兩秒，遠低於timeout上限。
- R2維持不動（S3相容API，`boto3`直接接，跟運算平台選擇無關）。

沿用itineraryManager已經驗證過的其他技術選型（LLM：Anthropic/OpenAI；LIFF前端：純HTML+JS）。不沿用的是Cloudflare Workers本身、`wrangler`/`pyodide`相關的build tooling、Cron Triggers——這些itineraryManager的`worker/pyproject.toml`／`worker/wrangler.jsonc.example`雛形對這個專案不適用，改成一般的`Dockerfile` + `requirements.txt` + Cloud Run service設定。

---

## 資料模型（D1 / SQLite）—— 初稿

```sql
topics (
  id TEXT PRIMARY KEY,
  line_group_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,              -- 'active' | 'ended'
  owner_line_user_id TEXT NOT NULL,
  organize_prompt TEXT,               -- NULL = 用organize.py的GENERIC_ORGANIZE_PROMPT
  -- 最初草稿是expense_enabled/poll_enabled兩個布林欄位，Phase 2設計討論後改成這個單一逗號分隔
  -- 字串欄位（例如"記帳,投票"）——見下方「指令設計」跟CLAUDE.md，不是每個模組各開一個欄位，
  -- 以後加新模組不用schema migration。
  enabled_modules TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL,
  ended_at INTEGER
)
-- unique index (line_group_id) WHERE status='active'，跟itineraryManager的trips表同樣手法

topic_messages (
  id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES topics(id),
  line_message_id TEXT NOT NULL UNIQUE,
  line_user_id TEXT NOT NULL,
  user_display_name TEXT,
  msg_type TEXT NOT NULL,
  text TEXT,
  sent_at INTEGER NOT NULL,
  unsent_at INTEGER,                 -- 直接比照itineraryManager已經做好的收回訊息處理，不重新設計
  organized_at INTEGER
)

topic_docs (
  topic_id TEXT PRIMARY KEY REFERENCES topics(id),
  content_md TEXT NOT NULL,
  updated_at INTEGER NOT NULL
)

topic_doc_revisions (
  id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES topics(id),
  content_md TEXT NOT NULL,
  triggered_by_message_id TEXT REFERENCES topic_messages(id),
  edited_by_user_id TEXT,
  edited_by_display_name TEXT,
  created_at INTEGER NOT NULL
)
-- 這張表的設計直接照抄itineraryManager同名機制（AI整理跟手動編輯共用一張表，
-- 用edited_by_*是否有值分辨來源），是Phase 3才會用到，但先建表不影響Phase 1

-- Phase 2（記帳/投票模組）才需要：
topic_expenses (...)       -- 照抄itineraryManager的expenses表結構
topic_expense_splits (...) -- 照抄expense_splits
topic_polls (...)          -- 照抄polls，但`topic`欄位改名`question`（跟本專案的topic概念撞名）
topic_poll_options (...)   -- 照抄poll_options
topic_poll_votes (...)     -- 照抄poll_votes

-- llm_usage表可以整個沿用itineraryManager的設計（不是複製資料，是同樣的表結構在這個獨立的D1裡重建一份）
```

---

## 指令設計

指令名稱直接沿用itineraryManager既有的短指令（不用「主題」前綴，見Context段落的理由），跟itineraryManager的差別純粹是「這是另一個bot」，指令文字本身完全相同：

| 指令 | 說明 | Phase |
|---|---|---|
| `/開始 <名稱> [記帳] [投票]` | 建立新主題，後面關鍵字決定開放哪些模組 | 1 |
| `/結束` | 結束目前主題（僅主揪） | 1 |
| `/整理` | 手動觸發整理 | 1 |
| `/問 <問題>` | 溯源問答 | 1 |
| `/檢查` | 查核機制 | 1 |
| `/結算` | 記帳結算查詢，僅開了記帳模組的主題可用 | 2 |
| （記帳/投票的新增/編輯/結算/投票指令） | 比照itineraryManager Epic D/E，指令文字也相同 | 2 |
| `/說明` | 說明文字 | 1 |

沒有`/模型`：先假設整理/問答/查核固定用一組模型常數（比照itineraryManager MVP剛上線時的作法），要不要做成runtime可調的`/模型`指令等這邊也長期穩定運作後再視需要補上，不在第一版就做——itineraryManager是先有痛點（sonnet-5連線問題）才生出`/模型`這個功能，這邊還沒有那個痛點，先不要為了對稱而对称。

---

## 整理/問答/查核的prompt設計

- **整理**：`organize_topic(env, topic_id, ...)`從`topics.organize_prompt`讀取這個主題的客製prompt，組進LLM呼叫的system prompt裡；建立主題時的預設值是一段很簡短、通用的指示（例如「請把新訊息中與主題相關的內容整理進文件，用條列式呈現，保留原本已有的內容」），不像itineraryManager那樣寫死複雜的章節骨架規則。
- **問答／查核**：直接照抄itineraryManager `qa.py`／`fact_check.py`的SYSTEM_PROMPT，只把措辭裡「旅行規劃助手」改成「記錄助手」之類的通用說法，規則本身不用因主題而異。
- **文件結構驗證**：比照itineraryManager的`validate_doc()`概念，但只檢查`doc.strip().startswith("# ")`，不檢查任何特定章節標題（因為客製prompt可能完全不會產生固定結構）。

---

## 分期規劃

- **Phase 1**（已完成並部署）：主題建立/結束、被動訊息收集、整理（客製prompt）、問答、查核。LIFF只做唯讀顯示。已在正式環境（Cloud Run + D1 + 真實LINE Channel）實測過。
- **Phase 2**（已完成並部署）：記帳、投票模組（`/開始 <名稱> [記帳] [投票]`時可選開啟）。模組開關存成`topics.enabled_modules`一個逗號分隔欄位，模組清單集中定義在`modules.py`，細節見`CLAUDE.md`「目前狀態」。LIFF頁面補上`liff.getProfile()`身分，記帳/投票的寫入動作（新增/編輯/刪除帳目、投票）都在LIFF頁面，跟itineraryManager Epic D/E的聊天/LIFF分工一致。
- **Phase 3**（已完成並部署）：LIFF頁面補上「編輯整理prompt」跟「手動編輯文件＋版本歷史」，UI細節（編輯紀錄放最下面、收合、限3筆、scrollIntoView）沿用itineraryManager已經走過使用者回饋修正的版本，不是重新設計。過程中發現並修正webhook處理方式的架構問題，詳見`CLAUDE.md`「目前狀態」——簡單說：不能沿用itineraryManager的「先回應LINE、背景處理」模式，Cloud Run沒有Cloudflare `ctx.waitUntil()`那種背景工作保證，改成同步處理（`await`到底才回應），這其實才是選Cloud Run本來的目的。

---

## 待確認/待實測

1. 全新LINE Channel的申請流程（Messaging API + Login/LIFF）——itineraryManager當初申請過一次，步驟應該類似，但要重新走一次。
2. GCP帳號、Cloud Run service設定、`Dockerfile`/`requirements.txt`雛形（不是`wrangler.jsonc`，這個專案不用wrangler）；新D1資料庫＋一組僅限D1 Read/Write權限的Cloudflare API token，存進Cloud Run的secrets。
3. **切換時機與方式**：三個Phase都做完、功能對等之後，怎麼把朋友群組從itineraryManager轉過來——搬移既有的`trips`/`messages`等資料進來，還是讓itineraryManager現有的旅程自然跑完、之後新旅程一律用這邊開始。這一步定案之前，itineraryManager繼續正常運作、不受影響。
4. D1 REST API實測：確認每次查詢的實際延遲、Cloudflare API的帳號層級rate limit（常見數字是每5分鐘1200次請求，這個規模應該遠用不到）在這個專案的使用模式下是否真的無感，Phase 1實作時順便驗證，不用另外花時間空測。
