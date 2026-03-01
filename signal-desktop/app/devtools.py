"""
Chrome DevTools Protocol client for Signal Desktop automation.

This module provides a Python interface to control Signal Desktop via the
Chrome DevTools Protocol, enabling:
- Sending messages to groups and individuals
- Receiving new messages in real-time
- Listing conversations and groups

Based on the approach from https://github.com/mandatoryprogrammer/signal-bot
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from contextlib import asynccontextmanager

import httpx
import websockets
from websockets.client import WebSocketClientProtocol

log = logging.getLogger(__name__)


@dataclass
class SignalConversation:
    """A Signal conversation (group or 1:1)."""
    id: str
    type: str  # "group" or "private"
    name: str
    group_id: Optional[str] = None
    e164: Optional[str] = None  # Phone number for private conversations
    uuid: Optional[str] = None


@dataclass
class SignalMessage:
    """An incoming Signal message."""
    id: str
    conversation_id: str
    timestamp: int
    sender: str  # Phone number or UUID
    body: str
    type: str  # "incoming" or "outgoing"
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    expire_timer: int = 0


class DevToolsClient:
    """
    Chrome DevTools Protocol client for Signal Desktop.
    
    Connects to Signal Desktop running with --remote-debugging-port=9222
    and provides methods to send/receive messages.
    """
    
    def __init__(self, debug_port: int = 9222, host: str = "localhost"):
        self.debug_port = debug_port
        self.host = host
        self._ws: Optional[WebSocketClientProtocol] = None
        self._message_id = 0
        self._pending_responses: Dict[int, asyncio.Future] = {}
        self._message_handlers: List[Callable[[SignalMessage], None]] = []
        self._receive_task: Optional[asyncio.Task] = None
        self._connected = False
    
    async def connect(self) -> bool:
        """
        Connect to Signal Desktop via Chrome DevTools Protocol.
        
        Returns True if connected successfully.
        """
        try:
            # Get the WebSocket debugger URL from the DevTools HTTP endpoint
            ws_url = await self._get_websocket_url()
            if not ws_url:
                log.error("Could not get WebSocket URL from DevTools")
                return False
            
            log.info("Connecting to Signal Desktop DevTools at %s", ws_url)
            self._ws = await websockets.connect(ws_url, max_size=50 * 1024 * 1024)
            self._connected = True
            
            # Start receiving messages in background
            self._receive_task = asyncio.create_task(self._receive_loop())
            
            # Enable Runtime domain for JavaScript execution
            await self._send_command("Runtime.enable")
            
            log.info("Connected to Signal Desktop DevTools")
            return True
            
        except Exception as e:
            log.exception("Failed to connect to Signal Desktop DevTools")
            return False
    
    async def disconnect(self):
        """Disconnect from Signal Desktop DevTools."""
        self._connected = False
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
    
    async def _get_websocket_url(self) -> Optional[str]:
        """Get the WebSocket debugger URL from the DevTools HTTP endpoint."""
        try:
            url = f"http://{self.host}:{self.debug_port}/json"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                pages = resp.json()
                
                # Find the main Signal Desktop page
                for page in pages:
                    # Look for the main renderer page
                    page_url = page.get("url", "")
                    if "signal" in page_url.lower() or page.get("type") == "page":
                        ws_url = page.get("webSocketDebuggerUrl")
                        if ws_url:
                            log.info("Found Signal Desktop page: %s", page.get("title", "untitled"))
                            return ws_url
                
                # Fallback: use the first available page
                if pages and pages[0].get("webSocketDebuggerUrl"):
                    return pages[0]["webSocketDebuggerUrl"]
                    
                return None
        except Exception as e:
            log.error("Failed to get WebSocket URL: %s", e)
            return None
    
    async def _send_command(self, method: str, params: Optional[Dict] = None) -> Any:
        """Send a DevTools protocol command and wait for response."""
        if not self._ws:
            raise RuntimeError("Not connected to DevTools")
        
        self._message_id += 1
        msg_id = self._message_id
        
        message = {
            "id": msg_id,
            "method": method,
        }
        if params:
            message["params"] = params
        
        # Create a future for the response
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_responses[msg_id] = future
        
        try:
            await self._ws.send(json.dumps(message))
            # Wait for response with timeout
            result = await asyncio.wait_for(future, timeout=30)
            return result
        finally:
            self._pending_responses.pop(msg_id, None)
    
    async def _receive_loop(self):
        """Background task to receive DevTools messages."""
        try:
            async for msg in self._ws:
                try:
                    data = json.loads(msg)
                    
                    # Handle command responses
                    if "id" in data:
                        msg_id = data["id"]
                        if msg_id in self._pending_responses:
                            future = self._pending_responses[msg_id]
                            if "error" in data:
                                future.set_exception(RuntimeError(data["error"].get("message", "Unknown error")))
                            else:
                                future.set_result(data.get("result"))
                    
                    # Handle events (like console messages from injected JS)
                    elif "method" in data:
                        await self._handle_event(data)
                        
                except json.JSONDecodeError:
                    log.warning("Invalid JSON from DevTools: %s", msg[:200])
                except Exception as e:
                    log.exception("Error processing DevTools message")
        except websockets.ConnectionClosed:
            log.info("DevTools connection closed")
            self._connected = False
        except asyncio.CancelledError:
            pass
    
    async def _handle_event(self, event: Dict):
        """Handle DevTools events."""
        method = event.get("method")
        params = event.get("params", {})
        
        # Handle console API calls (our injected code uses console.log for callbacks)
        if method == "Runtime.consoleAPICalled":
            args = params.get("args", [])
            if args and args[0].get("value") == "__signal_message__":
                # This is a message callback from our injected code
                if len(args) > 1:
                    try:
                        msg_data = json.loads(args[1].get("value", "{}"))
                        await self._on_message_received(msg_data)
                    except Exception as e:
                        log.warning("Failed to parse message callback: %s", e)
    
    async def _on_message_received(self, msg_data: Dict):
        """Called when a new message is received via DevTools hook."""
        try:
            message = SignalMessage(
                id=str(msg_data.get("id", "")),
                conversation_id=str(msg_data.get("conversationId", "")),
                timestamp=int(msg_data.get("timestamp", 0)),
                sender=str(msg_data.get("source", "")),
                body=str(msg_data.get("body", "")),
                type=str(msg_data.get("type", "incoming")),
                group_id=msg_data.get("groupId"),
                group_name=msg_data.get("groupName"),
                expire_timer=int(msg_data.get("expireTimer", 0)),
            )
            
            for handler in self._message_handlers:
                try:
                    handler(message)
                except Exception:
                    log.exception("Message handler failed")
                    
        except Exception as e:
            log.exception("Failed to process received message")
    
    def on_message(self, handler: Callable[[SignalMessage], None]):
        """Register a handler for incoming messages."""
        self._message_handlers.append(handler)
    
    async def evaluate_js(self, expression: str) -> Any:
        """Execute JavaScript in the Signal Desktop context."""
        result = await self._send_command("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        })
        
        if result.get("exceptionDetails"):
            exc = result["exceptionDetails"]
            raise RuntimeError(f"JS error: {exc.get('text', 'Unknown error')}")
        
        return result.get("result", {}).get("value")
    
    async def send_message(
        self,
        recipient: str,
        text: str,
        expire_timer: int = 0,
    ) -> bool:
        """
        Send a message to a Signal user (by phone number in E.164 format).
        
        Args:
            recipient: Phone number in E.164 format (e.g., "+12345678910")
            text: Message text to send
            expire_timer: Disappearing message timer in seconds (0 = no expiration)
        
        Returns:
            True if message was sent successfully.
        """
        # JavaScript to find conversation and send message
        js_code = f"""
        (async function() {{
            try {{
                // Find the conversation by phone number
                const conversations = window.ConversationController.getAll();
                let conversation = null;
                
                for (const conv of conversations) {{
                    const e164 = conv.get('e164');
                    const uuid = conv.get('uuid');
                    if (e164 === '{recipient}' || uuid === '{recipient}') {{
                        conversation = conv;
                        break;
                    }}
                }}
                
                if (!conversation) {{
                    // Try to create a new conversation
                    conversation = await window.ConversationController.getOrCreateAndWait('{recipient}', 'private');
                }}
                
                if (!conversation) {{
                    return {{ success: false, error: 'Conversation not found' }};
                }}
                
                // Set disappearing messages if specified
                if ({expire_timer} > 0) {{
                    await conversation.updateExpirationTimer({expire_timer});
                }}
                
                // Send the message
                await conversation.sendMessage({{
                    body: {json.dumps(text)},
                }});
                
                return {{ success: true }};
            }} catch (err) {{
                return {{ success: false, error: err.message || String(err) }};
            }}
        }})();
        """
        
        try:
            result = await self.evaluate_js(js_code)
            if isinstance(result, dict):
                if result.get("success"):
                    log.info("Sent message to %s", recipient)
                    return True
                else:
                    log.error("Failed to send message: %s", result.get("error"))
                    return False
            return False
        except Exception as e:
            log.exception("Failed to send message to %s", recipient)
            return False
    
    async def send_group_message(
        self,
        group_id: str,
        text: str,
        expire_timer: int = 0,
    ) -> bool:
        """
        Send a message to a Signal group.
        
        Args:
            group_id: The group ID (base64 encoded)
            text: Message text to send
            expire_timer: Disappearing message timer in seconds (0 = no expiration)
        
        Returns:
            True if message was sent successfully.
        """
        # JavaScript to find group and send message
        js_code = f"""
        (async function() {{
            try {{
                // Find the group conversation
                const conversations = window.ConversationController.getAll();
                let conversation = null;
                
                for (const conv of conversations) {{
                    const convGroupId = conv.get('groupId');
                    const convName = conv.get('name') || '';
                    if (convGroupId === '{group_id}' || convName === '{group_id}') {{
                        conversation = conv;
                        break;
                    }}
                }}
                
                if (!conversation) {{
                    return {{ success: false, error: 'Group not found' }};
                }}
                
                // Set disappearing messages if specified
                if ({expire_timer} > 0) {{
                    await conversation.updateExpirationTimer({expire_timer});
                }}
                
                // Send the message
                await conversation.sendMessage({{
                    body: {json.dumps(text)},
                }});
                
                return {{ success: true }};
            }} catch (err) {{
                return {{ success: false, error: err.message || String(err) }};
            }}
        }})();
        """
        
        try:
            result = await self.evaluate_js(js_code)
            if isinstance(result, dict):
                if result.get("success"):
                    log.info("Sent message to group %s", group_id)
                    return True
                else:
                    log.error("Failed to send group message: %s", result.get("error"))
                    return False
            return False
        except Exception as e:
            log.exception("Failed to send message to group %s", group_id)
            return False
    
    async def send_image(
        self,
        recipient: str,
        image_base64: str,
        caption: str = "",
    ) -> bool:
        """
        Send an image to a Signal user via Chrome DevTools Protocol.

        The image is passed as a base64-encoded string so no shared filesystem
        is required between the caller and this service.

        The JS tries two strategies:
        1. window.Signal.Migrations.processNewAttachment – writes the data to
           Signal's own attachment store and returns a proper attachment object.
        2. Raw ArrayBuffer attachment – pass the data directly to sendMessage.
           Older Signal Desktop builds accept this without extra processing.
        """
        js_code = f"""
        (async function() {{
            try {{
                // ── decode base64 → ArrayBuffer ───────────────────────────
                const b64 = {json.dumps(image_base64)};
                const binStr = atob(b64);
                const bytes = new Uint8Array(binStr.length);
                for (let i = 0; i < binStr.length; i++) {{
                    bytes[i] = binStr.charCodeAt(i);
                }}
                const arrayBuffer = bytes.buffer;

                // ── find / create conversation ────────────────────────────
                const recipient = {json.dumps(recipient)};
                const conversations = window.ConversationController.getAll();
                let conversation = null;
                for (const conv of conversations) {{
                    const e164 = conv.get('e164');
                    const uuid = conv.get('uuid');
                    if (e164 === recipient || uuid === recipient) {{
                        conversation = conv;
                        break;
                    }}
                }}
                if (!conversation) {{
                    conversation = await window.ConversationController.getOrCreateAndWait(recipient, 'private');
                }}
                if (!conversation) {{
                    return {{ success: false, error: 'Conversation not found for ' + recipient }};
                }}

                // ── build attachment ──────────────────────────────────────
                let attachment = {{
                    contentType: 'image/png',
                    data: arrayBuffer,
                    size: bytes.byteLength,
                    fileName: 'qr.png',
                }};

                // Strategy 1: process through Signal's migration pipeline
                // (writes data to attachment store, returns attachment with path)
                try {{
                    if (
                        window.Signal &&
                        window.Signal.Migrations &&
                        typeof window.Signal.Migrations.processNewAttachment === 'function'
                    ) {{
                        attachment = await window.Signal.Migrations.processNewAttachment(attachment);
                    }}
                }} catch (migErr) {{
                    // Strategy 2: fall back to raw ArrayBuffer attachment
                }}

                // ── send ──────────────────────────────────────────────────
                await conversation.sendMessage({{
                    body: {json.dumps(caption)},
                    attachments: [attachment],
                }});

                return {{ success: true }};
            }} catch (err) {{
                return {{ success: false, error: err.message || String(err) }};
            }}
        }})();
        """

        try:
            result = await self.evaluate_js(js_code)
            if isinstance(result, dict):
                if result.get("success"):
                    log.info("Sent image to %s", recipient)
                    return True
                else:
                    log.error("Failed to send image: %s", result.get("error"))
                    return False
            return False
        except Exception:
            log.exception("Failed to send image to %s", recipient)
            return False
    
    async def list_conversations(self) -> List[SignalConversation]:
        """Get all conversations from Signal Desktop."""
        js_code = """
        (function() {
            try {
                const conversations = window.ConversationController.getAll();
                return conversations.map(conv => ({
                    id: conv.id,
                    type: conv.get('type'),
                    name: conv.get('name') || conv.get('profileName') || '',
                    groupId: conv.get('groupId') || null,
                    e164: conv.get('e164') || null,
                    uuid: conv.get('uuid') || null,
                }));
            } catch (err) {
                return { error: err.message };
            }
        })();
        """
        
        try:
            result = await self.evaluate_js(js_code)
            if isinstance(result, dict) and "error" in result:
                log.error("Failed to list conversations: %s", result["error"])
                return []
            
            if isinstance(result, list):
                return [
                    SignalConversation(
                        id=c.get("id", ""),
                        type=c.get("type", "private"),
                        name=c.get("name", ""),
                        group_id=c.get("groupId"),
                        e164=c.get("e164"),
                        uuid=c.get("uuid"),
                    )
                    for c in result
                ]
            return []
        except Exception as e:
            log.exception("Failed to list conversations")
            return []
    
    async def find_group_by_name(self, name: str) -> Optional[SignalConversation]:
        """Find a group by name (case-insensitive partial match)."""
        conversations = await self.list_conversations()
        name_lower = name.lower().strip()
        
        # Exact match first
        for conv in conversations:
            if conv.type == "group" and conv.name.lower() == name_lower:
                return conv
        
        # Partial match
        for conv in conversations:
            if conv.type == "group" and name_lower in conv.name.lower():
                return conv
        
        return None
    
    async def trigger_attachment_downloads(self, group_id: str, group_name: str = "") -> dict:
        """Trigger Signal Desktop's background attachment downloader for a group.

        Signal Desktop syncs message metadata immediately after QR-link but does
        not auto-download historical attachment files in headless mode.  Calling
        this method injects JavaScript into the renderer to:

        1. Locate the conversation for ``group_id``.
        2. Load its messages and iterate over pending attachments (no local path).
        3. Enqueue each via ``window.Signal.Services.AttachmentDownloads.addJob()``.
        4. Fall back to ``showConversation()`` / Whisper events if the service API
           is not available in the running Signal Desktop version.

        Returns a dict: ``{ok, triggered, method, error?}``
        """
        js = """
