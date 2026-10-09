"""MCP tool definitions and the HTTP app that serves them."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.types import ASGIApp

from .auth import USER_ID_STATE_KEY, BearerAuthMiddleware
from .models import Verdict, Wine
from .photos import MAX_PHOTO_BYTES, UPLOAD_URL_SECONDS, InvalidPhotoError, Upload, photo_added_at, photo_id_of
from .service import PhotoNotFoundError, PhotosDisabledError, WineNotFoundError, WineService

INSTRUCTIONS = """\
Remembers which wines the user has tried and what they thought of them.
Before creating a wine, use search_wines to check whether it is already recorded; if it is, update it instead.
"""

WineId = Annotated[str, Field(description="wine_id returned by another tool")]
NAME_DESCRIPTION = "What the label says, e.g. 'Ridge Monte Bello Cabernet'"
VINTAGE_DESCRIPTION = "Year on the label; omit for non-vintage"
VARIETAL_DESCRIPTION = "Grape or blend, e.g. 'Cabernet Sauvignon' or 'Grenache blend'"
VERDICT_DESCRIPTION = "How the person feels about it"
PhotoRef = Annotated[
    str | None, Field(description="photo_ref from upload_search_photo, after uploading the photo to its upload_url")
]
# Mistakes the agent can fix (unknown wine, bad or missing upload, photos off) come back as tool errors.
AGENT_ERRORS = (WineNotFoundError, PhotoNotFoundError, InvalidPhotoError, PhotosDisabledError)


def current_user(ctx: Context) -> str:
    """The user this request acts for, as resolved from its bearer token by BearerAuthMiddleware."""
    request = ctx.request_context.request
    user_id = request.scope.get("state", {}).get(USER_ID_STATE_KEY) if request is not None else None
    if not user_id:
        # The middleware rejects unauthenticated requests, so this means the app was wired wrong.
        raise ToolError("No authenticated user for this request")
    return user_id


def _summary(wine: Wine) -> dict[str, Any]:
    data = wine.model_dump(mode="json", exclude={"user_id", "photo_keys"})
    data["photos"] = [{"photo_id": photo_id_of(k), "added_at": photo_added_at(k).isoformat()} for k in wine.photo_keys]
    data["label"] = wine.label
    return data


def build_server(service: WineService) -> MCPServer:
    mcp = MCPServer(name="wine-cellar", instructions=INSTRUCTIONS)

    @mcp.tool()
    def create_wine(
        ctx: Context,
        name: Annotated[str, Field(description=NAME_DESCRIPTION)],
        vintage: Annotated[int | None, Field(description=VINTAGE_DESCRIPTION)] = None,
        varietal: Annotated[str, Field(description=VARIETAL_DESCRIPTION)] = "",
        verdict: Annotated[Verdict | None, Field(description=VERDICT_DESCRIPTION)] = None,
        notes: Annotated[str, Field(description="What the person said about it")] = "",
    ) -> dict[str, Any]:
        """Record a wine the person tried and what they thought of it.

        Read the name, vintage and varietal off the label yourself; only the name is required. To keep the label
        photo, call add_photo next.
        """
        wine = service.create_wine(
            current_user(ctx), name=name, vintage=vintage, varietal=varietal, verdict=verdict, notes=notes
        )
        return {"wine": _summary(wine)}

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
    def update_wine(
        ctx: Context,
        wine_id: WineId,
        name: Annotated[str | None, Field(description=NAME_DESCRIPTION)] = None,
        vintage: Annotated[int | None, Field(description=VINTAGE_DESCRIPTION)] = None,
        varietal: Annotated[str | None, Field(description=VARIETAL_DESCRIPTION)] = None,
        verdict: Annotated[Verdict | None, Field(description=VERDICT_DESCRIPTION)] = None,
        notes: Annotated[
            str | None,
            Field(
                description="Replaces the notes. To add to them, send the existing notes with the new part "
                "appended; only rewrite them if the person asks."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Change a recorded wine, e.g. the person changed their mind or added a note. Omitted fields are kept."""
        try:
            wine = service.update_wine(
                current_user(ctx),
                wine_id,
                name=name,
                vintage=vintage,
                varietal=varietal,
                verdict=verdict,
                notes=notes,
            )
        except WineNotFoundError as e:
            raise ToolError(str(e)) from e
        return {"wine": _summary(wine)}

    def _upload(upload: Upload) -> dict[str, Any]:
        return {
            "upload_url": upload.upload_url,
            "method": "PUT",
            "max_bytes": MAX_PHOTO_BYTES,
            "expires_in_seconds": UPLOAD_URL_SECONDS,
        }

    @mcp.tool()
    def add_photo(ctx: Context, wine_id: WineId) -> dict[str, Any]:
        """Add a label photo to a wine: returns a link, and the photo is saved to the wine once uploaded to it.

        PUT the image file to upload_url (for example `curl -sSf --upload-file label.jpg "<upload_url>"`). JPEG, PNG,
        WebP or GIF up to max_bytes; anything else is discarded. The photo shows in the wine's photos within a few
        seconds.
        """
        try:
            return _upload(service.add_photo(current_user(ctx), wine_id))
        except AGENT_ERRORS as e:
            raise ToolError(str(e)) from e

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    def delete_photo(
        ctx: Context,
        wine_id: WineId,
        photo_id: Annotated[str, Field(description="photo_id from the wine's photos")],
    ) -> dict[str, Any]:
        """Remove a photo from a wine, e.g. one added by mistake. The latest photo has the latest added_at."""
        try:
            wine = service.delete_photo(current_user(ctx), wine_id, photo_id)
        except AGENT_ERRORS as e:
            raise ToolError(str(e)) from e
        return {"wine": _summary(wine)}

    @mcp.tool()
    def upload_search_photo(ctx: Context) -> dict[str, Any]:
        """Get a link for uploading a photo to search with. The photo is used for one search and never saved.

        PUT the image file to upload_url (for example `curl -sSf --upload-file label.jpg "<upload_url>"`), then pass
        photo_ref to search_wines. JPEG, PNG, WebP or GIF up to max_bytes.
        """
        try:
            upload = service.upload_search_photo(current_user(ctx))
        except PhotosDisabledError as e:
            raise ToolError(str(e)) from e
        return {"photo_ref": upload.photo_ref, **_upload(upload)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def search_wines(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                description="Free text, matched by name and by meaning: a wine's name, "
                "or a description like 'that bold red I loved'. With a photo, also pass what the label says."
            ),
        ] = "",
        photo_ref: PhotoRef = None,
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> dict[str, Any]:
        """Find wines the person has recorded, best match first, with their verdicts and notes.

        Give text, a label photo, or both. Text matches names (typos and partial names are fine) and meaning, using
        the person's notes too; a photo matches the label photos they saved. For "do I like this wine?" from a photo,
        upload it (see upload_search_photo) and pass its photo_ref plus the name and vintage you read off the label.
        The closest wines come back even when none is a real match, so check them before relying on one.
        """
        if not query.strip() and not photo_ref:
            raise ToolError("Give a query, a photo_ref, or both")
        try:
            matches = service.search_wines(current_user(ctx), query, photo_ref=photo_ref, limit=limit)
        except AGENT_ERRORS as e:
            raise ToolError(str(e)) from e
        return {"matches": [_summary(w) for w in matches]}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def list_wines(
        ctx: Context,
        verdict: Verdict | None = None,
        varietal: Annotated[str | None, Field(description="Varietal contains this text")] = None,
        created_after: Annotated[datetime | None, Field(description="First recorded at or after (UTC)")] = None,
        created_before: Annotated[datetime | None, Field(description="First recorded at or before (UTC)")] = None,
        updated_after: Annotated[datetime | None, Field(description="Last changed at or after (UTC)")] = None,
        updated_before: Annotated[datetime | None, Field(description="Last changed at or before (UTC)")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """List recorded wines, most recently updated first, optionally filtered.

        For "the red I tried for the first time yesterday", filter on created_after/created_before.
        """
        wines = service.list_wines(
            current_user(ctx),
            verdict=verdict,
            varietal=varietal,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
        )
        return {"total": len(wines), "wines": [_summary(w) for w in wines[:limit]]}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_wine(ctx: Context, wine_id: WineId) -> dict[str, Any]:
        """Fetch one wine."""
        try:
            return {"wine": _summary(service.get_wine(current_user(ctx), wine_id))}
        except WineNotFoundError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    def delete_wine(ctx: Context, wine_id: WineId) -> dict[str, Any]:
        """Permanently remove a wine. Only use when the person explicitly asks."""
        try:
            service.delete_wine(current_user(ctx), wine_id)
        except WineNotFoundError as e:
            raise ToolError(str(e)) from e
        return {"deleted": wine_id}

    return mcp


def build_app(
    service: WineService, resolve_user: Callable[[str], str | None], allowed_hosts: list[str]
) -> tuple[ASGIApp, StreamableHTTPSessionManager]:
    """Stateless, JSON-response Streamable HTTP app at /mcp behind a bearer-token check.

    Returns the app and its session manager; the caller must keep `session_manager.run()` open while serving.
    """
    security = (
        TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts)
        if allowed_hosts
        else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    server = build_server(service)
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=security,
        max_request_body_size=1024 * 1024,
    )
    return BearerAuthMiddleware(app, resolve_user), server.session_manager
