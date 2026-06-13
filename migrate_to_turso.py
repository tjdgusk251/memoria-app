"""로컬 memoria.db(SQLite) → Turso(libSQL) 1회 이전 스크립트.

사용 전 준비:
  pip install libsql
  그리고 아래 둘 중 하나로 Turso 접속정보를 제공:
   (1) 환경변수 TURSO_DATABASE_URL, TURSO_AUTH_TOKEN 설정, 또는
   (2) .streamlit/secrets.toml 의 [turso] url/token 작성

실행:
  python migrate_to_turso.py

기존 로컬 계정(users)과 카드(study_items)를 id 그대로 클라우드로 복사합니다.
이미 클라우드에 같은 데이터가 있으면(행 존재) 중단하니 안심하고 한 번만 실행하세요.
"""
import os
import sqlite3
import sys

LOCAL_DB = os.environ.get("MEMORIA_DB", "memoria.db")

USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    salt TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""
ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS study_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    topic TEXT NOT NULL,
    retention INTEGER NOT NULL,
    last_date TEXT NOT NULL,
    question TEXT,
    answer TEXT,
    stability REAL,
    qtype TEXT,
    keywords TEXT,
    user_id INTEGER
)"""


def get_creds():
    url = os.environ.get("TURSO_DATABASE_URL")
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if url and token:
        return url, token
    try:
        import tomllib
        with open(".streamlit/secrets.toml", "rb") as f:
            data = tomllib.load(f)
        return data["turso"]["url"], data["turso"]["token"]
    except Exception:
        return None, None


def main():
    url, token = get_creds()
    if not (url and token):
        sys.exit("[중단] Turso 접속정보가 없습니다. 환경변수 또는 .streamlit/secrets.toml을 설정하세요.")
    try:
        import libsql
    except ImportError:
        sys.exit("[중단] libsql 미설치. 먼저 'pip install libsql' 을 실행하세요.")

    if not os.path.exists(LOCAL_DB):
        sys.exit(f"[중단] 로컬 DB를 찾을 수 없습니다: {LOCAL_DB}")

    local = sqlite3.connect(LOCAL_DB)
    users = local.execute(
        "SELECT id, username, salt, pw_hash, created_at FROM users").fetchall()
    cols = [r[1] for r in local.execute("PRAGMA table_info(study_items)").fetchall()]
    items = local.execute(
        "SELECT id, subject, topic, retention, last_date, question, answer, "
        "stability, qtype, keywords, user_id FROM study_items").fetchall()
    local.close()

    remote = libsql.connect(database=url, auth_token=token)
    remote.execute(USERS_DDL)
    remote.execute(ITEMS_DDL)
    remote.commit()

    existing = remote.execute("SELECT COUNT(*) FROM study_items").fetchall()[0][0]
    existing_u = remote.execute("SELECT COUNT(*) FROM users").fetchall()[0][0]
    if existing or existing_u:
        sys.exit(f"[중단] 클라우드에 이미 데이터가 있습니다(users={existing_u}, items={existing}). "
                 "중복 방지를 위해 이전을 건너뜁니다.")

    for u in users:
        remote.execute(
            "INSERT INTO users (id, username, salt, pw_hash, created_at) VALUES (?,?,?,?,?)", u)
    for it in items:
        remote.execute(
            "INSERT INTO study_items (id, subject, topic, retention, last_date, question, "
            "answer, stability, qtype, keywords, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)", it)
    remote.commit()
    print(f"[완료] 사용자 {len(users)}명, 카드 {len(items)}개를 Turso로 이전했습니다.")


if __name__ == "__main__":
    main()
