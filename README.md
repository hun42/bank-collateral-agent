# 담보 배분 분석 프로토타입

계약번호 기준으로 담보, 설정, 피담보채권 관계를 조회하고 배분 결과와 순신용 발생 사유를 표시합니다.

## 실행

Python 3.12 및 PostgreSQL이 필요합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-prototype.txt
Copy-Item .env.example .env
```

`.env`에 로컬 DB 접속정보를 설정합니다. `collateral_agent` 데이터베이스를 준비하고 아래 스크립트로 테스트 원장을 구성합니다.
기존 테스트 원장을 초기화할 수 있으므로 전용 개발 DB에서 실행합니다.

```powershell
psql -h 127.0.0.1 -U postgres -d collateral_agent -f db_setup_all_ledgers.sql
.\.venv\Scripts\python.exe -m uvicorn prototype_api:app --host 127.0.0.1 --port 8000
```

브라우저에서 `collateral-agent-prototype.html`을 열어 조회합니다.
API 문서는 http://127.0.0.1:8000/docs 에서 확인합니다.

## 주요 파일

- `prototype_api.py`: FastAPI 조회 API
- `allocation_rules.py`: 설정 및 피담보채권 배분 계산
- `collateral_review_rules.py`: 최종심사가액 계산
- `collateral-agent-prototype.html`: 결과 표 및 설명 화면
- `db_setup_all_ledgers.sql`: 테스트 스키마 및 데이터

테스트 입력: `100000020202020`, `100000020202021`.
현재 조회값은 숫자형 내부계약번호(ICN)를 사용합니다.
DB 비밀번호와 로컬 환경파일은 Git 추적에서 제외합니다.
