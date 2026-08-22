# In-App Feedback Form — Design

**Date:** 2026-05-20
**Status:** Proposed
**Owner:** Tom
**Linear:** (TBD — create on plan-write)

## Problem

There is no in-app channel for users to send feedback. The current beta runs on personal Slack / email, which doesn't scale past a handful of invitees and gives no structured intake — every report is unstructured prose in a different place. Triage is manual and lossy, and the work that comes out of feedback isn't tracked alongside everything else in Linear.

## Goal

Ship a one-way feedback form that lets a signed-in user submit a short message from any page in the SPA. Each submission lands in Linear as a new issue with a Customer Request (CustomerNeed) attached, keyed to a Customer that represents the submitting user. Linear becomes the system of record; tvbf-backend stores nothing about the submission itself.

## Non-goals

- **Bidirectional reply UI.** Users can't see or respond to Linear comments in v1. The product hook to add this is intentionally absent so we can evaluate volume before building it.
- **Anonymous feedback.** Signed-in users only. The user object is already useful context on every submission.
- **Categories, tags, severity, screenshots, attachments.** Freeform subject + body only. Adding structure later is cheap; getting people to fill out forms is not.
- **In-app rate limiting.** Linear's API quota is the only ceiling. With <100 beta users the abuse surface doesn't justify the table.
- **Storing the submission in the tvbf DB.** No `app.feedback_*` tables yet. Reply UI will introduce them later; v1 is fire-and-forget.
- **Multi-tenant Customer modelling.** Each tvbf user maps to one Linear Customer keyed by `externalId = tvbf-user-{uuid}`. We do not try to group users into orgs.

## Architecture

### Linear data model

Linear's Customer Requests feature uses two objects:

- **Customer** — an external entity (typically a company; we use it per-user).
- **CustomerNeed** — a single request, attached to one Linear `Issue` via `issueId`.

The Free plan allows API usage of both. Each submission creates one Issue + one CustomerNeed; subsequent submissions from the same user reuse the existing Customer.

### Backend — tvbf-backend

#### New module: `src/tvbf/integrations/linear.py`

Async httpx client. One class `LinearClient`:

```python
class LinearError(Exception): ...

class LinearClient:
    def __init__(self, api_key: str, http: httpx.AsyncClient): ...
    async def customer_upsert(self, *, external_id: str, name: str) -> str:
        """Returns the Customer id. Idempotent."""
    async def issue_create(
        self, *, team_id: str, title: str, description: str,
        label_ids: list[str] | None = None,
    ) -> str:
        """Returns the Issue id."""
    async def customer_need_create(
        self, *, issue_id: str, customer_external_id: str, body: str,
    ) -> None: ...
```

All three call `POST https://api.linear.app/graphql` with header `Authorization: <api_key>`. Each raises `LinearError` on:
- HTTP status not 2xx
- Response body containing `"errors": [...]`
- `data.<mutation>.success == false`

The client takes the `httpx.AsyncClient` as a constructor arg so tests can inject a `respx`-backed client.

#### New service: `src/tvbf/app/services/feedback_service.py`

```python
async def submit_feedback(
    *,
    user: User,
    subject: str,
    body: str,
    linear: LinearClient,
    settings: Settings,
) -> None:
    name = user.display_name or user.email
    customer_external_id = f"tvbf-user-{user.id}"
    await linear.customer_upsert(external_id=customer_external_id, name=name)
    description = f"{body}\n\n---\nFrom: {user.email} (id `{user.id}`)"
    issue_id = await linear.issue_create(
        team_id=settings.linear_team_id,
        title=subject,
        description=description,
        label_ids=[settings.linear_feedback_label_id] if settings.linear_feedback_label_id else None,
    )
    await linear.customer_need_create(
        issue_id=issue_id,
        customer_external_id=customer_external_id,
        body=body,
    )
```

Calls run sequentially because `customer_need_create` needs the issue id from `issue_create`. The `customer_upsert` and `issue_create` calls could overlap, but the small latency win isn't worth the error-handling complexity.

#### New router: `src/tvbf/routers/feedback.py`

```python
class FeedbackIn(BaseModel):
    subject: str = Field(min_length=1, max_length=120)
    body: str    = Field(min_length=1, max_length=5000)

@router.post("/me/feedback", status_code=204, dependencies=[Depends(require_csrf)])
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
            user=user, subject=payload.subject, body=payload.body,
            linear=linear, settings=settings,
        )
    except LinearError:
        log.exception("linear feedback submission failed user_id=%s", user.id)
        raise HTTPException(status_code=502, detail="Could not submit feedback.")
    return Response(status_code=204)
```

Mounted on the main app router. Uses the existing session-cookie auth + CSRF dependency.

#### Config + DI

`src/tvbf/config.py` gets three new env-driven fields:

- `linear_api_key: SecretStr` — required at startup if `linear_feedback_enabled=True`, else optional.
- `linear_team_id: str | None` — required if enabled.
- `linear_feedback_label_id: str | None` — optional; applied as a label on every feedback issue if set.
- `linear_feedback_enabled: bool = False` — feature flag. When false, the route returns `503` and the SPA hides the menu item.

`src/tvbf/deps.py` adds `get_linear_client()` which constructs a process-wide `LinearClient` with a shared `httpx.AsyncClient` from the app lifespan (same pattern as the TV Maze client).

#### Errors and logging

- Validation failures (Pydantic) → 422, standard FastAPI handling.
- `LinearError` → log with stack trace + user id, return 502 with generic message. No upstream details exposed to the client.
- The user-visible error in the SPA is identical for any failure: "Could not send feedback. Try again later."

#### Tests

- **Unit:** `tests/unit/integrations/test_linear_client.py` — mock `httpx` with `respx` for each mutation. Cover happy path, GraphQL `errors` payload, non-2xx response, `success: false` payload.
- **Integration:** `tests/integration/routers/test_feedback.py` — patches `get_linear_client` to return an in-memory fake that records calls. Cover: success → 204 + fake records three calls in order; unauthenticated → 401; missing CSRF → 403; oversize subject/body → 422; fake raises `LinearError` → 502; feature flag off → 503.

### Frontend — tvbf-frontend

#### New components

- `src/components/feedback/FeedbackDialog.tsx` — shadcn `Dialog`. Fields: `<Input>` for subject (maxLength 120), `<Textarea>` for body (maxLength 5000). Submit button calls the mutation; on success, show toast "Thanks — we got it." and close. On error, surface a small inline error inside the dialog. Disable submit while pending.
- `src/components/feedback/FeedbackMenuItem.tsx` — menu item that opens the dialog. Lives next to the existing logout entry in `UserMenu.tsx`.

#### API hook

`src/api/me.ts` gains `useSubmitFeedback()`:

```ts
export function useSubmitFeedback() {
  return useMutation<void, ApiError, { subject: string; body: string }>({
    mutationFn: (input) =>
      apiFetch<void>("/me/feedback", {
        method: "POST",
        body: JSON.stringify(input),
      }),
  });
}
```

No `onSettled` invalidation — there's no cached resource to refresh.

#### Tests

- `FeedbackDialog.test.tsx` — render, type subject + body, submit, MSW handler returns 204, assert toast shown + dialog closed.
- MSW handler for `POST /me/feedback` in `src/test/msw/handlers.ts` accepting valid bodies and producing 204; an error variant for an error-path test.

### Data flow

```
SPA: FeedbackDialog
  └── POST /me/feedback (cookie + CSRF)
        └── tvbf-backend: feedback router
              └── feedback_service.submit_feedback
                    ├── LinearClient.customer_upsert       → Linear GraphQL
                    ├── LinearClient.issue_create          → Linear GraphQL
                    └── LinearClient.customer_need_create  → Linear GraphQL
        ← 204 (or 502 on Linear failure)
  ← toast "Thanks — we got it."
```

## Operational considerations

- **Secrets.** `LINEAR_API_KEY` is a server-only env var, set on Coolify alongside existing secrets. Never shipped to the SPA.
- **Feature flag.** `linear_feedback_enabled` defaults off. Set to `true` in production after the Linear team + label IDs are configured.
- **Linear rate limits.** Linear's public limit (per their docs) is well above any plausible per-day volume from the beta user base. We don't implement application-side throttling in v1.
- **PII.** The user's email is embedded in the Linear issue description so the maintainer can identify them in-app. Linear is internal-only, so this is acceptable.
- **Customer name fallback.** If `display_name` is null, we use `email`. If both are null (shouldn't happen — email is required at signup), we use `"User {id}"`.

## Open questions

- **Linear team + label IDs.** Need to be created in Linear and the IDs captured for env vars. Plan step.
- **Linear project to file feedback under.** Currently undecided; could be a new "App Feedback" project, or filed under an existing triage project. Plan step.

## Risks

- **Sequencing failure mid-flow.** If `issue_create` succeeds but `customer_need_create` fails, the issue exists in Linear without an attached CustomerNeed. Acceptable: the issue itself still contains the feedback in the description, and an out-of-band CustomerNeed can be attached manually. We log the failure with the issue id to make recovery possible.
- **Linear schema drift.** Linear has changed its Customer/CustomerNeed API once already in the past year. Pinning the mutation field set in our client and covering each one with a respx-backed unit test catches drift on the next bump.

## Out of scope (v2 candidates)

- Bidirectional reply UI (`app.feedback_thread` table, Linear webhook receiver, `/feedback/thread/{id}` page).
- Categories / labels chosen by the user at submission time.
- File / screenshot attachments via Linear `attachmentLinkURL`.
- Voting / upvotes on existing feedback (would require surfacing other users' feedback, which we don't want in v1).