(async function(groupIdStr, groupNameStr) {
    try {
        // ── find conversation by groupId OR name ─────────────────────────────
        const convs = window.ConversationController.getAll();
        let conv = null;
        const nameLower = (groupNameStr || '').toLowerCase().trim();
        for (const c of convs) {
            const gId = c.get('groupId') || '';
            const cName = (c.get('name') || '').toLowerCase().trim();
            if (gId === groupIdStr) { conv = c; break; }
            if (nameLower && cName === nameLower) { conv = c; break; }
        }
        // Partial name match as last resort
        if (!conv && nameLower) {
            for (const c of convs) {
                const cName = (c.get('name') || '').toLowerCase();
                if (cName.includes(nameLower) || nameLower.includes(cName)) { conv = c; break; }
            }
        }
        if (!conv) return {ok: false, error: 'conversation not found for group ' + groupIdStr + ' / ' + groupNameStr};

        const convId = conv.id;
        let triggered = 0;

        // ── approach 1: AttachmentDownloads.addJob (Signal 6+/7+) ───────────
        // NOTE: We intentionally do NOT require att.size — Signal Desktop stores
        // synced historical attachments with only {contentType} in the SQLite JSON
        // column, but the Backbone model may still have the full CDN pointer
        // internally (cdnKey, key, etc.).  Queuing without size lets Signal
        // Desktop attempt the download and populate the path if the pointer exists.
        const svc = window.Signal
                 && window.Signal.Services
                 && window.Signal.Services.AttachmentDownloads;
        if (svc && typeof svc.addJob === 'function') {
            let msgs = [];
            try {
                const col = await window.Signal.Data.getMessagesByConversation(
                    convId, {limit: 800});
                msgs = (col && col.models) ? col.models : [];
            } catch(e) {}

            for (const msg of msgs) {
                const atts = msg.get('attachments') || [];
                for (let i = 0; i < atts.length; i++) {
                    const att = atts[i];
                    // Queue any attachment that has no local path yet,
                    // regardless of whether size or cdnKey is populated.
                    if (!att.path && (att.contentType || att.cdnKey || att.size)) {
                        try {
                            await svc.addJob(msg, {
                                type: 'attachment',
                                attachment: att,
                                index: i,
                                messageId: msg.id,
                            });
                            triggered++;
                        } catch(e) { /* skip single failure, continue */ }
                    }
                }
                // Also trigger thumbnail downloads for quoted attachments
                const quote = msg.get('quote');
                if (quote && quote.attachments) {
                    for (let i = 0; i < quote.attachments.length; i++) {
                        const qatt = quote.attachments[i];
                        const thumb = qatt && qatt.thumbnail;
                        if (thumb && !thumb.path && (thumb.contentType || thumb.cdnKey)) {
                            try {
                                await svc.addJob(msg, {
                                    type: 'quoteThumbnail',
                                    attachment: thumb,
                                    index: i,
                                    messageId: msg.id,
                                });
                                triggered++;
                            } catch(e) {}
                        }
                    }
                }
            }
            if (triggered > 0) {
                return {ok: true, triggered, method: 'AttachmentDownloads.addJob'};
            }
        }

        // ── approach 2: showConversation Redux action ────────────────────────
        try {
            if (window.reduxActions
                    && window.reduxActions.conversations
                    && typeof window.reduxActions.conversations.showConversation === 'function') {
                window.reduxActions.conversations.showConversation({conversationId: convId});
                return {ok: true, triggered: -1, method: 'showConversation'};
            }
        } catch(e) {}

        // ── approach 3: legacy Whisper events ────────────────────────────────
        try {
            if (window.Whisper && window.Whisper.events) {
                window.Whisper.events.trigger('showConversation', conv);
                return {ok: true, triggered: -1, method: 'WhisperEvents'};
            }
        } catch(e) {}

        return {ok: false, error: 'no download API available', triggered: 0, method: 'none'};
    } catch(err) {
        return {ok: false, error: err.message || String(err), triggered: 0};
    }
})(%s, %s);
""" % (json.dumps(group_id), json.dumps(group_name))

        try:
            result = await self.evaluate_js(js)
            if isinstance(result, dict):
                log.info(
                    "Attachment trigger for group %s/%s: ok=%s triggered=%s method=%s error=%s",
                    group_id[:20], group_name, result.get("ok"), result.get("triggered"),
                    result.get("method"), result.get("error"),
                )
                return result
            log.warning("Unexpected trigger result: %r", result)
            return {"ok": False, "error": "unexpected result", "triggered": 0}
        except Exception as e:
            log.exception("trigger_attachment_downloads failed for group %s", group_id[:20])
            return {"ok": False, "error": str(e), "triggered": 0}

    async def setup_message_hook(self):
        """
        Inject JavaScript to hook into Signal's message receive pipeline.
        
        This sets up a listener that will call back to us when new messages arrive.
        """
        js_code = """
        (function() {
            if (window.__signalBotHooked) {
                return { success: true, message: 'Already hooked' };
            }
            
            try {
                // Hook into the message receive pipeline
                const originalReceive = window.Signal.Data.saveMessage;
                if (originalReceive) {
                    window.Signal.Data.saveMessage = async function(data, options) {
                        // Call original
                        const result = await originalReceive.call(this, data, options);
                        
                        // Notify our DevTools client
                        if (data && data.type === 'incoming') {
                            console.log('__signal_message__', JSON.stringify({
                                id: data.id,
                                conversationId: data.conversationId,
                                timestamp: data.timestamp || data.sent_at,
                                source: data.source || data.sourceUuid,
                                body: data.body || '',
                                type: data.type,
                                groupId: data.groupId,
                                expireTimer: data.expireTimer || 0,
                            }));
                        }
                        
                        return result;
                    };
                    
                    window.__signalBotHooked = true;
                    return { success: true };
                } else {
                    return { success: false, error: 'Signal.Data.saveMessage not found' };
                }
            } catch (err) {
                return { success: false, error: err.message };
            }
        })();
        """
        
        try:
            result = await self.evaluate_js(js_code)
            if isinstance(result, dict) and result.get("success"):
                log.info("Message hook installed successfully")
                return True
            else:
                log.warning("Failed to install message hook: %s", result)
                return False
        except Exception as e:
            log.exception("Failed to setup message hook")
            return False
    
    @property
    def is_connected(self) -> bool:
        """Check if connected to DevTools."""
        return self._connected and self._ws is not None


# Singleton instance
_devtools_client: Optional[DevToolsClient] = None


async def get_devtools_client(debug_port: int = 9222) -> DevToolsClient:
    """Get or create the DevTools client singleton."""
    global _devtools_client
    
    if _devtools_client is None or not _devtools_client.is_connected:
        _devtools_client = DevToolsClient(debug_port=debug_port)
        await _devtools_client.connect()
    
    return _devtools_client
