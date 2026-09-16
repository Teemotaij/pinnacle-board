"""Mirror Pinnacle's public tennis board to CSV. Standard library only, no state but the files.

WHY THIS REPOSITORY IS SEPARATE, AND PUBLIC
The book that consumes these prices is private, and must stay private: it contains the model, the rule
config and every stake and fill. But polling has to happen far oftener than the book needs to run, and
on GitHub Actions a private repository bills every minute while a public one does not. So the polling
is carved out into the smallest possible artefact — this file — which holds no model, no positions and
no results, only public odds that anyone can read on pinnacle.com without logging in. Someone who finds
this repository learns that a person mirrors tennis prices. That is the whole of the disclosure.

THE KEY BELOW IS NOT A CREDENTIAL
`x-api-key` is the guest key embedded in Pinnacle's own frontend bundle and sent by every anonymous
visitor to the site. It authenticates nothing, holds no account and cannot place a bet; it reads the
same public odds a logged-out browser sees. It is in the bundle precisely because it is not a secret.

WHAT THIS WRITES
  board/pin_open.csv           first price ever seen on a matchup, written once and never moved
  board/pin_close.csv          latest pre-match price, overwritten every poll
  board/ticks/YYYY-MM-DD.csv   an append-only row every time a price actually changed
  board/archive/pin_close_YYYY-MM.csv   closes for matches now well in the past

The split of open from close is the important one and is not cosmetic. An `open` is a claim about the
earliest price; a `close` is a claim about the latest. A single table that updated in place would
silently walk the open forward into the current line and destroy the only number the downstream model
is actually calibrated against. So `pin_open` is insert-only on every price column, and `pin_close`
upserts on every poll. They disagree by construction, and that disagreement is the line movement.

WHY THE SAME ENDPOINT IS FETCHED TWICE WITH DIFFERENT QUERY STRINGS
Pinnacle sits behind Cloudflare with `Cache-Control: public, max-age=902, must-revalidate`, so any one
URL serves a copy that can be up to ~15 minutes old, and polling that URL faster changes nothing at all
— measured here on 2026-09-16, nine polls fifteen seconds apart returned `cf-cache-status: HIT` every
time, an `Age` that simply counted up in lockstep with the wall clock, and not one changed `version`.

`max-age` applies per CACHE KEY, though, and the key includes the query string. So four spellings of the
same endpoint are four separate cached objects on four independently phased clocks — sampled in one
breath they read Age 58, 425, 709 and 852. Keeping the freshest reading per matchup makes staleness the
minimum of four draws on [0,905] rather than one, an expected ~181s instead of ~452s.

Four is all there is, and each boundary was established rather than assumed:
  ?primaryOnly=false                             normalises to the bare URL — same Age, same versions
  ?brandId=0, ?withSpecials=false&brandId=0      valid on the matchup LIST, but 204 No Content here
  any unrecognised parameter (?z=1)              204 No Content, so cache keys cannot be minted freely
  Accept-Encoding: gzip / identity / deflate     `Vary` advertises it, but Cloudflare normalises it and
                                                 all six spellings returned one identical Age
  Cache-Control: no-cache on the request         still HIT
  ?_=<timestamp>                                 204 No Content
~3 minutes is therefore the floor, which is why the workflow beside this file polls at five minutes and
no faster: a faster cron would mostly re-read bytes it already has.

"Fresher" is decided by `version`, not by `Age`. `version` is a monotonic counter Pinnacle stamps on
each market row, so the higher of two readings is the later one exactly, whereas `Age` is a property of
the whole HTTP response and only bounds how stale the rows inside it might be.
"""
from __future__ import annotations

import csv
import json
import os
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://guest.api.arcadia.pinnacle.com/0.1"
KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"  # public guest key — see the module docstring
SPORT_TENNIS = 33
TIMEOUT_S = 45
RETRIES = 3

