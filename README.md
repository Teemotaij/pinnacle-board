# pinnacle-board

A five-minute mirror of Pinnacle's **public** pre-match tennis moneyline board, kept as CSV.

Everything here is odds anyone can read on pinnacle.com without logging in. There is no model, no
strategy, no stake and no result in this repository, and nothing here authenticates or places a bet.
The `x-api-key` in `poll.py` is the guest key shipped in Pinnacle's own frontend bundle and sent by
every anonymous visitor to the site; it is not a credential.

## Files

| Path | What it is |
|---|---|
| `board/pin_open.csv` | The **first** price ever seen on a matchup. Insert-only: once a pair is on file its price columns never change. |
| `board/pin_close.csv` | The **latest** pre-match price. Overwritten every poll, so the last surviving write is the last price before play. |
| `board/ticks/YYYY-MM-DD.csv` | Append-only, one row each time a price actually moved. |
| `board/archive/pin_close_YYYY-MM.csv` | Closes for matches more than two days past, moved out to keep the live file small. |

`fair1`/`fair2` are devigged probabilities (odds-ratio method); `quote1`/`quote2` are the raw implied
probabilities including margin. `limit_usd` is Pinnacle's max risk stake. `seen_at` is when this
repository fetched the row; `obs_at` subtracts the CDN `Age` header, so it is an upper bound on how
recently Pinnacle itself produced the number.

## Why open and close are separate files

An *open* is a claim about the earliest price; a *close* is a claim about the latest. One table updated
in place would walk the open forward into the current line and destroy the earlier number, which cannot
be recovered afterwards. So the two are written by opposite rules, and the gap between them is the line
movement.

## Freshness

Pinnacle is served through Cloudflare with `Cache-Control: max-age=902`, so any one URL can be ~15
minutes stale and polling faster returns identical cached bytes. `max-age` applies per cache key, so
`poll.py` fetches two independently-phased query-string variants of the same endpoint and keeps the
higher `version` per matchup — `version` is a monotonic counter, so the higher reading is exactly the
later one. That brings expected staleness to about five minutes, which is why the cron is five minutes
and no faster. The measurements behind all of this are in the `poll.py` docstring.

## Running it

```sh
python3 poll.py
```

Standard library only. No dependencies, no configuration, no environment variables.
