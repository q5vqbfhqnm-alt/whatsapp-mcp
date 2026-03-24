import hashlib
import os
import secrets
import signal
import sys
import urllib.parse
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from whatsapp import (
    download_media as whatsapp_download_media,
)
from whatsapp import (
    get_chat as whatsapp_get_chat,
)
from whatsapp import (
    get_contact_chats as whatsapp_get_contact_chats,
)
from whatsapp import (
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
)
from whatsapp import (
    get_last_interaction as whatsapp_get_last_interaction,
)
from whatsapp import (
    get_message_context as whatsapp_get_message_context,
)
from whatsapp import (
    get_sender_name as whatsapp_get_sender_name,
)
from whatsapp import (
    list_chats as whatsapp_list_chats,
)
from whatsapp import (
    list_messages as whatsapp_list_messages,
)
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
)
from whatsapp import (
    send_audio_message as whatsapp_audio_voice_message,
)
from whatsapp import (
    send_file as whatsapp_send_file,
)
from whatsapp import (
    send_message as whatsapp_send_message,
)

# Initialize FastMCP server
mcp = FastMCP("whatsapp")


@mcp.tool()
def search_contacts(query: str) -> list[dict[str, Any]]:
    """Search WhatsApp contacts by name or phone number.

    Args:
        query: Search term to match against contact names or phone numbers
    """
    contacts = whatsapp_search_contacts(query)
    return contacts


@mcp.tool()
def get_contact(
    identifier: str | None = None,
    phone_number: str | None = None,
    phone: str | None = None,
) -> dict[str, Any]:
    """Look up a WhatsApp contact by phone number, LID, or full JID.

    Automatically detects the identifier type and queries appropriately.

    Args:
        identifier: Phone number, LID, or full JID. Examples:
                    - "12025551234" (phone number)
                    - "184125298348272" (LID - long numeric)
                    - "12025551234@s.whatsapp.net" (phone JID)
                    - "184125298348272@lid" (LID JID)
        phone_number: Backward-compatible alias for `identifier`.
        phone: Backward-compatible alias for `identifier` (matches README parameter name).

    Returns:
        Dictionary with jid, name, display_name, is_lid, and resolved status
    """
    if identifier is None:
        identifier = phone_number
    if identifier is None:
        identifier = phone
    if identifier is None:
        raise ValueError("Missing required argument: identifier (or phone_number / phone)")

    identifier = identifier.strip()
    if not identifier:
        raise ValueError("identifier must be non-empty")

    # Detect identifier type and normalize to JID.
    if "@" in identifier:
        # Already a JID - use as-is
        jid = identifier
        is_lid = jid.endswith("@lid") or jid.split("@", 1)[-1] == "lid"
    else:
        digits = "".join(c for c in identifier if c.isdigit())
        if digits:
            # WhatsApp phone numbers are max 15 digits (E.164). Longer numeric IDs are typically LIDs.
            # For 15-digit numbers, ambiguity exists (could be phone or LID), so we try phone first and
            # fall back to LID if nothing is found.
            if len(digits) > 15:
                jid = f"{digits}@lid"
                is_lid = True
            else:
                jid = f"{digits}@s.whatsapp.net"
                is_lid = False
        else:
            # Non-numeric and not a JID; try as-is.
            jid = identifier
            is_lid = False

    jid_user = jid.split("@", 1)[0]

    display_name: str | None = None
    resolved = False

    # Prefer chats table lookup via get_chat (works for both phone and LID contacts).
    candidates: list[tuple[str, bool]] = [(jid, is_lid)]
    if "@" not in identifier and identifier.isdigit() and len(identifier) == 15:
        # 15-digit numeric identifier is ambiguous (could be phone or LID).
        # Try LID JID as a fallback if phone JID isn't found.
        candidates.append((f"{identifier}@lid", True))

    chat = None
    for candidate_jid, candidate_is_lid in candidates:
        chat = whatsapp_get_chat(candidate_jid, include_last_message=False)
        if chat:
            jid = candidate_jid
            is_lid = candidate_is_lid
            jid_user = jid.split("@", 1)[0]
            break

    if chat and chat.get("name"):
        display_name = chat["name"]
        resolved = display_name not in (jid, jid_user)
    else:
        # Fallback: best-effort sender-name resolution (may use fuzzy LIKE lookup).
        display_name = whatsapp_get_sender_name(jid)
        resolved = display_name not in (jid, jid_user, identifier)

    return {
        "identifier": identifier,
        "jid": jid,
        "phone_number": jid_user if not is_lid else None,
        "lid": jid_user if is_lid else None,
        "name": display_name if resolved else jid_user,
        "display_name": display_name,
        "is_lid": is_lid,
        "resolved": resolved,
    }


