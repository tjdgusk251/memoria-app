import streamlit as st
import numpy as np
import pandas as pd
import plotly.express as px
import sqlite3
import PyPDF2
from datetime import datetime, timedelta

# 1. 서비스 환경 설정
st.set_page_config(page_title="MEMORIA - 스마트 복습 시스템", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght=400;600;700&display=swap');
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
}
</style>
""", unsafe_allow_html=True)

DB_FILE = "memoria.db"


# 2. 데이터베이스 인프라 초기화
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
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
    conn.commit()
    conn.close()


init_db()


# 3. 백엔드 핵심 비즈니스 로직
def get_unique_subjects():
    """DB에 저장된 과목들만 실시간 조회"""
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT DISTINCT subject FROM study_items ORDER BY subject ASC", conn)
    conn.close()
    return df['subject'].tolist() if not df.empty else []


def query_items_from_db(subjects, start_date, end_date, min_ret):
    conn = sqlite3.connect(DB_FILE)
    if not subjects:
        return pd.DataFrame(columns=["id", "subject", "topic", "retention", "last_date", "question", "answer"])

    placeholders = ",".join("?" for _ in subjects)
    query = f"""
        SELECT id, subject, topic, retention, last_date, question, answer 
        FROM study_items 
        WHERE subject IN ({placeholders}) 
          AND date(last_date) >= date(?) 
          AND date(last_date) <= date(?) 
          AND retention >= ?
        ORDER BY retention ASC
    """
    params = list(subjects) + [start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"), min_ret]
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


def update_retention_in_db(item_id, current_retention, score_type):
    new_ret = current_retention
    if score_type == 'again':
        new_ret = max(0, current_retention - 20)
    elif score_type == 'hard':
        new_ret = max(0, current_retention - 5)
    elif score_type == 'good':
        new_ret = min(100, current_retention + 15)
    elif score_type == 'easy':
        new_ret = min(100, current_retention + 30)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE study_items SET retention = ?, last_date = ? WHERE id = ?",
                   (new_ret, datetime.now().strftime("%Y-%m-%d"), item_id))
    conn.commit()
    conn.close()


def insert_new_item(subject, topic, question, answer):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO study_items (subject, topic, retention, last_date, question, answer)
        VALUES (?, ?, 50, ?, ?, ?)
    """, (subject, topic, datetime.now().strftime("%Y-%m-%d"), question, answer))
    conn.commit()
    conn.close()


