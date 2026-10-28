# NewsDesk: RSS to Discord forum threads

NewsDesk reads your OPML feed list every 10 minutes and removes duplicates. It groups coverage of the same story from different outlets into one Discord forum thread, tags it, and writes a summary of the group.

## What you get in Discord
- **One thread per story.** The title shows how many outlets covered it, e.g. `[7] Grand jury declines to indict Cassidy Hutchinson`.
- **Tags:** `Left`, `Center`, `Right` (whichever sides covered it), `Both Sides` (left and right outlets both covered it), `Single Source`, `Satire`, plus one topic (`Politics`, `Elections`, `Courts & Law`, `Economy`, `World`, `Immigration`, `Crime`, `Tech`, `Culture`, `Health`, `Military`). Use the forum's tag filter to pick stories.
- **The first post** has the summary, left framing, right framing, what's in dispute, and every outlet's link sorted into Left, Center, Right and Satire. Outlets marked `($)` may hit a paywall.
- **When more outlets pick up a story**, the thread gets an updated summary and tags plus a "New coverage added" reply, and it moves back to the top of the forum.
- **Duplicates pruned:** repeat links, links with tracking junk, the same outlet re-running a headline (Fox Politics + Fox Latest), and identical posts on sister sites (RedState/PJ Media/Hot Air).
- **Satire is kept apart from news.** It never mixes with real stories and is always labeled SATIRE. Not the Bee counts as right-leaning news, not satire.
- **Daily Top Stories ranking:** a pinned `TOP 10 STORIES - <date>` thread tagged `Daily Ranking`, posted at the times you set (default 7:00 AM and 5:00 PM). Later runs that day refresh the same thread instead of starting a new one. Each story links to its own thread and shows outlet count, left/right split, topic, score, and movement since the last ranking (new, up 2, down 1). A copy is saved to `rankings/YYYY-MM-DD.md` and `.json`.
- **`stories.json`:** every current story group written to a file, ready for the faceless news channel or other tools.

## One-time Discord setup (about 10 minutes)
1. In Discord, go to **User Settings > Advanced** and turn on **Developer Mode**.
2. In your server, create a **Forum** channel, e.g. `#news-desk`. Optionally create a second one, `#satire-desk`.
3. Go to https://discord.com/developers/applications, click **New Application**, open **Bot**, then **Reset Token** and copy the token. No privileged intents are needed.
4. Invite the bot with this link, replacing `YOUR_APP_ID` with the Application ID from **General Information**:
   `https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=326417599504`
   This grants View Channel, Send Messages, Embed Links, Read Message History, Manage Channels (to create the tags), Manage Threads, Create Public Threads and Send Messages in Threads.
5. Right-click each forum channel, choose **Copy Channel ID**, and paste the IDs into `config.toml`. Leave `satire_forum_id` blank to put satire in the news forum.
6. If your forum has its own permission overrides, make sure the bot's role is allowed in that channel too.

## Install on Windows
1. Install Python 3.11 or newer from python.org, and tick "Add Python to PATH".
2. Double-click `setup_windows.bat`. It installs the packages and creates `config.toml`.
3. Edit `config.toml` with your bot token and forum IDs.
4. Run these once in a terminal in this folder:
   - `python newsdesk.py --check` tests the token, forum channels, tags, feeds and AI without posting anything. Every line should say OK (tags will say FAIL until the next step).
   - `python newsdesk.py --setup` creates the tags in your forum(s).
   - `python newsdesk.py --test-post` creates, edits and replies to one test thread. Delete it afterwards.
   - `python newsdesk.py --dry-run` makes no Discord posts. It writes `preview.md` and today's ranking to `rankings/` so you can check the grouping. It uses a separate `newsdesk-dryrun.db`, so previewed stories still get posted later.
5. Double-click `run_newsdesk.bat` to run it. Leave it open, or add it to Task Scheduler "At log on".

