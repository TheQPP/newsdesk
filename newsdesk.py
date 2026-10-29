#!/usr/bin/env python3
"""
NewsDesk - RSS -> Discord forum threads, with de-duplication, story grouping,
bias tags, and per-group summaries.

  python newsdesk.py --check      test token, forum, tags, feeds and AI without posting
  python newsdesk.py --setup      create the forum tags in Discord (run once)
  python newsdesk.py --test-post  make one test thread to confirm the bot's permissions
  python newsdesk.py --once       one poll cycle, then exit
  python newsdesk.py              run forever (polls every poll_minutes)
  python newsdesk.py --dry-run    no Discord; writes preview.md and rankings/<date>.md
  python newsdesk.py --ranking    post today's Top Stories ranking right now

Requires Python 3.11+ and:  pip install feedparser requests
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.exit("NewsDesk needs Python 3.11 or newer (python.org). You have " + sys.version.split()[0])

import argparse
import calendar
import hashlib
import html
import json
import logging
import math
import re
import os
import sqlite3
import time
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

log = logging.getLogger("newsdesk")
HERE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewsDesk/1.0 (+RSS reader)"

LEAN_NAMES = {"left": "Left", "center": "Center", "right": "Right", "satire": "Satire"}
LEAN_ORDER = ("left", "center", "right", "satire")
BASE_TAGS = ["Left", "Center", "Right", "Both Sides", "Single Source", "Satire", "Daily Ranking"]
TOPICS = ["Politics", "Elections", "Courts & Law", "Economy", "World",
          "Immigration", "Crime", "Tech", "Culture", "Health", "Military"]
TOPIC_KEYWORDS = {
    "Elections": "election elections ballot ballots voter voters voting poll polls polling primary midterm midterms campaign candidate nominee gop dnc rnc electoral",
    "Courts & Law": "court courts judge judges justice supreme lawsuit sued sue ruling ruled indictment indicted trial jury attorney prosecutor doj subpoena appeals scotus",
    "Economy": "economy economic inflation tariff tariffs jobs unemployment fed rates market markets stocks prices tax taxes budget deficit debt gdp trade wages",
    "World": "ukraine russia china israel gaza iran nato europe mexico canada venezuela korea taiwan india un foreign putin xi netanyahu zelensky",
    "Immigration": "immigration immigrant immigrants migrant migrants border ice deportation deportations asylum visa daca",
    "Crime": "police shooting shot murder killed arrested arrest crime suspect charged stabbing fbi",
    "Tech": "ai tech technology google apple meta microsoft openai musk tesla x twitter tiktok crypto bitcoin chip chips",
    "Culture": "hollywood movie film music celebrity sports nfl nba college football school schools university trans woke church religion",
    "Health": "health vaccine vaccines covid cdc fda medicaid medicare abortion hospital disease drug drugs rfk",
    "Military": "military pentagon troops army navy war hegseth strike strikes missile drone veterans",
}
STOP = set("""a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have having he
her here hers him his how i if in into is it its itself just me more most my no nor not now of off on once only or
other our out over own same she should so some such than that the their them then there these they this those through
to too under until up very was we were what when where which while who whom why will with would you your new news
says said say report reports reported after amid over us u.s. via just one two first last year years week day days
today live update updates watch video photos opinion analysis exclusive breaking here's what s t re ll ve d""".split())
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_|cmpid|cmp$|src$|source$|ito$|taid|smid|mbid|ocid|traffic_source|dicbo|_hs)")


# ---------------------------------------------------------------- config ----
def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    d = cfg.setdefault("discord", {})
    d.setdefault("api_base", "https://discord.com/api/v10")
    g = cfg.setdefault("general", {})
    g.setdefault("opml", "feeds.opml")
    g.setdefault("database", "newsdesk.db")
    g.setdefault("poll_minutes", 10)
    g.setdefault("max_article_age_hours", 12)
    g.setdefault("group_window_hours", 36)
    g.setdefault("hold_minutes", 10)
    g.setdefault("min_sources_to_post", 2)
    g.setdefault("include_bluesky", False)
    g.setdefault("export_json", "stories.json")
    g.setdefault("first_run_max_threads", 25)
    g.setdefault("first_run_max_satire", 8)
    rk = cfg.setdefault("ranking", {})
    rk.setdefault("enabled", True)
    rk.setdefault("times", ["07:00", "17:00"])   # local PC time; each run refreshes that day's digest
    rk.setdefault("top_n", 10)
    rk.setdefault("hours", 24)
    rk.setdefault("include_satire", 3)          # extra "Top satire" list, 0 to hide
    rk.setdefault("folder", "rankings")
    gr = cfg.setdefault("grouping", {})
    gr.setdefault("similarity", 0.30)
    gr.setdefault("duplicate_title_similarity", 0.90)
    s = cfg.setdefault("summary", {})
    s.setdefault("backend", "extractive")   # extractive | ollama | openai
    s.setdefault("min_sources", 2)
    s.setdefault("ollama_url", "http://localhost:11434")
    s.setdefault("model", "llama3.1:8b")
    s.setdefault("openai_base_url", "http://localhost:1234/v1")
    s.setdefault("openai_api_key", "")
    s.setdefault("timeout_seconds", 120)
    s.setdefault("json_mode", False)          # send response_format=json_object (Groq/OpenAI/Gemini support it)
    s.setdefault("max_ai_per_cycle", 12)      # stay inside free-tier rate limits
    s.setdefault("max_ai_tokens_per_day", 0)   # 0 = no cap; Groq free = 200,000/day, so the cloud config uses 170,000
    s.setdefault("ai_time_budget_seconds", 0)  # 0 = no cap; stop AI work after this long so a run can't time out
    s.setdefault("max_output_tokens", 0)       # 0 = no cap; caps each AI reply (reasoning + summary) to limit cost
    s.setdefault("reasoning_effort", "")       # "low" keeps gpt-oss models from spending tokens on long reasoning
    g.setdefault("timezone", "")               # e.g. "America/Chicago"; blank = this computer's clock
    g.setdefault("keep_days", 7)               # prune older history so the database stays small
    g.setdefault("log_file", "newsdesk.log")
    cfg.setdefault("source_overrides", {})
    # Secrets can come from environment variables (used by GitHub Actions and other hosts)
    env = {"NEWSDESK_BOT_TOKEN": ("discord", "bot_token"), "DISCORD_BOT_TOKEN": ("discord", "bot_token"),
           "NEWSDESK_NEWS_FORUM_ID": ("discord", "news_forum_id"),
           "NEWSDESK_SATIRE_FORUM_ID": ("discord", "satire_forum_id"),
           "NEWSDESK_AI_API_KEY": ("summary", "openai_api_key")}
    for var, (sec, key) in env.items():
        if os.environ.get(var, "").strip():
            cfg[sec][key] = os.environ[var].strip()
    url = s["openai_base_url"]
    if s["backend"] == "openai" and not s["openai_api_key"] and not re.search(r"localhost|127\.0\.0\.1", url):
        s["backend"] = "extractive"     # cloud AI selected but no key provided yet
        s["note"] = "AI key missing; using extractive summaries"
    d.setdefault("bot_token", "")
    tok = re.sub(r"\s+", "", str(d["bot_token"])).strip('"\'')
    d["bot_token"] = tok[3:] if tok.lower().startswith("bot") and tok.count(".") == 2 and len(tok) > 60 else tok
    s["openai_api_key"] = re.sub(r"\s+", "", str(s.get("openai_api_key", "")))
    for secret in (d["bot_token"], s["openai_api_key"]):
        if len(secret) >= 8:
            SECRETS.add(secret)
    d.setdefault("news_forum_id", "")
    if not str(d.get("satire_forum_id", "")).strip() or set(str(d.get("satire_forum_id"))) == {"0"}:
        d["satire_forum_id"] = d["news_forum_id"]
    d["news_forum_id"], d["satire_forum_id"] = str(d["news_forum_id"]).strip(), str(d["satire_forum_id"]).strip()
    return cfg


SECRETS: set[str] = set()


def redact(text: str) -> str:
    """Never let the bot token or AI key reach a log file or the screen."""
    for secret in SECRETS:
        text = text.replace(secret, "[hidden]")
    return re.sub(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}", "[hidden token]", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def local_now(cfg) -> datetime:
    tz = cfg["general"].get("timezone")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz))
        except Exception:  # noqa: BLE001  (Windows needs: pip install tzdata)
            log.warning("Unknown timezone %r (on Windows run: pip install tzdata); using PC clock", tz)
    return datetime.now().astimezone()


