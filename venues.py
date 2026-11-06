"""Mirror Novig's and Polymarket's PUBLIC tennis moneyline order books to CSV. Standard library only.

Same deal as poll.py: public numbers anyone can read without logging in, no model, no stake, no result, and
nothing here authenticates or places an order. Both venues publish their trades after the fact (Novig as daily
CSVs at data.novig.com, Polymarket through its data API), but neither keeps the ORDER BOOK -- what was resting at
each price at a given moment. That is the one thing that has to be recorded as it happens, so it is the only
thing this file records, plus each market's identity (ids, players, start time) so the trades can be labelled.

WHAT THIS WRITES
  venues/novig/catalog.csv             one row per moneyline market ever seen: ids, players, start, status, first/last seen
  venues/novig/books/YYYY-MM-DD.csv    a row each time a market's book CHANGED: resting bids per outcome, top levels
  venues/polymarket/catalog.csv        the same for Polymarket's tennis moneylines (condition id, tokens, players)
  venues/polymarket/books/YYYY-MM-DD.csv
  venues/state.json                    each market's last recorded book, so an unchanged book writes nothing

Books are written only for markets starting within LOOKAHEAD_H or already in play: that is where the bets are,
and it keeps the files small. A book level is "price:quantity", best first, joined by "|".
  Novig: every resting order is a BID for one outcome (buying A at p is offering B at 1 - p), so each outcome's
         bids are kept. A contract pays 1 cent: dollars = quantity x price x 0.01.
  Polymarket: the first outcome's token book, bids and asks (the second token's book is its mirror). Size is
         in shares that pay $1.
"""
import csv, datetime as dt, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "venues")
NOVIG = "https://api.novig.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
LEAGUES = ("ATP", "WTA")
STATUSES = ("OPEN_PREGAME", "OPEN_INGAME")       # the public vocabulary: /v3/public/types/event-statuses
LOOKAHEAD_H = 36
LIVE_H = 6                                        # a start older than this is finished or a placeholder date
ITF = re.compile(r"^[MW]\d{2,3} ")               # Polymarket lists ITF (M15, W35, ...); Novig has none and we bet none
LEVELS = 6
NOW = dt.datetime.now(dt.timezone.utc)
STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


def req(url, body=None, pause=0.8):
    """GET (or POST a JSON body) with a pause before every call: Novig answers 429 to a burst of five."""
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        time.sleep(pause)
        try:
            r = urllib.request.Request(url, data=data, headers={"Accept": "application/json", "User-Agent": "venue-board",
                                                                **({"Content-Type": "application/json"} if data else {})})
            with urllib.request.urlopen(r, timeout=25) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))


