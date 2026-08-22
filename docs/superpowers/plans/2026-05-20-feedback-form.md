# In-App Feedback Form Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Per repo convention, this plan contains NO `git commit` steps — the user commits at their own cadence. Each numbered Task ships as its own PR off a shared work branch.

**Goal:** Ship a one-way in-app feedback form. Authed SPA users submit subject + body; tvbf-backend forwards to Linear as a new Issue with an attached CustomerNeed keyed to a per-user Customer.

**Architecture:** Thin async httpx client over Linear's GraphQL API (`customerUpsert`, `issueCreate`, `customerNeedCreate`). A feedback service composes the three calls; a `POST /me/feedback` router exposes it. No new DB tables. A shadcn Dialog in the SPA, triggered from `UserMenu`, mutates via React Query. Feature-flagged via `linear_feedback_enabled`.

**Tech Stack:** FastAPI, async httpx, Pydantic v2, pytest with respx + ASGITransport. React 19 + Vite 6 + TypeScript + Tailwind v4 + shadcn/ui + React Query + sonner toasts + MSW + Vitest. All commands run inside the `tvbf_backend` / `tvbf_frontend` containers via `task` targets.

**Spec:** `docs/superpowers/specs/2026-05-20-feedback-form-design.md`

---

## File map

### Backend (`tvbf-backend/`)

**Create:**
- `src/tvbf/integrations/__init__.py` — package marker.
- `src/tvbf/integrations/linear.py` — `LinearClient`, `LinearError`.
- `src/tvbf/app/services/feedback_service.py` — `submit_feedback`.
- `src/tvbf/routers/feedback.py` — `POST /me/feedback`.
- `tests/unit/integrations/__init__.py` — package marker.
- `tests/unit/integrations/test_linear_client.py` — respx-backed unit tests for `LinearClient`.
- `tests/integration/routers/test_feedback.py` — router integration tests with a fake `LinearClient`.

**Modify:**
- `src/tvbf/config.py` — add `linear_api_key`, `linear_team_id`, `linear_feedback_label_id`, `linear_feedback_enabled`.
- `src/tvbf/main.py` — construct a shared `httpx.AsyncClient` for Linear in `lifespan`; mount `feedback.router`.
- `src/tvbf/deps.py` — add `get_linear_client` dependency.

### Frontend (`tvbf-frontend/`)

**Create:**
- `src/components/feedback/FeedbackDialog.tsx` — modal with subject + body.
- `src/components/feedback/FeedbackDialog.test.tsx` — Vitest + MSW.

**Modify:**
- `src/api/me.ts` — add `useSubmitFeedback`.
- `src/components/UserMenu.tsx` — open the dialog from a new menu item.
- `src/test/msw/handlers.ts` — handler for `POST /me/feedback`.

---

## Task 1: Linear GraphQL client (unit-tested)

Ship a small async client for the three mutations we need, with respx-backed unit tests. No app wiring yet.

**Files:**
- Create: `tvbf-backend/src/tvbf/integrations/__init__.py`
- Create: `tvbf-backend/src/tvbf/integrations/linear.py`
- Create: `tvbf-backend/tests/unit/integrations/__init__.py`
- Create: `tvbf-backend/tests/unit/integrations/test_linear_client.py`

- [ ] **Step 1: Create the package markers**

`tvbf-backend/src/tvbf/integrations/__init__.py`:

```python
```

`tvbf-backend/tests/unit/integrations/__init__.py`:

```python
```

- [ ] **Step 2: Write the failing unit tests**

`tvbf-backend/tests/unit/integrations/test_linear_client.py`:

```python
from __future__ import annotations

import httpx
import pytest
import respx

from tvbf.integrations.linear import LinearClient, LinearError


@pytest.fixture
async def http() -> httpx.AsyncClient:
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
        await client.customer_need_create(
            issue_id="i", customer_external_id="x", body="b"
        )
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd tvbf-backend && task test -- tests/unit/integrations/test_linear_client.py
```

Expected: ImportError / collection failure for `tvbf.integrations.linear`.

- [ ] **Step 4: Implement `LinearClient` and `LinearError`**

`tvbf-backend/src/tvbf/integrations/linear.py`:

