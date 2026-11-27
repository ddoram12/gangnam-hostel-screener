import datetime
import sqlite3
import pandas as pd

DB_PATH = "comp_set_tracker.db"


def init_db():
    """Comp Set 및 가격 스냅샷 테이블을 초기화합니다."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS comp_set_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hotel_name TEXT UNIQUE NOT NULL,
            property_token TEXT NOT NULL,
            resolved_title TEXT,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_date TEXT NOT NULL,
            hotel_name TEXT NOT NULL,
            adults INTEGER NOT NULL,
            checkin_date TEXT NOT NULL,
            day_type TEXT NOT NULL,
            nightly_rate REAL,
            status TEXT NOT NULL,
            UNIQUE(captured_date, hotel_name, adults, checkin_date)
        )
    """)
    conn.commit()
    conn.close()


def get_saved_comp_set() -> pd.DataFrame:
    """등록된 Comp Set 목록을 반환합니다."""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT id, hotel_name AS '입력숙소명', resolved_title AS '구글매칭명', "
        "property_token, note AS '비고', created_at AS '등록일' "
        "FROM comp_set_properties ORDER BY id ASC",
        conn,
    )
    conn.close()
    return df


def upsert_comp_set_property(hotel_name: str, property_token: str, resolved_title: str, note: str = ""):
    """Comp Set 숙소를 등록하거나 기존 정보를 갱신합니다."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO comp_set_properties (hotel_name, property_token, resolved_title, note)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(hotel_name) DO UPDATE SET
            property_token=excluded.property_token,
            resolved_title=excluded.resolved_title,
            note=excluded.note
    """, (hotel_name.strip(), property_token, resolved_title, note.strip()))
    conn.commit()
    conn.close()


def delete_comp_set_property(hotel_name: str):
    """선택한 Comp Set 숙소를 삭제합니다."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM comp_set_properties WHERE hotel_name = ?", (hotel_name,))
    conn.commit()
    conn.close()


def clear_comp_set_properties():
    """등록된 모든 Comp Set 숙소를 삭제합니다."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM comp_set_properties")
    conn.commit()
    conn.close()


def save_snapshots_to_db(records: list[dict]):
    """수집된 일자별 가격 스냅샷을 DB에 저장합니다."""
    if not records:
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for r in records:
        cur.execute("""
            INSERT INTO price_snapshots (
                captured_date, hotel_name, adults, checkin_date, day_type, nightly_rate, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(captured_date, hotel_name, adults, checkin_date) DO UPDATE SET
                nightly_rate=excluded.nightly_rate,
                status=excluded.status
        """, (
            r["수집일자"], r["경쟁숙소"], r["기준인원"],
            r["체크인일자"], r["요일구분"], r["노출판매가(원)"], r["예약상태"],
        ))
    conn.commit()
    conn.close()


def load_historical_snapshots(days_back: int = 30) -> pd.DataFrame:
    """최근 N일간의 가격 스냅샷 히스토리를 조회합니다."""
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.date.today() - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
    df = pd.read_sql_query(
        "SELECT captured_date AS '수집일자', hotel_name AS '경쟁숙소', adults AS '기준인원', "
        "checkin_date AS '체크인일자', day_type AS '요일구분', nightly_rate AS '노출판매가(원)', "
        "status AS '예약상태' FROM price_snapshots WHERE captured_date >= ? "
        "ORDER BY captured_date DESC, checkin_date ASC",
        conn,
        params=(cutoff,),
    )
    conn.close()
    return df
