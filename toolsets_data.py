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
7. Events - 1 tool

Total: 18 tools across 7 toolsets
"""

from typing import Any, Callable, Optional
from fastmcp import FastMCP


def register_tags_tools(mcp: FastMCP, make_request: Callable = None) -> None:
    """
    Register tag management tools.

    Tags are labels that can be attached to candidates, jobs, and other entities.
    Tags are managed at the entity level (e.g., /candidates/{id}/tags).
    This toolset provides read access to the global tags list.
    """

    @mcp.tool()
    async def list_tags(per_page: int = 25, page: int = 1) -> dict[str, Any]:
        """
        List all tags in the system.

        Tags are labels used to categorize candidates, jobs, and other entities.
        To attach/detach tags, use the entity-specific endpoints:
        - POST /candidates/{id}/tags
        - DELETE /candidates/{id}/tags
        - POST /jobs/{id}/tags
        - DELETE /jobs/{id}/tags

        Wraps: GET /tags

        Args:
            per_page: Number of results per page (default: 25, max: 100)
            page: Page number to retrieve (default: 1)

        Returns:
            dict containing:
            - tags: List of tag objects with id, name, color
            - total_count: Total number of tags
            - page: Current page number
            - per_page: Results per page

        Example:
            >>> tags = await list_tags(per_page=50)
            >>> print(tags["tags"][0])
            {"id": 101, "name": "Python Developer", "color": "#3498db"}
        """
        return await make_request("GET", "/tags", params={"per_page": per_page, "page": page})

    @mcp.tool()
    async def get_tag(tag_id: int | str) -> dict[str, Any]:
        """
        Get details of a specific tag.

        Wraps: GET /tags/{id}

        Args:
            tag_id: The unique identifier of the tag

        Returns:
            dict containing:
            - id: Tag ID
            - name: Tag name
            - color: Tag color (hex code)
            - usage_count: Number of entities using this tag
            - created_at: Tag creation timestamp

        Example:
            >>> tag = await get_tag(101)
            >>> print(f"{tag['name']}: Used {tag['usage_count']} times")
        """
        return await make_request("GET", f"/tags/{tag_id}")


def register_webhooks_tools(mcp: FastMCP, make_request: Callable = None) -> None:
    """
    Register webhook management tools.

    Webhooks allow real-time notifications when events occur in CATS.
    Supports 24+ event types across candidates, jobs, applications, etc.

    Security: Webhooks include HMAC-SHA256 signatures for verification.
    """

    @mcp.tool()
    async def list_webhooks(per_page: int = 25, page: int = 1) -> dict[str, Any]:
        """
        List all configured webhooks.

        Wraps: GET /webhooks

        Available webhook events (24 types):
        - candidate.created, candidate.updated, candidate.deleted
        - job.created, job.updated, job.deleted, job.status_changed
        - application.created, application.updated, application.status_changed
        - pipeline.status_changed
        - activity.created, activity.updated
        - task.created, task.completed
        - interview.scheduled, interview.completed, interview.cancelled
        - attachment.uploaded, attachment.deleted
        - email.sent, email.received
        - tag.attached, tag.detached
        - trigger.fired

        Args:
            per_page: Number of results per page (default: 25, max: 100)
            page: Page number to retrieve (default: 1)

        Returns:
            dict containing:
            - webhooks: List of webhook configurations
            - total_count: Total number of webhooks
            - page: Current page
            - per_page: Results per page

        Example:
            >>> webhooks = await list_webhooks()
            >>> for wh in webhooks["webhooks"]:
            ...     print(f"{wh['url']}: {len(wh['events'])} events")
        """
        return await make_request("GET", "/webhooks", params={"per_page": per_page, "page": page})

    @mcp.tool()
    async def get_webhook(webhook_id: int | str) -> dict[str, Any]:
        """
        Get details of a specific webhook configuration.

        Wraps: GET /webhooks/{id}

        Args:
            webhook_id: The unique identifier of the webhook

        Returns:
            dict containing:
            - id: Webhook ID
            - url: Target URL for webhook POSTs
            - events: List of subscribed event types
            - active: Whether webhook is enabled
            - hmac_key: Signing key for HMAC signature verification
            - created_at: Creation timestamp
            - last_triggered: Last successful trigger timestamp
            - failure_count: Number of consecutive failures

        Security Note:
            Verify webhook signatures using HMAC-SHA256:
            signature = HMAC-SHA256(signing_key, request_body)
            Compare with X-CATS-Signature header

        Example:
            >>> webhook = await get_webhook(18128)
            >>> print(f"Status: {'Active' if webhook['active'] else 'Inactive'}")
            >>> print(f"Events: {', '.join(webhook['events'])}")
        """
        return await make_request("GET", f"/webhooks/{webhook_id}")

    @mcp.tool()
    async def create_webhook(
        target_url: str,
        events: list[str],
        signing_key: str
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
            target_url: Target URL for webhook POST requests (must be HTTPS in production)
            events: List of event types to subscribe to.
                    Available: candidate.created, candidate.updated, candidate.deleted,
                    job.created, job.updated, job.deleted, job.status_changed,
                    pipeline.created, pipeline.deleted, pipeline.status_changed,
                    contact.created, contact.updated, contact.deleted,
                    company.created, company.updated, company.deleted,
                    activity.created, activity.updated, activity.deleted,
                    user.created, user.updated, user.deleted
            signing_key: HMAC-SHA256 key for webhook signature verification

        Returns:
            dict: Created webhook object with ID
        """
        payload = {
            "target_url": target_url,
            "events": events,
        }
        payload["secret"] = signing_key  # noqa: S105
        return await make_request("POST", "/webhooks", json_data=payload)

    @mcp.tool()
    async def delete_webhook(webhook_id: int | str) -> dict[str, Any]:
        """
        Delete a webhook subscription.

        This permanently removes the webhook configuration. All pending deliveries
        will be cancelled.

        Wraps: DELETE /webhooks/{id}

        Args:
            webhook_id: The unique identifier of the webhook to delete

        Returns:
            dict containing:
            - success: True if deletion succeeded
            - message: Confirmation message

        Example:
            >>> result = await delete_webhook(18128)
            >>> print(result["message"])
            "Webhook successfully deleted"
        """
        return await make_request("DELETE", f"/webhooks/{webhook_id}")


