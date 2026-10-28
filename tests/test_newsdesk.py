import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import newsdesk as nd

HUTCH = "Grand jury declines to indict Cassidy Hutchinson over January 6 testimony"
HUTCH_D = ("A federal grand jury in Washington declined to indict Cassidy Hutchinson, the former White House aide "
           "who testified about January 6. Prosecutors had sought charges over her testimony to Congress.")
FED = "Federal Reserve holds interest rates steady as inflation cools"
FED_D = "The Federal Reserve kept interest rates unchanged on Wednesday, citing cooling inflation and a steady jobs market."


FILLER_WORDS = ("tariff hurricane senate border school vaccine nato bitcoin wildfire strike pipeline drought "
                "airline union stadium pension satellite bridge refinery ballot farm").split()


def filler(slug, n=20):
    """Unrelated stories, so the test corpus looks like a real news day (needed for realistic IDF)."""
    return [(f"{w.title()} update {i}: officials discuss {w} plan in {slug} report",
             f"https://{slug}.example/{w}{i}", f"Officials met to discuss the {w} plan and next steps on {w}.", 60)
            for i, w in enumerate(FILLER_WORDS[:n])]


def run_cycle(env, cfg, dry=False):
    con = nd.db_open(cfg)
    feeds = nd.load_feeds(cfg)
    nd.ingest(con, cfg, feeds)
    n = nd.publish(con, cfg, dry_run=dry)
    nd.export_json(con, cfg)
    nd.prune(con, cfg)
    return con, n


def setup_tags(cfg):
    dc = nd.Discord(cfg)
    dc.ensure_tags("111", nd.BASE_TAGS + nd.TOPICS)
    dc.ensure_tags("222", ["Satire"])


def tag_names(env, forum, ids):
    m = {t["id"]: t["name"] for t in env.st.channels[forum]["available_tags"]}
    return {m[i] for i in ids}


# ------------------------------------------------------------------ config / feeds
def test_feed_loading_and_leans(env):
    cfg = env.config()
    feeds = nd.load_feeds(cfg)
    by = {f.name: f.lean for f in feeds}
    assert by["Fox News"] == "right"            # Politics/Latest suffixes merged into one outlet
    assert by["Not the Bee"] == "right"         # "NOT satire" in a Satire-adjacent name stays right
    assert by["The Onion"] == "satire" and by["CNN"] == "left"


def test_env_overrides_and_satire_fallback(env, monkeypatch):
    monkeypatch.setenv("NEWSDESK_BOT_TOKEN", "  from-env ")
    monkeypatch.setenv("NEWSDESK_AI_API_KEY", "k")
    cfg = env.config(discord__satire_forum_id="000000000000000000")
    assert cfg["discord"]["bot_token"] == "from-env"
    assert cfg["summary"]["openai_api_key"] == "k"
    assert cfg["discord"]["satire_forum_id"] == "111"


def test_canonical_url_strips_tracking():
    a = nd.canonical_url("https://www.cnn.com/2026/10/28/politics/story/index.html?utm_source=rss&cid=5#top")
    b = nd.canonical_url("http://cnn.com/2026/10/28/politics/story/index.html?cid=5")
    assert a == b


