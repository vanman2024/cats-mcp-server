"""
CATS MCP Server - Data & Configuration Toolsets

This module contains 7 toolset registration functions for data and configuration
management in the CATS API v3:

1. Tags - 2 tools
2. Webhooks - 4 tools
3. Users - 2 tools
4. Triggers - 2 tools
5. Attachments - 4 tools
6. Backups - 3 tools
7. Events - 5 tools

Total: 22 tools across 7 toolsets
"""
from __future__ import annotations

from typing import Any, Optional
from fastmcp import FastMCP


async def make_request(method: str, endpoint: str, params: dict = None, json_data: dict = None) -> dict:
    """Make authenticated request to CATS API"""
    # This is imported from server.py at runtime
    pass


def register_tags_tools(mcp: FastMCP) -> None:
    """
    Register tag management tools.

    Tags are labels that can be attached to candidates, jobs, and other entities.
    Tags are managed at the entity level (e.g., /candidates/{id}/tags).
    This toolset provides read access to the global tags list.
    """

    @mcp.tool()
    async def list_tags(per_page: int = 25, page: int = 1) -> dict[str, Any]:
        """List all tags in the system. Tags are labels used to categorize candidates, jobs, and other entities. To attach/detach tags, use the entity-specific endpoints: - POST /candidates/{id}/tags - DELETE /candidates/{id}/tags - POST /jobs/{id}/tags - DELETE /jobs/{id}/tags."""
        return await make_request("GET", "/tags", params={"per_page": per_page, "page": page})

    @mcp.tool()
    async def get_tag(tag_id: int) -> dict[str, Any]:
        """Get details of a specific tag."""
        return await make_request("GET", f"/tags/{tag_id}")


def register_webhooks_tools(mcp: FastMCP) -> None:
    """
    Register webhook management tools.

    Webhooks allow real-time notifications when events occur in CATS.
    Supports 24+ event types across candidates, jobs, applications, etc.

    Security: Webhooks include HMAC-SHA256 signatures for verification.
    """

    @mcp.tool()
    async def list_webhooks(per_page: int = 25, page: int = 1) -> dict[str, Any]:
        """List all configured webhooks."""
        return await make_request("GET", "/webhooks", params={"per_page": per_page, "page": page})

    @mcp.tool()
    async def get_webhook(webhook_id: int) -> dict[str, Any]:
        """Get details of a specific webhook configuration."""
        return await make_request("GET", f"/webhooks/{webhook_id}")

    @mcp.tool()
    async def create_webhook(
        url: str,
        events: list[str],
        active: bool = True,
        description: Optional[str] = None
    ) -> dict[str, Any]:
        """
        Create a new webhook subscription.

        Wraps: POST /webhooks

        The webhook will receive POST requests with JSON payloads when subscribed
        events occur. Each request includes:
        - X-CATS-Signature: HMAC-SHA256 signature for verification
        - X-CATS-Event: Event type that triggered the webhook
        - X-CATS-Delivery: Unique delivery ID

        Args:
            url: Target URL for webhook POST requests (must be HTTPS in production)
            events: List of event types to subscribe to (e.g., ["candidate.created", "job.updated"])
            active: Whether webhook should be active immediately (default: True)
            description: Optional description for the webhook

        Returns:
            dict containing:
            - id: New webhook ID
            - url: Configured URL
            - events: Subscribed events
            - active: Active status
            - secret: Generated secret key for signature verification (save this!)
            - created_at: Creation timestamp

        Example:
            >>> webhook = await create_webhook(
            ...     url="https://myapp.com/webhooks/cats",
            ...     events=["candidate.created", "candidate.updated"],
            ...     description="Sync candidates to CRM"
            ... )
            >>> print(f"Webhook created! Secret: {webhook['secret']}")
            >>> # Save the secret securely for signature verification
        """
        payload = {
            "url": url,
            "events": events,
            "active": active
        }
        if description:
            payload["description"] = description
        return await make_request("POST", "/webhooks", json_data=payload)

    @mcp.tool()
    async def delete_webhook(webhook_id: int) -> dict[str, Any]:
        """Delete a webhook subscription. This permanently removes the webhook configuration. All pending deliveries will be cancelled."""
        return await make_request("DELETE", f"/webhooks/{webhook_id}")


