#!/usr/bin/env python3
"""
Parallel-Poke MCP Server

A bridge between Parallel's Monitor API and Poke's messaging service.
- Receives webhooks from Parallel when monitors detect changes
- Forwards event information to Poke for SMS notifications
- Exposes MCP tools for Poke to manage monitors conversationally
"""

import base64
import hashlib
import hmac
import math
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

PARALLEL_API_KEY = os.environ.get("PARALLEL_API_KEY", "")
PARALLEL_WEBHOOK_SECRET = os.environ.get("PARALLEL_WEBHOOK_SECRET", "")
POKE_API_KEY = os.environ.get("POKE_API_KEY", "")
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "")  # Your deployed server URL

PARALLEL_API_BASE = "https://api.parallel.ai/v1alpha"
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

# -----------------------------------------------------------------------------
# Initialize FastMCP
# -----------------------------------------------------------------------------

auth_provider = None
if MCP_AUTH_TOKEN:
    auth_provider = DebugTokenVerifier(
        validate=lambda token: token == MCP_AUTH_TOKEN,
        client_id="parallel-poke-mcp",
        scopes=["mcp:access"],
    )

mcp = FastMCP(
    "Parallel Monitor Bridge",
    instructions="""
    This server manages Parallel web monitors and sends SMS notifications via Poke.
    Use these tools to create, list, update, and delete monitors. A monitor watches
    for web changes and triggers alerts automatically.

    Key rules:
    - cadence must be one of: "hourly", "daily", "weekly"
    - monitors are identified by "monitor_id"
    - use list_monitors to find IDs, then get_monitor for details
    - list_recent_events returns recent detected changes (when available)
    - delete_monitor stops future checks; canceled monitors may still appear in lists

    When creating a monitor, pick a clear query describing what to watch.
    If an error occurs, return the error to the user verbatim.
    """,
    auth=auth_provider,
)

# -----------------------------------------------------------------------------
# HTTP Clients
# -----------------------------------------------------------------------------


def get_parallel_client() -> httpx.Client:
    """Create an HTTP client for Parallel API calls."""
    return httpx.Client(
        base_url=PARALLEL_API_BASE,
        headers={"x-api-key": PARALLEL_API_KEY},
        timeout=30.0,
    )


def get_poke_client() -> httpx.Client:
    """Create an HTTP client for Poke API calls."""
    return httpx.Client(
        headers={"Authorization": f"Bearer {POKE_API_KEY}"},
        timeout=30.0,
    )


# -----------------------------------------------------------------------------
# Webhook Signature Verification
# -----------------------------------------------------------------------------