```python
"""Thin async client over Linear's GraphQL API for the three mutations the
feedback flow needs: customerUpsert, issueCreate, customerNeedCreate.

Auth is a single `Authorization: <api_key>` header — Linear's Personal API
keys don't use a Bearer prefix.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

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
    issue { id }
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
        data = await self._call(
            _CUSTOMER_UPSERT,
            {"input": {"externalIds": [external_id], "name": name}},
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
    ) -> str:
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
        if not isinstance(iid, str):
            raise LinearError("issueCreate returned no issue id")
        return iid

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
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd tvbf-backend && task test -- tests/unit/integrations/test_linear_client.py
```

Expected: 6 passed.

- [ ] **Step 6: Lint, format, typecheck**

```bash
cd tvbf-backend && task format && task lint && task typecheck
```

Expected: all pass, 0 errors.

---

## Task 2: Settings, dependency, and app wiring

Add the four new env-driven settings, build the `LinearClient` once in the lifespan, and expose it through a FastAPI dependency. No new routes yet.

**Files:**
- Modify: `tvbf-backend/src/tvbf/config.py`
- Modify: `tvbf-backend/src/tvbf/main.py`
- Modify: `tvbf-backend/src/tvbf/deps.py`
- Modify: `tvbf-backend/tests/unit/test_config.py`

- [ ] **Step 1: Add settings fields**

In `tvbf-backend/src/tvbf/config.py`, inside `class Settings`, after the existing `frontend_base_url` line, add:

```python
    # Linear feedback integration. Disabled by default; flip
    # LINEAR_FEEDBACK_ENABLED=true once an API key + team id are configured.
    linear_feedback_enabled: bool = Field(default=False, alias="LINEAR_FEEDBACK_ENABLED")
    linear_api_key: str | None = Field(default=None, alias="LINEAR_API_KEY")
    linear_team_id: str | None = Field(default=None, alias="LINEAR_TEAM_ID")
    linear_feedback_label_id: str | None = Field(
        default=None, alias="LINEAR_FEEDBACK_LABEL_ID"
    )
```

- [ ] **Step 2: Extend the config unit test**

Append to `tvbf-backend/tests/unit/test_config.py`:

```python
def test_linear_settings_defaults(monkeypatch) -> None:
    monkeypatch.delenv("LINEAR_FEEDBACK_ENABLED", raising=False)
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("LINEAR_TEAM_ID", raising=False)
    monkeypatch.delenv("LINEAR_FEEDBACK_LABEL_ID", raising=False)
    from tvbf.config import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.linear_feedback_enabled is False
    assert s.linear_api_key is None
    assert s.linear_team_id is None
    assert s.linear_feedback_label_id is None


def test_linear_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("LINEAR_FEEDBACK_ENABLED", "true")
    monkeypatch.setenv("LINEAR_API_KEY", "sk_x")
    monkeypatch.setenv("LINEAR_TEAM_ID", "team_x")
    monkeypatch.setenv("LINEAR_FEEDBACK_LABEL_ID", "lbl_x")
    from tvbf.config import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.linear_feedback_enabled is True
    assert s.linear_api_key == "sk_x"
    assert s.linear_team_id == "team_x"
    assert s.linear_feedback_label_id == "lbl_x"
```

If `test_config.py` already has fixtures that wipe env vars between tests, drop the `monkeypatch.delenv` lines in `test_linear_settings_defaults` accordingly — read the file first to match local conventions.

- [ ] **Step 3: Run the new config tests**

```bash
cd tvbf-backend && task test -- tests/unit/test_config.py
```

Expected: PASS for both new tests (plus existing tests).

- [ ] **Step 4: Wire the lifespan client and dependency**

In `tvbf-backend/src/tvbf/main.py`, locate the `lifespan` context manager (currently handles stale-run cleanup). Add a shared `httpx.AsyncClient` for Linear and stash both the client and a `LinearClient` instance on `app.state`. Sketch — adapt to the actual function shape:

```python
import httpx  # add to imports if not already present
from tvbf.integrations.linear import LinearClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # ... existing stale-run cleanup ...
    linear_http = httpx.AsyncClient(timeout=10.0)
    app.state.linear_http = linear_http
    app.state.linear_client = (
        LinearClient(api_key=settings.linear_api_key, http=linear_http)
        if settings.linear_feedback_enabled and settings.linear_api_key
        else None
    )
    try:
        yield
    finally:
        await linear_http.aclose()
```

