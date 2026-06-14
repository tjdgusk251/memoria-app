"""
MEMORIA — 망각곡선 기반 스마트 복습 시스템 (13주차 완성도 향상 버전)
================================================================
13주차 강의교안('시스템 완성도 향상 — 디버깅·배포·발표 준비')의 5블록을
Streamlit 환경에 맞게 이식하여 안정성·UX·배포 준비도를 끌어올린 버전입니다.

  ① 자가점검  → 사이드바 '시스템 자가점검' 패널 (DB·인코딩·데이터 상태 점검)
  ② 디버깅5선 → NULL/빈 입력 방어, 한글 인코딩, reactive(캐시), 성능, 변수 일관성
  ③ UX 마감   → validate(검증) · withProgress(st.spinner) · showNotification(st.toast)
  ④ 배포      → 상대경로 DB, UTF-8, 빈 상태에서도 빨간 화면 없음 (Streamlit Cloud 호환)
  ⑤ 발표      → 사이드바 시스템 소개 + 일관된 빈 상태 안내 문구

[핵심 업그레이드] 에빙하우스 망각곡선 R = 100·e^(-t/S) 실시간 반영
  · 기억 유지율을 '저장된 고정값'이 아니라 '마지막 복습 이후 경과 시간'으로 매번 계산
  · 안정성 S(일): 기억 강도. 복습 성공 시 S↑(오래 기억), 실패 시 S↓(빨리 잊음)
  · 시간이 지날수록 자동으로 유지율이 떨어지고, 위험 항목이 스스로 떠오른다
"""

import hashlib
import hmac
import json
import math
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import sqlite3
import streamlit as st

# PyPDF2는 선택 의존성: 미설치 환경에서도 앱이 죽지 않도록 방어 (배포 점검 #패키지 누락)
try:
    import PyPDF2

    PYPDF2_OK = True
except Exception:  # pragma: no cover
    PYPDF2_OK = False


# ============================================================
# 0. 서비스 환경 설정 + 망각곡선 파라미터
# ============================================================
st.set_page_config(page_title="MEMORIA - 스마트 복습 시스템", layout="wide")

