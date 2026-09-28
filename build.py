#!/usr/bin/env python3
"""Build Dedicated News.

You do not need to edit this file. Change feeds.yml, the stories in
prompts/stories.yml, or the talking-points voice in prompts/talking_points.md,
then run:

    python build.py

The script reads every source in feeds.yml, fetches market figures, writes
index.html next to this file, and keeps going when a quote or a feed fails.
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "feeds.yml"
OUTPUT_PATH = ROOT / "index.html"
EASTERN = ZoneInfo("America/New_York")
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
UA_FEED = (
    "Mozilla/5.0 (compatible; DedicatedNews/1.0; "
    "+https://bglisson1.github.io/Dedicated-News/)"
)
MAX_BYTES = 2_000_000
FRED_TIMEOUT = 8
YAHOO_TIMEOUT = 15
FEED_TIMEOUT = 20

STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on",
    "or", "the", "to", "with", "after", "over", "its", "it", "his", "her",
    "their", "them", "they", "she", "him", "you", "your", "our", "this", "that",
    "have", "has", "had", "was", "were", "are", "been", "be", "will", "would",
    "could", "should", "about", "amid", "during", "while", "before", "under",
    "again", "now", "still", "also", "only", "back", "off", "than", "then",
    "but", "not", "who", "how", "why", "all", "out", "just", "more", "most",
    "when", "what", "says", "said", "new", "news", "week", "report", "reports",
    "latest", "updates", "update", "live",
}

BANNED_COPY = (
    r"\bguarante",
    r"\byou should buy\b",
    r"\byou should sell\b",
    r"\bbuy the\b",
    r"\bsell the\b",
    r"\bsell now\b",
    r"\bprice target\b",
    r"\bwill rally\b",
    r"\bwill crash\b",
    r"\bwill soar\b",
)


@dataclass
class Quote:
    label: str
    symbol: str
    group: str
    ok: bool = False
    error: str | None = None
    price: float | None = None
    change: float | None = None
    pct: float | None = None
    bp: int | None = None
    as_of: datetime | None = None
    session_label: str = ""
    hint: str = ""
    prefix: str = ""
    suffix: str = ""
    decimals: int = 2
    change_style: str = "points"
    comparison: str = ""
    source_label: str = ""
    show_time: bool = False


@dataclass
class Item:
    title: str
    link: str
    source: str
    role: str
    published: datetime
    boosts: list[str] = field(default_factory=list)
    cluster_id: int = -1


@dataclass
class FeedReport:
    name: str
    url: str
    kept: int = 0
    error: str | None = None


@dataclass
class Briefing:
    headline: str
    paragraphs: list[str]
    say: list[str]
    origin: str


def main() -> int:
    config = load_config(CONFIG_PATH)
    now = datetime.now(EASTERN)
    quotes, rates, quote_log = fetch_markets(config, now)
    reports, items = fetch_news(config, now)
    clusters = cluster_items(items)
    picked = arrange(items, clusters, config, now)
    briefing, talk_log = build_talking_points(config, now, quotes, rates, picked, items)
    page = render_page(
        config=config,
        now=now,
        quotes=quotes,
        rates=rates,
        picked=picked,
        briefing=briefing,
    )
    OUTPUT_PATH.write_text(page, encoding="utf-8")

    print(f"Wrote {OUTPUT_PATH.name}")
    print("--- quotes ---")
    for line in quote_log:
        print(line)
    print("--- feeds ---")
    failed = 0
    for report in reports:
        if report.error:
            failed += 1
            print(f"  SKIP {report.name}: {report.error}", file=sys.stderr)
        else:
            print(f"  OK   {report.name}: {report.kept} headlines")
    print(
        "Stories: "
        f"financial {len(picked['financial'])}, "
        f"political {len(picked['political'])}, "
        f"more {len(picked['more'])}, "
        f"industry {len(picked['industry'])}"
    )
    print(f"Talking points: {talk_log}")
    print("--- client talking points ---")
    print(briefing.headline)
    print()
    for paragraph in briefing.paragraphs:
        print(paragraph)
        print()
    for line in briefing.say:
        print(f"SAY: {line}")
    print(f"({word_count(briefing_text(briefing))} words)")
    if failed:
        print(f"{failed} feed(s) skipped. The page was still built.", file=sys.stderr)
    return 0


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit("Could not find feeds.yml next to build.py.")
    try:
        with path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise SystemExit(
            "feeds.yml could not be read. A line is probably indented wrong "
            f"or is missing a quote.\n{exc}"
        ) from exc
    if not isinstance(config, dict) or "site" not in config or "sections" not in config:
        raise SystemExit("feeds.yml needs a site: section and a sections: list.")
    if not isinstance(config["sections"], list):
        raise SystemExit("sections: in feeds.yml must be a list.")
    for section in config["sections"]:
        if not isinstance(section, dict) or not section.get("name"):
            raise SystemExit("Every section in feeds.yml needs a name: line.")
        role = str(section.get("role") or "").strip()
        if role not in {"financial", "political", "industry"}:
            raise SystemExit(
                f"Section '{section['name']}' needs role: financial, political, or industry."
            )
        for source in section.get("sources") or []:
            if not isinstance(source, dict) or not source.get("name"):
                raise SystemExit(f"A source under {section['name']} is missing its name.")
            if as_bool(source.get("enabled", True)) and not source.get("url"):
                raise SystemExit(
                    f"The source '{source['name']}' is turned on but has no url."
                )
    config.setdefault("block_keywords", [])
    config.setdefault("boost_keywords", [])
    config.setdefault("political_focus_keywords", [])
    config.setdefault("big_day_phrases", [])
    config.setdefault("keyword_lists", {})
    config.setdefault("markets", {})
    config.setdefault("talking_points", {})
    return config


def fetch_markets(config: dict, now: datetime) -> tuple[list[Quote], list[Quote], list[str]]:
    markets = config.get("markets") or {}
    specs: list[tuple[str, dict]] = []
    for group in ("previous_close", "futures", "extras"):
        for spec in markets.get(group) or []:
            if isinstance(spec, dict) and spec.get("symbol"):
                specs.append((group, spec))

    quotes: list[Quote | None] = [None] * len(specs)
    log: list[str] = []

    def job(index: int, group: str, spec: dict) -> tuple[int, Quote, str]:
        quote = fetch_yahoo_quote(group, spec, now)
        if quote.ok:
            line = (
                f"  OK   {quote.symbol}: {format_price(quote)} "
                f"{quote.session_label}"
            )
        else:
            line = f"  FAIL {quote.symbol}: {quote.error}"
        return index, quote, line

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(job, index, group, spec)
            for index, (group, spec) in enumerate(specs)
        ]
        for future in as_completed(futures):
            index, quote, line = future.result()
            quotes[index] = quote
            log.append(line)

    ordered = [quote for quote in quotes if quote is not None]
    log.sort()
    rates, rate_log = fetch_rates(markets.get("rates") or [], now)
    log.extend(rate_log)
    return ordered, rates, log


def fetch_yahoo_quote(group: str, spec: dict, now: datetime) -> Quote:
    symbol = str(spec.get("symbol") or "").strip()
    quote = Quote(
        label=str(spec.get("label") or symbol),
        symbol=symbol,
        group=group,
        hint=str(spec.get("hint") or ""),
        prefix=str(spec.get("prefix") or ""),
        suffix=str(spec.get("suffix") or ""),
        decimals=int(spec.get("decimals") or 2),
    )
    try:
        result = yahoo_chart(symbol)
        meta = result.get("meta") or {}
        bars = daily_bars(result)
        live = meta.get("regularMarketPrice")
        if spec.get("mode") == "yield" or symbol in {"^TNX", "^FVX", "^TYX", "^IRX"}:
            live, bars = scale_yield_index(live, bars)
        market_time = meta.get("regularMarketTime")
        stamped = None
        if market_time:
            stamped = datetime.fromtimestamp(int(market_time), EASTERN)

        if group == "previous_close":
            parsed = cash_close(bars, now)
            if parsed is None:
                raise RuntimeError("not enough daily closes")
            price, change, pct, as_of = parsed
            quote.session_label = "Previous close"
            quote.change_style = "points"
            quote.show_time = False
        else:
            parsed = live_vs_prior(bars, live, now)
            if parsed is None:
                raise RuntimeError("not enough daily closes")
            price, change, pct, as_of, in_progress = parsed
            if in_progress and stamped is not None:
                as_of = stamped
            mode = str(spec.get("mode") or "live")
            if mode == "yield":
                quote.change_style = "bp"
                if change is not None:
                    quote.bp = int(round(change * 100))
                quote.suffix = quote.suffix or "%"
                quote.comparison = "vs prior close"
            else:
                quote.change_style = "percent"
            if group == "futures":
                clock = futures_session_label(now)
                quote.session_label = clock if in_progress else "Last"
            else:
                quote.session_label = "Live" if in_progress else "Last"
            quote.show_time = in_progress

        quote.price = price
        quote.change = change
        quote.pct = pct
        quote.as_of = as_of
        quote.ok = price is not None
        if not quote.ok:
            quote.error = "no price"
    except Exception as exc:  # a dead quote must not stop the build
        quote.ok = False
        quote.error = str(exc).strip() or exc.__class__.__name__
        quote.session_label = "Previous close" if group == "previous_close" else "Live"
    return quote


def yahoo_chart(symbol: str) -> dict:
    encoded = quote(symbol, safe="")
    last_error: Exception | None = None
    for host in ("query1", "query2"):
        url = (
            f"https://{host}.finance.yahoo.com/v8/finance/chart/{encoded}"
            "?interval=1d&range=10d&includePrePost=true"
        )
        try:
            payload = http_get(url, YAHOO_TIMEOUT, UA_BROWSER)
            data = json.loads(payload.decode("utf-8", "replace"))
            chart = data.get("chart") or {}
            result = chart.get("result")
            if not result:
                err = chart.get("error") or {}
                raise RuntimeError(err.get("description") or "no data")
            return result[0]
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("Yahoo chart failed")


def daily_bars(result: dict) -> list[tuple[datetime, float]]:
    timestamps = result.get("timestamp") or []
    quote_block = (result.get("indicators") or {}).get("quote") or [{}]
    closes = quote_block[0].get("close") or []
    bars: list[tuple[datetime, float]] = []
    for stamp, close in zip(timestamps, closes):
        if stamp is None or close is None:
            continue
        bars.append((datetime.fromtimestamp(int(stamp), EASTERN), float(close)))
    return bars


def scale_yield_index(live, bars):
    """Old Yahoo yield indexes were quoted as yield times 10. Current ones are not."""
    sample = live if isinstance(live, (int, float)) else None
    if sample is None and bars:
        sample = bars[-1][1]
    if sample is not None and sample > 20:
        if isinstance(live, (int, float)):
            live = float(live) / 10.0
        bars = [(stamp, close / 10.0) for stamp, close in bars]
    return live, bars


def cash_close(bars, now: datetime):
    usable = [(stamp, close) for stamp, close in bars if close is not None]
    if len(usable) < 2:
        return None
    last_stamp, _last_close = usable[-1]
    session_over = (now.hour, now.minute) >= (16, 0)
    if last_stamp.date() == now.date() and not session_over and len(usable) >= 3:
        usable = usable[:-1]
    if len(usable) < 2:
        return None
    level_stamp, level = usable[-1]
    _base_stamp, base = usable[-2]
    if not base:
        return None
    change = level - base
    return level, change, change / base * 100.0, level_stamp


def live_vs_prior(bars, live, now: datetime):
    usable = [(stamp, close) for stamp, close in bars if close is not None]
    if not usable:
        return None
    last_stamp, last_close = usable[-1]
    age_hours = (now - last_stamp).total_seconds() / 3600.0
    in_progress = age_hours < 8 and len(usable) >= 2
    if len(usable) == 1:
        price = float(live) if isinstance(live, (int, float)) else float(last_close)
        return price, None, None, last_stamp, False
    if in_progress:
        _base_stamp, base = usable[-2]
        price = float(live) if isinstance(live, (int, float)) else float(last_close)
        as_of = last_stamp
    else:
        _base_stamp, base = usable[-2]
        price = float(last_close)
        as_of = last_stamp
    if not base:
        return price, None, None, as_of, in_progress
    change = price - base
    return price, change, change / base * 100.0, as_of, in_progress


def futures_session_label(now: datetime) -> str:
    minutes = now.hour * 60 + now.minute
    if now.weekday() == 6 and minutes >= 18 * 60:
        return "Premarket"
    if now.weekday() >= 5:
        return "Last"
    if minutes < 9 * 60 + 30:
        return "Premarket"
    if minutes < 16 * 60:
        return "Live"
    return "After hours"


def fetch_rates(specs: list, now: datetime) -> tuple[list[Quote], list[str]]:
    rates: list[Quote] = []
    log: list[str] = []
    fred_down = False
    treasury_rows: list[dict] | None = None
    mortgage_rows: list[tuple[datetime, float]] | None = None

    for spec in specs:
        if not isinstance(spec, dict) or not spec.get("series"):
            continue
        series = str(spec["series"]).strip()
        quote = Quote(
            label=str(spec.get("label") or series),
            symbol=series,
            group="rates",
            suffix="%",
            decimals=2,
            change_style="bp",
            comparison="vs prior week" if spec.get("change") == "week" else "vs prior close",
        )
        observations = None
        source_label = ""
        if not fred_down:
            try:
                observations = fred_observations(series)
                source_label = f"FRED {series}"
            except Exception as exc:
                fred_down = True
                log.append(f"  FAIL FRED {series}: {exc}. Using the official fallback.")
        if observations is None and series in {"DGS10", "DGS2"}:
            try:
                if treasury_rows is None:
                    treasury_rows = treasury_yield_rows(now)
                tenor = str(spec.get("tenor") or ("10 Yr" if series == "DGS10" else "2 Yr"))
                observations = treasury_series(treasury_rows, tenor)
                source_label = "U.S. Treasury"
            except Exception as exc:
                quote.error = str(exc).strip() or exc.__class__.__name__
                log.append(f"  FAIL {series}: {quote.error}")
                rates.append(quote)
                continue
        if observations is None and series == "MORTGAGE30US":
            try:
                if mortgage_rows is None:
                    mortgage_rows = freddie_mortgage_rows()
                observations = mortgage_rows
                source_label = "Freddie Mac weekly"
            except Exception as exc:
                quote.error = str(exc).strip() or exc.__class__.__name__
                log.append(f"  FAIL {series}: {quote.error}")
                rates.append(quote)
                continue
        if not observations or len(observations) < 1:
            quote.error = "no observations"
            log.append(f"  FAIL {series}: no observations")
            rates.append(quote)
            continue
        latest_dt, latest = observations[-1]
        quote.price = latest
        quote.as_of = latest_dt if latest_dt.tzinfo else latest_dt.replace(tzinfo=EASTERN)
        quote.source_label = source_label
        quote.session_label = "Week of" if spec.get("change") == "week" else "Close"
        quote.ok = True
        if len(observations) >= 2:
            _prior_dt, prior = observations[-2]
            quote.change = latest - prior
            quote.bp = int(round((latest - prior) * 100))
            if prior:
                quote.pct = (latest - prior) / prior * 100.0
        bp_text = "n/a" if quote.bp is None else f"{quote.bp:+d} bp"
        log.append(
            f"  OK   {series} via {source_label}: {latest:.2f}% {bp_text} "
            f"as of {latest_dt.date().isoformat()}"
        )
        rates.append(quote)
    return rates, log


def fred_observations(series: str) -> list[tuple[datetime, float]]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={quote(series, safe='')}"
    payload = http_get(url, FRED_TIMEOUT, UA_BROWSER, accept="text/csv,*/*")
    return parse_two_column_csv(payload.decode("utf-8", "replace"))


def parse_two_column_csv(text: str) -> list[tuple[datetime, float]]:
    rows: list[tuple[datetime, float]] = []
    reader = csv.reader(io.StringIO(text))
    for index, row in enumerate(reader):
        if index == 0 or len(row) < 2:
            continue
        raw_date, raw_value = row[0].strip(), row[1].strip()
        if not raw_value or raw_value == ".":
            continue
        try:
            stamp = datetime.strptime(raw_date, "%Y-%m-%d").replace(tzinfo=EASTERN)
            rows.append((stamp, float(raw_value)))
        except ValueError:
            continue
    if len(rows) < 1:
        raise RuntimeError("FRED file had no values")
    return rows


def treasury_yield_rows(now: datetime) -> list[dict]:
    months = [now.strftime("%Y%m")]
    previous = (now.replace(day=1) - timedelta(days=1)).strftime("%Y%m")
    if previous not in months:
        months.append(previous)
    collected: list[dict] = []
    last_error: Exception | None = None
    for ym in months:
        year = ym[:4]
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/"
            f"interest-rates/daily-treasury-rates.csv/all/{ym}"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
        )
        try:
            payload = http_get(url, 20, UA_BROWSER, accept="text/csv,*/*")
            text = payload.decode("utf-8", "replace")
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                if row.get("Date"):
                    collected.append(row)
        except Exception as exc:
            last_error = exc
    if not collected and last_error:
        raise last_error
    if not collected:
        raise RuntimeError("Treasury file had no rows")
    return collected


def treasury_series(rows: list[dict], tenor: str) -> list[tuple[datetime, float]]:
    points: list[tuple[datetime, float]] = []
    for row in rows:
        raw_date = (row.get("Date") or "").strip()
        raw_value = (row.get(tenor) or "").strip()
        if not raw_date or not raw_value or raw_value == ".":
            continue
        try:
            stamp = datetime.strptime(raw_date, "%m/%d/%Y").replace(tzinfo=EASTERN)
            points.append((stamp, float(raw_value)))
        except ValueError:
            continue
    points.sort(key=lambda item: item[0])
    if len(points) < 1:
        raise RuntimeError(f"no {tenor} values")
    return points


def freddie_mortgage_rows() -> list[tuple[datetime, float]]:
    url = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
    payload = http_get(url, 20, UA_BROWSER, accept="text/csv,*/*")
    text = payload.decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(text))
    points: list[tuple[datetime, float]] = []
    for row in reader:
        raw_date = (row.get("date") or "").strip()
        raw_value = (row.get("pmms30") or "").strip()
        if not raw_date or not raw_value:
            continue
        try:
            stamp = datetime.strptime(raw_date, "%m/%d/%Y").replace(tzinfo=EASTERN)
            points.append((stamp, float(raw_value)))
        except ValueError:
            continue
    if len(points) < 1:
        raise RuntimeError("Freddie Mac file had no 30-year rate")
    return points


def fetch_news(config: dict, now: datetime) -> tuple[list[FeedReport], list[Item]]:
    jobs = []
    for section in config["sections"]:
        if not as_bool(section.get("enabled", True)):
            continue
        for source in section.get("sources") or []:
            if not as_bool(source.get("enabled", True)):
                continue
            jobs.append((section, source))

    reports: list[FeedReport] = []
    items: list[Item] = []

    def job(section: dict, source: dict):
        return fetch_source(config, section, source, now)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(job, section, source) for section, source in jobs]
        for future in as_completed(futures):
            report, batch = future.result()
            reports.append(report)
            items.extend(batch)
    reports.sort(key=lambda report: report.name.lower())
    return reports, items


def fetch_source(config: dict, section: dict, source: dict, now: datetime):
    name = str(source["name"]).strip()
    url = str(source["url"]).strip()
    report = FeedReport(name=name, url=url)
    try:
        max_items = int(source.get("max_items", 5))
    except (TypeError, ValueError):
        report.error = "max_items must be a whole number"
        return report, []
    if max_items < 1:
        return report, []
    try:
        entries = download_entries(url)
    except Exception as exc:
        report.error = str(exc).strip() or exc.__class__.__name__
        return report, []

    window = section_window(config, section, now)
    keywords = resolve_keywords(config, section, source)
    require_match = source_requires_match(section, source, keywords)
    kept: list[Item] = []
    for entry in entries:
        try:
            item = entry_to_item(
                entry,
                source_name=name,
                role=str(section.get("role")),
                strip_suffix=as_bool(source.get("strip_publisher_suffix", False)),
                block_keywords=config.get("block_keywords") or [],
                boost_keywords=config.get("boost_keywords") or [],
                skip_url_parts=section.get("skip_url_parts") or [],
                skip_keywords=section.get("skip_keywords") or [],
                require_keywords=keywords,
                require_match=require_match,
                max_age=window,
                now=now,
            )
        except Exception:
            traceback.print_exc(file=sys.stderr)
            continue
        if item:
            kept.append(item)
    kept.sort(key=lambda item: item.published, reverse=True)
    kept = dedupe_links(kept)[:max_items]
    report.kept = len(kept)
    if not entries:
        report.error = "feed returned no stories"
    return report, kept


def section_window(config: dict, section: dict, now: datetime) -> timedelta:
    if section.get("max_age_hours") not in (None, ""):
        return timedelta(hours=float(section["max_age_hours"]))
    site = config["site"]
    hours = float(site.get("max_age_hours", 60))
    long_hours = float(site.get("long_window_hours", hours))
    monday_morning = now.weekday() == 0 and now.hour < 12
    if now.weekday() >= 5 or monday_morning:
        hours = max(hours, long_hours)
    return timedelta(hours=hours)


def resolve_keywords(config: dict, section: dict, source: dict) -> list:
    if "require_keywords" in source:
        return list(source.get("require_keywords") or [])
    ref = source.get("require_keyword_list")
    if ref:
        return list((config.get("keyword_lists") or {}).get(ref) or [])
    if section.get("require_keywords"):
        return list(section.get("require_keywords") or [])
    ref = section.get("require_keyword_list")
    if ref:
        return list((config.get("keyword_lists") or {}).get(ref) or [])
    return []


def source_requires_match(section: dict, source: dict, keywords: list) -> bool:
    if "require_match" in source:
        return as_bool(source.get("require_match"))
    return bool(keywords)


def download_entries(url: str) -> list:
    payload = http_get(
        url,
        FEED_TIMEOUT,
        UA_FEED,
        accept="application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    )
    parsed = feedparser.parse(payload)
    if parsed.entries:
        return list(parsed.entries)
    if getattr(parsed, "bozo", False):
        raise RuntimeError("feed was not valid RSS or Atom")
    return []


def entry_to_item(
    entry,
    source_name: str,
    role: str,
    strip_suffix: bool,
    block_keywords: list,
    boost_keywords: list,
    skip_url_parts: list,
    skip_keywords: list,
    require_keywords: list,
    require_match: bool,
    max_age: timedelta,
    now: datetime,
) -> Item | None:
    title = clean_text(entry.get("title") or "")
    link = clean_link(entry.get("link") or "")
    if not title or not link or not link.startswith(("http://", "https://")):
        return None
    published = entry_time(entry)
    if published is None or now - published > max_age or published - now > timedelta(hours=6):
        return None
    display_source = source_name
    if strip_suffix:
        title, publisher = split_publisher_suffix(title)
        if publisher:
            display_source = publisher
    if not title or is_junk_title(title):
        return None
    if any(keyword_in(title.lower(), keyword) for keyword in block_keywords):
        return None
    if section_skips(title, link, entry, skip_url_parts, skip_keywords):
        return None
    if not passes_required(title, link, require_keywords, require_match):
        return None
    boosts = [str(keyword) for keyword in boost_keywords if keyword_in(title.lower(), keyword)]
    return Item(
        title=title,
        link=link,
        source=display_source,
        role=role,
        published=published,
        boosts=boosts,
    )


def http_get(url: str, timeout: int, user_agent: str, accept: str = "*/*") -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": accept},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"could not connect ({reason})") from exc
    except TimeoutError as exc:
        raise RuntimeError("timed out") from exc


def entry_time(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def split_publisher_suffix(title: str) -> tuple[str, str | None]:
    if " - " not in title:
        return title, None
    headline, publisher = title.rsplit(" - ", 1)
    headline = headline.strip()
    publisher = publisher.strip()
    if headline and 2 <= len(publisher) <= 48:
        return headline, publisher
    return title, None


def keyword_in(text: str, keyword) -> bool:
    phrase = str(keyword).strip().lower()
    if not phrase:
        return False
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


def clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    for _ in range(3):
        updated = html.unescape(text)
        if updated == text:
            break
        text = updated
    return re.sub(r"\s+", " ", text).strip()


TRACKING_PARAMS = {"oc", "at_medium", "at_campaign", "ns_mchannel", "ns_source", "ns_campaign", "ns_linkname"}


def clean_link(url: str) -> str:
    link = url.strip()
    link = link.replace("https://news.google.com/rss/articles/", "https://news.google.com/articles/")
    link = link.replace("http://news.google.com/rss/articles/", "https://news.google.com/articles/")
    parts = urlsplit(link)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def canon_link(url: str) -> str:
    return clean_link(url).rstrip("/").lower()


def dedupe_links(items: list[Item]) -> list[Item]:
    seen: set[str] = set()
    unique: list[Item] = []
    for item in items:
        key = canon_link(item.link)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def is_junk_title(title: str) -> bool:
    text = title.lower().strip()
    words = re.findall(r"[A-Za-z][A-Za-z0-9']*", title)
    if len(words) < 4:
        return True
    if text in {"news", "annuity news & insights", "tax facts has the answers"}:
        return True
    if re.search(r"\barchives?\b", text):
        return True
    if text.startswith("tag:") or text.lstrip().startswith("-"):
        return True
    if re.search(r"\.(com|org|net)\b", text):
        return True
    if "latest news" in text or "latest and breaking" in text:
        return True
    return False


def section_skips(title: str, link: str, entry, skip_url_parts: list, skip_keywords: list) -> bool:
    categories = []
    for tag in entry.get("tags") or []:
        if isinstance(tag, dict):
            term = tag.get("term") or tag.get("label") or ""
        else:
            term = str(tag)
        if term:
            categories.append(str(term).lower())
    link_text = link.lower()
    for part in skip_url_parts:
        piece = str(part).strip().lower()
        if piece and piece in link_text:
            return True
        token = piece.strip("/")
        if token and any(token in category for category in categories):
            return True
    headline = title.lower()
    return any(keyword_in(headline, keyword) for keyword in skip_keywords)


def passes_required(title: str, link: str, keywords: list, require_match: bool) -> bool:
    if not require_match or not keywords:
        return True
    path = urlsplit(link).path.lower().replace("-", " ")
    text = f"{title.lower()} {path}"
    return any(keyword_in(text, keyword) for keyword in keywords)


def normalize_title(title: str) -> str:
    text = title.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    words = []
    for word in text.split():
        if word in STOPWORDS or len(word) <= 2:
            continue
        words.append(word)
    return " ".join(words)


def titles_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    left_words = set(left.split())
    right_words = set(right.split())
    if not left_words or not right_words:
        return False
    overlap = left_words & right_words
    if len(overlap) >= 4:
        return True
    union = left_words | right_words
    if len(overlap) >= 3 and len(overlap) / len(union) >= 0.55:
        return True
    shorter, longer = (
        (left_words, right_words) if len(left_words) <= len(right_words) else (right_words, left_words)
    )
    if len(shorter) >= 5 and len(shorter & longer) / len(shorter) >= 0.8:
        return True
    if len(overlap) >= 3 and SequenceMatcher(None, left, right).ratio() >= 0.9:
        return True
    return False


def cluster_items(items: list[Item]) -> list[list[Item]]:
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    signatures = [normalize_title(item.title) for item in items]
    links = [canon_link(item.link) for item in items]
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if links[left] == links[right] or titles_match(signatures[left], signatures[right]):
                union(left, right)

    groups: dict[int, list[Item]] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)
    clusters: list[list[Item]] = []
    for group in groups.values():
        cluster_id = len(clusters)
        for item in group:
            item.cluster_id = cluster_id
        clusters.append(group)
    return clusters


OUTLET_FAMILIES = (
    ("cnbc", "CNBC"),
    ("bloomberg", "Bloomberg"),
    ("the hill", "The Hill"),
    ("fox business", "Fox Business"),
    ("fox news", "Fox News"),
    ("yahoo", "Yahoo Finance"),
    ("wall street journal", "WSJ"),
    ("wsj", "WSJ"),
    ("reuters", "Reuters"),
    ("associated press", "AP"),
    ("marketwatch", "MarketWatch"),
    ("axios", "Axios"),
    ("politico", "Politico"),
)


def outlet_label(name: str) -> str:
    low = name.lower()
    for needle, label in OUTLET_FAMILIES:
        if needle in low:
            return label
    return name.strip()


def outlet_names(cluster: list[Item]) -> list[str]:
    """One name per newsroom. CNBC and CNBC Politics count as one outlet."""
    seen: list[str] = []
    lowered: set[str] = set()
    for item in sorted(cluster, key=lambda row: row.published, reverse=True):
        label = outlet_label(item.source)
        key = label.lower()
        if label and key not in lowered:
            seen.append(label)
            lowered.add(key)
    return seen


def freshness(cluster: list[Item], now: datetime) -> float:
    newest = max(item.published for item in cluster)
    age = max(0.0, (now - newest).total_seconds() / 3600.0)
    return max(0.0, 1.0 - age / 96.0)


def focus_hits(cluster: list[Item], keywords: list) -> int:
    text = " ".join(item.title.lower() for item in cluster)
    return sum(1 for keyword in keywords if keyword_in(text, keyword))


def choose_representative(cluster: list[Item]) -> Item:
    def rank(item: Item) -> tuple:
        direct = 0 if "news.google.com" in item.link else 1
        words = len(item.title.split())
        readable = 1 if 6 <= words <= 28 else 0
        return (direct, readable, len(item.boosts), item.published.timestamp())

    return max(cluster, key=rank)


def headline_is_major(title: str, phrases: list) -> bool:
    """A Fed decision, jobs report, or CPI release, not a passing mention of rates."""
    low = title.lower()
    fed = (
        keyword_in(low, "fed")
        or keyword_in(low, "fomc")
        or "federal reserve" in low
    )
    fed_move = any(
        keyword_in(low, phrase)
        for phrase in (
            "raises rates", "raised rates", "cuts rates", "cut rates",
            "rate hike", "rate cut", "rate decision",
        )
    )
    if fed and fed_move:
        return True
    loose = {
        "rate hike", "rate cut", "raises rates", "raised rates",
        "cuts rates", "cut rates",
    }
    return any(
        keyword_in(low, phrase)
        for phrase in phrases
        if str(phrase).strip().lower() not in loose
    )


def market_relevant(cluster: list[Item], keywords: list, phrases: list | None = None) -> bool:
    """Business and markets copy. A geopolitics brief needs market words or a major event."""
    if phrases and any(headline_is_major(item.title, phrases) for item in cluster):
        return True
    if not keywords:
        return True
    text = " ".join(item.title.lower() for item in cluster)
    return any(keyword_in(text, keyword) for keyword in keywords)


def arrange(items: list[Item], clusters: list[list[Item]], config: dict, now: datetime) -> dict:
    site = config["site"]
    top_count = int(site.get("top_count", 3))
    more_count = int(site.get("more_count", 12))
    industry_count = int(site.get("industry_count", 8))
    focus = config.get("political_focus_keywords") or []
    phrases = config.get("big_day_phrases") or []
    market_terms = list((config.get("keyword_lists") or {}).get("market_terms") or [])
    try:
        headline_hours = float(site.get("big_day_headline_hours", 20))
    except (TypeError, ValueError):
        headline_hours = 20

    def has_role(cluster: list[Item], role: str) -> bool:
        return any(item.role == role for item in cluster)

    def financial_score(cluster: list[Item]) -> float:
        boosts = {word.lower() for item in cluster for word in item.boosts}
        event = 0.0
        newest = max(item.published for item in cluster)
        age = (now - newest).total_seconds() / 3600.0
        if age <= headline_hours and any(headline_is_major(item.title, phrases) for item in cluster):
            # A fresh Fed decision or jobs/CPI print should not sink under a
            # one-outlet feature just because only one feed worded it that way.
            event = 14.0
        return len(outlet_names(cluster)) * 10 + freshness(cluster, now) * 5 + len(boosts) + event

    def political_score(cluster: list[Item]) -> float:
        hits = min(focus_hits(cluster, focus), 2)
        outlets = len(outlet_names(cluster))
        # A story every desk is carrying still belongs in the political top 3.
        coverage = 8 if outlets >= 3 else 0
        market_bonus = 4 if market_relevant(cluster, market_terms, phrases) else 0
        return hits * 6 + market_bonus + outlets * 5 + freshness(cluster, now) * 4 + coverage

    financial = sorted(
        [
            cluster for cluster in clusters
            if has_role(cluster, "financial") and market_relevant(cluster, market_terms, phrases)
        ],
        key=financial_score,
        reverse=True,
    )
    top_financial = financial[:top_count]
    used = {item.cluster_id for cluster in top_financial for item in cluster[:1]}

    political_pool = [
        cluster for cluster in clusters
        if has_role(cluster, "political") and cluster[0].cluster_id not in used
    ]
    focused = [cluster for cluster in political_pool if focus_hits(cluster, focus) > 0]
    # Keep a Washington story that every desk is carrying, even if the
    # headline does not use a policy keyword.
    for cluster in political_pool:
        if len(outlet_names(cluster)) >= 3 and cluster not in focused:
            focused.append(cluster)
    if len(focused) >= top_count:
        political_ranked = sorted(focused, key=political_score, reverse=True)
    else:
        rest = [cluster for cluster in political_pool if cluster not in focused]
        political_ranked = (
            sorted(focused, key=political_score, reverse=True)
            + sorted(rest, key=political_score, reverse=True)
        )
    top_political = political_ranked[:top_count]
    used.update(cluster[0].cluster_id for cluster in top_political)

    more = [cluster for cluster in financial if cluster[0].cluster_id not in used][:more_count]
    used.update(cluster[0].cluster_id for cluster in more)

    industry_pool = [
        cluster for cluster in clusters
        if has_role(cluster, "industry") and cluster[0].cluster_id not in used
    ]
    industry = sorted(
        industry_pool,
        key=lambda cluster: (freshness(cluster, now), len(outlet_names(cluster))),
        reverse=True,
    )[:industry_count]

    return {
        "financial": top_financial,
        "political": top_political,
        "more": more,
        "industry": industry,
    }


def build_talking_points(config, now, quotes, rates, picked, items) -> tuple[Briefing, str]:
    facts = fact_sheet(config, now, quotes, rates, picked, items)
    library = load_story_library(config)
    try:
        briefing = call_provider(config, facts, library)
        if briefing is None:
            briefing = library_briefing(facts, library)
            return briefing, briefing.origin
        return briefing, briefing.origin
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        briefing = library_briefing(facts, library)
        briefing.origin = f"{briefing.origin}; provider error: {exc}"
        return briefing, briefing.origin


def fact_sheet(config, now, quotes, rates, picked, items) -> dict:
    site = config["site"]
    threshold = float(site.get("big_move_percent", 1.5))
    headline_hours = float(site.get("big_day_headline_hours", 20))
    symbols = set((config.get("markets") or {}).get("big_move_symbols") or [])
    phrases = config.get("big_day_phrases") or []

    big_moves = []
    for quote in list(quotes):
        if quote.symbol in symbols and quote.pct is not None and abs(quote.pct) >= threshold:
            big_moves.append(quote)

    matched = []
    seen_titles: set[str] = set()
    for item in items:
        if item.role not in {"financial", "political"}:
            continue
        age = (now - item.published).total_seconds() / 3600.0
        if age > headline_hours:
            continue
        if not headline_is_major(item.title, phrases):
            continue
        key = item.title.lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        matched.append(item.title)

    big_day = bool(big_moves or matched)
    if big_day:
        timeframe = "today"
        why = []
        if big_moves:
            why.append(
                "a move of at least "
                f"{threshold:g} percent in an index or index future"
            )
        if matched:
            why.append("a fresh headline about a major event")
        reason = "Big day because of " + " and ".join(why) + "."
    else:
        timeframe = "this week and the long-term plan"
        reason = (
            "Not a big day. No index or index-future move of "
            f"{threshold:g} percent, and no fresh major-event headline. "
            "Talk about this week and the long-term plan, not about today."
        )

    lines = [f"TIMEFRAME: {timeframe}", f"NOTE: {reason}", "", "MARKET FIGURES:"]
    for quote in quotes:
        lines.append(describe_quote_fact(quote))
    lines.append("")
    lines.append("RATES:")
    for quote in rates:
        lines.append(describe_rate_fact(quote))
    lines.append("")
    lines.append("TOP FINANCIAL HEADLINES:")
    for cluster in picked["financial"]:
        item = choose_representative(cluster)
        lines.append(f"- {item.title} ({item.source})")
    lines.append("")
    lines.append("TOP POLITICAL HEADLINES:")
    for cluster in picked["political"]:
        item = choose_representative(cluster)
        lines.append(f"- {item.title} ({item.source})")
    if not picked["financial"] and not picked["political"]:
        lines.append("No headlines were available.")
    return {
        "now": now,
        "big_day": big_day,
        "timeframe": timeframe,
        "context": "\n".join(lines),
        "quotes": quotes,
        "rates": rates,
        "matched": matched,
        "big_moves": big_moves,
        "financial": picked["financial"],
        "political": picked["political"],
    }


def describe_quote_fact(quote: Quote) -> str:
    if not quote.ok or quote.price is None:
        return f"- {quote.label} ({quote.symbol}): unavailable"
    level = format_price(quote)
    when = format_when(quote.as_of, quote.show_time) if quote.as_of else "time unknown"
    if quote.change_style == "bp" and quote.bp is not None:
        move = f"{quote.bp:+d} basis points {quote.comparison}".strip()
    elif quote.change_style == "points" and quote.change is not None and quote.pct is not None:
        move = f"{quote.change:+.2f} points, {quote.pct:+.2f} percent"
    elif quote.pct is not None:
        move = f"{quote.pct:+.2f} percent"
    else:
        move = "change unavailable"
    return (
        f"- {quote.label} ({quote.symbol}): {level}, {move}, "
        f"label {quote.session_label}, as of {when}"
    )


def describe_rate_fact(quote: Quote) -> str:
    if not quote.ok or quote.price is None:
        return f"- {quote.label} ({quote.symbol}): unavailable"
    when = format_when(quote.as_of, False) if quote.as_of else "time unknown"
    if quote.bp is None:
        move = "change unavailable"
    else:
        move = f"{quote.bp:+d} basis points {quote.comparison}".strip()
    source = quote.source_label or "official series"
    return f"- {quote.label} ({quote.symbol}): {quote.price:.2f} percent, {move}, {source}, as of {when}"


def load_story_library(config: dict) -> dict:
    talking = config.get("talking_points") or {}
    path = ROOT / str(talking.get("stories_file") or "prompts/stories.yml")
    if not path.exists():
        print(f"  story library missing: {path.name}")
        return {"stories": [], "closers": []}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    stories = [
        item for item in (data.get("stories") or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    closers = [
        item for item in (data.get("closers") or [])
        if isinstance(item, dict) and str(item.get("line") or "").strip()
    ]
    return {"stories": stories, "closers": closers}


def call_provider(config: dict, facts: dict, library: dict) -> Briefing | None:
    """Use a small model when a repository secret is set. Otherwise skip."""
    talking = config.get("talking_points") or {}
    prompt_path = ROOT / str(talking.get("prompt_file") or "prompts/talking_points.md")
    providers = (
        (
            "openai",
            "OPENAI_API_KEY",
            "https://api.openai.com/v1/chat/completions",
            str(talking.get("openai_model") or "gpt-4.1-mini"),
        ),
        (
            "anthropic",
            "ANTHROPIC_API_KEY",
            "https://api.anthropic.com/v1/messages",
            str(talking.get("anthropic_model") or "claude-haiku-4-5"),
        ),
        (
            "xai",
            "XAI_API_KEY",
            "https://api.x.ai/v1/chat/completions",
            str(talking.get("xai_model") or "grok-3-mini"),
        ),
    )
    chosen = None
    for name, env_name, endpoint, model in providers:
        key = (os.environ.get(env_name) or "").strip()
        if key:
            chosen = (name, key, endpoint, model)
            break
    if chosen is None:
        print("  no provider key set; using the story library")
        return None
    if not prompt_path.exists():
        print(f"  provider skipped: missing {prompt_path.name}")
        return None
    name, key, endpoint, model = chosen
    system = prompt_path.read_text(encoding="utf-8")
    user = provider_user_message(facts, library)
    print(f"  asking {name} ({model})")
    content = post_provider(name, key, endpoint, model, system, user)
    if not content:
        return None
    briefing = parse_briefing(content, facts["context"])
    if briefing is None:
        print("  provider text rejected; using the story library")
        return None
    briefing.origin = f"{name}:{model}"
    return briefing


def provider_user_message(facts: dict, library: dict) -> str:
    primary, _tags = market_conditions(facts)
    today = pick_rotating(library.get("stories") or [], primary, facts["now"])
    skip = str((today or {}).get("id") or "")
    samples = style_samples(library.get("stories") or [], facts["now"], skip_id=skip)
    blocks = []
    for index, story in enumerate(samples, start=1):
        blocks.append(
            f"SAMPLE {index}\n"
            f"principle: {story.get('principle', '')}\n"
            f"headline: {story.get('headline', '')}\n"
            f"passage: {story.get('text', '')} {story.get('tie', '')}\n"
            f"say: {story.get('say', '')}"
        )
    sample_text = "\n\n".join(blocks) if blocks else "No samples were available."
    plain = plain_english_facts(facts)
    return (
        "Write the client talking points from these facts only. "
        "Do not invent any number or event. Do not copy a sample.\n\n"
        "STYLE SAMPLES (voice only):\n"
        f"{sample_text}\n\n"
        "PLAIN ENGLISH (use these size words, not the figures):\n"
        f"{plain}\n\n"
        "FACTS:\n"
        f"{facts['context']}"
    )


def style_samples(stories: list[dict], when: datetime, skip_id: str, count: int = 3) -> list[dict]:
    pool = [story for story in stories if str(story.get("id") or "") != skip_id]
    pool.sort(key=lambda story: str(story.get("id") or ""))
    if not pool:
        return []
    start = when.astimezone(EASTERN).date().toordinal()
    picked: list[dict] = []
    seen: set[str] = set()
    for step in range(count * 3):
        story = pool[(start + step * 5) % len(pool)]
        ident = str(story.get("id") or "")
        if ident in seen:
            continue
        seen.add(ident)
        picked.append(story)
        if len(picked) == count:
            break
    return picked


def post_provider(name: str, key: str, endpoint: str, model: str, system: str, user: str) -> str | None:
    if name == "anthropic":
        body = {
            "model": model,
            "max_tokens": 700,
            "temperature": 0.4,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    else:
        body = {
            "model": model,
            "temperature": 0.4,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = response.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", "replace")
        print(f"  {name} HTTP {exc.code}: {detail[:300]}")
        return None
    except Exception as exc:
        print(f"  {name} failed: {exc}")
        return None
    try:
        data = json.loads(payload.decode("utf-8"))
        if name == "anthropic":
            return str(data["content"][0]["text"])
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        print(f"  {name} response could not be read: {exc}")
        return None


JARGON = re.compile(
    r"\b(basis points?|bps|S&P|Nasdaq|Russell|futures|VIX|Dow|yields?|tickers?|premarket|indexes?|indices)\b",
    re.I,
)


def parse_briefing(content: str, context: str) -> Briefing | None:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    headline = clean_text(str(data.get("headline") or ""))
    passage = str(data.get("passage") or "").strip()
    paragraphs = [
        clean_text(part)
        for part in re.split(r"\n\s*\n", passage)
        if clean_text(part)
    ]
    if len(paragraphs) == 1 and paragraphs[0]:
        sentences = [
            clean_text(part)
            for part in re.split(r"(?<=[.!?])\s+", paragraphs[0])
            if clean_text(part)
        ]
        if len(sentences) > 3:
            paragraphs = [
                " ".join(sentences[:2]),
                " ".join(sentences[2:-1]) if len(sentences) > 3 else sentences[2],
                sentences[-1] if len(sentences) > 3 else "",
            ]
            paragraphs = [part for part in paragraphs if part]
    say_raw = data.get("say") or []
    if isinstance(say_raw, str):
        say_raw = [say_raw]
    say = [clean_text(str(part)) for part in say_raw if clean_text(str(part))][:2]
    if not headline or not paragraphs or not say:
        print("  provider text was missing a headline, a passage, or a say line")
        return None
    blob = " ".join([headline, *paragraphs, *say])
    if any(re.search(pattern, blob, flags=re.I) for pattern in BANNED_COPY):
        print("  provider text included a recommendation or a promise")
        return None
    if JARGON.search(blob):
        print("  provider text used market jargon")
        return None
    if re.search(r"\d", blob):
        print("  provider text included a figure")
        return None
    if not numbers_are_grounded(blob, context):
        print("  provider text included a number that was not in the facts")
        return None
    words = word_count(blob)
    if words < 120 or words > 200:
        print(f"  provider text was {words} words")
        return None
    return Briefing(headline, paragraphs, say, "provider")


def numbers_are_grounded(text: str, context: str) -> bool:
    ctx = context.replace(",", "")
    cleaned = text.replace(",", "")
    allowed = {str(number) for number in range(0, 11)}
    for raw in re.findall(r"\d+(?:\.\d+)?", cleaned):
        if raw in allowed or raw in {"401", "529"}:
            continue
        if re.fullmatch(r"20[12]\d", raw):
            continue
        if raw in ctx:
            continue
        try:
            value = float(raw)
        except ValueError:
            return False
        found = False
        for piece in re.findall(r"\d+(?:\.\d+)?", ctx):
            try:
                other = float(piece)
            except ValueError:
                continue
            if abs(value - other) <= 0.05:
                found = True
                break
        if not found:
            return False
    return True


def library_briefing(facts: dict, library: dict) -> Briefing:
    """One flowing client piece from the story library and today's figures."""
    primary, tags = market_conditions(facts)
    when = facts["now"]
    story = pick_rotating(library.get("stories") or [], primary, when)
    closer = pick_closer(library.get("closers") or [], primary, when, story)
    paragraphs = opener_paragraphs(facts, tags)
    opener_count = len(paragraphs)
    say: list[str] = []
    if story:
        headline = clean_text(str(story.get("headline") or "Your plan still leads"))
        paragraphs.append(clean_text(str(story.get("text") or "")))
        tie = clean_text(str(story.get("tie") or ""))
        if tie:
            paragraphs.append(tie)
        line = clean_text(str(story.get("say") or ""))
        if line:
            say.append(line)
        origin = f"library:{story.get('id')}:{primary}"
    else:
        headline = "Your plan still leads"
        paragraphs.append(
            "Picture a client who already has a map for the year, and then checks a noisy headline to see if the map still counts. It does. The headline is weather. The plan is the route."
        )
        paragraphs.append("We stay with the route you already chose.")
        say.append("We are staying with your plan. A loud week does not get to rewrite it.")
        origin = f"library:backup:{primary}"
    if closer:
        say.append(closer)
    paragraphs = [part for part in paragraphs if part]
    briefing = Briefing(headline, paragraphs, say[:2], origin)
    return fit_library_length(briefing, opener_count)