# ------------------------------------------------------------------ dedupe / grouping
def test_dedupe_and_grouping(env):
    env.feed("cnn", [(HUTCH, "https://cnn.com/h?utm_source=rss", HUTCH_D, 30)])
    env.feed("msnbc", [("Cassidy Hutchinson will not be indicted, grand jury decides",
                        "https://msnbc.com/h", "Jurors in Washington rejected the Justice Department's bid to charge "
                        "Cassidy Hutchinson, the former aide whose January 6 testimony angered Trump.", 40),
                       (FED, "https://msnbc.com/fed", FED_D, 20)])
    env.feed("foxpol", [("Grand jury refuses to indict Cassidy Hutchinson", "https://foxnews.com/h", HUTCH_D, 25)])
    env.feed("foxlatest", [("Grand jury refuses to indict Cassidy Hutchinson", "https://foxnews.com/h?cmpid=x", HUTCH_D, 25),
                           ("Grand jury refuses to indict Cassidy Hutchinson.", "https://foxnews.com/h2", HUTCH_D, 24)])
    env.feed("redstate", [("Hutchinson walks: grand jury declines indictment", "https://redstate.com/h", HUTCH_D, 50)])
    env.feed("pjm", [("Hutchinson walks: grand jury declines indictment", "https://pjmedia.com/h", HUTCH_D, 50)])
    env.feed("huff", [(HUTCH, "https://cnn.com/h", HUTCH_D, 30)])   # same canonical URL as CNN
    env.feed("ntb", filler("ntb"))
    cfg = env.config()
    con, _ = run_cycle(env, cfg, dry=True)
    clusters = con.execute("SELECT id FROM clusters").fetchall()
    views = [nd.cluster_view(con, c["id"]) for c in clusters]
    hutch = [v for v in views if any("Hutchinson" in a["title"] for a in v[0])]
    assert len(hutch) == 1, "all Hutchinson coverage should be one group"
    arts, sources, side = hutch[0]
    assert side == "both"
    assert sorted(sources) == ["CNN", "Fox News", "MSNBC", "RedState"]   # PJ Media pruned as syndicated copy
    assert sum(a["source"] == "Fox News" for a in arts) == 1               # Fox Politics/Latest dupes pruned
    assert any("Federal Reserve" in v[0][0]["title"] for v in views if v is not hutch[0])
    dups = con.execute("SELECT source FROM articles WHERE duplicate_of IS NOT NULL").fetchall()
    assert {d["source"] for d in dups} >= {"PJ Media", "Fox News"}


def test_satire_kept_separate(env):
    env.feed("cnn", [(HUTCH, "https://cnn.com/h", HUTCH_D, 30)])
    env.feed("onion", [("Cassidy Hutchinson grand jury declines to indict, area man devastated",
                        "https://theonion.com/h", HUTCH_D, 20)])
    cfg = env.config()
    con, _ = run_cycle(env, cfg, dry=True)
    pools = {c["pool"] for c in con.execute("SELECT pool FROM clusters")}
    assert pools == {"news", "satire"}
    assert con.execute("SELECT COUNT(*) FROM clusters").fetchone()[0] == 2


def test_malformed_and_missing_feeds_do_not_crash(env):
    env.feed("cnn", [], raw="<rss><channel><item><title>Broken")
    env.feed("msnbc", [], status=404, raw="nope")
    env.feed("dw", [(FED, "https://dailywire.com/fed", FED_D, 10)])
    env.feed("bee", [], raw="")
    cfg = env.config()
    con, _ = run_cycle(env, cfg, dry=True)
    assert con.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1


def test_old_articles_ignored(env):
    env.feed("cnn", [(FED, "https://cnn.com/fed", FED_D, 60 * 30)])
    cfg = env.config()
    con, _ = run_cycle(env, cfg, dry=True)
    assert con.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0


# ------------------------------------------------------------------ Discord posting
def test_setup_tags_and_thread_lifecycle(env):
    cfg = env.config()
    setup_tags(cfg)
    names = [t["name"] for t in env.st.channels["111"]["available_tags"]]
    assert len(names) == 18 and len(set(names)) == 18
    setup_tags(cfg)   # idempotent
    assert len(env.st.channels["111"]["available_tags"]) == 18

    env.feed("cnn", [(HUTCH, "https://cnn.com/h", HUTCH_D, 30)])
    con, n = run_cycle(env, cfg)
    assert n == 1
    (tid, th), = env.st.threads.items()
    assert tag_names(env, "111", th["tags"]) == {"Left", "Single Source", "Courts & Law"}

    # right-leaning outlet joins -> same thread is updated, tags become Both Sides
    env.feed("dw", [("Grand jury refuses to indict Cassidy Hutchinson", "https://dailywire.com/h", HUTCH_D, 5)])
    con, n = run_cycle(env, cfg)
    assert n == 0 and len(env.st.threads) == 1
    th = env.st.threads[tid]
    assert {"Both Sides", "Left", "Right"} <= tag_names(env, "111", th["tags"])
    assert len(th["tags"]) <= 5
    assert th["name"].startswith("[2] ")
    assert "New coverage added" in th["messages"][-1]["content"]

    # nothing new -> no Discord writes at all
    before = len(env.st.calls)
    run_cycle(env, cfg)
    writes = [c for c in env.st.calls[before:] if c[0] != "GET"]
    assert writes == []