DB_FILE = os.environ.get("MEMORIA_DB", "memoria.db")   # 테스트/배포 시 경로 교체 가능
DEFAULT_STABILITY = 3.0              # 새 카드의 초기 안정성 S(일)
S_MIN, S_MAX = 1.0, 365.0           # 안정성 허용 범위(일)
# 자기채점 → 안정성 S 배수 (못 맞히면 줄어 곧 다시 등장, 맞히면 늘어 한동안 안 뜸)
GRADE_FACTORS = {"wrong": 0.5, "vague": 0.85, "partial": 1.4, "correct": 2.3}
# 채점 항목 메타: (코드, 화면 라벨, 아이콘) — 좌→우로 갈수록 잘 기억한 상태
GRADE_OPTIONS = [
    ("wrong", "못 맞춤", "🔴"),
    ("vague", "헷갈림", "🟠"),
    ("partial", "일부 맞춤", "🟡"),
    ("correct", "맞춤", "🟢"),
]
RISK_THRESHOLD = 30                  # 유지율 30% 미만 = '위험'(빨강)
DUE_THRESHOLD = 60                  # 유지율 60% 미만 = '복습 대기'

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
html, body, .main, [data-testid="stAppViewContainer"] {
    background-color: #FDFDFD !important; color: #1A1C1E; font-family: 'Inter', sans-serif;
}
[data-testid="stSidebar"] {
    background-color: #F1F3F9 !important; border-right: 1px solid #E0E4EB;
}
[data-testid="stHeader"], [data-testid="stToolbar"], #MainMenu, footer {
    display: none !important;
}
.stMetric {
    background: #FFFFFF; padding: 20px; border-radius: 12px;
    border: 1px solid #E0E4EB; box-shadow: 0 2px 4px rgba(0,0,0,0.02);
}
.header-title { color: #0061FF; font-weight: 700; font-size: 2.5rem; margin-bottom: 0; }
.pdf-box {
    background-color: #F8F9FA; padding: 15px; border-radius: 8px;
    border: 1px solid #E0E4EB; height: 300px; overflow-y: scroll; font-size: 14px;
    white-space: pre-wrap;
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# 1. DB 연결 — 안전한 자동 종료 (교안: "DB 연결 시 dbDisconnect 호출")
# ============================================================
def _turso_creds():
    """배포 시 영구 저장소(Turso/libSQL) 접속 정보. secrets.toml의 [turso] 또는 환경변수.
    둘 다 없으면 (None, None) → 로컬 SQLite 파일 사용."""
    url = token = None
    try:
        url = st.secrets["turso"]["url"]
        token = st.secrets["turso"]["token"]
    except Exception:
        url = os.environ.get("TURSO_DATABASE_URL")
        token = os.environ.get("TURSO_AUTH_TOKEN")
    return url, token


@contextmanager
def get_conn():
    """with 블록을 벗어나면 예외가 나도 반드시 connection을 닫는다.
    Turso 접속정보가 있으면 클라우드(영구) DB, 없으면 로컬 SQLite 파일에 연결한다.
    두 드라이버 모두 sqlite3 호환(? 플레이스홀더, execute/commit/fetchall)이라 쿼리는 동일하다."""
    url, token = _turso_creds()
    if url and token:
        import libsql
        conn = libsql.connect(database=url, auth_token=token)
    else:
        conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# 2. 데이터베이스 초기화 + 망각곡선 스키마 마이그레이션
# ============================================================
def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS study_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                topic TEXT NOT NULL,
                retention INTEGER NOT NULL,
                last_date TEXT NOT NULL,
                question TEXT,
                answer TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                salt TEXT NOT NULL,
                pw_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


def ensure_user_column():
    """study_items에 소유자(user_id) 컬럼을 추가. 계정별 카드 분리의 기반."""
    with get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(study_items)").fetchall()]
        if "user_id" not in cols:
            conn.execute("ALTER TABLE study_items ADD COLUMN user_id INTEGER")
            conn.commit()


HAS_REVIEW_COUNT = False   # 마이그레이션 성공 여부 — 실패해도 앱이 죽지 않게 분기에 사용


def ensure_review_count_column():
    """복습 횟수(review_count) 컬럼 추가. review_count=0 이면 '아직 한 번도 복습 안 한 새 카드'로
    보고 '지금 복습' 대상에 항상 포함한다. 기존 카드는 1로 두어 유지율로만 판정한다.
    어떤 이유로든 실패하면 HAS_REVIEW_COUNT=False 로 두고, 앱은 컬럼 없이도 동작한다."""
    global HAS_REVIEW_COUNT
    try:
        with get_conn() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(study_items)").fetchall()]
            if "review_count" not in cols:
                conn.execute("ALTER TABLE study_items ADD COLUMN review_count INTEGER")
                conn.commit()
                conn.execute("UPDATE study_items SET review_count = 1 WHERE review_count IS NULL")
                conn.commit()
        HAS_REVIEW_COUNT = True
    except Exception:
        HAS_REVIEW_COUNT = False


def ensure_stability_column():
    """기존 DB에 안정성 S 컬럼이 없으면 추가하고, 옛 데이터를 자동 마이그레이션.
    저장돼 있던 retention과 경과일로부터 S를 역산하여 곡선이 자연스럽게 이어지게 한다.
        R = 100·e^(-t/S)  ⇒  S = -t / ln(R/100)
    """
    with get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(study_items)").fetchall()]
        if "stability" in cols:
            return
        conn.execute("ALTER TABLE study_items ADD COLUMN stability REAL")
        conn.commit()

        today = datetime.now().date()
        rows = conn.execute("SELECT id, retention, last_date FROM study_items").fetchall()
        for item_id, retention, last_date in rows:
            try:
                t = (today - datetime.strptime(last_date, "%Y-%m-%d").date()).days
                r = (retention or 50) / 100.0
                if t > 0 and 0.0 < r < 1.0:
                    S = -t / math.log(r)               # 역산
                    S = min(S_MAX, max(S_MIN, S))
                else:
                    S = DEFAULT_STABILITY
            except Exception:
                S = DEFAULT_STABILITY
            conn.execute("UPDATE study_items SET stability = ? WHERE id = ?", (S, item_id))
        conn.commit()


def ensure_qa_columns():
    """문제 유형(qtype)과 서술형 채점용 핵심어(keywords) 컬럼을 추가.
    유형은 단답형(short)·서술형(essay) 두 가지. 기존 카드는 정답 모양으로 자동 추정한다."""
    with get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(study_items)").fetchall()]
        if "qtype" not in cols:
            conn.execute("ALTER TABLE study_items ADD COLUMN qtype TEXT")
            conn.commit()
            rows = conn.execute("SELECT id, answer FROM study_items").fetchall()
            for item_id, answer in rows:
                a = (answer or "").strip()
                # 정답이 길거나 문장형으로 끝나면 서술형, 아니면 단답형
                if len(a) > 25 or a.endswith(("다", "다.", "요", "요.")):
                    qt = "essay"
                else:
                    qt = "short"
                conn.execute("UPDATE study_items SET qtype = ? WHERE id = ?", (qt, item_id))
            conn.commit()
        else:
            # 과거 빈칸형(blank)으로 저장된 카드를 단답형(short)으로 통합
            conn.execute("UPDATE study_items SET qtype = 'short' WHERE qtype = 'blank'")
            conn.commit()
        if "keywords" not in cols:
            conn.execute("ALTER TABLE study_items ADD COLUMN keywords TEXT")
            conn.commit()


init_db()
ensure_stability_column()
ensure_qa_columns()
ensure_user_column()
ensure_review_count_column()


# ============================================================
# 2-B. 인증 — 자체 아이디/비밀번호 (pbkdf2-sha256, 평문 저장 안 함)
# ============================================================
PBKDF2_ITERATIONS = 200_000


def hash_password(password, salt=None):
    """비밀번호를 솔트와 함께 pbkdf2-sha256으로 해시. (salt_hex, hash_hex) 반환."""
    if salt is None:
        salt = os.urandom(16)
    elif isinstance(salt, str):
        salt = bytes.fromhex(salt)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), dk.hex()


def count_users():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username, password):
    """신규 계정 생성. 성공 시 (user_id, None), 실패 시 (None, 오류메시지)."""
    username = (username or "").strip()
    if len(username) < 3:
        return None, "아이디는 3자 이상이어야 합니다."
    if len(password or "") < 4:
        return None, "비밀번호는 4자 이상이어야 합니다."
    salt_hex, pw_hex = hash_password(password)
    try:
        with get_conn() as conn:
            first_user = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            cur = conn.execute(
                "INSERT INTO users (username, salt, pw_hash, created_at) VALUES (?, ?, ?, ?)",
                (username, salt_hex, pw_hex, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            new_id = cur.lastrowid
            # 최초 가입자에게 소유자 없는 기존 카드를 귀속 (로컬 데이터 보존)
            if first_user:
                conn.execute("UPDATE study_items SET user_id = ? WHERE user_id IS NULL", (new_id,))
            conn.commit()
        invalidate_caches()
        return new_id, None
    except sqlite3.IntegrityError:
        return None, "이미 사용 중인 아이디입니다."


def authenticate(username, password):
    """로그인 검증. 성공 시 user_id, 실패 시 None."""
    username = (username or "").strip()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, salt, pw_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
    if not row:
        return None
    user_id, salt_hex, pw_hex = row
    _, check = hash_password(password, salt_hex)
    # 타이밍 공격 완화를 위해 상수시간 비교
    return user_id if hmac.compare_digest(check, pw_hex) else None


def current_user_id():
    return st.session_state.get("auth_user_id")


# ============================================================
# 3. 망각곡선 핵심 계산
# ============================================================
def live_retention(last_date_str, stability):
    """현재 시점의 실시간 기억 유지율(%) = 100·e^(-경과일/S)."""
    try:
        t = (datetime.now().date() - datetime.strptime(last_date_str, "%Y-%m-%d").date()).days
    except Exception:
        t = 0
    t = max(0, t)
    S = max(0.1, float(stability or DEFAULT_STABILITY))
    return 100.0 * math.exp(-t / S)


def add_live_columns(df):
    """DataFrame에 경과일·실시간 유지율 컬럼을 부여한다(벡터 연산)."""
    if df.empty:
        df["elapsed_days"] = []
        df["retention"] = []
        return df
    today = pd.Timestamp(datetime.now().date())
    last = pd.to_datetime(df["last_date"], errors="coerce")
    elapsed = (today - last).dt.days.clip(lower=0).fillna(0)
    S = df["stability"].astype(float).clip(lower=0.1).fillna(DEFAULT_STABILITY)
    df = df.copy()
    df["elapsed_days"] = elapsed.astype(int)
    df["retention"] = (100.0 * np.exp(-elapsed / S)).round().astype(int)
    return df


# ============================================================
# 4. 백엔드 로직 (읽기 쿼리는 캐시, 쓰기 후 invalidate)
# ============================================================
@st.cache_data(ttl=300, show_spinner=False)
def get_unique_subjects(user_id):
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT subject FROM study_items WHERE user_id = ? ORDER BY subject ASC",
                (user_id,),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        st.warning(f"과목 목록을 불러오지 못했습니다: {e}")
        return []


@st.cache_data(ttl=300, show_spinner=False)
def query_items_from_db(user_id, subjects, start_date, end_date):
    """필터 조건의 카드를 조회하고 실시간 망각곡선 유지율을 계산해 반환.
    유지율이 시간에 따라 변하므로 retention 정렬은 SQL이 아닌 pandas에서 수행한다."""
    # review_count 컬럼이 있으면 함께 조회, 없으면(마이그레이션 미완) 생략하고 1로 채움
    rc_sql = ", review_count" if HAS_REVIEW_COUNT else ""
    select_cols = ["id", "subject", "topic", "retention", "last_date",
                   "question", "answer", "stability", "qtype", "keywords"]
    if HAS_REVIEW_COUNT:
        select_cols = select_cols + ["review_count"]
    base_cols = ["id", "subject", "topic", "retention", "last_date", "question",
                 "answer", "stability", "qtype", "keywords", "review_count", "elapsed_days"]
    if not subjects:
        return pd.DataFrame(columns=base_cols)
    try:
        placeholders = ",".join("?" for _ in subjects)
        query = f"""
            SELECT id, subject, topic, retention, last_date, question, answer,
                   stability, qtype, keywords{rc_sql}
            FROM study_items
            WHERE user_id = ?
              AND subject IN ({placeholders})
              AND date(last_date) >= date(?)
              AND date(last_date) <= date(?)
        """
        params = [user_id] + list(subjects) + [
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
        ]
        with get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        # 드라이버 무관(로컬 sqlite3·클라우드 libsql 모두 호환)하도록 직접 DataFrame 구성
        df = pd.DataFrame(rows, columns=select_cols) if rows else pd.DataFrame(columns=select_cols)
        if "review_count" not in df.columns:
            df["review_count"] = 1                       # 컬럼 없을 때 기본값(=신규 아님)

        df = add_live_columns(df)                       # 실시간 유지율 계산
        if not df.empty:
            df["review_count"] = df["review_count"].fillna(1).astype(int)
            df = df.sort_values("retention").reset_index(drop=True)
        return df
    except Exception as e:
        st.warning(f"데이터 조회 중 문제가 발생했습니다: {e}")
        return pd.DataFrame(columns=base_cols)


def subject_summary(df):
    """과목별 덱 요약: 카드 수·위험 카드 수·평균 유지율.
    위험이 많은 덱, 평균 유지율이 낮은 덱이 위로 오도록 정렬한다."""
    out = []
    if df is None or df.empty:
        return out
    for sub, g in df.groupby("subject"):
        out.append({
            "subject": sub,
            "count": int(len(g)),
            "risk": int((g["retention"] < RISK_THRESHOLD).sum()),
            "avg": int(round(g["retention"].mean())),
        })
    out.sort(key=lambda d: (-d["risk"], d["avg"]))
    return out


def start_review_session(rows_df, label):
    """복습 세션 시작 — 유지율이 낮은(잘 잊은) 카드부터 한 장씩 풀도록 큐를 만든다."""
    ordered = rows_df.sort_values("retention").reset_index(drop=True)
    st.session_state["review_session"] = {
        "queue": ordered.to_dict("records"),
        "pos": 0,
        "revealed": False,
        "label": label,
        "stats": {"correct": 0, "partial": 0, "vague": 0, "wrong": 0},
    }


def invalidate_caches():
    get_unique_subjects.clear()
    query_items_from_db.clear()


def grade_item(item_id, score_type, user_id):
    """자기채점 → 안정성 S 갱신 + 복습일(last_date) 갱신.
    채점 직후엔 방금 복습했으므로 유지율이 100%로 회복되고, 이후 S 속도로 다시 감소한다.
        · wrong(못 맞춤) : S를 절반으로 → 내일이면 다시 복습 목록에 등장
        · vague(헷갈림)  : S를 살짝 줄여 곧 다시 등장
        · partial(일부)  : S를 소폭 늘림
        · correct(맞춤)  : S를 크게 키움 → 한동안 안 떠오름
    반환: (이전 S, 새로운 S)"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT stability FROM study_items WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        ).fetchone()
        old_S = float(row[0]) if row and row[0] else DEFAULT_STABILITY
        new_S = min(S_MAX, max(S_MIN, old_S * GRADE_FACTORS.get(score_type, 1.0)))
        rc_set = ", review_count = COALESCE(review_count, 0) + 1" if HAS_REVIEW_COUNT else ""
        conn.execute(
            f"UPDATE study_items SET stability = ?, retention = 100, last_date = ?{rc_set} "
            "WHERE id = ? AND user_id = ?",
            (new_S, datetime.now().strftime("%Y-%m-%d"), item_id, user_id),
        )
        conn.commit()
    invalidate_caches()
    return old_S, new_S


def next_interval_days(old_S, code):
    """이 채점을 선택하면 '다음 복습까지' 며칠인지 미리 계산.
    채점 후 S가 배수만큼 바뀌고, 유지율이 50%(반감기)로 떨어지는 시점을 다음 복습으로 본다."""
    new_S = min(S_MAX, max(S_MIN, old_S * GRADE_FACTORS.get(code, 1.0)))
    return max(1, round(new_S * math.log(2)))


def days_until_due(stability, elapsed_days):
    """유지율이 복습 기준(DUE_THRESHOLD %)으로 떨어질 때까지 남은 일수(대략)."""
    S = max(0.1, float(stability or DEFAULT_STABILITY))
    t_due = -S * math.log(DUE_THRESHOLD / 100.0)
    return max(0, round(t_due) - int(elapsed_days))


def split_due(df):
    """카드를 '지금 복습'(due)과 '복습 예정'(아직)으로 나눈다.
    due = 유지율이 기준 미만이거나, 아직 한 번도 복습 안 한 새 카드(review_count==0)."""
    if df is None or df.empty:
        empty = df
        return empty, empty
    is_new = df["review_count"].fillna(1).astype(int) == 0
    due_mask = (df["retention"] < DUE_THRESHOLD) | is_new
    return df[due_mask].copy(), df[~due_mask].copy()


def parse_keywords(raw):
    """keywords 컬럼(JSON 문자열)을 리스트로 복원. 비었거나 형식이 깨지면 빈 리스트."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def item_exists(subject, question, user_id):
    """같은 사용자의 같은 과목·문제 텍스트가 이미 있으면 True (중복 저장 방지용)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM study_items WHERE user_id = ? AND subject = ? AND question = ? LIMIT 1",
            (user_id, subject, question),
        ).fetchone()
    return row is not None


def insert_new_item(subject, topic, question, answer, user_id, qtype="short", keywords=None):
    kw = json.dumps(keywords, ensure_ascii=False) if keywords else None
    now = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        if HAS_REVIEW_COUNT:
            conn.execute(
                """INSERT INTO study_items
                   (subject, topic, retention, last_date, question, answer,
                    stability, qtype, keywords, user_id, review_count)
                   VALUES (?, ?, 100, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (subject, topic, now, question, answer, DEFAULT_STABILITY, qtype, kw, user_id),
            )
        else:
            conn.execute(
                """INSERT INTO study_items
                   (subject, topic, retention, last_date, question, answer,
                    stability, qtype, keywords, user_id)
                   VALUES (?, ?, 100, ?, ?, ?, ?, ?, ?, ?)""",
                (subject, topic, now, question, answer, DEFAULT_STABILITY, qtype, kw, user_id),
            )
        conn.commit()
    invalidate_caches()


def delete_item_in_db(item_id, user_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM study_items WHERE id = ? AND user_id = ?", (item_id, user_id))
        conn.commit()
    invalidate_caches()


def delete_subject_in_db(subject_name, user_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM study_items WHERE subject = ? AND user_id = ?",
                     (subject_name, user_id))
        conn.commit()
    invalidate_caches()


def rename_subject_in_db(old_name, new_name, user_id):
    """과목 이름 변경/병합 — 내 카드 중 old_name을 new_name으로 옮긴다.
    new_name이 이미 있으면 자연히 같은 덱으로 합쳐진다. 옮긴 카드 수를 반환."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE study_items SET subject = ? WHERE subject = ? AND user_id = ?",
            (new_name, old_name, user_id),
        )
        conn.commit()
        moved = cur.rowcount
    invalidate_caches()
    return moved


def count_all_items(user_id):
    try:
        with get_conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM study_items WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
    except Exception:
        return None


# ============================================================
# 5. PDF 파서 — ④ 한글 인코딩 / 빈 페이지 방어
# ============================================================
def extract_text_from_pdf(uploaded_file):
    if not PYPDF2_OK:
        return "[안내] PyPDF2가 설치되어 있지 않습니다. requirements.txt에 PyPDF2를 추가하세요."
    try:
        reader = PyPDF2.PdfReader(uploaded_file)
        parts = [page.extract_text() or "" for page in reader.pages]  # None 방어
        text = "\n".join(parts).strip()
        return text or "[안내] 텍스트를 추출하지 못했습니다(스캔 이미지 PDF일 수 있음)."
    except Exception as e:
        return f"[PDF 읽기 오류] {e}"


# ============================================================
# 5-B. AI 문제 출제 브릿지 — 프롬프트 생성 + 결과(JSON) 가져오기
#      앱이 LLM을 직접 호출하지 않고, 외부 AI에게 줄 '주문서'를 만들어 주고
#      그 결과(JSON)를 받아 카드로 등록한다(비용 0, 품질↑, 형식 강제로 안정).
# ============================================================
DOC_TYPES = ["교과서", "강의 필기", "논문/아티클", "요약 정리본", "기타"]
DIFFICULTIES = ["쉬움", "보통", "어려움"]
# 난이도 → 블룸 분류(Anderson & Krathwohl, 2001) 인지 수준별 실제 출제 지시
DIFFICULTY_BLOOM = {
    "쉬움": "기억·이해 수준. 핵심 용어를 정의하거나 개념을 그대로 설명하게 하는 문제 위주로 출제하라.",
    "보통": "적용·분석 수준. 개념을 구체적 사례·상황에 적용하거나, 둘 이상을 비교·구분하게 하는 문제로 출제하라.",
    "어려움": "분석·평가 수준. 근거를 따지게 하거나, 사례를 비판적으로 판단하거나, 반례·예외 상황을 다루게 하는 문제로 출제하라.",
}


def build_problem_prompt(subject, doc_type, difficulty, n_short, n_essay, goal=""):
    """외부 AI(ChatGPT/Gemini 등)에 그대로 붙여넣을 한국어 출제 프롬프트를 생성.
    프롬프트 엔지니어링 26원칙(Bsharat et al., 2023)과 블룸 분류를 반영한 범용 양식.
    어떤 학문 분야든 통용되며, 자료는 붙여넣기/파일첨부 어느 쪽이든 동작한다."""
    goal_text = goal.strip() if goal and goal.strip() else f"{subject}의 핵심 내용을 빠짐없이 복습한다."
    bloom = DIFFICULTY_BLOOM.get(difficulty, DIFFICULTY_BLOOM["보통"])
    schema = (
        '```json\n'
        '[\n'
        '  {"type":"short","subject":"…","topic":"세부주제","question":"…은 무엇인가?","answer":"짧은 정답(한 단어~한 문장)"},\n'
        '  {"type":"essay","subject":"…","topic":"세부주제","question":"…을 설명하시오","answer":"모범답안","keywords":["핵심어1","핵심어2","핵심어3"]}\n'
        ']\n'
        '```'
    )
    return f"""###지시###
당신은 모든 학문 분야에 능숙한 학습 문제 출제 전문가입니다. 당신의 임무는 아래 ###학습자료###를 바탕으로 ###학습목표###에 부합하는 복습 문제를 만드는 것입니다. 아래 규칙을 반드시 지키세요.

###학습목표###
{goal_text}

###출제조건###
- 과목/주제: {subject}
- 자료 종류: {doc_type}
- 난이도: {difficulty} — {bloom}
- 출제 수량: 단답형 {n_short}개, 서술형 {n_essay}개
- 학습목표에 암기·이해·적용 등 방향이 드러나면, 그 방향에 맞춰 문제의 초점을 잡아라.
- 단답형(short): 한 가지 핵심 개념을 묻는 질문으로 만들고, 답은 한 단어~한 문장으로 짧게 하라. 빈칸 뚫기 형식은 쓰지 마라.
- 서술형(essay): 이유·원리·관계를 설명하게 하고, 자가채점에 쓸 핵심어 3~6개를 keywords로 함께 제시하라.
- subject 값은 모두 "{subject}" 로 통일하고, topic에는 각 문제의 세부 주제를 적어라.
- 한국어로 작성하라.

###출력형식###
아래 JSON 배열 형식 그대로만 답하라. 반드시 ```json 코드블록 안에 넣고, 코드블록 밖에는 어떤 설명도 쓰지 마라.
{schema}

###학습자료###
아래에 붙여넣은 내용, 또는 이 대화에 첨부한 파일을 학습자료로 사용하라.
(여기에 공부할 내용을 붙여넣거나, 파일을 첨부하세요.)

이제 위 형식의 JSON만 출력하라.
```json
"""


def parse_problem_json(raw_text):
    """외부 AI가 돌려준 텍스트에서 JSON 배열을 추출·검증하여 카드 목록으로 변환.
    반환: (cards:list, error:str|None)."""
    if not raw_text or not raw_text.strip():
        return [], "붙여넣은 내용이 없습니다."

    # 1) ```json ... ``` 코드블록이 있으면 그 안만 사용
    m = re.search(r"```(?:json)?\s*(.*?)```", raw_text, re.DOTALL)
    payload = m.group(1).strip() if m else raw_text.strip()

    # 2) 혹시 앞뒤 잡텍스트가 있으면 첫 '[' ~ 마지막 ']' 만 시도
    if not payload.startswith("["):
        s, e = payload.find("["), payload.rfind("]")
        if s != -1 and e != -1 and e > s:
            payload = payload[s:e + 1]

    try:
        data = json.loads(payload)
    except Exception:
        return [], "JSON 형식을 읽지 못했습니다. AI 답변의 ```json … ``` 부분만 정확히 복사해 붙여넣어 주세요."

    if not isinstance(data, list):
        return [], "JSON 최상위가 목록([ ])이 아닙니다. 형식을 확인해 주세요."

    cards, skipped = [], 0
    for item in data:
        if not isinstance(item, dict):
            skipped += 1
            continue
        qtype = str(item.get("type", "")).lower()
        if qtype == "blank":            # 과거 형식 호환: 빈칸형은 단답형으로 흡수
            qtype = "short"
        subject = (item.get("subject") or "").strip()
        topic = (item.get("topic") or "").strip()
        question = (item.get("question") or "").strip()
        answer = (item.get("answer") or "").strip()
        if qtype not in ("short", "essay") or not (subject and question and answer):
            skipped += 1
            continue
        keywords = item.get("keywords") if qtype == "essay" else None
        if keywords is not None and not isinstance(keywords, list):
            keywords = [str(keywords)]
        cards.append({
            "type": qtype, "subject": subject, "topic": topic or subject,
            "question": question, "answer": answer, "keywords": keywords,
        })

    if not cards:
        return [], "유효한 문제를 찾지 못했습니다. 형식을 확인해 주세요."
    return cards, (f"{skipped}개 항목은 형식이 맞지 않아 제외했습니다." if skipped else None)


# ============================================================
# 6. 로그인 게이트 — 인증되지 않으면 로그인/회원가입 화면에서 멈춘다
# ============================================================
def render_login_gate():
    st.markdown('<h1 class="header-title">🧠 MEMORIA</h1>', unsafe_allow_html=True)
    st.caption("개인별 망각곡선 학습 관리 시스템 — 로그인 후 내 카드만 보입니다.")
    st.markdown("<br>", unsafe_allow_html=True)

    tab_login, tab_signup = st.tabs(["🔑 로그인", "📝 회원가입"])

    with tab_login:
        with st.form("login_form"):
            lu = st.text_input("아이디")
            lp = st.text_input("비밀번호", type="password")
            if st.form_submit_button("로그인", use_container_width=True):
                uid = authenticate(lu, lp)
                if uid:
                    st.session_state["auth_user_id"] = uid
                    st.session_state["auth_username"] = lu.strip()
                    st.rerun()
                else:
                    st.error("아이디 또는 비밀번호가 올바르지 않습니다.")

    with tab_signup:
        with st.form("signup_form"):
            su = st.text_input("아이디 (3자 이상)")
            sp1 = st.text_input("비밀번호 (4자 이상)", type="password")
            sp2 = st.text_input("비밀번호 확인", type="password")
            if st.form_submit_button("회원가입", use_container_width=True):
                if sp1 != sp2:
                    st.error("두 비밀번호가 일치하지 않습니다.")
                else:
                    uid, err = create_user(su, sp1)
                    if err:
                        st.error("⚠️ " + err)
                    else:
                        st.session_state["auth_user_id"] = uid
                        st.session_state["auth_username"] = su.strip()
                        st.success("✅ 가입 완료! 잠시 후 이동합니다.")
                        st.rerun()


if not current_user_id():
    render_login_gate()
    st.stop()

USER_ID = current_user_id()


# ============================================================
# 7. 제어 패널 (사이드바)
# ============================================================
with st.sidebar:
    st.markdown(f"👤 **{st.session_state.get('auth_username', '사용자')}** 님")
    if st.button("로그아웃", use_container_width=True):
        st.session_state.pop("auth_user_id", None)
        st.session_state.pop("auth_username", None)
        st.session_state.pop("review_session", None)
        st.rerun()
    st.markdown("---")
    st.markdown('<h2 style="color:#0061FF;">⚙️ 개인 맞춤 설정</h2>', unsafe_allow_html=True)
    today = datetime.now().date()
    date_range = st.date_input(
        "📅 데이터 조회 기간 선택",
        value=(today - timedelta(days=30), today),
        key="date_filter",
    )

    st.markdown("---")
    st.markdown("##### 📚 복습 대상 과목 선택")
    db_subjects = get_unique_subjects(USER_ID)
    selected_subs = []
    if not db_subjects:
        st.caption("등록된 과목이 없습니다. 우측 '새 학습 카드' 탭에서 먼저 생성해 주세요.")
    else:
        for sub in db_subjects:
            if st.checkbox(sub, value=True, key=f"chk_{sub}"):
                selected_subs.append(sub)

    st.markdown("---")
    with st.expander("🩺 시스템 자가점검 (Self-Check)"):
        total_cards = count_all_items(USER_ID)
        checks = [
            ("DB 연결 정상", total_cards is not None),
            ("학습 데이터 존재", bool(total_cards)),
            ("등록된 과목 1개 이상", len(db_subjects) > 0),
            ("PDF 파서 사용 가능", PYPDF2_OK),
            ("조회 기간 정상 선택", isinstance(date_range, tuple) and len(date_range) == 2),
            ("UTF-8 인코딩", sys.getdefaultencoding().lower() == "utf-8"),
        ]
        for label, ok in checks:
            st.write(("✅ " if ok else "⚠️ ") + label)
        st.caption(f"총 학습 카드: {total_cards if total_cards is not None else '확인 불가'}개")

    with st.expander("ℹ️ 시스템 소개"):
        st.caption(
            "MEMORIA는 에빙하우스 망각곡선 R = 100·e^(-t/S)를 기반으로 "
            "기억 유지율을 시간에 따라 실시간 계산하고, 잊혀갈 카드를 자동으로 "
            "상단에 띄워 복습을 유도하는 학습 리마인더입니다. 데이터는 "
            "계정별로 분리되어 클라우드 DB(또는 로컬)에 영구 저장됩니다."
        )


# ============================================================
# 8. 메인 화면
# ============================================================
st.markdown('<h1 class="header-title">🧠 MEMORIA</h1>', unsafe_allow_html=True)
st.caption("망각곡선 기반 데이터베이스 자동 연동 학습 관리 시스템")
st.markdown("<br>", unsafe_allow_html=True)

if isinstance(date_range, tuple) and len(date_range) == 2:
    start_d, end_d = date_range
else:
    st.info("📅 조회 기간의 **시작일과 종료일**을 모두 선택하면 데이터가 표시됩니다.")
    start_d, end_d = today - timedelta(days=30), today

filtered_df = query_items_from_db(USER_ID, selected_subs, start_d, end_d)

tab1, tab2, tab3, tab4 = st.tabs(
    ["📊 학습 통계", "📝 집중 복습 룸", "➕ 새 학습 카드 및 과목 관리", "🤖 AI 문제 출제"]
)

# ===== 탭 1: 통계 + 망각곡선 시각화 =====
with tab1:
    k1, k2, k3 = st.columns(3)
    k1.metric("📚 누적 학습 데이터", f"{len(filtered_df)}개")
    avg_ret = int(filtered_df["retention"].mean()) if not filtered_df.empty else 0
    k2.metric("📈 현재 평균 기억 유지율", f"{avg_ret}%")
    risk_cnt = len(filtered_df[filtered_df["retention"] < 30]) if not filtered_df.empty else 0
    k3.metric("🚨 즉시 복습 위험 항목", f"{risk_cnt}개")

    if not filtered_df.empty:
        fig = px.bar(
            filtered_df, x="topic", y="retention", color="subject",
            labels={"topic": "학습 개념(토픽)", "retention": "기억 유지율 (%)", "subject": "과목 분류"},
            title="🎯 학습 개념별 현재 기억 유지율 (망각곡선 실시간 반영)",
        )
        fig.update_yaxes(range=[0, 100])
        fig.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True)

        # ── 망각곡선 예측: 지금 복습하지 않으면 앞으로 30일간 어떻게 잊혀가는가 ──
        st.markdown("##### 📉 향후 30일 예상 기억 유지율 (복습하지 않을 경우)")
        days = np.arange(0, 31)
        elapsed = filtered_df["elapsed_days"].to_numpy().astype(float)
        S = filtered_df["stability"].astype(float).clip(lower=0.1).to_numpy()
        # 각 미래 시점 d에 대한 카드 평균 유지율
        proj = [float(np.mean(100.0 * np.exp(-(elapsed + d) / S))) for d in days]
        proj_df = pd.DataFrame({"경과일": days, "예상 평균 유지율(%)": proj})
        fig2 = px.line(proj_df, x="경과일", y="예상 평균 유지율(%)", markers=False)
        fig2.add_hline(y=50, line_dash="dash", line_color="#E0584E",
                       annotation_text="복습 권장선 50%", annotation_position="bottom right")
        fig2.update_yaxes(range=[0, 100])
        fig2.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                           margin=dict(t=10))
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("조회 조건에 맞는 데이터가 없습니다. 오른쪽 탭에서 과목과 카드를 추가해 보세요.")

# ===== 탭 2: 복습 및 카드 삭제 =====
with tab2:
    session = st.session_state.get("review_session")

    # ─────────────────────────────────────────────────────────
    # (A) 복습 세션 진행 화면 — 한 장씩 집중해서 풀기
    # ─────────────────────────────────────────────────────────
    if session and session.get("queue"):
        queue = session["queue"]
        pos = session["pos"]
        total = len(queue)

        # 1) 세션 완료 화면
        if pos >= total:
            s = session["stats"]
            st.markdown("### 🎉 복습 완료!")
            st.caption(f"'{session['label']}' 세션에서 {total}장을 모두 복습했습니다.")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("🟢 맞춤", s["correct"])
            m2.metric("🟡 일부 맞춤", s["partial"])
            m3.metric("🟠 헷갈림", s["vague"])
            m4.metric("🔴 못 맞춤", s["wrong"])
            if not session.get("celebrated"):
                st.balloons()
                session["celebrated"] = True
            if st.button("🏠 복습 허브로 돌아가기", type="primary", use_container_width=True):
                st.session_state.pop("review_session", None)
                st.rerun()

        # 2) 카드 풀이 화면
        else:
            card = queue[pos]
            top_l, top_r = st.columns([6, 1])
            top_l.progress((pos) / total, text=f"{pos + 1} / {total}  ·  {session['label']}")
            if top_r.button("⏹ 끝내기", use_container_width=True):
                st.session_state.pop("review_session", None)
                st.rerun()

            qtype = card.get("qtype") or "short"
            type_label = "📝 서술형" if qtype == "essay" else "✏️ 단답형"
            ret = int(card.get("retention", 0))
            ret_icon = "🔴" if ret < RISK_THRESHOLD else "🟡" if ret < DUE_THRESHOLD else "🟢"
            with st.container(border=True):
                st.markdown(
                    f"`{card['subject']}`  ·  {type_label}  ·  "
                    f"{ret_icon} 유지율 {ret}% · {card.get('elapsed_days', 0)}일 경과"
                )
                st.markdown(f"#### {card['question']}")

                if not session["revealed"]:
                    if st.button("🔄 정답 확인", type="primary", use_container_width=True):
                        session["revealed"] = True
                        st.rerun()
                else:
                    st.divider()
                    st.markdown(
                        f"<div style='color:#0061FF;font-size:1.2rem;font-weight:700;'>"
                        f"{card['answer']}</div>", unsafe_allow_html=True)
                    kw_list = parse_keywords(card.get("keywords"))
                    if qtype == "essay" and kw_list:
                        chips = "  ".join(f"`{k}`" for k in kw_list)
                        st.caption(f"🔑 채점 핵심어 — 내 답에 담겼는지 확인: {chips}")

            # 정답 공개 후에만 채점 — 각 버튼에 '다음 복습까지' 예상일 표시
            if session["revealed"]:
                st.markdown("##### ✍️ 스스로 채점 — 다음 복습 시점이 자동 계산됩니다")
                old_S = float(card.get("stability") or DEFAULT_STABILITY)
                gcols = st.columns(len(GRADE_OPTIONS))
                for gcol, (code, label, icon) in zip(gcols, GRADE_OPTIONS):
                    nd = next_interval_days(old_S, code)
                    if gcol.button(f"{icon} {label}\n\n약 {nd}일 후", key=f"sess_{code}",
                                   use_container_width=True):
                        grade_item(card["id"], code, USER_ID)
                        session["stats"][code] += 1
                        session["pos"] += 1
                        session["revealed"] = False
                        st.rerun()

    # ─────────────────────────────────────────────────────────
    # (B) 복습 허브 — 과목(덱)별로 묶어 한눈에 보기
    # ─────────────────────────────────────────────────────────
    else:
        st.markdown("### 🧠 복습 허브")
        if filtered_df.empty:
            st.info("복습할 카드가 없습니다. '🤖 AI 문제 출제' 또는 '➕ 새 학습 카드' 탭에서 먼저 카드를 만들어 보세요.")
        else:
            due_df, later_df = split_due(filtered_df)

            # ── 지금 복습 (복습일이 된 카드 + 새 카드) ──
            if due_df.empty:
                st.success("🎉 지금 복습할 카드가 없습니다! 모두 기억이 충분히 남아 있어요.")
            else:
                decks = subject_summary(due_df)
                head_l, head_r = st.columns([3, 1])
                head_l.markdown(
                    f"#### 🔴 지금 복습  **{len(due_df)}**장  ·  {len(decks)}과목\n"
                    f"복습일이 됐거나 새로 만든 카드입니다. 유지율이 낮은 것부터 한 장씩 풉니다."
                )
                if head_r.button("▶ 지금 복습 시작", type="primary", use_container_width=True):
                    start_review_session(due_df, "지금 복습")
                    st.rerun()

                st.markdown("##### 📚 과목별 덱")
                cols_per_row = 3
                for i in range(0, len(decks), cols_per_row):
                    cols = st.columns(cols_per_row)
                    for col, d in zip(cols, decks[i:i + cols_per_row]):
                        with col, st.container(border=True):
                            risk_txt = f"🔴 위험 {d['risk']}" if d["risk"] else "🟡 복습"
                            st.markdown(f"**{d['subject']}**　{risk_txt}")
                            st.progress(d["avg"] / 100, text=f"평균 유지율 {d['avg']}% · {d['count']}장")
                            if st.button("복습 시작", key=f"deck_{d['subject']}", use_container_width=True):
                                start_review_session(due_df[due_df["subject"] == d["subject"]], d["subject"])
                                st.rerun()

            # ── 복습 예정 (아직 복습일 전) — 접어 둠 ──
            if not later_df.empty:
                with st.expander(f"⏳ 복습 예정 ({len(later_df)}장) — 아직 복습일 전"):
                    upcoming = later_df.copy()
                    upcoming["남은일"] = upcoming.apply(
                        lambda r: days_until_due(r["stability"], r["elapsed_days"]), axis=1)
                    upcoming = upcoming.sort_values("남은일")
                    view = pd.DataFrame({
                        "과목": upcoming["subject"],
                        "토픽": upcoming["topic"],
                        "현재 유지율": upcoming["retention"].astype(str) + "%",
                        "복습 예정": "약 " + upcoming["남은일"].astype(str) + "일 후",
                    })
                    st.dataframe(view, use_container_width=True, hide_index=True)

            # ── 보조: 카드 관리(목록·삭제) — 공부 흐름과 분리해 접어 둠 ──
            with st.expander("🗂 카드 관리 (목록 보기·삭제)"):
                for _, row in filtered_df.iterrows():
                    c_txt, c_del = st.columns([6, 1])
                    alert_icon = ("🔴" if row["retention"] < RISK_THRESHOLD
                                  else "🟡" if row["retention"] < DUE_THRESHOLD else "🟢")
                    c_txt.write(
                        f"{alert_icon} **[{row['subject']}]** {row['topic']} — "
                        f"유지율 **{row['retention']}%** · {row['elapsed_days']}일 경과"
                    )
                    if c_del.button("🗑️ 삭제", key=f"del_{row['id']}", use_container_width=True):
                        delete_item_in_db(row["id"], USER_ID)
                        st.toast(f"[{row['subject']}] {row['topic']} 카드를 삭제했습니다.", icon="🗑️")
                        st.rerun()

# ===== 탭 3: 카드 등록 및 과목 관리 콘솔 =====
with tab3:
    col_register, col_management = st.columns([1.2, 1])

    with col_register:
        st.markdown("### ✍️ 신규 복습 데이터 등록")
        st.caption("PDF 텍스트를 활용하거나 직접 지식을 요약하여 등록하세요. 새로운 과목명을 적으면 자동으로 과목이 추가됩니다.")

        uploaded_file = st.file_uploader("참조용 PDF 문서 분석하기 (선택사항)", type=["pdf"])
        if uploaded_file is not None:
            with st.spinner("📄 PDF에서 텍스트를 추출하는 중..."):
                extracted_text = extract_text_from_pdf(uploaded_file)
            st.markdown(f'<div class="pdf-box">{extracted_text}</div>', unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)

        with st.form("smart_register_form", clear_on_submit=True):
            st.markdown("##### 📌 분류 및 내용 작성")
            sub_choice = st.selectbox("기존 과목 중에서 선택", ["(새로운 과목명 직접 입력)"] + db_subjects)
            new_sub_input = st.text_input("새로운 과목명 입력 (예: 통계학)", placeholder="위 선택창이 직접 입력일 때만 반영됩니다.")
            topic_input = st.text_input("학습 개념명 (토픽)", placeholder="예: 표본분포와 중심극한정리")
            q_input = st.text_area("질문 또는 본문 내용 (위 PDF에서 복사 가능)")
            a_input = st.text_input("가려질 핵심 정답")
            submit_btn = st.form_submit_button("지식 저장 및 자동 문제 변환 🪄")

            if submit_btn:
                final_subject = (
                    new_sub_input.strip()
                    if sub_choice == "(새로운 과목명 직접 입력)"
                    else sub_choice
                )
                missing = []
                if not final_subject or final_subject == "(새로운 과목명 직접 입력)":
                    missing.append("과목명")
                if not topic_input.strip():
                    missing.append("학습 개념명")
                if not q_input.strip():
                    missing.append("질문/본문")
                if not a_input.strip():
                    missing.append("정답")

                if missing:
                    st.error("⚠️ 다음 항목을 입력해 주세요: " + ", ".join(missing))
                else:
                    processed_q = (
                        q_input.replace(a_input, "  [ ________ ]  ")
                        if a_input in q_input
                        else q_input
                    )
                    with st.spinner("💾 데이터베이스에 저장하는 중..."):
                        insert_new_item(final_subject, topic_input, processed_q, a_input, USER_ID)
                    st.toast(f"[{final_subject}] 카드가 저장되었습니다!", icon="🪄")
                    st.success(f"✅ [{final_subject}] 과목에 데이터가 저장되었습니다. (초기 유지율 100%에서 망각 시작)")
                    st.rerun()

    with col_management:
        st.markdown("### 🧹 과목 이름 변경 · 병합")
        st.caption("AI 출제 등으로 비슷한 과목명(예: '심리학'과 '심리학개론')이 갈라졌을 때 하나로 합칠 수 있습니다.")
        if not db_subjects:
            st.info("정리할 과목이 아직 없습니다.")
        else:
            with st.form("subject_rename_form"):
                src_sub = st.selectbox("바꿀 과목 (원본)", db_subjects, key="rename_src")
                dst_sub = st.text_input("새 과목명 (기존 과목명을 적으면 그 덱으로 병합)",
                                        placeholder="예: 심리학")
                rename_btn = st.form_submit_button("과목 이름 변경 / 병합 🔀")
                if rename_btn:
                    new_name = dst_sub.strip()
                    if not new_name:
                        st.error("⚠️ 새 과목명을 입력해 주세요.")
                    elif new_name == src_sub:
                        st.info("원본과 동일한 이름입니다. 변경할 내용이 없습니다.")
                    else:
                        merging = new_name in db_subjects
                        moved = rename_subject_in_db(src_sub, new_name, USER_ID)
                        verb = "병합" if merging else "이름 변경"
                        st.toast(f"[{src_sub}] → [{new_name}] {verb} 완료 ({moved}장)", icon="🔀")
                        st.success(f"✅ {moved}장을 [{new_name}] 과목으로 {verb}했습니다.")
                        st.rerun()

        st.divider()
        st.markdown("### 🗑️ 시스템 과목 일괄 삭제 관리")
        st.caption("과목을 삭제하면 해당 과목에 포함된 모든 복습 카드가 데이터베이스에서 영구 소멸됩니다.")

        if not db_subjects:
            st.info("현재 정리할 과목이 시스템에 존재하지 않습니다.")
        else:
            with st.form("subject_clean_form"):
                target_del_sub = st.selectbox("삭제할 시스템 과목 선택 (예: 물리학)", db_subjects)
                st.warning(f"⚠️ 경고: [{target_del_sub}] 과목 삭제 시 하위 모든 데이터가 함께 지워집니다.")
                confirm_check = st.checkbox("위 일괄 삭제 위험 내용을 확인했으며 이에 동의합니다.")
                del_sub_btn = st.form_submit_button("선택한 과목 시스템에서 영구 삭제 ❌")

                if del_sub_btn:
                    if confirm_check:
                        with st.spinner("🧹 과목과 하위 데이터를 정리하는 중..."):
                            delete_subject_in_db(target_del_sub, USER_ID)
                        st.toast(f"[{target_del_sub}] 과목을 일괄 삭제했습니다.", icon="💥")
                        st.success(f"💥 [{target_del_sub}] 과목과 하위 모든 데이터가 완전히 격리 삭제되었습니다.")
                        st.rerun()
                    else:
                        st.error("⚠️ 삭제를 확정하시려면 동의 체크박스에 체크해 주셔야 합니다.")

# ===== 탭 4: AI 문제 출제 (프롬프트 생성 → 외부 AI → 결과 가져오기) =====
with tab4:
    st.markdown("### 🤖 외부 AI로 문제 만들기")
    st.caption(
        "이 앱은 AI를 직접 부르지 않습니다. 대신 'AI에게 줄 주문서(프롬프트)'를 만들어 드립니다. "
        "그 주문서를 ChatGPT·Gemini 등에 붙여넣고 학습 자료를 함께 주면, AI가 정해진 형식으로 "
        "문제를 만들어 줍니다. 그 결과를 아래 2단계에 붙여넣으면 학습 카드로 자동 등록됩니다."
    )

    step1, step2 = st.columns(2)

    # ── 1단계: 주문서(프롬프트) 만들기 ──
    with step1:
        st.markdown("#### 1단계 · 주문서 만들기")
        p_subject = st.text_input("과목/주제", placeholder="예: 통계학 / 심리학 / 헌법", key="gen_subject")

        p_goal = st.text_area(
            "학습 목표 (무엇을·어떤 수준으로·어떤 용도로)",
            placeholder="예: 회귀분석 단원의 핵심 가정과 계수 해석을 시험 대비로 정확히 암기했는지 확인. 학부 2학년 수준.",
            height=90, key="gen_goal",
        )
        with st.expander("✍️ 학습 목표 잘 쓰는 법 (예시 보기)"):
            st.markdown(
                "**좋은 목표 = 무엇을 + 어떤 수준으로 + 어떤 용도로** 를 한두 문장으로.\n"
                "목표가 구체적일수록 문제의 방향(암기/이해/활용)이 정확해집니다.\n\n"
                "- ❌ 너무 모호: *\"통계학 문제 내줘\"*\n"
                "- ✅ 암기 중심: *\"신경전달물질의 종류와 기능을 시험 대비로 **정확히 암기**했는지 확인. 학부 1학년 수준.\"*\n"
                "- ✅ 이해 중심: *\"방어기제 개념이 **왜·어떻게** 작동하는지 원리를 설명할 수 있는지 점검.\"*\n"
                "- ✅ 활용 중심: *\"운동량 보존 법칙을 실제 충돌 상황에 **적용해 계산**하는 연습 위주.\"*\n\n"
                "*(비워 두면 '핵심 내용 전반 복습'으로 자동 설정됩니다.)*"
            )

        doc_choice = st.selectbox("자료 종류", DOC_TYPES + ["✏️ 직접 입력"], key="gen_doctype")
        if doc_choice == "✏️ 직접 입력":
            p_doctype = st.text_input(
                "자료 종류 직접 입력", placeholder="예: 판례, 실험 매뉴얼, 문제집 해설, 코드 주석",
                key="gen_doctype_custom",
            ).strip() or "기타"
        else:
            p_doctype = doc_choice
        p_diff = st.selectbox("난이도", DIFFICULTIES, index=1, key="gen_diff",
                              help="쉬움=정의·설명 / 보통=적용·비교 / 어려움=분석·판단 (블룸 분류 기준)")
        cc1, cc2 = st.columns(2)
        p_short = cc1.number_input("단답형 개수", min_value=0, max_value=30, value=5, key="gen_short")
        p_essay = cc2.number_input("서술형 개수", min_value=0, max_value=30, value=3, key="gen_essay")

        if st.button("📝 주문서 생성", use_container_width=True):
            if not p_subject.strip():
                st.error("⚠️ 과목/주제를 입력해 주세요.")
            elif p_short + p_essay == 0:
                st.error("⚠️ 단답형과 서술형 중 최소 1개는 요청해야 합니다.")
            else:
                st.session_state["gen_prompt"] = build_problem_prompt(
                    p_subject.strip(), p_doctype, p_diff, int(p_short), int(p_essay), p_goal
                )

        if st.session_state.get("gen_prompt"):
            st.markdown("**아래 박스 우측 상단 복사 아이콘을 눌러 전체 복사하세요.**")
            st.code(st.session_state["gen_prompt"], language="text")
            st.info("복사한 주문서를 ChatGPT·Gemini에 붙여넣은 뒤, 공부할 자료를 "
                    "**붙여넣거나 파일(PDF 등)로 첨부**하세요. 둘 중 어느 방식이든 동작합니다.")

    # ── 2단계: AI 결과 붙여넣어 가져오기 ──
    with step2:
        st.markdown("#### 2단계 · 결과 가져오기")
        st.caption("AI가 만들어 준 답변(```json … ``` 포함 전체)을 그대로 붙여넣으세요.")
        raw = st.text_area("AI 응답 붙여넣기", height=240, key="import_raw",
                           placeholder='```json\n[ {"type":"short", ... } ]\n```')

        if st.button("🔍 문제 불러오기 (미리보기)", use_container_width=True):
            cards, msg = parse_problem_json(raw)
            if not cards:
                st.session_state.pop("import_cards", None)
                st.error(f"❌ {msg}")
            else:
                st.session_state["import_cards"] = cards
                if msg:
                    st.warning("⚠️ " + msg)

        cards = st.session_state.get("import_cards")
        if cards:
            n_short = sum(1 for c in cards if c["type"] == "short")
            n_essay = sum(1 for c in cards if c["type"] == "essay")
            st.success(f"✅ 총 {len(cards)}문제 인식 (단답 {n_short} · 서술형 {n_essay}).")

            # 과목 통일 — 모든 카드를 한 과목(덱)으로 저장해 그룹이 쪼개지지 않게 함
            default_subject = (st.session_state.get("gen_subject") or cards[0]["subject"] or "").strip()
            save_subject = st.text_input(
                "저장할 과목 (모든 문제를 이 과목 덱으로 묶어 저장)",
                value=default_subject, key="import_subject",
            )
            st.caption("표에서 내용을 **직접 수정**하거나 '저장' 체크를 풀어 제외할 수 있습니다. "
                       "과목은 위 칸으로 일괄 지정되어 같은 덱에 모입니다. 서술형 핵심어는 쉼표(,)로 구분합니다.")
            edit_df = pd.DataFrame([
                {"저장": True,
                 "유형": "서술형" if c["type"] == "essay" else "단답형",
                 "토픽": c["topic"], "문제": c["question"], "정답": c["answer"],
                 "핵심어": ", ".join(parse_keywords(c.get("keywords")))}
                for c in cards
            ])
            edited = st.data_editor(
                edit_df, hide_index=True, use_container_width=True, key="import_editor",
                column_config={
                    "저장": st.column_config.CheckboxColumn("저장", width="small"),
                    "유형": st.column_config.SelectboxColumn("유형", options=["단답형", "서술형"], width="small"),
                    "토픽": st.column_config.TextColumn("토픽"),
                    "문제": st.column_config.TextColumn("문제", width="large"),
                    "정답": st.column_config.TextColumn("정답", width="medium"),
                    "핵심어": st.column_config.TextColumn("핵심어(서술형, 쉼표구분)", width="medium"),
                },
            )

            if st.button("💾 선택한 문제 저장", type="primary", use_container_width=True):
                subject = save_subject.strip()
                if not subject:
                    st.error("⚠️ '저장할 과목'을 입력해 주세요.")
                    st.stop()
                inserted = dup = invalid = 0
                with st.spinner("학습 카드로 등록하는 중..."):
                    for _, r in edited.iterrows():
                        if not r["저장"]:
                            continue
                        question = str(r["문제"]).strip()
                        answer = str(r["정답"]).strip()
                        if not (question and answer):
                            invalid += 1
                            continue
                        if item_exists(subject, question, USER_ID):   # 중복 방지
                            dup += 1
                            continue
                        qtype = "essay" if r["유형"] == "서술형" else "short"
                        kws = [k.strip() for k in str(r["핵심어"]).split(",") if k.strip()]
                        insert_new_item(subject, str(r["토픽"]).strip() or subject,
                                        question, answer, USER_ID, qtype=qtype,
                                        keywords=kws if (qtype == "essay" and kws) else None)
                        inserted += 1
                if inserted:
                    st.session_state.pop("import_cards", None)
                    st.toast(f"{inserted}개 문제를 등록했습니다!", icon="🤖")
                    notes = []
                    if dup:
                        notes.append(f"이미 있는 문제 {dup}개 건너뜀")
                    if invalid:
                        notes.append(f"내용이 빈 {invalid}개 제외")
                    tail = f" ({', '.join(notes)})" if notes else ""
                    st.success(f"✅ {inserted}개 저장 완료{tail}. 초기 유지율 100%에서 망각이 시작됩니다.")
                    st.rerun()
                else:
                    msg = "저장된 문제가 없습니다."
                    if dup:
                        msg += f" (이미 있는 문제 {dup}개)"
                    if invalid:
                        msg += f" (빈 내용 {invalid}개)"
                    st.warning("⚠️ " + msg)