# ------------------------------------------------------------------ OPML ----
@dataclass
class Feed:
    name: str
    url: str
    lean: str  # left | center | right | satire


def folder_lean(folder: str) -> str | None:
    f = folder.lower()
    if f.startswith("left"):
        return "left"
    if f.startswith("right"):
        return "right"
    if f.startswith("center") or f.startswith("centre"):
        return "center"
    if f.startswith("satire"):
        return "satire"
    if f.startswith("bluesky"):
        return "bluesky"
    return None


def clean_source_name(title: str) -> str:
    name = re.sub(r"\s*[\[(].*?[\])]\s*", " ", title).strip()
    # "Fox News Politics" and "Fox News Latest" are the same outlet
    return re.sub(r"\s+(Politics|Latest|US)$", "", name).strip()


PAYWALL: set[str] = set()   # outlets marked [PAYWALL] in feeds.opml


def load_feeds(cfg: dict) -> list[Feed]:
    tree = ET.parse(HERE / cfg["general"]["opml"])
    overrides = {k.lower(): v.lower() for k, v in cfg["source_overrides"].items()}
    feeds = []
    for folder in tree.getroot().find("body"):
        lean = folder_lean(folder.get("text", ""))
        for o in folder.iter("outline"):
            url = o.get("xmlUrl")
            if not url:
                continue
            raw = o.get("title") or o.get("text") or url
            name = clean_source_name(raw)
            if "PAYWALL" in raw.upper():
                PAYWALL.add(name)
            this_lean = lean
            if lean == "bluesky":
                if not cfg["general"]["include_bluesky"]:
                    continue
                this_lean = "satire" if "SATIRE" in raw.upper() else None
            if "NOT SATIRE" in raw.upper():
                this_lean = "right"
            this_lean = overrides.get(name.lower(), this_lean)
            if this_lean not in LEAN_NAMES:
                log.warning("Skipping %s - unknown lean; add it to [source_overrides]", name)
                continue
            feeds.append(Feed(name, url, this_lean))
    return feeds


# ------------------------------------------------------------ text utils ----
def strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def canonical_url(u: str) -> str:
    try:
        p = urlsplit(u.strip())
    except ValueError:
        return u.strip()
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("amp.")
    path = re.sub(r"/(amp|amp\.html)/?$", "", p.path).rstrip("/") or "/"
    q = [(k, v) for k, v in parse_qsl(p.query) if not TRACKING_PARAMS.match(k.lower())]
    return urlunsplit(("https", host, path, urlencode(q), ""))


def norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"\s+[|\-–—]\s+[^|\-–—]{2,40}$", "", t)  # " - Fox News" suffixes
    return re.sub(r"[^a-z0-9 ]+", "", t).strip()


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9'\.\-]*", text):
        w = w.strip(".'-")
        lw = w.lower().removesuffix("'s")
        if len(lw) < 2 or lw in STOP:
            continue
        if lw.endswith("s") and len(lw) > 4 and not lw.endswith("ss"):
            lw = lw[:-1]
        out.append(lw)
        if w[:1].isupper() and len(w) > 2:   # names/places count extra
            out.append(lw)
    return out


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'“])", text) if len(s.strip()) > 30]


# -------------------------------------------------------------- database ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY, url_key TEXT UNIQUE, guid_key TEXT, url TEXT, title TEXT, title_norm TEXT,
  description TEXT, source TEXT, lean TEXT, published REAL, seen REAL, cluster_id INTEGER,
  duplicate_of INTEGER);
CREATE INDEX IF NOT EXISTS ix_art_guid ON articles(guid_key);
CREATE INDEX IF NOT EXISTS ix_art_cluster ON articles(cluster_id);
CREATE TABLE IF NOT EXISTS clusters(
  id INTEGER PRIMARY KEY, pool TEXT, created REAL, updated REAL, thread_id TEXT,
  posted_count INTEGER DEFAULT 0, dirty INTEGER DEFAULT 1, headline TEXT, summary_json TEXT,
  topic TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def db_open(cfg) -> sqlite3.Connection:
    path = HERE / cfg["general"]["database"]
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(clusters)")}
    for col, ddl in (("fails", "INTEGER DEFAULT 0"), ("ai_pending", "INTEGER DEFAULT 0")):
        if col not in cols:
            con.execute(f"ALTER TABLE clusters ADD COLUMN {col} {ddl}")
    con.commit()
    return con


def prune(con, cfg):
    cutoff = time.time() - cfg["general"]["keep_days"] * 86400
    old = [r[0] for r in con.execute("SELECT id FROM clusters WHERE updated<?", (cutoff,))]
    con.execute("DELETE FROM articles WHERE seen<? AND (cluster_id IS NULL OR cluster_id IN "
                "(SELECT id FROM clusters WHERE updated<?))", (cutoff, cutoff))
    con.execute("DELETE FROM clusters WHERE updated<?", (cutoff,))
    day = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d")
    con.execute("DELETE FROM meta WHERE (k LIKE 'ranking_done_%' AND substr(k, 14, 10) < ?)"
                " OR (k LIKE 'ranking_thread_%' AND substr(k, 16, 10) < ?)", (day, day))
    con.commit()
    if old:
        log.info("Pruned %d story groups older than %d days", len(old), cfg["general"]["keep_days"])


# --------------------------------------------------------------- fetching ---
def fetch_feed(feed: Feed) -> tuple[Feed, list[dict]]:
    try:
        r = requests.get(feed.url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    except Exception as e:  # noqa: BLE001
        log.warning("Feed failed: %s (%s)", feed.name, e)
        return feed, []
    items = []
    for e in parsed.entries:
        link = e.get("link") or ""
        title = strip_html(e.get("title", ""))
        if not link or not title:
            continue
        ts = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                ts = calendar.timegm(e[key])
                break
        desc = strip_html(e.get("summary", "") or (e.get("content") or [{}])[0].get("value", ""))
        items.append({"url": link, "guid": e.get("id") or link, "title": title,
                      "description": desc[:1500], "published": ts})
    return feed, items


# ---------------------------------------------------------- grouping core ---
def sims_peak(sims):
    return max(sims) if sims else 0.0


class Grouper:
    def __init__(self, con, cfg):
        self.con, self.cfg = con, cfg
        self.sim = cfg["grouping"]["similarity"]
        self.dup_sim = cfg["grouping"]["duplicate_title_similarity"]

    def _window_articles(self, pool: str):
        cutoff = time.time() - self.cfg["general"]["group_window_hours"] * 3600
        lean_filter = "lean='satire'" if pool == "satire" else "lean!='satire'"
        return self.con.execute(
            f"SELECT id,title,description,cluster_id,source,title_norm FROM articles "
            f"WHERE seen>? AND {lean_filter} AND duplicate_of IS NULL AND cluster_id IS NOT NULL",
            (cutoff,)).fetchall()

    @staticmethod
    def _vec(text: str, idf: dict) -> dict:
        tf = Counter(tokens(text))
        v = {t: (1 + math.log(c)) * idf.get(t, 1.0) for t, c in tf.items()}
        n = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / n for t, x in v.items()}

    @staticmethod
    def _cos(a: dict, b: dict) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(x * b.get(t, 0.0) for t, x in a.items())

    def add_batch(self, new_rows: list[sqlite3.Row]):
        """Assign each new (non-duplicate) article to an existing or new cluster."""
        for pool in ("news", "satire"):
            rows = [r for r in new_rows if (r["lean"] == "satire") == (pool == "satire")]
            if not rows:
                continue
            existing = self._window_articles(pool)
            docs = [f"{r['title']} {r['title']} {r['description'][:400]}" for r in existing] + \
                   [f"{r['title']} {r['title']} {r['description'][:400]}" for r in rows]
            df = Counter()
            for d in docs:
                df.update(set(tokens(d)))
            n = max(len(docs), 1)
            idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
            members = [(r["cluster_id"], self._vec(docs[i], idf), r["source"], r["id"])
                       for i, r in enumerate(existing)]
            fresh = set()
            for j, r in enumerate(rows):
                v = self._vec(docs[len(existing) + j], idf)
                scores: dict[int, list[float]] = {}
                for cid, mv, _src, _aid in members:
                    scores.setdefault(cid, []).append(self._cos(v, mv))
                best_cid, best = None, 0.0
                for cid, sims in scores.items():
                    sims.sort(reverse=True)
                    # strong match to one member, or decent match to several
                    s = sims[0] if len(sims) == 1 else max(sims[0] * 0.85, sum(sims[:3]) / min(3, len(sims)))
                    if s > best:
                        best_cid, best = cid, s
                now = time.time()
                if best_cid is not None and best >= self.sim:
                    cid = best_cid
                    self.con.execute("UPDATE clusters SET updated=?, dirty=1 WHERE id=?", (now, cid))
                else:
                    cid = self.con.execute(
                        "INSERT INTO clusters(pool,created,updated,dirty) VALUES(?,?,?,1)",
                        (pool, now, now)).lastrowid
                self.con.execute("UPDATE articles SET cluster_id=? WHERE id=?", (cid, r["id"]))
                members.append((cid, v, r["source"], r["id"]))
                if best_cid is None or best < self.sim:
                    fresh.add(cid)
            # second pass: an early article in this batch only saw what came before it, so let
            # new one-article groups join a group formed later in the same batch
            for cid in fresh:
                mine = [m for m in members if m[0] == cid]
                if len(mine) != 1:
                    continue
                _, v, _src, aid = mine[0]
                scores = {}
                for ocid, mv, _s, _a in members:
                    if ocid != cid:
                        scores.setdefault(ocid, []).append(self._cos(v, mv))
                best_cid, best = None, 0.0
                for ocid, sims in scores.items():
                    if len(sims) < 2:
                        continue
                    sims.sort(reverse=True)
                    sc = max(sims[0] * 0.85, sum(sims[:3]) / min(3, len(sims)))
                    if sc > best:
                        best_cid, best = ocid, sc
                if best_cid is not None and best >= self.sim * 0.9 and sims_peak(scores[best_cid]) >= self.sim:
                    self.con.execute("UPDATE articles SET cluster_id=? WHERE id=?", (best_cid, aid))
                    self.con.execute("DELETE FROM clusters WHERE id=?", (cid,))
                    members = [(best_cid if m[3] == aid else m[0], m[1], m[2], m[3]) for m in members]
        self.con.commit()


def ingest(con, cfg, feeds: list[Feed]) -> int:
    max_age = cfg["general"]["max_article_age_hours"] * 3600
    now = time.time()
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(fetch_feed, feeds))
    new_ids = []
    dup_count = 0
    for feed, items in results:
        for it in items:
            if it["published"] and now - it["published"] > max_age:
                continue
            ukey = canonical_url(it["url"])
            gkey = hashlib.sha1((feed.name + "|" + it["guid"]).encode()).hexdigest()
            if con.execute("SELECT 1 FROM articles WHERE url_key=? OR guid_key=?", (ukey, gkey)).fetchone():
                continue  # already seen (same link, or same item from this outlet)
            tnorm = norm_title(it["title"])
            dup = None
            # Same outlet re-running a headline (e.g. Fox Politics + Fox Latest) => prune.
            # Identical headlines from sister sites (RedState/PJ Media/Hot Air syndication) => prune too.
            for s in con.execute("SELECT id, title_norm, source FROM articles WHERE seen>? AND duplicate_of IS NULL"
                                 " AND (source=? OR title_norm=?)", (now - 48 * 3600, feed.name, tnorm)):
                if s["title_norm"] == tnorm or _jaccard(s["title_norm"], tnorm) >= cfg["grouping"]["duplicate_title_similarity"]:
                    dup = s["id"]
                    break
            cur = con.execute(
                "INSERT INTO articles(url_key,guid_key,url,title,title_norm,description,source,lean,published,seen,duplicate_of)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ukey, gkey, it["url"], it["title"], tnorm, it["description"], feed.name, feed.lean,
                 it["published"] or now, now, dup))
            if dup:
                dup_count += 1
                continue
            new_ids.append(cur.lastrowid)
    con.commit()
    rows = [con.execute("SELECT * FROM articles WHERE id=?", (i,)).fetchone() for i in new_ids]
    rows.sort(key=lambda r: r["published"])
    Grouper(con, cfg).add_batch(rows)
    log.info("Fetched %d feeds: %d new articles, %d duplicates pruned", len(feeds), len(rows), dup_count)
    return len(rows)


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / max(len(sa | sb), 1)


