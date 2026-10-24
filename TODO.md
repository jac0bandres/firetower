# Firetower — TODO

Vertical split across 3 tracks so no one blocks on another's work — the API/
schema contracts are already defined in [CLAUDE.md](CLAUDE.md), and the board
can be built right now against `fixtures/mock_tickets.json` (`FIRETOWER_MOCK=1`)
without waiting on the radio pipeline or the model.

## Radio pipeline (`gateway.py`, `db.py`)

- [ ] Implement `db.py` CRUD: `init_db`, `is_duplicate_packet`, `mark_packet_seen`,
      `next_ticket_code`, `insert_ticket`, `list_tickets`, `set_ticket_status`,
      `list_priority_nodes`, `set_priority_node` — against the schema in CLAUDE.md.
- [ ] Implement `gateway.py` `on_receive`: parse the Meshtastic packet dict
      (`decoded.text`, `id`, `from`, `hopStart`/`hopLimit`), dedupe via
      `db.is_duplicate_packet`, ignore our own node + `FT `-prefixed messages,
      call `triage.triage()`, insert the ticket.
- [ ] Implement `gateway.py` `send_ack`: DM the ticket code back to the sender,
      `FT ` prefix, chunked to ≤200 bytes UTF-8, max 3 chunks ~3s apart, never
      broadcast.

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

- [ ] Swap `board.py` off `FIRETOWER_MOCK` once `db.py` is live: verify the
      real `db.*` calls return the exact same shape as
      `fixtures/mock_tickets.json`, then demo with `FIRETOWER_MOCK=0`.
- [ ] Build the 911 call card UI: red banner for open `EMERGENCY` tickets, a
      "Prepare 911 call" card with the ready-to-read script, an "I called 911"
      button that `POST`s `/api/tickets/{code}/status=assigned`. Never auto-dials.
- [ ] Add priority-node toggle UI: let the coordinator mark/unmark a node via
      `GET`/`POST /api/priority_nodes`, show a "priority node: &lt;role&gt;" tag
      on matching tickets in the table.

## Stretch (after the above, time permitting)

- [ ] Hop count + traceroute button (`CLAUDE.md` §7 in handoff)
- [ ] Snowflake sync (`sync.py`) + Cortex queries
- [ ] TAK markers over UDP multicast
- [ ] Voice read-aloud (ElevenLabs online / Piper offline) for the 911 card