@mcp.tool()
def list_messages(
    after: str | None = None,
    before: str | None = None,
    sender_phone_number: str | None = None,
    chat_jid: str | None = None,
    query: str | None = None,
    limit: int = 50,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
    sort_by: str = "newest",
) -> list[dict[str, Any]]:
    """Get WhatsApp messages matching specified criteria with optional context.

    Each message includes sender_display showing "Name (phone)" for easy identification.

    Args:
        after: ISO-8601 date string (e.g., "2026-01-01" or "2026-01-01T09:00:00")
        before: ISO-8601 date string (e.g., "2026-01-09" or "2026-01-09T18:00:00")
        sender_phone_number: Phone number to filter by sender (e.g., "12025551234")
        chat_jid: Chat JID to filter by (e.g., "12025551234@s.whatsapp.net" or group JID)
        query: Search term to filter messages by content
        limit: Max messages to return (default 50, max 500)
        page: Page number for pagination (default 0)
        include_context: Include surrounding messages for context (default True)
        context_before: Messages to include before each match (default 1)
        context_after: Messages to include after each match (default 1)
        sort_by: "newest" (default, most recent first) or "oldest" (chronological)
    """
    # Cap limit at 500 to prevent excessive queries
    limit = min(limit, 500)
    messages = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after,
        sort_by=sort_by,
    )
    return messages


@mcp.tool()
def list_chats(
    query: str | None = None,
    limit: int = 50,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
) -> list[dict[str, Any]]:
    """Get WhatsApp chats matching specified criteria.

    Args:
        query: Search term to filter chats by name or JID
        limit: Max chats to return (default 50, max 200)
        page: Page number for pagination (default 0)
        include_last_message: Include the last message in each chat (default True)
        sort_by: "last_active" (default, most recent first) or "name" (alphabetical)
    """
    # Cap limit at 200 to prevent excessive queries
    limit = min(limit, 200)
    chats = whatsapp_list_chats(
        query=query, limit=limit, page=page, include_last_message=include_last_message, sort_by=sort_by
    )
    return chats


@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True) -> dict[str, Any]:
    """Get WhatsApp chat metadata by JID.

    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return chat


@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> dict[str, Any]:
    """Get WhatsApp chat metadata by sender phone number.

    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    return chat