def register_users_tools(mcp: FastMCP, make_request: Callable = None) -> None:
    """
    Register user management tools.

    Users are team members with access to the CATS system.
    Access levels: read_only, edit, admin
    """

    @mcp.tool()
    async def list_users(per_page: int = 25, page: int = 1) -> dict[str, Any]:
        """
        List all users in the organization.

        Wraps: GET /users

        Args:
            per_page: Number of results per page (default: 25, max: 100)
            page: Page number to retrieve (default: 1)

        Returns:
            dict containing:
            - users: List of user objects
            - total_count: Total number of users
            - page: Current page
            - per_page: Results per page

        User object contains:
        - id: User ID
        - first_name: First name
        - last_name: Last name
        - email: Email address
        - access_level: read_only, edit, or admin
        - active: Whether user account is active
        - last_login: Last login timestamp
        - created_at: Account creation timestamp

        Example:
            >>> users = await list_users()
            >>> admins = [u for u in users["users"] if u["access_level"] == "admin"]
            >>> print(f"Found {len(admins)} admin users")
        """
        return await make_request("GET", "/users", params={"per_page": per_page, "page": page})

    @mcp.tool()
    async def get_user(user_id: int | str) -> dict[str, Any]:
        """
        Get details of a specific user.

        Wraps: GET /users/{id}

        Args:
            user_id: The unique identifier of the user

        Returns:
            dict containing:
            - id: User ID
            - first_name: First name
            - last_name: Last name
            - email: Email address
            - access_level: read_only, edit, or admin
            - active: Whether user account is active
            - department: User's department
            - title: Job title
            - phone: Phone number
            - permissions: List of specific permissions
            - last_login: Last login timestamp
            - created_at: Account creation timestamp

        Example:
            >>> user = await get_user(80808)
            >>> print(f"{user['first_name']} {user['last_name']} ({user['access_level']})")
            >>> print(f"Last login: {user['last_login']}")
        """
        return await make_request("GET", f"/users/{user_id}")


