# Auth Layer — Prototype Plan

## Goal
Per-user, filesystem-level authorization for WSI Browser: a user must not see
or read any slide/directory they couldn't read via `ssh`/`bash`. Decisions are
made by impersonating the user (numeric UID/GID/groups from FreeIPA) for an
actual read/list test against the NFS-backed data, exactly mirroring kernel
NFSv4-ACL evaluation.

Scope is deliberately prototype-level: **application-level authorization only**.
OpenSlide/tile generation continues to run as the service user (`wsi`). The
helper only answers yes/no read-ability for a path; it does not open the slide
for real rendering.

---

## Architecture

```
Browser --(login form)--> FastAPI --LDAP bind--> FreeIPA  (authn)
                                   \-> session cookie (signed, holds uid+gids)
Browser --(cookie)--> FastAPI --authz check--> aclcheckd (root, unix socket)
                                   \-> fork child, setpriv to user's uid/gid/gids
                                       -> os.access(path, R_OK) or scandir
                                   \-> decision cached in Redis (short TTL)
```

- uvicorn/FastAPI stays non-root (UID 1000, `wsi`).
- A small **`aclcheckd` daemon** runs as root in the same container, listening
  on a Unix socket (`/run/wsi/aclcheck.sock`). Fork-per-check.
- Future Keycloak swap: replace the LDAP-login `Authenticator` only; the helper
  + authz layer stay unchanged.

---

## Components

