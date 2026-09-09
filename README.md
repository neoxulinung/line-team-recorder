# Line Team Recorder — a generic LINE group-chat recorder bot

[繁體中文](./README.zh-TW.md)

A LINE bot that passively reads a group chat while a "topic" is active, uses an LLM to
maintain a living markdown document of what's been discussed, and exposes it through a LIFF
web page with optional expense-splitting and voting modules. It's a generalized rewrite of a
sister project, [itineraryManager](https://github.com/neoxulinung/itinerary-manager) (a
travel-planning-only version of the same idea) — here, a "topic" can be a trip, a book club,
a renovation, a project, or anything else a group wants to track. What gets extracted and how
it's organized is a per-topic customizable prompt, not a fixed document structure.

Built for a small group of friends or a team (2–10 people) — not a multi-tenant SaaS product.
See [`docs/user_stories.md`](docs/user_stories.md) and [`docs/plan.md`](docs/plan.md) for the
full design rationale and a running log of real bugs found and fixed along the way.

## What it does

- **Passive capture**: while a topic is active, every text and photo message sent in the LINE
  group is recorded. The bot stays silent otherwise — it never speaks unless a command asks it
  to.
- **LLM-organized doc, custom per topic**: on a schedule (and on demand via `/整理`), an LLM
  folds newly captured messages into a single markdown document per topic, following that
  topic's own `organize_prompt` (editable via the LIFF page, with a 通用/旅遊規劃 preset
  dropdown to start from). Leave the default generic prompt for a simple running summary, or
  set a detailed one (e.g. "timeline / open questions / everything else" with source
  citations) for something closer to itineraryManager's format.
- **Photos and message recall**: an image sent while a topic is active is saved to Cloudflare
  R2 and embedded into the doc via markdown; if someone recalls ("unsend") a message, the next
  organize pass removes or corrects anything the doc already derived from it.
- **Q&A with grounding**: `/問 <question>` answers from the topic's document only, never from
  the model's own knowledge, and says it doesn't know when the doc doesn't have an answer.
- **Fact-check pass**: a second LLM pass, run after every organize, flags anything added that
  doesn't actually trace back to a real message — queryable via `/檢查`.
- **Per-topic model choice**: the LIFF page lets you pick which model (Claude Sonnet/Haiku,
  GPT-5, GPT-5 mini) handles organizing/Q&A/fact-checking for that specific topic — leave it on
  "use default" and it follows whatever `src/llm_client.py`'s constants are set to.
- **LIFF page**: a shareable web page rendering the current doc (with manual-edit and full
  version history), plus expenses/poll if those modules are enabled for the topic. `/懶人包`
  posts a button straight to it; `/開始` also nudges the group to add the bot as a friend
  (needed for real display names — see "Design choices" below) with a one-tap link.
- **Optional modules, opt in per topic**: expense-splitting (`/記帳`, `/結算`) and voting
  (`/投票`) are off by default — turn them on when starting a topic and they can't be added
  mid-topic (start a new topic instead). Adding a future module doesn't require a schema
  change; see "Adding a module" below.

Run `/說明` in the group chat for the full command list.

## Usage

All commands are Traditional Chinese slash-commands, typed directly in the LINE group.

**📋 Topic lifecycle**

| Command | What it does | Example |
| --- | --- | --- |
| `/開始 <name> [記帳] [投票]` | Starts a topic. From here on, every message in the group is recorded. Trailing `記帳`/`投票` keywords (in any position, any combination) turn on those optional modules for this topic only — omit both to run with just the core doc/Q&A features. | `/開始 讀書會進度 記帳` → recording starts, 記帳 module on, 投票 off |
| `/結束` | Ends the current topic: folds in anything not yet organized, then appends the final expense settlement if 記帳 was enabled. Only the person who started it can end it. | `/結束` → `🏁 已結束` + settlement, if applicable |

**🔍 Query (read-only, work anytime)**

| Command | What it does |
| --- | --- |
| `/問 <question>` | Answers strictly from the topic's document — never guesses. |
| `/檢查` | Shows anything the fact-check pass flagged as unsupported by the source messages. |
| `/整理` | Manually triggers the organize step (also runs hourly on its own). |
| `/懶人包` | Posts a button linking straight to the topic's LIFF page. |

**💰 Expenses** (only if 記帳 was enabled for this topic)