def iso_ms(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ms else ""


def levels(pairs):
    """[(price, qty)] -> 'p:q|p:q' over the best LEVELS prices, quantities summed per price."""
    agg = {}
    for p, q in pairs:
        agg[p] = agg.get(p, 0.0) + float(q)
    return "|".join(f"{p}:{agg[p]:g}" for p in list(agg)[:LEVELS])


def upsert_catalog(path, key, rows, cols):
    """Keep every market ever seen; refresh the mutable columns, never first_seen."""
    old = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            old = {r[key]: r for r in csv.DictReader(f)}
    for r in rows:
        prev = old.get(r[key])
        r["first_seen"] = prev["first_seen"] if prev else STAMP
        r["last_seen"] = STAMP
        old[r[key]] = {c: r.get(c, "") for c in cols}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for k in sorted(old, key=lambda k: (old[k]["first_seen"], k)):
            w.writerow(old[k])


def append_books(venue, rows, cols, state):
    """Append only the books that changed since the last recorded one."""
    seen = state.setdefault(venue, {})
    new = [r for r in rows if seen.get(r["market_id"]) != r["sig"]]
    for r in new:
        seen[r["market_id"]] = r["sig"]
    if not new:
        return 0
    path = os.path.join(OUT, venue, "books", f"{NOW:%Y-%m-%d}.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if fresh:
            w.writeheader()
        w.writerows(new)
    return len(new)


def soon(start_iso, status):
    if status == "OPEN_INGAME" or not start_iso:
        return status == "OPEN_INGAME"
    s = dt.datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return s - NOW <= dt.timedelta(hours=LOOKAHEAD_H)


def novig(state):
    cat, books = [], []
    for lg in LEAGUES:
        for st in STATUSES:
            after = None
            while True:
                q = dict(league=lg, status=st, limit=200, **({"after": after} if after else {}))
                d = req(f"{NOVIG}/v3/public/catalog/events?{urllib.parse.urlencode(q)}")
                for e in d.get("items") or []:
                    start = iso_ms(e.get("startsTs"))
                    ms = req(f"{NOVIG}/v3/public/catalog/markets?" + urllib.parse.urlencode(
                        dict(league=lg, event=e["eventId"], marketType="MONEY", limit=50))).get("items") or []
                    for m in ms:
                        if m.get("marketType") != "MONEY" or len(m.get("outcomes") or []) != 2:
                            continue
                        o1, o2 = m["outcomes"]
                        cat.append(dict(market_id=m["marketId"], event_id=e["eventId"], league=lg, description=e.get("description", ""),
                                        start=start, status=e.get("status", ""), outcome1_id=o1["outcomeId"], outcome1=o1.get("name", ""),
                                        outcome2_id=o2["outcomeId"], outcome2=o2.get("name", ""), volume=m.get("volume", ""),
                                        open_interest=m.get("openInterest", "")))
                        if not soon(start, e.get("status")):
                            continue
                        b = req(f"{NOVIG}/v3/public/catalog/markets/{m['marketId']}/book?depth={LEVELS}").get("orders") or {}
                        l1 = levels([(x["price"], x["qty"]) for x in b.get(o1["outcomeId"], [])])
                        l2 = levels([(x["price"], x["qty"]) for x in b.get(o2["outcomeId"], [])])
                        books.append(dict(seen_at=STAMP, market_id=m["marketId"], status=e.get("status", ""), outcome1=o1.get("name", ""),
                                          bids1=l1, last1=o1.get("last") or "", outcome2=o2.get("name", ""), bids2=l2,
                                          last2=o2.get("last") or "", volume=m.get("volume", ""), sig=f"{l1}#{l2}#{e.get('status')}"))
                after = d.get("next")
                if not after or not d.get("items"):
                    break
    upsert_catalog(os.path.join(OUT, "novig", "catalog.csv"), "market_id", cat,
                   ["market_id", "event_id", "league", "description", "start", "status", "outcome1_id", "outcome1", "outcome2_id",
                    "outcome2", "volume", "open_interest", "first_seen", "last_seen"])
    n = append_books("novig", books, ["seen_at", "market_id", "status", "outcome1", "bids1", "last1", "outcome2", "bids2", "last2", "volume"], state)
    return len(cat), len(books), n


def polymarket(state):
    cat, toks, off = [], [], 0
    while off < 3000:
        d = req(f"{GAMMA}/events?" + urllib.parse.urlencode(dict(closed="false", active="true", tag_slug="tennis", limit=100, offset=off,
                                                                    end_date_min=STAMP)), pause=0.3)
        for e in d:
            if ITF.match(e.get("title") or ""):
                continue
            for m in e.get("markets") or []:
                if (m.get("sportsMarketType") or "") != "moneyline" or m.get("closed"):
                    continue
                try:
                    outs, ids = json.loads(m.get("outcomes") or "[]"), json.loads(m.get("clobTokenIds") or "[]")
                except ValueError:
                    continue
                if len(outs) != 2 or len(ids) != 2 or "/" in outs[0] + outs[1]:      # doubles: outside every book we keep
                    continue
                gs = (m.get("gameStartTime") or "")[:19].replace(" ", "T")
                start = gs + "Z" if gs else ""
                cat.append(dict(market_id=m["conditionId"], event=e.get("title", ""), question=m.get("question", ""), start=start,
                                outcome1=outs[0], outcome2=outs[1], token1=ids[0], token2=ids[1], volume=m.get("volume", ""),
                                accepting=m.get("acceptingOrders", "")))
                s0 = dt.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc) if start else None
                live = bool(s0) and s0 <= NOW < s0 + dt.timedelta(hours=LIVE_H)
                if s0 and s0 + dt.timedelta(hours=LIVE_H) <= NOW:
                    continue
                if start and (live or soon(start, "")) and m.get("acceptingOrders"):
                    toks.append((m["conditionId"], ids[0], outs[0], live))
        off += 100
        if len(d) < 100:
            break
    upsert_catalog(os.path.join(OUT, "polymarket", "catalog.csv"), "market_id", cat,
                   ["market_id", "event", "question", "start", "outcome1", "outcome2", "token1", "token2", "volume", "accepting",
                    "first_seen", "last_seen"])
    books = []
    for i in range(0, len(toks), 50):
        chunk = toks[i:i + 50]
        res = req(f"{CLOB}/books", body=[{"token_id": t} for _, t, _, _ in chunk], pause=0.3)
        by = {b.get("asset_id"): b for b in res or []}
        for cid, t, o1, live in chunk:
            b = by.get(t) or {}
            bids = sorted(((x["price"], x["size"]) for x in b.get("bids") or []), key=lambda x: -float(x[0]))
            asks = sorted(((x["price"], x["size"]) for x in b.get("asks") or []), key=lambda x: float(x[0]))
            lb, la = levels(bids), levels(asks)
            books.append(dict(seen_at=STAMP, market_id=cid, live=int(live), outcome1=o1, bids=lb, asks=la,
                              last=b.get("last_trade_price", ""), sig=f"{lb}#{la}#{int(live)}"))
    n = append_books("polymarket", books, ["seen_at", "market_id", "live", "outcome1", "bids", "asks", "last"], state)
    return len(cat), len(books), n


def main():
    sp = os.path.join(OUT, "state.json")
    state = json.load(open(sp)) if os.path.exists(sp) else {}
    ok = 0
    for name, fn in (("novig", novig), ("polymarket", polymarket)):
        try:
            c, b, n = fn(state)
            print(f"{name}: {c} markets catalogued, {b} books read, {n} changed and written")
            ok += 1
        except Exception as e:          # one venue down must not cost the other its tick
            print(f"{name}: FAILED {type(e).__name__}: {e}", file=sys.stderr)
    os.makedirs(OUT, exist_ok=True)
    with open(sp, "w") as f:
        json.dump(state, f, separators=(",", ":"), sort_keys=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
