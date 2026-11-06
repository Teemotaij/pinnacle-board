"""Mirror Novig's and Polymarket US's PUBLIC tennis moneyline order books to CSV. Standard library only.

Same deal as poll.py: public numbers anyone can read without logging in, no model, no stake, no result, and
nothing here authenticates or places an order. Both venues publish their trades after the fact (Novig as daily
CSVs at data.novig.com; Polymarket US as book-derived price history), but neither keeps the ORDER BOOK -- what was resting at
each price at a given moment. That is the one thing that has to be recorded as it happens, so it is the only
thing this file records, plus each market's identity (ids, players, start time) so the trades can be labelled.

WHAT THIS WRITES
  venues/novig/catalog.csv             one row per moneyline market ever seen: ids, players, start, status, first/last seen
  venues/novig/books/YYYY-MM-DD.csv    a row each time a market's book CHANGED: resting bids per outcome, top levels
  venues/polymarket_us/catalog.csv     the same for Polymarket US (market slug, players, start, listing time, fee)
  venues/polymarket_us/books/YYYY-MM-DD.csv
  (venues/polymarket/ holds the INTERNATIONAL exchange's books from 6 Nov 2026 22:45-23:10 UTC only: US accounts
   cannot trade it, so TJ switched the recorder to Polymarket US the same evening)
  venues/state.json                    each market's last recorded book, so an unchanged book writes nothing

Books are written only for markets starting within LOOKAHEAD_H or already in play: that is where the bets are,
and it keeps the files small. A book level is "price:quantity", best first, joined by "|".
  Novig: every resting order is a BID for one outcome (buying A at p is offering B at 1 - p), so each outcome's
         bids are kept. A contract pays 1 cent: dollars = quantity x price x 0.01.
  Polymarket US: the book of the market's LONG side (the first-named player), bids and offers, plus the running
         stats (last trade, shares and dollars traded). Size is in contracts paying $1.
"""
import csv, datetime as dt, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "venues")
NOVIG = "https://api.novig.com"
PMUS = "https://gateway.polymarket.us"           # public: 20 requests a second per IP
PMUS_LEAGUES = ("atp", "atpcq", "wta")            # singles; atpdb / wtadb are doubles
LEAGUES = ("ATP", "WTA")
STATUSES = ("OPEN_PREGAME", "OPEN_INGAME")       # the public vocabulary: /v3/public/types/event-statuses
LOOKAHEAD_H = 36
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


def pmus(state):
    cat, books = [], []
    for lg in PMUS_LEAGUES:
        off = 0
        while off < 2000:
            d = req(f"{PMUS}/v2/leagues/{lg}/events?" + urllib.parse.urlencode(dict(limit=100, offset=off)), pause=0.1)
            evs = d.get("events") or []
            for e in evs:
                if e.get("ended") or e.get("closed"):
                    continue
                for m in e.get("markets") or []:
                    if m.get("marketType") != "moneyline" or m.get("closed"):
                        continue
                    sides = m.get("marketSides") or []
                    if len(sides) != 2:
                        continue
                    long_ = [x for x in sides if x.get("long")] or sides[:1]
                    other = [x for x in sides if x is not long_[0]]
                    start = (e.get("startTime") or m.get("gameStartTime") or "")[:19]
                    start = start + "Z" if start else ""
                    cat.append(dict(market_id=m["slug"], league=lg, event=e.get("title", ""), start=start, created=m.get("createdAt", ""),
                                    outcome1=long_[0].get("description", ""), outcome2=other[0].get("description", "") if other else "",
                                    fee=m.get("feeCoefficient", ""), tick=m.get("orderPriceMinTickSize", ""), min_qty=m.get("minimumTradeQty", "")))
                    live = bool(e.get("live"))
                    if not (live or soon(start, "")):
                        continue
                    b = req(f"{PMUS}/v1/markets/{m['slug']}/book", pause=0.1).get("marketData") or {}
                    lb = levels([(x["px"]["value"], x["qty"]) for x in b.get("bids") or []])
                    la = levels([(x["px"]["value"], x["qty"]) for x in b.get("offers") or []])
                    st = b.get("stats") or {}
                    books.append(dict(seen_at=STAMP, market_id=m["slug"], live=int(live), outcome1=long_[0].get("description", ""),
                                      bids=lb, offers=la, last=(st.get("lastTradePx") or {}).get("value", ""),
                                      shares_traded=st.get("sharesTraded", ""), usd_traded=(st.get("notionalTraded") or {}).get("value", ""),
                                      sig=f"{lb}#{la}#{int(live)}"))
            off += len(evs)
            if len(evs) < 100:
                break
    upsert_catalog(os.path.join(OUT, "polymarket_us", "catalog.csv"), "market_id", cat,
                   ["market_id", "league", "event", "start", "created", "outcome1", "outcome2", "fee", "tick", "min_qty",
                    "first_seen", "last_seen"])
    n = append_books("polymarket_us", books, ["seen_at", "market_id", "live", "outcome1", "bids", "offers", "last", "shares_traded",
                                              "usd_traded"], state)
    return len(cat), len(books), n


def main():
    sp = os.path.join(OUT, "state.json")
    state = json.load(open(sp)) if os.path.exists(sp) else {}
    ok = 0
    for name, fn in (("novig", novig), ("polymarket_us", pmus)):
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