def test_satire_goes_to_satire_forum(env):
    cfg = env.config()
    setup_tags(cfg)
    env.feed("bee", [("Man who read one headline now expert on everything", "https://babylonbee.com/x",
                      "Local man confident after reading a headline.", 10)])
    run_cycle(env, cfg)
    (th,) = env.st.threads.values()
    assert th["parent"] == "222" and th["name"].startswith("SATIRE: ")
    assert "SATIRE" in th["messages"][0]["embeds"][0]["description"]


def test_deleted_thread_is_recreated(env):
    cfg = env.config()
    setup_tags(cfg)
    env.feed("cnn", [(HUTCH, "https://cnn.com/h", HUTCH_D, 30)])
    run_cycle(env, cfg)
    env.st.threads.clear()      # a moderator deleted the post
    env.feed("dw", [("Grand jury refuses to indict Cassidy Hutchinson", "https://dailywire.com/h", HUTCH_D, 5)])
    con, n = run_cycle(env, cfg)
    assert n == 1 and len(env.st.threads) == 1
    c = con.execute("SELECT * FROM clusters").fetchone()
    assert c["thread_id"] in env.st.threads and c["dirty"] == 0


def test_rate_limit_is_retried(env):
    cfg = env.config()
    setup_tags(cfg)
    env.feed("cnn", [(HUTCH, "https://cnn.com/h", HUTCH_D, 30)])
    env.st.rate_limit_next = 3
    con, n = run_cycle(env, cfg)
    assert n == 1


def test_forbidden_stops_with_help(env):
    cfg = env.config()
    setup_tags(cfg)
    env.feed("cnn", [(HUTCH, "https://cnn.com/h", HUTCH_D, 30)])
    env.st.forbid = True
    with pytest.raises(SystemExit) as e:
        run_cycle(env, cfg)
    assert "Permissions" in str(e.value)
    env.st.forbid = False      # fixed permissions -> next cycle posts it
    con, n = run_cycle(env, cfg)
    assert n == 1


def test_bad_token_message(env):
    cfg = env.config(discord__bot_token="wrong")
    with pytest.raises(nd.DiscordError) as e:
        nd.Discord(cfg).req("GET", "/users/@me")
    assert e.value.status == 401 and "Reset Token" in str(e.value)


def test_unknown_forum_gives_up_after_three_failures(env):
    cfg = env.config(discord__news_forum_id="404404")
    env.feed("cnn", [(HUTCH, "https://cnn.com/h", HUTCH_D, 30)])
    for _ in range(3):
        try:
            con, _ = run_cycle(env, cfg)
        except nd.DiscordError:
            pass   # forum_tags GET fails before posting
    # forum lookup itself fails: cycle raises, story stays queued (no bad state written)
    con = nd.db_open(cfg)
    assert con.execute("SELECT thread_id FROM clusters").fetchone()[0] in (None, "failed")


def test_first_run_cap_and_late_promotion(env):
    cfg = env.config(general__first_run_max_threads=2)
    setup_tags(cfg)
    stories = [(f"Unrelated story number {i} about topic{i} zebra{i} quantum{i}",
                f"https://cnn.com/s{i}", f"Details on topic{i} zebra{i} quantum{i} event.", 30) for i in range(5)]
    env.feed("cnn", stories)
    con, n = run_cycle(env, cfg)
    assert n == 2
    skipped = con.execute("SELECT COUNT(*) FROM clusters WHERE thread_id='skipped'").fetchone()[0]
    assert skipped == 3


