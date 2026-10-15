"""MCP tool definitions and the HTTP app that serves them."""

import functools
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from aws_lambda_powertools import Tracer
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.types import ASGIApp

from .auth import USER_ID_STATE_KEY, BearerAuthMiddleware
from .models import ImageType, InvalidPhotoError, Upload, Verdict, Wine
from .service import (
    InvalidPageTokenError,
    InvalidWineError,
    PhotoNotFoundError,
    PhotosDisabledError,
    WineNotFoundError,
    WineService,
)

INSTRUCTIONS = """\
Remembers wines the person has tried and what they thought of them: a verdict (love, like, meh, dislike), notes and \
label photos.

Recipes:
- "Do I like this?" from a label photo: upload_search_photo, upload the photo to its link, then search_wines with \
photo_ref plus the name you read off the label. The closest wines come back even when none is this wine, \
so check before answering; if it isn't recorded, say so.
- Photo with no readable label (a glass, a blurry or far-off bottle): don't guess the wine. Ask for a clear photo of \
the bottle's label.
- Wine list, menu or shelf with several wines: read each name off it and search_wines with that text (no photo_ref; a \
menu photo won't match label photos). Answer with the ones that really match and their verdicts.
- Add a wine (from a photo or a name): search_wines first. If it's recorded, update_wine; otherwise create_wine with \
what you read off the label. If they sent a label photo, always add_photo and upload it to its link, so the next \
photo lookup finds it.
- Add a note: update_wine with the existing notes plus the new part appended.
- Change one thing (verdict, varietal, a label fix): update_wine with only that field.
- Show a wine's label photos: each photo has a url, a link to the image that works for an hour; get_wine again for a \
fresh one.
- Browse: list_wines filters by verdict, varietal and when a wine was first recorded or last changed; pass next_token \
back for more. For descriptions like "that bold red I loved", use search_wines.
"""

WineId = Annotated[str, Field(description="wine_id returned by another tool")]
NAME_DESCRIPTION = "What the label says, e.g. 'Ridge Monte Bello Cabernet'"
VARIETAL_DESCRIPTION = "Grape or blend, e.g. 'Cabernet Sauvignon' or 'Grenache blend'"
VERDICT_DESCRIPTION = "How the person feels about it"
ImageTypeArg = Annotated[ImageType, Field(description="The photo's type; the upload must be this type")]
PhotoRef = Annotated[str | None, Field(description="photo_ref from upload_search_photo, once the photo is uploaded")]
# Mistakes the agent can fix (unknown wine, no name, bad or missing upload, photos off) come back as tool errors.
AGENT_ERRORS = (
    WineNotFoundError,
    InvalidWineError,
    PhotoNotFoundError,
    InvalidPhotoError,
    PhotosDisabledError,
    InvalidPageTokenError,
)


class _Unset:
    """Default for update_wine fields the agent left out, so they can be told apart from null (empty it)."""


UNSET = _Unset()
# Disabled outside Lambda (tests, the CLI); in Lambda it adds to the trace each invocation already has.
tracer = Tracer()


def _unset() -> Any:
    return UNSET


def _agent_errors[**P, R](tool: Callable[P, R]) -> Callable[P, R]:
    """Turn the domain errors in AGENT_ERRORS into tool errors, so the agent gets a message it can act on."""

    @functools.wraps(tool)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return tool(*args, **kwargs)
        except AGENT_ERRORS as e:
            raise ToolError(str(e)) from e

    return wrapper


def _traced[**P, R](tool: Callable[P, R]) -> Callable[P, R]:
    """Record each call as an X-Ray subsegment named after the tool, with a `tool` annotation to filter traces by."""

    @functools.wraps(tool)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # wraps() copied the tool's name onto the wrapper.
        with tracer.provider.in_subsegment(f"## {wrapper.__name__}") as subsegment:
            subsegment.put_annotation("tool", wrapper.__name__)
            return tool(*args, **kwargs)

    return wrapper


def current_user(ctx: Context) -> str:
    """The user this request acts for, as resolved from its bearer token by BearerAuthMiddleware."""
    request = ctx.request_context.request
    user_id = request.scope.get("state", {}).get(USER_ID_STATE_KEY) if request is not None else None
    if not user_id:
        # The middleware rejects unauthenticated requests, so this means the app was wired wrong.
        raise ToolError("No authenticated user for this request")
    return user_id


