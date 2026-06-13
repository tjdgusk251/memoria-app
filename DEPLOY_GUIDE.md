# MEMORIA 공개 배포 가이드 (어디서나 접속 + 영구 저장 + 계정별 분리)

배포하면 `https://...streamlit.app` 공개 주소가 생기고, **내 PC를 꺼도** 누구나 접속해 **각자 계정으로** 사용할 수 있습니다. 데이터는 **Turso(클라우드 DB)**에 영구 저장됩니다.

전체 30~40분. 코드/파일은 이미 다 준비돼 있습니다. 아래 ①~⑤만 따라 하세요.

---

## ① Turso 가입 + DB 만들기 (영구 저장소)

1. https://turso.tech 접속 → **Sign up** (GitHub 계정으로 가입 권장, 무료).
2. 대시보드에서 **Create Database** → 이름 예: `memoria`, 가까운 지역(예: Tokyo/`nrt`) 선택.
3. 만든 DB를 열고 **접속 정보 2개**를 복사:
   - **Database URL**: `libsql://memoria-<유저명>.turso.io` 형태
   - **Auth Token**: **Create Token** 버튼 → 생성된 긴 토큰 복사 (한 번만 보이니 잘 보관)

> 이 두 값이 곧 `url`, `token` 입니다.

## ② 기존 카드/계정을 Turso로 1회 이전 (선택이지만 권장)

지금 로컬에 있는 카드를 클라우드로 옮깁니다. (안 하면 클라우드는 빈 상태로 시작)

```bash
pip install libsql
# 윈도우 PowerShell 기준
$env:TURSO_DATABASE_URL="libsql://memoria-...turso.io"
$env:TURSO_AUTH_TOKEN="①에서 복사한 토큰"
python migrate_to_turso.py
```
`[완료] 사용자 N명, 카드 M개를 Turso로 이전했습니다.` 가 나오면 성공.

> 아직 계정을 안 만들었다면, 먼저 로컬 앱에서 회원가입 한 번 하고(기존 12카드가 그 계정에 귀속됨) 이 스크립트를 돌리세요.

## ③ GitHub에 코드 올리기

1. https://github.com 에서 **New repository** → 이름 예: `memoria-app`, **Private** 가능.
2. 이 폴더를 올립니다(터미널):
   ```bash
   git init
   git add .
   git commit -m "MEMORIA 배포"
   git branch -M main
   git remote add origin https://github.com/<내아이디>/memoria-app.git
   git push -u origin main
   ```
   - `.gitignore`가 **secrets.toml·*.db·versions/** 를 자동 제외하므로 비밀·로컬DB는 안 올라갑니다. (안전)

## ④ Streamlit Community Cloud 배포

1. https://share.streamlit.io 접속 → **GitHub로 로그인**.
2. **Create app** → 방금 올린 저장소 선택.
   - **Branch**: `main`
   - **Main file path**: `app_v13.py`
3. 배포 전 **Advanced settings → Secrets** 칸에 아래를 붙여넣기(①의 값으로 교체):
   ```toml
   [turso]
   url = "libsql://memoria-...turso.io"
   token = "①에서 복사한 토큰"
   ```
4. **Deploy** 클릭 → 1~3분 빌드 후 `https://<앱이름>.streamlit.app` 주소 생성.

## ⑤ 확인

- 공개 주소 접속 → 로그인/회원가입 화면이 보이면 성공.
- ②를 했다면 기존 계정으로 로그인 → 카드가 그대로 보임.
- 친구는 같은 주소에서 **각자 회원가입** → 서로 카드가 안 섞임(계정 분리).

---

## 동작 원리 (참고)
- 코드의 `get_conn()`은 **Secrets에 `[turso]`가 있으면 클라우드 DB**, 없으면 **로컬 `memoria.db`**에 연결합니다. 즉 **같은 코드로 로컬 개발 + 클라우드 배포**가 됩니다.
- 비밀번호는 `pbkdf2-sha256`(솔트+20만 반복)로 해시 저장 — 평문 저장 안 함.

## 자주 묻는 것
- **비용**: Turso·Streamlit Cloud·GitHub 모두 무료 티어로 개인/소수 사용에 충분.
- **앱이 잠자면?**: 무료 Streamlit 앱은 미사용 시 잠들 수 있으나, 접속하면 깨어납니다. 데이터는 Turso에 있으니 안 사라집니다.
- **토큰이 노출되면?**: Turso에서 토큰을 폐기(revoke)하고 새로 만들어 Secrets만 교체하면 됩니다.
- **PDF 업로드**가 클라우드에서 안 되면: `requirements.txt`에 `PyPDF2`가 포함돼 있어 정상 동작합니다.
