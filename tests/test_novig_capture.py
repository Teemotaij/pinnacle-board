"""novig_capture.py with every Novig endpoint faked: no network. Run: python3 -m unittest discover -s tests"""
import csv, datetime as dt, gzip, io, json, os, sys, tempfile, unittest, urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import novig_capture as NC

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)


def ms(t):
    return int(t.timestamp() * 1000)


def uuid7(t, tail="7000-8000-000000000000"):
    h = f"{ms(t):012x}"
    return f"{h[:8]}-{h[8:12]}-{tail}"


def mkt(mid_time, ev, n1, n2, start, volume, bid1=None, bid2=None, suffix="a"):
    mid = uuid7(mid_time, f"7000-8000-00000000000{suffix}")
    o = [dict(outcomeId=mid + "-o1", name=n1), dict(outcomeId=mid + "-o2", name=n2)]
    if bid1: o[0]["bid"] = dict(price=str(bid1), qty=100)
    if bid2: o[1]["bid"] = dict(price=str(bid2), qty=100)
    return dict(marketId=mid, eventId=ev, marketType="MONEY", startsTs=ms(start), outcomes=o, volume=volume, openInterest=0)


class Fake:
    """Novig's public catalog in memory. trades[mid] newest first; pages of `page` prints with a `next` token."""

    def __init__(self, events, markets, books, trades, page=5000):
        self.ev, self.mk, self.bk, self.tr, self.page = events, markets, books, trades, page
        self.stats = dict(calls=0, http_429=0, retries=0)
        self.log = []

    def events(self, league, status):
        self.stats["calls"] += 1
        return [e for e in self.ev if e["league"] == league and e["status"] == status]

    def money_markets(self, league):
        self.stats["calls"] += 1
        return [m for m in self.mk if m.get("_lg", "ATP") == league]

    def book(self, mid, depth=NC.DEPTH):
        self.stats["calls"] += 1
        self.log.append(("book", mid))
        return dict(marketId=mid, seq=1, orders=self.bk.get(mid, {}))

    def trades_page(self, mid, limit=5000, after=None):
        self.stats["calls"] += 1
        self.log.append(("trades", mid, after))
        items = self.tr.get(mid, [])
        i = int(after or 0)
        page = items[i:i + self.page]
        nxt = str(i + self.page) if i + self.page < len(items) else None
        return page, nxt


def board(d, open_rows=(), close_rows=(), live_rows=(), ticks=()):
    os.makedirs(os.path.join(d, "ticks"), exist_ok=True)
    def w(fn, cols, rows):
        with open(os.path.join(d, fn), "w", newline="") as f:
            x = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); x.writeheader(); x.writerows(rows)
    oc = ["pair", "k1", "k2", "name1", "name2", "league", "start_time", "fair1", "fair2", "quote1", "quote2", "limit_usd", "seen_at", "obs_at", "cutoff_at", "version"]
    w("pin_open.csv", oc, open_rows)
    w("pin_close.csv", oc + ["mins_to_start", "n_seen", "matchup_id"], close_rows)
    w("pin_live.csv", ["pair", "matchup_id", "first_live"], live_rows)
    by = {}
    for r in ticks:
        by.setdefault(r["seen_at"][:10], []).append(r)
    for day, rows in by.items():
        w(os.path.join("ticks", f"{day}.csv"), ["pair", "seen_at", "obs_at", "start_time", "mins_to_start", "fair1", "fair2", "quote1", "quote2", "limit_usd", "version"], rows)


def pin_row(pair, start, seen, f1):
    k1, k2 = pair.split("|")[0] + "|" + pair.split("|")[1], pair.split("|")[2] + "|" + pair.split("|")[3]
    return dict(pair=pair, k1=k1, k2=k2, start_time=start, fair1=f1, fair2=1 - f1, seen_at=seen)