# -------------------------------------------------------------- summaries ---
SUMMARY_PROMPT = """You are a news desk editor preparing story notes for a political commentary show.
Below are headlines and feed blurbs from several outlets covering what is likely the same story.
Each item is labeled with the outlet and its lean (LEFT, CENTER or RIGHT).

Write ONLY a JSON object with these keys:
  "headline": a neutral, factual headline, max 90 characters
  "summary": 2-4 neutral sentences: what happened, who is involved, why it matters
  "left_angle": one sentence on how the LEFT-leaning outlets frame it ("" if none)
  "right_angle": one sentence on how the RIGHT-leaning outlets frame it ("" if none)
  "disputed": one sentence on facts or framing the sides disagree on ("" if none)
  (CENTER outlets count toward neither angle; use them for the neutral summary)
  "topic": exactly one of {topics}

Use only the information given. Do not invent facts, quotes, or numbers.

ITEMS:
{items}"""


AI_BUDGET = {"left": 0, "deadline": 0.0, "paused_until": 0.0, "tokens": 0, "day": "", "warned": False}
AI_MILESTONES = (3, 5, 8, 12, 20)   # re-summarize an existing thread with AI only when it reaches these outlet counts


class AILimit(RuntimeError):
    """The AI provider said 'not now' (daily quota or a long per-minute wait)."""
    def __init__(self, msg, daily=False, retry=0.0):
        super().__init__(msg)
        self.daily, self.retry = daily, retry


def _retry_seconds(text: str) -> float:
    m = re.search(r"try again in\s*(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?", text or "")
    if not m or not any(m.groups()):
        return 0.0
    h, mi, se = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)


def ai_available(cfg) -> bool:
    now = time.time()
    if AI_BUDGET["left"] <= 0 or now < AI_BUDGET["paused_until"]:
        return False
    if AI_BUDGET["deadline"] and now > AI_BUDGET["deadline"]:
        return False
    cap = cfg["summary"]["max_ai_tokens_per_day"]
    return not (cap and AI_BUDGET["tokens"] >= cap)


def _ai_pause(seconds: float, why: str):
    AI_BUDGET["paused_until"] = max(AI_BUDGET["paused_until"], time.time() + seconds)
    AI_BUDGET["left"] = 0
    if not AI_BUDGET["warned"]:
        AI_BUDGET["warned"] = True
        until = datetime.fromtimestamp(AI_BUDGET["paused_until"]).strftime("%H:%M")
        log.warning("%s - using quick summaries; AI summaries resume after %s (UTC on GitHub)", why, until)


def wants_ai(cfg, arts, pool) -> bool:
    s = cfg["summary"]
    return s["backend"] in ("ollama", "openai") and pool != "satire" and \
        len({a["source"] for a in arts}) >= s["min_sources"]


def summarize(cfg, arts: list[sqlite3.Row], pool: str) -> dict:
    s = cfg["summary"]
    use_ai = wants_ai(cfg, arts, pool)
    if use_ai and not ai_available(cfg):
        out = extractive_summary(arts)
        out["ai_pending"] = True      # try AI again next cycle
        return out
    if use_ai:
        AI_BUDGET["left"] -= 1
        items = "\n".join(
            f"- [{a['lean'].upper()}] {a['source']}: {a['title']} -- {a['description'][:350]}" for a in arts[:14])
        prompt = SUMMARY_PROMPT.format(items=items, topics=", ".join(TOPICS))
        try:
            raw = _llm(cfg, prompt)
            data = _parse_json(raw)
            if isinstance(data.get("summary"), str) and data.get("summary"):
                for k in ("headline", "left_angle", "right_angle", "disputed"):
                    if not isinstance(data.get(k, ""), str):
                        data[k] = ""
                if not data.get("headline"):
                    data["headline"] = extractive_summary(arts)["headline"]
                data["engine"] = s["model"]
                if data.get("topic") not in TOPICS:
                    data["topic"] = guess_topic(arts)
                return data
        except AILimit as e:
            if e.daily:
                _ai_pause(e.retry or 3600, "AI daily limit reached")
            else:
                AI_BUDGET["left"] = 0          # busy right now; try again next run
                log.info("AI is rate limited (%s); finishing this run with quick summaries", e)
            out = extractive_summary(arts)
            out["ai_pending"] = True
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("AI summary failed, using extractive fallback: %s", redact(str(e))[:300])
    return extractive_summary(arts)