| Command | What it does |
| --- | --- |
| `/記帳 <amount> <description> [@person...]` | Records who paid. No @-mentions → splits across everyone who has spoken during this topic. |
| `/結算` | Shows the current settlement without ending the topic. |

Editing/deleting an expense is LIFF-only.

**🗳️ Voting** (only if 投票 was enabled for this topic)

| Command | What it does |
| --- | --- |
| `/投票 開始 <question>` | Starts a poll (one active poll per topic at a time). |
| `/投票 新增 <option>` | Adds a candidate option. |
| `/投票 結果` | Shows current vote counts and who voted for what. |
| `/投票 結束` | Locks the poll and shows the final result. |

Casting/changing a vote is LIFF-only, multi-select, toggle on tap.

## Design choices worth knowing before you deploy this

- **All bot replies are command-triggered.** The only thing the bot does without being asked
  is capture messages while a topic is active.
- **The LIFF page has no access control.** Anyone with the link can view and edit it. This
  matches a small trusted group's threat model — it is not meant for untrusted participants.
- **One topic active per group at a time.** Start a new one with `/開始`, close the current
  one with `/結束` (only the person who started it can end it — there is no admin override in
  this project, unlike itineraryManager's `ADMIN_USER_ID`).
- **Modules are locked in at topic start.** `記帳`/`投票` can't be toggled mid-topic; start a
  new topic if you need a different combination.
- **Traditional Chinese only**, currently — all commands, replies, and the LIFF UI.
- **Friending the bot matters.** LINE's profile API only resolves a real display name for
  people who've added the bot as a friend (`/開始` nudges for this) — anyone who hasn't shows
  up as a raw LINE user ID instead. This self-heals automatically for anything organized
  *after* they friend the bot; text already written into the doc before that needs the LIFF
  page's 🔄 修正名稱顯示 button (or another `/整理` pass, for messages not yet organized).

### Adding a module

`topics.enabled_modules` is a single comma-separated column (e.g. `"記帳,投票"`), not one
boolean column per module, and the module list itself lives in [`src/modules.py`](src/modules.py)
as a plain dict (`MODULES = {"記帳": {...}, "投票": {...}}`) that drives both `/開始`'s keyword
parsing and each command's enabled-check. Adding a new optional module means adding one entry
to that dict and its command handlers — no migration, no framework.

## Architecture

- **Google Cloud Run** (Python/FastAPI) for the whole backend — a real long-lived container
  process, not a FaaS sandbox with a short hard timeout. Chosen specifically to move away from
  Cloudflare Workers' ~30-second, uncatchable execution ceiling (see itineraryManager's own
  `docs/plan.md` for what that cost in development pain). Every webhook event is processed
  fully synchronously before the HTTP response is sent back to LINE — see the note below on
  why this matters.
- **Cloudflare D1** (SQLite) for everything relational, accessed via its
  [REST API](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/)
  rather than a Workers binding, since Cloud Run isn't a Workers runtime.
- **Cloudflare R2** for photo storage, accessed through its S3-compatible API via `boto3`
  (`src/r2.py`, sync client run through `asyncio.to_thread` since R2 has no first-party async
  client) — same reasoning as D1 above, no Workers binding available outside a Worker.
- **Cloud Scheduler** triggers an hourly organize/fact-check sweep over a signed internal
  endpoint (`/internal/scheduled-organize`, guarded by a shared-secret header).
- **Anthropic or OpenAI API** for the LLM calls (organize / Q&A / fact-check), whichever
  `src/llm_client.py`'s model constants are pointed at.
- **LINE Messaging API** for the bot itself, plus a separate **LINE Login** channel for the
  LIFF app.

**A platform-specific bug worth knowing if you're adapting this to another FaaS/container
platform**: an earlier version of this webhook handler used FastAPI's `BackgroundTasks` to
ack the LINE webhook fast and process the message afterward — mirroring itineraryManager's
Cloudflare `ctx.waitUntil()` pattern. On Cloud Run, that background task can be **silently
killed with no exception and no log** if the instance scales down between the response being
sent and the task finishing. The fix was to drop `BackgroundTasks` entirely and process each
event synchronously — which is the correct use of Cloud Run's generous timeout anyway, not a
workaround. Don't assume a "fire and forget after response" pattern that worked on one
platform carries over to another; check what guarantee (if any) that platform actually makes.

## Cost

Realistically well under $5/month at friend-group scale:

