"""SQLite 存储层：documents 与 crawl_log 两张表。"""
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT NOT NULL,
    url              TEXT NOT NULL UNIQUE,
    title            TEXT,
    publish_date     TEXT,
    crawl_time       TEXT,
    content_text     TEXT,
    raw_path         TEXT,
    keywords_matched TEXT,
    created_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);
CREATE INDEX IF NOT EXISTS idx_documents_publish_date ON documents(publish_date);

CREATE TABLE IF NOT EXISTS crawl_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    pages_fetched INTEGER DEFAULT 0,
    items_found   INTEGER DEFAULT 0,
    items_new     INTEGER DEFAULT 0,
    status        TEXT,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source             TEXT NOT NULL,
    platform           TEXT,
    cert_type          TEXT,
    price_type         TEXT,
    price              REAL,
    volume             REAL,
    period_granularity TEXT,
    period_start       TEXT,
    period_end         TEXT,
    url                TEXT,
    quality            TEXT,
    crawl_time         TEXT,
    created_at         TEXT,
    UNIQUE (source, platform, price_type, period_start, cert_type)
);
CREATE INDEX IF NOT EXISTS idx_prices_period ON prices(period_start);
"""


def utc_now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        """老库补列：prices.quality（v0.3 数据质量分级）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(prices)")}
        if "quality" not in cols:
            self.conn.execute("ALTER TABLE prices ADD COLUMN quality TEXT")
            self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def document_exists(self, url):
        row = self.conn.execute(
            "SELECT 1 FROM documents WHERE url = ?", (url,)
        ).fetchone()
        return row is not None

    def touch_document(self, url):
        """重复抓取只更新 crawl_time，不产生新行。"""
        self.conn.execute(
            "UPDATE documents SET crawl_time = ? WHERE url = ?", (utc_now(), url)
        )
        self.conn.commit()

    def insert_document(self, doc):
        self.conn.execute(
            """INSERT INTO documents
               (source, url, title, publish_date, crawl_time, content_text,
                raw_path, keywords_matched, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc["source"],
                doc["url"],
                doc.get("title"),
                doc.get("publish_date"),
                utc_now(),
                doc.get("content_text"),
                doc.get("raw_path"),
                doc.get("keywords_matched"),
                utc_now(),
            ),
        )
        self.conn.commit()

    def upsert_price(self, row):
        """按 (source, platform, price_type, period_start, cert_type) 逻辑去重：
        已存在则更新 price/volume/crawl_time/url/period_end，返回 'new' 或 'updated'。"""
        key = (row["source"], row.get("platform"), row.get("price_type"),
               row.get("period_start"), row.get("cert_type"))
        existing = self.conn.execute(
            """SELECT id FROM prices
               WHERE source = ? AND platform IS ? AND price_type IS ?
                 AND period_start IS ? AND cert_type IS ?""",
            key,
        ).fetchone()
        if existing:
            self.conn.execute(
                """UPDATE prices SET price = ?, volume = ?, period_end = ?,
                       url = ?, quality = ?, crawl_time = ?
                   WHERE id = ?""",
                (row.get("price"), row.get("volume"), row.get("period_end"),
                 row.get("url"), row.get("quality"), utc_now(), existing["id"]),
            )
            self.conn.commit()
            return "updated"
        self.conn.execute(
            """INSERT INTO prices
               (source, platform, cert_type, price_type, price, volume,
                period_granularity, period_start, period_end, url, quality,
                crawl_time, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["source"], row.get("platform"), row.get("cert_type"),
             row.get("price_type"), row.get("price"), row.get("volume"),
             row.get("period_granularity"), row.get("period_start"),
             row.get("period_end"), row.get("url"), row.get("quality"),
             utc_now(), utc_now()),
        )
        self.conn.commit()
        return "new"

    def log_crawl(self, source, started_at, pages_fetched, items_found,
                  items_new, status, error=None):
        self.conn.execute(
            """INSERT INTO crawl_log
               (source, started_at, finished_at, pages_fetched,
                items_found, items_new, status, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, started_at, utc_now(), pages_fetched,
             items_found, items_new, status, error),
        )
        self.conn.commit()