def verify_webhook_signature(
    payload: bytes,
    webhook_id: str,
    timestamp: str,
    signature_header: str,
) -> bool:
    """
    Verify Parallel webhook signature using HMAC-SHA256.

    Signature format: v1,<base64-encoded-signature>
    Signing payload: <webhook-id>.<timestamp>.<payload>
    """
    if not PARALLEL_WEBHOOK_SECRET:
        # Skip verification if no secret configured
        return True

    try:
        # Construct signing payload
        signing_payload = f"{webhook_id}.{timestamp}.{payload.decode('utf-8')}"

        # Compute expected signature
        expected_sig = hmac.new(
            PARALLEL_WEBHOOK_SECRET.encode("utf-8"),
            signing_payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        # Parse signature header. May contain multiple signatures, space-delimited.
        for sig in signature_header.split():
            if not sig.startswith("v1,"):
                continue
            try:
                provided_sig = base64.b64decode(sig[3:])
            except Exception:
                continue
            if hmac.compare_digest(provided_sig, expected_sig):
                return True

        return False
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Poke Messaging
# -----------------------------------------------------------------------------


def send_to_poke(message: str) -> bool:
    """Send a message to Poke's inbound SMS webhook."""
    if not POKE_API_KEY:
        print("Warning: POKE_API_KEY not configured, skipping notification")
        return False

    try:
        with get_poke_client() as client:
            response = client.post(
                "https://poke.com/api/v1/inbound-sms/webhook",
                json={"message": message},
            )
            response.raise_for_status()
            return True
    except Exception as e:
        print(f"Error sending to Poke: {e}")
        return False


# -----------------------------------------------------------------------------
# Parallel API Helpers
# -----------------------------------------------------------------------------


def fetch_event_group(monitor_id: str, event_group_id: str) -> dict:
    """Fetch full event details from Parallel."""
    with get_parallel_client() as client:
        response = client.get(
            f"/monitors/{monitor_id}/event_groups/{event_group_id}"
        )
        response.raise_for_status()
        return response.json()


def fetch_monitor(monitor_id: str) -> dict:
    """Fetch monitor details from Parallel."""
    with get_parallel_client() as client:
        response = client.get(f"/monitors/{monitor_id}")
        response.raise_for_status()
        return response.json()


# -----------------------------------------------------------------------------
# Webhook Endpoint
# -----------------------------------------------------------------------------


@mcp.custom_route("/webhook/parallel", methods=["POST"])
async def parallel_webhook(request: Request) -> JSONResponse:
    """
    Receive webhooks from Parallel Monitor API.

    When a monitor detects changes, Parallel sends a webhook with the event
    group ID. We fetch the full event details and forward them to Poke.
    """
    # Read raw body for signature verification
    body = await request.body()

    # Verify signature if secret is configured
    webhook_id = request.headers.get("webhook-id", "")
    timestamp = request.headers.get("webhook-timestamp", "")
    signature = request.headers.get("webhook-signature", "")

    if PARALLEL_WEBHOOK_SECRET:
        if not verify_webhook_signature(body, webhook_id, timestamp, signature):
            return JSONResponse(
                {"error": "Invalid signature"},
                status_code=401,
            )

    # Parse payload
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid JSON payload"},
            status_code=400,
        )

    # Monitor webhook payload uses: data.monitor_id + data.event.event_group_id
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        data = {}
    event = data.get("event", {})
    if not isinstance(event, dict):
        event = {}

    monitor_id = data.get("monitor_id") or payload.get("monitor_id")
    event_group_id = (
        event.get("event_group_id")
        or data.get("event_group_id")
        or payload.get("event_group_id")
    )
    if not monitor_id or not event_group_id:
        # Handle non-detected event types (completed/failed) that don't include event_group_id
        event_type = payload.get("type", "")
        if event_type in ("monitor.execution.completed", "monitor.execution.failed"):
            try:
                monitor = fetch_monitor(monitor_id)
            except Exception as e:
                print(f"Error fetching monitor for completion/failure: {e}")
                return JSONResponse({"error": "Failed to fetch monitor details"}, status_code=502)

            monitor_query = monitor.get("query", "Unknown query")
            monitor_cadence = monitor.get("cadence", "unknown")

            message_parts = [
                "Monitor Alert from Parallel",
                f"Query: {monitor_query}",
                f"Cadence: {monitor_cadence}",
                "",
            ]

            if event_type == "monitor.execution.completed":
                message_parts.append("Monitor run completed with no detected events.")
            else:
                message_parts.append("Monitor run failed.")

            if isinstance(event, dict) and event:
                message_parts.append("")
                message_parts.append(f"Event details: {event}")

            message_parts.append("")
            message_parts.append(f"Monitor ID: {monitor_id}")

            send_to_poke("\n".join(message_parts))
            return JSONResponse({"status": "ok", "event_type": event_type})

        return JSONResponse(
            {"error": "Missing monitor_id or event_group_id"},
            status_code=400,
        )

    # Fetch full event details
    try:
        event_group = fetch_event_group(monitor_id, event_group_id)
        monitor = fetch_monitor(monitor_id)
    except Exception as e:
        print(f"Error fetching from Parallel: {e}")
        return JSONResponse(
            {"error": "Failed to fetch event details"},
            status_code=502,
        )

    # Format message for Poke
    # Poke is an AI service, so we send structured info and let it format the SMS
    # API returns: {events: [{type, output, event_date, source_urls, ...}, ...]}
    monitor_query = monitor.get("query", "Unknown query")
    monitor_cadence = monitor.get("cadence", "unknown")
    all_events = event_group.get("events", [])

    # Filter to actual events (exclude type="completion" metadata entries)
    events = [e for e in all_events if e.get("type") == "event"]

    message_parts = [
        f"Monitor Alert from Parallel",
        f"Query: {monitor_query}",
        f"Cadence: {monitor_cadence}",
        f"",
        f"{len(events)} event(s) detected:",
    ]

    for i, event in enumerate(events, 1):
        desc = event.get("output", "No description")
        source_urls = event.get("source_urls", [])
        url = source_urls[0] if source_urls else ""
        date = event.get("event_date", "")

        message_parts.append(f"")
        message_parts.append(f"{i}. {desc}")
        if url:
            message_parts.append(f"   Source: {url}")
        if date:
            message_parts.append(f"   Date: {date}")

    message_parts.append(f"")
    message_parts.append(f"Monitor ID: {monitor_id}")

    message = "\n".join(message_parts)

    # Send to Poke
    send_to_poke(message)

    return JSONResponse({"status": "ok", "events_processed": len(events)})