- **Cloud Run**: free tier covers 2M requests/month and 180k vCPU-seconds/month. A topic's
  webhook + hourly-cron traffic doesn't come close.
- **Cloudflare D1**: free tier (5GB storage, 5M reads + 100k writes/day) — same as
  itineraryManager, easily enough.
- **Cloudflare R2**: free tier (10GB storage, no egress fees) — a friend group's photo volume
  doesn't come close.
- **Cloud Scheduler**: free tier covers 3 jobs; this project uses 1.
- **LINE Messaging API**: $0 — every reply is a reply message (LINE doesn't charge for or
  count those against any quota); the bot never sends push messages.
- **LLM API calls**: the one real cost, billed per token to whichever provider/model
  `src/llm_client.py` is pointed at. Organizing runs in capped batches, only when there's
  something new to fold in.

## Setup

You'll need: a GCP account with billing enabled, a Cloudflare account, a LINE Developers
account, `gcloud`, and either an Anthropic or OpenAI API key.

### 1. LINE Messaging API channel (the bot itself)

Same as itineraryManager's setup — see the "LINE Messaging API channel" section of
[its README](https://github.com/neoxulinung/itinerary-manager#setup)
for the exact steps (create channel, note channel secret + access token, enable "allow bot to
join group chats" in manager.line.biz). Leave the webhook URL blank until step 4 below.

### 2. LINE Login channel (for the LIFF app)

Same idea as itineraryManager — create a separate LINE Login channel, add a LIFF app under it
with endpoint `https://<your-cloud-run-url>/liff`, scope `profile`, note the LIFF ID, and
publish the channel.

### 3. Cloudflare D1 and R2

```sh
npx wrangler login   # if you haven't already
npx wrangler d1 create line-team-recorder-db   # note the database_id it prints
npx wrangler d1 execute line-team-recorder-db --remote --file schema.sql
npx wrangler r2 bucket create <a-globally-unique-bucket-name>
```

Then create a Cloudflare API token (dashboard → My Profile → API Tokens → "Create Token",
permission `D1:Edit` scoped to your account) — the D1 binding used by Cloudflare Workers isn't
available here since this project runs on Cloud Run, so D1 is reached over its REST API
instead. Note your account ID (dashboard sidebar) and the database ID from above.

Separately, create an R2 API token (dashboard → R2 → Manage R2 API Tokens → "Create API
token", permission "Object Read & Write", scoped to the bucket you just created) for
`R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY` — this is a different kind of token than the D1 one
above. Enable public access on the bucket (dashboard → your bucket → Settings → Public access)
and note the `pub-*.r2.dev` URL it gives you for `R2_PUBLIC_BASE_URL`.

### 4. GCP project and Cloud Run

```sh
gcloud projects create line-team-recorder
gcloud billing projects link line-team-recorder --billing-account=<your-billing-account-id>
gcloud config set project line-team-recorder
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com
```

Copy `.env.example` to `.env` and fill in every value (LINE channel secret/token, LLM API
key(s), the Cloudflare account/D1 database ID/D1 API token/R2 keys and bucket info from step 3,
the LIFF ID from step 2, and a random `SCHEDULER_SECRET`, e.g. `openssl rand -hex 32`).

Deploy, passing `.env`'s contents as environment variables (do **not** commit a
`--set-env-vars` value list containing secrets to shell history in plain text — pipe it from
the file):

```sh
gcloud run deploy line-team-recorder \
  --source . \
  --region asia-east1 \
  --allow-unauthenticated \
  --env-vars-file <(awk -F= '!/^#/ && NF {print $1": \""$2"\""}' .env)
```

Note the service URL it prints. Set the LINE Messaging API channel's webhook URL to
`https://<that-url>/webhook` and verify it in the LINE Developers Console.

### 5. Cloud Scheduler (hourly organize)

```sh
gcloud scheduler jobs create http line-team-recorder-organize \
  --location asia-east1 \
  --schedule "0 * * * *" \
  --uri "https://<your-cloud-run-url>/internal/scheduled-organize" \
  --http-method POST \
  --headers "x-scheduler-secret=$(grep ^SCHEDULER_SECRET= .env | cut -d= -f2-)"
```

Add the bot to a LINE group and run `/開始 <name>` — it should start recording.

### Local development

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd src && uvicorn main:app --reload --port 8080
```

Local runs hit the same remote D1 (via REST API) and remote LLM provider as production —
there's no local database to spin up separately.

## License

[MIT](./LICENSE)
