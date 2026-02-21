# Media / Attachment Handling

## Live Group Messages (signal-bot)

When a message with an image arrives in a monitored group, the live ingestion
pipeline (`signal-bot/app/ingestion.py`) does the following:

1. Receives `image_paths` — absolute paths to attachment files on disk
   (downloaded by signal-cli into `/var/lib/signal/bot/…`)
2. For each image, calls `llm.image_to_text_json()` which sends the image to
   Gemini and returns `observations` + `extracted_text`
3. Appends the JSON result to `content_text` as `[image]\n{…}`
4. Stores the original file path in `raw_messages.image_paths`
5. Images are served at `/static/<relative-path>` for case evidence views

Non-image attachments (PDFs, files) attached via signal-cli are not currently
OCR'd — only images are processed.

## History Ingestion (signal-ingest → signal-desktop)

Historical messages are read from Signal Desktop's SQLCipher database.

### Before (gap that was fixed)

- `db_reader.py` only queried the `body` (text) column
- Messages with image-only content (no text) were silently dropped
  (`WHERE body IS NOT NULL AND body != ''`)
- Attachment metadata was never read

### After (current behaviour)

**`signal-desktop/app/db_reader.py`**

- Reads the `json` column from Signal Desktop's `messages` table
- Parses `attachments[]` array: extracts `path`, `fileName`, `contentType`
- Includes messages that have attachments even with no text body
- `SignalMessage.attachments` contains a list of `{path, fileName, contentType}` dicts
- Attachment paths are **relative to `<data_dir>/attachments.noindex/`**

**`signal-desktop/app/main.py`**

- `/group/messages` response now includes `attachments` per message
- New endpoint `GET /attachment?path=<rel_path>` serves raw attachment bytes
  - Only serves paths inside `<data_dir>/attachments.noindex/` (path traversal blocked)
  - Returns correct `Content-Type` based on file extension

**`signal-ingest/ingest/main.py`**

After fetching messages, before chunking:

1. Identifies messages that have attachments (`m.get("attachments")`)
2. For each attachment:
   - **Image** (`image/*` content type or image extension): fetches bytes via
     `GET /attachment`, sends to Gemini for OCR, appends result as
     `[image: filename]\n<description>` to message body
   - **Other file**: appends `[attachment: filename.ext]` to message body
3. This enriched body is then included in LLM chunks for case extraction

### Configuration

`MODEL_IMG` env var controls the Gemini model used for image OCR in
signal-ingest (default: `gemini-2.0-flash`). Set it in `.env` / docker-compose.

## What Is Not Handled

- **PDF attachments** in history: file name is noted but content is not extracted
- **Video attachments**: file name is noted; no transcription
- **URLs in message text**: not fetched or expanded — the LLM only sees the raw
  URL string
- **Live messages, non-image files**: not OCR'd (signal-cli doesn't extract
  non-image binary content)
