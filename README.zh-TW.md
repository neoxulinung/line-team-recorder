# Line Team Recorder — 通用型LINE群組記錄Bot

[English](./README.md)

一個LINE Bot，會在「主題」進行中被動讀取群組對話，用LLM維護一份即時更新的markdown文件，並透過LIFF網頁呈現，內建可選的記帳分攤跟投票模組。這是姊妹專案
[itineraryManager](https://github.com/neoxulinung/itinerary-manager)（旅行規劃專用版）的通用化重寫——這裡的「主題」可以是一趟旅行、讀書會進度、裝修進度、專案追蹤，或任何群組想記錄的事情。要整理成什麼結構、抓什麼重點，是每個主題各自可客製的prompt，不是寫死的文件格式。

設計對象是2-10人的朋友小團體或工作團隊——不是多租戶SaaS產品。完整的設計脈絡跟開發過程中踩過的坑，可以參考[`docs/user_stories.md`](docs/user_stories.md)跟[`docs/plan.md`](docs/plan.md)。

## 功能

- **被動記錄**：主題進行中，群組裡的每則文字訊息都會被記錄下來。其他時間Bot完全不插話，除非有指令觸發。
- **LLM整理文件，每個主題可客製**：定時（也可以用`/整理`手動觸發）讓LLM把新訊息整理進這個主題自己的markdown文件，依照該主題的`organize_prompt`（可在LIFF頁面編輯）。留預設的通用prompt就是簡單的重點摘要；設一份詳細的（例如比照itineraryManager「時間軸／未定事項／其他資訊」＋來源引用）就能重現類似的整理效果。
- **有根據的問答**：`/問 <問題>`只根據這個主題的文件回答，不會用模型自己的知識瞎猜，文件裡沒答案就明講不知道。
- **查核機制**：每次整理完都會跑第二輪LLM檢查，找出有沒有加進沒根據的內容，用`/檢查`查詢。
- **LIFF頁面**：一個可分享的網頁，顯示目前文件（可手動編輯、有完整版本歷史），如果該主題開啟了記帳/投票模組也會一併顯示。
- **可選模組，各主題自己決定要不要開**：記帳分攤（`/記帳`、`/結算`）跟投票（`/投票`）預設關閉——開始主題時決定要不要開，中途不能加開（要開別的模組請開新主題）。之後要加新模組不需要改資料庫schema，見下方「新增模組」。

在群組裡下`/說明`可以看完整指令列表。

## 使用方式

所有指令都是直接在LINE群組裡打繁體中文的斜線指令。

**📋 主題管理**

| 指令 | 說明 | 範例 |
| --- | --- | --- |
| `/開始 <名稱> [記帳] [投票]` | 開始一個主題，從此刻起群組裡的每則訊息都會被記錄。後面接的`記帳`／`投票`關鍵字（位置、組合不限）決定要不要開啟對應的可選模組——都不接就只跑核心的整理／問答功能。 | `/開始 讀書會進度 記帳` → 開始記錄，記帳模組開、投票模組關 |
| `/結束` | 結束目前主題：先把還沒整理的訊息整理進文件，如果有開記帳模組再附上最終結算。只有開始的人能結束。 | `/結束` → `🏁 已結束`＋結算（如果適用） |

**🔍 查詢（唯讀，隨時可用）**

| 指令 | 說明 |
| --- | --- |
| `/問 <問題>` | 只根據這個主題的文件回答，不會瞎猜。 |
| `/檢查` | 查看查核機制有沒有抓到跟原始訊息對不上的可疑內容。 |
| `/整理` | 手動觸發整理（平常每小時也會自動跑一次）。 |

**💰 記帳**（只有該主題開了記帳模組才能用）

| 指令 | 說明 |
| --- | --- |
| `/記帳 <金額> <說明> [@人...]` | 記錄誰代墊了多少。沒@人的話，分攤對象是這個主題期間發言過的所有人。 |
| `/結算` | 顯示目前結算狀況，不會結束主題。 |

編輯或刪除帳目只能在LIFF頁面操作。

**🗳️ 投票**（只有該主題開了投票模組才能用）

| 指令 | 說明 |
| --- | --- |
| `/投票 開始 <題目>` | 開一個新投票（同一個主題同時只能有一個進行中）。 |
| `/投票 新增 <選項>` | 加入候選項目。 |
| `/投票 結果` | 查看目前得票狀況跟每個人投了什麼。 |
| `/投票 結束` | 鎖定結果並顯示最終票數。 |

實際投票／取消投票只能在LIFF頁面操作，可複選、點一下切換。

## 部署前該知道的設計決策

- **所有回覆都靠指令觸發。** Bot唯一會主動做的事只有主題進行中被動記錄訊息。
- **LIFF頁面沒有存取控制。** 有連結的人都能看、都能編輯。這符合小型信任團體的模型，不適合有不受信任成員的場合。
- **同一個群組同時只能有一個進行中的主題。** 用`/開始`開始新的，用`/結束`結束目前的（只有開始的人能結束——這個專案沒有像itineraryManager的`ADMIN_USER_ID`那種管理員覆寫機制）。
- **模組在開始主題時就鎖定。** `記帳`／`投票`中途不能加開，需要不同組合就開新主題。
- **目前只有繁體中文介面**，所有指令、回覆、LIFF介面文字都是寫死的繁體中文。
- **只記錄文字訊息。** 跟itineraryManager不同，這個專案目前沒有照片記錄跟R2儲存（Phase 1的範圍決定）——之後要加不需要動到訊息表的schema。

### 新增模組

`topics.enabled_modules`是單一逗號分隔字串欄位（例如`"記帳,投票"`），不是每個模組各開一個布林欄位；模組清單本身集中定義在[`src/modules.py`](src/modules.py)的一個dict裡（`MODULES = {"記帳": {...}, "投票": {...}}`），`/開始`的關鍵字解析跟每個指令執行前的「這個模組有沒有開」檢查都從這裡取得。要加新的可選模組，只需要在這個dict跟對應的指令處理邏輯裡加一筆，不需要migration，也不需要額外的框架。

## 架構

- **Google Cloud Run**（Python/FastAPI）撐起整個後端——一個真正長駐的container process，不是有短暫硬性timeout的FaaS沙箱。特意選這個平台是為了離開Cloudflare Workers那個約30秒、無法完全catch的執行上限（itineraryManager自己的`docs/plan.md`記錄了這帶來過多少開發痛苦）。每個webhook事件都會在回應LINE之前完整同步處理完——下面有說明為什麼這點很重要。
- **Cloudflare D1**（SQLite）存所有關聯式資料，透過它的[REST API](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/)存取，不是Workers binding，因為Cloud Run不是Workers執行環境。
- **Cloud Scheduler**每小時觸發一次整理／查核掃描，打一個用共享密鑰保護的內部端點（`/internal/scheduled-organize`）。
- **Anthropic或OpenAI API**負責LLM呼叫（整理／問答／查核），實際用哪個看`src/llm_client.py`的模型常數設定。
- **LINE Messaging API**負責Bot本體，另外需要一個獨立的**LINE Login** channel給LIFF app用。

**如果要把這個架構搬到別的FaaS/container平台，有一個平台專屬的坑值得知道**：這個webhook handler早期版本用FastAPI的`BackgroundTasks`先回應LINE、再背景處理訊息——照抄itineraryManager的Cloudflare `ctx.waitUntil()`模式。但在Cloud Run上，如果instance在背景工作跑完之前因為縮編被回收，那個背景task會**直接被砍掉，不會拋例外、也不會留下任何log**。修法是完全拿掉`BackgroundTasks`，改成每個事件同步處理完才回應——這其實才是正確運用Cloud Run寬鬆timeout的方式，不是workaround。不要假設某個平台上「先回應、之後背景處理」的模式可以原封不動搬到另一個平台，每個平台對背景工作到底有什麼保證都要重新確認。

## 費用

以朋友團體的用量來說，現實上每月遠低於5美金：

- **Cloud Run**：免費額度涵蓋每月200萬次請求、18萬vCPU-秒——一個主題的webhook＋每小時cron流量遠遠碰不到。
- **Cloudflare D1**：免費額度（5GB儲存空間、每天500萬次讀取＋10萬次寫入）——跟itineraryManager一樣，綽綽有餘。
- **Cloud Scheduler**：免費額度涵蓋3個job；這個專案只用1個。
- **LINE Messaging API**：0元——所有回覆都是reply訊息（LINE不收費、不計入額度），Bot完全不會主動發送push訊息。
- **LLM API呼叫**：唯一真正會花錢的地方，按token計費，實際費率看`src/llm_client.py`指向哪個provider/model。整理是批次處理、有上限，只有真的有新內容待整理時才會呼叫。

## 建置步驟

需要準備：已啟用計費的GCP帳號、Cloudflare帳號、LINE Developers帳號、`gcloud`、Anthropic或OpenAI其中一個的API key。

### 1. LINE Messaging API channel（Bot本體）

步驟跟itineraryManager一樣——參考[它的README](https://github.com/neoxulinung/itinerary-manager#建置步驟)的「LINE Messaging API channel」段落（建立channel、記下channel secret＋access token、在manager.line.biz開啟「允許加入群組聊天」）。webhook網址先留白，等下方步驟4再回來設定。

### 2. LINE Login channel（給LIFF app用）

跟itineraryManager一樣的做法——建立一個獨立的LINE Login channel，底下加一個LIFF app，endpoint設成`https://<你的Cloud Run網址>/liff`，scope選`profile`，記下LIFF ID，並發布這個channel。

### 3. Cloudflare D1

```sh
npx wrangler login   # 如果還沒登入過
npx wrangler d1 create line-team-recorder-db   # 記下印出來的database_id
npx wrangler d1 execute line-team-recorder-db --remote --file schema.sql
```

接著建立一個Cloudflare API token（dashboard → My Profile → API Tokens →「Create Token」，權限給`D1:Edit`、範圍限定在你的帳號）——因為這個專案跑在Cloud Run而不是Workers，用不了Workers的D1 binding，所以改用REST API存取D1。記下你的帳號ID（dashboard側邊欄）跟上面的database ID。

### 4. GCP專案與Cloud Run

```sh
gcloud projects create line-team-recorder
gcloud billing projects link line-team-recorder --billing-account=<你的billing帳號ID>
gcloud config set project line-team-recorder
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com
```

把`.env.example`複製成`.env`，填入所有值（LINE channel secret/token、LLM API key、步驟3拿到的Cloudflare帳號ID／database ID／API token、步驟2的LIFF ID，以及一個隨機產生的`SCHEDULER_SECRET`，例如`openssl rand -hex 32`）。

Deploy，把`.env`的內容轉成環境變數傳進去（不要把包含密鑰的`--set-env-vars`清單以明文留在shell history裡——直接從檔案pipe進去）：

```sh
gcloud run deploy line-team-recorder \
  --source . \
  --region asia-east1 \
  --allow-unauthenticated \
  --env-vars-file <(awk -F= '!/^#/ && NF {print $1": \""$2"\""}' .env)
```

記下印出來的service網址。把LINE Messaging API channel的webhook網址設成`https://<那個網址>/webhook`，並在LINE Developers Console驗證。

### 5. Cloud Scheduler（每小時整理）

```sh
gcloud scheduler jobs create http line-team-recorder-organize \
  --location asia-east1 \
  --schedule "0 * * * *" \
  --uri "https://<你的Cloud Run網址>/internal/scheduled-organize" \
  --http-method POST \
  --headers "x-scheduler-secret=$(grep ^SCHEDULER_SECRET= .env | cut -d= -f2-)"
```

把Bot加進LINE群組，下`/開始 <名稱>`，應該就會開始記錄了。

### 本機開發

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd src && uvicorn main:app --reload --port 8080
```

本機開發連的是跟正式環境同一個遠端D1（透過REST API）跟同一個LLM provider，不需要另外架本機資料庫。

## License

[MIT](./LICENSE)
