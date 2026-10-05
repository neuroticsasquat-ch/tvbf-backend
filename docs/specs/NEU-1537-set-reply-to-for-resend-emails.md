# NEU-1537 — Set a default Reply-To on outbound email

**Ticket:** [NEU-1537](https://linear.app/neuroticsasquatch/issue/NEU-1537/set-reply_to-for-resend-emails)
**Project:** tvbf: Maintenance · no parent, no milestone, no relations
**Repo:** `tvbf-backend` only. No frontend change; no API change.
**Written:** 2026-10-05

Production mail goes out from `EMAIL_FROM_ADDRESS`, a `no-reply@` mailbox that
cannot receive. A user who hits reply on a verification, password-reset or
invite email gets a bounce. The fix is a configured reply-to address applied to
every outbound email that does not already name one.

---

## 1. Setting

One new optional setting in `config.py`, next to `email_from_address`:

```python
email_reply_to_address: str | None = Field(default=None, alias="EMAIL_REPLY_TO_ADDRESS")
```

- **Optional, defaults to `None`.** Unset means no `Reply-To` header: mail goes
  out exactly as it does today. Dev with Mailpit needs nothing set. This is the
  `FEEDBACK_NOTIFY_EMAIL` shape, not the `RESEND_API_KEY` shape — forgetting it
  in Coolify keeps the current behaviour rather than failing the deploy.
- **Same format as `EMAIL_FROM_ADDRESS`:** a bare address or
  `Display Name <address>`. Both Resend's `reply_to` field and an SMTP
  `Reply-To` header accept either, so nothing parses or validates it beyond
  Pydantic's `str`.
- **Deliberately a new variable, not `FEEDBACK_NOTIFY_EMAIL`.** That one means
  "the maintainer's mailbox" for server-sent notifications; this one is the
  mailbox a *user* reaches when they reply. They may be the same address in
  production today, but they are different roles, and the existing comment on
  `feedback_notify_email` already notes its name is at the edge of what it
  covers. Widening it again would be a third meaning under one key.
- The production value is set in the Coolify UI, as the other email settings are.

## 2. Seam: the client, not the call site

`build_email_client` passes `settings.email_reply_to_address` to **both**
clients as `default_reply_to`, and each client applies it when the caller
passed none:

```python
class ResendEmailClient(EmailClient):
    def __init__(self, *, api_key, from_address, default_reply_to: str | None = None, timeout_seconds=10.0): ...

class SmtpEmailClient(EmailClient):
    def __init__(self, *, host, port, from_address, default_reply_to: str | None = None, timeout_seconds=10.0): ...
```

In `send`, the effective value is `reply_to if reply_to is not None else self._default_reply_to`:

- `ResendEmailClient.send` — adds `"reply_to"` to the JSON payload when the
  effective value is not `None` (string, as today; Resend accepts a string or a
  list and nothing here needs a list).
- `SmtpEmailClient.send` — sets `msg["Reply-To"]` when the effective value is
  not `None`.

Why the client and not the six call sites:

- **Provider-agnostic.** The ticket says "emails sent via resend", but the
  Resend/SMTP split is a transport choice, not a product one. Applying the
  default in both clients means a dev looking at Mailpit sees the header
  production will send, and `test_email.py` can assert the behaviour against
  both fakes.
- **Nothing to remember.** Every existing caller (`password_reset_service`,
  `email_verification_service`, `email_change_service`, `feedback_service`,
  `report_service`, `admin_invites`) passes no `reply_to` and needs no edit;
  every future caller gets the default for free.

**An explicit `reply_to` always wins.** `routers/contact.py` passes the
submitter's address so the maintainer's reply reaches the person who wrote
(NEU-1164 §3.2); that must keep working unchanged. The rule is "caller value if
given, else the configured default, else no header" — never a merge of the two.

The `EmailClient.send` abstract signature and `factory.send_email` are
unchanged: the default lives in the constructed client, not in the module-level
helper's parameters.

## 3. Documentation

- `.env.example` currently documents no email variables at all. Add a short
  email block there — `EMAIL_PROVIDER`, `EMAIL_FROM_ADDRESS`,
  `EMAIL_REPLY_TO_ADDRESS`, `RESEND_API_KEY`, `SMTP_HOST`, `SMTP_PORT` — in the
  file's existing comment style, with `EMAIL_REPLY_TO_ADDRESS=` left blank and a
  one-line note that it is the mailbox a user reaches by replying and that unset
  means no header.
- The `config.py` comment block above the email settings gains one sentence
  for the new field.
- `.claude/CLAUDE.md`: no new section. If the email module is mentioned where
  settings are listed, add the variable there; otherwise leave it.

## 4. Tests (`tests/unit/test_email.py`)

- `build_email_client` threads `EMAIL_REPLY_TO_ADDRESS` into the Resend client
  and into the SMTP client (assert the constructed client's default).
- Resend: with a default and no caller value, the payload carries
  `"reply_to": <default>`; with a caller value, the payload carries the caller
  value; with neither, the key is absent. The existing
  `test_resend_send_posts_expected_payload` (no default, no caller value) must
  still pass unchanged.
- SMTP: the same three cases against the captured `EmailMessage`'s `Reply-To`.
- No integration test changes: `tests/conftest.py:_stub_outbound_email`
  replaces `send_email` at every call site, so nothing there reaches a client.

## 5. What this does not do

- Does not validate the address format, does not verify the mailbox receives,
  and does not touch `EMAIL_FROM_ADDRESS` or its default.
- Does not change any email template, subject or body.
- Does not fail startup when the variable is unset under `EMAIL_PROVIDER=resend`
  (considered; rejected in favour of the optional shape above).
- Does not add a per-email override beyond the `reply_to` parameter that
  already exists.

## 6. Acceptance criteria

1. With `EMAIL_REPLY_TO_ADDRESS` set and `EMAIL_PROVIDER=resend`, every email
   sent through `send_email` without a `reply_to` argument POSTs to Resend with
   `reply_to` equal to the configured value.
2. With `EMAIL_PROVIDER=smtp` and the variable set, the same emails arrive in
   Mailpit with a `Reply-To` header equal to the configured value.
3. `POST /contact` still sends with `Reply-To` set to the submitter's address,
   whether or not the default is configured.
4. With the variable unset, every email is byte-for-byte what it is today: no
   `reply_to` key in the Resend payload, no `Reply-To` header over SMTP.
5. `task test`, `task lint` and `task typecheck` pass, with the new unit cases in
   `tests/unit/test_email.py`.