def trade(tid_t, outcome, price, qty, n=0):
    tid = uuid7(tid_t, f"7000-8000-{n:012d}")
    return dict(tradeId=tid, takeId=tid, outcomeId=outcome, price=str(price), qty=qty, ts=ms(tid_t))


class Basics(unittest.TestCase):
    def test_name_key_pairs_novig_short_names_with_pinnacle_full_names(self):
        self.assertEqual(NC.name_key("M. Donald"), NC.name_key("Matthew William Donald"))
        self.assertEqual(NC.name_key("O. Roca-Batalla"), NC.name_key("Oriol Roca Batalla"))
        self.assertEqual(NC.name_key("S. Gueymard Wayenburg"), "wayenburg|s")

    def test_grid(self):
        for p in ("0.001", "0.05", "0.055", "0.5", "0.945", "0.95", "0.999", "0.205"):
            self.assertTrue(NC.on_grid(p), p)
        for p in ("0", "1", "0.052", "0.501", "0.9455", "1.2"):
            self.assertFalse(NC.on_grid(p), p)

    def test_uuid7_time_is_the_creation_time(self):
        t = dt.datetime(2026, 11, 18, 16, 15, 34, tzinfo=UTC)
        self.assertEqual(NC.uuid7_time(uuid7(t)), t)
        self.assertIsNone(NC.uuid7_time("zz"))

    def test_asks_are_the_other_sides_bids_and_le_pin_sums_dollars(self):
        bids2 = NC.levels([dict(price="0.40", qty=1000), dict(price="0.40", qty=500), dict(price="0.35", qty=2000)])
        self.assertEqual(bids2, [(0.4, 1500), (0.35, 2000)])
        asks1 = NC.ask_ladder(bids2)
        self.assertEqual(asks1, [(0.6, 9.0), (0.65, 13.0)])               # 1500 x 0.01 x 0.60; 2000 x 0.01 x 0.65
        c = NC.side_cols("p1", [(0.55, 100)], asks1, 0.63)
        self.assertEqual((c["p1_ask"], c["p1_ask_usd"], c["p1_le_pin"], c["p1_le_pin2"]), (0.6, 9.0, 9.0, 22.0))
        self.assertEqual(c["p1_asks"], "0.600:9|0.650:13")
        self.assertIsNone(NC.side_cols("p1", [], [], None)["p1_le_pin"])

    def test_gzip_appends_are_one_readable_file_with_one_header(self):
        with tempfile.TemporaryDirectory() as d:
            fn = os.path.join(d, "x", "a.csv.gz")
            NC.append_gz(fn, ["a", "b"], [dict(a=1, b=2)])
            NC.append_gz(fn, ["a", "b"], [dict(a=3, b=4)])
            self.assertEqual(NC.read_rows(fn), [dict(a="1", b="2"), dict(a="3", b="4")])


class PinRef(unittest.TestCase):
    def test_fair_in_force_and_first_live_of_this_meeting(self):
        with tempfile.TemporaryDirectory() as d:
            p = "donald|m|kwon|s"
            board(d, open_rows=[pin_row(p, "2026-11-19 11:30:00", "2026-11-19 08:00:00", 0.70)],
                  close_rows=[pin_row(p, "2026-11-19 11:30:00", "2026-11-19 09:50:00", 0.66)],
                  live_rows=[dict(pair=p, matchup_id=1, first_live="2026-10-01 10:00:00"), dict(pair=p, matchup_id=2, first_live="2026-11-19 11:33:00")],
                  ticks=[dict(pin_row(p, "2026-11-19 11:30:00", "2026-11-19 09:00:00", 0.68))])
            pin = NC.Pin(d, NOW)
            self.assertEqual(pin.fair(p, NOW.replace(hour=7))[:3], (None, None, ""))
            self.assertEqual(pin.fair(p, NOW.replace(hour=8, minute=30))[2], "open")
            f1, f2, src, at = pin.fair(p, NOW.replace(hour=9, minute=30))
            self.assertEqual((round(f1, 2), src), (0.68, "tick"))
            self.assertEqual(pin.fair(p, NOW)[2], "close")
            self.assertEqual(pin.first_priced(p), dt.datetime(2026, 11, 19, 8, tzinfo=UTC))
            self.assertEqual(pin.first_live(p, dt.datetime(2026, 11, 19, 11, 30, tzinfo=UTC)), dt.datetime(2026, 11, 19, 11, 33, tzinfo=UTC))


