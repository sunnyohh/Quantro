# Quantro

종목별 예산·현금·포지션을 분리하고 같은 전략을 Paper/Backtest에서 실행하는 투자 프로그램입니다.
설계 기준은 [아키텍처 명세서](docs/Quantro_Software_Architecture_Specification_v0.1.md),
완료·미완료 범위는 [구현 현황](docs/implementation-status.md)에 정리했습니다.

현재 Core, 주문 파이프라인, 일봉 Backtest, 영속 저장소 및 인증된 REST API를 제공합니다.
React 화면과 CYBOS gateway는 아직 구현 전이며 LIVE 계좌 생성은 차단합니다.

## 설치 및 테스트

Python 3.11 이상, Core/API는 64bit 환경을 사용합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
python -m pip install -e ".[database,api,test]" --no-deps
python -m unittest discover -s tests -v
python scripts/smoke_api.py
python apps/worker/demo.py
```

`requirements-dev.lock`은 검증한 Core/API/개발 의존성을 고정합니다. 32bit gateway 의존성은
별도 환경으로 분리할 예정입니다. Windows 시간대 데이터는 tzdata로 제공합니다.
PostgreSQL 테스트는 전용 테스트 DB URL이 없으면 skipped입니다.
샘플 종목 EXAMPLE은 테스트 식별자입니다.

## 로컬 API와 Worker

```powershell
.\.venv\Scripts\Activate.ps1
$env:QUANTRO_DATABASE_URL = 'sqlite:///quantro.db'
$env:QUANTRO_API_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
python -m uvicorn quantro.api.app:app_factory --factory --host 127.0.0.1 --port 8000
```

API는 모든 요청에 `Authorization: Bearer <QUANTRO_API_TOKEN>`을 요구합니다.
변경 요청에는 고유한 `Idempotency-Key`도 필요합니다. 금액·비율은 JSON string입니다.
API는 로컬 단일 사용자 설정입니다. 외부 배포 시 TLS와 운영 인증을 연결해야 합니다.
token을 로그·파일·Git에 기록하지 않습니다.

별도 터미널에서 worker를 실행합니다.

```powershell
.\.venv\Scripts\Activate.ps1
$env:QUANTRO_DATABASE_URL = 'sqlite:///quantro.db'
python apps/worker/main.py
# 한 번만 실행: python apps/worker/main.py --once
```

Worker는 예산 catch-up과 대기 Simulation Run을 처리합니다.
Run은 API에서 202/QUEUED로 생성하고 worker가 별도 실행합니다.
실시간 Paper 시세 수집/주문 dispatcher 데몬은 후속 작업입니다.

인증된 OpenAPI schema는 `GET /api/v1/openapi.json`에서 조회합니다.
계좌·종목·Allocation 생성, 예산 추가/스케줄 변경/pause, 전략 연결/수정/archive,
dashboard/주문 조회, execution stop, Simulation Run 생성/취소/결과/비교를 제공합니다.

## PostgreSQL

SQLite는 로컬 개발용입니다. PostgreSQL은 Alembic migration으로 초기화합니다.
Docker가 설치된 환경에서는 다음 설정으로 개발 DB를 실행할 수 있습니다.

```powershell
$env:QUANTRO_POSTGRES_PASSWORD = '<개발용으로 생성한 비밀번호>'
docker compose up -d postgres
$env:QUANTRO_DATABASE_URL = 'postgresql+psycopg://quantro:<URL-encoded 비밀번호>@127.0.0.1:5432/quantro'
python -m alembic upgrade head
```

DB writer row lock으로 변경을 직렬화하고 주문·예약·outbox·fill·원장·응답 cache를 원자적으로 저장합니다.
PostgreSQL migration은 원장 균형 deferred trigger와 원장/전략 이력 append-only trigger를 생성합니다.
typed JSON snapshot과 관계형 제약/projection을 함께 저장하는 초기 어댑터입니다.
현재 전체 State를 로드하므로 대용량 운영 전에 계좌별 repository/잠금으로 분리해야 합니다.
금융 이력을 자동 drop하는 downgrade는 제공하지 않습니다.

CI는 PostgreSQL 16에서 migration과 전체 테스트를 실행하도록 구성했습니다.
로컬 PostgreSQL 테스트는 격리된 임의 schema를 생성·정리하며,
`QUANTRO_TEST_POSTGRES_URL`에는 전용 테스트 DB만 설정합니다.

## 주요 동작

- 누적 예산 B는 매매·평가손익으로 바뀌지 않습니다. 현금 A는 `C-R-U`입니다.
- 정기 예산은 실제 미할당 현금의 내부 이동입니다. 자금 부족은 전액 PENDING_FUNDS입니다.
- 월말 스케줄은 31일→2월 말일→3월 31일입니다. 변경은 한 주기 뒤부터 적용합니다.
- 전략 config와 assignment revision을 보존합니다. PRIORITY는 작은 숫자가 우선이며 HOLD는 빈 intent입니다.
- 명시적 주문/일간 한도·비용 상한·freshness 정책, 실행 gate, worker lease를 검사합니다.
- 전송 timeout/crash는 UNKNOWN/예약 유지이며 조회 확정 전 재전송하지 않습니다.
- 부분 체결 뒤 취소는 최신 주문/누적 체결 확인 후 남은 예약만 해제합니다.
- Backtest는 다음 비조정 일봉에서 체결하며 비용·참여율·slippage·정산 근사를 manifest에 기록합니다.
- 외부 입금을 수익으로 계산하지 않습니다. 입력/data/code hash와 결과 checksum을 저장합니다.
- API 변경 작업과 멱등 응답은 같은 DB 트랜잭션에 저장합니다.