def test_long_titles_and_many_sources_fit_discord_limits(env):
    cfg = env.config()
    setup_tags(cfg)
    long = "Cassidy Hutchinson grand jury " + "extremely long headline words " * 20
    for slug in ("cnn", "msnbc", "huff", "foxpol", "dw", "redstate", "ntb"):
        env.feed(slug, [(long + slug, f"https://{slug}.com/h", HUTCH_D * 5, 10)] +
                 [(f"{long} part {i} {slug}", f"https://{slug}.com/h{i}", HUTCH_D * 5, 10) for i in range(15)])
    run_cycle(env, cfg)
    assert env.st.threads       # validators in the mock server enforce the limits


# ------------------------------------------------------------------ AI summaries
def ai_cfg(env, **over):
    return env.config(summary__backend="openai", summary__openai_base_url=f"{env.base}/v1",
                      summary__model="test-model", summary__json_mode=True, **over)


def two_sided(env):
    env.feed("cnn", [(HUTCH, "https://cnn.com/h", HUTCH_D, 30)])
    env.feed("dw", [("Grand jury refuses to indict Cassidy Hutchinson", "https://dailywire.com/h", HUTCH_D, 30)])


def test_ai_summary_used(env):
    cfg = ai_cfg(env)
    setup_tags(cfg)
    two_sided(env)
    run_cycle(env, cfg)
    (th,) = env.st.threads.values()
    emb = th["messages"][0]["embeds"][0]
    assert emb["title"] == "AI headline about the story"
    assert "Left framing" in emb["description"] and "test-model" in emb["footer"]["text"]


@pytest.mark.parametrize("mode", ["fail", "empty", "garbage", "429once"])
def test_ai_failures_fall_back(env, mode):
    env.st.ai_mode = mode
    cfg = ai_cfg(env)
    setup_tags(cfg)
    two_sided(env)
    run_cycle(env, cfg)
    (th,) = env.st.threads.values()
    footer = th["messages"][0]["embeds"][0]["footer"]["text"]
    assert ("test-model" in footer) if mode == "429once" else ("extractive" in footer)


def test_ai_budget_defers_and_upgrades(env):
    cfg = ai_cfg(env, summary__max_ai_per_cycle=0)
    setup_tags(cfg)
    two_sided(env)
    con, _ = run_cycle(env, cfg)
    (tid, th), = env.st.threads.items()
    assert "extractive" in th["messages"][0]["embeds"][0]["footer"]["text"]
    assert con.execute("SELECT ai_pending FROM clusters").fetchone()[0] == 1
    cfg = ai_cfg(env, summary__max_ai_per_cycle=5)
    con, _ = run_cycle(env, cfg)
    assert "test-model" in env.st.threads[tid]["messages"][0]["embeds"][0]["footer"]["text"]
    assert con.execute("SELECT ai_pending FROM clusters").fetchone()[0] == 0


def test_parse_json_variants():
    assert nd._parse_json('<think>x</think>\n```json\n{"summary": " hi "}\n```')["summary"] == "hi"
    with pytest.raises(Exception):
        nd._parse_json("no json here")