def _llm(cfg, prompt: str) -> str:
    s = cfg["summary"]
    if s["backend"] == "ollama":
        r = requests.post(f"{s['ollama_url'].rstrip('/')}/api/chat", timeout=s["timeout_seconds"], json={
            "model": s["model"], "stream": False, "format": "json",
            "options": {"temperature": 0.2, **({"num_predict": s["max_output_tokens"]} if s["max_output_tokens"] else {})},
            "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["message"]["content"]
    headers = {"Content-Type": "application/json"}
    if s["openai_api_key"]:
        headers["Authorization"] = f"Bearer {s['openai_api_key']}"
    body = {"model": s["model"], "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]}
    if s["json_mode"]:
        body["response_format"] = {"type": "json_object"}
    if s.get("reasoning_effort"):
        body["reasoning_effort"] = s["reasoning_effort"]
    if s.get("max_output_tokens"):
        body["max_completion_tokens"] = int(s["max_output_tokens"])
    for attempt in range(3):
        r = requests.post(f"{s['openai_base_url'].rstrip('/')}/chat/completions", headers=headers,
                          timeout=s["timeout_seconds"], json=body)
        if r.status_code == 429:
            text = r.text or ""
            try:
                wait = float(r.headers.get("retry-after", 0) or 0)
            except ValueError:
                wait = 0.0
            wait = max(wait, _retry_seconds(text))
            if re.search(r"per day|\(TPD\)|\(RPD\)|daily|quota", text, re.I):
                raise AILimit("daily limit", daily=True, retry=wait)
            if wait > 20 or attempt >= 2 or (AI_BUDGET["deadline"] and time.time() + wait > AI_BUDGET["deadline"]):
                raise AILimit(f"wait {wait:.0f}s", retry=wait)
            log.info("AI rate limit, waiting %.0fs", max(wait, 2))
            time.sleep(max(wait, 2))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"AI API {r.status_code}: {r.text[:200]}")
        data = r.json()
        used = (data.get("usage") or {}).get("total_tokens") or 0
        AI_BUDGET["tokens"] += int(used)
        content = data["choices"][0]["message"].get("content") or ""
        if not content.strip():
            raise RuntimeError("AI returned an empty reply")
        return content
    raise AILimit("rate limit retries exhausted")


def _parse_json(raw: str) -> dict:
    raw = re.sub(r"(?s)<think>.*?</think>", "", raw)        # reasoning models
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M)
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0) if m else raw)
    if not isinstance(data, dict):
        raise ValueError("AI reply was not a JSON object")
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}


def extractive_summary(arts: list[sqlite3.Row]) -> dict:
    """No-AI fallback: pick the most representative sentences across outlets."""
    sents = []
    for a in arts:
        for snt in split_sentences(a["description"])[:3]:
            if len(snt) <= 320:   # skip run-on blurbs that didn't split cleanly
                sents.append((snt, a["source"]))
    if not sents:
        return {"headline": arts[0]["title"], "summary": "", "engine": "extractive", "topic": guess_topic(arts)}
    all_tok = Counter(t for a in arts for t in set(tokens(a["title"] + " " + a["description"])))
    def score(snt):
        tk = set(tokens(snt))
        return sum(all_tok[t] for t in tk) / (len(tk) ** 0.5 or 1)
    picked, used_src = [], set()
    for snt, src in sorted(sents, key=lambda x: -score(x[0])):
        if src in used_src and len(used_src) < len({s for _, s in sents}):
            continue
        if any(_jaccard(norm_title(snt), norm_title(p)) > 0.5 for p in picked):
            continue
        picked.append(snt)
        used_src.add(src)
        if len(picked) == 2:
            break
    head = max(arts, key=lambda a: sum(all_tok[t] for t in set(tokens(a["title"]))))["title"]
    summary = " ".join(picked)
    if len(summary) > 450:
        summary = summary[:450].rsplit(" ", 1)[0] + "..."
    return {"headline": head, "summary": summary, "engine": "extractive",
            "topic": guess_topic(arts)}


def guess_topic(arts) -> str:
    text = " ".join(a["title"] + " " + a["description"][:300] for a in arts).lower()
    words = Counter(re.findall(r"[a-z][a-z\.]+", text))
    best, score = "Politics", 0
    for topic, kw in TOPIC_KEYWORDS.items():
        sc = sum(words[k] for k in kw.split())
        if sc > score:
            best, score = topic, sc
    return best if score >= 2 else "Politics"


# ---------------------------------------------------------------- Discord ---
class DiscordError(RuntimeError):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


HELP_403 = ("Discord said 'Missing Permissions/Access'. In the forum channel settings > Permissions, make sure the "
            "bot's role has View Channel, Send Messages, Send Messages in Threads, Create Posts/Public Threads, "
            "Manage Threads, Embed Links, Read Message History (and Manage Channels for --setup).")


