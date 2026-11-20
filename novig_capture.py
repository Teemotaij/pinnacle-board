"""Novig tennis capture: track Novig's tennis moneylines the way the CBB repo tracks Novig. Standard library only.

Public numbers anyone can read without logging in. Nothing here authenticates, holds a key or places an order, and
nothing private is read: the Pinnacle reference comes from this repo's own board/ files (pin_open, pin_close,
pin_live, ticks/), never from a model, a pick or an order.

WHAT EACH RUN DOES (one "tick"; the workflow would run it every 5 minutes)
  1. Lists Novig's tennis events (ATP + WTA, OPEN_PREGAME + OPEN_INGAME; Novig has no ITF league, and its ATP
     league carries the Challengers) and every MONEY market in one paged call per league (volume, best bids).
  2. Decides which matches are DUE this tick:
       early   inside EARLY_H hours after Novig listed the market (its id is a UUIDv7: the creation time is in the
               id) OR after Pinnacle first priced the pair (pin_open seen_at): every tick
       near    starting within NEAR_H hours (not yet in play): every tick
       hourly  everything else Novig lists before play: once every EVERY_MIN minutes
       live    in play: at most once a tick (no faster live cadence), and LAST in the order (TJ 19 Nov: in-play
               Novig matters little; it must never crowd out a pre-match read when a tick runs short)
     and reads them early first, then near, then hourly (each nearest start first), then live, until
     PASS_BUDGET_S runs out. What is due and not reached is
     counted as UNREAD in the health log (and per match in the match clock).
  3. For each match read: the full order book (both outcomes' resting bids; the asks to buy a side are the other
     side's bids at 1 - price) -> one row in books/<UTC date>.csv; and, when Novig's volume counter moved since
     the last read, the trade tape walked back to what we already hold -> trades/<UTC date>.csv. A read that
     cannot reach back to what we hold is logged in tape_gaps.csv.
  4. Rewrites match_clock.csv (one row per match: when Novig listed it, when Pinnacle first priced it, its first
     two-sided book, first READY_USD book, first trade, first seen in play, how many early-window books it got and,
     if none, why) and appends one line to capture_log.csv (the health log).

UNITS. A Novig contract pays 1 cent: dollars = contracts x 0.01 x price. Side 1 / side 2 follow the board's pair
order (k1 < k2 alphabetically), so pin fair1 / fair2 line up with p1_* / p2_*.

    python3 novig_capture.py --out data/novig_tennis --board board     # one tick
    python3 novig_capture.py --check data/novig_tennis                 # the doc's checks over what is on file
"""
import argparse, csv, datetime as dt, glob, gzip, io, json, os, sys, time, unicodedata, urllib.error, urllib.parse, urllib.request

HOST = "https://api.novig.com"
LEAGUES = ("ATP", "WTA")                 # /v3/public/types/leagues: no ITF league (checked 19 Nov 2026)
STATUSES = ("OPEN_PREGAME", "OPEN_INGAME")
EARLY_H = 3.0
NEAR_H = 12.0
EVERY_MIN = 50
PASS_BUDGET_S = 200                      # a 5-minute cadence: leave room for the commit
DEPTH = 20                               # price LEVELS per outcome (depth=50 is refused); 20 is the whole book in practice
PAUSE_S = 0.6                            # before every call: Novig answers 429 to a burst
READY_USD = 50.0                         # match clock: both sides show >= this at the best bid
TRADES_LIMIT = 5000
MAX_PAGES = 10
CONTRACT_USD = 0.01
PIN_DAYS = 3                             # ticks files read for the Pinnacle reference (today and two before)


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else ""


def from_ms(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc)


def parse_t(s):
    """'2026-11-19 12:00:00', '2026-11-19T12:00:00Z' or ISO with an offset -> aware UTC datetime; '' -> None."""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00").replace(" ", "T")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t.astimezone(dt.timezone.utc)


def uuid7_time(u):
    """Novig's ids are UUIDv7: the first 48 bits are the creation time in ms (a trade id's equals its ts)."""
    try:
        return from_ms(int(str(u).replace("-", "")[:12], 16))
    except (ValueError, TypeError):
        return None


def hours(a, b):
    return round((a - b).total_seconds() / 3600, 3) if a and b else None


