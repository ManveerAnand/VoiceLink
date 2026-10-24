from copy import deepcopy
from uuid import uuid4

import pytest
from httpx import AsyncClient


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
    await client.put(f"/v1/tts/lookahead/{stream_id}", json=snapshot(revision=2))

    response = await client.put(f"/v1/tts/lookahead/{stream_id}", json=snapshot(revision=1))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale"


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
async def test_put_rejects_an_oversized_unit_before_replacing_state(client: AsyncClient) -> None:
    body = snapshot()
    body["units"][0]["text"] = "x" * 4_001

    response = await client.put(f"/v1/tts/lookahead/{uuid4()}", json=body)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "over-limit"


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
