"""HTTP endpoints for the optional VoiceLink look-ahead protocol."""

import json
import logging
import time
from collections.abc import Callable
from hashlib import sha256
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from server.config import LookAheadSettings, settings
from server.lookahead.protocol import (
    MAX_BODY_BYTES,
    MAX_CHARACTERS,
    MAX_UNIT_CHARACTERS,
    MAX_UNITS,
    PROTOCOL,
    LookAheadSnapshot,
)
from server.lookahead.store import ReplaceResult, SnapshotStore
from server.lookahead.service import LookAheadService


logger = logging.getLogger(__name__)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def _parse_stream_id(value: str) -> UUID | None:
    try:
        stream_id = UUID(value)
    except ValueError:
        return None
    return stream_id if str(stream_id) == value else None


def create_router(
    *,
    settings: LookAheadSettings,
    store: SnapshotStore,
    worker_ready: Callable[[], bool] = lambda: False,
    on_cancel_stream: Callable[[UUID], None] | None = None,
    service: LookAheadService | None = None,
) -> APIRouter:
    """Create an injectable router so protocol tests never load a TTS model."""
    store.configure_budgets(
        max_active_streams=settings.max_active_streams,
        max_global_characters=settings.max_active_streams * MAX_CHARACTERS,
    )
    router = APIRouter(prefix="/v1", tags=["Look-ahead"])

    @router.get("/capabilities")
    async def capabilities() -> dict[str, object]:
        body: dict[str, object] = {
            "structuredPauses": True,
            "bridgeProtocolRequiredForPauses": 2,
        }
        if settings.enabled and worker_ready():
            body = {
                "lookahead": {
                    "protocols": [PROTOCOL],
                    "maxUnits": MAX_UNITS,
                    "maxCharacters": MAX_CHARACTERS,
                    "maxUnitCharacters": MAX_UNIT_CHARACTERS,
                    "maxBodyBytes": MAX_BODY_BYTES,
                },
                **body,
            }
        return body

    @router.put("/tts/lookahead/{stream_id}")
    async def replace_snapshot(stream_id: str, request: Request) -> JSONResponse:
        parsed_stream_id = _parse_stream_id(stream_id)
        if parsed_stream_id is None:
            return _error(400, "invalid", "streamId must be a canonical lowercase UUID")

        raw_body = await request.body()
        if len(raw_body) > MAX_BODY_BYTES:
            return _error(
                413, "over-limit", "request body exceeds the protocol byte limit"
            )
        try:
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error(400, "invalid", "request body must be valid JSON")
        if not isinstance(payload, dict):
            return _error(400, "invalid", "request body must be a JSON object")
        protocol = payload.get("protocol")
        if (
            isinstance(protocol, int)
            and not isinstance(protocol, bool)
            and protocol != PROTOCOL
        ):
            return _error(
                426, "unsupported-protocol", f"protocol {protocol} is not supported"
            )
        try:
            snapshot = LookAheadSnapshot.model_validate(payload)
        except ValidationError:
            return _error(400, "invalid", "request body does not match protocol 1")
        limit_error = snapshot.limit_error()
        if limit_error is not None:
            return _error(413, "over-limit", limit_error)

        # The digest is intentionally of the original bytes, not a normalized model.
        snapshot._body_digest = sha256(raw_body).hexdigest()
        accepted_at = time.monotonic()
        if service is not None:
            try:
                accepted_at = service.now()
            except Exception:
                logger.error(
                    "look-ahead clock failed; snapshot will remain an optional optimization"
                )
        result = store.replace(parsed_stream_id, snapshot, len(raw_body), accepted_at)
        if result is ReplaceResult.ACCEPTED:
            if service is not None:
                record = store.get(parsed_stream_id)
                if record is not None:
                    try:
                        service.observe_snapshot(parsed_stream_id, record)
                    except Exception:
                        logger.error(
                            "look-ahead snapshot integration failed; authoritative TTS is unchanged"
                        )
            return JSONResponse(
                status_code=202,
                content={
                    "status": "accepted",
                    "generation": snapshot.generation,
                    "revision": snapshot.revision,
                },
            )
        if result is ReplaceResult.DUPLICATE:
            return JSONResponse(
                status_code=200,
                content={
                    "status": "duplicate",
                    "generation": snapshot.generation,
                    "revision": snapshot.revision,
                },
            )
        if result is ReplaceResult.BUDGET_EXHAUSTED:
            return _error(429, "budget-exhausted", "look-ahead budget is exhausted")
        if result is ReplaceResult.STALE:
            return _error(409, "stale", "snapshot is older than the accepted snapshot")
        return _error(
            409, "revision-conflict", "revision was already used with different content"
        )

    @router.delete("/tts/lookahead/{stream_id}", status_code=204)
    async def cancel_snapshot(stream_id: str) -> Response:
        parsed_stream_id = _parse_stream_id(stream_id)
        if parsed_stream_id is None:
            return _error(400, "invalid", "streamId must be a canonical lowercase UUID")
        store.cancel(parsed_stream_id)
        if service is not None:
            try:
                service.cancel_stream(parsed_stream_id)
            except Exception:
                logger.error(
                    "look-ahead service stream cancellation failed; DELETE remains idempotent"
                )
        if on_cancel_stream is not None:
            try:
                on_cancel_stream(parsed_stream_id)
            except Exception:
                # Look-ahead is optional; DELETE must retain its idempotent legacy result.
                logger.exception("look-ahead scheduler stream cancellation failed")
        return Response(status_code=204)

    return router


router = create_router(settings=settings.lookahead, store=SnapshotStore())