class Discord:
    def __init__(self, cfg):
        d = cfg["discord"]
        self.base = d["api_base"].rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bot {d['bot_token']}",
                               "User-Agent": "DiscordBot (newsdesk, 1.0)"})

    def req(self, method, path, **kw):
        for attempt in range(6):
            try:
                r = self.s.request(method, self.base + path, timeout=30, **kw)
            except requests.exceptions.InvalidHeader as e:
                raise DiscordError(401, "The bot token contains invalid characters. Re-copy it from the "
                                        "Developer Portal > Bot > Reset Token.") from None
            except requests.RequestException as e:
                if attempt >= 2:
                    raise DiscordError(0, f"Discord unreachable: {e}") from e
                time.sleep(3)
                continue
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 2))
                except ValueError:
                    wait = float(r.headers.get("retry-after", 2))
                log.info("Discord rate limit, waiting %.1fs", wait + 0.25)
                time.sleep(min(wait, 120) + 0.25)
                continue
            if r.status_code >= 500 and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise DiscordError(401, "Discord rejected the bot token (401). Re-copy it from the Developer "
                                        "Portal > Bot > Reset Token, and paste it with no spaces.")
            if r.status_code >= 400:
                raise DiscordError(r.status_code, f"Discord {method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.text else {}
        raise DiscordError(429, "Discord rate limit retries exhausted")

    def pinned_in_forum(self, forum_id) -> list[dict]:
        """Every pinned post in a forum, open or archived (Discord allows one pinned post per forum)."""
        guild = self.req("GET", f"/channels/{forum_id}").get("guild_id")
        found = {}
        if guild:
            for th in self.req("GET", f"/guilds/{guild}/threads/active").get("threads", []):
                found[th["id"]] = th
        for th in self.req("GET", f"/channels/{forum_id}/threads/archived/public", params={"limit": 50}).get("threads", []):
            found.setdefault(th["id"], th)
        return [t for t in found.values() if t.get("parent_id") == forum_id and int(t.get("flags") or 0) & 2]

    def unpin_thread(self, th: dict):
        flags = int(th.get("flags") or 0) & ~2
        if (th.get("thread_metadata") or {}).get("archived"):
            self.req("PATCH", f"/channels/{th['id']}", json={"archived": False})
            self.req("PATCH", f"/channels/{th['id']}", json={"flags": flags})
            self.req("PATCH", f"/channels/{th['id']}", json={"archived": True})
        else:
            self.req("PATCH", f"/channels/{th['id']}", json={"flags": flags})

    def forum_tags(self, forum_id) -> dict[str, str]:
        ch = self.req("GET", f"/channels/{forum_id}")
        return {t["name"]: t["id"] for t in ch.get("available_tags", [])}

    def ensure_tags(self, forum_id, names: list[str]):
        ch = self.req("GET", f"/channels/{forum_id}")
        if ch.get("type") != 15:
            raise SystemExit(f"Channel {forum_id} is not a Forum channel (type {ch.get('type')}). "
                             "Create a channel of type Forum and copy its ID.")
        tags = ch.get("available_tags", [])
        have = {t["name"] for t in tags}
        missing = [n for n in names if n not in have]
        if missing:
            if len(tags) + len(missing) > 20:
                raise SystemExit("Discord allows 20 tags per forum; remove some tags or topics.")
            tags = [{"id": t["id"], "name": t["name"], "moderated": t.get("moderated", False),
                     "emoji_id": t.get("emoji_id"), "emoji_name": t.get("emoji_name")} for t in tags]
            tags += [{"name": n} for n in missing]
            self.req("PATCH", f"/channels/{forum_id}", json={"available_tags": tags})
        return self.forum_tags(forum_id)


LEAN_COLOR = {"left": 0x2F6FED, "center": 0x95A5A6, "right": 0xD93636, "both": 0x8E44AD, "satire": 0xF1C40F}


def cluster_view(con, cid):
    arts = con.execute("SELECT * FROM articles WHERE cluster_id=? AND duplicate_of IS NULL ORDER BY published",
                       (cid,)).fetchall()
    leans = {a["lean"] for a in arts}
    sources = list(dict.fromkeys(a["source"] for a in arts))
    if "satire" in leans:
        side = "satire"
    elif {"left", "right"} <= leans:
        side = "both"
    else:
        side = next(l for l in LEAN_ORDER if l in leans)   # left or right wins over center
    return arts, sources, side


def build_message(arts, sources, side, summ: dict, also=()) -> dict:
    lines = []
    if side == "satire":
        lines.append("**SATIRE - not real news.**")
    if summ.get("summary"):
        lines.append(summ["summary"])
    for key, label in (("left_angle", "Left framing"), ("right_angle", "Right framing"),
                       ("disputed", "In dispute")):
        if summ.get(key):
            lines.append(f"**{label}:** {summ[key]}")
    for lean in LEAN_ORDER:
        group = [a for a in arts if a["lean"] == lean]
        if not group:
            continue
        lines.append(f"\n**{LEAN_NAMES[lean]} ({len({a['source'] for a in group})})**")
        for a in group[:12]:
            t = a["title"].replace("[", "(").replace("]", ")")[:150]
            pw = " ($)" if a["source"] in PAYWALL else ""
            lines.append(f"- {a['source']}{pw}: [{t}]({a['url']})")
    if also:
        lines.append(f"\nAlso carried by: {', '.join(sorted(also))}")
    if any(a["source"] in PAYWALL for a in arts):
        lines.append("($) = may hit a paywall")
    desc = "\n".join(lines)
    if len(desc) > 4000:
        desc = desc[:3990] + "\n..."
    foot = f"{len(sources)} source(s) | {len(arts)} article(s) | summary: {summ.get('engine', 'n/a')}"
    return {"embeds": [{"title": (summ.get("headline") or arts[0]["title"] or "Story")[:256], "url": arts[0]["url"],
                        "description": desc, "color": LEAN_COLOR[side], "footer": {"text": foot},
                        "timestamp": datetime.now(timezone.utc).isoformat()}],
            "allowed_mentions": {"parse": []}}


def tags_for(side, sources, topic, tag_ids, leans=None) -> list[str]:
    """Both Sides = covered by left AND right outlets. Left/Center/Right = which sides covered it."""
    names = []
    leans = set(leans or ())
    if side == "satire":
        names.append("Satire")
    else:
        if side == "both":
            names.append("Both Sides")
        leans |= {"left", "right"} if side == "both" else {side}
        names += [LEAN_NAMES[l] for l in ("left", "center", "right") if l in leans]
    if len(sources) == 1 and side != "satire":
        names.append("Single Source")
    if topic and side != "satire":
        names.append(topic)
    return [tag_ids[n] for n in names if n in tag_ids][:5]


def thread_title(summ, arts, sources, side) -> str:
    head = (summ.get("headline") or arts[0]["title"] or "").strip() or "Untitled story"
    prefix = "SATIRE: " if side == "satire" else (f"[{len(sources)}] " if len(sources) > 1 else "")
    return (prefix + head)[:100]


def publish(con, cfg, dry_run=False) -> int:
    g = cfg["general"]
    now = time.time()
    hold = g["hold_minutes"] * 60
    ai_load(con, cfg)
    if con.execute("SELECT 1 FROM meta WHERE k='satire_requeued'").fetchone() is None:
        # one-time repair: older versions skipped every satire piece on the first run
        fresh = now - g["max_article_age_hours"] * 3600
        con.execute("UPDATE clusters SET thread_id=NULL, posted_count=0, dirty=1 WHERE id IN ("
                    "SELECT id FROM clusters WHERE pool='satire' AND thread_id='skipped' AND created>? "
                    "ORDER BY created DESC LIMIT ?)", (fresh, g["first_run_max_satire"]))
        con.execute("INSERT OR REPLACE INTO meta VALUES('satire_requeued','1')")
        con.commit()
    # most-covered stories first, so the first run (and busy cycles) post the big stories
    rows = con.execute(
        "SELECT c.*, (SELECT COUNT(DISTINCT source) FROM articles a WHERE a.cluster_id=c.id AND a.duplicate_of IS NULL) n"
        " FROM clusters c WHERE dirty=1 OR ai_pending=1 ORDER BY n DESC, created").fetchall()
    first_run = con.execute("SELECT v FROM meta WHERE k='first_run_done'").fetchone() is None
    dc = None if dry_run else Discord(cfg)
    tag_cache = {}
    created = 0
    first_count = {"news": 0, "satire": 0}
    bad_forums = set()
    for c in rows:
        arts, sources, side = cluster_view(con, c["id"])
        if not arts:
            con.execute("UPDATE clusters SET dirty=0, ai_pending=0 WHERE id=?", (c["id"],))
            continue
        if c["thread_id"] == "failed":
            con.execute("UPDATE clusters SET dirty=0, ai_pending=0 WHERE id=?", (c["id"],))
            continue
        if c["thread_id"] == "skipped":
            # held back on the first run; post it later only if it turns into a big story
            if len(sources) < 3 or side == "satire":
                con.execute("UPDATE clusters SET dirty=0, ai_pending=0 WHERE id=?", (c["id"],))
                continue
            con.execute("UPDATE clusters SET thread_id=NULL, posted_count=0 WHERE id=?", (c["id"],))
            c = con.execute("SELECT * FROM clusters WHERE id=?", (c["id"],)).fetchone()
        new_thread = not c["thread_id"]
        if new_thread:
            # wait a little so related stories can join before the thread is created
            if not first_run and now - c["created"] < hold:
                continue
            if len(sources) < g["min_sources_to_post"] and side != "satire":
                continue
            if first_run:
                # news and satire get separate first-run limits so satire is never crowded out
                pool = "satire" if side == "satire" else "news"
                cap = g["first_run_max_satire"] if pool == "satire" else g["first_run_max_threads"]
                if first_count[pool] >= cap:
                    con.execute("UPDATE clusters SET dirty=0, thread_id='skipped' WHERE id=?", (c["id"],))
                    continue
                first_count[pool] += 1
        has_new = len(arts) > (c["posted_count"] or 0)
        if not new_thread and not has_new and not c["ai_pending"]:
            con.execute("UPDATE clusters SET dirty=0 WHERE id=?", (c["id"],))
            continue
        if c["ai_pending"] and not has_new and not new_thread and not ai_available(cfg):
            continue   # still waiting for AI budget; keep the extractive version for now
        prev = json.loads(c["summary_json"]) if c["summary_json"] else {}
        prev_n = prev.get("n_sources", 0)
        reuse = (not new_thread and prev.get("engine") not in (None, "extractive") and not c["ai_pending"]
                 and not any(prev_n < m <= len(sources) for m in AI_MILESTONES))
        summ = dict(prev) if reuse else summarize(cfg, arts, c["pool"])
        pending = 1 if summ.pop("ai_pending", False) else 0
        summ["n_sources"] = len(sources)
        if dry_run:
            con.execute("UPDATE clusters SET dirty=0, ai_pending=?, headline=?, summary_json=?, topic=? WHERE id=?",
                        (pending, summ.get("headline"), json.dumps(summ), summ.get("topic"), c["id"]))
            created += 1
            continue
        forum = cfg["discord"]["satire_forum_id"] if side == "satire" else cfg["discord"]["news_forum_id"]
        if forum in bad_forums:
            continue
        if forum not in tag_cache:
            try:
                tag_cache[forum] = dc.forum_tags(forum)
            except DiscordError as e:
                which = "satire_forum_id" if side == "satire" else "news_forum_id"
                log.error("Can't open the forum in %s (%s): %s. Its stories stay queued until this is fixed. %s",
                          which, forum, e, HELP_403 if e.status in (403, 404) else "")
                bad_forums.add(forum)
                continue
        also = {r["source"] for r in con.execute(
            "SELECT DISTINCT d.source FROM articles d JOIN articles a ON d.duplicate_of=a.id "
            "WHERE a.cluster_id=? AND d.source NOT IN (SELECT source FROM articles WHERE cluster_id=? AND duplicate_of IS NULL)",
            (c["id"], c["id"]))}
        msg = build_message(arts, sources, side, summ, also)
        tags = tags_for(side, sources, summ.get("topic"), tag_cache[forum], {a["lean"] for a in arts})
        title = thread_title(summ, arts, sources, side)
        try:
            tid = c["thread_id"]
            if tid:
                try:
                    dc.req("PATCH", f"/channels/{tid}", json={"archived": False})
                except DiscordError as e:
                    if e.status != 404:
                        raise
                    log.warning("Thread for '%s' was deleted in Discord; creating a new one", title)
                    tid = None
            if not tid:
                th = dc.req("POST", f"/channels/{forum}/threads",
                            json={"name": title, "applied_tags": tags, "message": msg})
                con.execute("UPDATE clusters SET thread_id=?, posted_count=? WHERE id=?",
                            (th["id"], len(arts), c["id"]))
                created += 1
                log.info("New thread: %s", title)
            else:
                dc.req("PATCH", f"/channels/{tid}/messages/{tid}", json=msg)   # refresh summary + source list
                dc.req("PATCH", f"/channels/{tid}", json={"name": title, "applied_tags": tags})
                if has_new:
                    newest = sorted(arts, key=lambda a: a["id"])[c["posted_count"] or 0:]
                    note = "\n".join(
                        f"+ **{a['source']}** ({LEAN_NAMES[a['lean']]}): "
                        f"[{a['title'][:150].replace('[', '(').replace(']', ')')}]({a['url']})" for a in newest[:10])
                    dc.req("POST", f"/channels/{tid}/messages",
                           json={"content": f"New coverage added:\n{note}"[:1990], "allowed_mentions": {"parse": []},
                                 "flags": 4})  # flags=4 hides link previews
                    log.info("Updated thread: %s (+%d)", title, len(newest))
                else:
                    log.info("Upgraded summary: %s", title)
                con.execute("UPDATE clusters SET posted_count=? WHERE id=?", (len(arts), c["id"]))
        except DiscordError as e:
            if e.status in (401, 403):
                con.commit()
                raise SystemExit(redact(f"{e}\n{HELP_403 if e.status == 403 else ''}"))
            fails = (c["fails"] or 0) + 1
            log.error("%s (attempt %d)", e, fails)
            if fails >= 3 and e.status and e.status != 429:
                log.error("Giving up on story group %d after 3 failures", c["id"])
                con.execute("UPDATE clusters SET fails=?, dirty=0, ai_pending=0, thread_id=COALESCE(thread_id,'failed')"
                            " WHERE id=?", (fails, c["id"]))
            else:
                con.execute("UPDATE clusters SET fails=? WHERE id=?", (fails, c["id"]))
            con.commit()
            continue
        con.execute("UPDATE clusters SET dirty=0, fails=0, ai_pending=?, headline=?, summary_json=?, topic=? WHERE id=?",
                    (pending, summ.get("headline"), json.dumps(summ), summ.get("topic"), c["id"]))
        con.commit()
        time.sleep(1.0)
    con.execute("INSERT OR REPLACE INTO meta VALUES('first_run_done','1')")
    ai_save(con)
    con.commit()
    return created


def ai_load(con, cfg):
    """Restore today's AI token count and any pause from earlier runs."""
    s = cfg["summary"]
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meta = dict(con.execute("SELECT k, v FROM meta WHERE k IN ('ai_day','ai_tokens','ai_paused_until')").fetchall())
    AI_BUDGET.update(left=s["max_ai_per_cycle"], warned=False, day=day,
                     tokens=int(meta.get("ai_tokens", 0) or 0) if meta.get("ai_day") == day else 0,
                     paused_until=float(meta.get("ai_paused_until", 0) or 0),
                     deadline=time.time() + s["ai_time_budget_seconds"] if s["ai_time_budget_seconds"] else 0.0)
    cap = s["max_ai_tokens_per_day"]
    if wants_ai(cfg, [{"source": "a"}, {"source": "b"}], "news") and cap and AI_BUDGET["tokens"] >= cap:
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        log.info("Daily AI token budget used (%d/%d); quick summaries until %s UTC",
                 AI_BUDGET["tokens"], cap, midnight.strftime("%H:%M"))


def ai_save(con):
    con.execute("INSERT OR REPLACE INTO meta VALUES('ai_day', ?)", (AI_BUDGET["day"],))
    con.execute("INSERT OR REPLACE INTO meta VALUES('ai_tokens', ?)", (str(AI_BUDGET["tokens"]),))
    con.execute("INSERT OR REPLACE INTO meta VALUES('ai_paused_until', ?)", (str(AI_BUDGET["paused_until"]),))


# ---------------------------------------------------------------- ranking ---
def rank_stories(con, cfg, now=None) -> list[dict]:
    """Trending score for each story group over the last N hours.

    score = 3 x outlets  + 4 if both left and right cover it (+1 more if center also does)
          + 1.5 x outlets that joined in the last 6 hours (momentum)
          + 0.5 x articles (capped at 10)
    """
    now = now or time.time()
    since = now - cfg["ranking"]["hours"] * 3600
    out = []
    for c in con.execute("SELECT * FROM clusters WHERE updated>?", (since,)):
        arts = con.execute("SELECT * FROM articles WHERE cluster_id=? AND duplicate_of IS NULL AND seen>?",
                           (c["id"], since)).fetchall()
        if not arts:
            continue
        srcs = {a["source"] for a in arts}
        left = {a["source"] for a in arts if a["lean"] == "left"}
        right = {a["source"] for a in arts if a["lean"] == "right"}
        center = {a["source"] for a in arts if a["lean"] == "center"}
        recent = {a["source"] for a in arts if a["seen"] > now - 6 * 3600}
        both = bool(left and right)
        score = 3 * len(srcs) + (4 if both else 0) + (1 if both and center else 0) + 1.5 * len(recent) + 0.5 * min(len(arts), 10)
        summ = json.loads(c["summary_json"]) if c["summary_json"] else {}
        out.append({"id": c["id"], "pool": c["pool"], "score": round(score, 1), "outlets": len(srcs),
                    "left": len(left), "center": len(center), "right": len(right), "both": both, "articles": len(arts),
                    "recent_outlets": len(recent),
                    "headline": c["headline"] or summ.get("headline") or arts[0]["title"],
                    "topic": c["topic"] or guess_topic(arts), "summary": summ.get("summary", ""),
                    "thread_id": c["thread_id"], "url": arts[0]["url"],
                    "sources": sorted(srcs)})
    out.sort(key=lambda r: (-r["score"], -r["outlets"]))
    return out


def side_label(r) -> str:
    if r["both"]:
        return "Both Sides"
    has = [n for n, k in (("Left", "left"), ("Center", "center"), ("Right", "right")) if r.get(k)]
    return " + ".join(has) if len(has) > 1 else f"{has[0]} only" if has else "n/a"


def ranking_report(con, cfg, dry_run=False, label=None) -> str:
    rk = cfg["ranking"]
    ranked = rank_stories(con, cfg)
    news = [r for r in ranked if r["pool"] == "news"][:rk["top_n"]]
    sat = [r for r in ranked if r["pool"] == "satire"][:rk["include_satire"]]
    today = local_now(cfg)
    day = today.strftime("%Y-%m-%d")
    prev_row = con.execute("SELECT v FROM meta WHERE k='last_ranking_ids'").fetchone()
    prev = json.loads(prev_row[0]) if prev_row else {}
    prev_pos = {int(k): v for k, v in prev.get("pos", {}).items()}
    guild = None
    if not dry_run:
        dc = Discord(cfg)
        try:
            guild = dc.req("GET", f"/channels/{cfg['discord']['news_forum_id']}").get("guild_id")
        except RuntimeError as e:
            log.error("Could not reach the forum for the ranking: %s", e)
            dry_run = True

    def link(r):
        if guild and r["thread_id"] and r["thread_id"] not in ("skipped", "failed"):
            return f"https://discord.com/channels/{guild}/{r['thread_id']}"
        return r["url"]

    def move(i, r):
        p = prev_pos.get(r["id"])
        if p is None:
            return "new"
        if p > i:
            return f"up {p - i}"
        if p < i:
            return f"down {i - p}"
        return "same"

    lines, md = [], [f"# Top trending stories - {today.strftime('%A, %B %d, %Y')}",
                     f"Updated {today.strftime('%I:%M %p')} | last {rk['hours']} hours | "
                     f"{len(ranked)} story groups tracked", ""]
    for i, r in enumerate(news, 1):
        side = side_label(r)
        meta = f"{r['outlets']} outlets ({r['left']}L/{r.get('center', 0)}C/{r['right']}R) | {side} | {r['topic']} | score {r['score']} | {move(i, r)}"
        title = r["headline"].replace("[", "(").replace("]", ")")[:140]
        lines.append(f"**{i}. [{title}]({link(r)})**\n{meta}")
        md.append(f"{i}. **{r['headline']}**  \n   {meta}  \n   {', '.join(r['sources'])}  \n   {r['url']}")
        if r["summary"]:
            md.append(f"   > {r['summary']}")
    if sat:
        lines.append("\n**Top satire (not real news)**")
        md += ["", "## Top satire (not real news)"]
        for r in sat:
            title = r["headline"].replace("[", "(").replace("]", ")")[:140]
            lines.append(f"- [{title}]({link(r)}) ({', '.join(r['sources'])})")
            md.append(f"- {r['headline']} ({', '.join(r['sources'])}) {r['url']}")

    folder = HERE / rk["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{day}.md").write_text("\n".join(md), encoding="utf-8")
    (folder / f"{day}.json").write_text(json.dumps({"day": day, "generated": today.isoformat(),
                                                    "news": news, "satire": sat}, indent=2), encoding="utf-8")
    con.execute("INSERT OR REPLACE INTO meta VALUES('last_ranking_ids', ?)",
                (json.dumps({"day": day, "pos": {r["id"]: i for i, r in enumerate(news, 1)}}),))
    con.commit()
    log.info("Ranking written: %s (%d stories)", folder / f"{day}.md", len(news))
    if dry_run or not news:
        return "\n".join(md)

    # one digest thread per day; later runs that day edit it and add a short "updated" reply
    desc = "\n\n".join(lines)
    if len(desc) > 4000:
        desc = desc[:3990] + "\n..."
    msg = {"embeds": [{"title": f"Top {rk['top_n']} trending - {today.strftime('%a %b %d')} ({label or today.strftime('%I:%M %p')})",
                       "description": desc, "color": 0x42FCC6,
                       "footer": {"text": "Score = outlets, left+right coverage, last-6h momentum, article count"},
                       "timestamp": today.astimezone(timezone.utc).isoformat()}],
           "allowed_mentions": {"parse": []}}
    forum = cfg["discord"]["news_forum_id"]
    tags = dc.forum_tags(forum)
    tag = [tags["Daily Ranking"]] if "Daily Ranking" in tags else []
    key = f"ranking_thread_{day}"
    row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    title = f"TOP {rk['top_n']} STORIES - {today.strftime('%a %b %d, %Y')}"
    try:
        if row:
            try:
                dc.req("PATCH", f"/channels/{row[0]}", json={"archived": False})
            except DiscordError as e:
                if e.status != 404:
                    raise
                log.warning("Today's ranking thread was deleted in Discord; creating a new one")
                con.execute("DELETE FROM meta WHERE k=?", (key,))
                row = None
        if row:
            tid = row[0]
            dc.req("PATCH", f"/channels/{tid}/messages/{tid}", json=msg)
            top3 = "\n".join(f"{i}. {r['headline'][:120]}" for i, r in enumerate(news[:3], 1))
            dc.req("POST", f"/channels/{tid}/messages",
                   json={"content": f"Ranking refreshed {today.strftime('%I:%M %p')}. Top 3 now:\n{top3}",
                         "allowed_mentions": {"parse": []}})
        else:
            th = dc.req("POST", f"/channels/{forum}/threads",
                        json={"name": title, "applied_tags": tag, "message": msg})
            con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, th["id"]))
            con.commit()
            pin_ranking(dc, con, forum, th["id"], key)
        log.info("Ranking posted to Discord")
    except RuntimeError as e:
        log.error("Ranking post failed: %s", e)
    return "\n".join(md)