If `lifespan` does not currently exist in `main.py`, find where startup/shutdown logic lives (`@app.on_event` or a `lifespan` defined elsewhere) and adapt. Do NOT introduce a new lifespan if one already exists — extend it in place.

- [ ] **Step 5: Add the dependency**

In `tvbf-backend/src/tvbf/deps.py`, add:

```python
from fastapi import Request

from tvbf.integrations.linear import LinearClient


def get_linear_client(request: Request) -> LinearClient:
    client = getattr(request.app.state, "linear_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Feedback is currently disabled.")
    return client
```

(Use the existing `HTTPException`/`status` imports already in the file — do not duplicate.)

- [ ] **Step 6: Run the full backend suite to confirm nothing regressed**

```bash
cd tvbf-backend && task test
```

Expected: full suite passes; no new tests yet for `deps`/lifespan changes (covered indirectly by Task 3).

- [ ] **Step 7: Lint, format, typecheck**

```bash
cd tvbf-backend && task format && task lint && task typecheck
```

Expected: all pass.

---

## Task 3: Feedback service + router + integration tests

Compose the three Linear calls in a service, expose `POST /me/feedback`, cover the router with integration tests using a fake client.

**Files:**
- Create: `tvbf-backend/src/tvbf/app/services/feedback_service.py`
- Create: `tvbf-backend/src/tvbf/routers/feedback.py`
- Create: `tvbf-backend/tests/integration/routers/test_feedback.py`
- Modify: `tvbf-backend/src/tvbf/main.py` — `app.include_router(feedback.router)`

- [ ] **Step 1: Write the failing integration tests**

`tvbf-backend/tests/integration/routers/test_feedback.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tvbf.config import get_settings
from tvbf.deps import get_linear_client
from tvbf.integrations.linear import LinearError
from tvbf.main import app


@dataclass
class FakeLinear:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    customer_upsert_id: str = "cust_123"
    issue_create_id: str = "iss_456"
    raise_on: str | None = None

    async def customer_upsert(self, *, external_id: str, name: str) -> str:
        self.calls.append(("customer_upsert", {"external_id": external_id, "name": name}))
        if self.raise_on == "customer_upsert":
            raise LinearError("boom")
        return self.customer_upsert_id

    async def issue_create(
        self,
        *,
        team_id: str,
        title: str,
        description: str,
        label_ids: list[str] | None = None,
    ) -> str:
        self.calls.append(
            (
                "issue_create",
                {
                    "team_id": team_id,
                    "title": title,
                    "description": description,
                    "label_ids": label_ids,
                },
            )
        )
        if self.raise_on == "issue_create":
            raise LinearError("boom")
        return self.issue_create_id

    async def customer_need_create(
        self, *, issue_id: str, customer_external_id: str, body: str
    ) -> None:
        self.calls.append(
            (
                "customer_need_create",
                {
                    "issue_id": issue_id,
                    "customer_external_id": customer_external_id,
                    "body": body,
                },
            )
        )
        if self.raise_on == "customer_need_create":
            raise LinearError("boom")


@pytest.fixture
def fake_linear() -> FakeLinear:
    return FakeLinear()


@pytest.fixture
def feedback_enabled(monkeypatch, fake_linear):
    settings = get_settings()
    monkeypatch.setattr(settings, "linear_feedback_enabled", True)
    monkeypatch.setattr(settings, "linear_team_id", "team_x")
    monkeypatch.setattr(settings, "linear_feedback_label_id", "lbl_x")
    app.dependency_overrides[get_linear_client] = lambda: fake_linear
    yield
    app.dependency_overrides.pop(get_linear_client, None)


async def test_submit_feedback_happy_path(authed_client, feedback_enabled, fake_linear):
    # `authed_client` is the standard fixture used by other /me/* router tests;
    # it returns an AsyncClient already carrying a session cookie + CSRF token
    # in the default header. Reuse it as-is.
    r = await authed_client.post(
        "/me/feedback",
        json={"subject": "Bug in star rating", "body": "Clicking flashes then reverts."},
    )
    assert r.status_code == 204
    kinds = [k for k, _ in fake_linear.calls]
    assert kinds == ["customer_upsert", "issue_create", "customer_need_create"]
    upsert_args = fake_linear.calls[0][1]
    assert upsert_args["external_id"].startswith("tvbf-user-")
    issue_args = fake_linear.calls[1][1]
    assert issue_args["title"] == "Bug in star rating"
    assert "Clicking flashes then reverts." in issue_args["description"]
    assert "From:" in issue_args["description"]
    assert issue_args["label_ids"] == ["lbl_x"]
    need_args = fake_linear.calls[2][1]
    assert need_args["issue_id"] == fake_linear.issue_create_id
    assert need_args["customer_external_id"] == upsert_args["external_id"]


async def test_submit_feedback_requires_auth():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as c:
        r = await c.post(
            "/me/feedback", json={"subject": "x", "body": "y"}
        )
    assert r.status_code == 401


async def test_submit_feedback_requires_csrf(authed_client_without_csrf, feedback_enabled):
    # `authed_client_without_csrf` is the parallel fixture in this repo for
    # exercising the require_csrf dependency. If it doesn't exist under that
    # name, use whatever pattern test_me.py uses to drop the CSRF header.
    r = await authed_client_without_csrf.post(
        "/me/feedback", json={"subject": "x", "body": "y"}
    )
    assert r.status_code == 403


async def test_submit_feedback_rejects_oversize_body(authed_client, feedback_enabled):
    r = await authed_client.post(
        "/me/feedback",
        json={"subject": "x", "body": "y" * 5001},
    )
    assert r.status_code == 422


async def test_submit_feedback_rejects_empty_subject(authed_client, feedback_enabled):
    r = await authed_client.post(
        "/me/feedback",
        json={"subject": "", "body": "y"},
    )
    assert r.status_code == 422


async def test_submit_feedback_returns_502_on_linear_error(
    authed_client, feedback_enabled, fake_linear
):
    fake_linear.raise_on = "issue_create"
    r = await authed_client.post(
        "/me/feedback", json={"subject": "x", "body": "y"}
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "Could not submit feedback."


async def test_submit_feedback_disabled_returns_503(authed_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "linear_feedback_enabled", False)
    r = await authed_client.post(
        "/me/feedback", json={"subject": "x", "body": "y"}
    )
    assert r.status_code == 503
```

