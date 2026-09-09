# Frontend

React 18 + TypeScript + Vite 6 + Tailwind 3. Deliberately the same stack,
file layout and visual language as the sibling
[private-document-assistant](../../private-document-assistant/frontend)
frontend, so the two products read as a matched pair - what differs is the
domain (databases instead of documents) and every word of copy.

```bash
npm install
npm run dev      # http://localhost:5173, proxies /api -> http://localhost:8000
npm run build    # tsc -b && vite build -> dist/
```

## Layout

```
src/
  main.tsx            ThemeProvider > BrowserRouter > App
  App.tsx             routes: / /login /signup /forgot-password /reset-password /app
  index.css           Tailwind entry + the base body colors
  lib/
    config.ts         apiUrl() - VITE_API_BASE_URL, blank = relative /api/*
    auth.ts           access/refresh token storage (localStorage)
    api.ts            fetch wrapper: JSON, Bearer header, 401 -> refresh -> retry once
                      (get / post / patch / del / uploadFile)
    types.ts          mirrors of the backend response models
    chart.ts          asChartSpec() validation + numeric coercion for chart values
    chatStream.ts     the SSE consumer (fetch + ReadableStream, NOT EventSource)
    engines.ts        per-engine label / icon / badge / default port / extra_params key
    theme.tsx         light | dark | system, persisted, Tailwind `dark` class strategy
    passwordPolicy.ts client-side mirror of the backend's password rules
    avatar.ts         initials for the user's message avatar
    useConfigStatus.ts  GET /api/config/status, once
  components/
    AuthLayout.tsx        the centered card the four auth pages share
    ProtectedRoute.tsx    redirect to /login when there's no stored token
    ThemeToggle.tsx       light/dark/system segmented control
    MarkdownMessage.tsx   react-markdown + remark-gfm for assistant replies
    ChartAdapter.tsx      one `chart` event / `chart_spec` -> a Chart.js bar/line/pie
    QueryResultTable.tsx  columns + rows as a scrollable table
    ConnectionsModal.tsx  "My databases": list, test, re-index, delete, add
  pages/
    HomePage.tsx          landing page, config-status aware
    LoginPage.tsx SignupPage.tsx ForgotPasswordPage.tsx ResetPasswordPage.tsx
    AppShellPage.tsx      the app: chat list, transcript, database selector, composer
```

## Three things worth knowing

**The SSE stream is read manually.** `POST /api/chats/{id}/messages`
returns `text/event-stream` and is bearer-token protected, but the browser
`EventSource` API only does GET and cannot attach an `Authorization`
header - so `lib/chatStream.ts` uses `fetch` plus a `ReadableStream`
reader and splits events on `\n\n` itself. Its event set is
`token` / `query` / `query_result` / `chart` / `done` / `error`; a
`query_result` with `ok: false` is a live status update, **not** the end of
the turn (the model is handed the error and explains it in its next
tokens), so the reader keeps going.

**Charts arrive as query results, not as chart config.** A `chart` event
carries `{chart_type, title, x_field, y_field, columns, rows}` - the raw
result of the query that just ran, plus the names of the two columns to
plot. `ChartAdapter.tsx` builds Chart.js's `{labels, datasets}` from that
itself, which is why "show the data" underneath a chart is free: the rows
are already on the client. A field name that doesn't match any column
falls back to rendering the table rather than throwing.

**`PATCH /api/chats/{id}` keys off presence, not value.** The database
selector in the chat header sends `{"connection_id": 7}` or
`{"connection_id": null}` and nothing else; renaming sends `{"title": ...}`
and nothing else. Omitting a key leaves that field alone - which is why
`lib/api.ts` passes the caller's body through verbatim instead of
assembling one.

## Configuration

`VITE_API_BASE_URL` (Vite inlines it at **build** time):

- **dev** - leave it blank (see `.env`); calls go to a relative `/api/...`
  and the dev server proxies them to `http://localhost:8000`.
- **docker-compose** - passed as a build **arg** (see the root
  `docker-compose.yml` and this directory's `Dockerfile`), defaulting to
  `http://localhost:8000`, because the browser talks to the backend
  directly and the frontend is served from a different origin (`:4173`).

## Config-status gating

`GET /api/config/status` drives three pieces of UI, the same way the
sibling project's `rag` flag does:

| Flag | When false |
|---|---|
| `connections_llm` | Composer disabled with a "Configuration missing" banner; "Add database" and "Re-index" disabled |
| `encryption` | "Add database" disabled, with the reason (a password can't be stored without `ENCRYPTION_KEY`) |
| `smtp` | Forgot-password reports that email sending isn't configured |