class Tick(unittest.TestCase):
    def setUp(self):
        guard = mock.patch("urllib.request.urlopen", side_effect=AssertionError("a test reached for the network"))
        guard.start(); self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.out, self.bd = os.path.join(self.tmp.name, "out"), os.path.join(self.tmp.name, "board")
        os.makedirs(self.out)
        # A: listed by Novig 1 h ago (early), starts in 30 h, Pinnacle priced 20 h ago.
        # B: listed 40 h ago, starts in 5 h (near), on the board.  C: listed 40 h ago, starts in 30 h (hourly), not on the board.
        self.A = mkt(NOW - dt.timedelta(hours=1), "evA", "M. Donald", "S. Kwon", NOW + dt.timedelta(hours=30), 300, suffix="a")
        self.B = mkt(NOW - dt.timedelta(hours=40), "evB", "B. Gojo", "N. Basavareddy", NOW + dt.timedelta(hours=5), 0, suffix="b")
        self.C = mkt(NOW - dt.timedelta(hours=40), "evC", "L. Tu", "T. Schoolkate", NOW + dt.timedelta(hours=30), 0, suffix="c")
        self.events = [dict(eventId=e, league="ATP", status="OPEN_PREGAME", startsTs=0) for e in ("evA", "evB", "evC")]
        a1, a2 = self.A["outcomes"][0]["outcomeId"], self.A["outcomes"][1]["outcomeId"]     # Donald, Kwon
        self.books = {self.A["marketId"]: {a1: [dict(price="0.30", qty=20000)], a2: [dict(price="0.68", qty=10000), dict(price="0.65", qty=5000)]},
                      self.B["marketId"]: {}}
        self.trades = {self.A["marketId"]: [trade(NOW - dt.timedelta(minutes=10), a2, "0.68", 200, 2),
                                            trade(NOW - dt.timedelta(minutes=30), a1, "0.30", 100, 1)]}
        board(self.bd, open_rows=[pin_row("donald|m|kwon|s", "2026-11-20 16:00:00", "2026-11-18 14:00:00", 0.33),
                                  pin_row("basavareddy|n|gojo|b", "2026-11-19 15:00:00", "2026-11-18 14:00:00", 0.45)])

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, **kw):
        return Fake(self.events, [self.A, self.B, self.C], self.books, self.trades, **kw)

    def books_on_file(self):
        return NC.read_rows(os.path.join(self.out, "books", "2026-11-19.csv"))

    def trades_on_file(self):
        return NC.read_rows(os.path.join(self.out, "trades", "2026-11-19.csv"))

    def test_one_tick_reads_early_first_and_writes_books_trades_clock_and_log(self):
        nc = self.fake()
        log = NC.run_once(self.out, self.bd, nc=nc, now=NOW)
        self.assertEqual([x[1] for x in nc.log if x[0] == "book"], [self.A["marketId"], self.B["marketId"], self.C["marketId"]])
        self.assertEqual((log["due_early"], log["due_near"], log["due_hourly"], log["unread"]), (1, 1, 1, 0))
        b = {r["market_id"]: r for r in self.books_on_file()}
        a = b[self.A["marketId"]]
        # side 1 = donald|m (alphabetical), Pinnacle fair1 0.33
        self.assertEqual((a["pair"], a["name1"], a["pin_match"], a["early"], a["why_due"]), ("donald|m|kwon|s", "M. Donald", "yes", "1", "early"))
        self.assertEqual((a["p1_bid"], a["p1_ask"], a["p2_ask"]), ("0.3", "0.32", "0.7"))      # ask1 = 1 - Kwon's 0.68
        self.assertEqual(float(a["p1_le_pin"]), 32.0)           # asks 0.32 ($32 = 10000 x 0.01 x 0.32) and 0.35 ($17.50): only 0.32 <= Pinnacle's 0.33
        self.assertEqual(float(a["p1_le_pin2"]), 49.5)          # both <= 0.35
        self.assertEqual((float(a["h_since_novig"]), float(a["h_to_start"])), (1.0, 30.0))
        self.assertEqual(b[self.C["marketId"]]["pin_match"], "")
        t = self.trades_on_file()
        self.assertEqual([x["maker_name"] for x in t], ["M. Donald", "S. Kwon"])               # oldest first
        self.assertEqual((t[0]["maker_side"], t[0]["taker_side"], t[0]["taker_price"], t[0]["usd"], t[0]["taker_usd"]), ("1", "2", "0.7", "0.3", "0.7"))
        self.assertEqual(round(float(t[1]["pin_novig"]), 2), 0.67)                               # Kwon is side 2: 1 - 0.33
        c = {r["event_id"]: r for r in NC.read_rows(os.path.join(self.out, "match_clock.csv"))}
        self.assertEqual((c["evA"]["early_reads"], c["evA"]["first_trade_price"], c["evA"]["first_ready"]), ("1", "0.3", NC.iso(NOW)))
        self.assertEqual(c["evB"]["first_two_sided"], "")                                      # empty book
        self.assertEqual(c["evB"]["early_reason"], "windows ended before capture started")
        lg = NC.read_rows(os.path.join(self.out, "capture_log.csv"))
        self.assertEqual((lg[0]["read"], lg[0]["trade_rows"], lg[0]["pin_matched"]), ("3", "2", "2"))

    def test_second_tick_dedupes_the_tape_and_reads_it_only_when_volume_moved(self):
        NC.run_once(self.out, self.bd, nc=self.fake(), now=NOW)
        nc = self.fake()
        NC.run_once(self.out, self.bd, nc=nc, now=NOW + dt.timedelta(minutes=5))
        self.assertFalse([x for x in nc.log if x[0] == "trades"])                               # volume unchanged
        self.assertEqual([x[1] for x in nc.log if x[0] == "book"], [self.A["marketId"], self.B["marketId"]])   # C read 5 min ago: hourly, not due
        a2 = self.A["outcomes"][1]["outcomeId"]
        self.trades[self.A["marketId"]].insert(0, trade(NOW + dt.timedelta(minutes=7), a2, "0.675", 50, 3))
        self.A["volume"] = 350
        NC.run_once(self.out, self.bd, nc=self.fake(), now=NOW + dt.timedelta(minutes=10))
        t = self.trades_on_file()
        self.assertEqual(len(t), 3)
        self.assertEqual(len({x["trade_id"] for x in t}), 3)
        self.assertEqual(NC.read_rows(os.path.join(self.out, "capture_log.csv"))[-1]["trade_rows"], "1")

    def test_hourly_match_comes_back_after_every_min(self):
        NC.run_once(self.out, self.bd, nc=self.fake(), now=NOW)
        nc = self.fake()
        NC.run_once(self.out, self.bd, nc=nc, now=NOW + dt.timedelta(minutes=NC.EVERY_MIN))
        self.assertIn(("book", self.C["marketId"]), nc.log)

    def test_budget_leaves_the_rest_unread_and_counts_it(self):
        ticks = iter(range(0, 10000, 150))
        log = NC.run_once(self.out, self.bd, nc=self.fake(), now=NOW, budget_s=200, clock=lambda: next(ticks))
        self.assertEqual((log["read"], log["unread"], log["read_early"]), (1, 2, 1))
        c = {r["event_id"]: r for r in NC.read_rows(os.path.join(self.out, "match_clock.csv"))}
        self.assertEqual(c["evC"]["unread_due"], "1")

    def test_a_tape_longer_than_max_pages_is_logged_as_a_gap(self):
        a1 = self.A["outcomes"][0]["outcomeId"]
        NC.run_once(self.out, self.bd, nc=self.fake(), now=NOW)
        self.trades[self.A["marketId"]] = [trade(NOW + dt.timedelta(minutes=9, seconds=-i), a1, "0.31", 1, 100 + i) for i in range(30)] + self.trades[self.A["marketId"]]
        self.A["volume"] = 330
        with mock.patch.object(NC, "MAX_PAGES", 2):
            NC.run_once(self.out, self.bd, nc=self.fake(page=5), now=NOW + dt.timedelta(minutes=10))
        g = NC.read_rows(os.path.join(self.out, "tape_gaps.csv"))
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["pages"], "2")
        self.assertEqual(len(self.trades_on_file()), 2 + 10)

    def test_walk_back_stops_at_the_mark(self):
        NC.run_once(self.out, self.bd, nc=self.fake(), now=NOW)
        a1 = self.A["outcomes"][0]["outcomeId"]
        self.trades[self.A["marketId"]] = [trade(NOW + dt.timedelta(minutes=8, seconds=-i), a1, "0.31", 1, 200 + i) for i in range(6)] + self.trades[self.A["marketId"]]
        self.A["volume"] = 306
        nc = self.fake(page=4)
        NC.run_once(self.out, self.bd, nc=nc, now=NOW + dt.timedelta(minutes=10))
        self.assertEqual(len([x for x in nc.log if x[0] == "trades"]), 2)                        # page 2 reaches the old prints
        self.assertEqual(len(self.trades_on_file()), 8)
        self.assertFalse(os.path.exists(os.path.join(self.out, "tape_gaps.csv")))

    def test_in_play_is_near_and_a_rematch_on_the_board_is_stale(self):
        self.events[1]["status"] = "OPEN_INGAME"
        board(self.bd, open_rows=[pin_row("donald|m|kwon|s", "2026-11-10 16:00:00", "2026-11-09 14:00:00", 0.33)])
        NC.run_once(self.out, self.bd, nc=self.fake(), now=NOW)
        b = {r["market_id"]: r for r in self.books_on_file()}
        self.assertEqual((b[self.B["marketId"]]["in_play"], b[self.B["marketId"]]["why_due"]), ("1", "live"))
        self.assertEqual((b[self.A["marketId"]]["pin_match"], b[self.A["marketId"]]["pin_fair1"]), ("stale", ""))

    def test_in_play_is_read_last_and_never_crowds_out_pre_match(self):
        self.events[1]["status"] = "OPEN_INGAME"                  # B in play, starts "in 5 h" by Novig's clock
        nc = self.fake()
        NC.run_once(self.out, self.bd, nc=nc, now=NOW)
        self.assertEqual([x[1] for x in nc.log if x[0] == "book"], [self.A["marketId"], self.C["marketId"], self.B["marketId"]])
        ticks = iter(range(0, 10000, 90))                        # room for two reads: early A and hourly C, not live B
        log = NC.run_once(os.path.join(self.out, "2"), self.bd, nc=self.fake(), now=NOW, budget_s=200, clock=lambda: next(ticks))
        self.assertEqual((log["read_early"], log["read_hourly"], log["read_live"], log["due_live"], log["unread"]), (1, 1, 0, 1, 1))

    def test_a_failing_endpoint_is_logged_not_raised(self):
        nc = self.fake()
        def boom(mid, depth=NC.DEPTH):
            raise NC.NovigError(500, "x", "/book")
        nc.book = boom
        log = NC.run_once(self.out, self.bd, nc=nc, now=NOW)
        self.assertEqual((log["read"], log["unread"], log["errors"]), (0, 3, 3))

    def test_a_finished_day_is_sealed_to_gzip_on_the_next_days_first_run(self):
        NC.run_once(self.out, self.bd, nc=self.fake(), now=NOW)
        n = len(self.books_on_file())
        log = NC.run_once(self.out, self.bd, nc=self.fake(), now=NOW + dt.timedelta(days=1))
        self.assertFalse(os.path.exists(os.path.join(self.out, "books", "2026-11-19.csv")))
        self.assertEqual(len(NC.read_rows(os.path.join(self.out, "books", "2026-11-19.csv.gz"))), n)
        self.assertEqual(len(NC.read_rows(os.path.join(self.out, "trades", "2026-11-19.csv.gz"))), 2)
        self.assertTrue(os.path.exists(os.path.join(self.out, "books", "2026-11-20.csv")))
        self.assertIn("sealed 2 day files", log["note"])
        # a late row for a sealed day is added, never overwrites it
        NC.append_csv(os.path.join(self.out, "books", "2026-11-19.csv"), NC.BOOK_COLS, [dict(ts="late")])
        NC.seal_days(self.out, "2026-11-20")
        self.assertEqual(len(NC.read_rows(os.path.join(self.out, "books", "2026-11-19.csv.gz"))), n + 1)
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertTrue(NC.checks(self.out)["ask = 1 - other best bid (rows off)"].startswith("0 of"))

    def test_checks_pass_on_a_tick(self):
        NC.run_once(self.out, self.bd, nc=self.fake(), now=NOW)
        with mock.patch("sys.stdout", io.StringIO()):
            r = NC.checks(self.out)
        self.assertTrue(r["ask = 1 - other best bid (rows off)"].startswith("0 of"))
        self.assertTrue(r["usd = qty x 0.01 x maker_price (off)"].startswith("0 of"))
        self.assertTrue(r["prices on grid (book levels off / trades off)"].startswith("0 of"))
        self.assertTrue(r["match clock: early books or a reason (rows missing both)"].startswith("0 of"))