**Note on fixtures:** This repo's `tests/integration/routers/conftest.py` (and/or `tests/conftest.py`) already provides `authed_client` for the rating/watch routers. If the exact fixture name in this repo differs, mirror whatever `test_me_ratings.py` uses for an authenticated AsyncClient. Do NOT invent new auth setup here — reuse the existing pattern.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd tvbf-backend && task test -- tests/integration/routers/test_feedback.py
```

Expected: collection error (no `tvbf.routers.feedback`) or 404 on the route.

- [ ] **Step 3: Implement the service**

`tvbf-backend/src/tvbf/app/services/feedback_service.py`:

```python
"""Compose the three Linear mutations that submit a feedback issue.

Calls are sequential because customerNeedCreate depends on the issue id from
issueCreate. customerUpsert is idempotent on externalId, so repeat submissions
from the same user reuse the existing Customer.
"""

from __future__ import annotations

from tvbf.app.models import User
from tvbf.config import Settings
from tvbf.integrations.linear import LinearClient


def _external_id(user: User) -> str:
    return f"tvbf-user-{user.id}"


def _display(user: User) -> str:
    return user.display_name or user.email


def _description(user: User, body: str) -> str:
    return f"{body}\n\n---\nFrom: {user.email} (id `{user.id}`)"


async def submit_feedback(
    *,
    user: User,
    subject: str,
    body: str,
    linear: LinearClient,
    settings: Settings,
) -> None:
    if not settings.linear_team_id:
        raise RuntimeError("linear_team_id is not configured")

    external_id = _external_id(user)
    await linear.customer_upsert(external_id=external_id, name=_display(user))
    label_ids = [settings.linear_feedback_label_id] if settings.linear_feedback_label_id else None
    issue_id = await linear.issue_create(
        team_id=settings.linear_team_id,
        title=subject,
        description=_description(user, body),
        label_ids=label_ids,
    )
    await linear.customer_need_create(
        issue_id=issue_id,
        customer_external_id=external_id,
        body=body,
    )
