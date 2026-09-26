# Transport dashboard

This is a private, deliberately small transport dashboard with two cards:

- today's scheduled last train, selected by line, direction, and station; and
- live arrival minutes for a short owner-scoped list of saved bus-stop and
  service pairs.

Neither card polls. Opening the page reads only local SQLite state. The train
card calls DataMall only when **Refresh timetable** is pressed, then imports
the downloaded GTFS schedule into SQLite. The bus card calls Bus Arrival v3
only when **Refresh arrivals** is pressed, once per unique saved stop rather
than once per saved service.

## Development

The repository-root `.env` may contain `LTA_DATAMALL_KEY` for local
development. The file is ignored by Git. Production uses
`TRANSPORT_DATAMALL_KEY_FILE` so the credential is mounted as a read-only
secret rather than exposed in Compose configuration.

```bash
pixi run transport-dev
```

Then browse to `http://127.0.0.1:8102/` while supplying the development
identity header. Normal browser access is expected to go through the private
reverse proxy; direct requests without identity are rejected.

The train result is the scheduled arrival time from the GTFS feed. A service
time after `24:00` is displayed as the following calendar day while remaining
part of today's operating schedule.