class ClientRetries(unittest.TestCase):
    """The real Client against a faked urlopen: no network."""

    def resp(self, body):
        m = mock.MagicMock()
        m.__enter__.return_value = io.BytesIO(json.dumps(body).encode())
        m.__exit__.return_value = False
        return m

    def err(self, code):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b"x"))

    def test_429_is_retried_and_counted(self):
        c = NC.Client(pause=0)
        with mock.patch("time.sleep"), mock.patch("urllib.request.urlopen", side_effect=[self.err(429), self.err(503), self.resp(dict(items=[1], next=None))]):
            self.assertEqual(c.events("ATP", "OPEN_PREGAME"), [1])
        self.assertEqual(c.stats, dict(calls=3, http_429=1, retries=2))

    def test_400_raises_without_retry(self):
        c = NC.Client(pause=0)
        with mock.patch("time.sleep"), mock.patch("urllib.request.urlopen", side_effect=[self.err(400)]):
            with self.assertRaises(NC.NovigError):
                c.book("m")
        self.assertEqual(c.stats["calls"], 1)

    def test_pages_follow_next(self):
        c = NC.Client(pause=0)
        seen = []
        def fake(req, timeout=None):
            seen.append(req.full_url)
            return self.resp(dict(items=[1, 2], next="tok") if "after" not in req.full_url else dict(items=[3], next=None))
        with mock.patch("time.sleep"), mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(c.money_markets("WTA"), [1, 2, 3])
        self.assertIn("after=tok", seen[1])
        self.assertTrue(all(u.startswith(NC.HOST + "/v3/public/catalog/markets?") for u in seen))

    def test_trades_page_returns_items_and_next(self):
        c = NC.Client(pause=0)
        with mock.patch("time.sleep"), mock.patch("urllib.request.urlopen", return_value=self.resp(dict(items=[dict(a=1)], next="n2"))):
            self.assertEqual(c.trades_page("m", after="n1"), ([dict(a=1)], "n2"))


if __name__ == "__main__":
    unittest.main()
