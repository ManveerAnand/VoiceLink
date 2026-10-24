from copy import deepcopy
import json
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.config import LookAheadSettings
from server.lookahead.protocol import MAX_BODY_BYTES
from server.lookahead.store import SnapshotStore
from server.routers.lookahead import create_router


def snapshot(*, generation: int = 3, revision: int = 18, current_sequence: int = 42) -> dict:
    return {
        "protocol": 1,
        "generation": generation,
        "revision": revision,
        "currentSequence": current_sequence,
        "source": {"rate": 1.0, "voiceName": "Heart (Kokoro)", "lang": "en-GB"},
        "units": [
            {
                "sequence": current_sequence,
                "text": "Example sentence.",
                "lang": "en-GB",
                "direction": "ltr",
                "block": {"id": "b7", "kind": "paragraph", "htmlTag": "p"},
                "position": {
                    "indexInBlock": 0,
                    "firstInBlock": True,
                    "lastInBlock": True,
                },
                "boundaryBefore": "paragraph",
                "boundaryAfter": "paragraph",
            }
        ],
    }


@pytest.mark.asyncio
async def test_capabilities_advertise_the_normative_protocol(client: AsyncClient) -> None:
    response = await client.get("/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "lookahead": {
            "protocols": [1],
            "maxUnits": 12,
            "maxCharacters": 12000,
            "maxUnitCharacters": 4000,
            "maxBodyBytes": 65536,
        },
        "structuredPauses": True,
        "bridgeProtocolRequiredForPauses": 2,
    }


@pytest.mark.asyncio
async def test_put_accepts_a_valid_snapshot(client: AsyncClient) -> None:
    body = snapshot()

    response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=body)

    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "generation": 3, "revision": 18}


@pytest.mark.asyncio
async def test_put_returns_duplicate_for_an_identical_replay(client: AsyncClient) -> None:
    stream_id = uuid4()
    body = snapshot()
    await client.put(f"/v1/tts/lookahead/{stream_id}", json=body)

    response = await client.put(f"/v1/tts/lookahead/{stream_id}", json=body)

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate", "generation": 3, "revision": 18}


@pytest.mark.asyncio
async def test_put_rejects_a_lower_revision_without_replacing_state(client: AsyncClient) -> None:
    stream_id = uuid4()
    accepted = snapshot(revision=2)
    await client.put(f"/v1/tts/lookahead/{stream_id}", json=accepted)

    response = await client.put(f"/v1/tts/lookahead/{stream_id}", json=snapshot(revision=1))
    replay = await client.put(f"/v1/tts/lookahead/{stream_id}", json=accepted)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale"
    assert replay.status_code == 200
    assert replay.json()["status"] == "duplicate"


@pytest.mark.asyncio
async def test_put_rejects_a_reused_revision_with_different_content(client: AsyncClient) -> None:
    stream_id = uuid4()
    body = snapshot()
    await client.put(f"/v1/tts/lookahead/{stream_id}", json=body)
    conflicting_body = deepcopy(body)
    conflicting_body["units"][0]["text"] = "A different sentence."

    response = await client.put(f"/v1/tts/lookahead/{stream_id}", json=conflicting_body)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "revision-conflict"


@pytest.mark.asyncio
async def test_put_rejects_a_regressing_current_sequence(client: AsyncClient) -> None:
    stream_id = uuid4()
    await client.put(f"/v1/tts/lookahead/{stream_id}", json=snapshot(revision=1, current_sequence=2))

    response = await client.put(
        f"/v1/tts/lookahead/{stream_id}", json=snapshot(revision=2, current_sequence=1)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale"


@pytest.mark.asyncio
async def test_put_rejects_a_higher_generation_with_a_regressing_current_sequence(
    client: AsyncClient,
) -> None:
    stream_id = uuid4()
    await client.put(
        f"/v1/tts/lookahead/{stream_id}", json=snapshot(generation=3, revision=2, current_sequence=2)
    )

    response = await client.put(
        f"/v1/tts/lookahead/{stream_id}", json=snapshot(generation=4, revision=0, current_sequence=1)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale"


@pytest.mark.asyncio
async def test_put_rejects_an_oversized_unit_before_replacing_state(client: AsyncClient) -> None:
    body = snapshot()
    body["units"][0]["text"] = "x" * 4_001

    response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=body)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "over-limit"


@pytest.mark.asyncio
async def test_put_accepts_protocol_unit_and_total_character_boundaries(client: AsyncClient) -> None:
    unit_boundary = snapshot()
    unit_boundary["units"] = [deepcopy(unit_boundary["units"][0]) for _ in range(12)]
    for sequence, unit in enumerate(unit_boundary["units"], start=42):
        unit["sequence"] = sequence

    total_boundary = snapshot()
    total_boundary["units"] = [deepcopy(total_boundary["units"][0]) for _ in range(3)]
    for sequence, unit in enumerate(total_boundary["units"], start=42):
        unit["sequence"] = sequence
        unit["text"] = "x" * 4_000

    units_response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=unit_boundary)
    total_response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=total_boundary)

    assert units_response.status_code == 202
    assert total_response.status_code == 202


