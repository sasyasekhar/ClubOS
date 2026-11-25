# ClubOS frontend design system

The [workspace showcase](http://127.0.0.1:8000/dev/design) and [authentication preview](http://127.0.0.1:8000/dev/design/auth) use synthetic records. They are available to a loopback client using a loopback hostname with `APP_ENV=development`. Run the preview on a loopback bind with proxy headers disabled. The routes return 404 in production and test environments, stay out of OpenAPI, and send a no-store policy. A preview-specific content security policy restricts assets and requests to the current origin and blocks native form submissions.

## Build and preview

Use Python 3.14 with the existing `.venv`, or create it with `python3.14 -m venv .venv` and run `python -m pip install -r requirements-dev.txt` after activation. Node.js 24.22.3 with its bundled npm is pinned in `.nvmrc`. Install it to rebuild or run the browser suite. The committed stylesheet lets FastAPI serve the preview with Python alone.

```sh
source .venv/bin/activate
npm ci
npm run build
```

From the repository root, serve the preview with the following command. The placeholder password satisfies settings validation because the preview never connects to PostgreSQL.

```sh
APP_ENV=development DB_PASSWORD=design-fixtures-only python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Port 8000 is for the interactive preview. The Playwright suite starts a separate loopback server on 8177. Linux may need `npx playwright install --with-deps chromium firefox webkit` to install the browsers and system libraries.

```sh
npx playwright install chromium firefox webkit
npm run test:frontend
```

On macOS 27, if bundled Firefox reports a missing profile, run the suite with a disposable browser data directory. [Playwright Firefox issue](https://github.com/microsoft/playwright/issues/42768)

```sh
firefox_test_data=$(mktemp -d /tmp/clubos-firefox.XXXXXX)
CFFIXED_USER_HOME="$firefox_test_data" npm run test:frontend
```

## Structure and customization

| Location | Responsibility |
| --- | --- |
| `app/templates/layouts/base.html` | Shared document metadata, static entry points and skip link |
| `app/templates/layouts/auth.html` | Authentication form area and decorative school-club line art |
| `app/templates/layouts/dashboard.html` | Shared workspace with school, navigation, academic year, notifications, profile and persistent status region |
| `app/templates/components/ui.html` | Shared controls, form/error associations, status, panels, dialogs, pagination, skeletons and profile disclosure |
| `app/templates/components/navigation.html` | Sidebar and breadcrumb macros |
| `app/templates/components/design-*.html`, `app/templates/pages/design*.html` | Synthetic examples and page composition |
| `app/templates/errors/` | Permission and session-expiry specimens |
| `frontend/clubos.css` | Tailwind theme, base rules, shared component styles and responsive layout |
| `app/static/css/clubos.css` | Compiled stylesheet, rebuilt and checked in CI |
| `app/static/js/core/` | Same-origin requests and shared shell behavior |
| `app/static/js/components/` | Validation, confirmation, account search, pagination and impersonation banner behavior |
| `app/static/js/pages/` | Preview-only wiring and synthetic adapters |
| `app/static/fonts/`, `app/static/icons/`, `app/static/artwork/` | Local fonts and licenses, functional icon sprite and original auth illustration |

Extend a layout and fill its blocks from a page template. Pass navigation and authorized profile options from the page’s server-side context. Change styles in `frontend/clubos.css`, rebuild the stylesheet and keep the matching compiled file in the same commit. Tailwind scans templates and static JavaScript explicitly. Write full utility class names where Tailwind needs to discover them.

## Modern Academia tokens

| Token | Value | Use |
| --- | --- | --- |
| Background | `#F4EBDD` | Main parchment canvas |
| Sidebar | `#E8DCC5` | Soft sand navigation and table header |
| Primary | `#365747` | Forest actions and selection edge |
| Accent | `#CBA052` | Academic-year marker and colour specimen |
| Surface | `#FFF9EE` | Form, dialog and panel background |
| Selection | `#E1EBDD` | Current navigation and subtle success background |
| Ink / muted | `#252C24` / `#4F5A48` | Normal and secondary text |
| Dark forest / focus | `#263F33` / `#2C523E` | Emphasized text, pressed primary and focus outline |
| Rule / control | `#CFC1AA` / `#7A7364` | Subtle separators and functional control edges |
| Placeholder | `#E4DACA` | Static skeleton shapes |
| Error | `#832E29` on `#F7E2DB` | Error text, background and destructive actions. Pressed shade `#692421` |
| Warning | `#65490F` on `#F5E7C7` | Warning text and background |
| Success | `#294B36` on `#E1EBDD` | Success text and background |

Quicksand is used for headings, navigation and short labels. Nunito Sans is used for body text and controls. The main heading scales from 28–32px, section headings from 20–24px, body text is 16px and supporting text is 13–14px. Each font has a system fallback. The Latin variable WOFF2 files are vendored from Fontsource 5.3.0 with their SIL Open Font License notices. Characters outside the subset use the next available system font. The functional SVG sprite and auth artwork were drawn for this checkpoint.

Spacing starts at 4px. Panels and controls have a default radius of 10px. Buttons and field boxes provide at least 44px of height. Actions typically have a 12px gap. Focus uses an offset 3px forest outline. Wide tables scroll in a labeled region, and the workspace switches to a modal navigation drawer below 1024px.

The ratios below use the WCAG 2.2 relative luminance formula. [WCAG 2.2](https://www.w3.org/TR/WCAG22/#dfn-contrast-ratio)

| Foreground and background | Ratio |
| --- | --- |
| Charcoal on cream / parchment / sand / sage | 13.68 / 12.14 / 10.57 / 11.70 |
| Muted text on cream / parchment / sand / sage | 6.94 / 6.15 / 5.36 / 5.93 |
| Dark forest on cream / parchment / sand / sage | 10.88 / 9.65 / 8.40 / 9.30 |
| Cream text on primary / destructive | 7.67 / 8.39 |
| Error, warning and success text on corresponding surfaces | 7.06 / 6.82 / 7.95 |
| Control border against cream / parchment / sand / sage | 4.49 / 3.98 / 3.47 / 3.84 |
| Focus outline against cream / parchment / sand / sage / warning | 8.41 / 7.46 / 6.49 / 7.19 / 7.19 |

Text pairs above exceed 4.5:1 and listed control/focus pairs exceed 3:1. Gold marks the academic year decoratively. Status always has a text label.

## Workflow contracts

- `validateFields` updates linked inline errors, focuses the first invalid field and preserves values. Apply backend validation independently.
- `confirmation` creates one operation key when a user opens a new review and retains that key through every attempt and reconciliation. An adapter returns confirmed, definitely rejected or uncertain. It can submit once and reconcile with a supported status/read operation or the documented same-key replay guarantee. There is no automatic retry. This checkpoint’s activity adapter simulates losing the original response and looks up its receipt by key.
- Account search is debounced, cancels superseded requests, ignores late responses and consumes bounded pages. The demo queries a server endpoint with fictional records from one school. Live integration must use `/admin/impersonation/eligible-accounts` and its `q`, `limit` and UUID `after` cursor. The current API exposes account ID, full name and role. Review a safe username field before using usernames for live disambiguation.
- The banner consumes an absolute `expires_at` timestamp. Its fixture response supplies that timestamp once, and tab storage retains it across preview reloads. A live workspace must obtain current state from the backend, use its timestamp and call the backend end operation. The countdown is a display of expiry. Backend permission checks remain authoritative.
- Students have read-only access while represented. Teachers can perform authorized activity reviews. Neither gains administrative actions or changes to personal information or credentials. The workspace links to other component examples for review purposes and applies no real permissions.

## Authentication integration

The existing `app/static/js/session.js` remains the entry point for authenticated requests. It uses the shared same-origin transport. The login API takes `identifier` and `password`, responds with an account and tab key, and sets a HttpOnly, SameSite=Lax session cookie with Secure required in production. Only the tab key goes in per-tab `sessionStorage`. The client sends `X-ClubOS-Tab` with the cookie on authenticated requests. A 401 clears the tab binding. Handle permission denials separately. Link sign-out to the explicit logout request. The existing heartbeat runs in a visible, recently active tab, and PostgreSQL remains authoritative for the 12-hour idle timeout. Tab closure has no reliable logout signal.

Keep mutation requests on the same origin and keep the request security middleware enabled. Take the idempotency requirements from each endpoint, including UUID `Idempotency-Key` on impersonation start and impersonated reviews. Map status codes to safe messages and render request data as text. Scope account navigation and avatar sources to authorized data. Live sign-in, registration, role dashboards, notification persistence and impersonation integration remain decisions for later checkpoints.

## Accessibility review

The browser suite checks keyboard navigation, focus restoration, form error descriptions, reduced-motion media, axe WCAG tags, 200% text enlargement and reflow at 320, 390, 768, 1024 and 1440 CSS pixels. The stylesheet uses stationary skeletons. Add screen-reader checks for the navigation drawer, live results, announcements and table headers, and verify zoom and touch with representative devices before release. Automated scans and contrast calculations cover subsets of WCAG 2.2 AA.