@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> list[dict[str, Any]]:
    """Get all WhatsApp chats involving the contact.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    chats = whatsapp_get_contact_chats(jid, limit, page)
    return chats


@mcp.tool()
def get_last_interaction(jid: str) -> dict[str, Any]:
    """Get most recent WhatsApp message involving the contact.

    Args:
        jid: The JID of the contact to search for

    Returns:
        Message dictionary with id, timestamp, sender, content, etc. or empty dict if not found.
    """
    message = whatsapp_get_last_interaction(jid)
    return message if message else {}


@mcp.tool()
def get_message_context(message_id: str, before: int = 5, after: int = 5) -> dict[str, Any]:
    """Get context around a specific WhatsApp message.

    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
    """
    context = whatsapp_get_message_context(message_id, before, after)
    return context


@mcp.tool()
def send_message(recipient: str, message: str) -> dict[str, Any]:
    """Send a WhatsApp message to a person or group. For group chats use the JID.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        message: The message text to send

    Returns:
        A dictionary containing success status and a status message
    """
    # Validate input
    if not recipient:
        return {"success": False, "message": "Recipient must be provided"}

    # Call the whatsapp_send_message function with the unified recipient parameter
    success, status_message = whatsapp_send_message(recipient, message)
    return {"success": success, "message": status_message}


@mcp.tool()
def send_file(recipient: str, media_path: str) -> dict[str, Any]:
    """Send a file such as a picture, raw audio, video or document via WhatsApp to the specified recipient. For group messages use the JID.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the media file to send (image, video, document)

    Returns:
        A dictionary containing success status and a status message
    """

    # Call the whatsapp_send_file function
    success, status_message = whatsapp_send_file(recipient, media_path)
    return {"success": success, "message": status_message}


@mcp.tool()
def send_audio_message(recipient: str, media_path: str) -> dict[str, Any]:
    """Send any audio file as a WhatsApp audio message to the specified recipient. For group messages use the JID. If it errors due to ffmpeg not being installed, use send_file instead.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the audio file to send (will be converted to Opus .ogg if it's not a .ogg file)

    Returns:
        A dictionary containing success status and a status message
    """
    success, status_message = whatsapp_audio_voice_message(recipient, media_path)
    return {"success": success, "message": status_message}


@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message

    Returns:
        A dictionary containing success status, a status message, and the file path if successful
    """
    file_path = whatsapp_download_media(message_id, chat_jid)

    if file_path:
        return {"success": True, "message": "Media downloaded successfully", "file_path": file_path}
    else:
        return {"success": False, "message": "Failed to download media"}


class ApiKeyAuthMiddleware:
    """ASGI middleware that checks for a valid Bearer token on MCP endpoints."""

    # Paths that bypass Bearer auth (handled by OAuth routes or public)
    BYPASS_PATHS = (
        "/.well-known/",
        "/authorize",
        "/token",
        "/register",
    )

    def __init__(self, app: ASGIApp, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")

            # OAuth endpoints are handled by explicit routes, let them through
            if any(path.startswith(p) for p in self.BYPASS_PATHS):
                await self.app(scope, receive, send)
                return

            from starlette.requests import Request as _Req

            request = _Req(scope, receive)
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {self.api_key}":
                response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# --- Minimal OAuth 2.1 server for claude.ai connector compatibility ---

# In-memory stores (fine for single-instance deployment)
_oauth_clients: dict[str, dict] = {}
_oauth_codes: dict[str, dict] = {}

LOGIN_PAGE = """<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsApp MCP — Authorize</title>
<style>
body{font-family:system-ui,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#0a0a0a;color:#fff}
.card{background:#1a1a1a;padding:40px;border-radius:12px;width:100%;max-width:360px;box-shadow:0 4px 24px rgba(0,0,0,.5)}
h1{font-size:1.3rem;margin:0 0 24px;text-align:center}
input[type=password]{width:100%;padding:12px;border:1px solid #333;border-radius:8px;background:#0a0a0a;color:#fff;font-size:1rem;box-sizing:border-box;margin-bottom:16px}
button{width:100%;padding:12px;border:none;border-radius:8px;background:#25D366;color:#fff;font-size:1rem;font-weight:600;cursor:pointer}
button:hover{background:#1da851}
</style></head>
<body><div class="card">
<h1>WhatsApp MCP</h1>
{{error}}
<form method="POST" action="{{action}}">
<input type="password" name="password" placeholder="Enter password" autofocus required>
<button type="submit">Authorize</button>
</form></div></body></html>"""


def build_oauth_routes(api_key: str, base_url: str) -> list[Route]:
    """Build OAuth routes that issue the MCP API key as the access token."""

    async def metadata(request: Request):
        """OAuth 2.0 Authorization Server Metadata (RFC 8414)."""
        return JSONResponse({
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/authorize",
            "token_endpoint": f"{base_url}/token",
            "registration_endpoint": f"{base_url}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        })

    async def register(request: Request):
        """Dynamic client registration (RFC 7591)."""
        body = await request.json()
        client_id = secrets.token_hex(16)
        _oauth_clients[client_id] = {
            "redirect_uris": body.get("redirect_uris", []),
            "client_name": body.get("client_name", "unknown"),
        }
        return JSONResponse({
            "client_id": client_id,
            "redirect_uris": body.get("redirect_uris", []),
            "client_name": body.get("client_name", "unknown"),
        }, status_code=201)

    async def authorize(request: Request):
        """Authorization endpoint — requires password before issuing auth code."""
        client_id = request.query_params.get("client_id", "")
        redirect_uri = request.query_params.get("redirect_uri", "")
        state = request.query_params.get("state", "")
        code_challenge = request.query_params.get("code_challenge", "")
        code_challenge_method = request.query_params.get("code_challenge_method", "")

        oauth_password = os.getenv("MCP_OAUTH_PASSWORD", "")

        if request.method == "POST":
            form = await request.form()
            password = form.get("password", "")

            if not oauth_password or password != oauth_password:
                # Re-render form with error
                qs = request.url.query
                return HTMLResponse(LOGIN_PAGE.replace("{{action}}", f"/authorize?{qs}").replace("{{error}}", '<p style="color:#e74c3c;margin-bottom:16px">Wrong password.</p>'), status_code=403)

            # Password correct — issue auth code
            code = secrets.token_hex(32)
            _oauth_codes[code] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
            }
            params = urllib.parse.urlencode({"code": code, "state": state})
            return RedirectResponse(f"{redirect_uri}?{params}", status_code=302)

        # GET — show login form
        qs = request.url.query
        return HTMLResponse(LOGIN_PAGE.replace("{{action}}", f"/authorize?{qs}").replace("{{error}}", ""))

    async def token(request: Request):
        """Token endpoint — exchanges auth code for access token (our API key)."""
        body = await request.form()
        grant_type = body.get("grant_type", "")
        code = body.get("code", "")
        code_verifier = body.get("code_verifier", "")

        if grant_type != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        stored = _oauth_codes.pop(code, None)
        if not stored:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Verify PKCE code_challenge
        if stored.get("code_challenge"):
            digest = hashlib.sha256(code_verifier.encode()).digest()
            import base64
            computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if computed != stored["code_challenge"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

        return JSONResponse({
            "access_token": api_key,
            "token_type": "bearer",
            "scope": "",
        })

    return [
        Route("/.well-known/oauth-authorization-server", metadata, methods=["GET"]),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token, methods=["POST"]),
    ]


def shutdown_handler(signum, frame):
    """Handle shutdown signals gracefully to prevent zombie processes."""
    sys.exit(0)


if __name__ == "__main__":
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if transport == "sse":
        api_key = os.getenv("MCP_API_KEY")
        if not api_key:
            print("ERROR: MCP_API_KEY is required when using SSE transport", file=sys.stderr)
            sys.exit(1)

        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8765"))
        base_url = os.getenv("MCP_BASE_URL", f"https://wa.cansavasan.com")

        # Build the SSE Starlette app
        app = mcp.sse_app()

        # Add OAuth routes
        oauth_routes = build_oauth_routes(api_key, base_url)
        for route in oauth_routes:
            app.routes.insert(0, route)

        # Add auth middleware (skips OAuth paths)
        app.add_middleware(ApiKeyAuthMiddleware, api_key=api_key)

        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)

        import anyio

        anyio.run(server.serve)
    else:
        mcp.run(transport="stdio")
