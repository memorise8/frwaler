# E-CIP Frontend MVP

This is a standalone, dependency-free static dashboard prototype. It always builds for the `/` route and does not import or contact the existing application, E-CIP, or any data system.

## Local commands

```bash
npm run check
npm run build
npm run dev
```

`npm run dev` serves the generated `dist/` directory on loopback only. Run the build command first.

## Fixture and handoff boundary

All view data belongs in `src/fixtures/dashboard-fixture.mjs`; it is intentionally synthetic and non-sensitive. The fixture already reserves a `도서 소개` material-overview field for the future dashboard.

`src/config/handoff-config.mjs` keeps an optional future operator handoff URL representation. Its shipped value is blank, so the prototype renders no external anchor and performs no network work.

## Registration placeholders

Before registration, obtain written confirmation of:

- the destination route or base-path requirement;
- approved data contract and refresh responsibility;
- visual identity and accessibility requirements;
- asset-hosting, security, and deployment ownership;
- whether any handoff should be a link, API integration, or another operator-approved method.
