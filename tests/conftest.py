import sys
import time
from email.utils import formatdate
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import newsdesk  # noqa: E402
from mockserver import start  # noqa: E402

FEEDS = [("Left", [("CNN", "cnn"), ("MSNBC", "msnbc"), ("HuffPost", "huff")]),
         ("Center (AllSides-rated Center)", [("The Hill", "hill"), ("Bloomberg [PAYWALL]", "bloomberg")]),
         ("Right", [("Fox News Politics", "foxpol"), ("Fox News Latest", "foxlatest"),
                    ("Daily Wire", "dw"), ("RedState", "redstate"), ("PJ Media", "pjm"),
                    ("Not the Bee (news, NOT satire)", "ntb")]),
         ("Satire", [("The Onion", "onion"), ("Babylon Bee", "bee")])]


def rss(items):
    out = ['<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>']
    for it in items:
        title, link, desc, mins = it[:4]
        guid = it[4] if len(it) > 4 else link
        out.append(f"<item><title>{escape(title)}</title><link>{escape(link)}</link>"
                   f"<guid>{escape(guid)}</guid><description>{escape(desc)}</description>"
                   f"<pubDate>{formatdate(time.time() - mins * 60)}</pubDate></item>")
    out.append("</channel></rss>")
    return "".join(out)


class Env:
    def __init__(self, tmp, srv, st):
        self.tmp, self.srv, self.st = tmp, srv, st
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"
        for _, feeds in FEEDS:
            for _, slug in feeds:
                st.feeds[f"/feeds/{slug}"] = (200, rss([]))
        body = ['<?xml version="1.0"?><opml version="2.0"><head><title>t</title></head><body>']
        for folder, feeds in FEEDS:
            body.append(f'<outline text="{folder}">')
            for name, slug in feeds:
                body.append(f'<outline type="rss" text="{escape(name)}" title="{escape(name)}" '
                            f'xmlUrl="{self.base}/feeds/{slug}"/>')
            body.append("</outline>")
        body.append("</body></opml>")
        (tmp / "feeds.opml").write_text("".join(body), encoding="utf-8")

    def feed(self, slug, items, status=200, raw=None):
        self.st.feeds[f"/feeds/{slug}"] = (status, raw if raw is not None else rss(items))

    def config(self, **over):
        sections = {
            "discord": {"bot_token": "good-token", "news_forum_id": "111", "satire_forum_id": "222",
                        "api_base": f"{self.base}/api"},
            "general": {"opml": str(self.tmp / "feeds.opml"), "database": str(self.tmp / "nd.db"),
                        "hold_minutes": 0, "export_json": str(self.tmp / "stories.json"),
                        "log_file": "", "timezone": "America/Chicago", "min_sources_to_post": 1},
            "ranking": {"folder": str(self.tmp / "rankings"), "times": ["07:00", "17:00"]},
            "summary": {"backend": "extractive"},
            "source_overrides": {"Not the Bee": "right"},
        }
        for k, v in over.items():
            sec, key = k.split("__")
            sections[sec][key] = v
        lines = []
        for sec, vals in sections.items():
            lines.append(f"[{sec}]")
            for key, v in vals.items():
                key = f'"{key}"'
                if isinstance(v, bool):
                    lines.append(f"{key} = {'true' if v else 'false'}")
                elif isinstance(v, (int, float)):
                    lines.append(f"{key} = {v}")
                elif isinstance(v, list):
                    lines.append(f"{key} = [{', '.join(repr(x).replace(chr(39), chr(34)) for x in v)}]")
                else:
                    lines.append(f'{key} = "{v}"')
        p = self.tmp / "config.toml"
        p.write_text("\n".join(lines), encoding="utf-8")
        return newsdesk.load_config(p)

    def threads(self):
        return self.st.threads


@pytest.fixture
def env(tmp_path, monkeypatch):
    srv, st = start()
    monkeypatch.setattr(newsdesk.time, "sleep", lambda s: None)
    for var in ("NEWSDESK_BOT_TOKEN", "DISCORD_BOT_TOKEN", "NEWSDESK_NEWS_FORUM_ID",
                "NEWSDESK_SATIRE_FORUM_ID", "NEWSDESK_AI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    e = Env(tmp_path, srv, st)
    yield e
    srv.shutdown()
    assert not st.errors, st.errors