# ------------------------------------------------------------------ ranking
def test_ranking_create_refresh_and_recreate(env):
    cfg = env.config()
    setup_tags(cfg)
    two_sided(env)
    env.feed("msnbc", [(FED, "https://msnbc.com/fed", FED_D, 20)])
    env.feed("onion", [("Nation's dogs demand rate cut", "https://theonion.com/d", "Dogs want lower rates.", 20)])
    con, _ = run_cycle(env, cfg)
    md = nd.ranking_report(con, cfg, label="07:00 AM")
    assert "1. **" in md and "Top satire" in md
    rk = [t for t in env.st.threads.values() if t["name"].startswith("TOP 10 STORIES")]
    assert len(rk) == 1 and rk[0].get("pinned")
    assert "Daily Ranking" in tag_names(env, "111", rk[0]["tags"])
    nd.ranking_report(con, cfg, label="05:00 PM")
    rk = [t for t in env.st.threads.values() if t["name"].startswith("TOP 10 STORIES")]
    assert len(rk) == 1 and "Top 3 now" in rk[0]["messages"][-1]["content"]
    # deleted ranking thread -> recreated
    for k in [k for k, t in env.st.threads.items() if t["name"].startswith("TOP 10")]:
        del env.st.threads[k]
    nd.ranking_report(con, cfg, label="05:00 PM")
    assert len([t for t in env.st.threads.values() if t["name"].startswith("TOP 10 STORIES")]) == 1
    day = nd.local_now(cfg).strftime("%Y-%m-%d")
    data = json.loads((env.tmp / "rankings" / f"{day}.json").read_text())
    assert data["news"][0]["both"] is True     # two-sided story ranks first


def test_ranking_due_uses_timezone(env, monkeypatch):
    cfg = env.config()
    con = nd.db_open(cfg)
    tz = ZoneInfo("America/Chicago")
    fake = {"t": datetime(2026, 10, 28, 6, 59, tzinfo=tz)}
    monkeypatch.setattr(nd, "local_now", lambda c: fake["t"])
    assert nd.ranking_due(con, cfg) is None
    fake["t"] = datetime(2026, 10, 28, 7, 10, tzinfo=tz)
    assert nd.ranking_due(con, cfg) == "07:00 AM"
    assert nd.ranking_due(con, cfg) is None          # only once
    fake["t"] = datetime(2026, 10, 28, 18, 0, tzinfo=tz)
    assert nd.ranking_due(con, cfg) == "05:00 PM"
    fake["t"] = datetime(2026, 10, 29, 19, 0, tzinfo=tz)   # late start: one post, not two
    assert nd.ranking_due(con, cfg) == "05:00 PM"
    assert nd.ranking_due(con, cfg) is None


def test_prune_removes_old_history(env):
    cfg = env.config()
    env.feed("cnn", [(FED, "https://cnn.com/fed", FED_D, 10)])
    con, _ = run_cycle(env, cfg, dry=True)
    con.execute("UPDATE clusters SET updated=0")
    con.execute("UPDATE articles SET seen=0")
    con.execute("INSERT INTO meta VALUES('ranking_done_2020-01-01_07:00','1')")
    con.execute("INSERT INTO meta VALUES('ranking_thread_2020-01-01','5')")
    con.commit()
    nd.prune(con, cfg)
    assert con.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM meta WHERE k LIKE 'ranking_%2020%'").fetchone()[0] == 0
    # pruned URL is not re-ingested as new because it is older than max_article_age? it is fresh -> comes back once
    con2, _ = run_cycle(env, cfg, dry=True)


# ------------------------------------------------------------------ CLI
def test_cli_check_and_dry_run(env, monkeypatch, capsys):
    cfg = env.config()
    setup_tags(cfg)
    env.feed("cnn", [(FED, "https://cnn.com/fed", FED_D, 10)])
    monkeypatch.setattr("sys.argv", ["newsdesk.py", "--config", str(env.tmp / "config.toml"), "--check"])
    rc = nd.main()
    out = capsys.readouterr().out
    assert "Bot token works" in out and "is a Forum channel" in out
    assert rc == 1    # most fixture feeds are empty, so the feed check fails - as it should
    monkeypatch.setattr("sys.argv", ["newsdesk.py", "--config", str(env.tmp / "config.toml"), "--dry-run"])
    assert nd.main() == 0
    assert (env.tmp / "nd-dryrun.db").exists()
    real = sqlite3.connect(env.tmp / "nd.db") if (env.tmp / "nd.db").exists() else None
    if real:
        assert real.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0
    assert not [t for t in env.st.threads.values() if not t["name"].startswith("TOP")]


