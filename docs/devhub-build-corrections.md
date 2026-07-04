# DEVHUB build — corrections log & pipeline post-mortem

A running log of every fix needed to make the autonomously-built **MCP Dev Hub**
(110/110 stories, CI-green) actually *work* when a human ran it — plus the likely reason
our agent pipeline shipped each defect. Kept so we can harden the system.

**Meta-theme:** CI was green and 63 test files passed, but the product was **broken on
first human use**. Every defect below shares one root cause: **the pipeline never ran the
software the way a user does** (no browser, no real ASGI server, no exercise of
auth-gated paths, no fresh-environment run). Unit tests + a diff-reading reviewer cannot
see "does it render / does it actually work."

---

## Corrections (product: `mcp-dev-hub-python-re-implementation`)

### 1. UI dead — fabricated SRI `integrity` hashes blocked all JS  ⛔ critical
- **Symptom:** admin UI stuck on "Loading servers…" forever; nothing interactive.
- **Root cause:** every `<script>` (htmx, hyperscript, tailwind) loaded from a public CDN
  with an `integrity="sha256-…"` hash the coder **made up**. Browsers compute the real
  hash, find a mismatch, and **block the script**. With htmx blocked, `hx-get`/`hx-post`
  never fire. (Tailwind additionally used `crossorigin="anonymous"` on an endpoint with no
  CORS header → also blocked.)
- **Fix:** vendored htmx/hyperscript/js-yaml/tailwind into `static/vendor/` and pointed all
  HTML + 9 templates at local paths (no CDN, no SRI, works offline/LAN).
- **Why the pipeline missed it:**
  - **LLM fabrication:** the coder generated confident-looking base64 SHA-256 strings
    instead of computing them — a classic hallucination the model can't self-check.
  - **No browser/E2E test** — unit tests never load the page in a real browser, so a
    100%-broken UI passes CI.
  - **Reviewer read the diff, not the running app** — a human (or LLM) reading a diff
    won't recompute an SRI hash or open a browser.
  - **Violated the stated "local-first" requirement** — the design depended on the public
    internet; nothing enforced the local-first constraint.

### 2. "Add Server" form could never work  ⛔ high
- **Symptom:** submitting the form did nothing / errored.
- **Root cause (three independent bugs):**
  1. Form omitted the **required `id` field** (`RegisterRequest.id` is required; the form
     only sent `name` + `url`).
  2. htmx posted **form-urlencoded**, but `/v1/register` expects a **JSON** body.
  3. `POST /v1/register` requires **Basic auth**, and no password is set → 401.
- **Fix:** added the `id` input, added `hx-ext="json-enc"` (+ vendored the extension),
  reset-and-refresh on success, and set `auth.type: none` in the local config for LAN use.
