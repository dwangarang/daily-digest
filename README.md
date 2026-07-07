# 🧠 Daily Digest

A personal learning digest that curates, summarizes, and reinforces a stream of ideas and frameworks — delivered to your inbox every morning.

**What it does:** Pulls content from RSS feeds, APIs, web pages, and anything you email it (links, PDFs, images) → analyzes each piece via Claude → sends a short morning email: a couple of thematically connected deep reads, an in-depth take on the day's most relevant news events, and a recall check on concepts you chose to remember.

**What it replaces:** Anki, manual newsletter triage, and the guilt of 47 unread tabs.

## Features

- **Thematic curation** — each digest has a connecting thread, not random items
- **News Desk** — the day's few genuinely relevant events, analyzed in depth with web-search enrichment (free sources only; zero events on slow days, never filler)
- **Opt-in spaced repetition** — 👍 an item and it becomes an atomic Q&A card that resurfaces on an interval ladder; grade yourself by reply, retire cards you're done with
- **Reply-to-interact, multi-modal** — reply with commands, links, PDFs, or images; your submissions jump the queue into the next digest
- **Grounded expert lenses** — every expert-lens claim cites a real source (book/memo/talk) or it doesn't ship
- **Topic balancing** — prevents any one topic from dominating across days
- **Evergreen library** — load entire essay collections (Paul Graham, Howard Marks) and serve them over time

## Quick Start (15-20 minutes)

### Prerequisites