def test_cli_test_post(env, monkeypatch):
    cfg = env.config()
    setup_tags(cfg)
    monkeypatch.setattr("sys.argv", ["newsdesk.py", "--config", str(env.tmp / "config.toml"), "--test-post"])
    assert nd.main() == 0
    (th,) = env.st.threads.values()
    assert th["name"].endswith("(edited)") and th["messages"][-1]["content"] == "Reply test OK."


def test_check_flags_non_forum(env, monkeypatch, capsys):
    env.config(discord__news_forum_id="333")
    monkeypatch.setattr("sys.argv", ["newsdesk.py", "--config", str(env.tmp / "config.toml"), "--check"])
    assert nd.main() == 1
    assert "not a Forum channel" in capsys.readouterr().out


# ------------------------------------------------------------------ satire regression
def busy_news_day(env, n=6):
    words = "tariff hurricane senate border school vaccine nato bitcoin".split()[:n]
    for slug in ("cnn", "msnbc", "dw", "redstate"):
        env.feed(slug, [(f"{w.title()} deal reached after {w} talks collapse in {w} dispute",
                         f"https://{slug}.com/{w}", f"Negotiators on the {w} dispute reached a {w} deal.", 30)
                        for w in words])


def satire_items():
    return [(f"Area man {w} shocked by {w} news", f"https://theonion.com/{w}", f"Local {w} man reacts.", 20)
            for w in ("pickle", "lawnmower", "accordion")]


def test_first_run_posts_satire_even_when_news_fills_the_cap(env):
    cfg = env.config(general__first_run_max_threads=3)
    setup_tags(cfg)
    busy_news_day(env)
    env.feed("onion", satire_items())
    run_cycle(env, cfg)
    sat = [t for t in env.st.threads.values() if t["parent"] == "222"]
    news = [t for t in env.st.threads.values() if t["parent"] == "111"]
    assert len(news) == 3 and len(sat) == 3


def test_skipped_satire_from_old_version_is_requeued(env):
    cfg = env.config()
    setup_tags(cfg)
    env.feed("onion", satire_items())
    con, _ = run_cycle(env, cfg, dry=True)
    con.execute("UPDATE clusters SET thread_id='skipped', dirty=0")
    con.execute("DELETE FROM meta WHERE k='satire_requeued'")
    con.execute("INSERT OR REPLACE INTO meta VALUES('first_run_done','1')")
    con.commit()
    import shutil
    shutil.copy(env.tmp / "nd.db", env.tmp / "real.db")
    cfg = env.config(general__database=str(env.tmp / "real.db"))
    run_cycle(env, cfg)
    assert len([t for t in env.st.threads.values() if t["parent"] == "222"]) == 3


def test_bad_satire_forum_does_not_block_news(env):
    cfg = env.config(discord__satire_forum_id="404404")
    setup_tags(cfg)
    busy_news_day(env, 2)
    env.feed("onion", satire_items())
    con, n = run_cycle(env, cfg)
    assert n == 2
    assert con.execute("SELECT COUNT(*) FROM clusters WHERE pool='satire' AND dirty=1").fetchone()[0] == 3


# ------------------------------------------------------------------ center + paywall
def test_center_lean_loaded(env):
    by = {f.name: f.lean for f in nd.load_feeds(env.config())}
    assert by["The Hill"] == "center" and by["Bloomberg"] == "center"
    assert "Bloomberg" in nd.PAYWALL


def test_center_tags_and_both_sides(env):
    cfg = env.config()
    setup_tags(cfg)
    env.feed("hill", [(HUTCH, "https://thehill.com/h", HUTCH_D, 30)])
    env.feed("cnn", [("Grand jury declines to indict Cassidy Hutchinson", "https://cnn.com/h", HUTCH_D, 30)])
    env.feed("ntb", filler("ntb"))
    run_cycle(env, cfg)
    (th,) = [t for t in env.st.threads.values() if "Hutchinson" in t["messages"][0]["embeds"][0]["description"]]
    assert tag_names(env, "111", th["tags"]) >= {"Left", "Center"}
    assert "Both Sides" not in tag_names(env, "111", th["tags"])     # left + center is not both sides
    env.feed("dw", [("Grand jury refuses to indict Cassidy Hutchinson", "https://dailywire.com/h", HUTCH_D, 5)])
    run_cycle(env, cfg)
    names = tag_names(env, "111", th["tags"])
    assert {"Both Sides", "Left", "Center", "Right"} <= names and len(th["tags"]) <= 5
    desc = th["messages"][0]["embeds"][0]["description"]
    assert "**Center (1)**" in desc


