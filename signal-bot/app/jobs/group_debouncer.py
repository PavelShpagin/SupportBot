"""Per-group debounce timer for batch response processing.

When a message arrives:
1. Cancel any in-progress batch processing for that group
2. Reset the group's silence timer to DEBOUNCE_SECONDS

When the timer fires (no new messages for DEBOUNCE_SECONDS):
3. Collect all unprocessed messages
4. Run batch gate → extract questions
5. For each question, run synthesizer (checking cancel between each)
6. If interrupted at any point → drop all results, timer resets
7. If all succeed → send responses
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 60  # 1 minute of silence before processing


@dataclass
class _GroupState:
    """Per-group state for debounce tracking."""
    group_id: str
    timer: threading.Timer | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    processing: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Track unprocessed message count (since last batch)
    unprocessed_count: int = 0
    last_message_ts: float = 0.0


class GroupDebouncer:
    """Manages per-group debounce timers and batch processing.

    Usage:
        debouncer = GroupDebouncer(deps=worker_deps)
        # Called from message ingestion:
        debouncer.on_message(group_id)
    """

    def __init__(self, *, deps: Any) -> None:
        self._deps = deps
        self._groups: Dict[str, _GroupState] = {}
        self._global_lock = threading.Lock()

    def _get_state(self, group_id: str) -> _GroupState:
        with self._global_lock:
            if group_id not in self._groups:
                self._groups[group_id] = _GroupState(group_id=group_id)
            return self._groups[group_id]

    def on_message(self, group_id: str) -> None:
        """Called when a new message arrives in a group.

        Cancels any in-progress processing and resets the debounce timer.
        """
        state = self._get_state(group_id)

        with state.lock:
            state.unprocessed_count += 1
            state.last_message_ts = time.time()

            # Cancel in-progress batch processing
            if state.processing:
                state.cancel_event.set()
                log.info("Debouncer: cancelled in-progress batch for group %s", group_id[:20])

            # Cancel existing timer
            if state.timer is not None:
                state.timer.cancel()
                state.timer = None

            # Start new timer
            state.timer = threading.Timer(
                DEBOUNCE_SECONDS,
                self._on_timer_fire,
                args=[group_id],
            )
            state.timer.daemon = True
            state.timer.start()
            log.debug("Debouncer: reset timer for group %s (%ds)", group_id[:20], DEBOUNCE_SECONDS)

    def _on_timer_fire(self, group_id: str) -> None:
        """Called when the debounce timer fires (silence period elapsed)."""
        state = self._get_state(group_id)

        with state.lock:
            if state.unprocessed_count == 0:
                log.debug("Debouncer: timer fired but no unprocessed messages for %s", group_id[:20])
                return
            if state.processing:
                log.info("Debouncer: timer fired but already processing %s, skipping", group_id[:20])
                return
            state.processing = True
            state.cancel_event.clear()

        # Run batch processing in a separate thread
        t = threading.Thread(
            target=self._process_group,
            args=[group_id],
            daemon=True,
        )
        t.start()

    def _process_group(self, group_id: str) -> None:
        """Run batch processing for a group. Handles cancellation and sending."""
        state = self._get_state(group_id)

        try:
            self._do_process(group_id, state)
        except Exception:
            log.exception("Debouncer: batch processing failed for group %s", group_id[:20])
        finally:
            with state.lock:
                state.processing = False

    def _do_process(self, group_id: str, state: _GroupState) -> None:
        from app.jobs.batch_responder import process_batch
        from app.db.queries_mysql import get_group_admins, get_admin_session

        deps = self._deps

        # Respect WORKER_ENABLED — don't process/send when disabled
        if not deps.settings.worker_enabled:
            log.info("Debouncer: WORKER_ENABLED=0, skipping batch for group %s", group_id[:20])
            with state.lock:
                state.unprocessed_count = 0
            return

        # Check if group has active admins
        admins = get_group_admins(deps.db, group_id)
        active_admin_sessions = [(aid, get_admin_session(deps.db, aid)) for aid in admins]
        active_admins = [aid for aid, sess in active_admin_sessions if sess is not None]

        if not active_admins:
            log.info("Debouncer: group %s has no active admins, skipping", group_id[:20])
            with state.lock:
                state.unprocessed_count = 0
            return

        if deps.settings.admin_whitelist and not any(a in deps.settings.admin_whitelist for a in active_admins):
            log.info("Debouncer: group %s has no whitelisted admins, skipping", group_id[:20])
            with state.lock:
                state.unprocessed_count = 0
            return

        # Get language
        group_lang = "uk"
        for aid, sess in active_admin_sessions:
            if sess is not None:
                group_lang = sess.lang or "uk"
                break

        # Snapshot unprocessed count before processing
        with state.lock:
            batch_size = state.unprocessed_count

        def cancel_check() -> bool:
            return state.cancel_event.is_set()

        log.info("Debouncer: processing group %s, ~%d unprocessed messages", group_id[:20], batch_size)

        result = process_batch(
            group_id=group_id,
            db=deps.db,
            llm=deps.llm,
            ultimate_agent=deps.ultimate_agent,
            settings=deps.settings,
            bot_sender_hash=deps.bot_sender_hash,
            last_n=batch_size,
            lang=group_lang,
            cancel_check=cancel_check,
        )

        # Check cancellation BEFORE sending
        if state.cancel_event.is_set():
            log.info("Debouncer: batch cancelled before sending for group %s (new message arrived)", group_id[:20])
            return

        # Send responses
        for resp in result.responses:
            # Final cancellation check before each send
            if state.cancel_event.is_set():
                log.info("Debouncer: cancelled mid-send for group %s", group_id[:20])
                return

            answer_text = resp.response_text
            if not answer_text or answer_text == "SKIP":
                continue

            # Handle admin escalation
            mention_recipients = []
            from app.db.queries_mysql import get_tag_targets
            tag_targets = get_tag_targets(deps.db, group_id)

            if answer_text.strip() == "[[TAG_ADMIN]]":
                tag_msg = "Потребує уваги адміністратора." if group_lang == "uk" else "Needs admin attention."
                answer_text = f"[[MENTION_PLACEHOLDER]] {tag_msg}"
                mention_recipients = list(tag_targets or active_admins)
            elif "[[TAG_ADMIN]]" in answer_text or "@admin" in answer_text:
                answer_text = answer_text.replace("[[TAG_ADMIN]]", "[[MENTION_PLACEHOLDER]]").replace("@admin", "[[MENTION_PLACEHOLDER]]")
                mention_recipients = list(tag_targets or active_admins)

            # Get quote target info for reply
            quote_ts = None
            quote_text = ""
            quote_author = ""
            if resp.reply_to_message_id:
                from app.db import get_raw_message
                reply_msg = get_raw_message(deps.db, message_id=resp.reply_to_message_id)
                if reply_msg:
                    quote_ts = reply_msg.ts
                    quote_text = reply_msg.content_text or ""
                    quote_author = reply_msg.sender_uuid or ""

            # Clean for storage
            stored_text = answer_text.replace("[[MENTION_PLACEHOLDER]]", "@admin")

            log.info("Debouncer SEND: group=%s reply_to=%s len=%d",
                     group_id[:20], resp.reply_to_message_id, len(stored_text))

            try:
                sent_ts = deps.signal.send_group_text(
                    group_id=group_id,
                    text=answer_text,
                    quote_timestamp=quote_ts,
                    quote_author=quote_author or None,
                    quote_message=(quote_text[:200] if quote_text else None),
                    mention_recipients=mention_recipients or None,
                )

                # Store bot response in raw_messages for future context
                if sent_ts and deps.bot_sender_hash:
                    from app.db import RawMessage, insert_raw_message
                    bot_msg = RawMessage(
                        message_id=str(sent_ts),
                        group_id=group_id,
                        ts=sent_ts,
                        sender_hash=deps.bot_sender_hash,
                        content_text=stored_text,
                        image_paths=[],
                        reply_to_id=resp.reply_to_message_id,
                        sender_name="BOT",
                    )
                    insert_raw_message(deps.db, bot_msg)
            except RuntimeError:
                # Retry without quote — quote_author may be missing or unregistered
                log.warning("Debouncer: send with quote failed, retrying without quote for group %s", group_id[:20])
                try:
                    sent_ts = deps.signal.send_group_text(
                        group_id=group_id,
                        text=answer_text,
                        mention_recipients=mention_recipients or None,
                    )
                except Exception:
                    log.exception("Debouncer: failed to send response for group %s", group_id[:20])
            except Exception:
                log.exception("Debouncer: failed to send response for group %s", group_id[:20])

        # Mark all as processed (reset counter)
        with state.lock:
            # Only reset the count we processed — new messages may have arrived
            state.unprocessed_count = max(0, state.unprocessed_count - batch_size)

        log.info("Debouncer: done for group %s, sent %d responses", group_id[:20], len(result.responses))