- **Why the pipeline missed it:** no browser test ever *submitted* the form; and the
  register endpoint was **auth-gated**, so no test/agent ever drove it end-to-end (see #3).

### 3. `/v1/register` returned HTTP 500 — `BaseHTTPMiddleware` + body-read  ⛔ high (latent)
- **Symptom:** once auth was opened, every register 500'd with
  `RuntimeError: No response returned` / `cancel scope … isn't the current task's`.
- **Root cause:** the request-ID/metrics middleware subclassed Starlette's
  `BaseHTTPMiddleware`, which wraps the receive channel in a task group. When a downstream
  handler reads the body (`await request.body()`, as `/v1/register` does), this deadlocks
  and raises the cancel-scope error. **This endpoint had never worked** — it was always
  hidden behind the 401.
- **Fix:** rewrote the middleware as a **pure-ASGI middleware** (wraps only `send`, not
  `receive`), so body reads work. Verified live: register now returns a proper 201/400 and
  the `X-Request-ID` header is set.
- **Why the pipeline missed it:**
  - **Auth-gated path never exercised** — 401 masked a 500 for the entire build.
  - **Dependency version drift / no lockfile:** the fresh `uv` install pulled
    `starlette 1.3.1` where this `BaseHTTPMiddleware` anti-pattern breaks; CI likely ran an
    earlier, tolerant version. `pyproject.toml` uses only lower-bound pins (`>=`) with no
    lockfile, so "works in CI" ≠ "works on a fresh install."
  - **Known-fragile pattern:** `BaseHTTPMiddleware` + body-read is a documented Starlette
    footgun; neither coder nor reviewer flagged it.

### 4. Server-list fragment mismatch  ⚠️ cosmetic
- **Symptom:** the landing page swaps `GET /ui/servers` (a **full HTML document**) into a
  `<div>` via `hx-swap="innerHTML"` — nested `<html>/<head>/<body>` inside a div.
- **Status:** works (browser strips the wrapper tags) but is sloppy; `/ui/servers` should
  return a fragment. Left as-is; noted.
- **Why missed:** no browser test; reviewer wouldn't catch a full-page-vs-fragment swap
  from a diff.

### 5. Stray `=0.23.0` file at repo root  ⚠️ minor
- **Root cause:** a coder step ran `pip install pytest-asyncio >=0.23.0` unquoted; the
  shell parsed `>=0.23.0` as a redirect (`>` + `=0.23.0`), creating an empty file.
- **Fix:** `git rm '=0.23.0'` (pending).
- **Why missed:** committed by the coder; not caught by lint/CI (it's an inert empty file).

### 6. `auth.type: basic` with no password = unusable, not "secure"  ⚠️ UX/design
- The shipped default makes **every** registration 401 (no password exists to authenticate
  with), so the admin UI's core action is dead out of the box. A safer default would be
  `none` on localhost with a clear "set a password to expose" note, or a generated
  first-run password.
- **Why missed:** no "first-run UX" acceptance test; the security-review lens optimized for
  "no hardcoded password" without checking "can the primary flow run at all."

### 7. SSRF guard blocks LAN/localhost — contradicts "local-first"  ⛔ high
- **Symptom:** registering `http://192.168.0.100/webcalendar/mcp.php` → "URL validation failed".
- **Root cause:** `is_url_safe_for_discovery` (SSRF protection) rejects loopback/private/
  link-local IPs. But this is a **local-first hub for localhost/LAN MCP servers** — the guard
  blocks its entire reason to exist. The security lens and the product requirement were never
  reconciled.
- **Fix:** added `security.allow_private_networks` config (default False); when true the guard
  permits private ranges. Threaded through the register + tool-invoke paths; set true in the
  local config.
- **Why the pipeline missed it:** the security-review agent added a textbook SSRF guard
  (correct in isolation) with no awareness of the "must reach LAN" product requirement; no
  test registered a realistic (LAN) server, so the contradiction never surfaced.

### 8. Auth-protected MCP servers can't be added from the UI  ⚠️ high
- **Symptom:** after the SSRF fix, registering the (token-protected) WebCalendar server →
  "discovery failed". The server responds `initialize` with "API token required".
- **Root cause:** `manual` registration **requires a successful MCP handshake** (and
  unregisters on failure), but the **Add Server form has no field for a bearer token**, so an
  auth-protected server can never pass discovery from the UI. (The backend *does* support it:
  `RegisterRequest.bearer_token` → `apply_server_auth` sends `Authorization: Bearer`.)
- **Fix:** added an optional "Bearer token" field to the form (backend infers
  `auth_type=bearer`). Also noted: `DELETE /v1/register/{id}` of a missing id returns 500
  (should be 404) — minor, logged.
- **Why the pipeline missed it:** the form was never driven against a real, auth-protected MCP
  server; the "happy path" (open server) was the only case considered. Discovery being
  mandatory-and-destructive on registration is also an untested UX edge.

### 9. MCP client transport broken — anyio cancel-scope violation  ⛔ critical
- **Symptom:** every real MCP connection (discovery, health, proxy, tool-invoke) failed with
  `RuntimeError: Attempted to exit a cancel scope that isn't the current task's`.
- **Root cause:** `MCPClient` opened the SDK's streamable transport with a nested
  `async with transport:` **inside** `_open_transport`, then stored the session and used it
  from other methods after that block exited. The SDK transport is an anyio task group that
  must stay open for the session's whole life, in one task; splitting it across
  `__aenter__`/methods/`__aexit__` violates anyio's cancel-scope rules.
- **Fix:** manage the transport + session on a single `contextlib.AsyncExitStack`, entered in
  `_open_transport` and closed in `_close_transport` (same task via `async with MCPClient`).
- **Why the pipeline missed it:** unit tests mocked the MCP SDK, so the real transport
  lifecycle was never exercised; and (again) newer `anyio` enforces cancel scopes strictly
  where an older pinned version tolerated the bug.

### 10. Second SSRF layer (SafePinnedTransport) also blocked LAN  ⚠️ high
- After #7, the *outbound* pinned-IP transport (`_ip_allowed`) still rejected the LAN target.
- **Fix:** a module-level `set_allow_private_networks()` flag, set from config in `create_app`,
  that both SSRF layers honor. **Lesson:** the SSRF policy was duplicated in two places and
  only one was config-aware.

### 11. Auth header stripped — only `Authorization: Bearer` sent  ⚠️ high (interop)
- **Symptom:** with a valid token, the server still returned "API token required" — because it
  runs behind **Apache/PHP, which strips the `Authorization` header** (a classic gotcha).
- **Fix:** `apply_server_auth` now also sends `X-MCP-Token` (the server's documented
  alternative). Harmless for servers that only read `Authorization`.
- **Why the pipeline missed it:** never tested against a real Apache/PHP MCP server.

### 12. MCP SDK returns typed objects; code assumed dicts  ⛔ high (version drift)
- **Symptom:** discovery threw `'Tool' object has no attribute 'get'` — `validate_tool_schemas`
  did `tool.get("name")` on Pydantic `Tool` objects returned by the newer SDK.
- **Fix:** normalize `list_tools/prompts/resources` results to dicts (`_as_dicts`, by_alias)
  at the `MCPClient.list` boundary.
- **Why the pipeline missed it:** mocked SDK in tests returned dicts; the real (newer) SDK
  returns models — the third distinct **no-lockfile version-drift** failure in this list
  (see also #3, #9).

**Result:** WebCalendar MCP server registers cleanly — healthy, 4 tools discovered
(list_events, get_user_info, search_events, add_event).

### 13. Every result panel was permanently hidden — buttons looked dead  ⛔ critical
- **Symptom:** on every browser (incl. a first-time, cache-free iPad Safari), the card
  buttons "did nothing" and no tools showed.
- **Root cause:** each result `<div>` (tools/initialize/trace/conformance/faults/playground)
  shipped with `class="hidden"` (`display:none`) and **nothing ever removed it**. htmx *did*
  fire and load content — into an invisible div. (My own automated checks were fooled because
  `innerText` reads hidden elements; only checking `offsetParent`/computed `display` exposed
  it.)
- **Fix:** remove `hidden` on `htmx:after-swap`; also list the discovered tools directly on
  the card so they're visible without clicking.
- **Why the pipeline missed it:** no browser test ever asserted **visibility** after a click —
  the deepest version of "CI green ≠ works." A DOM-text assertion would have passed too.

### 14. Tool output blank for servers without a content[] array  ⚠️ high
- **Symptom:** invoking a tool returned "No output" despite the server returning data.
- **Root cause:** the invoke view only read the standard `result.content[].text`; a server that
  returns the result payload directly (no `content[]` wrapper) rendered as empty.
- **Fix:** fall back to rendering the raw `result` JSON when there's no `content[]`.
- **Why the pipeline missed it:** only the spec-perfect response shape was considered; never
  tested against a real, slightly-non-conformant MCP server.

### 15. Tool form vanished after one invocation  ⚠️ medium
- **Symptom:** invoking a tool worked once, then the form disappeared (couldn't invoke again).
- **Root cause:** the invoke form used `hx-swap="outerHTML"` with no target, so the result
  replaced the entire form.
- **Fix:** render the result into a dedicated `#result-<server>-<tool>` div (`innerHTML`) so the
  form persists and is reusable.
- **Why the pipeline missed it:** invoking a tool *twice* was never tested.

### 16. Trace controls replaced the whole page + verbose never toggled  ⚠️ medium
- **Symptoms:** "Enable Verbose" (and Refresh/Clear) replaced the entire page with just the
  trace; the verbose toggle never actually turned on.
- **Root causes:** the trace panel is a standalone full page loaded into the card slot, but its
  controls used `hx-target="body"` (replaces everything when nested). And "Enable Verbose" did
  `hx-get ...?verbose=true`, which the GET route ignores (it renders the *persisted*
  `srv.trace_verbose`); the real state lives behind `POST .../trace/verbose`.
- **Fix:** target the card's `#trace-<id>` slot; point the verbose button at the toggle route.
- **Why the pipeline missed it:** panels built as standalone pages were never tested *nested*
  in a card, and the verbose toggle was never actually exercised.

**Caching footnote:** several "still broken after your fix" reports were iOS Safari serving a
stale page (the built pages had **no cache headers**). Fixed with `Cache-Control: no-store` +
a visible build marker; a browser test never caught it because the server was always correct.

### 17. Tool-script download hit a nonexistent route  ⚠️ medium
- **Symptom:** "Download Bash Script" saved a file `bash.json` containing `{"detail":"Not Found"}`.
- **Root cause:** the button did `window.location.href` (GET) to
  `/ui/invoke/<id>/<tool>/download/bash` — wrong path *and* wrong method. The real route is
  `POST /ui/server/<id>/tool/<tool>/download[-python]` and needs the invoke form's args.
- **Fix:** a `downloadToolScript()` helper (on the main page — scripts in htmx-swapped
  fragments don't run) that POSTs the form via fetch and saves the returned attachment.
- **Why the pipeline missed it:** the button URL never matched the route; never clicked/tested.

**Full working flow now:** register (auth) → tools listed on the card → buttons reveal panels
(in place) → invoke a tool repeatedly → output shown each time → trace/verbose toggle in the
slot → download runnable bash/python scripts. Verified in both Blink and WebKit (iPad engine).

---

## Systemic gaps in our agent pipeline (the "why")

1. **No browser / E2E stage.** The single biggest hole. Defects #1, #2, #4 are invisible
   to unit tests and diff review but obvious the instant a browser loads the page.
   *Action:* add a Playwright/headless-chrome smoke step to the tester role for any story
   touching HTML/templates/static — load the page, assert no console errors, assert the
   primary flow works.
2. **Reviewer reviews diffs, not running software.** It can't recompute an SRI hash, can't
   see a blocked script, can't submit a form. *Action:* for UI stories, have the reviewer
   (or a new "manual-QA" agent) drive the live app, not just read the diff.
3. **LLM-fabricated artifacts slip through.** SRI hashes (#1) are the tell: models emit
   plausible constants they cannot verify. *Action:* a lint/verify step for
   integrity/checksums/pinned-hashes, or a rule to **vendor local assets** for local-first
   projects instead of CDN+SRI.
4. **Auth-gated / conditional code paths are never driven.** #3 hid behind a 401 for the
   whole build. *Action:* integration tests must exercise protected endpoints *with*
   credentials (or with auth disabled) so the handler actually runs.
5. **No dependency lockfile / upper bounds.** Fresh installs drift from CI (#3). *Action:*
   generate and commit a lockfile (`uv.lock` / `requirements.txt` pinned), and have the
   fresh-venv gate install *from the lock*.
6. **"CI green" was trusted as "done."** Green meant "imports resolve + unit assertions
   pass in a fresh venv" — not "a user can run it." *Action:* redefine done-ness to include
   a run-the-app acceptance check.
7. **Stated non-functional requirements weren't enforced.** "Local-first" was in the PRD but
   the build reached for public CDNs. *Action:* encode key NFRs (local-first, offline-capable)
   as checks the tester/security roles assert.

---

## Status of the local working copy
`/home/cknudsen/programs/mcp-dev-hub-python-re-implementation` — patched with fixes
#1–#3 (+ auth=none for LAN). Running on `http://192.168.0.100:8095`. These fixes live only
in the local clone so far; the Forgejo repo / build still has the original defects (good
candidates to feed back as new stories to prove the hardened pipeline).
