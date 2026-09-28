# Habit Tracker

One household application with Overview, Water, Budget, and Study pages. It owns
one container, authentication boundary, SQLite database, and backup lifecycle.

The hosting pattern is
[Application services](../../docs/services.md). The feature reference,
data model, API routes, and deployment-specific paths live in the ignored
`local/docs/habit-tracker.md` runbook.

## Source map

```text
services/habit_tracker/
  main.py                 app factory, identity, page rendering, API routes
  water.py                drink persistence, classification, daily water goals
  budget.py               spending, daily allowances, savings targets, ledger
  study.py                named focus sessions and daily study totals
  templates/
    dashboard.html        overview with cup, piggy bank, and reading owl art
    water.html            drink logging, goal calendar, and drink mix
    budget.html           spending, sinking fund, and ledger
    study.html            focus countdown, recent sessions, and daily calendar
  static/
    shell.css             shared page frame and navigation
    dashboard.css / .js   overview layout and progress rendering
    water.css / .js       water page
    budget.css / .js      budget page
    study.css / .js       study page
  tests/                  app, data, migration, and authorization checks
  Dockerfile              application image
  compose.yaml            one application container
```

## Development

```bash
pixi run habits-dev
```

This runs on loopback port 8100, with an empty external base path for local
development. Supply `X-Habit-Tracker-Dev-User: demo@example.test` using a test
client or browser development header. For example:

```bash
curl -H 'X-Habit-Tracker-Dev-User: demo@example.test' \
  http://127.0.0.1:8100/api/budget/summary
```

Local pages are `/`, `/water`, `/budget`, and `/study`. Production uses the
`/habits`
prefix and disables development identity. The only environment prefix is
`HABIT_TRACKER_`; see
[Configuration](../../docs/configuration.md#habit-tracker).

```bash
pixi run check
git diff --check
```

## Deployment naming

- Python package: `services.habit_tracker`
- Image/container/systemd service: `home-platform-habit-tracker`
- Compose project: `home-platform-habits`
- Backup service/timer: `home-platform-habit-backup`
- Private base route: `/habits`
- Database filename: `habit-tracker.db`

The host state and identity-token paths are supplied to Compose by the private
deployment environment. The source rename does not move or recreate live data.
The unnamespaced water API routes (`/api/today`, `/api/drinks`,
`/api/settings`, `/api/history`) remain compatibility aliases. New UI code uses
`/habits/api/water/...`.