```

- [ ] **Step 4: Implement the router**

`tvbf-backend/src/tvbf/routers/feedback.py`:

```python
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from tvbf.app.models import User
from tvbf.app.services import feedback_service
from tvbf.config import Settings, get_settings
from tvbf.deps import get_current_user, get_linear_client, require_csrf
from tvbf.integrations.linear import LinearClient, LinearError

log = logging.getLogger(__name__)

router = APIRouter(tags=["feedback"])


class FeedbackIn(BaseModel):
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=5000)


@router.post(
    "/me/feedback",
    status_code=204,
    dependencies=[Depends(require_csrf)],
)
async def submit_feedback_route(
    payload: FeedbackIn,
    user: User = Depends(get_current_user),
    linear: LinearClient = Depends(get_linear_client),
    settings: Settings = Depends(get_settings),
) -> Response:
    if not settings.linear_feedback_enabled:
        raise HTTPException(status_code=503, detail="Feedback is currently disabled.")
    try:
        await feedback_service.submit_feedback(
            user=user,
            subject=payload.subject,
            body=payload.body,
            linear=linear,
            settings=settings,
        )
    except LinearError:
        log.exception("linear feedback submission failed user_id=%s", user.id)
        raise HTTPException(status_code=502, detail="Could not submit feedback.")
    return Response(status_code=204)
```

- [ ] **Step 5: Mount the router**

In `tvbf-backend/src/tvbf/main.py`, add to the imports near other router imports:

```python
from tvbf.routers import feedback
```

And in `create_app`, alongside the existing `app.include_router(...)` calls:

```python
    app.include_router(feedback.router)
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd tvbf-backend && task test -- tests/integration/routers/test_feedback.py
```

Expected: all 7 tests pass.

- [ ] **Step 7: Run the full suite**

```bash
cd tvbf-backend && task test
```

Expected: full suite still passes.

- [ ] **Step 8: Lint, format, typecheck**

```bash
cd tvbf-backend && task format && task lint && task typecheck
```

Expected: all pass.

---

## Task 4: Frontend mutation hook + MSW handler

Add the React Query mutation hook and the MSW handler that backs frontend tests. No UI yet.

**Files:**
- Modify: `tvbf-frontend/src/api/me.ts`
- Modify: `tvbf-frontend/src/test/msw/handlers.ts`

- [ ] **Step 1: Add the mutation hook**

In `tvbf-frontend/src/api/me.ts`, near the other `useMutation` hooks (e.g., `useUpdatePreferences`), add:

```ts
export type FeedbackInput = { subject: string; body: string };