RANKING_TITLE = re.compile(r"^TOP \d+ STORIES - ")


def pin_ranking(dc, con, forum: str, new_id: str, key: str):
    """Pin the newest Top 10 and unpin older ones. Looks in Discord itself, so it also catches rankings
    posted by another copy of the bot (for example the PC copy before moving to GitHub)."""
    ours = {r[0] for r in con.execute("SELECT v FROM meta WHERE k LIKE 'ranking_thread_%' AND k<>?", (key,))}
    other = None
    try:
        pinned = dc.pinned_in_forum(forum)
    except RuntimeError as e:
        log.warning("Could not list pinned posts (%s); unpinning only rankings this bot remembers", e)
        pinned = [{"id": i, "flags": 2} for i in ours]
    for th in pinned:
        if th["id"] == new_id:
            continue
        if th["id"] in ours or RANKING_TITLE.match(th.get("name", "")):
            try:
                dc.unpin_thread(th)
                log.info("Unpinned older ranking: %s", th.get("name", th["id"]))
            except RuntimeError as e:
                log.warning("Could not unpin older ranking %s: %s", th.get("name", th["id"]), e)
        else:
            other = th.get("name", th["id"])
    try:
        dc.req("PATCH", f"/channels/{new_id}", json={"flags": 2})
        log.info("Pinned today's ranking to the top of the forum")
    except RuntimeError as e:
        if other:
            log.warning("Could not pin the ranking: another post is pinned in the forum (%s). Discord allows "
                        "one pinned post per forum - unpin it and the next ranking will pin.", other)
        else:
            log.warning("Could not pin the ranking post (bot needs Manage Threads): %s", e)


