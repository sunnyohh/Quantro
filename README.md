# Quantro

종목별 누적 예산과 현금을 분리하는 투자 프로그램입니다. 설계 기준은
[아키텍처 명세서](docs/Quantro_Software_Architecture_Specification_v0.1.md)입니다.

## 초기 Core 구현

- Python 3.11+, Decimal 기반 KRW 원장과 계좌/종목/Allocation 불변조건
- 실제 미할당 현금에서 즉시 예산 추가, revision 충돌 검사와 요청 멱등성
- 일간/주간/월간 정기 할당, 월말 보정, 변경 시점 기준 주기 재설정
- 자금 부족 시 전액 대기 및 기한순 재시도, occurrence 중복 방지
- 예산 비율 매수/가용 보유 비율 매도 수량 계산과 비용·브로커 한도 반영
- 개발용 메모리 UnitOfWork의 잠금, 원자적 commit, 예외 시 변경 폐기

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -s tests/unit -v
python apps/worker/demo.py
```

Windows의 Asia/Seoul 시간대 데이터는 패키지 설치 시 tzdata로 제공됩니다.
샘플 종목 EXAMPLE은 테스트 식별자이며 실제 투자 상품이 아닙니다.

## 구현 범위와 다음 단계

현재는 명세서 16.1의 첫 단계인 Core를 시작한 상태입니다. 메모리 저장소는
프로세스 종료 시 데이터를 잃으며, 프로세스 간 동시성과 재시작 복구는 보장하지 않습니다.
계좌는 PAPER/BACKTEST만 생성할 수 있고 실행 gate는 CLOSED입니다.
AT-08 중 예산 명령/occurrence 중복을 검증하며, Fill 중복 처리는 다음 단계입니다.
AT-05는 체결 완료 snapshot의 평가 계산을 검증합니다.

다음 구현 순서는 PostgreSQL/Alembic 영속 저장소와 재시작 복구,
전략 registry·주문 reservation·상태 머신·SimulatedBroker,
결정적 Backtest, 인증된 FastAPI와 React/ECharts 화면,
Windows 32bit CYBOS gateway 및 LIVE 검증입니다.
스케줄 API 멱등성, pause, 명시적 첫 실행 시점, 대량 catch-up 배치도 후속 범위입니다.

Core는 외부 SDK/ORM/HTTP에 의존하지 않습니다. Application은 UnitOfWork/Clock port를
주입받으며, 현재 infrastructure/memory.py를 PostgreSQL 어댑터로 교체할 수 있습니다.