export function useSubmitFeedback() {
  return useMutation<void, ApiError, FeedbackInput>({
    mutationFn: (input) =>
      apiFetch<void>("/me/feedback", {
        method: "POST",
        body: JSON.stringify(input),
      }),
  });
}
```

Confirm `ApiError` is imported at the top of `me.ts`; if not, add `import { ApiError } from "./client";` (use the existing import line if `apiFetch` is imported from the same module).

- [ ] **Step 2: Add the MSW handler**

In `tvbf-frontend/src/test/msw/handlers.ts`, add (using the existing `http.post(...)` pattern in the file):

```ts
http.post("*/me/feedback", async ({ request }) => {
  const body = (await request.json()) as { subject?: string; body?: string };
  if (!body.subject || !body.body) {
    return HttpResponse.json({ detail: "Validation error" }, { status: 422 });
  }
  return new HttpResponse(null, { status: 204 });
}),
```

If the handlers file groups handlers into an exported array, place the new handler alongside other `/me/*` mutations.

- [ ] **Step 3: Run frontend tests to confirm nothing regressed**

```bash
cd tvbf-frontend && task test
```

Expected: existing tests still pass.

- [ ] **Step 4: Lint, typecheck**

```bash
cd tvbf-frontend && task lint && task typecheck
```

Expected: 0 errors.

---

## Task 5: Feedback dialog component + tests

Build the modal: shadcn `Dialog`, subject + body fields, submit button wired to `useSubmitFeedback`, toast + close on success, inline error on failure.

**Files:**
- Create: `tvbf-frontend/src/components/feedback/FeedbackDialog.tsx`
- Create: `tvbf-frontend/src/components/feedback/FeedbackDialog.test.tsx`

- [ ] **Step 1: Write the failing component test**

`tvbf-frontend/src/components/feedback/FeedbackDialog.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";
import { FeedbackDialog } from "./FeedbackDialog";

function renderDialog(open = true, onOpenChange = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <Toaster />
      <FeedbackDialog open={open} onOpenChange={onOpenChange} />
    </QueryClientProvider>,
  );
}

describe("FeedbackDialog", () => {
  it("submits subject + body and closes on success", async () => {
    const onOpenChange = vi.fn();
    renderDialog(true, onOpenChange);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/subject/i), "A subject");
    await user.type(screen.getByLabelText(/details/i), "Some details about the bug.");
    await user.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(await screen.findByText(/thanks/i)).toBeInTheDocument();
  });

  it("shows an inline error when the API rejects the body", async () => {
    const onOpenChange = vi.fn();
    renderDialog(true, onOpenChange);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText(/subject/i), "A subject");
    // body left empty → MSW handler returns 422
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(
      await screen.findByText(/could not send feedback/i),
    ).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
```

Confirm `src/test/setup.ts` already wires `Toaster` / MSW globally; otherwise, mirror the pattern from a sibling test that uses both.

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd tvbf-frontend && task test -- src/components/feedback/FeedbackDialog.test.tsx
```

Expected: module-not-found for `./FeedbackDialog`.

- [ ] **Step 3: Implement the component**

`tvbf-frontend/src/components/feedback/FeedbackDialog.tsx`:

```tsx
import { useState } from "react";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { useSubmitFeedback } from "@/api/me";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

const SUBJECT_MAX = 120;
const BODY_MAX = 5000;

export function FeedbackDialog({ open, onOpenChange }: Props) {
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);
  const mutation = useSubmitFeedback();

  const reset = () => {
    setSubject("");
    setBody("");
    setError(null);
  };

  const submit = async () => {
    setError(null);
    try {
      await mutation.mutateAsync({ subject, body });
      toast.success("Thanks — we got it.");
      reset();
      onOpenChange(false);
    } catch {
      setError("Could not send feedback. Try again later.");
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Send feedback</DialogTitle>
          <DialogDescription>
            Tell us what's broken, what's confusing, or what you'd like to see.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="feedback-subject">Subject</Label>
            <Input
              id="feedback-subject"
              value={subject}
              maxLength={SUBJECT_MAX}
              onChange={(e) => setSubject(e.target.value)}
              disabled={mutation.isPending}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="feedback-body">Details</Label>
            <Textarea
              id="feedback-body"
              value={body}
              maxLength={BODY_MAX}
              rows={6}
              onChange={(e) => setBody(e.target.value)}
              disabled={mutation.isPending}
            />
          </div>
          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}
        </div>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={submit}
            disabled={
              mutation.isPending || subject.trim().length === 0 || body.trim().length === 0
            }
          >
            {mutation.isPending ? "Sending…" : "Send"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
```

Check that `@/components/ui/{dialog,input,textarea,label}` exist in this repo. They are shadcn primitives and have been added in prior PRs (search `src/components/ui/`). If `textarea` or `label` is missing, generate via the shadcn workflow used in earlier features (see the existing `ui/*.tsx` for the pattern) BEFORE writing the component.

- [ ] **Step 4: Run the component tests**

```bash
cd tvbf-frontend && task test -- src/components/feedback/FeedbackDialog.test.tsx
```

Expected: both tests pass.

- [ ] **Step 5: Full frontend test suite**

```bash
cd tvbf-frontend && task test
```

Expected: full suite passes.

- [ ] **Step 6: Lint, typecheck**

```bash
cd tvbf-frontend && task lint && task typecheck
```

Expected: 0 errors.

---

## Task 6: Wire the dialog into `UserMenu`

Add a "Send feedback" item to `UserMenu`. Manage the dialog's `open` state at the parent of `UserMenu` (the same place `onChangePassword` / `onDeleteAccount` are wired today), to match the existing pattern of "the menu emits an intent, the parent owns the modal."

**Files:**
- Modify: `tvbf-frontend/src/components/UserMenu.tsx`
- Modify: `tvbf-frontend/src/components/AppShell.tsx` (or wherever `UserMenu` is currently rendered — grep for it)
- Modify: `tvbf-frontend/src/components/UserMenu.test.tsx` (if it exists)

- [ ] **Step 1: Locate UserMenu's parent**

```bash
cd tvbf-frontend && grep -rn "<UserMenu" src --include='*.tsx'
```

Expected: one or two call sites (likely `AppShell.tsx` and possibly a bottom-tab nav). Note them.

- [ ] **Step 2: Extend the `UserMenu` props**

In `tvbf-frontend/src/components/UserMenu.tsx`, change the props type to add `onSendFeedback`:

```ts
type UserMenuProps = {
  onChangePassword: () => void;
  onDeleteAccount: () => void;
  onSendFeedback: () => void;
  variant?: "icon" | "bottom-tab" | "icon-only";
};
```

Add the prop to the destructure: `({ onChangePassword, onDeleteAccount, onSendFeedback, variant = "icon" })`.

In the menu `<ul>`, insert a new `<li>` immediately above the "Change password" entry:

```tsx
          <li>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onSendFeedback();
              }}
              className="w-full text-left px-3 py-2 hover:bg-muted"
            >
              Send feedback
            </button>
          </li>
```

- [ ] **Step 3: Wire the dialog in the parent**

In each `<UserMenu .../>` call site found in Step 1, add local state and the dialog. Sketch (adapt to whatever the parent looks like):

```tsx
import { useState } from "react";
import { FeedbackDialog } from "@/components/feedback/FeedbackDialog";
// ...
const [feedbackOpen, setFeedbackOpen] = useState(false);
// ...
<UserMenu
  onChangePassword={...}
  onDeleteAccount={...}
  onSendFeedback={() => setFeedbackOpen(true)}
/>
<FeedbackDialog open={feedbackOpen} onOpenChange={setFeedbackOpen} />
```

If a second call site exists (bottom-tab variant on mobile), wire the dialog at that parent as well, OR lift both `<UserMenu>` renders into a single ancestor that owns one `FeedbackDialog` — pick whichever matches the existing pattern for the change-password/delete-account modals.

- [ ] **Step 4: Update the UserMenu test if present**

If `UserMenu.test.tsx` exists, add a test:

```tsx
it("invokes onSendFeedback when 'Send feedback' is clicked", async () => {
  const onSendFeedback = vi.fn();
  // render with the existing helper, passing onSendFeedback
  // open the menu, click "Send feedback"
  // expect onSendFeedback to have been called
});
```

Match the helpers / patterns already used in the file.

- [ ] **Step 5: Run frontend tests**

```bash
cd tvbf-frontend && task test
```

Expected: full suite passes.

- [ ] **Step 6: Lint, typecheck**

```bash
cd tvbf-frontend && task lint && task typecheck
```

Expected: 0 errors.

- [ ] **Step 7: Manual smoke test**

```bash
cd tvbf-frontend && task up
cd tvbf-backend  && task up
```

Set in `tvbf-backend/.env` (or your local override):

```
LINEAR_FEEDBACK_ENABLED=true
LINEAR_API_KEY=<a real personal API key>
LINEAR_TEAM_ID=<a real team id>
LINEAR_FEEDBACK_LABEL_ID=<optional>
```

`task down && task up` on tvbf-backend to pick up the env. Then in the SPA at `https://app.tvbf.localhost/`:

1. Sign in.
2. Open the account menu → "Send feedback".
3. Submit a real test message.
4. Verify in Linear: a new Issue exists with your text in the description and a CustomerNeed attached to a Customer named after your tvbf user.
5. Submit a second message; verify the same Customer is reused (no duplicate Customer in Linear).

---

## Self-review notes (resolved)

- Spec coverage: every section of the spec has at least one task (Linear client → Task 1; settings + DI + lifespan → Task 2; service + router + feature flag + tests → Task 3; SPA hook + MSW → Task 4; component + tests → Task 5; menu wiring + smoke test → Task 6).
- Type consistency: `LinearClient` method names, `submit_feedback` signature, `FeedbackInput` shape, and `FeedbackDialog` props match across tasks.
- Out-of-scope per spec (reply UI, attachments, categories, rate-limit table, anonymous submission) is not implemented anywhere in the plan.
