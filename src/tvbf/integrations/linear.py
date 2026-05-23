"""Thin async client over Linear's GraphQL API for the three mutations the
feedback flow needs: customerUpsert, issueCreate, customerNeedCreate.

Auth is a single `Authorization: <api_key>` header — Linear's Personal API
keys don't use a Bearer prefix.
"""

from __future__ import annotations

from typing import Any

import httpx

_LINEAR_URL = "https://api.linear.app/graphql"

_CUSTOMER_UPSERT = """
mutation CustomerUpsert($input: CustomerUpsertInput!) {
  customerUpsert(input: $input) {
    success
    customer { id }
  }
}
""".strip()

_ISSUE_CREATE = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id url }
  }
}
""".strip()

_CUSTOMER_NEED_CREATE = """
mutation CustomerNeedCreate($input: CustomerNeedCreateInput!) {
  customerNeedCreate(input: $input) {
    success
  }
}
""".strip()


class LinearError(Exception):
    """Raised on transport failure or any non-success response from Linear."""


class LinearClient:
    def __init__(self, *, api_key: str, http: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._http = http

    async def customer_upsert(self, *, external_id: str, name: str) -> str:
        # Linear's CustomerUpsertInput uses `externalId` (singular string),
        # not `externalIds` (plural array) — verified against prod after a
        # 400 from the GraphQL endpoint citing "Field externalIds is not
        # defined by type CustomerUpsertInput". Keep the singular form.
        data = await self._call(
            _CUSTOMER_UPSERT,
            {"input": {"externalId": external_id, "name": name}},
            "customerUpsert",
        )
        customer = data.get("customer") or {}
        cid = customer.get("id")
        if not isinstance(cid, str):
            raise LinearError("customerUpsert returned no customer id")
        return cid

    async def issue_create(
        self,
        *,
        team_id: str,
        title: str,
        description: str,
        label_ids: list[str] | None = None,
    ) -> dict[str, str]:
        """Returns `{"id": ..., "url": ...}` for the created issue."""
        payload: dict[str, Any] = {
            "teamId": team_id,
            "title": title,
            "description": description,
        }
        if label_ids:
            payload["labelIds"] = label_ids
        data = await self._call(_ISSUE_CREATE, {"input": payload}, "issueCreate")
        issue = data.get("issue") or {}
        iid = issue.get("id")
        iurl = issue.get("url")
        if not isinstance(iid, str) or not isinstance(iurl, str):
            raise LinearError("issueCreate returned no issue id/url")
        return {"id": iid, "url": iurl}

    async def customer_need_create(
        self,
        *,
        issue_id: str,
        customer_external_id: str,
        body: str,
    ) -> None:
        await self._call(
            _CUSTOMER_NEED_CREATE,
            {
                "input": {
                    "issueId": issue_id,
                    "customerExternalId": customer_external_id,
                    "body": body,
                }
            },
            "customerNeedCreate",
        )

    async def _call(
        self, query: str, variables: dict[str, Any], mutation_name: str
    ) -> dict[str, Any]:
        try:
            res = await self._http.post(
                _LINEAR_URL,
                json={"query": query, "variables": variables},
                headers={
                    "Authorization": self._api_key,
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise LinearError(f"transport error: {exc}") from exc

        if res.status_code // 100 != 2:
            raise LinearError(
                f"linear {mutation_name} returned http {res.status_code}: {res.text[:200]}"
            )

        body = res.json()
        if errors := body.get("errors"):
            msg = "; ".join(e.get("message", "?") for e in errors)
            raise LinearError(f"linear {mutation_name} graphql error: {msg}")

        payload = (body.get("data") or {}).get(mutation_name) or {}
        if not payload.get("success"):
            raise LinearError(f"linear {mutation_name} success=false")
        return payload