def fit_library_length(briefing: Briefing, opener_count: int) -> Briefing:
    pad = (
        "You do not have to follow the market to stay on track. "
        "The plan is there so one week does not turn into a decision."
    )
    if word_count(briefing_text(briefing)) < 130:
        briefing.paragraphs.append(pad)
    while word_count(briefing_text(briefing)) > 190 and len(briefing.say) > 1:
        briefing.say.pop()
    while word_count(briefing_text(briefing)) > 190 and opener_count > 1 and len(briefing.paragraphs) > 1:
        briefing.paragraphs.pop(1)
        opener_count -= 1
    if (
        word_count(briefing_text(briefing)) > 190
        and briefing.paragraphs
        and briefing.paragraphs[-1] == pad
    ):
        briefing.paragraphs.pop()
    return briefing


def briefing_text(briefing: Briefing) -> str:
    return " ".join([briefing.headline, *briefing.paragraphs, *briefing.say])


def market_conditions(facts: dict) -> tuple[str, set[str]]:
    tags: set[str] = set()
    spx = quote_by(facts["quotes"], "^GSPC")
    pct = spx.pct if spx is not None else None
    if pct is None or abs(pct) < 0.20:
        tags.add("flat")
        stock = "flat"
    elif pct > 0:
        tags.add("stocks_up")
        stock = "stocks_up"
    else:
        tags.add("stocks_down")
        stock = "stocks_down"
    if facts.get("big_day"):
        tags.add("big_news")
    vix = quote_by(facts["quotes"], "^VIX")
    if vix is not None and vix.price is not None and vix.price >= 20:
        tags.add("volatility")
    elif pct is not None and abs(pct) >= 1.5:
        tags.add("volatility")
    ten = quote_by(facts["rates"], "DGS10")
    if ten is not None and ten.bp is not None:
        if ten.bp >= 3:
            tags.add("rates_up")
        elif ten.bp <= -3:
            tags.add("rates_down")
    mortgage = quote_by(facts["rates"], "MORTGAGE30US")
    if mortgage is not None and mortgage.bp is not None:
        if mortgage.bp >= 5:
            tags.add("rates_up")
        elif mortgage.bp <= -5:
            tags.add("rates_down")
    for name in ("big_news", "volatility", "stocks_down", "stocks_up", "flat", "rates_up", "rates_down"):
        if name in tags:
            return name, tags
    return stock, tags


