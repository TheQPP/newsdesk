"""In-process fake Discord API + fake OpenAI-compatible API + static RSS host, for tests."""
import itertools
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class State:
    def __init__(self):
        self.ids = itertools.count(5000)
        self.calls = []            # (method, path, body)
        self.channels = {"111": {"id": "111", "type": 15, "name": "news", "guild_id": "999", "available_tags": []},
                         "222": {"id": "222", "type": 15, "name": "satire", "guild_id": "999", "available_tags": []},
                         "333": {"id": "333", "type": 0, "name": "general", "guild_id": "999"}}
        self.threads = {}          # id -> {"name", "tags", "messages": [...], "parent"}
        self.feeds = {}            # path -> (status, body)
        self.rate_limit_next = 0   # number of upcoming Discord requests to answer with 429
        self.forbid = False        # answer 403 to thread creation
        self.ai_mode = "ok"        # ok | fail | empty | 429once | garbage
        self.ai_calls = 0
        self.errors = []           # validation problems found in requests

    def check(self, cond, msg):
        if not cond:
            self.errors.append(msg)


def make_handler(st: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200, headers=None):
            b = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw) if raw else {}

        def _validate_msg(self, m):
            if "content" in m:
                st.check(len(m["content"]) <= 2000, f"content too long {len(m['content'])}")
            for e in m.get("embeds", []):
                st.check(len(e.get("title", "")) <= 256, "embed title > 256")
                st.check(len(e.get("description", "")) <= 4096, "embed description > 4096")
                total = len(e.get("title", "")) + len(e.get("description", "")) + len(e.get("footer", {}).get("text", ""))
                st.check(total <= 6000, "embed total > 6000")
                st.check(e.get("title", "x") != "", "empty embed title")

        def _discord(self, method):
            path = self.path[len("/api"):]
            body = self._body() if method in ("POST", "PATCH", "PUT") else {}
            st.calls.append((method, path, body))
            if self.headers.get("Authorization") != "Bot good-token":
                return self._send({"message": "401: Unauthorized", "code": 0}, 401)
            if st.rate_limit_next > 0:
                st.rate_limit_next -= 1
                return self._send({"message": "You are being rate limited.", "retry_after": 0.01, "global": False}, 429)
            parts = path.strip("/").split("/")
            if path == "/users/@me":
                return self._send({"id": "42", "username": "NewsDesk"})
            if parts[0] != "channels":
                return self._send({"message": "Unknown"}, 404)
            cid = parts[1]
            if len(parts) == 2:
                if cid in st.channels:
                    ch = st.channels[cid]
                    if method == "PATCH" and "available_tags" in body:
                        tags = body["available_tags"]
                        st.check(len(tags) <= 20, "more than 20 forum tags")
                        for t in tags:
                            t.setdefault("id", str(next(st.ids)))
                        ch["available_tags"] = tags
                    return self._send(ch)
                if cid in st.threads:
                    th = st.threads[cid]
                    if method == "PATCH":
                        if "flags" in body:
                            th["pinned"] = bool(body["flags"] & 2)
                        if "name" in body:
                            st.check(1 <= len(body["name"]) <= 100, f"bad thread name length {len(body['name'])}")
                            th["name"] = body["name"]
                        if "applied_tags" in body:
                            st.check(len(body["applied_tags"]) <= 5, "more than 5 tags")
                            th["tags"] = body["applied_tags"]
                    return self._send({"id": cid, "type": 11, "parent_id": th["parent"]})
                return self._send({"message": "Unknown Channel", "code": 10003}, 404)
            sub = parts[2]
            if sub == "threads" and method == "POST":
                if cid not in st.channels:
                    return self._send({"message": "Unknown Channel", "code": 10003}, 404)
                if st.forbid:
                    return self._send({"message": "Missing Permissions", "code": 50013}, 403)
                name = body.get("name", "")
                st.check(1 <= len(name) <= 100, f"bad thread name length {len(name)}")
                st.check(len(body.get("applied_tags", [])) <= 5, "more than 5 tags")
                valid = {t["id"] for t in st.channels[cid].get("available_tags", [])}
                st.check(all(t in valid for t in body.get("applied_tags", [])), "unknown tag id")
                self._validate_msg(body.get("message", {}))
                tid = str(next(st.ids))
                st.threads[tid] = {"name": name, "tags": body.get("applied_tags", []), "parent": cid,
                                   "messages": [body.get("message")]}
                return self._send({"id": tid, "name": name})
            if cid not in st.threads:
                return self._send({"message": "Unknown Channel", "code": 10003}, 404)
            th = st.threads[cid]
            if sub == "messages" and method == "POST":
                self._validate_msg(body)
                th["messages"].append(body)
                return self._send({"id": str(next(st.ids))})
            if sub == "messages" and method == "PATCH":
                self._validate_msg(body)
                th["messages"][0] = body
                return self._send({"id": parts[3]})
            if sub == "pins" and method == "PUT":
                th["pinned"] = True
                return self._send(None, 204)
            return self._send({"message": "Unknown"}, 404)

        def _ai(self):
            self._body()
            st.ai_calls += 1
            if st.ai_mode == "fail":
                return self._send({"error": "boom"}, 500)
            if st.ai_mode == "429once" and st.ai_calls == 1:
                return self._send({"error": "slow down"}, 429, {"retry-after": "0"})
            if st.ai_mode == "empty":
                return self._send({"choices": [{"message": {"content": ""}}]})
            if st.ai_mode == "garbage":
                return self._send({"choices": [{"message": {"content": "I cannot help with that."}}]})
            c = {"headline": "AI headline about the story", "summary": "A neutral AI summary.",
                 "left_angle": "Left frames it one way.", "right_angle": "Right frames it another.",
                 "disputed": "", "topic": "Courts & Law"}
            return self._send({"choices": [{"message": {"content": "<think>hmm</think>```json\n" + json.dumps(c) + "\n```"}}]})

        def do_GET(self):
            if self.path.startswith("/feeds/"):
                status, body = st.feeds.get(self.path, (404, "not found"))
                b = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/rss+xml")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            return self._discord("GET")

        def do_POST(self):
            if self.path == "/v1/chat/completions":
                return self._ai()
            return self._discord("POST")

        def do_PATCH(self):
            return self._discord("PATCH")

        def do_PUT(self):
            return self._discord("PUT")
    return H


def start():
    st = State()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st
