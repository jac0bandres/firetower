# Firetower — TODO

Vertical split across 3 tracks so no one blocks on another's work — the API/
schema contracts are already defined in [CLAUDE.md](CLAUDE.md), and the board
can be built right now against `fixtures/mock_tickets.json` (`FIRETOWER_MOCK=1`)
without waiting on the radio pipeline or the model.

## Radio pipeline (`gateway.py`, `db.py`)

- [x] Implement `db.py` CRUD: `init_db`, `is_duplicate_packet`, `mark_packet_seen`,
      `next_ticket_code`, `insert_ticket`, `list_tickets`, `set_ticket_status`,
      `list_priority_nodes`, `set_priority_node` — against the schema in CLAUDE.md.
- [x] Implement `gateway.py` `on_receive`: parse Meshtastic text packets
      (`decoded.text`, `id`, `fromId`, `hopStart`/`hopLimit`), dedupe per sender
      and packet id, ignore our own node and `FT `-prefixed messages, triage and
      insert tickets. Handle separate position packets and preserve both packet
      snapshots.
- [x] Implement `gateway.py` `send_ack`: DM the ticket code back to the sender,
      with `FT ` prefix, within the ≤200-byte UTF-8 limit, never broadcast.
- [x] Store Meshtastic text and position packet snapshots; populate North
      Georgia demo GPS positions for every mock ticket.

## Intelligence (`triage.py`, `router.py`)

- [x] Implement `triage.py` `keyword_floor` + `apply_priority_floor`:
      `keyword_floor(text)` scans `EMERGENCY_KEYWORDS`/`PRIORITY_KEYWORDS` and
      returns a floor urgency or `None`. `apply_priority_floor(result, node_id)`
      checks `db.list_priority_nodes()` and never lets a priority node's ticket
      rank below `PRIORITY`.
- [x] Implement `triage.py` `extract_with_model` + `triage()`: call Ollama with
      `EXTRACTION_SCHEMA` (`format` param), `temperature=0`, `think=False`.
      Fall back to `TRIAGE_MODEL_BACKUP`, then keyword-only, on failure.
      `triage()` merges model output + `keyword_floor` + `apply_priority_floor`
      into the final `TriageResult`.
- [x] Implement `router.py` `is_online` + `sitrep_model`: `is_online()` probes
      `HEALTHCHECK_HOST` with `HEALTHCHECK_TIMEOUT_SECONDS`, cached for
      `HEALTHCHECK_INTERVAL_SECONDS`. `sitrep_model()` returns the
      DigitalOcean/home-GPU tier when online, `TRIAGE_MODEL` when offline.
      Covered by `tests/test_triage.py` + `tests/test_router.py` (23 tests,
      all passing — Ollama/requests/db mocked, no live server needed).

## Board (`board.py`, `static/`)

- [x] Add a map that pins tickets with `latitude`/`longitude`, shows the
      position timestamp, and leaves unlocated tickets in the table. OpenFreeMap
      supplies the vector map and Mapterhorn supplies terrain and contours. Mock
      tickets have synthetic North Georgia positions; map tiles need an online
      tile source.
- [x] Swap `board.py` off `FIRETOWER_MOCK` once `db.py` is live: verify the
      real `db.*` calls return the exact same shape as
      `fixtures/mock_tickets.json`, then demo with `FIRETOWER_MOCK=0`.
- [x] Build the 911 call card UI: red banner for open `EMERGENCY` tickets, a
      "Prepare 911 call" card with the ready-to-read script, an "I called 911"
      button that `POST`s `/api/tickets/{code}/status=assigned`. Never auto-dials.
- [x] Add priority-node toggle UI: let the coordinator mark/unmark a node via
      `GET`/`POST`/`DELETE /api/priority_nodes`, show a "priority node: &lt;role&gt;" tag
      on matching tickets in the table.
- [x] Improve the 911 call card: read-aloud script now includes the sender
      radio's GPS coordinates (with a caveat it may not be the exact incident
      site), and cards sort oldest-open-first instead of newest-first, so the
      longest-waiting emergency is the one a coordinator sees first. Verified
      live in the browser with two open EMERGENCY tickets.

## Intelligence — `!ai` assistant

- [x] `!ai <question>` on the mesh replies by DM (never broadcast, never
      becomes a ticket), in a persistent per-sender thread (`ai_threads`/
      `ai_messages`), with the model's context built from every ticket
      triaged on the channel so far. Fixed a real bug found via live testing:
      `qwen3:4b` leaks its reasoning trace into unconstrained chat replies
      despite `think=False`; now stripped. Covered by
      `tests/test_ai_assistant.py` + `tests/test_gateway_ai.py` (14 tests).

- [x] Clarification follow-up: a new EMERGENCY/PRIORITY ticket missing
      location or injured_count gets one templated follow-up DM
      (`pending_clarifications` table, expires after
      `FIRETOWER_CLARIFICATION_TTL_SECONDS`). The sender's next plain-text
      reply (not `!ai`) is extracted and merged into the *same* ticket, never
      a new one. Verified live against the real model and a real SQLite DB,
      not just mocked. `tests/test_gateway_clarification.py` (6 tests).

## Stretch (after the above, time permitting)

- [ ] Hop count + traceroute button (`CLAUDE.md` §7 in handoff)
- [ ] Snowflake sync (`sync.py`) + Cortex queries
- [ ] TAK markers over UDP multicast
- [ ] Voice read-aloud (ElevenLabs online / Piper offline) for the 911 card
