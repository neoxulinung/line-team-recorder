# CLAUDE.md

## 這個專案是什麼

通用版的「LINE群組討論 → LLM整理成文件 → 指令式溯源問答」bot。姊妹專案是 `~/vs_code/itineraryManager`（旅行規劃專用版，已上線、有真實使用者）。**這個專案的終點是取代itineraryManager**——不是永久並存的獨立實驗，旅遊規劃只會變成這個通用系統底下「客製`organize_prompt`」的其中一種設定，等功能對等後itineraryManager這個bot就會退役。指令名稱直接沿用itineraryManager既有的（`/開始`、`/整理`、`/問`、`/檢查`），不用「主題」前綴另外發明一套。詳細背景、範圍、決策見 `docs/user_stories.md` 跟 `docs/plan.md`——開始任何工作前先讀這兩份，不要重新臆測已經寫在裡面的決策。

## 跟itineraryManager的關係——最重要的一條規則

**開發期間完全獨立部署，永遠不要修改 `~/vs_code/itineraryManager` 底下的任何檔案。** 那是正在被真實朋友群組使用的bot，退役之前持續有真實使用者在用，這個專案開發期間的任何改動、實驗、部署都不該碰到它——等三個Phase都做完、功能對等，才是另一個獨立的「切換/cutover」步驟。需要參考itineraryManager的做法（技術選型、踩過的坑、某個功能怎麼實作）時用Read工具去讀，不要用Edit/Write動它的檔案。

**重要：這個專案不用Cloudflare Workers/Pyodide當運算平台**（改用Google Cloud Run，見`docs/plan.md`「部署拓樸」章節——這是因為itineraryManager被Cloudflare Workers的~30秒不可catch平台kill搞得很痛苦，才重新選型），資料庫（D1）跟照片儲存（R2）維持在Cloudflare。所以itineraryManager `docs/plan.md`裡以下幾個坑**不適用**於這個專案，不要照抄：
- Cloudflare Workers的~30秒hard ceiling、webhook/cron要分開設計timeout預算——Cloud Run是一般container process，timeout自己設、可以拉到60分鐘，這整套問題不存在。
- `httpx2`這個Anthropic SDK的Pyodide專屬fork——Cloud Run是原生CPython，直接用一般的`httpx`/`anthropic`/`openai`官方SDK預設的httpx transport即可，不用管WASM相容性。

以下幾個坑**還是適用**，因為是模型行為或前端邏輯，跟運算平台無關：
- Claude（`thinking`參數）跟GPT-5系列（`reasoning_effort`參數）都有可能無提示地用掉一部分`max_tokens`/`max_completion_tokens`額度做看不到的推理——記得從一開始就明確關掉（`thinking={"type":"disabled"}` / `reasoning_effort="minimal"`），不要等真的撞到才修。
- LIFF頁面的XSS防護模式：使用者可控內容一律經過`DOMPurify.sanitize(marked.parse(...))`才能進`innerHTML`；純文字欄位（人名等）用`escapeHtml()`。這個專案的LIFF頁面直接照抄這套模式。
- D1改用REST API存取（不是Workers binding）：`POST https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query`，帶`Authorization: Bearer <API token>`。細節跟優缺點（vs. Turso）見`docs/plan.md`。

## 開發流程（沿用itineraryManager這整個engagement下來的習慣）

1. **User story → Plan → 確認 → 實作**，不要跳過中間確認直接動工，尤其是架構層級的決策。
2. **每次重要改動之後主動做一次code review**，即使使用者沒有每次都明講——這是這個作者一貫的期待。
3. 涉及外部服務可行性（例如新LINE Channel的某個功能、Cloudflare的某個限制）要**實測或查證**，不要憑訓練資料裡的印象假設。
4. 秘密/API key絕對不要印在對話輸出裡；設定secrets優先用pipe的方式（不管是`gcloud`還是其他CLI，值要直接從檔案/stdin流進指令，不要先印出來確認格式再貼）。

## Coding style

Ponytail（lazy=efficient不是carelessness）：能重用itineraryManager已經寫好、驗證過的模式就直接照抄或搬過來改，不要因為「這是新專案」就重新發明一遍。三行重複的程式碼好過一個只用一次的抽象。

## 目前狀態

**Phase 1已完成並部署上線**：GCP專案`line-team-recorder`、Cloud Run service（`asia-east1`）、獨立的D1資料庫、LINE Channel（Messaging API + Login/LIFF）、Cloud Scheduler（每小時觸發`/internal/scheduled-organize`）全部建好，主題建立/結束、被動訊息收集、`/整理`、`/問`、`/檢查`都已經在正式環境實測過。指令是bare `/開始`／`/結束`（不是`/旅程`的子指令，也不是`/主題`前綴——見`docs/user_stories.md`「指令命名」段落的沿革）。目前`llm_client.py`的三個模型常數暫時指向`gpt-5-mini`（因為Anthropic key一開始填錯了，先用OpenAI驗證整條pipeline），要記得等Anthropic key確認可用後改回`claude-sonnet-5`/`claude-haiku-4-5`。

