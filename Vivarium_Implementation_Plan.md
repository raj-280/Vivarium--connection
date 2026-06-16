# Vivarium Gantry System — Full Implementation Plan (v2)

> This version reorganizes the plan around two physical deliverables — a **server** package and a **pi** package — and folds in every security control from the architecture document that was missing from the original plan: rack locking, command queuing, position verification/recovery, manual-vs-auto-scan conflict handling, image-delivery scoping, MQTT ACL/Ansible details for the image topic, capture lock-keepalive signalling, and device lifecycle management. No source code is included anywhere — every item below is a description of what a file/module/table must do, written so it can be implemented later.

**Core principles carried through everything below:**
- Every behaviour is driven by configuration (`.env` on the server, `device.conf` on each Pi). Nothing is hard-coded.
- The database is **SQLite** for now. Every table is designed so the same schema works unchanged on PostgreSQL later — just change `DATABASE_URL`.
- **S3 stays off** until credentials exist. A single `S3_ENABLED` flag controls whether the capture/scan flow uses pre-signed URLs or simply writes images to local disk on the server/Pi. No code changes are needed to turn it on.
- **Redis is optional.** A `CACHE_BACKEND` flag selects `redis` or `sqlite` (a small local table acts as the pending-command/lock/rate-limit store when Redis isn't available).
- Live video uses **go2rtc** end-to-end (Pi → RTSP → server relay → WebRTC in browser), exactly as in the architecture document.
- All `[PROD ONLY]` items are inert/disabled in local development but must exist as configuration switches from day one, so enabling them later is a config change, not a rewrite.

---

## 1. Top-Level Project Layout

```
vivarium/                         ← project root
│
├── frontend/                     ← existing React/Vite app (Section 7)
├── server/                       ← NEW — everything that runs on the central machine (Section 4)
├── pi/                           ← NEW — everything that runs on each Raspberry Pi (Section 5)
├── arduino/                      ← existing firmware, unchanged (Section 6)
└── docs/
    ├── mqtt_topics.md            ← topic reference (Section 11)
    ├── db_schema.md              ← schema reference (Section 3)
    └── security_matrix.md        ← consolidated security checklist (Section 9)
```

The detailed folder trees for `server/` and `pi/` are given inside their own sections (4 and 5) rather than here, so each section is self-contained and can be handed to whoever is building that piece.

---

## 2. Configuration Strategy

Two configuration surfaces exist. Both are plain key/value files — `.env` for the server (loaded via pydantic-settings) and `/etc/gantry/device.conf` (INI-style) for each Pi. Nothing below is code; it's the list of settings each module reads.

### 2.1 Server configuration groups (`server/.env`)

| Group | Keys | Purpose |
|---|---|---|
| MQTT | `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_USE_TLS`, `MQTT_TLS_CA_PATH` | Connection to Mosquitto. TLS keys are blank/unused locally, populated in production. |
| Server | `BACKEND_HOST`, `BACKEND_PORT`, `CORS_ALLOWED_ORIGINS` | FastAPI bind address and allowed frontend origins. |
| Auth | `ADMIN_TOKEN`, `PI_API_KEY`, `JWT_SECRET_KEY`, `JWT_EXPIRE_MINUTES`, `CSRF_ENABLED`, `COOKIE_SECURE` | Two independent credential spaces (browser vs Pi) plus toggles for CSRF/secure cookies — `false` locally, `true` in production. |
| Provisioning | `PROVISIONING_SECRET`, `PROVISION_TOKEN_TTL_HOURS` | Shared secret baked into SD images; token lifetime for pre-assigned devices. |
| Database | `DATABASE_URL` | `sqlite:///./vivarium.db` locally; swap to a PostgreSQL URL later — no other change needed. |
| Cache / Rate-limit | `CACHE_BACKEND` (`sqlite`/`redis`), `REDIS_URL` | Selects where pending-command tracking, locks, and rate-limit counters live. |
| S3 | `S3_ENABLED`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `S3_BUCKET`, `S3_ENDPOINT_URL`, `LOCAL_IMAGE_DIR` | When `S3_ENABLED=false`, images are written to `LOCAL_IMAGE_DIR` on the server instead of S3, and `/presign` returns a local-upload target instead of an S3 URL. |
| Timeouts | `COMMAND_TIMEOUT_S`, `MOTION_TIMEOUT_S`, `HOMING_TIMEOUT_S`, `CAPTURE_LOCK_TIMEOUT_S`, `MANUAL_VS_SCAN_RESUME_WINDOW_S` | Drive the escalation ladder and lock lifetimes described in Sections 4.3–4.5. |
| Rack geometry defaults | `RACK_ROWS`, `RACK_COLS`, `X0_OFFSET_MM`, `PITCH_X_MM`, `Y0_OFFSET_MM`, `PITCH_Y_MM`, `POSITION_TOLERANCE_X_MM`, `POSITION_TOLERANCE_Y_MM` | Defaults applied when a new rack row is created; each rack can override these in its own row in `racks`. |
| Streaming | `GO2RTC_INTERNAL_URL`, `GO2RTC_PROXY_PATH`, `STREAM_URL_TTL_S` | Used by `services/streaming.py` to build per-rack stream URLs (Section 4.2, 8). |
| Scan engine | `SCAN_POSTPONE_MINUTES`, `SCAN_STAGGER_GROUP_SIZE`, `SCAN_STAGGER_DELAY_MINUTES` | Controls scheduling and bandwidth staggering for auto-scan across many racks. |
| Production toggles | `TLS_ENABLED`, `HSTS_ENABLED`, `CSP_ENABLED`, `WIREGUARD_REQUIRED`, `ANSIBLE_INVENTORY_PATH` | All default to `false`/empty locally; flipping them is the entirety of the "go to production" work for that control. |

### 2.2 Pi configuration groups (`/etc/gantry/device.conf`, mode 600)

| Group | Keys | Purpose |
|---|---|---|
| Identity | `device_id`, `cpu_serial` (read-only, not stored after provisioning) | This Pi's logical rack ID, e.g. `rack-047`. |
| Server | `server_host`, `presign_api_key`, `mqtt_password`, `rtsp_password` | Credentials issued by `/provision` (Section 5.3). |
| MQTT | `broker_host`, `broker_port`, `mqtt_use_tls`, `ca_cert_path` | Mirrors server-side MQTT settings; TLS fields unused locally. |
| Serial | `serial_port`, `serial_baud`, `serial_timeout_s`, `serial_retry_count` | Arduino link configuration. |
| Camera / Capture | `capture_dir`, `batch_upload_enabled`, `tmp_is_tmpfs` | Local-first capture; batch upload to S3 stays dormant until `batch_upload_enabled=true` **and** the server has `S3_ENABLED=true`. |
| Streaming | `go2rtc_port`, `go2rtc_stream_name` | Stream name always matches `device_id` so it lines up with the server's stream registry. |
| Scan engine | `scan_lock_keepalive_interval_s` | How often the Pi pings the server to extend a scan lock. |

---

## 3. Database Schema (SQLite now, PostgreSQL-ready)

All tables live in one SQLite file (`vivarium.db`) for development. Every column choice below avoids SQLite-incompatible types so the same schema can be replayed against PostgreSQL with only the engine URL changed. Foreign keys are enforced at the application layer in SQLite and at the database layer once on PostgreSQL.

### 3.1 `racks`
| Column | Type | Notes |
|---|---|---|
| `id` | text, PK | e.g. `rack-047`. |
| `display_name` | text | Human label. |
| `location` | text | Free-text physical location. |
| `pi_ip` | text | Used by go2rtc to build its pull list and by the server to reach the Pi's presign callbacks if ever needed. |
| `mqtt_username` | text | Per-Pi MQTT identity. |
| `rtsp_password_ref` | text | Reference/handle to the RTSP credential (not the raw secret in plaintext columns once production secrets management is added). |
| `presign_api_key_ref` | text | Same pattern as above for the presign API key. |
| `grid_rows`, `grid_cols` | integer | Defaults from `.env`, overridable per rack. |
| `x0_offset_mm`, `pitch_x_mm`, `y0_offset_mm`, `pitch_y_mm` | real | Geometry, per rack. |
| `position_tolerance_x_mm`, `position_tolerance_y_mm` | real | Per Section 4.4. |
| `mqtt_status` | text | `online` / `offline`, updated by Last Will + heartbeat. |
| `camera_status` | text | `online` / `offline` / `unknown`, updated by go2rtc health checks. |
| `last_position_x`, `last_position_y`, `last_position_c` | real | Last reported M114 values. |
| `homed_x`, `homed_y`, `homed_c` | boolean | From the most recent M114 homed flags. |
| `last_homed_at` | datetime | Used by the "homing required after 12h / reboot" rule (Section 4.4). |
| `lock_holder_user_id` | text, FK → `users.id`, nullable | Current lock owner. |
| `lock_type` | text, nullable | `motion` / `capture` / `scan`. |
| `lock_acquired_at`, `lock_expires_at` | datetime, nullable | Drives the 30s / 120s lock expiries (Section 4.3). |
| `scan_state` | text | `idle` / `running` / `paused` / `complete` / `aborted`. |
| `maintenance_required` | boolean | Set by the escalation ladder; disables auto-scan when true. |
| `created_at`, `updated_at` | datetime | |

### 3.2 `users`
| Column | Type | Notes |
|---|---|---|
| `id` | text, PK | |
| `username` | text, unique | |
| `password_hash` | text | bcrypt via passlib. |
| `role` | text | `viewer` / `operator` / `admin`. |
| `created_at`, `last_login_at` | datetime | |

### 3.3 `user_rack_assignments`
| Column | Type | Notes |
|---|---|---|
| `user_id` | text, FK → `users.id` | |
| `rack_id` | text, FK → `racks.id` | |
| Composite PK on (`user_id`, `rack_id`) | | Defines which racks an **operator** may command, lock, or view a camera stream for. Admins bypass this table; viewers are read-only regardless of assignment. |

### 3.4 `image_records`
| Column | Type | Notes |
|---|---|---|
| `id` | integer, PK autoincrement | |
| `rack_id` | text, FK → `racks.id` | |
| `s3_key` | text, **UNIQUE** | Enforced at the DB level so duplicate MQTT image notifications are rejected (SQLite `UNIQUE` raises on insert; PostgreSQL raises `23505`). |
| `local_path` | text, nullable | Populated instead of/alongside `s3_key` when `S3_ENABLED=false`. |
| `sha256_checksum` | text | Computed on the Pi, carried through for verification. |
| `triggered_by_operator` | text, FK → `users.id`, nullable | Null + audit flag if attribution expired (Section 4.3). |
| `trigger_type` | text | `manual` / `auto_scan`. |
| `scan_session_id` | integer, FK → `scan_sessions.id`, nullable | |
| `cell_row`, `cell_col` | integer, nullable | Only set for scan-triggered images. |
| `capture_timestamp` | datetime | From the Pi's filename timestamp. |
| `created_at` | datetime | |

### 3.5 `audit_log`
| Column | Type | Notes |
|---|---|---|
| `id` | integer, PK autoincrement | |
| `event_type` | text | `capture_triggered`, `presign_issued`, `upload_confirmed`, `image_notification_received`, `duplicate_image_notification`, `validation_failure`, `command_published`, `command_ack_missing`, `position_error`, `re_home_triggered`, `maintenance_flagged`, `stream_opened`, `stream_closed`, `provisioning_event`, etc. |
| `rack_id` | text, nullable | |
| `user_id` | text, nullable | |
| `pi_credential_ref` | text, nullable | |
| `details` | text (JSON) | Free-form context for the event. |
| `outcome` | text | `success` / `failure` / `flagged`. |
| `created_at` | datetime | |

### 3.6 `scan_sessions`
| Column | Type | Notes |
|---|---|---|
| `id` | integer, PK autoincrement | |
| `rack_id` | text, FK → `racks.id` | |
| `status` | text | `running` / `paused` / `complete` / `aborted`. |
| `started_at`, `completed_at` | datetime, nullable | |
| `cells_total`, `cells_completed`, `cells_failed` | integer | |
| `last_completed_row`, `last_completed_col` | integer, nullable | Resume point for paused scans. |
| `abort_reason` | text, nullable | e.g. `emergency_stop`. |

### 3.7 `scan_schedule`
| Column | Type | Notes |
|---|---|---|
| `rack_id` | text, PK, FK → `racks.id` | |
| `interval_hours` | real | How often this rack auto-scans. |
| `next_scan_at` | datetime | Checked every minute by APScheduler. |
| `last_scan_started_at` | datetime, nullable | |
| `enabled` | boolean | Set to `false` automatically when `maintenance_required` is true. |

### 3.8 `device_pool`
| Column | Type | Notes |
|---|---|---|
| `device_id` | text, PK | e.g. `rack-001` … `rack-300`. |
| `assigned` | boolean | |
| `cpu_serial` | text, nullable | |
| `assigned_at` | datetime, nullable | |

### 3.9 `provision_tokens`
| Column | Type | Notes |
|---|---|---|
| `token` | text, PK | One-time, shown once in the UI. |
| `device_id` | text, FK → `device_pool.device_id` | Pre-assigned target. |
| `expires_at` | datetime | |
| `used` | boolean | |
| `created_at` | datetime | |

### 3.10 `certificates` *(production only — table exists from day one but unused locally)*
| Column | Type | Notes |
|---|---|---|
| `rack_id` | text, FK → `racks.id` | |
| `cert_serial` | text | |
| `issued_at`, `expires_at`, `last_rotated_at` | datetime | Surfaced on the admin dashboard for the Ansible rotation job (Section 9). |

### 3.11 `pending_commands` *(used only when `CACHE_BACKEND=sqlite`)*
| Column | Type | Notes |
|---|---|---|
| `rack_id` | text, PK | One outstanding command per rack at a time. |
| `command` | text | |
| `operator_id` | text, nullable | |
| `published_at` | datetime | |
| `retry_count` | integer | |
| `timeout_at` | datetime | Background task (Section 4.5) polls this every 2s; identical semantics to the Redis key `pending_cmd:{rack_id}` described in the architecture doc. |

### 3.12 `capture_attribution` *(used only when `CACHE_BACKEND=sqlite`)*
| Column | Type | Notes |
|---|---|---|
| `rack_id` | text, PK | |
| `operator_id` | text | |
| `expires_at` | datetime | Mirrors the Redis key `{rack_id → operator_id, expire_at: now+120s}`. Consumed (deleted) when the image notification arrives; if expired first, `image_records.triggered_by_operator` is written as null and an `audit_log` entry of type `validation_failure` (or a dedicated `attribution_expired`) is written. |

---

## 4. Server Package (`server/`)

### 4.1 Folder Structure

```
server/
├── config/
│   └── settings.py                  ← loads every key from Section 2.1
│
├── db/
│   ├── database.py                  ← engine + session factory (SQLite now, swappable)
│   └── models.py                    ← ORM models for every table in Section 3
│
├── core/
│   ├── state.py                     ← in-memory GantryState mirror per rack (position, status, pi_online)
│   ├── security.py                  ← JWT issue/validate, password hashing, role checks
│   ├── locking.py                   ← Rack Locking (4.3)
│   └── queue_manager.py             ← Per-rack Command Queue (4.3)
│
├── middleware/
│   ├── auth.py                      ← role enforcement (Viewer/Operator/Admin), routes ADMIN_TOKEN vs PI_API_KEY
│   ├── rate_limit.py                ← slowapi: global per-user + 2/min on /presign + per-Pi MQTT rate limit hooks
│   └── csrf.py                      ← double-submit cookie validation (active even in dev, cookie just non-Secure)
│
├── services/
│   ├── mqtt_client.py               ← paho-mqtt connect/pub/sub, Last Will handling
│   ├── command_handler.py           ← whitelist + M700/M701-704 range validation
│   ├── s3_handler.py                ← pre-signed PUT/GET when S3_ENABLED, else local-disk equivalent
│   ├── provisioning.py              ← POST /provision logic (4.6)
│   ├── streaming.py                 ← go2rtc stream URL builder from `racks.pi_ip` (Section 8)
│   ├── scan_engine.py               ← APScheduler-driven auto-scan scheduling (4.7)
│   ├── position_monitor.py          ← homed-flag + tolerance checks, re-home triggers (4.4)
│   └── cache.py                     ← uniform get/set/expire interface backed by Redis or `pending_commands` / `capture_attribution` tables
│
├── api/
│   ├── websocket.py                 ← /ws — JWT-on-connect, broadcasts MQTT → browser, routes capture_complete to the lock holder only
│   └── routes.py                    ← /health, /command, /provision, /rack/{id}/presign, /rack/{id}/lock, /devices (pending/add)
│
├── main.py                          ← FastAPI app + lifespan (starts MQTT client, APScheduler, cache backend)
├── requirements.txt
├── .env.example
└── Dockerfile
```

### 4.2 Module Responsibilities (no code, behaviour only)

- **`config/settings.py`** — single source of truth for every key in Section 2.1; every other module imports `settings` rather than reading the environment directly.
- **`db/database.py` / `db/models.py`** — one ORM model per table in Section 3; `database.py` exposes a session dependency for FastAPI routes and a context-manager session for background tasks (scan engine, position monitor).
- **`core/state.py`** — a per-rack in-memory dictionary mirroring the live `racks` row (position, online flags, scan state) so the WebSocket layer doesn't hit the database on every position update; periodically reconciled with the DB.
- **`core/security.py`** — JWT creation/validation for the three roles, password hashing for the `users` table, and helper functions used by `middleware/auth.py`.
- **`middleware/auth.py`** — for every request, determines whether the caller is a browser (JWT/`ADMIN_TOKEN` cookie), a Pi (`PI_API_KEY` on `/presign` only), checks role against the requested rack via `user_rack_assignments`, and rejects mismatched credential types (e.g. an MQTT credential presented to `/presign`).
- **`middleware/rate_limit.py`** — applies per-user limits to all command endpoints, a stricter 2/min limit on `/presign` keyed by Pi credential, and exposes a hook the MQTT client can call to enforce the 60 msg/min per-Pi publish limit when `CACHE_BACKEND` tracks it.
- **`middleware/csrf.py`** — issues a CSRF token in a `Secure SameSite=Strict` cookie (Secure flag controlled by `COOKIE_SECURE`), and validates it on every POST/PUT and on the WebSocket `CAPTURE`/command messages when `CSRF_ENABLED=true`.

### 4.3 Rack Locking & Command Queue (NEW — previously missing)

This is the section that was entirely absent from the original plan despite being central to the architecture document.

**Rack Locking (`core/locking.py`)**
- Before any command (including `CAPTURE`) is published to MQTT, the server checks `racks.lock_holder_user_id` for that rack.
- If unlocked, it writes `lock_holder_user_id`, `lock_type`, `lock_acquired_at`, and `lock_expires_at` in the same transaction, then proceeds to publish.
- **Motion commands**: lock expiry = `now + MOTION_TIMEOUT_S` (with auto-release if no `M114` arrives — see 4.5).
- **Capture commands**: lock expiry = `now + CAPTURE_LOCK_TIMEOUT_S` (120s by default), because the upload cycle is longer than a motion command.
- **Lock-keepalive for captures**: when the Pi publishes `CAPTURE_STARTED`, the server resets `lock_expires_at` to `now + CAPTURE_LOCK_TIMEOUT_S` again — this prevents the lock from expiring during a slow S3 upload. `CAPTURE_DONE` (or `capture_complete`/`image` notification) releases the lock immediately.
- **Scan commands**: a scan-lock covers the entire session; the Pi sends a keepalive every `scan_lock_keepalive_interval_s` and the server extends `lock_expires_at` accordingly (Section 4.7).
- A background sweep (the same loop as 4.5) releases any lock whose `lock_expires_at` has passed, regardless of cause — this is the "30-second auto-release" guarantee from the architecture doc, generalised to all lock types.

**Command Queue (`core/queue_manager.py`)**
- One FIFO queue per rack, held in memory (rebuilt from `pending_commands`/`capture_attribution` on restart if needed for durability).
- If a command arrives for a rack that is currently locked, it is appended to that rack's queue instead of being rejected.
- When the active lock releases (success, timeout, or manual release), the queue manager pops the next item, acquires a fresh lock, and publishes it.
- **`!` (emergency stop)** always jumps to the front of the queue and bypasses the lock check entirely — it is published immediately regardless of any existing lock or queue state.
- `CAPTURE` is queued like any other command: if a motion command is in progress when an operator clicks Capture, the capture waits for the motion to finish, then runs.

**Image delivery scoping (capture_complete / scan_cell_complete)**
- `capture_complete` is delivered over `/ws` **only** to the WebSocket connection belonging to the `lock_holder_user_id` recorded at the time the capture was triggered — never broadcast. The server resolves the session from the lock record and routes to that connection only. Viewer-role connections never receive this message type.
- `scan_cell_complete` (auto-scan) is broadcast to **all** viewers of that rack, since no single operator triggered it.

### 4.4 Position Verification & Recovery (NEW — previously missing)

Three checks run continuously in `services/position_monitor.py`, fed by every `M114` response relayed through MQTT:

1. **Homed-flag check** — every `M114` carries `homed: X=?/Y=?/C=?`. If any axis is `N`, that position is treated as unreliable. For an **auto-scan**, this always triggers a mandatory `G28` before the scan's first move (never skipped). For a **manual command**, the server blocks the command and asks the operator "Gantry not homed — home first?"; on confirmation it issues `G28` then the original command.
2. **Tolerance check** — the server compares the reported `X`/`Y` to the commanded target. If the difference exceeds `position_tolerance_x_mm` / `position_tolerance_y_mm` (defaults 3mm / 2mm, per-rack overridable), a position error is recorded and the automatic recovery sequence below fires.
3. **Stale-homing check** — on the first command after server restart or Pi reconnect, if `racks.last_homed_at` is null or older than a configurable threshold (recommended 12 hours), the operator is prompted to home before proceeding — this guards against a step count that survived a reboot but no longer matches reality.

**Automatic recovery sequence (on tolerance failure or `STALL_DETECTED`/`SERIAL_TIMEOUT`):**
1. Server publishes `!` (QoS 2) then `G28` (QoS 1).
2. Pi forwards both to the Arduino; Arduino runs the full homing sequence (camera retract, Y dual-square, X to minimum switch).
3. Server sets the rack's UI status to `re-homing` (orange) while this is in progress.
4. On successful homing, `racks.last_homed_at` is updated and the **original failed command is retried from scratch**.
5. If the position error recurs after re-home, `racks.maintenance_required = true` (red indicator), `scan_schedule.enabled = false` for that rack, and an admin alert is pushed over `/ws`.

This is the same five-level escalation ladder (Section 9 table) wired to concrete DB fields and a concrete monitoring module.

### 4.5 Pending-Command Tracking & Escalation

- On every command publish, `services/mqtt_client.py` (via `services/cache.py`) writes a pending-command entry: `{command, operator_id, published_at, retry_count: 0, timeout_at}` — backed by Redis (`pending_cmd:{rack_id}`) or the `pending_commands` table, depending on `CACHE_BACKEND`.
- A background task (started in `main.py`'s lifespan) runs every 2 seconds, scans all pending entries, and:
  - If `timeout_at` passed and `retry_count == 0`: re-publish the command once, increment `retry_count`.
  - If `timeout_at` passed and `retry_count == 1`: trigger the L2 escalation (`!` then `G28`, Section 4.4) and mark the lock for release.
  - If recovery fails twice total: `maintenance_required = true`, auto-scan disabled, admin alerted — matching the L3 "Suspended" row of the fallback ladder.
- `COMMAND_ACK:{command}` (published by the Pi immediately on receipt, before forwarding to the Arduino) clears the "no ACK" failure mode; absence of `M114` after an ACK clears into the "motion unconfirmed" failure mode. The pending-command entry is annotated with which signal was/wasn't received so the audit log records the correct failure category.

### 4.6 Provisioning Service (`services/provisioning.py`)

- `POST /provision` validates `provisioning_secret` (401 if wrong), then — if present — validates `provision_token` against `provision_tokens` (401 if invalid/expired/used).
- If `cpu_serial` already exists in `device_pool`, returns the existing credentials unchanged (idempotent — safe to reflash an SD card).
- Device-ID assignment: if a token was supplied, use its pre-assigned `device_id`; otherwise select the next unassigned row from `device_pool` using `SELECT ... FOR UPDATE SKIP LOCKED` semantics (on SQLite this is approximated with an immediate-transaction + retry loop, since SQLite lacks true row-level locking — documented as a known limitation to revisit on the PostgreSQL migration).
- On success: generates `mqtt_password`, `presign_api_key`, `rtsp_password` (256-bit random each), inserts/updates the `racks` row, creates the MQTT ACL entry (Section 9), adds the rack to the go2rtc pull list (Section 8), marks the token used, and writes a `provisioning_event` audit row.
- **Hardware replacement**: if a Pi reports a new `cpu_serial` for a `device_id` that has been offline for more than 7 days, it is auto-reassigned to that `device_id` with the same logical identity (all history, dashboards, and go2rtc stream name stay intact). Otherwise an admin assigns it manually from the "pending devices" UI.

### 4.7 Auto-Scan Engine (`services/scan_engine.py`)

- APScheduler wakes every minute, checks `scan_schedule` for rows where `next_scan_at` has passed.
- Before firing `SCAN_START`, two gates: (a) Pi heartbeat within the last 60 seconds — if not, postpone by `SCAN_POSTPONE_MINUTES`; (b) rack not currently locked by an operator — if locked, same postponement. The scan is never skipped, only delayed.
- On both gates passing: publish `SCAN_START` to the existing command topic, create a `scan_sessions` row, update `last_scan_started_at`, and acquire a scan-type lock (Section 4.3).
- Monitors `scan_progress` / `scan_status` messages, updating `scan_sessions.cells_completed/failed` and `racks.scan_state` as they arrive; updates `image_records` for each photo with `trigger_type=auto_scan`, `scan_session_id`, `cell_row`, `cell_col`.
- **Pause/Resume**: `SCAN_STOP` (operator-initiated or from the manual-command conflict flow below) is published; on the next `scan_status: paused` message, `scan_sessions.status='paused'` and `last_completed_row/col` are recorded. `SCAN_START` with the same `scan_session_id` resumes from the next cell (mandatory re-home still applies first).
- **Bandwidth staggering**: when many racks share a scan window, `SCAN_STAGGER_GROUP_SIZE` and `SCAN_STAGGER_DELAY_MINUTES` group racks into batches with offset start times — a config-only change, no Pi update required.

### 4.8 Manual-vs-Auto-Scan Conflict Resolution (NEW — previously missing)

When a manual command arrives for a rack mid-scan:
1. `racks.scan_state` is set to `paused` immediately.
2. The Pi finishes the **current** cell's full sequence (move, temperature read, photo, camera-out) — never interrupted mid-motion.
3. The Pi checks for `SCAN_STOP` between cells, finds it, and publishes `scan_status: paused` with the last-completed cell.
4. The manual command then executes under its own fresh lock.
5. After the manual command completes, the WebSocket offers the operator two choices: **resume scan from last cell** or **restart from beginning** — surfaced as a simple prompt in the UI.
6. If no response within `MANUAL_VS_SCAN_RESUME_WINDOW_S` (default 5 minutes), the scan restarts from the beginning at the next scheduled interval.
7. **Emergency stop overrides all of this** — `!` stops the Arduino immediately regardless of scan/manual state; the session is marked `aborted` with `abort_reason='emergency_stop'`, the lock is released, and a `G28` is required before any further motion (enforced by the homed-flag check in 4.4).

---

## 5. Pi Package (`pi/`)

### 5.1 Folder Structure

```
pi/
├── config/
│   └── settings.py                  ← reads /etc/gantry/device.conf (Section 2.2)
│
├── services/
│   ├── serial_handler.py            ← pyserial read/write, per-command timeouts, retry-once logic
│   ├── camera_handler.py            ← capture → local save → (optional) S3 batch upload
│   ├── mqtt_client.py                ← paho-mqtt, Last Will, QoS 1 commands / QoS 2 emergency
│   ├── scan_executor.py             ← runs the 84-cell scan sequence (5.5)
│   └── go2rtc_health.py             ← periodic check that the go2rtc systemd unit is up; reports camera_status
│
├── provisioner.py                   ← first-boot identity flow (5.3)
├── bridge.py                        ← main loop: MQTT ↔ serial coordinator + heartbeat (5.2)
│
├── go2rtc/
│   └── go2rtc.example.yaml          ← reference config (stream name = device_id, RTSP credentials from device.conf) — not executed code, a template the provisioner fills in
│
├── device.conf.example
├── requirements.txt
└── systemd/
    ├── vivarium-provisioner.service ← pre-start, runs provisioner.py, exits fast if device.conf exists
    ├── vivarium-bridge.service      ← main bridge, starts after provisioner
    └── vivarium-camera.service      ← go2rtc, independent service — runs even if the bridge crashes
```

### 5.2 `bridge.py` — Main Loop Responsibilities

- On any message received on `vivarium/rack/{id}/command`, immediately publish `COMMAND_ACK:{command}` **before** forwarding to the Arduino — this is the signal that lets the server distinguish "Pi never got it" from "Pi got it but Arduino didn't respond."
- `CAPTURE` and `SCAN_START`/`SCAN_STOP` are intercepted here and never forwarded to the Arduino over serial; everything else is forwarded as-is.
- Heartbeat published to `vivarium/rack/{id}/status` every 30 seconds; Last Will registered at connect time so the broker auto-publishes `{"status":"offline","reason":"unexpected_disconnect"}` (QoS 1, retained) if the Pi disappears.
- **Serial retry**: if no response within `serial_timeout_s`, retry once after 1 second; if still nothing, publish `SERIAL_TIMEOUT:{command}`.
- **Reconnect cleanup**: on every reconnect (MQTT or process restart), run — Arduino health check → homed-flag verify → `/tmp` sweep (delete any leftover `rack-*-*.jpg`) → publish `BRIDGE_RECONNECTED`.

### 5.3 Provisioner (`provisioner.py`)

- If `/etc/gantry/device.conf` exists, exit immediately (milliseconds) — the bridge starts normally.
- Otherwise: read the CPU serial from `/proc/cpuinfo`, `POST /provision` with `{cpu_serial, provisioning_secret, provision_token?}`, write the returned credentials into `device.conf` with `chmod 600`, then delete the provisioning secret and token from disk so they can never be read again.
- This file is the **only** thing on the SD card that differs between "auto-assign" and "pre-assigned" images: auto-assign images carry only `PROVISIONING_SECRET`; pre-assigned images additionally carry a one-time `PROVISION_TOKEN` generated from the "Devices → Add New" admin page.

### 5.4 Camera / Capture Handler (`services/camera_handler.py`)

- `capture(row, col)`:
  1. Take a photo (`libcamera-still`) into `/tmp/rack-{id}-{timestamp}.jpg`, permissions 600. `/tmp` is mounted as tmpfs (RAM only — `tmp_is_tmpfs=true` in `device.conf`), so the SD card is never touched.
  2. Compute the SHA-256 hash.
  3. Publish `CAPTURE_STARTED` on the response topic — this is the lock-keepalive signal the server resets the capture lock on (Section 4.3).
  4. If `S3_ENABLED` (server) and `batch_upload_enabled` (Pi): `POST /rack/{id}/presign` with the hash, then `PUT` the file directly to the returned URL.
  5. If S3 is **not** enabled: save into `capture_dir/{rack_id}/{date}/` instead, and the `/image` MQTT message carries `local_path` rather than `s3_key`.
  6. Either way, publish to `vivarium/rack/{id}/image` with the key/path + timestamp, then publish `CAPTURE_DONE`, then delete the `/tmp` file.
- `batch_upload()` — only relevant when `batch_upload_enabled=true`: iterates anything still sitting in `capture_dir`, presigns + uploads + deletes locally on success. Completely dormant (no-op) until that flag is set, which itself is gated by `S3_ENABLED` on the server.

### 5.5 Scan Executor (`services/scan_executor.py`)

Runs **inside** the same process as `bridge.py` (not a separate process) because it must own the same serial port — a second process writing to `/dev/ttyACM0` would corrupt commands.

On `SCAN_START`:
1. Acquire/confirm the scan lock; start the keepalive timer (`scan_lock_keepalive_interval_s`).
2. **Always** send `G28` first and wait for all three homing confirmations — never skipped, even on resume.
3. Loop the configured grid (12×7 = 84 cells by default) in a **snake pattern** (alternating direction per row to roughly halve total travel): for each cell — `M700 Rn Cn` → wait `M114` → read temperature → `M710` (camera in) → capture (5.4) → `M711` (camera out) → publish `scan_progress` → check for `SCAN_STOP` before the next cell.
4. On completion, `G28` again, camera left at OUT (C=0mm).
5. Publish `scan_status: complete` with a summary (cells captured/failed/duration) and release the lock.

`SCAN_STOP` is only acted on **between** cells, never mid-motion (Section 4.8).

### 5.6 Live Streaming Agent (go2rtc on the Pi)

- go2rtc runs as its own systemd unit (`vivarium-camera.service`), completely independent of `bridge.py` — if the bridge crashes, the stream keeps running, and vice versa.
- Reads the camera (CSI Pi Camera Module or USB `/dev/video0`) and exposes it as RTSP on port 8554, named after `device_id` so the stream name always matches the rack ID.
- RTSP digest authentication uses the `rtsp_password` issued during provisioning (Section 5.3), stored alongside the MQTT credential at 600 permissions.
- `services/go2rtc_health.py` periodically checks the unit is active and reports `camera_status` in the heartbeat, independent of `mqtt_status`.

---

## 6. Arduino Layer (unchanged)

No changes to `arduino/RackMonitor_Mega_IS_S.ino`. Three fallback layers already live in firmware and remain the last line of defence: per-axis stall watchdog (500ms no step-counter movement while `distanceToGo != 0` → `STALL_DETECTED`), serial watchdog (60s silence during active motion → `SERIAL_TIMEOUT_ESTOP`, stop all motors), and the hardware E-stop wired to an interrupt pin (microsecond response, independent of all software). `CAPTURE` never reaches the Arduino — it's intercepted by the Pi bridge.

---

## 7. Frontend (`frontend/`) — Wiring Summary

Unchanged in scope from the original plan; listed briefly here for completeness since it consumes the server's WebSocket and stream URLs:

- New: `config/app.config.ts` (WS URL, jog steps, rack dimensions, stream paths — all reading from `VITE_*` env vars so they're configurable per deployment).
- New: `types/gantry.types.ts` (shared message types, including `stream_url`, `capture_complete`, `scan_cell_complete`, `alert`).
- New components: `ConnectionBar` (WS/MQTT/Pi status), `EmergencyStop` (always visible, never disabled, sends `!`), `GantryGrid` (grid colored by cell state, click → `M700 Rn Cn`), `CameraPanel` (`<video>` for WebRTC + Capture button + spinner driven by `CAPTURE_STARTED`/`capture_complete`).
- `SystemContext` extended with `wsStatus`, `gantryPosition`, `piOnline`, `gridCells`, `streamUrl`, `userRole`, `sendCommand()`.
- Role enforcement is **also** done server-side (4.2/4.3) — the frontend role checks are a UX convenience, not the security boundary.
- CSRF: when `CSRF_ENABLED=true`, the frontend reads the CSRF cookie and attaches it as a header on every POST/PUT and on the WebSocket `CAPTURE`/command messages.

---

## 8. Live Streaming Architecture (go2rtc, End-to-End)

```
Pi Camera (CSI or USB) ──► go2rtc agent on Pi (port 8554, RTSP, digest auth)
                                   │  RTSP pull (server → Pi, server IP allow-listed via UFW on the Pi)
                                   ▼
                    go2rtc relay on the server (port 1984, bound to localhost only)
                                   │  reverse-proxied at /camera/ by Nginx, behind the same JWT as motion commands
                                   ▼
                          Browser <video> element — WebRTC, sub-200ms latency
                                   │
                          MJPEG available as a fallback at /camera/mjpeg?src={rack_id}
```

**How the browser gets a stream URL:**
1. Operator selects a rack → the existing rack-lock flow runs (Section 4.3).
2. Once the lock is acquired, `services/streaming.py` builds `{ "type": "stream_url", "data": { "rack_id": ..., "url": "/camera/api/webrtc?src=rack-047" } }` and sends it over the **same** `/ws` connection, alongside the lock confirmation.
3. `CameraPanel.tsx` opens the `<video>` element with that URL.
4. When the lock releases, the server sends a stream-close signal over `/ws` and the panel tears down the `<video>` element.

**Server-side stream list**: go2rtc on the server auto-generates its RTSP pull list from `racks.pi_ip` (and the per-rack RTSP credential) — adding or replacing a Pi requires no manual go2rtc config edit, because provisioning (4.6) already updates that table.

**Security boundary**: MQTT carries motion/capture/status only. go2rtc/RTSP/WebRTC is a fully separate path that never touches MQTT. A failure in one cannot take down the other — this separation is intentional and must not be blurred.

---

## 9. Security Implementation — All Layers

Local dev: items marked **[PROD ONLY]** are configuration switches that exist but default to off/inert. Turning the system "production-ready" is, by design, a matter of flipping these flags and supplying real secrets/certs — not rewriting code.

### Layer 1 — Browser
- **[PROD ONLY]** HTTPS + HSTS enforced (`TLS_ENABLED`, `HSTS_ENABLED`).
- **[PROD ONLY]** WSS instead of plain WS.
- **[PROD ONLY]** Auth tokens in HttpOnly Secure SameSite=Strict cookies (`COOKIE_SECURE`).
- **[PROD ONLY]** CSP headers (`CSP_ENABLED`).
- Role-based UI — Viewer never sees command buttons or the camera feed (enforced server-side too, 4.2).
- CSRF double-submit cookie on all POST/PUT and on the WebSocket `CAPTURE`/command messages (`CSRF_ENABLED` — can be turned on locally too, just with non-Secure cookies).
- Timeout/retry UI states: `pending` → `retrying` → `re-homing` → `maintenance required`, driven by the WebSocket messages from Sections 4.4/4.5.
- Emergency stop always visible, never disabled.
- Capture never assumed to succeed: spinner from `CAPTURE_STARTED` to `capture_complete`, with a hard 60s client-side timeout that clears the spinner and shows an error if `capture_complete` never arrives.
- `capture_complete` is delivered only to the operator who holds the lock (Section 4.3) — viewers never receive it.

### Layer 2A — FastAPI Server
- OAuth2 + JWT, three roles (Viewer/Operator/Admin), assignments via `user_rack_assignments`.
- Rate limiting per user on all command endpoints; `/presign` additionally limited to 2/min per Pi credential, returning 429 with `Retry-After`.
- Command whitelist: `G28, M700, M701-M704, M710, M711, M114, !, CAPTURE, SCAN_START, SCAN_STOP` — anything else is rejected with 400 before MQTT is touched. `M700`/`M701-704` parameters validated against the rack's `grid_rows`/`grid_cols`.
- `/ws` validates `ADMIN_TOKEN`/JWT on connect; `/rack/{id}/presign` validates `PI_API_KEY` only — MQTT credentials are explicitly rejected on this endpoint.
- `s3_key` (or `local_path`) validated against the pattern `images/rack-{id}/{iso-timestamp}.jpg`, and the `{id}` segment cross-checked against the MQTT topic the notification arrived on — a Pi cannot claim a key belonging to a different rack.
- Operator attribution via `capture_attribution` (4.3 / 3.12), with expiry handling and audit flagging.
- Rack Locking + Command Queue (4.3), Position Verification + Recovery (4.4), and Manual-vs-Scan conflict handling (4.8) — all previously-missing pieces from the original plan.
- Audit log covers: `capture_triggered`, `presign_issued`, `upload_confirmed`, `image_notification_received`, `duplicate_image_notification`, `validation_failure`, plus the command/position/maintenance events listed in Section 3.5.

### Layer 2B — MQTT Broker (Mosquitto)
- Local dev: `allow_anonymous true`, port 1883, no TLS.
- **[PROD ONLY]** `allow_anonymous false` + password file; TLS on 8883; **port 1883 disabled entirely**.
- **[PROD ONLY]** mosquitto-go-auth backed by the same database as the `racks`/`users` tables; per-Pi ACL restricts each Pi to `vivarium/rack/{id}/*`.
- **[PROD ONLY]** The image topic is included in this ACL from the start: each Pi's entry grants publish on `vivarium/rack/{id}/image` (its own rack only); only the server holds subscribe rights on `vivarium/rack/+/image`. Rolled out via an Ansible playbook that regenerates ACL entries for all Pis and reloads the broker without dropping connections (`ANSIBLE_INVENTORY_PATH`).
- **[PROD ONLY]** Broker bound to internal interface only.
- **[PROD ONLY]** Per-client rate limiting: iptables connlimit (10 connections/IP on 8883) plus mosquitto-go-auth enforcing 60 msg/min per client via the cache backend; clients exceeding the limit are disconnected and logged.
- Last Will on every Pi connection (`{"status":"offline","reason":"unexpected_disconnect"}`, QoS 1, retained) — broker publishes within 30s of an unexpected disconnect, no polling.
- `!` is QoS 2 (exactly-once); all motion commands QoS 1; persistent sessions (`cleansession=false`) queue commands while a Pi is offline.

### Layer 2C — Database
- SQLite locally; **[PROD ONLY]** PostgreSQL with TLS connections and SCRAM-SHA-256 auth.
- **[PROD ONLY]** Least-privilege roles per component; **[PROD ONLY]** `pg_audit`; **[PROD ONLY]** full-disk encryption.
- All access via SQLAlchemy ORM — no string interpolation, on either engine.
- `UNIQUE` constraint on `image_records.s3_key` (Section 3.4) prevents duplicate inserts from duplicate MQTT notifications, on SQLite and PostgreSQL alike.

### Layer 2D — Camera Streaming (go2rtc)
- go2rtc port 1984, localhost only — never exposed directly.
- **[PROD ONLY]** All external access through Nginx + JWT validation.
- Stream URLs are only issued for racks the operator is assigned to (`user_rack_assignments`), and only while that operator holds the rack lock.
- go2rtc runs as a non-root user on both the Pi and the server.
- **[PROD ONLY]** UFW on each Pi: port 8554 open only to the server's IP.
- RTSP digest auth per Pi, credential issued at provisioning time (Section 5.3/5.6) and mirrored in `racks.rtsp_password_ref`.

### Layer 2E — S3 / Capture Security
- No Pi ever holds AWS credentials — the server generates all pre-signed URLs, and only when `S3_ENABLED=true`.
- Pre-signed PUT: single object key, PUT-only, 5-minute expiry, SHA-256 checksum condition, `Content-Type: image/jpeg` locked.
- All Pi → server traffic (including `/presign`) over HTTPS in production; `PI_API_KEY` is separate from the MQTT credential, so compromising one does not expose the other.
- **[PROD ONLY]** Bucket policy: Block Public Access, `aws:SecureTransport` deny-if-false, SSE-KMS with annual key rotation, lifecycle expiry at 90 days, IAM split so the presign role has `PutObject` only and the browser-serve role has `GetObject` only — neither has `DeleteObject`.
- Pre-signed GET for the browser: 15-minute expiry, generated on demand by the image-history endpoint — never a permanent or public URL.
- `/tmp` is tmpfs on the Pi (RAM only); files are 600 permissions and deleted immediately after the MQTT publish; the reconnect-cleanup sequence (5.2) sweeps any leftovers.
- **When `S3_ENABLED=false`**: the same code paths run against `LOCAL_IMAGE_DIR` on the server and `capture_dir` on the Pi — the security properties that matter locally (no public exposure, checksum recorded, unique-key constraint) still hold; only the "pre-signed URL" mechanics are replaced with a local file copy.

### Layer 3 — Raspberry Pi
- `device.conf` at `/etc/gantry/device.conf`, mode 600.
- **[PROD ONLY]** SSH key-only, from management VLAN/VPN only.
- All services (`bridge.py`, go2rtc) run as a non-root `gantry` user.
- **[PROD ONLY]** UFW: outbound MQTT 8883, inbound RTSP 8554 from server IP, inbound SSH from management network only.
- **[PROD ONLY]** Unattended security upgrades.
- **[PROD ONLY]** Ansible for all deployments, including the camera agent and capture-logic updates (same playbook mechanism as the ACL rollout above).
- **[PROD ONLY]** TLS certificate rotation automated via Ansible, 30 days before expiry, tracked in the `certificates` table and surfaced on the admin dashboard; MQTT client reloads via SIGHUP without dropping the connection.
- `COMMAND_ACK:{command}` published before serial forwarding; serial retry once after 1s then `SERIAL_TIMEOUT:{command}`; reconnect cleanup sequence as in Section 5.2.
- `CAPTURE_STARTED` / `CAPTURE_DONE` lock-keepalive responses (Section 4.3/5.4).

### Layer 4 — Arduino
- Command whitelist in firmware; unrecognised serial input discarded.
- Hardware E-stop on an interrupt pin, independent of serial/MQTT/network.
- `CAPTURE` never forwarded to the Arduino.
- Per-axis stall watchdog (500ms) → `STALL_DETECTED`; serial watchdog (60s silence during motion) → `SERIAL_TIMEOUT_ESTOP`.

### Cross-Cutting (Production)
- **[PROD ONLY]** IoT VLAN — all Pis isolated from the rest of the facility network.
- **[PROD ONLY]** WireGuard VPN for all remote management (`WIREGUARD_REQUIRED`) — no internet-facing SSH.
- **[PROD ONLY]** S3 egress restricted via NAT gateway to the bucket's hostname on port 443 only, even from a compromised Pi.

---

## 10. Fallback / Escalation Ladder

| Level | Trigger | Automatic Action | Browser Notification |
|---|---|---|---|
| L1 — Retry | No `COMMAND_ACK` within `COMMAND_TIMEOUT_S` | Re-publish command once | "Retrying command..." |
| L2 — E-stop + Re-home | Motion timeout / `SERIAL_TIMEOUT` / `STALL_DETECTED` / tolerance failure | Publish `!` (QoS 2) then `G28` | "Re-homing rack" (orange) |
| L3 — Suspended | Re-home fails, or error recurs after re-home | `maintenance_required = true`, `scan_schedule.enabled = false` | Red alert to operator + admin |
| L4 — Stall | Arduino watchdog fires | Arduino stops motors, `STALL_DETECTED` → server runs L2 | Stall error displayed |
| L5 — Hardware E-stop | Physical button pressed | All motors stop in microseconds, all `homed_*` → false, session `aborted` | Audible alarm, manual reset required |

---

## 11. MQTT Topic Reference

| Topic | Direction | QoS | Purpose |
|---|---|---|---|
| `vivarium/rack/{id}/command` | Server → Pi | 1 | Motion, `CAPTURE`, `SCAN_START`/`SCAN_STOP`. |
| `vivarium/rack/{id}/response` | Pi → Server | 1 | `COMMAND_ACK`, Arduino responses (`M114`, etc.), `SERIAL_TIMEOUT`, `CAPTURE_STARTED`/`CAPTURE_DONE`, `BRIDGE_RECONNECTED`. |
| `vivarium/rack/{id}/status` | Pi → Server | 0 | Heartbeat every 30s (also Last Will target). |
| `vivarium/rack/{id}/emergency` | Server → Pi | 2 | `!` only. |
| `vivarium/rack/{id}/image` | Pi → Server | 1 | `s3_key`/`local_path` + timestamp after capture. ACL'd per-Pi to its own rack (Section 9, Layer 2B). |
| `vivarium/rack/{id}/scan_progress` | Pi → Server | 0 | Per-cell progress during auto-scan. |
| `vivarium/rack/{id}/scan_status` | Pi → Server | 1 | Scan lifecycle: `paused`/`complete`/`aborted`/error. |
| `vivarium/all/command` | Server → All | 1 | Broadcast (rarely used). |

---

## 12. S3 Upgrade Path (Config-Only)

When AWS credentials become available, the switch is purely configuration — no code changes on either side:

1. On the server, set `S3_ENABLED=true` and fill in `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `S3_BUCKET`. Leave `S3_ENDPOINT_URL` blank for real AWS, or point it at a local MinIO instance for testing.
2. On each Pi, set `batch_upload_enabled=true` in `device.conf` (rolled out via the same Ansible mechanism used for everything else).
3. `services/s3_handler.py` and `services/camera_handler.py` already branch on these flags — when both are true, the pre-signed PUT/GET flow (Section 4 / 5.4) activates automatically; when either is false, the local-disk path continues to be used.
4. Once S3 is enabled, work through the **[PROD ONLY]** items in Layer 2E (Section 9) — bucket policy, IAM split, lifecycle rule, KMS — as a one-time hardening pass.

---

## 13. Stage-by-Stage Rollout Checklist

1. **MQTT broker** — install Mosquitto, local config (`allow_anonymous true`, port 1883). Verify with `mosquitto_pub`/`mosquitto_sub`.
2. **Server skeleton** — `config/settings.py`, `db/database.py` + `db/models.py` (Section 3), SQLite file created on first run, `core/state.py`.
3. **Server core services** — `mqtt_client.py`, `command_handler.py`, `core/locking.py`, `core/queue_manager.py`, `services/cache.py` (SQLite-backed locally).
4. **Auth & middleware** — `core/security.py`, `middleware/auth.py`, `middleware/rate_limit.py`, `middleware/csrf.py` (CSRF can be exercised locally with non-Secure cookies).
5. **API surface** — `api/websocket.py`, `api/routes.py`; verify `/health`, `/ws` connect with `ADMIN_TOKEN`.
6. **Frontend wiring** — connect to `/ws`, replace mock data, add `ConnectionBar`/`EmergencyStop`/`GantryGrid`/`CameraPanel`.
7. **Pi bridge** — `bridge.py`, `serial_handler.py`, `mqtt_client.py`; test with `socat` virtual serial ports before touching hardware.
8. **Provisioning** — `provisioner.py` + `services/provisioning.py`; flash a test SD card, confirm auto-assignment and `device.conf` write.
9. **Hardware integration** — connect real Arduino/gantry, run `G28`, `M700`, emergency stop, confirm `M114` round-trips.
10. **Capture flow** — `camera_handler.py` with `S3_ENABLED=false` first (local disk), confirm `image_records` rows and `capture_complete` delivery to the lock holder only.
11. **go2rtc streaming** — Pi agent (port 8554), server relay (port 1984, localhost), Nginx proxy stub, confirm MJPEG then WebRTC.
12. **Auto-scan engine** — `scan_engine.py` + `scan_executor.py`; run one full 84-cell scan, verify pause/resume and the manual-command conflict flow (4.8).
13. **Position monitor** — `position_monitor.py`; deliberately induce a tolerance failure and confirm the L2 re-home + retry sequence.
14. **End-to-end validation** — repeat the original Stage 7 checklist (move, capture, e-stop, Pi offline/reconnect, camera stream, auto-scan, provisioning of a new device).
15. **Production hardening pass** — flip the **[PROD ONLY]** flags in Section 9 one layer at a time (TLS/Mosquitto auth → JWT cookies/CSP → PostgreSQL → S3 bucket policy → Pi UFW/SSH/Ansible/VLAN/VPN), validating after each layer before moving to the next.