def ranking_due(con, cfg) -> str | None:
    if not cfg["ranking"]["enabled"]:
        return None
    now = local_now(cfg)
    # newest slot that has passed today; a late start posts once, not once per missed slot
    for t in sorted(cfg["ranking"]["times"], reverse=True):
        hh, mm = (int(x) for x in t.split(":"))
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        key = f"ranking_done_{now:%Y-%m-%d}_{t}"
        if slot <= now < slot.replace(hour=23, minute=59) and \
                not con.execute("SELECT 1 FROM meta WHERE k=?", (key,)).fetchone():
            # mark every earlier slot done too
            for t2 in cfg["ranking"]["times"]:
                if t2 <= t:
                    con.execute("INSERT OR REPLACE INTO meta VALUES(?, '1')", (f"ranking_done_{now:%Y-%m-%d}_{t2}",))
            con.commit()
            return now.replace(hour=hh, minute=mm).strftime("%I:%M %p")
    return None


# ------------------------------------------------------------ exports ------
def export_json(con, cfg):
    cutoff = time.time() - cfg["general"]["group_window_hours"] * 3600
    out = []
    for c in con.execute("SELECT * FROM clusters WHERE updated>? AND summary_json IS NOT NULL ORDER BY updated DESC",
                         (cutoff,)):
        arts, sources, side = cluster_view(con, c["id"])
        out.append({"id": c["id"], "side": side, "headline": c["headline"], "topic": c["topic"],
                    "summary": json.loads(c["summary_json"]), "sources": sources,
                    "discord_thread_id": c["thread_id"],
                    "articles": [{"source": a["source"], "lean": a["lean"], "title": a["title"], "url": a["url"],
                                  "paywall": a["source"] in PAYWALL,
                                  "published": datetime.fromtimestamp(a["published"], timezone.utc).isoformat()}
                                 for a in arts]})
    (HERE / cfg["general"]["export_json"]).write_text(json.dumps(out, indent=2), encoding="utf-8")