**重大架構修正（Phase 3測試時發現）：webhook改成同步處理，不用FastAPI的`BackgroundTasks`**。原本`main.py`的`/webhook`是「先回200給LINE、再用`background_tasks.add_task()`背景處理」，直接照抄itineraryManager的`ctx.waitUntil()`模式——但live測試時發現`/整理`偶爾完全沒反應：沒有錯誤訊息、文件沒被寫入、訊息甚至沒被存進`topic_messages`。追查發現：Cloud Run沒有`ctx.waitUntil()`那種「保證背景工作做完才能真的關閉instance」的機制，如果instance在背景工作跑完之前就因為閒置被縮編/回收，那個asyncio task會直接被砍掉，**不會拋例外、不會留下任何log**。這不是`--no-cpu-throttling`能解決的（那只保證CPU不被throttle，不保證instance不被回收）。

**修法**：`/webhook`直接`await _handle_event(env, event)`，處理完才回應LINE，拿掉`BackgroundTasks`——這其實才是選Cloud Run的原始理由（不用再像Cloudflare那樣搞「先回應、背景處理」，因為timeout本來就很寬鬆），一開始沿用itineraryManager的背景處理習慣是多餘的。改完之後`--no-cpu-throttling`也跟著拿掉了（`gcloud run services update ... --cpu-throttling`），因為那個flag本來就是為了讓背景工作在請求結束後還能拿到CPU，同步處理不需要它，拿掉還比較省錢。**這條經驗值得記住：itineraryManager的「先ack再背景處理」模式是Cloudflare `ctx.waitUntil()`這個平台專屬機制底下的產物，不能原封不動搬到任何其他平台，每個平台都要重新確認背景工作的保證機制是什麼。**

**Phase 2（記帳/投票模組）已完成並部署上線**：`topic_expenses`／`topic_expense_splits`／`topic_polls`（`question`欄位，不叫`topic`——跟本專案的topic概念撞名）／`topic_poll_options`／`topic_poll_votes`五張表都建好了，`expenses.py`／`polls.py`直接照抄itineraryManager邏輯，`trip_id`→`topic_id`；`default_participants`收窄成只查該`topic_id`期間講過話的人（跟itineraryManager唯一不同的行為，其他direct照抄）。

**模組開關的實作方式**（Phase 2設計討論定案）：`topics.enabled_modules`是單一逗號分隔字串欄位（例如`"記帳,投票"`），不是每個模組各開一個布林欄位——以後加新模組不需要schema migration。模組清單集中定義在`modules.py`的`MODULES` dict裡（目前只有`記帳`／`投票`兩個key），`/開始 <名稱> [記帳] [投票]`的關鍵字解析（`modules.parse_start_args`）跟每個指令執行前的「這個模組有沒有開」檢查（`modules.is_enabled`）都從這個dict取得，不是散落在main.py裡的字串比對。指令分派本身刻意維持`main.py`裡一長串`if cmd == ...`，沒有做成command registry/plugin loader——這個規模的bot（十幾個指令）用不到那種抽象，「加新功能方便」靠的是`enabled_modules`跟`MODULES`這兩點，不是分派機制本身。

LIFF頁面（`liff_page.py`）現在有`ME`/`liff.getProfile()`身分（記帳/投票的寫入動作需要知道是誰），記帳/投票的UI區塊依`enabled_modules`決定要不要渲染。

**Phase 3（LIFF編輯prompt＋手動編輯文件/版本歷史）已完成並部署上線**：`organize.py`的`validate_doc`/`save_doc_revision`跟`topic_doc_revisions`表Phase 1就做好了，Phase 3只是把API端點（`PATCH .../doc`、`GET .../doc/revisions`、`POST .../doc/revisions/{id}/restore`）跟LIFF UI補上，另外新增`PATCH .../prompt`編輯`topics.organize_prompt`。UI設計直接沿用itineraryManager**已經走過兩輪使用者回饋**修正後的版本，不是從頭設計：編輯紀錄一開始就放在頁面最下面（記帳/投票區塊之後）、預設收合、只顯示最近3筆、點開/查看時都有`scrollIntoView`——這些都是itineraryManager實際被使用者抱怨「畫面沒反應」「太礙眼」之後才修出來的，這次直接套用，沒有重新踩一次。

**這次自己抓到的bug**（itineraryManager沒有的）：`liff_page.py`一開始寫成`<h1>{name}</h1>` + 渲染後的`content_md`兩者疊在一起，但`content_md`本身一定以`# 主題名稱`開頭（`validate_doc`要求），導致主題名稱在頁面上會顯示兩次。itineraryManager沒有這個問題是因為它從來就沒有另外加`<h1>`，直接讓文件自己的標題顯示。已修正：拿掉多餘的`<h1>`，跟itineraryManager做法一致。

## 公開版發布後新增的功能

**整理prompt範本選單**：`organize.py`新增`TRAVEL_ORGANIZE_PROMPT`（從itineraryManager的`SYSTEM_PROMPT`搬過來，拿掉照片embed跟收回訊息處理這兩條規則——這個專案的訊息收集管線不支援這兩種事件，見下方）跟`PRESETS`dict，`GET /api/prompt-presets`把它暴露出去，LIFF編輯prompt的表單多一個下拉選單，選了就把對應文字填進textarea（使用者還能繼續手動改，不是直接送出）。要加新範本只需要在`PRESETS`裡加一筆。

**確認過的功能缺口**：照片記錄跟收回訊息（unsend）處理，itineraryManager都有、這個專案都還沒做——`messages.py`目前非文字事件直接`return`跳過。這兩個是通用需求（不是旅遊專屬），如果要補，得動到LINE webhook事件處理（新增image event capture、R2上傳、unsend event更新訊息狀態），跟prompt範本是分開的、比較大的功能缺口，還沒排進哪個phase。
