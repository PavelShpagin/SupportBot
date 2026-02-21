# Admin Session Management

## Key Concept: Sessions Are Independent of Signal Chat History

The bot stores admin sessions in **MySQL** (`admin_sessions` table).  
This is **completely independent** from Signal message history on the user's phone.

**Clearing Signal chat history does NOT reset the bot session.**  
If an admin deletes their conversation with the bot in the Signal app, the bot
still has their session in the database — the bot gets no notification about
client-side history deletion.

## When Sessions Are Created / Cleared

| Event | Effect |
|---|---|
| Admin sends first message (no session exists) | Session created; welcome message + language detection sent |
| Admin re-opens chat after clearing history | Session still exists → no re-welcome, continue from last state |
| Admin removes bot from contacts / blocks bot | Session fully deleted (`_handle_contact_removed`) |
| Admin sends `/wipe` | All data including session deleted |
| Bot removed from a group | Group data deleted, admin session preserved |

## Session State Machine

```
[None / no session]
        │
        │ first message received
        ▼
awaiting_group_name  ◄─────────────────────────────────────┐
        │                                                    │
        │ admin types a group name                          │
        ▼                                                    │
awaiting_qr_scan                                            │
        │                                                    │
        │ QR scanned & history processed (success or fail)  │
        └────────────────────────────────────────────────────┘
```

### State descriptions

- **`None` (no row)** — User has never messaged the bot, or was fully wiped.  
  Next message triggers welcome + language detection.

- **`awaiting_group_name`** — Bot sent the onboarding prompt; waiting for the
  admin to type a group name.

- **`awaiting_qr_scan`** — A HISTORY_LINK job is running; waiting for the admin
  to scan the QR code in Signal Desktop.  
  If the admin sends a new group name now, the pending job is cancelled and the
  new group search starts immediately.

## Language Detection

Language is detected **once**, on the very first message, from the message text:

- Ukrainian-specific characters (`і ї є ґ`) → `uk`
- Any other Cyrillic characters → `uk`  
- Latin-only text → `en`
- Default → `uk`

Language can be overridden at any time with `/uk` or `/en`.

## Session Fields (MySQL `admin_sessions`)

| Column | Description |
|---|---|
| `admin_id` | Signal phone number / UUID of the admin |
| `state` | Current state (see above) |
| `pending_group_id` | Signal group ID being linked |
| `pending_group_name` | Human-readable group name |
| `pending_token` | One-time token for the active HISTORY_LINK job |
| `lang` | `uk` or `en` |
| `updated_at` | Timestamp of last update (informational) |