# -----------------------------------------------------------------------------
# Health Check
# -----------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for load balancers and monitoring."""
    return JSONResponse({
        "status": "healthy",
        "service": "parallel-poke-mcp",
        "parallel_configured": bool(PARALLEL_API_KEY),
        "poke_configured": bool(POKE_API_KEY),
        "webhook_url": f"{WEBHOOK_BASE_URL}/webhook/parallel" if WEBHOOK_BASE_URL else None,
    })


# -----------------------------------------------------------------------------
# MCP Tools - Monitor Management
# -----------------------------------------------------------------------------


@mcp.tool(
    description="Create a new monitor. cadence must be hourly/daily/weekly. Returns monitor_id."
)
def create_monitor(
    query: str,
    cadence: str = "daily",
) -> dict:
    """
    Create a monitor to track web changes.

    Args:
        query: Natural language description of what to monitor.
               Examples: "OpenAI announcements", "React changelog updates",
               "funding news for AI startups"
        cadence: How often to check - "hourly", "daily", or "weekly"

    Returns:
        The created monitor details including its ID.
    """
    if cadence not in ("hourly", "daily", "weekly"):
        return {"error": f"Invalid cadence '{cadence}'. Must be hourly, daily, or weekly."}

    if not PARALLEL_API_KEY:
        return {"error": "PARALLEL_API_KEY not configured"}

    webhook_config = None
    if WEBHOOK_BASE_URL:
        webhook_config = {
            "url": f"{WEBHOOK_BASE_URL}/webhook/parallel",
            "event_types": ["monitor.event.detected"],
        }

    try:
        with get_parallel_client() as client:
            response = client.post(
                "/monitors",
                json={
                    "query": query,
                    "cadence": cadence,
                    "webhook": webhook_config,
                },
            )
            response.raise_for_status()
            monitor = response.json()

            return {
                "success": True,
                "monitor_id": monitor.get("monitor_id"),
                "query": query,
                "cadence": cadence,
                "webhook_configured": bool(webhook_config),
                "message": f"Monitor created! It will check {cadence} for: {query}",
            }
    except httpx.HTTPStatusError as e:
        return {"error": f"Failed to create monitor: {e.response.text}"}
    except Exception as e:
        return {"error": f"Failed to create monitor: {str(e)}"}


@mcp.tool(description="List monitors with id/query/cadence/status.")
def list_monitors() -> dict:
    """
    List all active monitors.

    Returns:
        A list of monitors with their IDs, queries, and cadences.
    """
    if not PARALLEL_API_KEY:
        return {"error": "PARALLEL_API_KEY not configured"}

    # API returns: [{monitor_id, query, status, cadence, ...}, ...]
    try:
        with get_parallel_client() as client:
            response = client.get("/monitors")
            response.raise_for_status()
            monitors = response.json()  # Direct list

            result = [
                {
                    "id": m.get("monitor_id"),
                    "query": m.get("query"),
                    "cadence": m.get("cadence"),
                    "status": m.get("status"),
                }
                for m in monitors
            ]

            return {
                "monitors": result,
                "count": len(result),
            }
    except httpx.HTTPStatusError as e:
        return {"error": f"Failed to list monitors: {e.response.text}"}
    except Exception as e:
        return {"error": f"Failed to list monitors: {str(e)}"}


@mcp.tool(description="Get details for a monitor_id (query/cadence/status/webhook/created_at).")
def get_monitor(monitor_id: str) -> dict:
    """
    Get details of a specific monitor.

    Args:
        monitor_id: The ID of the monitor to retrieve.

    Returns:
        Monitor details including configuration and status.
    """
    if not PARALLEL_API_KEY:
        return {"error": "PARALLEL_API_KEY not configured"}

    try:
        with get_parallel_client() as client:
            response = client.get(f"/monitors/{monitor_id}")
            response.raise_for_status()
            monitor = response.json()

            return {
                "id": monitor.get("monitor_id"),
                "query": monitor.get("query"),
                "cadence": monitor.get("cadence"),
                "status": monitor.get("status"),
                "webhook": monitor.get("webhook"),
                "created_at": monitor.get("created_at"),
                "last_run_at": monitor.get("last_run_at"),
                "metadata": monitor.get("metadata"),
            }
    except httpx.HTTPStatusError as e:
        return {"error": f"Failed to get monitor: {e.response.text}"}
    except Exception as e:
        return {"error": f"Failed to get monitor: {str(e)}"}


