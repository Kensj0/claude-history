"""Claude history: lists earlier Claude Code sessions over localhost HTTP.

Runs on its own with `python3 server.py`. Any client can use it, a panel in Jarri is optional.

Endpoints (JSON):
  GET /sessions -> [{id, title, cwd, messages, modified}, ...], newest first

Field meanings:
  id        session id (the transcript file name without .jsonl)
  title     last "ai-title" in the transcript, else last "last-prompt",
            else the first user prompt, else the id
  cwd       working directory from the first record that has one, or null
  messages  number of user records with typed text (tool results excluded)
  modified  transcript file mtime in milliseconds since the epoch

Transcripts are read line by line and cached per file by (mtime, size), so a
refresh only re-reads sessions that changed. Broken lines are skipped.

Security: binds to 127.0.0.1 only. Requests are accepted only when the Origin
and Host headers match an allowed list, so ordinary web pages in a browser
cannot read the history.
"""

import glob
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = int(os.environ.get("JARRI_CLAUDE_HISTORY_PORT", "8784"))
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
TITLE_LIMIT = 120
ALLOWED_ORIGINS = {
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:8792",  # Jarri dev server (devUrl in tauri.conf.json)
    "http://127.0.0.1:8792",
}
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}

CACHE = {}  # path -> ((mtime_ns, size), summary)
CACHE_LOCK = threading.Lock()


def clip(text):
    text = " ".join(text.split())
    return text if len(text) <= TITLE_LIMIT else text[: TITLE_LIMIT - 1] + "…"


def prompt_text(record):
    """Return the typed text of a user record, or None for tool results."""
    content = record.get("message", {}).get("content")
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part.strip())
        return text or None
    return None


def summarize(path):
    session_id = os.path.basename(path)[: -len(".jsonl")]
    ai_title = None
    last_prompt = None
    first_prompt = None
    cwd = None
    messages = 0

    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if cwd is None and isinstance(record.get("cwd"), str):
                cwd = record["cwd"]
            kind = record.get("type")
            if kind == "ai-title" and isinstance(record.get("aiTitle"), str):
                ai_title = record["aiTitle"]
            elif kind == "last-prompt" and isinstance(record.get("lastPrompt"), str):
                last_prompt = record["lastPrompt"]
            elif kind == "user":
                text = prompt_text(record)
                if text is not None:
                    messages += 1
                    if first_prompt is None:
                        first_prompt = text

    title = ai_title or last_prompt or first_prompt or session_id
    return {
        "id": session_id,
        "title": clip(title),
        "cwd": cwd,
        "messages": messages,
    }


def list_sessions():
    sessions = []
    seen = set()
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        key = (stat.st_mtime_ns, stat.st_size)
        seen.add(path)
        with CACHE_LOCK:
            cached = CACHE.get(path)
        if cached and cached[0] == key:
            summary = cached[1]
        else:
            try:
                summary = summarize(path)
            except OSError:
                continue
            with CACHE_LOCK:
                CACHE[path] = (key, summary)
        sessions.append({**summary, "modified": stat.st_mtime_ns // 1_000_000})

    with CACHE_LOCK:
        for path in list(CACHE):
            if path not in seen:
                del CACHE[path]

    sessions.sort(key=lambda session: session["modified"], reverse=True)
    return sessions


class Handler(BaseHTTPRequestHandler):
    server_version = "jarri-claude-history"

    def log_message(self, fmt, *args):
        pass

    def _allowed(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return False
        return self.headers.get("Host") in ALLOWED_HOSTS

    def _send(self, status, body=None):
        self.send_response(status)
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if body is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        self._send(204)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden origin"})
        if urlparse(self.path).path != "/sessions":
            return self._send(404, {"error": "not found"})
        self._send(200, list_sessions())


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"claude history service on http://{HOST}:{PORT}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