def register_triggers_tools(mcp: FastMCP, make_request: Callable = None) -> None:
    """
    Register trigger management tools (read-only).

    Triggers are automated actions that fire when pipeline statuses change.
    They are configured through the CATS UI and cannot be created via API.
    This toolset provides read access for monitoring trigger configurations.
    """

    @mcp.tool()
    async def list_triggers(per_page: int = 25, page: int = 1) -> dict[str, Any]:
        """
        List all configured triggers.

        Triggers automatically fire actions when candidates move through pipeline
        stages. Common uses:
        - Send automated emails when status changes
        - Create tasks for recruiters
        - Update custom fields
        - Fire webhooks

        Note: Triggers are configured in CATS UI, not via API.

        Wraps: GET /triggers

        Args:
            per_page: Number of results per page (default: 25, max: 100)
            page: Page number to retrieve (default: 1)

        Returns:
            dict containing:
            - triggers: List of trigger configurations
            - total_count: Total number of triggers
            - page: Current page
            - per_page: Results per page

        Trigger object contains:
        - id: Trigger ID
        - name: Trigger name
        - event: Event type (e.g., "pipeline.status_changed")
        - conditions: Trigger conditions (status changes, field values)
        - actions: Actions to perform (send_email, create_task, etc.)
        - active: Whether trigger is enabled
        - fire_count: Number of times trigger has fired
        - created_at: Creation timestamp

        Example:
            >>> triggers = await list_triggers()
            >>> for t in triggers["triggers"]:
            ...     print(f"{t['name']}: Fired {t['fire_count']} times")
        """
        return await make_request("GET", "/triggers", params={"per_page": per_page, "page": page})

    @mcp.tool()
    async def get_trigger(trigger_id: int | str) -> dict[str, Any]:
        """
        Get details of a specific trigger configuration.

        Wraps: GET /triggers/{id}

        Args:
            trigger_id: The unique identifier of the trigger

        Returns:
            dict containing:
            - id: Trigger ID
            - name: Trigger name
            - description: Trigger description
            - event: Event type that fires the trigger
            - conditions: Detailed trigger conditions
            - actions: Actions performed when triggered
            - active: Whether trigger is enabled
            - fire_count: Total number of times fired
            - last_fired: Last fire timestamp
            - created_at: Creation timestamp
            - created_by: User who created the trigger

        Example:
            >>> trigger = await get_trigger(70707)
            >>> print(f"Trigger: {trigger['name']}")
            >>> print(f"Fires on: {trigger['event']}")
            >>> print(f"Actions: {len(trigger['actions'])} configured")
        """
        return await make_request("GET", f"/triggers/{trigger_id}")


def register_attachments_tools(mcp: FastMCP, make_request: Callable = None) -> None:
    """
    Register attachment management tools.

    Attachments include resumes, cover letters, and other documents.
    Special feature: parse_resume endpoint for AI-powered resume parsing.
    """

    @mcp.tool()
    async def get_attachment(attachment_id: int | str) -> dict[str, Any]:
        """
        Get metadata for a specific attachment.

        Returns attachment details but not the file content itself.
        Use download_attachment() to retrieve the actual file.

        Wraps: GET /attachments/{id}

        Args:
            attachment_id: The unique identifier of the attachment

        Returns:
            dict containing:
            - id: Attachment ID
            - filename: Original filename
            - content_type: MIME type (e.g., "application/pdf")
            - size: File size in bytes
            - entity_type: Type of entity (candidate, job, etc.)
            - entity_id: ID of the entity this is attached to
            - uploaded_by: User ID who uploaded
            - uploaded_at: Upload timestamp
            - download_url: URL to download the file

        Example:
            >>> attachment = await get_attachment(44444)
            >>> print(f"File: {attachment['filename']} ({attachment['size']} bytes)")
            >>> print(f"Type: {attachment['content_type']}")
        """
        return await make_request("GET", f"/attachments/{attachment_id}")

    @mcp.tool()
    async def delete_attachment(attachment_id: int | str) -> dict[str, Any]:
        """
        Delete an attachment.

        This permanently removes the attachment file and its metadata.
        The file cannot be recovered after deletion.

        Wraps: DELETE /attachments/{id}

        Args:
            attachment_id: The unique identifier of the attachment to delete

        Returns:
            dict containing:
            - success: True if deletion succeeded
            - message: Confirmation message

        Example:
            >>> result = await delete_attachment(44444)
            >>> print(result["message"])
            "Attachment successfully deleted"
        """
        return await make_request("DELETE", f"/attachments/{attachment_id}")

    @mcp.tool()
    async def download_attachment(attachment_id: int | str) -> dict[str, Any]:
        """
        Download an attachment file.

        Returns a pre-signed URL for downloading the file. The URL is temporary
        and expires after 1 hour.

        Wraps: GET /attachments/{id}/download

        Args:
            attachment_id: The unique identifier of the attachment

        Returns:
            dict containing:
            - download_url: Pre-signed URL for downloading (expires in 1 hour)
            - filename: Original filename
            - content_type: MIME type
            - size: File size in bytes
            - expires_at: URL expiration timestamp

        Example:
            >>> download = await download_attachment(44444)
            >>> print(f"Download URL: {download['download_url']}")
            >>> print(f"Expires: {download['expires_at']}")
            >>> # Use the URL to download the file within 1 hour
        """
        return await make_request("GET", f"/attachments/{attachment_id}/download")

    @mcp.tool()
    async def parse_resume(file_content: str, filename: str) -> dict[str, Any]:
        """
        Parse a resume using AI to extract structured data.

        This is a special endpoint that parses resume content without creating
        a candidate record. The extracted data can be used to pre-fill candidate
        creation forms or validate resume quality.

        Wraps: POST /attachments/parse

        Supported formats: PDF, DOC, DOCX, TXT, RTF

        Args:
            file_content: Base64-encoded file content
            filename: Original filename with extension

        Returns:
            dict containing extracted data:
            - name: Full name
            - first_name: First name
            - last_name: Last name
            - email: Email address(es)
            - phone: Phone number(s)
            - address: Physical address
            - summary: Professional summary
            - experience: List of work history entries
              - company: Company name
              - title: Job title
              - start_date: Start date
              - end_date: End date (or "Present")
              - description: Job description
            - education: List of education entries
              - institution: School name
              - degree: Degree earned
              - field: Field of study
              - graduation_date: Graduation date
            - skills: List of skills
            - certifications: List of certifications
            - languages: List of languages
            - linkedin_url: LinkedIn profile URL
            - confidence_score: Overall parsing confidence (0-100)

        Note: This endpoint does NOT create a candidate record or attachment.
        It only parses and returns the extracted data. Use create_candidate()
        separately if you want to create a candidate record.

        Example:
            >>> import base64
            >>> with open("resume.pdf", "rb") as f:
            ...     content = base64.b64encode(f.read()).decode()
            >>> parsed = await parse_resume(content, "resume.pdf")
            >>> print(f"Candidate: {parsed['name']}")
            >>> print(f"Email: {parsed['email']}")
            >>> print(f"Experience: {len(parsed['experience'])} jobs")
            >>> print(f"Confidence: {parsed['confidence_score']}%")
        """
        payload = {
            "file": file_content,
            "filename": filename
        }
        return await make_request("POST", "/attachments/parse", json_data=payload)