The first run posts at most 25 threads (the most-covered stories first) so the forum isn't flooded. After that it posts new stories as they appear. Delete `newsdesk.db` to start fresh. History older than `keep_days` (7) is cleaned up automatically, and a log is kept in `newsdesk.log`.

## How the trending score works
Each story group gets a score from its last 24 hours of coverage:
- **3 points per outlet** covering it (the main signal)
- **+4 if both left and right outlets cover it**
- **+1.5 per outlet that picked it up in the last 6 hours** (momentum, so stories that are still growing rise)
- **+0.5 per article**, capped at 10 (follow-ups and multiple angles)

Satire is ranked separately at the bottom. Run `python newsdesk.py --ranking` any time, e.g. right before you go live, to post a fresh ranking. Change the times in `[ranking]` in `config.toml`.

## Summaries
Set `[summary] backend` in `config.toml`:
| backend | What it uses | Notes |
|---|---|---|
| `extractive` | No AI | Picks the key sentences from the outlets' blurbs. Works immediately. |
| `ollama` | Local AI on your PC | Install Ollama, run `ollama pull llama3.1:8b` (or `qwen2.5:14b` on a 16 GB+ GPU). Free and private. |
| `openai` | Any OpenAI-compatible API | Groq free tier (`https://api.groq.com/openai/v1`, model `openai/gpt-oss-120b`), Google Gemini (`https://generativelanguage.googleapis.com/v1beta/openai`), LM Studio (`http://localhost:1234/v1`), OpenAI, OpenRouter. Set `json_mode = true` for Groq, Gemini and OpenAI. |

AI summaries only run for stories covered by 2 or more outlets (`min_sources`), and at most `max_ai_per_cycle` (12) per cycle to stay inside free-tier limits. Any extra stories get an extractive summary first and are upgraded to an AI summary on the next cycle. Single-source stories use the outlet's own blurb. If the AI service is down, NewsDesk falls back to `extractive` instead of failing. Summaries are written only from the headlines and feed blurbs; the AI is told not to add facts. Still, open the links before you quote anything on air.

