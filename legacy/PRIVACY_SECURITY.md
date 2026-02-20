# SupportBot — Privacy & Security Behaviour

**Last Updated**: 2026-02-18

---

## Data Lifecycle

### When admin removes bot from a group (kicked/left)
- All cases for that group → deleted from DB and RAG index
- All raw messages for that group → deleted
- All chat buffers, reactions, jobs for that group → deleted
- Admin–group links → removed
- Triggered automatically by the reconcile loop (runs every 15 s)

### When admin removes bot from contacts (chat deleted)
- Admin session (state, language, pending token) → deleted
- All pending history jobs for that admin → cancelled
- All history tokens for that admin → deleted
- All admin–group links → removed
- Triggered immediately by the `on_contact_removed` signal event

### Architecture: Two Separate Signal Identities

There are two completely independent Signal accounts involved:

| Identity | Account | Used for |
|----------|---------|---------|
| **Bot** | signal-cli, registered phone number | Receives/sends group messages at runtime |
| **Admin (temporary)** | Signal Desktop, admin's personal account | Reads historical messages for import |

These are checked independently and **both must be in the group** for ingest to proceed.

### History Ingestion (QR flow)
1. Admin sends group name to bot in a 1:1 chat
2. **Check 1 (bot side)**: Bot calls signal-cli `list-groups` — group must be in bot's list. Rejected if not found.
3. Signal Desktop resets to show a fresh QR code (previous session wiped)
4. Admin scans QR → their personal Signal account is temporarily linked
5. **Check 2 (admin side)**: Ingest calls Signal Desktop `/conversations` — verifies admin's account is in the group by ID or name. Rejected if not found.
6. Bot reads the last ~800 messages from the group via admin's account
7. LLM extracts solved support cases
8. **Check 3 (bot side again)**: `/history/cases` endpoint re-verifies bot is still in group via signal-cli. No bypass — if signal-cli fails to respond, request is blocked with 503.
9. Cases and embeddings saved to DB + RAG
10. **Signal Desktop session reset immediately** — admin's account unlinked, no data retained
11. Every QR scan requires a new code (sessions never reused)

### Security guarantees
- **3 independent group membership checks**: (1) at job creation, (2) after QR scan via admin's account (polls up to 60s for post-link sync), (3) at data submission via bot's account
- No bypass: if any check cannot be completed, the flow is **blocked**
- Signal Desktop is reset after **every** ingest — successful or cancelled
- History tokens are single-use and expire after a configurable TTL

---

## Admin Commands (1:1 chat with bot)

| Command | Effect |
|---------|--------|
| `/en` | Switch bot language to English |
| `/uk` | Switch bot language to Ukrainian |
| `/wipe` | **Erase ALL bot data** (cases, messages, sessions, groups, RAG index). Keeps signal-cli phone registration. |

---

## Fresh Start (`/wipe`)

Send `/wipe` to the bot in a private chat. This deletes:
- All cases and evidence from DB and RAG
- All raw messages
- All buffers and reactions
- All admin sessions and group links
- All history tokens and pending jobs

Does **not** delete:
- signal-cli phone registration (`/var/lib/signal/bot`)
- Docker volumes, server config, `.env` file

After wiping, re-add the bot to groups and run history ingestion again.

---

## Data Storage

| Data | Location | Retention |
|------|----------|-----------|
| Signal messages (buffer) | MySQL `raw_messages`, `buffers` | Until group removed or `/wipe` |
| Support cases | MySQL `cases`, `case_evidence` | Until group removed or `/wipe` |
| RAG embeddings | ChromaDB `/var/lib/chroma` | Until group removed or `/wipe` |
| Admin sessions | MySQL `admin_sessions` | Until contact removed or `/wipe` |
| Bot account | signal-cli `/var/lib/signal/bot` | Permanent (phone registration) |
| Ingest user session | Signal Desktop `/var/lib/signal/desktop` | Reset after every QR scan |
