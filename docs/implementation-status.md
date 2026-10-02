# 구현 현황 — 2026-10-02

명세서 v0.1의 다음 개발 단위를 구현한 상태이며, 전체 제품 완료를 의미하지 않습니다.

| 영역 | 현재 구현 | 남은 작업 |
|---|---|---|
| Core | 예산/현금/NAV 분리, Decimal, 균형 원장, 스케줄 버전·대기·pause | 명시적 첫 실행 시점, 대량 catch-up 배치, 기업행사 |
| 영속성 | PostgreSQL/SQLAlchemy, Alembic, 관계형 제약·잔액 projection·typed JSON snapshot, 원장 deferred balance trigger | 실제 PostgreSQL 현장 검증, UUID 컬럼 전환, snapshot 필드를 세부 테이블로 추가 정규화 |
| 동시성 | DB writer row lock / SQLite BEGIN IMMEDIATE, 계좌 실행 lease, dispatcher gate 재검사 | 계좌별 세분화된 DB 잠금과 운영 writer lease heartbeat |
| 전략 | 설치된 plugin registry, JSON Schema, 불변 config/revision, PRIORITY/HOLD/suppressed 이력 | 상태를 가진 전략의 next-state 저장, version code hash registry, 런타임 자원 제한 |
| 주문 | sizing/risk, 예약+주문+outbox 원자 저장, 부분체결/취소/만료/UNKNOWN, 중복 fill, sell 정산 | 실제 gateway 송신 journal, snapshot 전체 대사, 브로커별 tick/fee 정책 |
| Backtest | 독립 MemoryStore, 다음 비조정 일봉 체결, 참여율/비용/슬리피지, 입금·예산 분리, hash/checksum | 정책 변경 타임라인, 기업행사, 거래소/정산 달력, benchmark, XIRR·연율화·승률 |
| Run | durable queue, 별도 worker, 진행/취소/실패, 만료 lease 재실행, 결과/manifest 저장·비교 | 별도 artifact 저장소, worker heartbeat, 대용량 dataset 외부 저장 |
| API | Bearer 인증, Decimal string, 변경 요청과 idempotency 응답의 원자 저장, dashboard·budget·strategy·Run | WebSocket, broker 시작/조회/취소·대사 API, 세션/사용자 관리, 요청량 제한 |
| UI | backend dashboard/비교 데이터를 제공 | React/TypeScript/ECharts 화면 전체 |
| CYBOS / LIVE | 계좌 생성은 PAPER/BACKTEST만 허용 | 32bit Windows COM gateway, SDK contract 검증, LIVE 운영 검증 |

## 어댑터 선택

Application의 초기 State UnitOfWork 계약을 유지하기 위해 각 aggregate의 typed JSON snapshot을
테이블별로 저장하고, 고유키·금액·포지션·예약·원장 entries는 관계형 projection으로 함께 저장합니다.
Decimal은 snapshot에서 string으로 보존합니다. SQL 금액 컬럼은 numeric(28,8)입니다.
현재 ID는 기존 Core의 문자열 ID와 호환되는 varchar이며 API와 신규 주문은 UUID를 생성합니다.
DB writer_lock의 단일 row를 트랜잭션 내 잠그므로 프로세스 간 초과 예약/할당을 막습니다.
이 방식은 초기 단일 사용자 구현이며 데이터가 커지면 계좌별 repository 및 잠금으로 분리해야 합니다.
트랜잭션 전체를 로드하므로 DB 관리자가 직접 projection만 변경하는 운영은 지원하지 않습니다.

PostgreSQL은 Alembic으로 초기화합니다. 금융 이력을 자동 drop하는 downgrade는 제공하지 않습니다.
SQLite는 로컬 개발·계약 검증용이며 PostgreSQL의 deferred trigger와 잠금을 검증하는 대체 수단은 아닙니다.
DB에 이미 생성한 개발 테이블을 바꿀 때도 기존 데이터를 지우지 말고 새 migration을 작성합니다.

## 실행 경계

- SDK/브로커 호출 중 DB 트랜잭션을 열지 않습니다. SUBMITTING 상태를 먼저 저장하고 호출합니다.
- timeout/crash 후 SUBMITTING/UNKNOWN은 예약을 유지하고 조회로 확정하기 전 재전송하지 않습니다.
- BrokerPort와 SimulatedBroker는 초기 주문 실행 계약을 제공합니다. API worker는 예산과 Run을
  처리하며 실시간 PAPER 시세 polling/dispatcher 데몬은 아직 연결하지 않았습니다.
- SimulatedBroker 내부 주문 상태는 메모리입니다. 운영 DB와 함께 자동 복원하는 gateway가 아니므로
  종료 후 기존 주문을 새 broker에 재전송하지 않습니다. UNKNOWN 조회는 증거 없는 REJECTED로 바꾸지 않습니다.
- 일봉 체결은 다음 bar, KRW 1원 tick, calendar-day settlement를 사용하는 명시적 근사입니다.
- 비교 API는 기간/데이터/초기 자금/외부 입금 차이를 반환합니다. 서로 다른 그룹을 숨기지 않습니다.
- API는 로컬 단일 사용자 Bearer token 설정을 요구합니다. 외부 배포 시 TLS와 운영 인증을 연결해야 합니다.

## 검증

로컬 단위·SQLite 통합·API·Simulation 테스트와 PostgreSQL migration SQL 생성 검증을 수행합니다.
PostgreSQL 테스트는 전용 `QUANTRO_TEST_POSTGRES_URL`이 있어야 실행하며, 각 테스트는 임의의
`quantro_test_...` schema를 생성·정리합니다. 운영 DB URL을 테스트에 사용하지 않습니다.
CI는 PostgreSQL 16 서비스를 띄우고 Alembic upgrade와 전체 테스트를 실행하도록 구성했습니다.
로컬에서 PostgreSQL 전용 테스트가 skipped이면 서버 검증을 수행한 것으로 간주하지 않습니다.
이번 로컬 검증 결과는 57개 통과, PostgreSQL 전용 18개 skipped입니다.
실제 Uvicorn API의 HTTP/인증/멱등 응답 및 별도 worker 프로세스 실행 smoke check도 통과했습니다.
Alembic의 초기 schema는 `migrations/schema_v0001.py`에 고정하여 후속 model 변경이
이미 배포한 migration의 의미를 바꾸지 않도록 했습니다.

라이브러리의 트랜잭션 및 migration API는
[SQLAlchemy 공식 문서](https://docs.sqlalchemy.org/en/20/core/connections.html)와
[Alembic 공식 문서](https://alembic.sqlalchemy.org/en/latest/tutorial.html)를 기준으로 구현했습니다.