def register_backups_tools(mcp: FastMCP, make_request: Callable = None) -> None:
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
    async def get_backup(backup_id: int | str) -> dict[str, Any]:
        """
        Get details of a specific backup.

        Wraps: GET /backups/{id}

        Args:
            backup_id: The unique identifier of the backup

        Returns:
            dict containing:
            - id: Backup ID
            - status: pending, processing, completed, or expired
            - type: full or incremental
            - size: Backup size in bytes
            - includes_attachments: Whether attachments are included
            - includes_emails: Whether emails are included
            - record_counts: Breakdown of records by type
              - candidates: Number of candidates
              - jobs: Number of jobs
              - applications: Number of applications
              - activities: Number of activities
              - etc.
            - created_at: Backup creation timestamp
            - started_at: Processing start timestamp
            - completed_at: Completion timestamp
            - expires_at: Expiration timestamp
            - download_url: Pre-signed download URL (if completed)
            - created_by: User who created the backup

        Example:
            >>> backup = await get_backup(10001)
            >>> print(f"Status: {backup['status']}")
            >>> if backup['status'] == 'completed':
            ...     print(f"Download: {backup['download_url']}")
            ...     print(f"Records: {backup['record_counts']['candidates']} candidates")
        """
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


def register_events_tools(mcp: FastMCP, make_request: Callable = None) -> None:
    """
    Register system event stream tools.

    Events represent a chronological stream of system changes (audit log).
    Use to poll for changes instead of repeatedly fetching all records.
    Event types include: candidate.created, candidate.status_changed,
    job.created, pipeline.status_changed, etc.
    """

    @mcp.tool()
    async def list_events(
        starting_after_id: Optional[int] = None,
        starting_after_timestamp: Optional[str] = None
    ) -> dict[str, Any]:
        """
        List system events (audit log stream).

        Returns a chronological stream of system changes. Use either
        starting_after_id or starting_after_timestamp to paginate through
        the event stream. Events are returned in chronological order.

        Wraps: GET /events

        Event types include:
        - candidate.created, candidate.updated, candidate.deleted
        - candidate.status_changed
        - job.created, job.updated, job.deleted, job.status_changed
        - pipeline.created, pipeline.deleted, pipeline.status_changed
        - company.created, company.updated, company.deleted
        - contact.created, contact.updated, contact.deleted
        - activity.created, activity.updated, activity.deleted
        - user.created, user.updated, user.deleted

        Args:
            starting_after_id: Return events after this event ID (for cursor-based pagination)
            starting_after_timestamp: Return events after this timestamp (ISO 8601 / RFC 3339)

        Returns:
            dict: Stream of system events with event type, entity info, and timestamps
        """
        params = {}
        if starting_after_id is not None:
            params["starting_after_id"] = starting_after_id
        if starting_after_timestamp is not None:
            params["starting_after_timestamp"] = starting_after_timestamp
        return await make_request("GET", "/events", params=params)


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