def quote_by(quotes: list[Quote], symbol: str) -> Quote | None:
    for quote in quotes:
        if quote.symbol == symbol and quote.ok:
            return quote
    return None


def pick_rotating(items: list[dict], primary: str, when: datetime, salt: int = 0) -> dict | None:
    eligible = [item for item in items if primary in (item.get("conditions") or [])]
    if len(eligible) < 8:
        widened = [
            item for item in items
            if primary in (item.get("conditions") or []) or "flat" in (item.get("conditions") or [])
        ]
        if len(widened) > len(eligible):
            eligible = widened
    if not eligible:
        eligible = list(items)
    eligible.sort(key=lambda item: str(item.get("id") or ""))
    if not eligible:
        return None
    ordinal = when.astimezone(EASTERN).date().toordinal() + salt
    return eligible[ordinal % len(eligible)]


def pick_closer(closers: list[dict], primary: str, when: datetime, story: dict | None) -> str:
    say = clean_text(str((story or {}).get("say") or ""))
    ordinal = when.astimezone(EASTERN).date().toordinal()
    eligible = [item for item in closers if primary in (item.get("conditions") or [])]
    if not eligible:
        eligible = list(closers)
    eligible.sort(key=lambda item: str(item.get("id") or ""))
    if not eligible:
        return ""
    for step in range(len(eligible)):
        item = eligible[(ordinal + 7 + step) % len(eligible)]
        line = clean_text(str(item.get("line") or ""))
        if not line or line == say:
            continue
        if say and SequenceMatcher(None, say.lower(), line.lower()).ratio() > 0.72:
            continue
        return line
    return ""


