# Planting labels: truncated JSON from `/kasvimuseo/planting-labels/data/`

Reported 2026-07-30. The planting-label page fails to render because its data endpoint
returns a body that stops mid-string:

```
http://kandev.example.test:8000/kasvimuseo/planting-labels/data/
SyntaxError: JSON.parse: unterminated string at line 1 column 42872 of the JSON data
```

The body the browser received ends here:

```
, {"nicknames": [""], "all_photos": {"134": "/media/photologue/photos/cache/S%C3%A4rkynytsyd%C3%A4n.
```

## What was checked

Fetched the same URL from a different client (`curl`, 4 times, same day):

- HTTP 200, `content-type: application/json`, body 54613 bytes every time.
- The body parses cleanly (`json.loads`) and ends with the expected `…"genus": "Ribes"}]}`.
- Same result with `--compressed`, with a Firefox `User-Agent`, and with
  `Accept-Encoding: gzip, deflate, br, zstd` — the response never came back short.

So the payload itself is well-formed. The failure is a **truncated transfer**, not bad
serialisation: the browser got the first ~42871 bytes of a 54613-byte response and the
JSON parser reported the first unterminated token in it. Byte 42871 falls exactly at
`…/cache/S%C3%A4rkynytsyd%C3%A4n.` in the good body, which matches the reported fragment
character for character — the client stopped reading at an arbitrary offset, not at a
record boundary.

Note that the truncation point is *not* at a non-ASCII character: `%C3%A4` is already
percent-encoded ASCII in the payload, and the good body is pure ASCII (54613 chars =
54613 bytes). Encoding is not implicated.

## Origin

```
server: WSGIServer/0.1 Python/2.7.18
```

That is Django's `runserver` on Python 2.7 — single-threaded, development-grade. The
`Connection: keep-alive` / `Transfer-Encoding: chunked` headers seen in the `curl` run
were added by an intervening proxy, so the origin's own framing was not observable from
here.

## Likely causes, in order

1. **`runserver` dropping the connection.** The dev WSGI server handles one request at a
   time and aborts the in-flight response when the handler thread dies or the client
   opens a second connection. Photologue generates its `cache/` thumbnails lazily, so the
   page very plausibly issues image requests *while* this 54 KB response is still being
   written — exactly the concurrency the dev server does not survive. This fits best: it
   is intermittent, offset-arbitrary, and invisible to a lone `curl`.
2. **A reverse proxy / tunnel timing out mid-body.** The URL is a Tailscale hostname; a
   proxy that gives up part-way would produce the same symptom.
3. **The browser aborting the fetch** (navigation, reload, or a second in-flight request
   cancelling the first) and the page parsing the partial body anyway.

## Suggested next steps

- Reproduce in the browser with devtools open and check whether the network entry is
  marked as failed/cancelled and what its transferred size is. If the browser reports a
  net error, cause 1 or 2 is confirmed and the fix is server-side, not in the JSON view.
- Serve behind a real WSGI server (gunicorn/uwsgi) instead of `runserver`, or at minimum
  run `runserver` with threading enabled, and see whether it recurs.
- Make the client defensive regardless: treat a `JSON.parse` failure as a retryable fetch
  error rather than letting it break the page.
- Pre-generate the Photologue caches so the label page does not fan out thumbnail
  requests concurrently with its data fetch.