def name_key(n):
    """poll.name_key, copied so this file stands alone: 'last|initial'. 'M. Donald' and 'Matthew William Donald' both
    give 'donald|m', so Novig's outcome names key the same pair as Pinnacle's full names."""
    n = unicodedata.normalize("NFKD", str(n)).encode("ascii", "ignore").decode().lower()
    n = n.replace("-", " ").replace(".", " ").replace("'", "")
    t = [x for x in n.split() if x not in ("jr", "ii", "iii")]
    return (t[-1] + "|" + t[0][0]) if len(t) > 1 else (t[0] + "|" if t else "")


def on_grid(p):
    """Novig's price grid: 0.001-0.050 by 0.001, 0.055-0.945 by 0.005, 0.950-0.999 by 0.001."""
    m = round(float(p) * 1000)
    if abs(float(p) * 1000 - m) > 1e-6 or not 1 <= m <= 999:
        return False
    return m <= 50 or m >= 950 or m % 5 == 0


# --------------------------------------------------------------------------------------------- the public client

class NovigError(Exception):
    def __init__(self, status, text, path):
        super().__init__(f"HTTP {status} {path}: {str(text)[:200]}")
        self.status = status


class Client:
    """Novig's public catalog, GET only, no key. Retries 429 / 5xx with a growing back-off; counts calls and 429s."""

    def __init__(self, host=HOST, pause=PAUSE_S, timeout=25):
        self.host, self.pause, self.timeout = host, pause, timeout
        self.stats = dict(calls=0, http_429=0, retries=0)

    def get(self, path, params=None):
        q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = self.host + path + ("?" + q if q else "")
        for attempt in range(5):
            time.sleep(self.pause)
            self.stats["calls"] += 1
            try:
                r = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "pinnacle-board-novig-capture"})
                with urllib.request.urlopen(r, timeout=self.timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self.stats["http_429"] += 1
                if e.code not in (429, 500, 502, 503, 504) or attempt == 4:
                    raise NovigError(e.code, e.read()[:200] if hasattr(e, "read") else "", path)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == 4:
                    raise NovigError(0, repr(e), path)
            self.stats["retries"] += 1
            time.sleep(3 * (attempt + 1))

    def _pages(self, path, params):
        out, after = [], None
        for _ in range(50):
            d = self.get(path, dict(params, limit=200, after=after)) or {}
            items = d.get("items") or []
            out += items
            after = d.get("next")
            if not after or not items:
                break
        return out

    def events(self, league, status):
        return self._pages("/v3/public/catalog/events", dict(league=league, status=status))

    def money_markets(self, league):
        return self._pages("/v3/public/catalog/markets", dict(league=league, marketType="MONEY"))

    def book(self, market_id, depth=DEPTH):
        return self.get(f"/v3/public/catalog/markets/{market_id}/book", dict(depth=depth)) or {}

    def trades_page(self, market_id, limit=TRADES_LIMIT, after=None):
        d = self.get(f"/v3/public/catalog/markets/{market_id}/trades", dict(limit=limit, after=after)) or {}
        return d.get("items") or [], d.get("next")


# --------------------------------------------------------------------------------------------- Pinnacle reference

class Pin:
    """The board's Pinnacle prices for a pair: first priced (pin_open seen_at), the no-vig price in force at a moment
    (the latest tick, pin_open or pin_close row seen at or before it) and the first in-play sighting (pin_live)."""

    def __init__(self, board, now):
        self.open, self.close, self.lives, self.ticks = {}, {}, {}, {}
        rd = lambda fn: list(csv.DictReader(open(fn, newline=""))) if os.path.exists(fn) else []
        for r in rd(os.path.join(board, "pin_open.csv")):
            self.open[r["pair"]] = r
        for r in rd(os.path.join(board, "pin_close.csv")):
            self.close[r["pair"]] = r
        for r in rd(os.path.join(board, "pin_live.csv")):
            t = parse_t(r.get("first_live"))
            if t:
                self.lives.setdefault(r["pair"], []).append(t)
        for k in range(PIN_DAYS - 1, -1, -1):
            for r in rd(os.path.join(board, "ticks", f"{(now - dt.timedelta(days=k)):%Y-%m-%d}.csv")):
                t = parse_t(r.get("seen_at"))
                if t:
                    self.ticks.setdefault(r["pair"], []).append((t, r))
        for v in self.ticks.values():
            v.sort(key=lambda x: x[0])

    def has(self, pair):
        return pair in self.open or pair in self.close

    def start(self, pair):
        r = self.close.get(pair) or self.open.get(pair)
        return parse_t(r.get("start_time")) if r else None

    def first_priced(self, pair):
        r = self.open.get(pair)
        return parse_t(r.get("seen_at")) if r else None

    def fair(self, pair, t):
        """(fair1, fair2, source, seen_at) in force at t, or (None, None, '', None)."""
        best = None
        for src, r in (("open", self.open.get(pair)), ("close", self.close.get(pair))):
            s = parse_t(r.get("seen_at")) if r else None
            if s and s <= t and (best is None or s > best[2]):
                best = (r, src, s)
        for s, r in self.ticks.get(pair, ()):
            if s > t:
                break
            if best is None or s >= best[2]:
                best = (r, "tick", s)
        if best is None:
            return None, None, "", None
        r, src, s = best
        try:
            return float(r["fair1"]), float(r["fair2"]), src, s
        except (KeyError, TypeError, ValueError):
            return None, None, "", None

    def first_live(self, pair, start):
        """Pinnacle's first in-play sighting of THIS meeting: the earliest at or after start - 6 h (a pair meets more
        than once a season, and the board's start moves)."""
        if not start:
            return None
        c = [t for t in self.lives.get(pair, ()) if t >= start - dt.timedelta(hours=6)]
        return min(c) if c else None


# --------------------------------------------------------------------------------------------- book arithmetic

def levels(orders):
    """Resting orders -> [(price, contracts)] by price level, best (highest) first. Equal prices merged."""
    agg = {}
    for o in orders or ():
        if o.get("price") is None:
            continue
        p = round(float(o["price"]), 3)
        agg[p] = agg.get(p, 0) + int(float(o.get("qty") or 0))
    return sorted(agg.items(), key=lambda x: -x[0])


def usd(p, q):
    return round(p * q * CONTRACT_USD, 2)


def ask_ladder(bids_other):
    """The asks to buy a side, from the bids resting on the OTHER outcome: [(price, dollars)], cheapest first."""
    return [(round(1 - p, 3), usd(1 - p, q)) for p, q in sorted(bids_other, key=lambda x: -x[0])]


def side_cols(tag, bids, asks, fair):
    le = lambda cap: round(sum(u for p, u in asks if p <= cap + 1e-9), 2) if cap is not None else None
    return {f"{tag}_bid": bids[0][0] if bids else None, f"{tag}_bid_usd": usd(*bids[0]) if bids else 0.0,
            f"{tag}_ask": asks[0][0] if asks else None, f"{tag}_ask_usd": asks[0][1] if asks else 0.0,
            f"{tag}_le_pin": le(fair), f"{tag}_le_pin2": le(fair + 0.02) if fair is not None else None,
            f"{tag}_bids": "|".join(f"{p:.3f}:{usd(p, q):.0f}" for p, q in bids),
            f"{tag}_asks": "|".join(f"{p:.3f}:{u:.0f}" for p, u in asks)}


# --------------------------------------------------------------------------------------------- files

BOOK_COLS = ["ts", "event_id", "market_id", "pair", "pin_match", "league", "name1", "name2", "start", "status", "in_play",
             "h_to_start", "novig_listed", "h_since_novig", "pin_first", "h_since_pin", "early", "why_due",
             "pin_fair1", "pin_fair2", "pin_src", "pin_at",
             "p1_bid", "p1_bid_usd", "p1_ask", "p1_ask_usd", "p1_le_pin", "p1_le_pin2",
             "p2_bid", "p2_bid_usd", "p2_ask", "p2_ask_usd", "p2_le_pin", "p2_le_pin2",
             "volume", "open_interest", "list_bid1", "list_bid2", "seq", "p1_bids", "p1_asks", "p2_bids", "p2_asks"]
TRADE_COLS = ["trade_id", "take_id", "ts", "logged", "event_id", "market_id", "pair", "maker_side", "maker_name", "maker_price",
              "taker_side", "taker_price", "qty", "usd", "taker_usd", "pin_novig", "pin_src", "h_to_start", "in_play"]
GAP_COLS = ["logged", "market_id", "pair", "gap_from", "gap_to", "minutes", "pages"]
LOG_COLS = ["ts", "listed", "pin_matched", "due", "read", "unread", "due_early", "due_near", "due_hourly", "due_live",
            "read_early", "read_near", "read_hourly", "read_live", "in_play", "book_rows", "trade_rows", "tapes_read", "gaps", "calls", "http_429",
            "retries", "errors", "seconds", "note"]
CLOCK_COLS = ["event_id", "market_id", "pair", "pin_match", "league", "name1", "name2", "start", "novig_listed", "pin_first",
              "pin_open_fair1", "first_read", "reads", "early_reads", "unread_due", "first_two_sided", "two_bid1", "two_bid2",
              "first_ready", "ready_bid1", "ready_bid2", "ready_pin_fair1", "first_trade", "first_trade_side", "first_trade_price",
              "novig_in_play", "pin_first_live", "last_read", "last_status", "early_reason"]


def seal_days(out, today):
    """Gzip every finished day file (books/ and trades/ before `today`) as one stream and remove the plain copy. The
    current day stays a plain CSV that each run appends to: git stores those appends as small deltas, and one gzip
    stream per finished day is ~3x smaller than gzip members appended run by run (dry run 19 Nov: 32 KB v 92 KB)."""
    n = 0
    for sub in ("books", "trades"):
        for fn in sorted(glob.glob(os.path.join(out, sub, "*.csv"))):
            if os.path.basename(fn)[:10] >= today:
                continue
            gz = fn + ".gz"
            with open(fn, "rb") as src:
                data = src.read()
            if os.path.exists(gz):                         # never overwrite a sealed day: add its rows, header dropped
                with gzip.open(gz, "ab", compresslevel=9) as f:
                    f.write(data.split(b"\n", 1)[1] if b"\n" in data else b"")
            else:
                with gzip.open(gz + ".tmp", "wb", compresslevel=9) as f:
                    f.write(data)
                os.replace(gz + ".tmp", gz)
            os.remove(fn)
            n += 1
    return n


def day_files(out, sub):
    """A folder's day files, sealed (.csv.gz) and current (.csv), in date order."""
    return sorted(glob.glob(os.path.join(out, sub, "*.csv.gz")) + glob.glob(os.path.join(out, sub, "*.csv")))


def append_gz(fn, cols, rows):
    """Append rows to a gzipped CSV. Each call adds a gzip member (gzip readers concatenate them); the header is
    written once, when the file is created."""
    if not rows:
        return
    os.makedirs(os.path.dirname(fn), exist_ok=True)
    new = not os.path.exists(fn)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    if new:
        w.writeheader()
    w.writerows(rows)
    with gzip.open(fn, "ab", compresslevel=9) as f:
        f.write(buf.getvalue().encode())


def append_csv(fn, cols, rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(fn) or ".", exist_ok=True)
    new = not os.path.exists(fn)
    with open(fn, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        if new:
            w.writeheader()
        w.writerows(rows)


def write_csv(fn, cols, rows):
    tmp = fn + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, fn)


def read_rows(fn):
    if not os.path.exists(fn):
        return []
    op = gzip.open if fn.endswith(".gz") else open
    with op(fn, "rt", newline="") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------------------------- the tape

def read_tape(nc, mid, mark_ms):
    """New prints of a market, newest first: pages are walked back (after=<next>) until one reaches the watermark or
    the market's first print. Returns (prints, pages, gap) where gap = (from_ms, to_ms) if MAX_PAGES ran out before
    reaching what we hold."""
    out, nxt, pages = [], None, 0
    while True:
        items, nxt = nc.trades_page(mid, limit=TRADES_LIMIT, after=nxt)
        pages += 1
        out += items
        oldest = min((int(t["ts"]) for t in items), default=None)
        if not nxt or not items or (mark_ms is not None and oldest is not None and oldest <= mark_ms):
            return out, pages, None
        if pages >= MAX_PAGES:
            return out, pages, ((mark_ms, oldest) if mark_ms is not None else (None, oldest))


def tape_new(prints, mark):
    """Prints after the market's mark [ms, ids at that ms]; returns (new, new mark)."""
    m_ms, m_ids = (int(mark[0]), set(mark[1])) if mark else (None, set())
    new, top_ms, top_ids = [], m_ms, set(m_ids)
    seen = set()
    for t in prints:
        tid, ms = str(t.get("tradeId")), int(t["ts"])
        if tid in seen:
            continue
        seen.add(tid)
        if m_ms is not None and (ms < m_ms or (ms == m_ms and tid in m_ids)):
            continue
        new.append(t)
        if top_ms is None or ms > top_ms:
            top_ms, top_ids = ms, {tid}
        elif ms == top_ms:
            top_ids.add(tid)
    return new, ([top_ms, sorted(top_ids)] if top_ms is not None else mark)


# --------------------------------------------------------------------------------------------- one tick

def due_reason(now, start, in_play, listed, pin_first, last_read):
    """('early' | 'near' | 'hourly' | 'live' | None, early flag). In play is 'live' whatever its windows say."""
    if in_play:
        return "live", False
    early = any(t and dt.timedelta(0) <= now - t <= dt.timedelta(hours=EARLY_H) for t in (listed, pin_first))
    if early:
        return "early", True
    if (start and start - now <= dt.timedelta(hours=NEAR_H)):
        return "near", False
    if last_read is None or now - last_read >= dt.timedelta(minutes=EVERY_MIN) - dt.timedelta(seconds=30):
        return "hourly", False
    return None, False


def run_once(out, board, nc=None, now=None, budget_s=PASS_BUDGET_S, clock=time.monotonic):
    t0 = clock()
    now = now or utcnow()
    nc = nc or Client()
    os.makedirs(out, exist_ok=True)
    st_fn = os.path.join(out, "state.json")
    try:
        state = json.load(open(st_fn))
    except (OSError, ValueError):
        state = {}
    vol, wm, last = state.get("vol", {}), state.get("wm", {}), state.get("last_read", {})
    clock_rows = {r["event_id"]: r for r in read_rows(os.path.join(out, "match_clock.csv"))}
    pin = Pin(board, now)
    errors, note = [], []

    # 1. listing: events (for status) and every MONEY market (for ids, names, volume)
    status, mkts = {}, []
    for lg in LEAGUES:
        for st in STATUSES:
            try:
                for e in nc.events(lg, st):
                    status[e["eventId"]] = (st, e)
            except NovigError as e:
                errors.append(f"events {lg} {st}: {e}")
        try:
            mkts += [dict(m, _league=lg) for m in nc.money_markets(lg)]
        except NovigError as e:
            errors.append(f"markets {lg}: {e}")

    # 2. who is due
    todo, n_matched = [], 0
    stats = dict(due_early=0, due_near=0, due_hourly=0, due_live=0, read_early=0, read_near=0, read_hourly=0, read_live=0)
    for m in mkts:
        ev = m.get("eventId")
        if ev not in status or len(m.get("outcomes") or []) != 2:
            continue                                         # settled / closed events drop out of the listing
        o = m["outcomes"]
        k = [name_key(x.get("name", "")) for x in o]
        if not k[0] or not k[1] or k[0] == k[1]:
            continue
        pair = "|".join(sorted(k))
        i1 = 0 if k[0] < k[1] else 1                         # the outcome that is side 1 (k1)
        start = from_ms(m["startsTs"]) if m.get("startsTs") else (from_ms(status[ev][1]["startsTs"]) if status[ev][1].get("startsTs") else None)
        pin_match = ""
        if pin.has(pair):
            ps = pin.start(pair)
            pin_match = "yes" if ps is None or start is None or abs((ps - start).total_seconds()) <= 36 * 3600 else "stale"
            n_matched += pin_match == "yes"
        pin_first = pin.first_priced(pair) if pin_match == "yes" else None
        listed = uuid7_time(m["marketId"])
        in_play = status[ev][0] == "OPEN_INGAME"
        why, early = due_reason(now, start, in_play, listed, pin_first, parse_t(last.get(m["marketId"])))
        if why:
            stats["due_" + why] += 1
            todo.append(dict(m=m, ev=ev, o=o, i1=i1, pair=pair, start=start, pin_match=pin_match, pin_first=pin_first,
                             listed=listed, in_play=in_play, why=why, early=early, status=status[ev][0]))
        c = clock_rows.setdefault(ev, dict(event_id=ev, reads=0, early_reads=0, unread_due=0))
        c.update(market_id=m["marketId"], pair=pair, pin_match=pin_match, league=m["_league"],
                 name1=o[i1].get("name"), name2=o[1 - i1].get("name"), start=iso(start), novig_listed=iso(listed),
                 pin_first=iso(pin_first), last_status=status[ev][0])
        if pin_first and not c.get("pin_open_fair1"):
            f1, _, _, _ = pin.fair(pair, pin_first)
            c["pin_open_fair1"] = f1
        if in_play and not c.get("novig_in_play"):
            c["novig_in_play"] = iso(now)
        if pin_match == "yes":
            fl = pin.first_live(pair, start)
            if fl:
                c["pin_first_live"] = iso(fl)

    rank = {"early": 0, "near": 1, "hourly": 2, "live": 3}          # in play last: never ahead of a pre-match read
    todo.sort(key=lambda x: (rank[x["why"]], x["start"] or now))

    # 3. read in priority order until the budget runs out
    books, trades, gaps, n_read, n_tapes = [], [], [], 0, 0
    unread = []
    ts = iso(now)
    for x in todo:
        if clock() - t0 > budget_s:
            unread.append(x)
            continue
        m, o, i1, pair = x["m"], x["o"], x["i1"], x["pair"]
        mid = m["marketId"]
        try:
            b = nc.book(mid)
        except NovigError as e:
            errors.append(f"book {pair}: {e}")
            unread.append(x)
            continue
        n_read += 1
        stats["read_" + x["why"]] += 1
        orders = b.get("orders") or {}
        bid1, bid2 = levels(orders.get(o[i1]["outcomeId"])), levels(orders.get(o[1 - i1]["outcomeId"]))
        f1, f2, src, at = pin.fair(pair, now) if x["pin_match"] == "yes" else (None, None, "", None)
        lb = lambda oo: (oo.get("bid") or {}).get("price")
        r = dict(ts=ts, event_id=x["ev"], market_id=mid, pair=pair, pin_match=x["pin_match"], league=m["_league"],
                 name1=o[i1].get("name"), name2=o[1 - i1].get("name"), start=iso(x["start"]), status=x["status"],
                 in_play=int(x["in_play"]), h_to_start=hours(x["start"], now), novig_listed=iso(x["listed"]),
                 h_since_novig=hours(now, x["listed"]), pin_first=iso(x["pin_first"]), h_since_pin=hours(now, x["pin_first"]),
                 early=int(x["early"]), why_due=x["why"], pin_fair1=f1, pin_fair2=f2, pin_src=src, pin_at=iso(at),
                 volume=m.get("volume"), open_interest=m.get("openInterest"), list_bid1=lb(o[i1]), list_bid2=lb(o[1 - i1]), seq=b.get("seq"))
        r.update(side_cols("p1", bid1, ask_ladder(bid2), f1))
        r.update(side_cols("p2", bid2, ask_ladder(bid1), f2))
        books.append(r)
        last[mid] = ts
        c = clock_rows[x["ev"]]
        c["reads"] = int(c.get("reads") or 0) + 1
        c["early_reads"] = int(c.get("early_reads") or 0) + int(x["early"])
        c["first_read"] = c.get("first_read") or ts
        c["last_read"] = ts
        if bid1 and bid2 and not c.get("first_two_sided"):
            c.update(first_two_sided=ts, two_bid1=bid1[0][0], two_bid2=bid2[0][0])
        if bid1 and bid2 and usd(*bid1[0]) >= READY_USD and usd(*bid2[0]) >= READY_USD and not c.get("first_ready"):
            c.update(first_ready=ts, ready_bid1=bid1[0][0], ready_bid2=bid2[0][0], ready_pin_fair1=f1)

        # the tape, when the volume counter moved (or the market was never read)
        v_now = float(m.get("volume") or 0)
        v_was = vol.get(mid)
        if (v_was is not None and v_now <= float(v_was)) or (v_was is None and v_now <= 0):
            vol[mid] = v_now
            continue
        mark = wm.get(mid)
        try:
            prints, pages, gap = read_tape(nc, mid, int(mark[0]) if mark else None)
        except NovigError as e:
            errors.append(f"trades {pair}: {e}")
            continue
        n_tapes += 1
        new, wm[mid] = tape_new(prints, mark)
        vol[mid] = v_now
        if gap:
            a, z = gap
            gaps.append(dict(logged=ts, market_id=mid, pair=pair, gap_from=iso(from_ms(a)) if a else "", gap_to=iso(from_ms(z)),
                             minutes=round((z - a) / 60000, 1) if a else "", pages=pages))
        fl = parse_t(c.get("pin_first_live")) or parse_t(c.get("novig_in_play"))
        for t in sorted(new, key=lambda t: (int(t["ts"]), str(t.get("tradeId")))):
            side = 1 if t.get("outcomeId") == o[i1]["outcomeId"] else 2 if t.get("outcomeId") == o[1 - i1]["outcomeId"] else None
            if side is None or t.get("price") is None:
                continue
            px, q, tt = float(t["price"]), int(float(t.get("qty") or 0)), from_ms(t["ts"])
            g1, g2, gsrc, _ = pin.fair(pair, tt) if x["pin_match"] == "yes" else (None, None, "", None)
            trades.append(dict(trade_id=t.get("tradeId"), take_id=t.get("takeId"), ts=iso(tt), logged=ts, event_id=x["ev"], market_id=mid,
                               pair=pair, maker_side=side, maker_name=o[i1 if side == 1 else 1 - i1].get("name"), maker_price=px,
                               taker_side=3 - side, taker_price=round(1 - px, 3), qty=q, usd=usd(px, q), taker_usd=usd(1 - px, q),
                               pin_novig=(g1 if side == 1 else g2), pin_src=gsrc, h_to_start=hours(x["start"], tt),
                               in_play=(int(tt >= fl) if fl else 0)))  # no live sighting yet at logging = pre-match)
            if not c.get("first_trade") or iso(tt) < c["first_trade"]:
                c.update(first_trade=iso(tt), first_trade_side=side, first_trade_price=px)

    for x in unread:
        c = clock_rows[x["ev"]]
        c["unread_due"] = int(c.get("unread_due") or 0) + 1

    # early_reason: a match whose early windows are over without one early-window book says why
    t_first_run = parse_t(state.get("first_run")) or now
    for c in clock_rows.values():
        if int(c.get("early_reads") or 0) > 0:
            c["early_reason"] = ""
            continue
        ends = [t + dt.timedelta(hours=EARLY_H) for t in (parse_t(c.get("novig_listed")), parse_t(c.get("pin_first"))) if t]
        if ends and max(ends) > now:
            c["early_reason"] = "window open"
        elif ends and max(ends) <= t_first_run:
            c["early_reason"] = "windows ended before capture started"
        elif int(c.get("unread_due") or 0):
            c["early_reason"] = f"budget: due and unread {c['unread_due']} ticks"
        else:
            c["early_reason"] = "no tick ran in its windows"

    # 4. write
    day = f"{now:%Y-%m-%d}"
    append_csv(os.path.join(out, "books", f"{day}.csv"), BOOK_COLS, books)
    append_csv(os.path.join(out, "trades", f"{day}.csv"), TRADE_COLS, trades)
    sealed = seal_days(out, day)
    append_csv(os.path.join(out, "tape_gaps.csv"), GAP_COLS, gaps)
    keep = {k: v for k, v in clock_rows.items() if (parse_t(v.get("start")) or now) > now - dt.timedelta(days=60)}
    write_csv(os.path.join(out, "match_clock.csv"), CLOCK_COLS, sorted(keep.values(), key=lambda r: (r.get("start") or "", r["event_id"])))
    live_ids = {m["marketId"] for m in mkts}
    cut = now - dt.timedelta(days=4)
    state = dict(first_run=state.get("first_run") or iso(now), last_tick=iso(now),
                 vol={k: v for k, v in vol.items() if k in live_ids},
                 wm={k: v for k, v in wm.items() if v and v[0] is not None and from_ms(v[0]) >= cut or k in live_ids},
                 last_read={k: v for k, v in last.items() if k in live_ids})
    with open(st_fn + ".tmp", "w") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(st_fn + ".tmp", st_fn)
    secs = round(clock() - t0, 1)
    s = getattr(nc, "stats", {})
    log = dict(ts=ts, listed=sum(1 for m in mkts if m.get("eventId") in status), pin_matched=n_matched, due=len(todo), read=n_read,
               unread=len(unread), in_play=sum(1 for v in status.values() if v[0] == "OPEN_INGAME"), book_rows=len(books),
               trade_rows=len(trades), tapes_read=n_tapes, gaps=len(gaps), calls=s.get("calls"), http_429=s.get("http_429"),
               retries=s.get("retries"), errors=len(errors), seconds=secs, note="; ".join(([f"sealed {sealed} day files"] if sealed else []) + errors)[:500], **stats)
    append_csv(os.path.join(out, "capture_log.csv"), LOG_COLS, [log])
    print(f"{ts} novig capture: {log['listed']} listed, {n_matched} on the board, due {len(todo)} "
          f"(early {stats['due_early']}, near {stats['due_near']}, hourly {stats['due_hourly']}, live {stats['due_live']}), read {n_read}, UNREAD {len(unread)}, "
          f"{len(books)} books, {len(trades)} new trades, {n_tapes} tapes, {len(gaps)} gaps, {s.get('calls')} calls, "
          f"{s.get('http_429')} x 429, {len(errors)} errors, {secs}s", flush=True)
    for e in errors[:5]:
        print("  error:", e, flush=True)
    return log


# --------------------------------------------------------------------------------------------- the doc's checks

def checks(out):
    """The checks over what is on file: asks = 1 - other side's best bid; prices on the grid; dollars = contracts x
    0.01 x price; trades ~ volume-counter moves; match-clock rows have an early-window book or a reason."""
    books = [r for f in day_files(out, "books") for r in read_rows(f)]
    trades = [r for f in day_files(out, "trades") for r in read_rows(f)]
    clock = read_rows(os.path.join(out, "match_clock.csv"))
    res = {}
    fl = lambda v: float(v) if v not in ("", None) else None
    bad_ask = sum(1 for r in books for a, b in (("p1_ask", "p2_bid"), ("p2_ask", "p1_bid"))
                  if fl(r[a]) is not None and fl(r[b]) is not None and abs(fl(r[a]) - (1 - fl(r[b]))) > 1e-9)
    res["ask = 1 - other best bid (rows off)"] = f"{bad_ask} of {len(books)}"
    crossed = sum(1 for r in books if fl(r["p1_bid"]) is not None and fl(r["p2_bid"]) is not None and fl(r["p1_bid"]) + fl(r["p2_bid"]) > 1 + 1e-9)
    res["crossed books (bid1 + bid2 > 1)"] = crossed
    lst = [(fl(r["list_bid1"]), fl(r["p1_bid"])) for r in books if r.get("list_bid1") not in ("", None) and r.get("p1_bid") not in ("", None)]
    res["listing best bid = book best bid"] = f"{sum(1 for a, b in lst if abs(a - b) < 1e-9)} of {len(lst)} (the listing is read ~seconds before the book)"
    off = 0
    nprice = 0
    for r in books:
        for col in ("p1_bids", "p2_bids", "p1_asks", "p2_asks"):
            for lv in filter(None, (r.get(col) or "").split("|")):
                nprice += 1
                off += not on_grid(lv.split(":")[0])
    off_t = sum(1 for t in trades if not on_grid(t["maker_price"]))
    res["prices on grid (book levels off / trades off)"] = f"{off} of {nprice} / {off_t} of {len(trades)}"
    bad_usd = sum(1 for t in trades if abs(float(t["usd"]) - round(int(t["qty"]) * 0.01 * float(t["maker_price"]), 2)) > 0.011)
    res["usd = qty x 0.01 x maker_price (off)"] = f"{bad_usd} of {len(trades)}"
    # volume counter vs prints. The first tape read of a market walks back to its first print, so every book row's
    # counter (read from the listing seconds after the tick's timestamp) should equal the contracts printed up to the
    # tick, give or take prints in those seconds. Rows of markets whose tape has a gap are left out.
    gapped = {g["market_id"] for g in read_rows(os.path.join(out, "tape_gaps.csv"))}
    q_by = {}
    for t in trades:
        q_by.setdefault(t["market_id"], []).append((t["ts"], int(t["qty"])))
    exact = near = n = 0
    worst = []
    for r in books:
        v = fl(r["volume"])
        if v is None or r["market_id"] in gapped:
            continue
        q = sum(qq for ts, qq in q_by.get(r["market_id"], ()) if ts <= r["ts"])
        late = sum(qq for ts, qq in q_by.get(r["market_id"], ()) if r["ts"] < ts <= r["ts"][:14] + "59:59Z")
        n += 1
        exact += abs(v - q) < 0.5
        near += abs(v - q) < 0.5 or q <= v <= q + late
        if abs(v - q) >= 0.5:
            worst.append((r["ts"], r["pair"], v, q))
    res["volume counter = contracts printed to that tick (book rows exact / or short by prints later that hour)"] = f"{exact} / {near} of {n}"
    if worst:
        res["volume mismatches (first 5)"] = worst[:5]
    no_reason = sum(1 for c in clock if int(c.get("early_reads") or 0) == 0 and not c.get("early_reason"))
    res["match clock: early books or a reason (rows missing both)"] = f"{no_reason} of {len(clock)}"
    reasons = {}
    for c in clock:
        k = "early books" if int(c.get("early_reads") or 0) else c.get("early_reason") or "?"
        reasons[k] = reasons.get(k, 0) + 1
    res["match clock rows by early status"] = reasons
    for k, v in res.items():
        print(f"{k}: {v}")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "novig_tennis"))
    ap.add_argument("--board", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "board"))
    ap.add_argument("--budget", type=float, default=PASS_BUDGET_S)
    ap.add_argument("--check", metavar="DIR")
    a = ap.parse_args()
    if a.check:
        checks(a.check)
    else:
        os.makedirs(a.out, exist_ok=True)
        run_once(a.out, a.board, budget_s=a.budget)