def opener_paragraphs(facts: dict, tags: set[str]) -> list[str]:
    spx = quote_by(facts["quotes"], "^GSPC")
    es = quote_by(facts["quotes"], "ES=F")
    ten = quote_by(facts["rates"], "DGS10")
    mortgage = quote_by(facts["rates"], "MORTGAGE30US")
    event = plain_event(facts.get("matched") or [])
    stock = stock_phrase(spx.pct if spx is not None else None)
    stock_in_session = session_phrase(stock)
    sentences: list[str] = []
    big = bool(facts.get("big_day"))

    if event == "fed_up":
        sentences.append(
            "You may hear that the Fed, which sets short-term interest rates, "
            f"raised them, and {stock_in_session}."
        )
    elif event == "fed_down":
        sentences.append(
            "You may hear that the Fed, which sets short-term interest rates, "
            f"cut them, and {stock_in_session}."
        )
    elif event == "jobs":
        sentences.append(
            "A jobs report is in the news. That is simply the monthly count of people working, "
            f"and {stock_in_session}."
        )
    elif event == "inflation":
        sentences.append(
            "An inflation report is in the news, which is a read on how fast everyday prices are rising, "
            f"and {stock_in_session}."
        )
    elif big and event == "other":
        sentences.append(
            f"You may hear a loud headline, and {stock_in_session}. "
            "A loud day is still just one day inside a long-term plan."
        )
    elif big:
        sentences.append(
            f"It was a louder session than usual, and {stock}. "
            "A swing like that can feel big, and it still fits inside a long-term plan."
        )
    else:
        rate = ordinary_rate_clause(ten, mortgage)
        lead = stock[0].upper() + stock[1:]
        if rate:
            sentences.append(f"{session_sentence(lead)}, and {rate}.")
        else:
            sentences.append(f"{session_sentence(lead)}.")

    early = early_sentence(es, big)
    if early and (big or "volatility" in tags):
        sentences.append(early)
    elif early and not big:
        # Keep the opener to one or two sentences. A quiet week can mention
        # the early read only when rates were not already the second clause.
        if len(sentences) == 1 and " and " not in sentences[0]:
            sentences.append(early)
    return sentences


