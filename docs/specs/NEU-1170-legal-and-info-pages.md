# NEU-1170 — Frontend: legal and informational pages

**Ticket:** [NEU-1170](https://linear.app/neuroticsasquatch/issue/NEU-1170/frontend-terms-privacy-about-and-contact-pages)
**Parent:** [NEU-1155](https://linear.app/neuroticsasquatch/issue/NEU-1155/legal-and-informational-pages) · **Project:** TVBF: Open Registration · Milestone 3, Launch switch
**Blocked by:** [NEU-1164](https://linear.app/neuroticsasquatch/issue/NEU-1164/backend-contact-form-endpoint) ✅ Done · [NEU-1158](https://linear.app/neuroticsasquatch/issue/NEU-1158/drop-watch-archive-pii-before-open-registration) ✅ Done
**Repo:** `tvbf-frontend` only. The backend half is NEU-1164, which shipped `POST /contact`.
**Written:** 2026-08-21

Four unauthenticated pages — terms of service, privacy policy, about, and contact — reachable
without logging in. This is the public-facing legal and informational surface that lets a stranger
decide whether to sign up.

---

## 1. Routes

Four new routes in `router.tsx`, outside `<RequireAuth>` alongside the existing auth pages
(`/login`, `/signup`, `/verify-email`, etc.):

| Path | Component | Description |
|---|---|---|
| `/terms` | `<TermsPage />` | Terms of Service |
| `/privacy` | `<PrivacyPage />` | Privacy Policy |
| `/about` | `<AboutPage />` | Product description |
| `/contact` | `<ContactPage />` | Contact form |

All four render inside `<AppShell />` (so they get the header and footer) but with no session
requirement. This matches the existing pattern — the nav links, search box, email banner, and
mobile bottom nav are all gated behind `user &&`, so an unauthenticated visitor sees only the
header logo, the page content, and the footer.

There is no index-route redirect to `/about` — the four pages are reachable from the footer and
signup form as specified below.

### 1.1 Pages directory

New files under `src/pages/`:

```
src/pages/
  TermsPage.tsx
  PrivacyPage.tsx
  AboutPage.tsx
  ContactPage.tsx
```

Each is a simple functional component rendering prose (or a form, for contact) with a "last
updated" date.

---

## 2. Page content

### 2.1 Terms of Service (`/terms`)

Lightweight terms covering:

- **Account responsibilities** — don't abuse the service, don't impersonate others
- **Intellectual property** — your watch data is yours; our code, design, and catalog data
  (via TMDB) are not
- **Limitation of liability** — the service is provided as-is; no warranty
- **Governing law** — Maryland, USA
- **Termination** — we may suspend or terminate accounts for violations

### 2.2 Privacy Policy (`/privacy`)

Lightweight policy covering the six points NEU-1155 requires:

- **Session cookies** — httpOnly session cookie scoped to the app domain; CSRF token returned
  in the response body and required on mutating requests
- **What friends can see** — friend-scoped activity feed (watches, ratings), the per-show
  `hide_from_activity` toggle, and the `activity_feed_enabled` master switch
- **Data export** — `GET /me/export` lets you download your data
- **Account deletion** — `DELETE /me` removes your account and associated data. NEU-1158 has
  shipped, so this claim is truthful — no PII survives deletion.
- **Sentry error reporting** — automatic crash reporting; no personal data is intentionally
  included
- **Third-party data sources** — TMDB (show catalog, images, credits) and TVmaze (airdate
  corrections, used under CC BY-SA 4.0)

### 2.3 About (`/about`)

Short product description:

- What TV BingeFriend is — track TV watching with friends, see what they're watching,
  get recommendations
- That it's a solo project by Tom Boone
- The release line: "A neuroticsasquat.ch release." (matches the footer)

### 2.4 Contact (`/contact`)

A form that POSTs to the NEU-1164 endpoint (`POST /contact`). Three fields:

- **Name** — text input, required, max 100 characters
- **Email** — email input, required, max 254 characters
- **Message** — textarea, required, max 5000 characters

Plus a Turnstile widget (when `env.turnstileSiteKey` is non-empty), reusing the same patterns
as `SignupPage.tsx`:

- `captchaBlocked` flag — submit button is `aria-disabled` when Turnstile is enabled but no
  token has been provided
- On any error during submit, the widget remounts (`captchaNonce` increment)
- The three detail tokens from NEU-1164 §1.3: `captcha_required`, `captcha_invalid`,
  `captcha_unavailable`

The endpoint returns 204 on success (NEU-1164 §1.3), so the page shows a success message
("Message sent — we'll get back to you.") and clears the form. A 429 shows the same
rate-limited state as the signup page.

No `mailto:` fallback — the form is the only contact path.

---

## 3. Footer links

A new block between the attribution block and the publisher block in `AppShell.tsx`:

```
[TMDB logo + sentence]    [Terms  Privacy  About  Contact]    [© TV BingeFriend + neuroticsasquat.ch]
[TVmaze CC BY-SA credit]
```

Four links in a row, separated by a visual separator (e.g., `·` or a gap):

```
Terms · Privacy · About · Contact
```

Layout behaviour:

- **Below `lg` (~1024px):** the link row wraps and stays center-aligned, matching the
  existing stacked/centered mobile layout. The attribution block's comment-block reasoning
  is preserved — the TVmaze credit stays on its own line so it does not read as a qualifier
  on any other attribution.
- **At `lg` and up:** the three blocks sit in one flex row.

---

## 4. Signup form link

Below the submit button on `SignupPage.tsx`, before the "Already have an account?" line:

> By signing up, you agree to our [Terms of Service](/terms) and [Privacy Policy](/privacy).

The two links use React Router `<Link>` components (not `<a>`) so navigation is client-side.

---

## 5. Shared page shell

All four pages share the same visual shell: a centered `<article>` with `max-w-prose` (or
equivalent), a `<h1>` title, the prose/contact body, and a `<time>` element for the "last
updated" date at the bottom. The date is `2026-08-21` on all four pages.

Create a reusable `<LegalPage>` wrapper or keep each page self-contained — either approach is
fine as long as the four pages look consistent.

---

## 6. Operator identity

Legal documents (terms, privacy) name the operator as:

> **Tom Boone d/b/a [neuroticsasquat.ch](https://neuroticsasquat.ch)** (Maryland)

The about page refers to "Tom Boone" as the solo developer.

---

## 7. Out of scope

- **Changing the `/` route for unauthenticated users.** The four pages are reachable from the
  footer and signup form only; no landing-page redirect.
- **Backend work.** NEU-1164 shipped `POST /contact`; this ticket is frontend only.
- **`mailto:` fallback.** The contact form is the only contact path.
- **Internationalisation.** All content is English only.

---

## 8. Acceptance criteria

1. `/terms`, `/privacy`, `/about`, and `/contact` are reachable without authentication.
2. The four footer links appear between the attribution block and the publisher block in
   `AppShell.tsx`, and the stacking layout on narrow screens does not break the TVmaze
   credit's single-line isolation.
3. "By signing up, you agree to our Terms of Service and Privacy Policy" appears below the
   submit button on the signup form, with client-side `<Link>` navigation.
4. Each page carries a visible "Last updated: 2026-08-21" date.
5. The operator is named as **Tom Boone d/b/a neuroticsasquat.ch (Maryland)** in the terms
   and privacy policy.
6. The privacy policy describes the six items in §2.2.
7. The contact form POSTs to `/contact` with the three fields plus a Turnstile token, reusing
   the signup page's Turnstile patterns.
8. `task test`, `task lint`, `task typecheck` green in the frontend repo.
