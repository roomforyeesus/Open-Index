from __future__ import annotations

import ipaddress
import json
import math
import re
import sqlite3
import threading
import time
from collections import Counter, deque
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "search.db"
HOST = "127.0.0.1"
PORT = 8765
USER_AGENT = "OpenIndexBot"
MAX_SEEDS = 8
MAX_PAGE_BYTES = 2_000_000
# MAX_PAGE_BYTES = 2_000_000  # Disabled: do not cap fetched page size.
TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
RESTRICTED_CRAWL_SUFFIXES = ("craigslist.org", "craiglist.org")
SKIP_EXTENSIONS = (
    ".7z", ".avi", ".css", ".csv", ".doc", ".docx", ".gif", ".gz",
    ".ico", ".jpeg", ".jpg", ".js", ".json", ".mp3", ".mp4", ".pdf",
    ".png", ".ppt", ".pptx", ".rar", ".svg", ".tar", ".webp", ".xls",
    ".xlsx", ".xml", ".zip",
)

STATE_LOCK = threading.Lock()
CRAWL_STOP = threading.Event()
STATE = {
    "state": "ready",
    "done": 0,
    "limit": 100,
    "message": "Add a few starting URLs to build your index.",
    "error": "",
}


def connect_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    with connect_db() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            """CREATE TABLE IF NOT EXISTS pages (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )


def normalize_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("Enter at least one starting URL.")
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"That does not look like a web address: {raw}")
    if parsed.username or parsed.password:
        raise ValueError("Web addresses cannot include a username or password.")
    host = parsed.hostname.lower().rstrip(".")
    if any(host == suffix or host.endswith("." + suffix) for suffix in RESTRICTED_CRAWL_SUFFIXES):
        raise ValueError(
            "Open Index does not crawl Craigslist. Its Terms of Use prohibit automated collection. "
            "Also, Craigslist is spelled with an 's': craigslist.org."
        )
    if host == "localhost" or host.endswith((".localhost", ".local", ".test")):
        raise ValueError("Use a public website address, not a local network address.")
    try:
        address = ipaddress.ip_address(host)
        if not address.is_global:
            raise ValueError("Use a publicly reachable website address.")
    except ValueError as exc:
        if "does not appear to be an IPv4 or IPv6 address" not in str(exc):
            raise
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


class TextPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description = ""
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self.in_title = False
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
        elif tag == "meta" and values.get("name", "").lower() == "description":
            self.description = values.get("content") or ""
        elif tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")
        elif tag in {"br", "p", "div", "li", "h1", "h2", "h3", "section", "article"}:
            self.text_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg", "template"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "section", "article"}:
            self.text_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)


class SameHostRedirect(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, request, fp, code, message, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme not in {"http", "https"} or (target.hostname or "").lower() not in self.allowed_hosts:
            return None
        return super().redirect_request(request, fp, code, message, headers, newurl)


def robots_can_fetch(url: str, cache: dict[str, tuple[RobotFileParser, bool]]) -> bool:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in cache:
        robots_url = origin + "/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            request = Request(robots_url, headers={"User-Agent": USER_AGENT})
            allowed_host = {(parsed.hostname or "").lower()}
            with build_opener(SameHostRedirect(allowed_host)).open(request, timeout=5) as response:
                content = response.read(256_000).decode("utf-8", errors="replace")
            parser.parse(content.splitlines())
            cache[origin] = (parser, True)
        except HTTPError as exc:
            if exc.code == 404:
                parser.parse([])
                cache[origin] = (parser, True)
            else:
                parser.parse(["User-agent: *", "Disallow: /"])
                cache[origin] = (parser, False)
        except (URLError, TimeoutError, OSError):
            parser.parse(["User-agent: *", "Disallow: /"])
            cache[origin] = (parser, False)
    parser, reachable = cache[origin]
    return reachable and parser.can_fetch(USER_AGENT, url)


def request_interval(url: str, cache: dict[str, tuple[RobotFileParser, bool]]) -> float:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    parser, _ = cache[origin]
    delay = parser.crawl_delay(USER_AGENT) or 0
    rate = parser.request_rate(USER_AGENT)
    if rate and rate.requests:
        delay = max(delay, rate.seconds / rate.requests)
    return max(1.0, float(delay))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def fetch_page(url: str, allowed_hosts: set[str]) -> tuple[str, str, str, list[str]] | None:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    opener = build_opener(SameHostRedirect(allowed_hosts))
    with opener.open(request, timeout=12) as response:
        final = response.geturl()
        if (urlsplit(final).hostname or "").lower() not in allowed_hosts:
            return None
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            return None
        # raw = response.read(MAX_PAGE_BYTES)  # Disabled per-page byte cap.
        raw = response.read(MAX_PAGE_BYTES)
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    parser = TextPageParser()
    try:
        decoded = raw.decode(charset, errors="replace")
    except LookupError:
        decoded = raw.decode("utf-8", errors="replace")
    parser.feed(decoded)
    title = clean_text(" ".join(parser.title_parts)) or final
    description = clean_text(parser.description)
    body = clean_text(" ".join(parser.text_parts))
    links = [urljoin(final, link) for link in parser.links]
    return final, title, description, [body, *links]


def update_state(**changes) -> None:
    with STATE_LOCK:
        STATE.update(changes)


def page_count() -> int:
    with connect_db() as db:
        return int(db.execute("SELECT COUNT(*) FROM pages").fetchone()[0])


def crawl(seeds: list[str], limit: int) -> None:
def crawl(seeds: list[str]) -> None:
    allowed_hosts = {(urlsplit(seed).hostname or "").lower() for seed in seeds}
    queue = deque(seeds)
    queued = set(seeds)
    visited: set[str] = set()
    robots_cache: dict[str, tuple[RobotFileParser, bool]] = {}
    last_request: dict[str, float] = {}
    saved = 0
    examined = 0
    update_state(state="crawling", done=0, limit=limit, error="", message="Checking site rules and starting the crawl.")
    update_state(state="crawling", done=0, error="", message="Checking site rules and starting the crawl.")
    try:
        while queue and saved < limit:
        # No page-count cap: keep indexing discovered URLs until the queue is
        # empty, the user stops the crawl, or the seeded hosts have no new pages.
        while queue and not CRAWL_STOP.is_set():
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            host = (urlsplit(url).hostname or "").lower()
            if host not in allowed_hosts or not robots_can_fetch(url, robots_cache):
                continue

            origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
            interval = request_interval(url, robots_cache)
            wait_for = interval - (time.time() - last_request.get(origin, 0.0))
            if wait_for > 0:
                time.sleep(wait_for)
            if wait_for > 0 and CRAWL_STOP.wait(wait_for):
                break
            last_request[origin] = time.time()
            examined += 1
            try:
                page = fetch_page(url, allowed_hosts)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                page = None

            if page:
                final_url, title, description, content = page
                body, *links = content
                if body:
                    with connect_db() as db:
                        db.execute(
                            """INSERT INTO pages (url, title, description, body, updated_at)
                               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                               ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                                 description=excluded.description, body=excluded.body,
                                 updated_at=CURRENT_TIMESTAMP""",
                            # body[:1_000_000] was a per-page truncation cap; keep the full text.
                            (final_url, title[:500], description[:1_200], body[:1_000_000]),
                            (final_url, title[:500], description[:1_200], body),
                        )
                    saved += 1
                    visited.add(final_url)
                    if len(visited) % 5 == 0:
                        update_state(done=saved, message=f"Indexed {saved} pages so far.")

                for link in links:
                    try:
                        candidate = normalize_url(link)
                    except ValueError:
                        continue
                    parsed = urlsplit(candidate)
                    if (parsed.hostname or "").lower() not in allowed_hosts:
                        continue
                    if parsed.path.lower().endswith(SKIP_EXTENSIONS):
                        continue
                    if candidate not in queued and candidate not in visited:
                        queued.add(candidate)
                        queue.append(candidate)
            update_state(done=saved, message=f"Visited {examined} pages · indexed {saved}.")

        final_count = page_count()
        if CRAWL_STOP.is_set():
            update_state(state="stopped", done=saved, message=f"Crawl stopped. Your index contains {final_count} pages.")
        else:
        update_state(state="done", done=saved, limit=limit, message=f"Crawl finished. Your index contains {final_count} pages.")
            update_state(state="done", done=saved, message=f"Crawl finished with no page-count cap. Your index contains {final_count} pages.")
    except Exception as exc:  # Keep the local server available if one crawl fails unexpectedly.
        update_state(state="error", done=saved, error=str(exc), message="The crawl stopped before it finished.")


def search_index(query: str) -> dict:
    terms = list(dict.fromkeys(TOKEN_RE.findall(query.lower())))
    indexed = page_count()
    if not terms:
        return {"query": query, "count": 0, "indexed": indexed, "results": []}

    with connect_db() as db:
        rows = db.execute("SELECT url, title, description, body FROM pages").fetchall()

    documents = []
    document_frequency = Counter()
    total_length = 0
    for row in rows:
        words = TOKEN_RE.findall((row["title"] + " " + row["description"] + " " + row["body"]).lower())
        counts = Counter(words)
        for term in set(terms):
            if counts[term]:
                document_frequency[term] += 1
        total_length += len(words)
        documents.append((row, counts, len(words)))

    total_docs = max(len(documents), 1)
    average_length = max(total_length / total_docs, 1.0)
    ranked = []
    for row, counts, length in documents:
        score = 0.0
        matched = []
        for term in terms:
            frequency = counts[term]
            if not frequency:
                continue
            matched.append(term)
            df = document_frequency[term]
            inverse_frequency = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * length / average_length)
            score += inverse_frequency * frequency * 2.5 / denominator
        if score:
            body_lower = row["body"].lower()
            positions = [body_lower.find(term) for term in matched if body_lower.find(term) >= 0]
            position = min(positions, default=0)
            start = max(0, position - 90)
            end = min(len(row["body"]), start + 250)
            snippet = row["body"][start:end].strip()
            if start:
                snippet = "…" + snippet
            if end < len(row["body"]):
                snippet += "…"
            ranked.append({
                "url": row["url"],
                "title": row["title"],
                "description": row["description"],
                "snippet": snippet or row["description"],
                "score": score,
            })

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return {"query": query, "count": len(ranked), "indexed": indexed, "results": ranked[:30]}


class SearchHandler(BaseHTTPRequestHandler):
    server_version = "OpenIndex/0.1"

    def send_json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self.serve_file("index.html", "text/html; charset=utf-8")
        elif parsed.path == "/app.js":
            self.serve_file("app.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/styles.css":
            self.serve_file("styles.css", "text/css; charset=utf-8")
        elif parsed.path == "/api/status":
            with STATE_LOCK:
                result = dict(STATE)
            result["indexed"] = page_count()
            self.send_json(result)
        elif parsed.path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0][:300]
            self.send_json(search_index(query))
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/crawl":
        route = urlsplit(self.path).path
        if route == "/api/stop":
            with STATE_LOCK:
                if STATE["state"] != "crawling":
                    self.send_json({"error": "There is no active crawl to stop."}, 409)
                    return
                CRAWL_STOP.set()
            self.send_json({"state": "crawling", "message": "Stopping after the current page."}, 202)
            return
        if route != "/api/crawl":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 32_000:
            if length < 1 or length > 1_000_000:
                raise ValueError("The crawl request is empty or too large.")
            payload = json.loads(self.rfile.read(length))
            raw_seeds = payload.get("seeds", [])
            if not isinstance(raw_seeds, list) or not raw_seeds or len(raw_seeds) > MAX_SEEDS:
                raise ValueError(f"Enter between 1 and {MAX_SEEDS} starting URLs.")
            if not isinstance(raw_seeds, list) or not raw_seeds:
                raise ValueError("Enter at least one starting URL.")
            seeds = list(dict.fromkeys(normalize_url(str(value)) for value in raw_seeds))
            limit = max(10, min(500, int(payload.get("limit", 100))))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
            return

        with STATE_LOCK:
            if STATE["state"] == "crawling":
                self.send_json({"error": "A crawl is already running."}, 409)
                return
            CRAWL_STOP.clear()
            STATE.update(state="crawling", done=0, limit=limit, error="", message="Starting crawl.")
        thread = threading.Thread(target=crawl, args=(seeds, limit), daemon=True)
            STATE.update(state="crawling", done=0, error="", message="Starting crawl.")
        thread = threading.Thread(target=crawl, args=(seeds,), daemon=True)
        thread.start()
        self.send_json({"state": "crawling", "message": "Your crawl has started."}, 202)

    def serve_file(self, filename: str, content_type: str) -> None:
        path = ROOT / "static" / filename
        try:
            data = path.read_bytes()
        except OSError:
            self.send_json({"error": "The app files could not be found."}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.log_date_time_string()} {self.address_string()} {format % args}")


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), SearchHandler)
    print(f"Open Index is ready at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop the local search app.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Open Index.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