def plain_event(titles: list[str]) -> str | None:
    for title in titles:
        low = f" {title.lower()} "
        fed = "fed" in low or "federal reserve" in low or "fomc" in low
        if fed and any(piece in low for piece in (" raise", " raises", " raised", " hike", " hikes")):
            return "fed_up"
        if fed and any(piece in low for piece in (" cut", " cuts", " lower")):
            return "fed_down"
        if any(piece in low for piece in ("jobs report", "payroll", "nonfarm", "jobs numbers")):
            return "jobs"
        if any(piece in low for piece in ("cpi", "consumer price", "inflation report", "inflation data")):
            return "inflation"
    if titles:
        return "other"
    return None


def stock_phrase(pct: float | None) -> str:
    if pct is None or abs(pct) < 0.20:
        return "stocks were quiet"
    up = pct > 0
    size = abs(pct)
    if size < 0.45:
        return "stocks rose a little" if up else "stocks slipped a little"
    if size < 0.90:
        return "stocks rose a bit" if up else "stocks slipped"
    if size < 1.50:
        return "stocks had a strong session" if up else "stocks had a soft session"
    return "stocks had a big up day" if up else "stocks had a rough day"


def session_phrase(phrase: str) -> str:
    if "session" in phrase or "day" in phrase:
        return phrase
    return phrase + " in the last session"