def build_server(service: WineService) -> MCPServer:
    mcp = MCPServer(name="wine-cellar", instructions=INSTRUCTIONS)

    def _summary(wine: Wine) -> dict[str, Any]:
        data = wine.model_dump(mode="json", exclude={"user_id", "photo_keys"})
        data["photos"] = [
            {"photo_id": p.photo_id, "added_at": p.added_at.isoformat(), "url": service.photo_url(key)}
            for key, p in zip(wine.photo_keys, wine.photos, strict=True)
        ]
        return data

    @mcp.tool()
    @_traced
    @_agent_errors
    def create_wine(
        ctx: Context,
        name: Annotated[str, Field(description=NAME_DESCRIPTION)],
        varietal: Annotated[str, Field(description=VARIETAL_DESCRIPTION)] = "",
        verdict: Annotated[Verdict | None, Field(description=VERDICT_DESCRIPTION)] = None,
        notes: Annotated[str, Field(description="What the person said about it")] = "",
    ) -> dict[str, Any]:
        """Record a wine the person tried and what they thought of it. Only the name is required."""
        wine = service.create_wine(current_user(ctx), name=name, varietal=varietal, verdict=verdict, notes=notes)
        return {"wine": _summary(wine)}

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
    @_traced
    @_agent_errors
    def update_wine(
        ctx: Context,
        wine_id: WineId,
        name: Annotated[str, Field(default_factory=_unset, description=NAME_DESCRIPTION)],
        varietal: Annotated[str | None, Field(default_factory=_unset, description=VARIETAL_DESCRIPTION)],
        verdict: Annotated[Verdict | None, Field(default_factory=_unset, description=VERDICT_DESCRIPTION)],
        notes: Annotated[
            str | None,
            Field(
                default_factory=_unset,
                description="Replaces the notes",
            ),
        ],
    ) -> dict[str, Any]:
        """Change a recorded wine.

        A partial update: send only the fields that change, and every field left out keeps its value. Send null to
        empty a field, e.g. verdict null when the person is no longer sure. The name can't be emptied.
        """
        fields = {"name": name, "varietal": varietal, "verdict": verdict, "notes": notes}
        wine = service.update_wine(current_user(ctx), wine_id, **{k: v for k, v in fields.items() if v is not UNSET})
        return {"wine": _summary(wine)}

    def _upload(upload: Upload) -> dict[str, Any]:
        return {
            "upload_url": upload.upload_url,
            "fields": upload.fields,
            "max_bytes": upload.max_bytes,
            "expires_in_seconds": upload.expires_in_seconds,
        }

    @mcp.tool()
    @_traced
    @_agent_errors
    def add_photo(ctx: Context, wine_id: WineId, content_type: ImageTypeArg) -> dict[str, Any]:
        """Add a label photo to a wine: returns an upload link, and the photo is saved to the wine once uploaded.

        Upload with an HTTP POST to upload_url as a multipart form: every entry in fields, then the image as the last
        field, named file (for example `curl -sSf -F key=... -F Content-Type=image/jpeg ... -F file=@label.jpg
        "<upload_url>"`). Up to max_bytes. The photo shows in the wine's photos within a few seconds.
        """
        return _upload(service.add_photo(current_user(ctx), wine_id, content_type))

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    @_traced
    @_agent_errors
    def delete_photo(
        ctx: Context,
        wine_id: WineId,
        photo_id: Annotated[str, Field(description="photo_id from the wine's photos")],
    ) -> dict[str, Any]:
        """Remove a photo from a wine."""
        wine = service.delete_photo(current_user(ctx), wine_id, photo_id)
        return {"wine": _summary(wine)}

    @mcp.tool()
    @_traced
    @_agent_errors
    def upload_search_photo(ctx: Context, content_type: ImageTypeArg) -> dict[str, Any]:
        """Get a link for uploading a photo to search with. The photo is used for one search and never saved.

        Upload with an HTTP POST to upload_url as a multipart form: every entry in fields, then the image as the last
        field, named file. Up to max_bytes.
        """
        upload = service.upload_search_photo(current_user(ctx), content_type)
        return {"photo_ref": upload.photo_ref, **_upload(upload)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @_traced
    @_agent_errors
    def search_wines(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                description="Free text, matched by name and by meaning: a wine's name, "
                "or a description like 'that bold red I loved'"
            ),
        ] = "",
        photo_ref: PhotoRef = None,
        limit: Annotated[int, Field(ge=1, le=20, description="Most matches to return")] = 5,
    ) -> dict[str, Any]:
        """Find wines the person has recorded, best match first, with their verdicts and notes.

        Give text, a label photo, or both. Text matches names (typos and partial names are fine) and meaning, using
        the person's notes too; a photo matches the label photos they saved. The closest wines come back even when
        none is a real match.
        """
        if not query.strip() and not photo_ref:
            raise ToolError("Give a query, a photo_ref, or both")
        matches = service.search_wines(current_user(ctx), query, photo_ref=photo_ref, limit=limit)
        return {"matches": [_summary(w) for w in matches]}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @_traced
    @_agent_errors
    def list_wines(
        ctx: Context,
        verdict: Annotated[Verdict | None, Field(description="Only wines with this verdict")] = None,
        varietal: Annotated[str | None, Field(description="Varietal contains this text")] = None,
        created_after: Annotated[datetime | None, Field(description="First recorded at or after (UTC)")] = None,
        created_before: Annotated[datetime | None, Field(description="First recorded at or before (UTC)")] = None,
        updated_after: Annotated[datetime | None, Field(description="Last changed at or after (UTC)")] = None,
        updated_before: Annotated[datetime | None, Field(description="Last changed at or before (UTC)")] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="Most wines to return in this page")] = 25,
        next_token: Annotated[
            str | None, Field(description="next_token from the previous call, to get the next page")
        ] = None,
    ) -> dict[str, Any]:
        """List recorded wines, most recently recorded first, optionally filtered.

        A next_token in the result means there are more wines; pass it back with the same filters to get them.

        Dates are when a wine was first recorded (created) and last changed (updated).
        """
        wines, token = service.list_wines(
            current_user(ctx),
            verdict=verdict,
            varietal=varietal,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
            limit=limit,
            next_token=next_token,
        )
        return {"wines": [_summary(w) for w in wines], "next_token": token}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @_traced
    @_agent_errors
    def get_wine(ctx: Context, wine_id: WineId) -> dict[str, Any]:
        """Fetch one wine."""
        return {"wine": _summary(service.get_wine(current_user(ctx), wine_id))}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    @_traced
    @_agent_errors
    def delete_wine(ctx: Context, wine_id: WineId) -> dict[str, Any]:
        """Permanently remove a wine."""
        service.delete_wine(current_user(ctx), wine_id)
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