@mcp.tool(description="Update a monitor (cadence, webhook_url/event_types, metadata).")
def update_monitor(
    monitor_id: str,
    cadence: Optional[str] = None,
    webhook_url: Optional[str] = None,
    webhook_event_types: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> dict:
    """
    Update a monitor's settings.

    Args:
        monitor_id: The ID of the monitor to update.
        cadence: New cadence - "hourly", "daily", or "weekly"
        webhook_url: New webhook URL for this monitor.
        webhook_event_types: Optional list of event types to subscribe to.
                             Defaults to ["monitor.event.detected"] if not provided with webhook_url.
        metadata: Optional dictionary of user-provided metadata to store with the monitor.

    Returns:
        Updated monitor details.
    """
    if not PARALLEL_API_KEY:
        return {"error": "PARALLEL_API_KEY not configured"}

    if cadence and cadence not in ("hourly", "daily", "weekly"):
        return {"error": f"Invalid cadence '{cadence}'. Must be hourly, daily, or weekly."}

    update_data: Dict[str, Any] = {}
    if cadence:
        update_data["cadence"] = cadence

    if webhook_event_types is not None:
        if not isinstance(webhook_event_types, list) or not webhook_event_types:
            return {"error": "webhook_event_types must be a non-empty list"}
        allowed = {
            "monitor.event.detected",
            "monitor.execution.completed",
            "monitor.execution.failed",
        }
        invalid = [t for t in webhook_event_types if t not in allowed]
        if invalid:
            return {
                "error": "Invalid webhook_event_types. Allowed: "
                + ", ".join(sorted(allowed))
            }

    if webhook_url:
        update_data["webhook"] = {
            "url": webhook_url,
            "event_types": webhook_event_types or ["monitor.event.detected"],
        }
    elif webhook_event_types is not None:
        return {"error": "webhook_url is required when webhook_event_types is provided"}

    if metadata is not None:
        if not isinstance(metadata, dict):
            return {"error": "metadata must be an object/dict"}
        # Parallel API expects string values for metadata.
        update_data["metadata"] = {str(k): str(v) for k, v in metadata.items()}

    if not update_data:
        return {"error": "No updates specified"}

    try:
        with get_parallel_client() as client:
            response = client.post(
                f"/monitors/{monitor_id}",
                json=update_data,
            )
            response.raise_for_status()
            monitor = response.json()

            return {
                "success": True,
                "id": monitor.get("monitor_id"),
                "cadence": monitor.get("cadence"),
                "webhook": monitor.get("webhook"),
                "metadata": monitor.get("metadata"),
                "message": "Monitor updated successfully",
            }
    except httpx.HTTPStatusError as e:
        return {"error": f"Failed to update monitor: {e.response.text}"}
    except Exception as e:
        return {"error": f"Failed to update monitor: {str(e)}"}


@mcp.tool(description="Delete a monitor. This stops future checks (may appear as canceled).")
def delete_monitor(monitor_id: str) -> dict:
    """
    Delete a monitor.

    Args:
        monitor_id: The ID of the monitor to delete.

    Returns:
        Confirmation of deletion.
    """
    if not PARALLEL_API_KEY:
        return {"error": "PARALLEL_API_KEY not configured"}

    try:
        with get_parallel_client() as client:
            response = client.delete(f"/monitors/{monitor_id}")
            response.raise_for_status()

            return {
                "success": True,
                "message": f"Monitor {monitor_id} deleted successfully",
            }
    except httpx.HTTPStatusError as e:
        return {"error": f"Failed to delete monitor: {e.response.text}"}
    except Exception as e:
        return {"error": f"Failed to delete monitor: {str(e)}"}


@mcp.tool(
    description="List recent detected events (may be empty). Supports monitor_id + limit (max 50)."
)
def list_recent_events(
    monitor_id: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """
    List recent events from monitors.

    Args:
        monitor_id: Optional - filter to a specific monitor's events.
                   If not provided, returns events from all active monitors.
        limit: Maximum number of events to return (default 10, max 50).

    Returns:
        List of recent events with descriptions and sources.
    """
    # -------------------------------------------------------------------------
    # Sorting & Ordering Approach:
    #
    # - Single monitor: Trust API order. Parallel returns events in reverse
    #   chronological order (most recent detections first).
    #
    # - All monitors: Sort by event_date (YYYY-MM-DD). This is day-granular,
    #   so same-day events across monitors cannot be precisely ordered by
    #   detection time. For ties, we use stable sort (preserve fetch order).
    #
    # Note: The Parallel Monitor API is currently in alpha. In the future,
    # finer-grained timestamps on events may enable better cross-monitor
    # ordering. An alternative would be to parse the `completion` entries'
    # `monitor_ts` timestamps and associate them with events, but this adds
    # complexity for marginal benefit given the current API shape.
    # -------------------------------------------------------------------------

    if not PARALLEL_API_KEY:
        return {"error": "PARALLEL_API_KEY not configured"}

    limit = min(limit, 50)

    # Helper to extract event data
    # API event fields: output, event_date, source_urls (array)
    def parse_event(e: dict, monitor_query: Optional[str] = None) -> dict:
        source_urls = e.get("source_urls", [])
        result = {
            "description": e.get("output"),
            "url": source_urls[0] if source_urls else None,
            "date": e.get("event_date"),
        }
        if monitor_query:
            result["monitor_query"] = monitor_query
        return result

    try:
        with get_parallel_client() as client:
            if monitor_id:
                # -------------------------------------------------------------
                # Single monitor: Trust API order (most recent first)
                # -------------------------------------------------------------
                response = client.get(
                    f"/monitors/{monitor_id}/events",
                    params={"limit": limit},
                )
                response.raise_for_status()
                # API returns: {events: [{type, output, event_date, source_urls, ...}, ...]}
                raw_events = response.json().get("events", [])

                # Filter to actual events (exclude type="completion" metadata)
                events = [
                    parse_event(e)
                    for e in raw_events
                    if e.get("type") == "event"
                ][:limit]

                return {
                    "monitor_id": monitor_id,
                    "events": events,
                    "count": len(events),
                }
            else:
                # -------------------------------------------------------------
                # All monitors: Aggregate and sort by event_date
                # -------------------------------------------------------------
                monitors_resp = client.get("/monitors")
                monitors_resp.raise_for_status()
                # API returns: [{monitor_id, query, status, ...}, ...]
                all_monitors = monitors_resp.json()

                # Filter to active monitors only
                active_monitors = [
                    m for m in all_monitors if m.get("status") == "active"
                ]

                # Edge case: no active monitors
                if not active_monitors:
                    return {"events": [], "count": 0}

                # Calculate how many events to fetch per monitor
                # We want `limit` total, distributed across monitors
                events_per_monitor = math.ceil(limit / len(active_monitors))

                all_events = []
                for m in active_monitors:
                    try:
                        mid = m.get("monitor_id")
                        if not mid:
                            continue

                        events_resp = client.get(
                            f"/monitors/{mid}/events",
                            params={"limit": events_per_monitor},
                        )
                        events_resp.raise_for_status()
                        # API returns: {events: [...]}
                        raw_events = events_resp.json().get("events", [])

                        # Filter to actual events, parse, and add monitor context
                        for e in raw_events:
                            if e.get("type") == "event":
                                all_events.append(
                                    parse_event(e, monitor_query=m.get("query"))
                                )
                    except Exception:
                        # Best effort: skip monitors that fail
                        continue

                # Sort by event_date descending (most recent first)
                # Stable sort: ties preserve fetch order
                # Events without dates sort to the end (treated as oldest)
                all_events.sort(
                    key=lambda x: x.get("date") or "",
                    reverse=True,
                )

                # Return top `limit` events
                result_events = all_events[:limit]

                return {
                    "events": result_events,
                    "count": len(result_events),
                }
    except httpx.HTTPStatusError as e:
        return {"error": f"Failed to list events: {e.response.text}"}
    except Exception as e:
        return {"error": f"Failed to list events: {str(e)}"}


# -----------------------------------------------------------------------------
# Main Entry Point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")

    print(f"Starting Parallel-Poke MCP server on {host}:{port}")
    print(f"  MCP endpoint: http://{host}:{port}/mcp")
    print(f"  Webhook endpoint: http://{host}:{port}/webhook/parallel")
    print(f"  Health check: http://{host}:{port}/health")

    if not PARALLEL_API_KEY:
        print("  Warning: PARALLEL_API_KEY not set")
    if not POKE_API_KEY:
        print("  Warning: POKE_API_KEY not set")
    if not WEBHOOK_BASE_URL:
        print("  Warning: WEBHOOK_BASE_URL not set (monitors won't auto-configure webhooks)")

    mcp.run(
        transport="http",
        host=host,
        port=port,
        stateless_http=True,
    )