def session_sentence(lead: str) -> str:
    if "session" in lead or "day" in lead:
        return lead
    return lead + " in the last session"


def ordinary_rate_clause(ten: Quote | None, mortgage: Quote | None) -> str | None:
    if ten is not None and ten.bp is not None and abs(ten.bp) >= 3:
        if ten.bp > 0:
            return "interest rates ticked higher" if ten.bp < 10 else "interest rates moved up"
        return "interest rates eased a little" if ten.bp > -10 else "interest rates moved lower"
    if mortgage is not None and mortgage.bp is not None and abs(mortgage.bp) >= 5:
        if mortgage.bp > 0:
            return "the rate on a 30-year mortgage is a little higher than last week"
        return "the rate on a 30-year mortgage is a little lower than last week"
    if ten is not None and ten.bp is not None:
        return "interest rates barely moved"
    return None


def early_sentence(quote: Quote | None, big: bool) -> str | None:
    if quote is None or quote.pct is None or abs(quote.pct) < 0.15:
        return None
    size = abs(quote.pct)
    if quote.pct > 0:
        tone = "firmer" if size >= 0.75 else "a little firmer"
    else:
        tone = "softer" if size >= 0.75 else "a little softer"
    label = (quote.session_label or "").lower()
    if "premarket" in label or label == "live":
        lead = "The early read before the next open is"
    else:
        lead = "The latest read before the next open is"
    if big:
        return f"{lead} {tone}, which is normal noise around a loud headline."
    return f"{lead} {tone}."


def plain_english_facts(facts: dict) -> str:
    spx = quote_by(facts["quotes"], "^GSPC")
    es = quote_by(facts["quotes"], "ES=F")
    ten = quote_by(facts["rates"], "DGS10")
    mortgage = quote_by(facts["rates"], "MORTGAGE30US")
    lines = [
        f"- Time frame: {facts.get('timeframe')}",
        f"- Stocks: {stock_phrase(spx.pct if spx is not None else None)}",
    ]
    early = early_sentence(es, bool(facts.get("big_day")))
    if early:
        lines.append(f"- Next session: {early}")
    rate = ordinary_rate_clause(ten, mortgage)
    if rate:
        lines.append(f"- Rates: {rate}")
    event = plain_event(facts.get("matched") or [])
    if event == "fed_up":
        lines.append("- News: the Fed, which sets short-term interest rates, raised them")
    elif event == "fed_down":
        lines.append("- News: the Fed, which sets short-term interest rates, cut them")
    elif event == "jobs":
        lines.append("- News: a jobs report, the monthly count of people working")
    elif event == "inflation":
        lines.append("- News: an inflation report, a read on how fast everyday prices are rising")
    elif event == "other":
        lines.append("- News: a loud headline is in the facts below. Describe it without jargon or figures.")
    else:
        lines.append("- News: nothing in the facts is large enough to make this about today")
    return "\n".join(lines)



def direction_of(quote: Quote) -> str:
    value = quote.pct if quote.pct is not None else quote.change
    if value is None or abs(value) < 0.005:
        return "flat"
    return "up" if value > 0 else "down"


def weekday_name(stamp: datetime | None) -> str:
    if stamp is None:
        return ""
    return stamp.astimezone(EASTERN).strftime("%A")