def register_users_tools(mcp: FastMCP) -> None:
    """
    Register user management tools.

    Users are team members with access to the CATS system.
    Access levels: read_only, edit, admin
    """

    @mcp.tool()
    async def list_users(per_page: int = 25, page: int = 1) -> dict[str, Any]:
        """List all users in the organization."""
        return await make_request("GET", "/users", params={"per_page": per_page, "page": page})

    @mcp.tool()
    async def get_user(user_id: int) -> dict[str, Any]:
        """Get details of a specific user."""
        return await make_request("GET", f"/users/{user_id}")


def register_triggers_tools(mcp: FastMCP) -> None:
    """
    Register trigger management tools (read-only).

    Triggers are automated actions that fire when pipeline statuses change.
    They are configured through the CATS UI and cannot be created via API.
    This toolset provides read access for monitoring trigger configurations.
    """

    @mcp.tool()
    async def list_triggers(per_page: int = 25, page: int = 1) -> dict[str, Any]:
        """List all configured triggers. Triggers automatically fire actions when candidates move through pipeline stages. Common uses: - Send automated emails when status changes - Create tasks for recruiters - Update custom fields - Fire webhooks Note: Triggers are configured in CATS UI, not via API."""
        return await make_request("GET", "/triggers", params={"per_page": per_page, "page": page})

    @mcp.tool()
    async def get_trigger(trigger_id: int) -> dict[str, Any]:
        """Get details of a specific trigger configuration."""
        return await make_request("GET", f"/triggers/{trigger_id}")


def register_attachments_tools(mcp: FastMCP) -> None:
    """
    Register attachment management tools.

    Attachments include resumes, cover letters, and other documents.
    Special feature: parse_resume endpoint for AI-powered resume parsing.
    """

    @mcp.tool()
    async def get_attachment(attachment_id: int) -> dict[str, Any]:
        """Get metadata for a specific attachment. Returns attachment details but not the file content itself. Use download_attachment() to retrieve the actual file."""
        return await make_request("GET", f"/attachments/{attachment_id}")

    @mcp.tool()
    async def delete_attachment(attachment_id: int) -> dict[str, Any]:
        """Delete an attachment. This permanently removes the attachment file and its metadata. The file cannot be recovered after deletion."""
        return await make_request("DELETE", f"/attachments/{attachment_id}")

    @mcp.tool()
    async def download_attachment(attachment_id: int) -> dict[str, Any]:
        """Download an attachment file. Returns a pre-signed URL for downloading the file. The URL is temporary and expires after 1 hour."""
        return await make_request("GET", f"/attachments/{attachment_id}/download")

    @mcp.tool()
    async def parse_resume(file_content: str, filename: str) -> dict[str, Any]:
        """Parse a resume using AI to extract structured data. This is a special endpoint that parses resume content without creating a candidate record. The extracted data can be used to pre-fill candidate creation forms or validate resume quality."""
        payload = {
            "file": file_content,
            "filename": filename
        }
        return await make_request("POST", "/attachments/parse", json_data=payload)


