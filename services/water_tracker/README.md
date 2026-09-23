# Water Tracker

The first independently deployable Home Platform application service. It is a
small multi-user drink log with one SQLite database of its own.

## Drink classification contract

Every drink stores an amount, timestamp, owner, temperature, and one stable
category code:

`water`, `supplement_water`, `coffee`, `tea`, `milk`, `juice`, `soft_drink`,
`sports_drink`, `alcohol`, or `other`.

The API accepts the code in `drink_type` and returns it on each entry. Omitting
the field remains backwards-compatible and means `water`. Unknown values are
rejected. Human labels belong in the UI; analysis and storage use stable codes
so a later wording change does not rewrite historical data.

Temperature uses the stable codes `hot`, `normal`, and `iced`; omitted values
remain backwards-compatible and mean `normal`. Coffee and tea may additionally
store one sweetness marker: `none`, `less`, `regular`, or `extra`. Sweetness is
nullable for old entries and drinks where it does not apply, and the API rejects
a sweetness value on any category other than coffee or tea.

`total_ml` is literal beverage volume, not a medical hydration estimate. Today
and history responses also contain `breakdown_ml`, keyed by category, so future
analysis can make an explicit decision about caffeine, alcohol, sugar, or other
weighting rather than baking an unsupported assumption into raw data.

Version `0.2.1` replaced the original `sparkling_water` code with
`supplement_water`. Startup migrates any earlier rows before recreating the
database validation triggers. Version `0.3.0` adds temperature and optional
tea/coffee sweetness; existing entries migrate to `normal` with no inferred
sweetness.

## Local development

```bash
WATER_TRACKER_ALLOW_DEV_IDENTITY=true \
  pixi run uvicorn services.water_tracker.main:app \
    --reload --host 127.0.0.1 --port 8100
```

Open `http://127.0.0.1:8100/` through a client that sends
`X-Water-Tracker-Dev-User`. This bypass is deliberately disabled in the
container and must never be enabled in production.

## Production shape

- container port: `8100`, published only on Pi loopback;
- private external path: `/water` through Tailscale Serve;
- authentication: `Tailscale-User-Login` added by Tailscale Serve;
- owner identity: linked Home Platform UUID resolved through the narrow
  control-plane identity endpoint;
- state: `/var/lib/home-platform/water-tracker/water-tracker.db`;
- health: `/health`; readiness: `/ready`.

The service never accepts a user ID in an API payload. A user first links the
trusted proxy identity to an existing Home Platform account in Job Desk. Every
row is then scoped to that stable account UUID; an unlinked or disabled account
fails closed.