def human_join(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:[.’'][A-Za-z0-9]+)?", text))


def format_price(quote: Quote) -> str:
    if quote.price is None:
        return "—"
    if quote.decimals <= 0:
        body = f"{quote.price:,.0f}"
    else:
        body = f"{quote.price:,.{quote.decimals}f}"
    return f"{quote.prefix}{body}{quote.suffix}"


def format_abs_number(value: float, decimals: int) -> str:
    number = abs(value)
    if decimals <= 0:
        return f"{number:,.0f}"
    if number >= 1000:
        return f"{number:,.{decimals}f}"
    return f"{number:.{decimals}f}"


def format_when(stamp: datetime | None, show_time: bool) -> str:
    if stamp is None:
        return ""
    local = stamp.astimezone(EASTERN)
    day = f"{local.strftime('%a')}, {local.strftime('%b')} {local.day}"
    if not show_time:
        return day
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{day} · {clock} ET"


def move_display(quote: Quote) -> tuple[str, str]:
    if not quote.ok or quote.price is None:
        return "flat", "Unavailable"
    if quote.change_style == "bp":
        if quote.bp is None:
            return "flat", "—"
        if quote.bp == 0:
            return "flat", "Unchanged"
        direction = "up" if quote.bp > 0 else "down"
        sign = "+" if quote.bp > 0 else "−"
        text = f"{sign}{abs(quote.bp)} bp"
        if quote.comparison:
            text += f" {quote.comparison}"
        return direction, text
    if quote.pct is None:
        return "flat", "—"
    if abs(quote.pct) < 0.005:
        return "flat", "Unchanged"
    direction = "up" if quote.pct > 0 else "down"
    sign = "+" if quote.pct > 0 else "−"
    percent = f"{sign}{abs(quote.pct):.2f}%"
    if quote.change_style == "points" and quote.change is not None:
        point_sign = "+" if quote.change > 0 else "−"
        points = format_abs_number(quote.change, 2)
        return direction, f"{point_sign}{points} · {percent}"
    return direction, percent


HERO_FUTURES = {"^GSPC": "ES=F", "^DJI": "YM=F"}
HERO_INDEXES = ("^GSPC", "^DJI", "^RUT")


def futures_heading(quotes: list[Quote]) -> str:
    futures = [quote for quote in quotes if quote.group == "futures"]
    labels = {quote.session_label for quote in futures if quote.ok}
    if labels == {"Premarket"}:
        return "Futures · premarket"
    if labels == {"Live"}:
        return "Futures · live"
    if labels == {"After hours"}:
        return "Futures · after hours"
    if labels == {"Last"}:
        return "Futures · last price"
    return "Futures · premarket / live"


def render_page(config, now, quotes, rates, picked, briefing: Briefing) -> str:
    site = config["site"]
    title = str(site.get("title") or "Dedicated News")
    eyebrow = str(site.get("eyebrow") or "Morning prep")
    date_line = f"{now.strftime('%A')}, {now.strftime('%B')} {now.day}, {now.year}"
    clock = now.strftime("%I:%M %p").lstrip("0")
    by_symbol = {quote.symbol: quote for quote in quotes}
    hero_cards = []
    for symbol in HERO_INDEXES:
        quote = by_symbol.get(symbol)
        if quote is None:
            continue
        hero_cards.append(render_hero_card(quote, by_symbol.get(HERO_FUTURES.get(symbol, ""))))
    mortgage = next((quote for quote in rates if quote.symbol == "MORTGAGE30US"), None)
    if mortgage is not None:
        hero_cards.append(render_hero_card(mortgage, None))
    hero_html = "".join(hero_cards)
    more_html = render_more_markets(quotes, rates)
    source_line = rate_source_line(rates)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>{esc(title)} · {esc(date_line)}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect fill='%230E2340' width='32' height='32'/%3E%3Crect fill='%23C4A15A' y='24' width='32' height='8'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&family=Source+Serif+4:opsz,wght@8..60,520;8..60,650&display=swap" rel="stylesheet">
<style>
{CSS}
</style>
</head>
<body>
<div class="goldbar"></div>
<header class="band">
  <div class="band-inner">
    <div>
      <p class="eyebrow">{esc(eyebrow)}</p>
      <h1>{esc(title)}</h1>
    </div>
    <div class="when">
      <p class="date">{esc(date_line)}</p>
      <p class="updated">Last updated {esc(clock)} ET</p>
    </div>
  </div>
</header>
<div class="wrap">
  <nav class="jump" aria-label="On this page">
    <a href="#markets">Markets</a>
    <a href="#talking">Talking points</a>
    <a href="#financial">Financial</a>
    <a href="#political">Political</a>
    <a href="#more-markets">More markets</a>
    <a href="#more">More news</a>
    <a href="#industry">Industry</a>
  </nav>
  <section id="markets" class="markets">
    <div class="group-head hero-head">
      <h3>At a glance</h3>
      <p>Last stock close and this week's mortgage rate. A one-line futures percent shows when it fits.</p>
    </div>
    <div class="hero-grid">{hero_html}</div>
  </section>
  {render_briefing(briefing)}
  {render_top("financial", "Top 3 Financial News", "The stories the most outlets are carrying, with a lift for the newest.", picked["financial"])}
  {render_top("political", "Top 3 Political News", "Washington, policy, the Fed, taxes, trade, and elections, ahead of the rest.", picked["political"])}
  {more_html}
  {render_list("more", "More Market & Economy", "The next set of market headlines, without repeating the top three.", picked["more"])}
  {render_list("industry", "Industry News", "Wealth management and financial advisors. Trade journals update more slowly than the wires.", picked["industry"])}
  <footer class="footer">
    <p class="disclaimer">For internal team prep only. Not investment advice.</p>
    <p>{esc(source_line)} Headlines come from the feeds in feeds.yml. The same story is shown once. Each link opens the original.</p>
  </footer>
</div>
</body>
</html>
"""


def rate_source_line(rates: list[Quote]) -> str:
    bits = ["Index, futures, and the extra quotes are from Yahoo Finance."]
    used_fred = any(quote.ok and quote.source_label.startswith("FRED") for quote in rates)
    labels = []
    for quote in rates:
        if quote.ok and quote.source_label:
            labels.append(f"{quote.label} from {quote.source_label}")
        elif not quote.ok:
            labels.append(f"{quote.label} unavailable")
    if labels:
        bits.append("Rates: " + "; ".join(labels) + ".")
    if rates and not used_fred:
        bits.append(
            "FRED did not respond on this run, so the Treasury yields are the U.S. Treasury "
            "daily yield curve, the same figures FRED publishes as DGS10 and DGS2, and the "
            "mortgage rate is Freddie Mac's weekly survey, the same series as FRED MORTGAGE30US."
        )
    bits.append("Figures can be delayed. This page is a briefing, not a quote terminal.")
    return " ".join(bits)


def render_quote(quote: Quote) -> str:
    hint = f'<span class="q-hint">{esc(quote.hint)}</span>' if quote.hint else ""
    if not quote.ok:
        return (
            '<article class="quote is-missing">'
            f'<p class="q-name">{esc(quote.label)}{hint}</p>'
            '<p class="q-price">—</p>'
            '<p class="q-move flat">Unavailable</p>'
            f'<p class="q-asof">{esc(quote.session_label or "No quote")}</p>'
            "</article>"
        )
    direction, move = move_display(quote)
    arrow = {"up": "▲", "down": "▼", "flat": "–"}[direction]
    as_of = format_when(quote.as_of, quote.show_time)
    source = quote.source_label
    meta_bits = [bit for bit in (quote.session_label, as_of, source) if bit]
    if quote.group == "rates" and quote.comparison == "vs prior week" and quote.as_of:
        local = quote.as_of.astimezone(EASTERN)
        week = f"Week of {local.strftime('%b')} {local.day}"
        meta_bits = [week, source] if source else [week]
    return (
        f'<article class="quote {direction}">'
        f'<p class="q-name">{esc(quote.label)}{hint}</p>'
        f'<p class="q-price">{esc(format_price(quote))}</p>'
        f'<p class="q-move {direction}"><span aria-hidden="true">{arrow}</span> {esc(move)}</p>'
        f'<p class="q-asof">{esc(" · ".join(meta_bits))}</p>'
        "</article>"
    )


def render_more_markets(quotes: list[Quote], rates: list[Quote]) -> str:
    nasdaq = [quote for quote in quotes if quote.group == "previous_close" and quote.symbol == "^IXIC"]
    futures = [quote for quote in quotes if quote.group == "futures"]
    treasuries = [quote for quote in rates if quote.symbol != "MORTGAGE30US"]
    extras = [quote for quote in quotes if quote.group == "extras"]
    groups = [
        (
            "Nasdaq",
            "Last completed session. Level, point change, and percent change.",
            nasdaq,
        ),
        (
            futures_heading(quotes),
            "S&P, Dow, and Nasdaq futures (ES, YM, NQ). Percent change versus the prior settle.",
            futures,
        ),
        (
            "Treasuries",
            "10-year and 2-year yields versus the prior close, in basis points. One basis point is 0.01 percentage points.",
            treasuries,
        ),
        (
            "Also moving",
            "Oil, gold, the 10-year market yield, VIX, and Bitcoin. Live when that market is trading. Otherwise the last print.",
            extras,
        ),
    ]
    blocks = []
    for heading, note, rows in groups:
        if not rows:
            continue
        cards = "".join(render_quote(quote) for quote in rows)
        blocks.append(
            '<div class="group">'
            f'<div class="group-head"><h3>{esc(heading)}</h3><p>{esc(note)}</p></div>'
            f'<div class="quote-grid">{cards}</div></div>'
        )
    return (
        '<section class="block more-markets" id="more-markets">'
        '<div class="section-head"><h2>More markets</h2>'
        "<p>Nasdaq, Treasuries, futures, oil, gold, VIX, and Bitcoin.</p></div>"
        f"{''.join(blocks)}</section>"
    )


def render_hero_card(quote: Quote, future: Quote | None) -> str:
    chip = futures_chip(future) if future is not None else ""
    if not quote.ok:
        return (
            '<article class="quote hero is-missing">'
            f'<p class="q-name">{esc(quote.label)}</p>'
            '<p class="q-price">—</p>'
            '<p class="q-move flat">Unavailable</p>'
            "</article>"
        )
    direction, move = move_display(quote)
    if quote.comparison == "vs prior week":
        move = move.replace(" vs prior week", " this week")
    arrow = {"up": "▲", "down": "▼", "flat": "–"}[direction]
    if quote.comparison == "vs prior week" and quote.as_of:
        local = quote.as_of.astimezone(EASTERN)
        meta = f"Week of {local.strftime('%b')} {local.day}"
    else:
        meta = format_when(quote.as_of, False)
    return (
        f'<article class="quote hero {direction}">'
        f'<p class="q-name">{esc(quote.label)}</p>'
        f'<p class="q-price">{esc(format_price(quote))}</p>'
        f'<p class="q-move {direction}"><span aria-hidden="true">{arrow}</span> {esc(move)}</p>'
        f"{chip}"
        f'<p class="q-asof">{esc(meta)}</p>'
        "</article>"
    )


def futures_chip(quote: Quote) -> str:
    if not quote.ok or quote.pct is None:
        return ""
    prefix = {
        "Premarket": "Premkt",
        "Live": "Live",
        "After hours": "After hrs",
        "Last": "Last",
    }.get(quote.session_label, "Fut")
    if abs(quote.pct) < 0.005:
        direction = "flat"
        text = f"{prefix} flat"
    else:
        direction = "up" if quote.pct > 0 else "down"
        sign = "+" if quote.pct > 0 else "−"
        text = f"{prefix} {sign}{abs(quote.pct):.2f}%"
    # A longer label would wrap on a phone card. Leave it off rather than crowd the top.
    if len(text) > 16:
        return ""
    return f'<p class="q-fut {direction}">{esc(text)}</p>'


def render_briefing(briefing: Briefing) -> str:
    paragraphs = "".join(f"<p>{esc(sentence)}</p>" for sentence in briefing.paragraphs)
    if briefing.say:
        quoted = " ".join(f"“{esc(line)}”" for line in briefing.say)
        paragraphs += f'<p class="script">You can say it this way: {quoted}</p>'
    return f"""
  <section class="talking" id="talking">
    <p class="kicker">Client talking points</p>
    <h2>{esc(briefing.headline)}</h2>
    <div class="passage">{paragraphs}</div>
    <p class="fine">Internal prep. Not a forecast and not investment advice.</p>
  </section>
"""


def render_top(anchor: str, title: str, note: str, clusters: list[list[Item]]) -> str:
    if not clusters:
        body = '<p class="empty">No fresh headlines from these sources right now.</p>'
    else:
        cards = []
        for index, cluster in enumerate(clusters, start=1):
            item = choose_representative(cluster)
            names = outlet_names(cluster)
            if len(names) == 1:
                outlets = names[0]
            elif len(names) <= 3:
                outlets = human_join(names)
            else:
                outlets = human_join(names[:3]) + f" +{len(names) - 3}"
            count = f"{len(names)} outlet" if len(names) == 1 else f"{len(names)} outlets"
            cards.append(
                '<article class="story">'
                f'<p class="rank">{index}</p>'
                '<div>'
                f'<a href="{esc(item.link)}" target="_blank" rel="noopener noreferrer">{esc(item.title)}</a>'
                f'<p class="meta">{esc(outlets)} · {esc(count)} · {esc(format_when(item.published, True))}</p>'
                "</div></article>"
            )
        body = "".join(cards)
    return (
        f'<section class="block" id="{anchor}">'
        f'<div class="section-head"><h2>{esc(title)}</h2><p>{esc(note)}</p></div>'
        f"{body}</section>"
    )


def render_list(anchor: str, title: str, note: str, clusters: list[list[Item]]) -> str:
    if not clusters:
        body = '<p class="empty">No fresh headlines from these sources right now.</p>'
    else:
        rows = []
        for cluster in clusters:
            item = choose_representative(cluster)
            rows.append(
                '<li>'
                f'<a href="{esc(item.link)}" target="_blank" rel="noopener noreferrer">{esc(item.title)}</a>'
                f'<span class="meta">{esc(item.source)} · {esc(format_when(item.published, True))}</span>'
                "</li>"
            )
        body = f'<ul class="link-grid">{"".join(rows)}</ul>'
    return (
        f'<section class="block" id="{anchor}">'
        f'<div class="section-head"><h2>{esc(title)}</h2><p>{esc(note)}</p></div>'
        f"{body}</section>"
    )


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


CSS = """
:root {
  color-scheme: light;
  --navy: #0e2340;
  --navy-2: #16345c;
  --gold: #b0893e;
  --gold-2: #c4a15a;
  --paper: #f4f1ea;
  --card: #fffcf8;
  --ink: #1c2430;
  --muted: #5e6a78;
  --line: #e4dcc8;
  --up: #0f7a45;
  --down: #b13333;
  --shadow: 0 8px 24px rgba(14, 35, 64, 0.05);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: "Source Sans 3", "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 17px;
  line-height: 1.45;
}
.goldbar { height: 5px; background: var(--gold-2); }
.band { background: var(--navy); color: #f7f3ea; }
.band-inner {
  max-width: 1120px;
  margin: 0 auto;
  padding: 26px 22px 22px;
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 18px;
}
.eyebrow {
  margin: 0 0 4px;
  color: var(--gold-2);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}
h1 {
  margin: 0;
  font-family: "Source Serif 4", Georgia, "Times New Roman", serif;
  font-weight: 650;
  font-size: 46px;
  letter-spacing: -0.03em;
  line-height: 1;
}
.when { text-align: right; }
.date { margin: 0; font-size: 18px; font-weight: 600; }
.updated { margin: 4px 0 0; color: #d9c89a; font-size: 14px; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 8px 22px 56px; }
.jump {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  padding: 14px 0 6px;
}
.jump a {
  color: var(--navy-2);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  text-decoration: none;
}
.jump a:hover { color: var(--gold); }
.group { margin-top: 18px; }
.group-head h3 {
  margin: 0;
  font-family: "Source Serif 4", Georgia, serif;
  font-size: 22px;
  color: var(--navy);
}
.group-head p { margin: 3px 0 10px; color: var(--muted); font-size: 14px; max-width: 70ch; }
.quote-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}
.quote {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 12px 14px 11px;
  box-shadow: 0 1px 0 rgba(14, 35, 64, 0.04);
  min-height: 118px;
}
.q-name {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.q-hint {
  margin-left: 6px;
  color: var(--gold);
  letter-spacing: 0.04em;
}
.q-price {
  margin: 6px 0 2px;
  color: var(--navy);
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.q-move {
  margin: 0;
  font-size: 14px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.q-move.up { color: var(--up); }
.q-move.down { color: var(--down); }
.q-move.flat { color: var(--muted); font-weight: 600; }
.q-asof { margin: 6px 0 0; color: #7b8794; font-size: 12px; }
.talking {
  margin-top: 26px;
  background: var(--card);
  border: 1px solid var(--line);
  border-top: 4px solid var(--gold);
  border-radius: 16px;
  padding: 22px 26px 18px;
  box-shadow: var(--shadow);
}
.kicker {
  margin: 0 0 6px;
  color: var(--gold);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.talking h2 {
  margin: 0 0 10px;
  font-family: "Source Serif 4", Georgia, serif;
  font-size: 30px;
  line-height: 1.2;
  color: var(--navy);
  font-weight: 650;
}
.passage p { margin: 0 0 10px; font-size: 18px; }
.passage .script { margin: 6px 0 0; font-weight: 650; color: var(--navy); }
.fine { margin: 8px 0 0; color: var(--muted); font-size: 12px; }
.hero-head { margin-top: 8px; }
.hero-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.quote.hero { min-height: 0; padding: 10px 12px 9px; }
.quote.hero .q-price { font-size: 24px; }
.q-fut {
  margin: 3px 0 0;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.01em;
  white-space: nowrap;
}
.q-fut.up { color: var(--up); }
.q-fut.down { color: var(--down); }
.q-fut.flat { color: var(--muted); font-weight: 600; }
.more-markets .group { margin-top: 14px; }
.more-markets .group:first-of-type { margin-top: 4px; }
.block { margin-top: 28px; }
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 16px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 12px;
  padding-bottom: 8px;
}
.section-head h2 {
  margin: 0;
  font-family: "Source Serif 4", Georgia, serif;
  font-size: 28px;
  color: var(--navy);
  font-weight: 650;
}
.section-head p { margin: 0; color: var(--muted); font-size: 14px; max-width: 46ch; text-align: right; }
.story {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 14px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 14px 16px;
  margin: 0 0 10px;
}
.rank {
  width: 32px;
  height: 32px;
  margin: 2px 0 0;
  border-radius: 50%;
  background: var(--navy);
  color: #f3e6c4;
  display: grid;
  place-items: center;
  font-weight: 700;
  font-size: 14px;
}
.story a {
  color: var(--navy);
  font-family: "Source Serif 4", Georgia, serif;
  font-size: 22px;
  line-height: 1.25;
  text-decoration: none;
}
.story a:hover, .link-grid a:hover { text-decoration: underline; }
.meta { display: block; margin-top: 4px; color: var(--muted); font-size: 13px; }
.link-grid {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 28px;
}
.link-grid li { padding: 9px 0; border-bottom: 1px solid var(--line); }
.link-grid a {
  color: var(--navy);
  text-decoration: none;
  font-weight: 600;
  font-size: 16px;
}
.empty { color: var(--muted); }
.footer {
  margin-top: 34px;
  padding-top: 14px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 13px;
}
.disclaimer {
  margin: 0 0 6px;
  color: var(--navy);
  font-weight: 700;
  font-size: 14px;
}
@media (max-width: 800px) {
  h1 { font-size: 30px; }
  .band-inner { padding: 14px 14px 12px; }
  .band-inner, .section-head { flex-direction: column; align-items: flex-start; }
  .when, .section-head p { text-align: left; }
  .date { font-size: 15px; }
  .wrap { padding: 4px 14px 40px; }
  .jump { gap: 6px 12px; padding: 10px 0 2px; }
  .talking { padding: 16px 14px; }
  .talking h2 { font-size: 24px; }
  .passage p { font-size: 17px; }
  .story a { font-size: 19px; }
  .link-grid { grid-template-columns: 1fr; }
  .quote-grid { grid-template-columns: 1fr 1fr; }
  .q-price { font-size: 22px; }
  .hero-grid { grid-template-columns: 1fr 1fr; gap: 8px; }
  .quote.hero { padding: 8px 10px 8px; }
  .quote.hero .q-price { font-size: 20px; }
  .quote.hero .q-move { font-size: 13px; }
  .hero-head p { margin-bottom: 8px; }
}
@media (max-width: 420px) {
  .quote-grid { grid-template-columns: 1fr; }
  .hero-grid { grid-template-columns: 1fr 1fr; }
  h1 { font-size: 28px; }
}
"""


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