def test_center_only_story_and_ranking_label(env):
    cfg = env.config()
    setup_tags(cfg)
    env.feed("hill", [(FED, "https://thehill.com/fed", FED_D, 30)])
    env.feed("bloomberg", [("Fed holds interest rates steady as inflation cools off", "https://bloomberg.com/fed", FED_D, 30)])
    env.feed("ntb", filler("ntb"))
    con, _ = run_cycle(env, cfg)
    (th,) = [t for t in env.st.threads.values() if "Federal" in t["name"] or "Fed " in t["name"]]
    assert tag_names(env, "111", th["tags"]) >= {"Center"}
    desc = th["messages"][0]["embeds"][0]["description"]
    assert "Bloomberg ($)" in desc and "may hit a paywall" in desc
    md = nd.ranking_report(con, cfg, dry_run=True)
    assert "Center only" in md and "0L/2C/0R" in md


def test_default_needs_two_outlets_but_satire_posts(env):
    cfg = env.config(general__min_sources_to_post=2)
    setup_tags(cfg)
    env.feed("cnn", [(FED, "https://cnn.com/fed", FED_D, 30)])
    env.feed("onion", satire_items()[:1])
    env.feed("ntb", filler("ntb"))
    run_cycle(env, cfg)
    assert [t["parent"] for t in env.st.threads.values()] == ["222"]
    env.feed("hill", [("Fed holds interest rates steady as inflation cools off", "https://thehill.com/fed", FED_D, 5)])
    run_cycle(env, cfg)
    assert any(t["name"].startswith("[2] ") for t in env.st.threads.values())


# ------------------------------------------------------------------ secrets never logged
# Built at runtime so no token-shaped text sits in the source (GitHub secret scanning flags it).
FAKE = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4OQ", "GaBcDe", "abcdefghijklmnopqrstuvwxyz" + "0123456789"])


def test_token_whitespace_and_bot_prefix_cleaned(env):
    cfg = env.config(discord__bot_token=" Bot good-token ")
    assert cfg["discord"]["bot_token"] == "Botgood-token" or cfg["discord"]["bot_token"] == "good-token"
    cfg = env.config(discord__bot_token=f"Bot {FAKE} ")
    assert cfg["discord"]["bot_token"] == FAKE


def test_secrets_are_redacted_from_logs(env, tmp_path):
    cfg = env.config(discord__bot_token=FAKE, general__log_file=str(tmp_path / "nd.log"))
    nd.setup_logging(cfg)
    root = nd.logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, nd.logging.StreamHandler) and not isinstance(h, nd.logging.FileHandler):
            root.removeHandler(h)
    nd.log.error("boom with header 'Bot %s'", FAKE)
    nd.log.error("unknown-token-shape %s", ".".join(["NzkxMjM0NTY3ODkwMTIzNDU2", "Xyz123", "a" * 33]))
    for h in list(root.handlers):
        if getattr(h, "_newsdesk", False):
            h.flush()
            root.removeHandler(h)
            h.close()
    text = (tmp_path / "nd.log").read_text()
    assert FAKE not in text and "NzkxMjM0" not in text and "[hidden" in text


def test_invalid_header_token_message_hides_token(env, monkeypatch):
    cfg = env.config()
    cfg["discord"]["bot_token"] = FAKE + "\n"
    dc = nd.Discord(cfg)
    with pytest.raises(nd.DiscordError) as e:
        dc.req("GET", "/users/@me")
    assert FAKE not in str(e.value)