@pytest.mark.asyncio
async def test_put_rejects_unit_count_and_total_character_limit_excesses(client: AsyncClient) -> None:
    too_many_units = snapshot()
    too_many_units["units"] = [deepcopy(too_many_units["units"][0]) for _ in range(13)]
    for sequence, unit in enumerate(too_many_units["units"], start=42):
        unit["sequence"] = sequence

    too_many_characters = snapshot()
    too_many_characters["units"] = [deepcopy(too_many_characters["units"][0]) for _ in range(4)]
    for sequence, unit in enumerate(too_many_characters["units"], start=42):
        unit["sequence"] = sequence
        unit["text"] = "x" * (4_000 if sequence < 45 else 1)

    units_response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=too_many_units)
    total_response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=too_many_characters)

    assert units_response.status_code == 413
    assert units_response.json()["error"]["code"] == "over-limit"
    assert total_response.status_code == 413
    assert total_response.json()["error"]["code"] == "over-limit"


@pytest.mark.asyncio
async def test_put_enforces_the_body_byte_boundary_before_json_parsing(client: AsyncClient) -> None:
    body = json.dumps(snapshot()).encode()
    at_limit = body + (b" " * (MAX_BODY_BYTES - len(body)))
    over_limit = at_limit + b" "

    accepted = await client.put(
        f"/v1/tts/lookahead/{uuid4()}", content=at_limit, headers={"content-type": "application/json"}
    )
    rejected = await client.put(
        f"/v1/tts/lookahead/{uuid4()}", content=over_limit, headers={"content-type": "application/json"}
    )

    assert accepted.status_code == 202
    assert rejected.status_code == 413
    assert rejected.json()["error"]["code"] == "over-limit"


@pytest.mark.asyncio
async def test_put_treats_byte_different_equivalent_json_as_a_revision_conflict(
    client: AsyncClient,
) -> None:
    stream_id = uuid4()
    body = snapshot()
    first = json.dumps(body, separators=(",", ":")).encode()
    replay_with_different_bytes = json.dumps(body, indent=2).encode()

    accepted = await client.put(
        f"/v1/tts/lookahead/{stream_id}", content=first, headers={"content-type": "application/json"}
    )
    conflict = await client.put(
        f"/v1/tts/lookahead/{stream_id}", content=replay_with_different_bytes,
        headers={"content-type": "application/json"},
    )

    assert accepted.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "revision-conflict"


@pytest.mark.asyncio
async def test_put_rejects_an_unsupported_protocol(client: AsyncClient) -> None:
    body = snapshot()
    body["protocol"] = 2

    response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=body)

    assert response.status_code == 426
    assert response.json()["error"]["code"] == "unsupported-protocol"


@pytest.mark.asyncio
async def test_put_rejects_when_the_active_stream_budget_is_exhausted(
    limited_client: AsyncClient,
) -> None:
    first = await limited_client.put(f"/v1/tts/lookahead/{uuid4()}", json=snapshot())
    second = await limited_client.put(f"/v1/tts/lookahead/{uuid4()}", json=snapshot())

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "budget-exhausted"


@pytest.mark.asyncio
async def test_delete_is_idempotent(client: AsyncClient) -> None:
    stream_id = uuid4()

    first = await client.delete(f"/v1/tts/lookahead/{stream_id}")
    second = await client.delete(f"/v1/tts/lookahead/{stream_id}")

    assert first.status_code == 204
    assert second.status_code == 204


@pytest.mark.asyncio
async def test_put_rejects_null_optionals_and_non_consecutive_sequences(client: AsyncClient) -> None:
    null_optional = snapshot()
    null_optional["source"]["voiceName"] = None
    null_response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=null_optional)

    non_consecutive = snapshot()
    non_consecutive["units"].append(deepcopy(non_consecutive["units"][0]))
    non_consecutive["units"][1]["sequence"] = 44
    sequence_response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=non_consecutive)

    assert null_response.status_code == 400
    assert null_response.json()["error"]["code"] == "invalid"
    assert sequence_response.status_code == 400
    assert sequence_response.json()["error"]["code"] == "invalid"


@pytest.mark.asyncio
async def test_put_rejects_unknown_enum_values(client: AsyncClient) -> None:
    body = snapshot()
    body["units"][0]["direction"] = "sideways"

    response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "ready"),
    [(False, True), (True, False)],
)
async def test_capabilities_omit_lookahead_until_enabled_and_worker_ready(
    enabled: bool, ready: bool
) -> None:
    app = FastAPI()
    app.include_router(
        create_router(
            settings=LookAheadSettings(enabled=enabled),
            store=SnapshotStore(),
            worker_ready=lambda: ready,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as capability_client:
        response = await capability_client.get("/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "structuredPauses": True,
        "bridgeProtocolRequiredForPauses": 2,
    }
