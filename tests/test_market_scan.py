from datetime import date

from fixtures.patterns import index_series, pullback_series
from swing_agent import db
from swing_agent.market_scan import format_market_scan, parse_equity_list, scan_market
from swing_agent.scanner import Signal, rank_and_select

EQUITY_L = (
    "SYMBOL,NAME OF COMPANY, SERIES,DATE OF LISTING\n"
    "AAA,Aaa Ltd,EQ,01-JAN-2000\n"
    "BBB,Bbb Ltd,EQ,01-JAN-2000\n"
    "ZZZ,Zzz Ltd,BE,01-JAN-2000\n"
).encode()


def test_parse_equity_list_keeps_eq_series_and_known_sectors():
    df = parse_equity_list(EQUITY_L, {"AAA": "IT"})
    assert df["symbol"].tolist() == ["AAA", "BBB"]
    assert df.set_index("symbol")["sector"].to_dict() == {"AAA": "IT", "BBB": "Unknown"}


def _setup(tmp_path):
    list_path = tmp_path / "nse_all.csv"
    parse_equity_list(EQUITY_L).to_csv(list_path, index=False)
    all_conn = db.connect(tmp_path / "all.db")
    main_conn = db.connect(tmp_path / "main.db")
    pb = pullback_series()
    idx = index_series("bull", len(pb))
    db.upsert_prices(all_conn, "^NSEI", idx.reset_index())
    db.upsert_prices(all_conn, "^INDIAVIX", idx.assign(open=15.0, high=15.0, low=15.0, close=15.0).reset_index())
    db.upsert_prices(all_conn, "AAA", pb.reset_index())
    db.upsert_prices(all_conn, "BBB", pullback_series(depth_steps=9, step_pct=2.0).reset_index())
    return list_path, all_conn, main_conn


def test_scan_market_finds_setups_across_the_listing(cfg, tmp_path):
    list_path, all_conn, main_conn = _setup(tmp_path)
    m = scan_market(cfg, capital=50000, download=False, all_conn=all_conn, main_conn=main_conn,
                    list_path=list_path)
    assert m.what_if and m.capital == m.cash == 50000
    assert m.listed == 2 and m.with_data == 2
    assert [s.symbol for s in m.result.signals] == ["AAA"]
    assert m.result.signals[0].qty >= 1
    text = format_market_scan(m, cfg)
    assert "Whole-NSE scan" in text and "AAA" in text and "WHAT-IF capital ₹50,000" in text


def test_unknown_sector_is_not_capped():
    def sig(sym):
        return Signal(symbol=sym, name="", sector="Unknown", date=date(2024, 1, 1), signal_close=100,
                      buy_zone_low=100, buy_zone_high=101, stop=95, target=113, r=6, stop_pct=6,
                      rr=2, score=1, rs=None, atr=2, swing_low=96, reasons=[], warnings=[])

    ranked = rank_and_select([sig("A"), sig("B"), sig("C")], max_positions=5, max_per_sector=2)
    assert [s.status for s in ranked] == ["TAKE"] * 3