ROOT = os.path.dirname(os.path.abspath(__file__))
BOARD = os.path.join(ROOT, "board")
OPEN_CSV = os.path.join(BOARD, "pin_open.csv")
CLOSE_CSV = os.path.join(BOARD, "pin_close.csv")
TICK_DIR = os.path.join(BOARD, "ticks")
ARCHIVE_DIR = os.path.join(BOARD, "archive")

# Keep the live close file small. It is rewritten on every poll, so its size is what this repository
# costs in git objects per run; everything older than this moves to a monthly archive that changes once
# a day. Two days rather than zero because a match is graded after it finishes, and the downstream book
# may not have run since.
ARCHIVE_AFTER_DAYS = 2

# The list endpoint carries the names, league and start time; the bulk market endpoint carries none of
# that, only matchupId and prices. Measured 2026-09-16: the bare URL is a strict SUPERSET of the other
# two — of a 58-matchup union it misses none, `?withSpecials=false` misses 2 and
# `?withSpecials=false&brandId=0` misses 3. The book's own poller was reading that last, narrowest one,
# which is why fixtures sometimes appeared later here than on the site.
#
# All three are kept anyway, for coverage rather than freshness: a matchup is worth having from whichever
# variant answers, and if the bare URL times out the other two still carry 55 of the 58. `?primaryOnly`
# is 403 on this path, though it is valid on the market path below.
LIST_VARIANTS = [
    f"sports/{SPORT_TENNIS}/matchups",
    f"sports/{SPORT_TENNIS}/matchups?withSpecials=false",
    f"sports/{SPORT_TENNIS}/matchups?withSpecials=false&brandId=0",
]

# The four independently cached market variants. See the module docstring for why there are exactly four
# and why no fifth can be minted.
MARKET_VARIANTS = [
    f"sports/{SPORT_TENNIS}/markets/straight",
    f"sports/{SPORT_TENNIS}/markets/straight?primaryOnly=true",
    f"sports/{SPORT_TENNIS}/markets/straight?withSpecials=false",
    f"sports/{SPORT_TENNIS}/markets/straight?primaryOnly=true&withSpecials=false",
]

OPEN_COLS = ["pair", "k1", "k2", "name1", "name2", "league", "start_time",
             "fair1", "fair2", "quote1", "quote2", "limit_usd", "seen_at",
             "obs_at", "cutoff_at", "version"]
CLOSE_COLS = OPEN_COLS + ["mins_to_start", "n_seen"]
TICK_COLS = ["pair", "seen_at", "obs_at", "start_time", "mins_to_start",
             "fair1", "fair2", "quote1", "quote2", "limit_usd", "version"]


# ---------------------------------------------------------------------------------------------------
# fetching

def _ctx() -> ssl.SSLContext:
    """Verification stays fully on. Some Pythons ship without a usable CA bundle of their own and die
    with CERTIFICATE_VERIFY_FAILED; the fix is to point at the platform store, never to disable
    checking, which would hand the odds to anyone able to sit in the middle."""
    for p in (os.environ.get("SSL_CERT_FILE"), "/etc/ssl/cert.pem"):
        if p and os.path.exists(p):
            return ssl.create_default_context(cafile=p)
    return ssl.create_default_context()


_CTX = _ctx()