def delete_item_in_db(item_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM study_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


def delete_subject_in_db(subject_name):
    """지정한 과목과 하위 모든 데이터 일괄 삭제"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM study_items WHERE subject = ?", (subject_name,))
    conn.commit()
    conn.close()


# 4. 복습 모달 윈도우
@st.dialog("📖 몰입형 복습 대화방")
def show_detail_modal(item):
    st.write(f"### {item['topic']}")
    st.caption(f"학습 과목: {item['subject']} | 현재 기억 유지율: {item['retention']}%")
    st.progress(int(item['retention']) / 100)
    st.divider()

    st.markdown(f"**❓ 질문 및 내용**\n\n{item['question']}")
    with st.expander("💡 가려진 정답 / 모범 답안 확인하기"):
        st.markdown(f"<h4 style='color:#0061FF;'>{item['answer']}</h4>", unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("##### 체감 난이도 평가")
    c1, c2, c3, c4 = st.columns(4)

    if c1.button("🔴 위험", use_container_width=True):
        update_retention_in_db(item['id'], item['retention'], 'again')
        st.rerun()
    if c2.button("🟠 어려움", use_container_width=True):
        update_retention_in_db(item['id'], item['retention'], 'hard')
        st.rerun()
    if c3.button("🟢 적당함", use_container_width=True):
        update_retention_in_db(item['id'], item['retention'], 'good')
        st.rerun()
    if c4.button("🔵 쉬움", use_container_width=True):
        update_retention_in_db(item['id'], item['retention'], 'easy')
        st.rerun()


# PDF 파서
def extract_text_from_pdf(uploaded_file):
    try:
        reader = PyPDF2.PdfReader(uploaded_file)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text
    except Exception as e:
        return f"PDF 읽기 오류 발생: {e}"


# ========== 제어 패널 (사이드바) ==========
with st.sidebar:
    st.markdown('<h2 style="color:#0061FF;">⚙️ 개인 맞춤 설정</h2>', unsafe_allow_html=True)
    today = datetime.now().date()
    date_range = st.date_input("📅 데이터 조회 기간 선택", value=(today - timedelta(days=30), today), key="date_filter")

    st.markdown("---")
    st.markdown("##### 📚 복습 대상 과목 선택")

    # DB에 등록된 유니크한 과목 목록 호출
    db_subjects = get_unique_subjects()
    selected_subs = []

    # [변경 완료] 멀티셀렉트를 제거하고 직관적인 체크박스 구조로 전면 교체
    if not db_subjects:
        st.caption("등록된 과목이 없습니다. 우측에서 과목을 먼저 생성해 주세요.")
    else:
        for sub in db_subjects:
            # 기본적으로 모든 체크박스가 선택(True)된 상태로 배치
            if st.checkbox(sub, value=True, key=f"chk_{sub}"):
                selected_subs.append(sub)

    st.markdown("---")
    min_retention = st.slider("🎯 최소 기억 유지율 기준 설정 (%)", 0, 100, 0)

# ========== 메인 화면 인터페이스 ==========
st.markdown('<h1 class="header-title">🧠 MEMORIA</h1>', unsafe_allow_html=True)
st.caption("망각곡선 기반 데이터베이스 자동 연동 학습 관리 시스템")
st.markdown("<br>", unsafe_allow_html=True)

if isinstance(date_range, tuple) and len(date_range) == 2:
    start_d, end_d = date_range
else:
    start_d, end_d = today - timedelta(days=30), today

# 실시간 필터 적용 데이터 추출
filtered_df = query_items_from_db(selected_subs, start_d, end_d, min_retention)

tab1, tab2, tab3 = st.tabs(["📊 학습 통계", "📝 집중 복습 룸", "➕ 새 학습 카드 및 과목 관리"])

# ===== 탭 1: 통계 시각화 =====
with tab1:
    k1, k2, k3 = st.columns(3)
    k1.metric("📚 누적 학습 데이터", f"{len(filtered_df)}개")
    k2.metric("📈 전체 평균 기억 유지율", f"{int(filtered_df['retention'].mean()) if not filtered_df.empty else 0}%")
    k3.metric("🚨 즉시 복습 위험 항목", f"{len(filtered_df[filtered_df['retention'] < 30])}개")

    if not filtered_df.empty:
        fig = px.bar(
            filtered_df, x="topic", y="retention", color="subject",
            labels={"topic": "학습 개념(토픽)", "retention": "기억 유지율 (%)", "subject": "과목 분류"},
            title="🎯 학습 개념별 실시간 기억 유지율 상태"
        )
        fig.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("조회 조건에 맞는 데이터가 없습니다. 오른쪽 탭에서 과목과 카드를 추가해 보세요.")

# ===== 탭 2: 복습 및 카드 삭제 =====
with tab2:
    st.markdown("### 🖱️ 잊혀가는 지식 복습하기")
    if filtered_df.empty:
        st.caption("복습 대기열이 비어 있습니다.")
    else:
        for index, row in filtered_df.iterrows():
            col_text, col_btn_rev, col_btn_del = st.columns([5.5, 1.2, 0.8])
            alert_icon = "🔴" if row['retention'] < 30 else "🟡" if row['retention'] < 60 else "🟢"
            col_text.write(f"{alert_icon} **[{row['subject']}]** {row['topic']} — 현재 유지율: **{row['retention']}%**")

            if col_btn_rev.button("복습하기", key=f"rev_{row['id']}", use_container_width=True):
                show_detail_modal(row)

            if col_btn_del.button("🗑️ 카드 삭제", key=f"del_{row['id']}", use_container_width=True):
                delete_item_in_db(row['id'])
                st.rerun()

# ===== 탭 3: 카드 등록 및 과목 삭제/추가 관리 콘솔 =====
with tab3:
    col_register, col_management = st.columns([1.2, 1])

    # 데이터 등록 및 자동 과목 추가 영역
    with col_register:
        st.markdown("### ✍️ 신규 복습 데이터 등록")
        st.caption("PDF 텍스트를 활용하거나 직접 지식을 요약하여 등록하세요. 새로운 과목명을 적으면 자동으로 과목이 추가됩니다.")

        uploaded_file = st.file_uploader("참조용 PDF 문서 분석하기 (선택사항)", type=["pdf"])
        extracted_text = ""
        if uploaded_file is not None:
            extracted_text = extract_text_from_pdf(uploaded_file)
            st.markdown(f'<div class="pdf-box">{extracted_text}</div>', unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)

        with st.form("smart_register_form", clear_on_submit=True):
            st.markdown("##### 📌 분류 및 내용 작성")

            # [변경 완료] 과목을 선택하거나 새로 타이핑할 수 있는 유연한 구조 보완
            sub_choice = st.selectbox("기존 과목 중에서 선택", ["(새로운 과목명 직접 입력)"] + db_subjects)
            new_sub_input = st.text_input("새로운 과목명 입력 (예: 통계학)", placeholder="위 선택창이 직접 입력일 때만 반영됩니다.")

            topic_input = st.text_input("학습 개념명 (토픽)", placeholder="예: 표본분포와 중심극한정리")
            q_input = st.text_area("질문 또는 본문 내용 (위 PDF에서 복사 가능)")
            a_input = st.text_input("가려질 핵심 정답")

            submit_btn = st.form_submit_button("지식 저장 및 자동 문제 변환 🪄")

            if submit_btn:
                # 최종 과목명 결정 로직
                final_subject = new_sub_input.strip() if sub_choice == "(새로운 과목명 직접 입력)" else sub_choice

                if final_subject and final_subject != "(새로운 과목명 직접 입력)" and topic_input and q_input and a_input:
                    # 빈칸 뚫기 메커니즘 연동
                    if a_input in q_input:
                        processed_q = q_input.replace(a_input, "  [ ________ ]  ")
                    else:
                        processed_q = q_input

                    insert_new_item(final_subject, topic_input, processed_q, a_input)
                    st.success(f"✅ [{final_subject}] 과목에 데이터가 안전하게 저장되었습니다.")
                    st.rerun()
                else:
                    st.error("⚠️ 과목명, 개념명, 질문 및 정답 칸을 빠짐없이 입력해 주세요.")

    # [신설 완료] 과목 단위를 통째로 청소 및 삭제할 수 있는 관리 대화창
    with col_management:
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
                        delete_subject_in_db(target_del_sub)
                        st.success(f"💥 [{target_del_sub}] 과목과 하위 모든 데이터가 완전히 격리 삭제되었습니다.")
                        st.rerun()
                    else:
                        st.error("⚠️ 삭제를 확정하시려면 동의 체크박스에 체크해 주셔야 합니다.")