## Tuning
- Groups too big or mixing stories: raise `[grouping] similarity` (e.g. 0.35).
- The same story split into several threads: lower it (e.g. 0.25).
- Only want stories with multiple outlets: set `min_sources_to_post = 2`.
- Add feeds by editing `feeds.opml`. Any folder whose name starts with Left, Center, Right or Satire sets the lean, and `[source_overrides]` lets you force one. Put `[PAYWALL]` in a feed's title to show the `($)` marker on its links.
- The included list has 106 feeds: 39 left, 10 center, 39 right (including Not the Bee) and 13 satire, all rated by [AllSides](https://www.allsides.com/media-bias/ratings), plus 5 optional Bluesky feeds that are off by default.
- By default only stories covered by 2+ outlets get a thread (`min_sources_to_post = 2`). With 100+ feeds, most one-outlet stories are noise. Satire always posts. Set it to 1 if you want everything.
- If you already ran `--setup`, run it once more so the new `Center` tag is created.


## Free hosting (so your PC can stay off)
NewsDesk doesn't need to stay connected to Discord. It just wakes up, checks the feeds, posts, and exits. That means it can run for free on **GitHub Actions** as a scheduled job. Everything for this is already in the folder (`.github/workflows/newsdesk.yml` and `config.cloud.toml`).

| Option | Cost | Notes |
|---|---|---|
| **GitHub Actions (recommended)** | Free | Public repo: free and runs every 15 minutes. Private repo: 2,000 free minutes a month, so change the schedule to every 30 minutes. Scheduled runs can start a few minutes late. |
| Oracle Cloud Always Free VM | Free | Always-on Linux server. Needs a card to sign up, and idle free VMs can be reclaimed. Use `deploy/newsdesk.service`. |
| Your PC | Free | `run_newsdesk.bat`, or Task Scheduler "At log on". |

### GitHub Actions setup (about 15 minutes)
1. Create a free account at github.com, then click **New repository**. Name it `newsdesk`. Public gives you every 15 minutes for free. Your token stays secret either way, because it lives in Secrets and not in the code.
2. On the new repo page, click **uploading an existing file** and drag in **everything in this folder**, including the `.github` folder. Don't upload `config.toml`. If the browser skips the `.github` folder, use **Add file > Create new file**, type `.github/workflows/newsdesk.yml` as the name, and paste the file's contents.
3. Go to **Settings > Secrets and variables > Actions > New repository secret** and add:
   - `NEWSDESK_BOT_TOKEN`: your bot token
   - `NEWSDESK_NEWS_FORUM_ID`: news forum channel ID
   - `NEWSDESK_SATIRE_FORUM_ID`: satire forum ID (optional)
   - `NEWSDESK_AI_API_KEY`: optional free Groq key from console.groq.com for AI summaries. Without it, summaries are extractive.
4. Go to the **Actions** tab and enable workflows if GitHub asks. Click **NewsDesk > Run workflow** and run these modes in order: `check`, `setup`, `test-post`. Open each run to read its log.
5. That's it. From now on it runs on its own every 15 minutes. To post a ranking right away, use **Run workflow > ranking**.

The database is saved between runs on a branch called `state`, which also holds `stories.json` and the daily `rankings/`. Don't delete that branch unless you want to start over.

Keep in mind: GitHub pauses scheduled workflows in public repos after 60 days without repository activity. If you get that email, open the Actions tab and click **Enable workflow**, or make any small edit to the repo.

### Oracle / any Linux server
```
sudo apt install -y python3-venv git
cd ~ && mkdir newsdesk && cd newsdesk   # copy the files here
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml && nano config.toml
.venv/bin/python newsdesk.py --check && .venv/bin/python newsdesk.py --setup
sudo cp deploy/newsdesk.service /etc/systemd/system/ && sudo systemctl enable --now newsdesk
```

## Uploading to GitHub safely

Upload the files from a fresh copy of `newsdesk.zip`, not your working folder. GitHub's web upload ignores `.gitignore`, so never upload `config.toml`, `newsdesk.log`, `*.db`, `preview.md`, `stories.json` or `rankings/`. If GitHub's secret scanning warns you, click Cancel, never Allow Secret. The bot now hides tokens in all log output, but older log files may still contain one, so delete them.

## Troubleshooting
| Message | Fix |
|---|---|
| `401` / "rejected the bot token" | Reset the token in the Developer Portal and paste it again with no spaces or quotes. On GitHub, update the secret. |
| `403` / "Missing Permissions" | Open the forum's **Edit Channel > Permissions** and give the bot's role View Channel, Send Messages, Send Messages in Threads, Create Posts, Manage Threads, Embed Links and Read Message History. `--setup` also needs Manage Channels. |
| `404` / "Unknown Channel" | The forum ID is wrong, or the bot isn't in that server. Copy the ID again with Developer Mode on. |
| "is not a Forum channel" | You copied a text channel's ID. Create a channel of type **Forum**. |
| Missing tags | Run `--setup` (or the `setup` mode on GitHub). |
| A feed keeps failing | That outlet's site is slow or down. NewsDesk skips it and tries again next cycle. Remove it from `feeds.opml` if it never works. |
| Wrong ranking times | Set `timezone = "America/Chicago"` in `[general]`. On Windows, run `pip install tzdata` once. |
| A deleted thread | NewsDesk creates a new one the next time that story gets coverage. |
| Summaries say "extractive" with AI turned on | The AI was down, hit a rate limit, or no key is set. NewsDesk falls back so posting never stops. |

## Tests
`pip install pytest` and then `python -m pytest tests`. The tests run against a fake Discord server and fake feeds, so they never post to your real server.
