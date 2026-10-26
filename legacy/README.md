# Legacy Framework & Historical Artefacts (v1 Prototype)

이 디렉토리는 2026-10-13 ~ 2026-10-14 초기 프로토타입 단계에서 작성되었던 레거시 코드, 초기 템플릿 기반 설계기, 이전 러너 스크립트 및 테스트벤치를 보존하는 아카이브입니다.

## 아카이브 디렉토리 구조

* **`analog_verifier/`**: 초기 Neuro-Symbolic 검증 파이프라인 (Stage 0 ~ Stage 5).
  * 현재는 최상위 `ai_analog_designer/neuro_symbolic_verifier/` 로 현대화 및 통합되었습니다.
* **`circuit_designer/`**: 초기 템플릿/파라메트릭 기반 아날로그 회로 설계기 (BJT Op-Amp, CMOS OTA, Comparator, Buck).
  * 현재는 `ai_analog_designer/circuit_synthesis/` 및 `Harness2CircuitArchitect` 로 대체되었습니다.
* **`agent_engine/`**: 초기 최적화기 및 LTspice 실행기.
  * LTspice 실행 엔진은 `ai_analog_designer/neuro_symbolic_verifier/ltspice_runner.py` 로 이관되었습니다.
* **`legacy_runners/`**: 초기 벤치마크 및 검증 스크립트 (`integrated_system.py`, `run_10_ic_benchmark.py`, `run_evaluation.py` 등).
* **`generated_designs/`**, **`generated_netlists/`**, **`generated_testbenches/`**: 초기 실행에서 생성되었던 임시 넷리스트 및 테스트벤치 아카이브.
* **`testbenches/`**, **`testbenches_pmic/`**: 초기 Op-Amp 및 PMIC 검증용 `.cir` 파일 아카이브 (대용량 SPICE 바이너리 `.raw`/`.db`는 저장소 경량화를 위해 정리됨).

> 💡 **현재 권장 사용법**:
> 모든 능동적인 아날로그 회로 합성 및 폐루프 검증은 루트의 `run_studio.py` (Interactive Web Studio) 또는 `run_harness2_netlist_loop.py` 를 통해 실행하십시오.