def register_backups_tools(mcp: FastMCP) -> None:
    """
    Register backup management tools.

    Backups create snapshots of your CATS data for disaster recovery.
    Options: include_attachments, include_emails
    Statuses: pending, processing, completed, expired
    """

    @mcp.tool()
    async def list_backups(
        per_page: int = 25,
        page: int = 1,
        status: Optional[str] = None
    ) -> dict[str, Any]:
        """
        List all system backups.

        Wraps: GET /backups

        Args:
            per_page: Number of results per page (default: 25, max: 100)
            page: Page number to retrieve (default: 1)
            status: Filter by status (pending, processing, completed, expired)

        Returns:
            dict containing:
            - backups: List of backup objects
            - total_count: Total number of backups
            - page: Current page
            - per_page: Results per page

        Backup object contains:
        - id: Backup ID
        - status: pending, processing, completed, or expired
        - type: full or incremental
        - size: Backup size in bytes
        - includes_attachments: Whether attachments are included
        - includes_emails: Whether emails are included
        - created_at: Backup creation timestamp
        - completed_at: Backup completion timestamp
        - expires_at: Expiration timestamp (backups expire after 90 days)
        - download_url: Pre-signed download URL (for completed backups)

        Example:
            >>> backups = await list_backups(status="completed")
            >>> latest = backups["backups"][0]
            >>> print(f"Latest backup: {latest['created_at']}")
            >>> print(f"Size: {latest['size'] / 1024 / 1024:.2f} MB")
        """
        params = {"per_page": per_page, "page": page}
        if status:
            params["status"] = status
        return await make_request("GET", "/backups", params=params)

    @mcp.tool()
    async def get_backup(backup_id: int) -> dict[str, Any]:
        """Get details of a specific backup."""
        return await make_request("GET", f"/backups/{backup_id}")

    @mcp.tool()
    async def create_backup(
        include_attachments: bool = True,
        include_emails: bool = True,
        description: Optional[str] = None
    ) -> dict[str, Any]:
        """
        Create a new system backup.

        Creates a full backup of all CATS data. The backup process is asynchronous
        and may take several minutes to hours depending on data volume.

        Backups expire after 90 days and are automatically deleted.

        Wraps: POST /backups

        Args:
            include_attachments: Include attachment files (default: True)
            include_emails: Include email history (default: True)
            description: Optional description for the backup

        Returns:
            dict containing:
            - id: New backup ID
            - status: "pending" (backup is queued for processing)
            - type: "full"
            - includes_attachments: Whether attachments are included
            - includes_emails: Whether emails are included
            - created_at: Creation timestamp
            - expires_at: Expiration timestamp (90 days from now)
            - created_by: User who created the backup

        Note: Monitor backup progress using get_backup() to check status.
        The backup status will change: pending -> processing -> completed

        Example:
            >>> backup = await create_backup(
            ...     include_attachments=True,
            ...     include_emails=False,
            ...     description="Pre-migration backup"
            ... )
            >>> print(f"Backup created with ID: {backup['id']}")
            >>> print(f"Status: {backup['status']}")
            >>> # Poll get_backup() to monitor progress
            >>> import asyncio
            >>> while True:
            ...     status = await get_backup(backup['id'])
            ...     if status['status'] == 'completed':
            ...         print(f"Backup complete! Download: {status['download_url']}")
            ...         break
            ...     await asyncio.sleep(30)  # Check every 30 seconds
        """
        payload = {
            "include_attachments": include_attachments,
            "include_emails": include_emails
        }
        if description:
            payload["description"] = description
        return await make_request("POST", "/backups", json_data=payload)


