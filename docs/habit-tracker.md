# Habit Tracker

Habit Tracker is one application with three connected pages: Overview, Water,
and Budget. They share the same user identity, SQLite database, container,
backup, and navigation. It is independent of the compute control plane.

## Pages and goals

| Page | Purpose |
|---|---|
| `/habits/` | Overview: a pixel-art cup for today's drink progress and a piggy bank for savings progress |
| `/habits/water` | Log drinks, edit the daily volume goal, and inspect daily/category history |
| `/habits/budget` | Record spending or fund movements, change the daily allowance, and inspect history and the ledger |

The graphics use lightweight SVG with integer pixel shapes; their fills come
from stored data. The overview refreshes when returning to the page and at the
next local midnight. Empty, completed, and over-goal states retain the actual
numeric totals while visual fills stay between zero and full.

Water progress is total logged beverage volume divided by the daily volume
goal. It resets with the local day. All beverage categories count as their raw
volume, matching the Water page; this is not a clinical hydration estimate.

The piggy bank uses the available sinking-fund balance divided by an optional
savings target. Click **Set a savings goal** or the current target on Overview
to set, edit, or remove it. No target is invented for an existing account.
Savings carry across days; a negative balance remains visible even though the
graphic is empty. Today's positive surplus stays pending and does not fill the
piggy bank until the day closes. Changing a savings target does not move money
or change the daily allowance.

## Budget accounting

New accounts start with S$10 per day. Every real allowance adjustment records
the previous amount, new amount, timestamp, and effective date. The effective
budget applies from that day and carries forward. Multiple edits on one day
remain separate audit events, while the day's final effective allowance drives
the daily balance. An intentional zero allowance survives restarts.

| Entry | Effect |
|---|---|
| Daily spending | Reduces today's remaining allowance |
| Completed-day settlement | Adds allowance minus spending to the sinking fund |
| Current-day overage | Reduces the available fund immediately |
| Fund contribution | Adds directly to the fund |
| Fund redemption | Deducts directly from the fund without consuming today's allowance |
| Budget adjustment | Changes allowance; shown in the ledger, not added as a separate fund credit |
| Savings target change | Changes the visual target; does not affect any balance |

Settlement is derived from the historical daily allowance and transactions.
The remaining-allowance meter on Budget starts full and drains as spending
increases. Money uses integer SGD cents. The default day boundary is midnight
in `Asia/Singapore`.

## Persisted data and analysis

| Data | Retained fields |
|---|---|
| Drinks | ID, owner, amount in ml, stable category, temperature, optional sweetness, UTC consumption time |
| Daily allowances | Owner, effective date, exact amount in cents, change timestamp |
| Allowance audit | Event ID, owner, effective date, old/new amounts, UTC occurrence time; delta is derived |
| Money transactions | ID, owner, kind, integer cents, description, UTC occurrence time |
| Savings-target history | Sequence, owner, previous/new targets (nullable), currency, UTC occurrence time |

Savings-target edits are appended and unchanged saves create no duplicate
event. Goal history survives removal of a current target. Allowance updates and
target updates serialize their read/write transaction to preserve the correct
previous value under simultaneous requests.

Existing limitations matter for analysis: transaction deletion is permanent
and recalculates derived balances; there is no complete transaction reversal
audit or export UI yet. Water goals currently store a current setting, so the
water history does not preserve every past goal edit. Historical budget
allowances and their adjustment events are preserved. Old allowance edits
overwritten before the audit feature existed cannot be reconstructed.

Drink category codes are `water`, `supplement_water`, `coffee`, `tea`, `milk`,
`juice`, `soft_drink`, `sports_drink`, `alcohol`, and `other`. Temperature is
`hot`, `normal`, or `iced`. Only tea and coffee accept sweetness: `none`,
`less`, `regular`, or `extra`. API validation and database triggers enforce the
drink vocabulary. Older drink records retain migration defaults rather than
invented classifications.

## API and ownership

Paths below are relative to the external `/habits` prefix.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/water/today` | Today's raw volume, goal, category totals, entries |
| POST / DELETE | `/api/water/drinks` / `/api/water/drinks/{id}` | Record/remove an owned drink |
| GET / PUT | `/api/water/settings` | Daily volume goal |
| GET | `/api/water/history` | Daily volume history |
| GET | `/api/budget/summary` | Daily allowance, remaining amount, fund, savings target |
| PUT | `/api/budget/daily-budget` | Set allowance from today with adjustment audit |
| PUT | `/api/budget/savings-goal` | Set positive `target_cents`, or `null` to remove the target |
| POST / DELETE | `/api/budget/transactions` / `/api/budget/transactions/{id}` | Record/remove an owned money entry |
| GET | `/api/budget/history` | Exact effective allowance and spending by day |
| GET | `/api/budget/ledger` | Daily settlements, fund movements, allowance adjustments |

User pages and data routes require the linked proxy identity. Callers never
supply an owner ID. Static assets contain no private data and are served without
identity; `/health`, `/ready`, and `/version` are diagnostic routes. Legacy
`/api/today`, `/api/drinks`, `/api/settings`, and `/api/history` remain aliases to
the same protected Water handlers for older clients.

See the [source map](../services/habit_tracker/README.md),
[configuration](configuration.md#habit-tracker), and
[application hosting pattern](services.md).