### 1. `app/auth.py` (new)
- `AuthCfg` (added to `config.py`): `ldap_url`, `ldap_search_base` (or explicit
  user DN template `uid={u},cn=users,cn=accounts,dc=…`), `ldap_bind_dn` +
  `ldap_bind_password` (optional service account for group lookup; if unset,
  reuse the user's own bind to read `memberOf`), `session_secret`, `session_ttl`.
- `Principal` dataclass: `username, uid:int, gid:int, groups:list[int]`.
- `ldap_login(username, password) -> Principal | None`:
  - `ldaps://` bind with user DN (start TLS / TLS required). Bind success =
    authenticated. On any LDAP error → fail closed (return None).
  - Read `uidNumber`, `gidNumber`, and group memberships. Prefer reading
    `memberOf` from the user entry (gives group DNs → look up `gidNumber`).
    Fallback: search `cn=groups,cn=accounts,dc=…` for `(member=<user DN>)`.
  - Public datasets: handled by the ACL itself (the user's groups will simply
    include a broad/`all` group, or the path is world-readable → `os.access`
    passes). No special-casing needed.
- `session` helpers: signed-cookie creation/verification (itsdangerous or
  FastAPI `Starlette` SessionMiddleware with a secret). Cookie payload:
  `username, uid, gid, groups, exp`. HttpOnly, SameSite=Lax, Secure behind TLS.
- FastAPI dependency `current_principal(request) -> Principal`:
  - Reads cookie. Invalid/expired → 401 (frontend redirects to login).
  - Stashes `Principal` on `request.state.principal`.

### 2. `aclcheckd` (new, root daemon)
New file `app/aclcheckd.py` + entry point. A minimal Unix-socket server:

- Startup (root): bind `/run/wsi/aclcheck.sock`, `chmod 0600`.
- Per request (one JSON line in, one JSON line out), **fork**:
  - Child: `os.setgid(gid)`, `os.setgroups(groups)`, `os.setuid(uid)` (order:
    gid/groups first, then uid — drops setuid privilege last), then:
    - `read` op: `os.access(path, os.R_OK)` — and additionally attempt
      `os.open(path, os.O_RDONLY)` then immediately close, to test exactly
      what OpenSlide will do (catches cases `os.access` over-promises on NFS).
      Return `{"ok": bool, "err": str|none}`.
    - `listdir` op: `os.scandir(path)` as the user, returning the entry names
      the user can see (the kernel/NFS filters traversal on the caller's creds,
      so this is the correct per-user listing). Used by tree/expand/dir
      endpoints. Returns `{"ok": bool, "entries": [...], "err": str|none}`.
  - Parent: collect child exit, return result. Timeout (e.g. 5s) per check;
    on timeout/error → **fail closed** (`ok=false`).
- Protocol is deliberately tiny and synchronous. One socket, line-delimited JSON.
- Why fork not thread: `setuid` is process-wide; concurrent threads would clash.
  Fork-per-check is ~1-3ms, amortized by the Redis decision cache.

Container wiring:
- Dockerfile: add a root entrypoint that starts `aclcheckd` (root) then drops to
  `wsi` and runs uvicorn. Compose: keep `:ro` data mounts (reads still authorize
  by caller identity — `:ro` only blocks writes, which we want anyway).
- Socket dir `/run/wsi` created by the root entrypoint, owned `wsi` so the app
  can connect.

### 3. `app/authz.py` (new) — client + cache
- `AclClient(socket_path)`: connect to `aclcheckd`, send JSON, read reply, with
  short timeouts and fail-closed semantics.
- `can_read(principal, path) -> bool` and `list_dir(principal, path) -> list[str]|None`:
  - **Decision cache** in Redis (global `cache` client), key
    `wsi:authz|r|<uid>|<path_hash>` (path hashed via existing `Cache.key`), TTL
    short (default 300s, configurable `auth.authz_ttl`). `list_dir` result cached
    similarly under `wsi:authz|l|<uid>|<path_hash>`.
  - Cache miss → call helper. `ok=false` results are cached too (shorter TTL,
    e.g. 60s) to avoid hammering on denied paths, but with a short TTL so newly
    granted access propagates quickly.

---

## Endpoint authz ordering (the critical part)

Order for every data endpoint becomes: **resolve path → authz check → cache read**.

### Tile (`/dzi/{slide_id}_files/...`) and thumbnail (`/api/thumb/{slide_id}`)
Today: resolve-by-id → cache lookup → generate. New order:
1. Resolve path (`resolve_by_id_with_fallback`).
2. **`authz.can_read(principal, p)`** — deny → 403 (tile) / 404 (thumb; avoid
   leaking existence — return same as "not found").
3. ETag / 304 short-circuit.
4. **Global tile/thumb cache** lookup (unchanged, still keyed by slide_id+params
   — these are byte-identical across users, safe to share **because step 2
   already authorized**).
5. Generate / return.
   - Tile cache key is **unchanged** and global. ✅ Multi-user friendly.
   - Reorder point: authz must precede the cache `get`. Today the tile handler
     resolves `p_for_etag` then builds the cache key and checks cache before any
     authz. Insert the `can_read` call right after resolution, before the ETag/
     cache block.

### DZI (`/dzi/{slide_id}.dzi`), meta, markers, qptiff channels, associated images
Same pattern: resolve → `can_read(principal, p)` (403/404) → existing logic.
Associated-image list returns only names; the per-image fetch still re-checks.

### Tree / expand / dir — **must run as the user** (per-user listings)
These currently `scandir` as UID 1000, which would leak entries the user can't
traverse. New approach:
- `/api/tree`, `/api/expand`, `/api/dir`: use `authz.list_dir(principal, path)`
  (forked scandir as the user) instead of the service-user scandir.
  - Reuse `fs_index.scan_directory_shallow_optimized` logic but feed it the
    per-user entry list from the helper, OR move the scandir into the helper
    `listdir` op and post-process (filter extensions/exclude) in the app.
    Chosen: helper returns raw entry names + is_dir; app applies `exclude` +
    extension filter + `slide_count`/`has_children` derivation as today. Keeps
    helper dumb.
  - `quick_has_subdirs` for child folders: either a per-user `list_dir` (could
    be many calls) or accept optimistic `has_children=true` and let `/api/expand`
    be the authority. Prototype: keep optimistic `has_children` (existing
    behavior) and only filter at expand/dir time.
- **Per-user caching**: tree/expand/dir cache keys gain `principal.uid`.
  - Today: `Cache.key("tree_shallow", base)` and `Cache.key("expand", path)`.
  - New: `Cache.key("tree_shallow", str(uid), base)` / `("expand", str(uid), path)`.
  - `/api/dir` has no cache today — leave uncached or add per-user caching
    optionally.
- The "don't cache empty results (NFS glitch)" guard stays, now per-user.

### Path cache (`path_cache`) — unchanged
`slide_id -> path` is identity, not authorization; safe to share globally.
(The existing `_is_under_root` guard stays; authz is a separate layer on top.)

### `/health`, `/logo`, `/static`, `/` 
- `/health`, `/logo`, `/static`: no authz (public assets / health).
- `/` (index): serve without authz; the page itself is just a shell. The API
  calls will 401 and the frontend redirects to login.

---

## Frontend (`index.html` + `useRequests.js`)
Prototype-level: add a simple login route rather than a SPA router.
- New minimal login page (can be a second Jinja template `/login` or a Vue
  overlay in `index.html` shown when `principal` is unset / 401 received).
  - Form: username + password → `POST /api/login`.
  - On success: redirect to `/`.
- `useRequests.makeRequest`: on `401`, set a reactive `authRequired=true` flag
  that shows the login overlay; on `403`, surface a "no access to this slide"
  toast and skip the item (don't retry).
- Tiles/thumbs already go through `fetch`; the signed cookie is sent
  automatically (SameSite=Lax, same origin). No header changes needed.
- `cors_allow_origins`: default `*` won't work with credentials; but since this
  is same-origin (cookie), keep `*` for the public assets and rely on same-origin
  for the API. Note: with `allow_credentials=True` + `allow_origins=["*"]`
  FastAPI/Starlette is technically inconsistent; for a prototype same-origin
  deployment this is fine — flag it for hardening later.

### New endpoints
- `POST /api/login` `{username, password}` → sets session cookie, returns `{ok}`.
- `POST /api/logout` → clears cookie.
- `GET /api/me` → `{username}` (for the UI to know who's logged in).

---

## Config additions (`config.py` + `config.example.yml`)
```yaml
auth:
  enabled: true            # if false, authz is skipped (dev/legacy mode)
  ldap_url: "ldaps://ipa.example.com"
  user_dn_template: "uid={username},cn=users,cn=accounts,dc=example,dc=com"
  group_base: "cn=groups,cn=accounts,dc=example,dc=com"
  bind_dn: ""              # optional service account for group lookups
  bind_password: ""        # read from env WSI_LDAP_BIND_PASSWORD ideally
  session_secret: "..."    # env WSI_SESSION_SECRET
  session_ttl: 86400
  authz_ttl: 300           # redis TTL for positive authz decisions
  authz_ttl_deny: 60       # TTL for denied decisions
  acl_socket: "/run/wsi/aclcheck.sock"
```
Secrets (`session_secret`, `bind_password`) read from env vars, not committed.

---

## Container / runtime changes
- **Dockerfile**: replace `USER wsi` + `CMD` with a root entrypoint script that:
  1. `mkdir -p /run/wsi && chown wsi:wsi /run/wsi`
  2. starts `aclcheckd` as root (e.g. `python -m app.aclcheckd &`)
  3. `exec gosu wsi uvicorn ...` (add `gosu` or use `runuser`).
- `aclcheckd` must run as root to `setuid` to arbitrary users. This is the only
  privileged component. `CAP_SETUID`/`CAP_SETGID` are the real requirement; root
  is the prototype-simple way.
- Data mounts stay `:ro`. ✅
- No need for the container to be an IPA client / have `/etc/passwd` entries —
  the helper switches by **numeric** UID/GID only, exactly what we get from LDAP.
- `aclcheckd` should validate the socket perms so only `wsi` can connect.

---

## Implementation order
1. `auth.py`: LDAP login + session cookie + `current_principal` dep. Add config.
   (`python-ldap` or `ldap3` dependency — prefer `ldap3`, pure-python, easier in
   slim containers.)
2. `aclcheckd.py` + `authz.py`: helper daemon + client + Redis decision cache.
   Test standalone first (CLI: given uid/gid/gids + path → prints ok).
3. Wire `current_principal` + `authz.can_read` into tile/thumb/dzi/meta/etc.
   (the reorder). Fail-closed everywhere.
4. Per-user tree/expand/dir via `authz.list_dir`; per-user cache keys.
5. Frontend login overlay + 401/403 handling in `useRequests`.
6. Dockerfile/compose entrypoint changes; integration test against real FreeIPA
   + NFS with two users of differing group membership.

---

## Explicitly out of scope (prototype)
- Per-user OpenSlide workers / setuid tile generation (tiles still read as `wsi`;
  authz gate at the endpoint is the trust boundary).
- Keycloak / OIDC (auth backend is interface-isolated for later swap).
- Re-implementing NFSv4 ACL parsing (we test real reads instead).
- Fine-grained UI hiding of denied roots (denied paths return 403/empty at
  expand time; the root list itself may still show labels — acceptable for v1).
- Cache invalidation on group membership changes (short TTL handles it).

---

## Risks / notes
- **Fail-closed default**: every authz path returns deny on helper timeout/error,
  missing principal, or `auth.enabled` ambiguity. `auth.enabled=false` is the
  only "open" mode, for local dev.
- **`os.access` caveat on NFS**: can over-report; mitigated by also doing a real
  `open(O_RDONLY)` in the helper. Deny-side is reliable (an `open` that fails
  EACCES is authoritative).
- **Decision-cache poisoning**: only `ok` results for *this uid*; keys are
  uid-scoped, so no cross-user leak. Global tile cache is safe because it is
  only reached *after* a per-uid authz allow.
- **16-group NFS limit**: you've already solved server-side via group-mapped
  ACLs; the helper sets the full supplementary group list via `setgroups`, and
  the NFS server does the rest. No client-side limit handling needed.