def _get(path: str):
    """GET one path. Returns (parsed, age_seconds). Retries, because a single dropped request on a
    five-minute cron means a five-minute hole, and the retry costs a second."""
    req = urllib.request.Request(
        f"{API}/{path}",
        headers={"x-api-key": KEY, "accept": "application/json",
                 "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )
    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_CTX) as r:
                body = r.read()
                if not body:           # 204 No Content — a variant this endpoint does not accept
                    return None, None
                try:
                    age = int(r.headers.get("Age") or 0)
                except ValueError:
                    age = 0
                return json.loads(body.decode("utf-8", "replace")), age
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            last = e
            if attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    print(f"  ! {path} failed after {RETRIES}: {type(last).__name__}: {last}", file=sys.stderr)
    return None, None


# ---------------------------------------------------------------------------------------------------
# keys and prices

def name_key(n) -> str:
    """'last|initial', verbatim the convention used by the private pipeline, so a name that matches
    here matches there. On its own it collides — 'Carolina Alves' and 'Carolina Meligeni Alves' are
    both 'alves|c' — which is exactly why every lookup is keyed on the PAIR and never on one name."""
    n = unicodedata.normalize("NFKD", str(n)).encode("ascii", "ignore").decode().lower()
    n = n.replace("-", " ").replace(".", " ").replace("'", "")
    t = [x for x in n.split() if x not in ("jr", "ii", "iii")]
    return (t[-1] + "|" + t[0][0]) if len(t) > 1 else (t[0] + "|" if t else "")


def _dec(american: float) -> float:
    return 1 + american / 100 if american > 0 else 1 + 100 / -american


def devig(o1: float, o2: float, it: int = 60) -> tuple[float, float]:
    """Odds-ratio devig — the same Newton iteration the private pipeline uses, so the number it
    produces is bit-for-bit what that pipeline would produce rather than an approximation of it.
    A plain normalisation q1/(q1+q2) would NOT agree: it splits the margin evenly between the two
    sides and so misprices exactly the longshots this is most often used to look at."""
    q1, q2 = 1 / o1, 1 / o2
    c = 1.0
    for _ in range(it):
        p1 = q1 / (c + q1 - c * q1)
        p2 = q2 / (c + q2 - c * q2)
        g = p1 + p2 - 1
        d = -q1 * (1 - q1) / (c + q1 - c * q1) ** 2 - q2 * (1 - q2) / (c + q2 - c * q2) ** 2
        c = min(max(c - g / d, 1e-4), 100.0)
    return q1 / (c + q1 - c * q1), q2 / (c + q2 - c * q2)


def _utc(v) -> str | None:
    """Pinnacle's ISO timestamps as the 'YYYY-MM-DD HH:MM:SS' UTC string every file here stores."""
    if not v:
        return None
    try:
        return (datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                .astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------------
# the board

def fetch_board() -> dict:
    """One poll. Returns {pair_key: row}, or {} if the network was unusable.

    Four requests total, replacing the one-list-plus-one-per-matchup fan-out the private poller used —
    that was ~85 round trips for the same information, and it capped how many new matchups could be
    priced in a pass because the fan-out was the expensive part.
    """
    meta: dict[int, dict] = {}
    for v in LIST_VARIANTS:
        raw, age = _get(v)
        if not raw:
            continue
        for m in raw:
            # Derived children (sets, handicaps) repeat the parent's participants, so letting them
            # through would overwrite a real moneyline with a set line under the same key. They are
            # over half the list.
            if m.get("parentId") or not m.get("hasMarkets") or m.get("isLive"):
                continue
            meta.setdefault(m["id"], m)
        print(f"  list {v.split('/')[-1]:38s} n={len(raw):4d} age={age}")

    # Highest `version` wins, per matchup. Monotonic, so this is an exact "which reading is later",
    # not a heuristic. `obs_at` carries how stale the winning response was; `seen_at` stays the fetch
    # time, so nothing already written is ever restated by a later change of mind about the clock.
    now = datetime.now(timezone.utc)
    best: dict[int, tuple[dict, int]] = {}
    for v in MARKET_VARIANTS:
        raw, age = _get(v)
        if not raw:
            continue
        n = 0
        for mk in raw:
            if (mk.get("type") != "moneyline" or mk.get("period") != 0
                    or mk.get("isAlternate") or mk.get("status") != "open"
                    or len(mk.get("prices") or []) != 2):
                continue
            mid, ver = mk.get("matchupId"), mk.get("version") or 0
            if mid is None:
                continue
            n += 1
            if mid not in best or ver > best[mid][1]:
                best[mid] = (dict(mk, _age=age), ver)
        print(f"  mkts {v.split('/')[-1]:38s} n={n:4d} age={age}")

    out: dict = {}
    for mid, (mk, ver) in best.items():
        m = meta.get(mid)
        if not m:
            continue                   # priced but not listed: no names, so nothing usable
        ps = [p for p in (m.get("participants") or []) if p.get("name")]
        if len(ps) != 2:
            continue
        k1, k2 = name_key(ps[0]["name"]), name_key(ps[1]["name"])
        if not k1 or not k2 or k1 == k2:
            continue
        by = {p["designation"]: p["price"] for p in mk["prices"]}
        a1 = by.get(ps[0].get("alignment")) or by.get("home")
        a2 = by.get(ps[1].get("alignment")) or by.get("away")
        if a1 is None or a2 is None:
            continue
        d1, d2 = _dec(a1), _dec(a2)
        f1, f2 = devig(d1, d2)
        age = mk.get("_age") or 0
        lo, hi = sorted((k1, k2))
        out[f"{lo}|{hi}"] = {
            "pair": f"{lo}|{hi}", "k1": lo, "k2": hi,
            "name1": ps[0]["name"] if k1 == lo else ps[1]["name"],
            "name2": ps[1]["name"] if k1 == lo else ps[0]["name"],
            "league": (m.get("league") or {}).get("name"),
            "start_time": _utc(m.get("startTime")),
            "fair1": f1 if k1 == lo else f2, "fair2": f2 if k1 == lo else f1,
            "quote1": (1 / d1) if k1 == lo else (1 / d2),
            "quote2": (1 / d2) if k1 == lo else (1 / d1),
            "limit_usd": max([l.get("amount", 0) for l in (mk.get("limits") or [])], default=None),
            "seen_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "obs_at": (now - timedelta(seconds=age)).strftime("%Y-%m-%d %H:%M:%S"),
            # Pinnacle's real betting cutoff. Better than `startTime`, which it keeps revising — across
            # 297 pairs it moved a median +14 minutes between first and last sighting.
            "cutoff_at": _utc(mk.get("cutoffAt")),
            "version": ver,
        }
    return out


# ---------------------------------------------------------------------------------------------------
# files

def _read(path: str, cols: list[str]) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["pair"]: r for r in csv.DictReader(fh) if r.get("pair")}


def _write(path: str, cols: list[str], rows: list[dict]) -> None:
    """Atomic, and sorted, so a diff between two commits is the prices that changed and nothing else.
    Written to a temp file first because a cron that is killed mid-write would otherwise commit a
    truncated CSV, and a truncated pin_open.csv loses opens that cannot be re-derived."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    os.replace(tmp, path)


def update_open(board: dict) -> int:
    """First sightings. Returns how many pairs were new.

    Every price column is insert-only: a pair already on file keeps the number it was first seen with,
    because what this file exists to hold is the EARLIEST price. An update would quietly walk it
    forward to the current line and turn it into a second copy of pin_close.

    `start_time` is the one exception, and it is an exception because it is a different kind of claim —
    a start time is a fact about the FIXTURE, not about the open, so freezing it preserves nothing but a
    stale schedule. Measured on 2026-09-15: 142 of 264 pairs had moved since first sighting and 13 of
    those crossed a day boundary, so matches were scored and priced correctly and still filed on the
    wrong day.

    Matchups whose listed start has already passed are skipped. Pinnacle takes a moment to flip a match
    to isLive, so without this guard a match first seen a few minutes after it began would file an
    in-play price as an open — the one error that would be invisible afterwards.
    """
    have = _read(OPEN_CSV, OPEN_COLS)
    now_s = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    new = 0
    for pair, m in board.items():
        st = m["start_time"]
        if not st or st <= now_s:
            continue
        if pair in have:
            have[pair]["start_time"] = st          # clock only; every price column untouched
            have[pair]["name1"] = m["name1"]
            have[pair]["name2"] = m["name2"]
            have[pair]["league"] = m["league"]
            have[pair]["cutoff_at"] = m["cutoff_at"]
        else:
            have[pair] = dict(m)
            new += 1
    _write(OPEN_CSV, OPEN_COLS, [have[k] for k in sorted(have)])
    return new


def update_close(board: dict) -> tuple[int, int]:
    """Latest pre-match price, plus a tick wherever it actually moved. Returns (closes, ticks).

    Deliberately the opposite of update_open: this upserts on every poll. Pinnacle drops a match from
    the pre-match board the instant it starts, so the last write to survive is by construction the last
    price before play — the close.

    What counts as "pre-match" is Pinnacle's answer and not Pinnacle's clock. Every row here has already
    cleared `isLive == False` on the matchup and `status == "open"` on the full-match moneyline, which
    is the book saying it is still taking pre-match bets. Requiring the advertised start to be in the
    future as well was tried and was wrong: that clock drifts, so a delayed or re-hung match sailed past
    its advertised time and could never have its close updated again. 27 of 86 graded rows froze more
    than thirty minutes early that way.

    `mins_to_start` may therefore be NEGATIVE, and that is information rather than an error: it says
    Pinnacle was still quoting this match that many minutes after the time it had advertised.
    """
    have = _read(CLOSE_CSV, CLOSE_COLS)
    now = datetime.now(timezone.utc)
    ticks = []
    for pair, m in board.items():
        st = m["start_time"]
        if not st:
            continue
        mins = round((datetime.strptime(st, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                      - now).total_seconds() / 60.0, 1)
        prev = have.get(pair)
        row = dict(m, mins_to_start=mins, n_seen=(int(prev["n_seen"]) + 1 if prev and prev.get("n_seen") else 1))
        have[pair] = row
        # The audit trail, written only where the price actually moved. Gating on `version` rather than
        # on the price makes a suspension show up as a gap in seen_at rather than as a run of identical
        # rows nobody can read.
        if not prev or str(prev.get("version") or "") != str(m["version"]):
            ticks.append(row)
    _write(CLOSE_CSV, CLOSE_COLS, [have[k] for k in sorted(have)])
    return len(board), _append_ticks(ticks)


def _append_ticks(rows: list[dict]) -> int:
    """One file per UTC day. Sharded because this is the only append-only file here and a single one
    would be rewritten by git on every poll forever; a day file stops changing at midnight."""
    if not rows:
        return 0
    os.makedirs(TICK_DIR, exist_ok=True)
    n = 0
    for day in sorted({r["seen_at"][:10] for r in rows}):
        path = os.path.join(TICK_DIR, f"{day}.csv")
        fresh = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=TICK_COLS, extrasaction="ignore")
            if fresh:
                w.writeheader()
            for r in rows:
                if r["seen_at"][:10] == day:
                    w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in TICK_COLS})
                    n += 1
    return n


def archive_old() -> int:
    """Move finished matches out of the live close file into a monthly archive.

    Nothing is dropped — the archive is the same rows in the same columns. The point is purely that
    pin_close.csv is rewritten on every poll, so its size is the per-run cost in git objects, while an
    archive file changes about once a day.
    """
    have = _read(CLOSE_CSV, CLOSE_COLS)
    cut = (datetime.now(timezone.utc) - timedelta(days=ARCHIVE_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    old = {k: v for k, v in have.items() if (v.get("start_time") or "9999") < cut}
    if not old:
        return 0
    for month in sorted({v["start_time"][:7] for v in old.values()}):
        path = os.path.join(ARCHIVE_DIR, f"pin_close_{month}.csv")
        merged = _read(path, CLOSE_COLS)
        merged.update({k: v for k, v in old.items() if v["start_time"][:7] == month})
        _write(path, CLOSE_COLS, [merged[k] for k in sorted(merged)])
    for k in old:
        have.pop(k, None)
    _write(CLOSE_CSV, CLOSE_COLS, [have[k] for k in sorted(have)])
    return len(old)


def main() -> int:
    t0 = time.time()
    print(f"poll {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z")
    board = fetch_board()
    if not board:
        # Exit 0, not 1. An empty board is nearly always Cloudflare or a quiet hour, and a red cron is
        # a notification that trains you to ignore notifications. The counts below are the signal.
        print("  board empty — nothing written")
        return 0
    opened = update_open(board)
    closes, ticks = update_close(board)
    archived = archive_old()
    print(f"  board={len(board)} new_open={opened} close={closes} ticks={ticks} "
          f"archived={archived} {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