def register_events_tools(mcp: FastMCP) -> None:
    """
    Register event/calendar management tools.

    Events represent scheduled activities like interviews, meetings, calls.
    Full CRUD operations supported.
    """

    @mcp.tool()
    async def list_events(
        per_page: int = 25,
        page: int = 1,
        candidate_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> dict[str, Any]:
        """
        List all calendar events.

        Wraps: GET /events

        Args:
            per_page: Number of results per page (default: 25, max: 100)
            page: Page number to retrieve (default: 1)
            candidate_id: Filter by candidate ID
            start_date: Filter events starting after this date (ISO 8601)
            end_date: Filter events ending before this date (ISO 8601)

        Returns:
            dict containing:
            - events: List of event objects
            - total_count: Total number of events
            - page: Current page
            - per_page: Results per page

        Event object contains:
        - id: Event ID
        - title: Event title
        - description: Event description
        - type: interview, meeting, call, other
        - start_time: Event start timestamp (ISO 8601)
        - end_time: Event end timestamp (ISO 8601)
        - location: Physical or virtual location
        - candidate_id: Associated candidate ID (if applicable)
        - job_id: Associated job ID (if applicable)
        - attendees: List of attendee user IDs
        - created_by: User who created the event
        - created_at: Creation timestamp

        Example:
            >>> # Get upcoming events for next week
            >>> from datetime import datetime, timedelta
            >>> start = datetime.now().isoformat()
            >>> end = (datetime.now() + timedelta(days=7)).isoformat()
            >>> events = await list_events(start_date=start, end_date=end)
            >>> print(f"Upcoming events: {events['total_count']}")
            >>> for evt in events["events"]:
            ...     print(f"{evt['start_time']}: {evt['title']}")
        """
        params = {"per_page": per_page, "page": page}
        if candidate_id:
            params["candidate_id"] = candidate_id
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return await make_request("GET", "/events", params=params)

    @mcp.tool()
    async def get_event(event_id: int) -> dict[str, Any]:
        """Get details of a specific event."""
        return await make_request("GET", f"/events/{event_id}")

    @mcp.tool()
    async def create_event(
        title: str,
        start_time: str,
        end_time: str,
        event_type: str = "meeting",
        description: Optional[str] = None,
        location: Optional[str] = None,
        meeting_url: Optional[str] = None,
        candidate_id: Optional[int] = None,
        job_id: Optional[int] = None,
        attendee_ids: Optional[list[int]] = None
    ) -> dict[str, Any]:
        """
        Create a new calendar event.

        Wraps: POST /events

        Args:
            title: Event title
            start_time: Event start timestamp (ISO 8601, e.g., "2025-11-01T14:00:00Z")
            end_time: Event end timestamp (ISO 8601)
            event_type: Type of event (interview, meeting, call, other)
            description: Event description
            location: Physical location or address
            meeting_url: Virtual meeting URL (e.g., Zoom, Google Meet)
            candidate_id: Associated candidate ID
            job_id: Associated job ID
            attendee_ids: List of user IDs to invite

        Returns:
            dict containing:
            - id: New event ID
            - title: Event title
            - type: Event type
            - start_time: Start timestamp
            - end_time: End timestamp
            - location: Location
            - attendees: List of attendee objects
            - created_at: Creation timestamp
            - calendar_invite_sent: Whether calendar invites were sent

        Example:
            >>> event = await create_event(
            ...     title="Technical Interview - Jane Doe",
            ...     start_time="2025-11-01T14:00:00Z",
            ...     end_time="2025-11-01T15:00:00Z",
            ...     event_type="interview",
            ...     meeting_url="https://zoom.us/j/123456789",
            ...     candidate_id=407373086,
            ...     job_id=16456911,
            ...     attendee_ids=[80808, 80809]
            ... )
            >>> print(f"Event created with ID: {event['id']}")
            >>> print(f"Calendar invites sent: {event['calendar_invite_sent']}")
        """
        payload = {
            "title": title,
            "start_time": start_time,
            "end_time": end_time,
            "type": event_type
        }
        if description:
            payload["description"] = description
        if location:
            payload["location"] = location
        if meeting_url:
            payload["meeting_url"] = meeting_url
        if candidate_id:
            payload["candidate_id"] = candidate_id
        if job_id:
            payload["job_id"] = job_id
        if attendee_ids:
            payload["attendee_ids"] = attendee_ids
        return await make_request("POST", "/events", json_data=payload)

    @mcp.tool()
    async def update_event(
        event_id: int,
        title: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        meeting_url: Optional[str] = None,
        attendee_ids: Optional[list[int]] = None
    ) -> dict[str, Any]:
        """
        Update an existing calendar event.

        Only provided fields will be updated. Omitted fields remain unchanged.

        Wraps: PUT /events/{id}

        Args:
            event_id: The unique identifier of the event to update
            title: Updated event title
            start_time: Updated start timestamp (ISO 8601)
            end_time: Updated end timestamp (ISO 8601)
            description: Updated description
            location: Updated location
            meeting_url: Updated virtual meeting URL
            attendee_ids: Updated list of attendee user IDs

        Returns:
            dict containing:
            - id: Event ID
            - title: Updated title
            - start_time: Updated start time
            - end_time: Updated end time
            - updated_at: Update timestamp
            - calendar_update_sent: Whether update notifications were sent

        Example:
            >>> updated = await update_event(
            ...     event_id=60606,
            ...     start_time="2025-11-01T15:00:00Z",  # Reschedule
            ...     end_time="2025-11-01T16:00:00Z",
            ...     location="Conference Room B"
            ... )
            >>> print(f"Event updated: {updated['title']}")
            >>> print(f"New time: {updated['start_time']}")
        """
        payload = {}
        if title is not None:
            payload["title"] = title
        if start_time is not None:
            payload["start_time"] = start_time
        if end_time is not None:
            payload["end_time"] = end_time
        if description is not None:
            payload["description"] = description
        if location is not None:
            payload["location"] = location
        if meeting_url is not None:
            payload["meeting_url"] = meeting_url
        if attendee_ids is not None:
            payload["attendee_ids"] = attendee_ids
        return await make_request("PUT", f"/events/{event_id}", json_data=payload)

    @mcp.tool()
    async def delete_event(event_id: int) -> dict[str, Any]:
        """Delete a calendar event. This permanently removes the event. Attendees will receive cancellation notifications."""
        return await make_request("DELETE", f"/events/{event_id}")


# Export all registration functions
__all__ = [
    "register_tags_tools",
    "register_webhooks_tools",
    "register_users_tools",
    "register_triggers_tools",
    "register_attachments_tools",
    "register_backups_tools",
    "register_events_tools",
]