def write_preview(con, cfg):
    lines = ["# NewsDesk preview", ""]
    rows = con.execute("SELECT * FROM clusters WHERE summary_json IS NOT NULL").fetchall()
    scored = []
    for c in rows:
        arts, sources, side = cluster_view(con, c["id"])
        scored.append((len(sources), c, arts, sources, side))
    scored.sort(key=lambda x: -x[0])
    for n, c, arts, sources, side in scored:
        summ = json.loads(c["summary_json"])
        lines.append(f"## {thread_title(summ, arts, sources, side)}")
        lines.append(f"Tags: {side} | {summ.get('topic')} | {n} sources")
        if summ.get("summary"):
            lines.append(f"> {summ['summary']}")
        for a in arts:
            lines.append(f"- [{a['lean']}] {a['source']}: {a['title']}")
        lines.append("")
    (HERE / "preview.md").write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote preview.md with %d story groups", len(rows))


# ------------------------------------------------------------------- main ---
def setup_logging(cfg):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        if getattr(h, "_newsdesk", False):
            root.removeHandler(h)
            h.close()
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh._newsdesk = True
    root.addHandler(sh)
    if cfg["general"].get("log_file"):
        fh = RotatingFileHandler(HERE / cfg["general"]["log_file"], maxBytes=2_000_000, backupCount=2,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        fh._newsdesk = True
        root.addHandler(fh)


def check(cfg) -> bool:
    """Read-only health check: token, forum channels, tags, feeds, AI backend."""
    ok = True
    def res(good, msg):
        nonlocal ok
        ok &= bool(good)
        print(redact(("  OK    " if good else "  FAIL  ") + msg))
    print("NewsDesk check")
    d = cfg["discord"]
    tok = d.get("bot_token", "")
    if not tok or "PASTE" in tok:
        res(False, "Bot token missing - paste it into config.toml [discord] bot_token")
    else:
        dc = Discord(cfg)
        try:
            me = dc.req("GET", "/users/@me")
            res(True, f"Bot token works (logged in as {me.get('username')})")
            for label, fid in (("News forum", d["news_forum_id"]), ("Satire forum", d["satire_forum_id"])):
                if label == "Satire forum" and fid == d["news_forum_id"]:
                    print("  INFO  Satire uses the news forum (satire_forum_id blank or same)")
                    continue
                try:
                    ch = dc.req("GET", f"/channels/{fid}")
                    res(ch.get("type") == 15, f"{label} '#{ch.get('name')}' is a Forum channel"
                        if ch.get("type") == 15 else f"{label} {fid} is not a Forum channel (type {ch.get('type')})")
                    names = {t["name"] for t in ch.get("available_tags", [])}
                    need = set(BASE_TAGS + TOPICS) if label == "News forum" else {"Satire"}
                    miss = need - names
                    res(not miss, "All tags present" if not miss else
                        f"Missing tags: {', '.join(sorted(miss))} - run: python newsdesk.py --setup")
                except DiscordError as e:
                    res(False, f"{label} {fid}: {e}" + (f"\n        {HELP_403}" if e.status in (403, 404) else ""))
        except DiscordError as e:
            res(False, str(e))
    feeds = load_feeds(cfg)
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(fetch_feed, feeds))
    empty = [f.name for f, items in results if not items]
    res(len(empty) <= len(feeds) // 5, f"Feeds: {len(feeds) - len(empty)}/{len(feeds)} returned stories"
        + (f" (no stories from: {', '.join(empty)})" if empty else ""))
    sm = cfg["summary"]
    if sm["backend"] == "extractive":
        print("  INFO  Summaries: extractive (no AI)")
    else:
        try:
            raw = _llm(cfg, 'Reply with only this JSON: {"ok": true}')
            res("ok" in raw, f"AI summaries reachable ({sm['backend']}, model {sm['model']})")
        except Exception as e:  # noqa: BLE001
            res(False, f"AI backend {sm['backend']} failed: {e} (NewsDesk will fall back to extractive)")
    print("All good." if ok else "Fix the FAIL lines above, then run --check again.")
    return ok


def test_post(cfg):
    dc = Discord(cfg)
    forum = cfg["discord"]["news_forum_id"]
    tags = dc.forum_tags(forum)
    th = dc.req("POST", f"/channels/{forum}/threads", json={
        "name": "NewsDesk test post - safe to delete",
        "applied_tags": [tags[t] for t in ("Single Source", "Politics") if t in tags],
        "message": {"embeds": [{"title": "NewsDesk can post here", "description":
                                "Threads, tags and embeds work. Delete this post whenever you like.",
                                "color": 0x42FCC6}]}})
    dc.req("PATCH", f"/channels/{th['id']}/messages/{th['id']}", json={"embeds": [{
        "title": "NewsDesk can post here", "color": 0x42FCC6,
        "description": "Threads, tags, embeds and edits work. Delete this post whenever you like."}]})
    dc.req("PATCH", f"/channels/{th['id']}", json={"name": "NewsDesk test post - safe to delete (edited)"})
    dc.req("POST", f"/channels/{th['id']}/messages", json={"content": "Reply test OK."})
    print("Test post created, edited and replied to. Check your forum, then delete it.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("NEWSDESK_CONFIG", str(HERE / "config.toml")))
    ap.add_argument("--setup", action="store_true", help="create forum tags in Discord")
    ap.add_argument("--check", action="store_true", help="test token, forums, tags, feeds and AI (no posting)")
    ap.add_argument("--test-post", action="store_true", help="create one test thread to confirm permissions")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="no Discord; write preview.md")
    ap.add_argument("--ranking", action="store_true", help="build and post today's ranking now, then exit")
    args = ap.parse_args()
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"No config file at {cfg_path}. Copy config.example.toml to config.toml and fill it in.")
    try:
        cfg = load_config(cfg_path)
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"config.toml has a typo: {e}. Check quotes around text values.")
    setup_logging(cfg)
    if cfg["summary"].get("note"):
        log.warning(cfg["summary"]["note"])

    if args.check:
        return 0 if check(cfg) else 1
    if args.test_post:
        test_post(cfg)
        return 0
    if args.setup:
        dc = Discord(cfg)
        news = dc.ensure_tags(cfg["discord"]["news_forum_id"], BASE_TAGS + TOPICS)
        log.info("News forum tags ready: %s", ", ".join(news))
        if cfg["discord"]["satire_forum_id"] != cfg["discord"]["news_forum_id"]:
            sat = dc.ensure_tags(cfg["discord"]["satire_forum_id"], ["Satire"])
            log.info("Satire forum tags ready: %s", ", ".join(sat))
        return 0

    if not args.dry_run and (not cfg["discord"]["bot_token"] or "PASTE" in cfg["discord"]["bot_token"]):
        sys.exit("Add your bot token to config.toml (or use --dry-run to test without Discord).")
    if args.dry_run:
        # keep previews out of the real database, otherwise previewed stories would never be posted
        db = Path(cfg["general"]["database"])
        cfg["general"]["database"] = str(db.with_name(db.stem + "-dryrun.db"))
    con = db_open(cfg)
    feeds = load_feeds(cfg)
    log.info("Loaded %d feeds (%d left, %d center, %d right, %d satire)", len(feeds),
             sum(f.lean == "left" for f in feeds), sum(f.lean == "center" for f in feeds),
             sum(f.lean == "right" for f in feeds),
             sum(f.lean == "satire" for f in feeds))
    failed = False
    while True:
        started = time.time()
        try:
            ingest(con, cfg, feeds)
            n = publish(con, cfg, dry_run=args.dry_run)
            export_json(con, cfg)
            if args.dry_run:
                write_preview(con, cfg)
            if args.ranking or args.dry_run:
                ranking_report(con, cfg, dry_run=args.dry_run, label="on demand")
            else:
                label = ranking_due(con, cfg)
                if label:
                    ranking_report(con, cfg, label=label)
            prune(con, cfg)
            log.info("Cycle done in %.0fs: %d new threads", time.time() - started, n)
            failed = False
        except Exception:  # noqa: BLE001
            log.exception("Cycle failed; will retry next cycle")
            failed = True
        if args.once or args.dry_run or args.ranking:
            break
        time.sleep(max(30, cfg["general"]["poll_minutes"] * 60 - (time.time() - started)))
    con.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