- **Python 3.10+** — check with `python3 --version`
- **An Anthropic API key** — get one at [console.anthropic.com](https://console.anthropic.com/settings/keys)
- **A Gmail account for the bot** — create a new one (e.g., `mydigest.bot@gmail.com`)

### Step 1: Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/daily-digest.git
cd daily-digest
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 2: Set up Gmail App Password

The bot sends email from (and reads replies to) a dedicated Gmail account. You need an "App Password" because Gmail blocks regular password login from scripts.

1. Log into your bot Gmail account
2. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
3. Enable **2-Step Verification** (required before you can create app passwords)
4. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
5. Select **Mail** as the app, name it "Daily Digest"
6. Copy the 16-character password (remove spaces)

### Step 3: Configure environment

```bash
cp .env.example .env
```

Open `.env` in any text editor and fill in:

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
GMAIL_ADDRESS=your-digest-bot@gmail.com
GMAIL_APP_PASSWORD=your16charpassword
RECIPIENT_EMAIL=your-personal-email@example.com
```

### Step 4: Configure your digest

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` and customize:

- **Topics and weights** — what categories of content you want, and how much of each
- **Sources** — RSS feeds, APIs, and websites to pull from
- **Interest profile** — a paragraph describing what you care about (used by Claude for relevance scoring)
- **Schedule** — how many items per digest, your timezone

### Step 5: Test it

```bash
# Dry run: fetches content, processes it, generates email, but doesn't send
python main.py test
```

This creates a preview HTML file at `data/preview.html`. Open it in your browser to see what the digest looks like.

### Step 6: Send your first real digest

```bash
python main.py digest
```

### Step 7: Schedule daily runs (Mac)

Create a cron job that runs the digest every morning:

```bash
crontab -e
```

Add this line (adjust the time — this runs at 7:00 AM):

```
0 7 * * * cd /path/to/daily-digest && /path/to/daily-digest/venv/bin/python main.py digest >> /path/to/daily-digest/data/cron.log 2>&1
```

To find your exact paths, run:

```bash
echo "cd $(pwd) && $(pwd)/venv/bin/python main.py digest"
```

### Step 8: Or run it on GitHub Actions instead (optional)

If you don't want the pipeline depending on your laptop being awake, `.github/workflows/digest.yml` runs it on GitHub's infrastructure instead, for free.

1. **Fork this repo.**
2. In your fork's Settings → Secrets and variables → Actions, add these repository secrets:
   - `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL` — same values as your `.env`
   - `CONFIG_YAML` — the full contents of your `config.yaml` (it's gitignored, so it has to be injected this way rather than committed)
   - `DATA_REPO_TOKEN` — see next step
3. **Create a second, private repo** (e.g. `daily-digest-data`) to hold your `data/digest.db`. The public repo never commits your reading history or personal data — only this private repo does. Seed it with an initial `digest.db` (even an empty one works; `main.py` will initialize the schema on first run).
4. Generate a fine-grained personal access token scoped to just that private repo with read/write access to contents, and save it as the `DATA_REPO_TOKEN` secret in step 2.
5. That's it — the workflow runs hourly and only actually executes the pipeline when it's your configured `send_time` in your configured `schedule.timezone` (see `scripts/check_schedule.py`). Traveling? Just update `schedule.timezone` in your `config.yaml` and re-paste it into the `CONFIG_YAML` secret — no code or cron change needed to keep the digest landing in your local morning.
6. To test without waiting for the schedule, trigger the workflow manually from the Actions tab with the "force" input checked.

## Usage

### Run modes

| Command | What it does |
|---------|-------------|
| `python main.py digest` | Full pipeline: ingest → process → curate → send |
| `python main.py ingest` | Only fetch new content from sources |
| `python main.py process` | Only process unprocessed articles through Claude |
| `python main.py test` | Full pipeline but saves email to file instead of sending |
| `python main.py replies` | Only check inbox for feedback replies |
| `python main.py sweep` | Process ALL unprocessed articles (useful right after initial setup) |
| `python main.py add-concept` | Interactively add something you learned elsewhere (a lecture, a conversation) into the digest pool and spaced-repetition queue |
| `python main.py add-file path/to/file.pdf` | Ingest a PDF or text file (a memo, a transcript) the same way |

### Replying to digests

Reply to any digest email with:

| Command | Example | Effect |
|---------|---------|--------|
| 👍 an item | `more item 2` | Boosts similar content **and enrolls the concept as a recall card** |
| 👎 an item | `less item 2` | Reduces similar content |
| Save an item | `save item 2` | Re-surfaces item 2 in a future digest |
| Explore deeper | `explore item 2` | Emails back a deep-dive prompt to paste into claude.ai |
| Grade a recall card | `recall 1 missed` | Resets that card to the shortest interval (`recall 1 got it` confirms; silence counts as a pass) |
| Retire a recall card | `stop recall 1` | Removes it from the review queue permanently |
| Topic adjustment | `less crypto` or `more GTM` | Adjusts topic weights |
| Add a URL | `add https://example.com/article` or just paste a link | Fetched, analyzed, and **guaranteed a slot in your next digest** |
| Add a concept | `concept: OODA loops \| my explanation \| General Interest` | Processed and enrolled for recall |

You can also **attach files** — a PDF, an image (chart, slide, article screenshot), or a text file. Attachments are extracted (PDFs via text extraction, images via Claude vision), analyzed, and prioritized into your next digest, same as links. Forwarding an email to the bot works too; matching is by sender, not subject line.

### Adding sources

Edit `config.yaml` and add entries under `sources`:

```yaml
# RSS feed
- name: "Benedict Evans"
  type: "rss"
  url: "https://www.ben-evans.com/benedictevans?format=rss"
  topics: ["AI & Machine Learning", "GTM & Product Strategy"]

# Hacker News
- name: "Hacker News"
  type: "api"
  api_name: "hackernews"
  max_items: 10
  topics: ["AI & Machine Learning", "General Interest"]

# A single web page
- name: "Interesting Article"
  type: "scrape"
  url: "https://example.com/some-article"
  topics: ["General Interest"]

# An essay collection (served one at a time over days)
- name: "Paul Graham Essays"
  type: "evergreen"
  url: "https://paulgraham.com/articles.html"
  topics: ["General Interest", "Investing & Mental Models"]
```

### Expert lenses

Add named analytical lenses under `experts` in `config.yaml` — the most relevant one gets applied to each digest article, matched by domain overlap with the article's tags. Because these claims are attributed to real people, every rendered lens must cite the specific source (book chapter, memo, talk) where the expert articulated the concept, at high confidence — otherwise it's dropped and the section simply doesn't appear (tune via the `expert_lens` config block):

```yaml
experts:
  - name: "Ben Thompson"
    lens_key: "Aggregation Theory — control demand, suppliers follow"
    framework: "Aggregation Theory: platforms that control demand aggregate suppliers without owning them..."
    domains: ["AI & Machine Learning", "GTM & Product Strategy"]
```

Write `framework` as their specific documented mental model, not generic wisdom — the analysis is only as sharp as what you put in. Experts are config-only until you run:

```bash
python scripts/sync_experts.py
```

which pushes them through the concept pipeline so they also show up in your spaced-repetition review queue, not just as a digest-time lens.

You can also mark any source `role: "lens"` (see the comment above the `sources` block in `config.example.yaml`) to have it supply a connecting framework across the digest instead of competing for one of the content slots — this is how the Paul Graham essays source works by default.

## Project Structure

```
daily-digest/
├── .env                  ← Your secrets (never committed)
├── config.yaml           ← Your preferences (never committed)
├── main.py               ← Orchestrator: runs the pipeline
├── sources/              ← Content fetching
│   ├── rss.py            ← RSS/Atom feed reader
│   ├── api.py            ← Hacker News, Reddit APIs
│   └── scraper.py        ← Web scraping + evergreen libraries
├── processing/           ← LLM-powered analysis
│   ├── summarizer.py     ← Summarize, tag, score via Claude
│   ├── curator.py        ← Select thematically connected items
│   └── repetition.py     ← Spaced repetition scheduling
├── delivery/             ← Email output
│   ├── template.html     ← Email layout (Jinja2)
│   ├── sender.py         ← Gmail SMTP sending
│   └── reply_parser.py   ← Parse feedback from email replies
├── scripts/              ← One-off / maintenance scripts (expert sync, PDF backfills, cron gate)
├── .github/workflows/    ← Optional GitHub Actions scheduler (see Step 8)
└── data/                 ← Local state (never committed)
    ├── db.py             ← Database schema and helpers
    └── digest.db         ← SQLite database (auto-created)
```

## Costs

- **Claude API:** ~$0.10-0.30/day depending on article count (Sonnet is cost-efficient)
- **Gmail:** Free
- **Hosting:** Free if you run it on GitHub Actions (see Step 8) — no server to pay for or maintain. If you'd rather self-host on a always-on machine instead, budget ~$4-6/month for the cheapest tier of any VPS provider (Hetzner, DigitalOcean, Vultr); this workload is I/O-bound, not compute-heavy, so the smallest tier is enough.

## Security

- All secrets stored in `.env` (gitignored)
- Personal config stored in `config.yaml` (gitignored)
- Database stored in `data/` (gitignored)
- Pre-commit hook scans for accidentally committed secrets (gitleaks)
- To set up secret scanning: `pip install pre-commit && pre-commit install`

## License

MIT
