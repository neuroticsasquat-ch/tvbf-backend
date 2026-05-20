from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from tvbf.integrations.linear import LinearClient, LinearError


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        yield client


@respx.mock
async def test_customer_upsert_returns_id(http: httpx.AsyncClient) -> None:
    route = respx.post("https://api.linear.app/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "customerUpsert": {
                        "success": True,
                        "customer": {"id": "cust_123"},
                    }
                }
            },
        )
    )
    client = LinearClient(api_key="sk_test", http=http)
    customer_id = await client.customer_upsert(external_id="tvbf-user-1", name="Alice")
    assert customer_id == "cust_123"
    assert route.called
    sent = route.calls[0].request
    assert sent.headers["Authorization"] == "sk_test"
    assert sent.headers["Content-Type"] == "application/json"
    body = sent.read().decode()
    assert "customerUpsert" in body
    assert "tvbf-user-1" in body
    assert "Alice" in body


@respx.mock
async def test_issue_create_returns_id_with_labels(http: httpx.AsyncClient) -> None:
    respx.post("https://api.linear.app/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {"id": "iss_456"},
                    }
                }
            },
        )
    )
    client = LinearClient(api_key="sk_test", http=http)
    issue_id = await client.issue_create(
        team_id="team_1",
        title="A subject",
        description="A body",
        label_ids=["lbl_1"],
    )
    assert issue_id == "iss_456"


@respx.mock
async def test_customer_need_create_succeeds(http: httpx.AsyncClient) -> None:
    respx.post("https://api.linear.app/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"customerNeedCreate": {"success": True}}},
        )
    )
    client = LinearClient(api_key="sk_test", http=http)
    await client.customer_need_create(
        issue_id="iss_456",
        customer_external_id="tvbf-user-1",
        body="A body",
    )


@respx.mock
async def test_raises_on_graphql_errors(http: httpx.AsyncClient) -> None:
    respx.post("https://api.linear.app/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"errors": [{"message": "unauthorized"}]},
        )
    )
    client = LinearClient(api_key="sk_test", http=http)
    with pytest.raises(LinearError, match="unauthorized"):
        await client.customer_upsert(external_id="x", name="x")


@respx.mock
async def test_raises_on_non_2xx(http: httpx.AsyncClient) -> None:
    respx.post("https://api.linear.app/graphql").mock(
        return_value=httpx.Response(500, text="boom"),
    )
    client = LinearClient(api_key="sk_test", http=http)
    with pytest.raises(LinearError, match="500"):
        await client.issue_create(team_id="t", title="s", description="b")


@respx.mock
async def test_raises_on_success_false(http: httpx.AsyncClient) -> None:
    respx.post("https://api.linear.app/graphql").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "customerNeedCreate": {"success": False},
                }
            },
        )
    )
    client = LinearClient(api_key="sk_test", http=http)
    with pytest.raises(LinearError, match="success=false"):
        await client.customer_need_create(issue_id="i", customer_external_id="x", body="b")
