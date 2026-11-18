# Open Index

Open Index is a small, local search engine for websites you choose. It crawls pages on the same websites as your starting URLs, saves them in a local SQLite index, and ranks search results with BM25.

It does not search all of the public web. Large web search engines use much larger crawling and indexing systems; this starter lets you build a focused index on your own computer.

## Run it

Requires Python 3.10 or newer. It uses only Python's standard library.

1. Open a terminal in this folder.
2. Run `py app.py` on Windows, or `python3 app.py` on macOS and Linux.
3. Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in your browser.
4. Add one or more public website starting URLs. The crawler follows links on those same hosts, observes each site's `robots.txt` rules, and honors crawl delays when provided. It keeps going until it runs out of new pages or you stop it.
5. Search the pages after they are indexed.

The first crawl can take a while. Open Index waits between requests to each website. There is no fixed page-count cap; the crawl can continue as long as it discovers pages. Use **Stop crawling** at any time. The local database is saved as `search.db` in this folder; remove that file to clear the index.

## Current limits

- The index is limited to the hosts in the starting URLs. Its size is bounded by available disk space rather than a page-count cap.
- It indexes HTML text. It does not run JavaScript, parse PDFs, or access pages that require a login.
- The app binds to `127.0.0.1`, so it is available only on this computer.
- Search ranking is a simple BM25 baseline and can be improved as the project grows.
