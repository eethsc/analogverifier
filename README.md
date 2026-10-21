# AnalogVerifier — 데이터시트 기반 아날로그 IC 자동 합성·검증

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Status: 중간발표 WIP](https://img.shields.io/badge/Status-중간발표%20WIP-yellow.svg)](#-현재-상태-2026-10-21)

IC 데이터시트(PDF)를 읽어 **하드웨어 지식 그래프(HKG)** 와 **포트 의존성 그래프(PDG)** 를 만듭니다.
그다음 LLM으로 SPICE netlist를 합성하고, **고정된(결정적) LTspice 테스트벤치**로 데이터시트 spec을 측정합니다.
결과는 다시 LLM에 피드백되어 폐루프로 수정됩니다.

> ⚠️ **정정 (2026-10-21).** 이전 README의 "10개 IC 91.7–100 % 통과" 표는 실제 실행 결과가 아니었습니다.
> 실제 저장된 결과(`benchmark_results/10_IC_BENCHMARK_SUMMARY.json`)는 **9개 IC 0 %, TLC2932A 2 %** 입니다.
> 원인은 두 가지였습니다. 첫째, LLM이 작성한 테스트벤치가 spec을 측정하지 못했습니다(`not observed`).
> 둘째, 동작 조건과 절대최대정격까지 검증 대상 spec으로 계산됐습니다. 이번 작업으로 두 문제를 모두 고쳤습니다.

---

## 🚧 현재 상태 (2026-10-21)

| 단계 | 상태 | 근거 |
|---|---|---|
| Phase 1 – PDF → HKG 추출 | ✅ **동작·측정 완료** | 8개 IC 정답(GT) 비교 평균 **95.2 / 100** (`openai/gpt-6-luna`) |
| Phase 2 – HKG → PDG·시작 순서 σ(p) | ✅ **동작·측정 완료** | 의존성 recall 0.86–1.00, 시작 순서 일관성 1.00 |
| PDG 충실도 검증 | ✅ **신규** | 정답 없는 출처(provenance) 감사 + 정답 비교 벤치마크 |
| Phase 4/5 – 결정적 테스트벤치 + 평가 | ✅ **신규·제조사 모델로 검증 완료** | LM393 / NE555 vendor 모델 커버리지 100 %, 오판정 0건 |
| 컴파일 사전 점검 + LTspice 오류 피드백 | ✅ **신규** | 깨진 netlist는 측정 전에 걸러지고, 오류 줄이 그대로 LLM에 전달됨 |
| Phase 3 – LLM netlist 합성 | ❌ **핵심 병목** | 합성된 LM358 netlist가 아직 컴파일·수렴 단계를 통과하지 못함 (아래 참고) |
| Web Studio (브라우저) | ✅ **Playwright E2E 통과** | PDG 추출·시각화·타임라인·경로 샌드박스 검증 |
| LLM 백엔드 | ✅ OpenRouter + NVIDIA NIM | 작업별 모델 라우팅, 타임아웃·fallback, 비용 상한 |

### 요약
- **데이터시트 이해(PDG)와 검증기(테스트벤치)는 신뢰할 수 있는 수준**에 도달했습니다. 둘 다 정답 또는 제조사 모델과 대조해 확인했습니다.
- **남은 병목은 LLM이 시뮬레이션 가능한 netlist를 만드는 단계**입니다. 검증기가 오류를 정확히 짚어 피드백하지만, 아직 컴파일·수렴을 통과한 LM358 netlist는 없습니다.

---

## 1. PDG 충실도 검증 — "PDG가 데이터시트와 일치하는가?"

두 가지 방법을 함께 씁니다.

**(A) 정답 없이 하는 출처 감사** (`PDGFidelityVerifier.audit_pdg`): 파이프라인을 실행할 때마다 돌고, 라벨 없이도 LLM이 지어낸 내용을 잡아냅니다. 검사 항목은 다음과 같습니다.
- 추출한 핀 이름과 인용문이 **인용한 페이지에 실제로 있는지**
- spec 숫자가 **인용한 페이지에 실제로 있는지**
- 그래프에 순환이 없는지(DAG), 모든 출력이 전원에서 도달 가능한지
- 연결 중 데이터시트에서 나온 비율과 규칙으로 추가된 비율
- 지연 시간 중 데이터시트 타이밍 spec에서 나온 비율

**(B) 정답 비교 벤치마크** (`run_pdg_benchmark.py`): 정답은 `benchmarks/pdg_groundtruth/*.json`에 있습니다. 8개 IC(LM358, OPA2134, INA128, LM393, NE555, TL431, LM317, LM386)를 손으로 라벨링했고, 모든 항목에 페이지 번호와 원문 인용이 붙어 있습니다. 측정 지표는 다음과 같습니다.
- 핀 P/R, 핀 역할 정확도
- 인과 의존성 P/R, 시작 순서 일관성(σ(u) < σ(v))
- ELECTRICAL spec 행 recall, min/typ/max 값 정확도
- 절대최대정격·권장조건이 검증 대상으로 잘못 섞인 비율(leak)
- 타이밍 spec recall

지표 정의는 [`benchmarks/README.md`](benchmarks/README.md)에 있습니다.

### 결과 (모델별 평균 GT 점수)

| 추출 모델 | 평균 GT 점수 | IC당 소요 시간 | 비용 (8개 IC) |
|---|---|---|---|
| **openai/gpt-6-luna (OpenRouter, 현재 기본값)** | **95.2** | 28–120 s | 약 $0.03 |
| nvidia/nemotron-3-ultra-550b-a55b (NIM, 열 태깅 적용) | 94.0 | 80–200 s | 무료 (요청 한도 있음) |
| z-ai/glm-5.3-flash (OpenRouter) | 4개 IC 평균 85.1 (중단) | 24–68 s | – |
| deepseek-ai/deepseek-v4.1-flash (NIM) | 82.0 (7개 IC) | – | – |

`openai/gpt-6-luna`의 IC별 결과입니다(`benchmark_results/pdg_fidelity/openai_gpt-6-luna/SUMMARY.md`).

| IC | GT 점수 | 핀 P/R | 의존성 P/R | 전기 spec recall | 값 정확도 | 정격 leak |
|---|---|---|---|---|---|---|
| LM358 | **100.0** | 1.00/1.00 | 1.00/1.00 | 1.00 | 1.00 | 0.00 |
| NE555 | 98.9 | 1.00/1.00 | 1.00/0.92 | 1.00 | 0.96 | 0.00 |
| LM393 | 97.5 | 1.00/1.00 | 1.00/1.00 | 1.00 | 1.00 | 0.00 |
| OPA2134 | 97.3 | 1.00/1.00 | 1.00/1.00 | 0.92 | 0.92 | 0.00 |
| LM386 | 94.2 | 1.00/0.88 | 0.58/0.88 | 1.00 | 1.00 | 0.00 |
| INA128 | 92.8 | 1.00/0.88 | 0.60/0.86 | 0.96 | 0.99 | 0.06 |
| TL431 | 90.5 | 1.00/1.00 | 0.67/1.00 | 1.00 | 0.90 | 0.00 |
| LM317 | 90.4 | 1.00/1.00 | 0.67/1.00 | 0.95 | 0.96 | 0.00 |

점수를 올린 핵심 개선은 다음과 같습니다.
- spec 표 페이지를 단어 좌표로 다시 구성하고, 숫자마다 `{MIN}/{TYP}/{MAX}` 열 태그를 붙였습니다. 이것으로 값 정확도가 0.68에서 0.96으로 올랐습니다.
- spec을 페이지마다 따로 병렬 추출합니다.
- 각 항목에 근거 페이지와 원문 인용을 받습니다.
- 각 행을 ELECTRICAL / ABS_MAX / RECOMMENDED / THERMAL / ESD 중 하나로 분류합니다.

---

## 2. 결정적 테스트벤치 (Phase 4/5 교체)

LLM이 테스트벤치를 작성하던 방식은 폐기했습니다. 대신 **spec 종류별 고정 LTspice fixture**가 측정합니다. 관련 파일은 다음과 같습니다.
- `neuro_symbolic_verifier/spec_planner.py`: 소자 종류 판별, 핀 역할 매핑, spec을 측정 종류(kind)로 분류, 측정 조건(VS·RL·IOUT 등) 파싱
- `neuro_symbolic_verifier/deterministic_testbench.py`: 측정 종류별 fixture. 각 fixture는 DUT 인스턴스 1개와 이름이 고정된 `.meas`로 구성됩니다.
  - op-amp: VOS, IB, IOS, IQ, AOL, GBW, PM, CMRR, PSRR, SR, 출력 스윙, ISC, IO, 노이즈
  - comparator: VIO, IIB, IIO, AVD, VOL, IOL, IOH, ICC, 응답 시간
  - 555: VTH, VTRIG, VRESET, VCONT, VOL, VOH, ICC, 핀 전류, DISCH, tr/tf
  - TL431: Vref, Iref, Imin, Ioff, ZKA, ΔVref/ΔVKA
  - LM317: VREF, IADJ, 라인·로드 레귤레이션, ILIM, 리플 제거비
- `neuro_symbolic_verifier/units.py`: 단위를 정규화합니다. 예를 들어 V/mV를 dB로, µV/V 단위의 CMRR·PSRR을 양의 dB로 바꿉니다.
- `neuro_symbolic_verifier/verification_flow.py`: 컴파일 사전 점검 → fixture 실행 → 평가 → 피드백 순서로 진행합니다.

**보고 방식이 바뀌었습니다.** 측정 커버리지(측정된 수 / 측정 가능한 spec 수)와 측정된 spec 중 통과율을 따로 보고합니다. 측정하지 못한 spec은 더 이상 "통과"나 "실패"로 숨겨지지 않습니다.

### 제조사 모델로 검증기 자체를 검증 (`tools/calibrate_testbench.py`)

| 대상 | 측정 커버리지 | PASS | FAIL | 해석 |
|---|---|---|---|---|
| **LM393** (TI 모델, LTspice 내장) | **100 %** (16/16) | 14 + typ 1 | 0 (+typ 1*) | 맞는 회로를 통과시킴 |
| **NE555** (LTspice 내장 모델) | **100 %** (28/28) | 26 + typ 2 | 0 | 맞는 회로를 통과시킴 |
| LM358 spec × LM741 모델 (다른 부품) | 100 % (31/31) | 13 + typ 3 | 14 | IB·IQ·ISC·스윙 차이를 정확히 잡아냄 |

\* LM393의 TTL 입력 응답 시간 행(typ 300 ns)은 100 mV 스텝 조건과 다른 측정 조건이라 FAIL_TYP로 남겼습니다.

전체 결과는 `benchmark_results/testbench_calibration/CALIBRATION.md`에 있습니다.

캘리브레이션 중에 찾아 고친 문제는 다음과 같습니다.
- 1 ns 자극 엣지 때문에 시뮬레이션이 멈추는 문제 → 엣지를 10 ns로 바꿨습니다.
- 공급전원 0 V→VS 램프에서 일부 vendor 모델이 수렴하지 않는 문제 → DC 전원을 기본값으로 하고, 램프는 `TB_RAMP_SUPPLY=1`로 켤 수 있게 했습니다.
- ±15 V 전용 매크로모델 → `TB_SPLIT_SUPPLY=1` 옵션을 추가했습니다.

---

## 3. 폐루프 합성 (Phase 3) — 현재 병목

LM358을 대상으로 실제 E2E를 실행했습니다(`output_e2e/`).
- **NIM nemotron-3-ultra 합성 결과:** 서브서킷 중첩, `\` 줄 이어쓰기, netlist 안의 산문(`... repeat 18 more times`), 정의되지 않은 파라미터 때문에 LTspice가 컴파일하지 못했습니다.
- **OpenRouter claude-sonnet-5.5 합성 결과:** validator가 최상위 `.model` 줄을 주석으로 바꾸는 버그 때문에 `Undefined model "qpin"`이 발생했습니다. 이 버그는 고쳤습니다.
- **gemini-3.8-flash 합성 결과:** 동작점(operating point)이 수렴하지 않았습니다(컴파일 점검 60초 타임아웃).

이를 바탕으로 이번에 다음을 고쳤습니다.
1. **컴파일 사전 점검:** `.op` 한 번으로 먼저 확인하고, 실패하면 fixture 55개를 돌리지 않습니다. LTspice 오류 메시지와 문제가 된 netlist 줄을 그대로 LLM에 피드백합니다.
2. **잘못된 피드백 수정:** 측정값이 0개일 때 "All measured specifications passed"라는 피드백이 나가던 버그를 고쳤습니다.
3. **validator 구조 보정:** 중첩 서브서킷을 밖으로 꺼내고, `\` 이어쓰기를 `+`로 바꾸고, 산문을 주석 처리합니다. 최상위 `.model`·`.param`은 그대로 보존합니다.
4. **합성 프롬프트 보강:** 금지 규칙(중첩, `\`, 산문, 정의되지 않은 파라미터)을 추가했습니다. 실제 측정 조건(VS, RL, 측정 종류)도 프롬프트에 넣었습니다.
5. **기록과 재사용:** 가장 좋은 회차의 netlist를 보관하고(`*_BEST.cir`), LLM 원본 출력을 저장합니다(`*_iterN.raw.cir`). `--reuse-hkg` 옵션으로 Phase 1의 LLM 호출을 건너뛸 수 있습니다.

**다음 단계:** 수정한 validator와 프롬프트로 synthesis 모델 3종(sonnet-5.5 / gpt-6-sol / gemini-3.8-flash) 비교를 다시 실행합니다. 이번 비교는 종료된 이전 실행에서 남은 LTspice 프로세스가 쌓여 중단했습니다.

---

## 4. LLM 백엔드와 응답 시간

`ai_analog_designer/llm_client.py`는 OpenAI 호환 API를 쓰는 provider 공용 클라이언트입니다(NIM / OpenRouter).
- 스트리밍으로 받으며, 첫 토큰 대기(TTFT)에 90초, 전체 응답에 240초 타임아웃이 있고 초과하면 다음 모델로 넘어갑니다.
- 프로세스 전체에서 동시 요청 수를 제한해(`LLM_MAX_CONCURRENCY`) HTTP 429를 막습니다.
- 작업별로 모델을 따로 지정할 수 있습니다: `LLM_EXTRACT_MODEL`(Phase 1), `LLM_SYNTH_MODEL`(Phase 3).
- OpenRouter 비용을 호출마다 집계하고, `LLM_BUDGET_USD`에 도달하면 중단합니다.

### OpenRouter 측정 (`tools/llm_bench.py`, 2회 실행의 중앙값)

| 모델 | spec 페이지 추출 (s) | netlist 합성 (s) | 비용/호출 (추출 / 합성) | 비고 |
|---|---|---|---|---|
| **openai/gpt-6-luna** | 28.9 | 21.8 | $0.0024 / $0.0007 | **추출 기본값**: 정확도 1위(95.2) |
| anthropic/claude-haiku-5.5 | 19.6 | 40.3 | $0.0033 / $0.0042 | fallback |
| z-ai/glm-5.3-flash | 32.2 | 16.4 | $0.0007 / $0.0010 | 저렴하지만 정확도 85 |
| anthropic/claude-sonnet-5.5 | 25.5 | **8.5** | $0.062 / $0.014 | **합성 기본값** |
| google/gemini-3.8-flash | 92.4 | 14.5 | $0.024 / $0.0067 | 추출이 느림 |
| deepseek/deepseek-v4.1-flash, qwen/qwen3.8-flash | 빈 응답 | – | – | 사용 불가 |

### NVIDIA NIM 측정 (`tools/nim_latency_bench.py`)
- **편차가 큽니다.** 같은 모델도 3–52초로 응답 시간이 흔들립니다. LM358 Phase 1에서는 첫 토큰 대기 90초 타임아웃이 3번 나서 fallback으로 넘어갔고, 그 결과 Phase 1에만 **약 5분**이 걸렸습니다.
- **thinking을 켜면 5–30배 느려집니다.** 그래서 기본값을 끔으로 했습니다(`chat_template_kwargs`).
- **결론:** 속도와 안정성 때문에 현재 기본 provider는 **OpenRouter**입니다. NIM은 `LLM_PROVIDER=nim`으로 계속 쓸 수 있습니다.

원본 데이터는 `benchmark_results/LLM_BENCH_openrouter.json`, `benchmark_results/NIM_LATENCY_BENCH*.json`에 있습니다.

---

## 5. Web Studio 브라우저 테스트

`tests/test_web_studio_pdg.py`는 실제 Chromium을 Playwright로 조작하는 E2E 테스트입니다.
1. 페이지를 열고 샘플 IC 목록(11개)이 로드되는지 확인합니다.
2. Sample 모드에서 LM393을 고르고 **Extract & Visualize PDG**를 누르면, SVG 그래프(노드 8개)와 핀 표(8행)가 렌더링되는지 확인합니다.
3. 타임라인 슬라이더를 끝까지 옮기면 모든 핀이 Active로 바뀌는지 확인합니다.
4. 허용 범위 밖 경로를 보내면 `/api/extract_pdg`가 **HTTP 400**을 돌려주는지 확인합니다(경로 샌드박스).
5. 콘솔 오류가 0개인지 확인합니다.

결과는 **PASS**이고, 스크린샷은 `benchmark_results/web_studio/*.png`에 있습니다. `--generate N` 옵션을 주면 합성 작업까지 브라우저에서 실행합니다.

함께 고친 것은 다음과 같습니다.
- `pdf_path`를 `testcases/`·`uploads/` 안으로만 허용합니다. 이전에는 임의 파일을 읽을 수 있었습니다.
- UI의 "Synthesis Model" 표시가 하드코딩된 `gemini-2.5-flash` 대신 실제 설정을 보여줍니다(`/api/llm_config`).

---

## ⚡ 빠른 시작

```powershell
git clone https://github.com/eethsc/analogverifier.git
cd analogverifier
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt playwright
python -m playwright install chromium-headless-shell
```

루트에 `.env` 파일을 만듭니다. 이 파일은 git에서 제외되어 있으니 키를 커밋하지 마세요.

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY="sk-or-..."
LLM_EXTRACT_MODEL=openai/gpt-6-luna
LLM_SYNTH_MODEL=anthropic/claude-sonnet-5.5
LLM_FALLBACK_MODELS=anthropic/claude-haiku-5.5,z-ai/glm-5.3-flash
LLM_BUDGET_USD=3.0
LLM_MAX_CONCURRENCY=6

# NVIDIA NIM을 쓸 경우
# LLM_PROVIDER=nim
# NVIDIA_API_KEY="nvapi-..."
```

LTspice 경로는 자동으로 찾습니다. 찾지 못하면 `LTSPICE_PATH=...\LTspice.exe`로 지정하세요.

### 주요 명령

```powershell
# Web Studio
python run_studio.py --port 8080

# 폐루프 설계 (Phase 1 재사용 시 --reuse-hkg)
python run_interactive_designer.py --pdf testcases/lm358.pdf --iterations 3 --no-open --output-dir output_e2e/lm358
python run_interactive_designer.py --pdf testcases/lm358.pdf --iterations 3 --reuse-hkg output_e2e/lm358/LM358B_knowledge_graph.json

# PDG 충실도 벤치마크
python run_pdg_benchmark.py --model openai/gpt-6-luna
python run_pdg_benchmark.py --reuse          # 캐시된 추출 결과로 재채점 (LLM 호출 없음)

# 테스트벤치 검증 (제조사 모델 대조)
python tools/calibrate_testbench.py

# LLM 응답 시간·비용 비교
python tools/llm_bench.py --provider openrouter --models openai/gpt-6-luna,anthropic/claude-sonnet-5.5

# 브라우저 E2E
python tests/test_web_studio_pdg.py --ic LM393
```

---

## 📂 주요 구조

```
ai_analog_designer/
  llm_client.py                     # provider 공용 LLM 클라이언트 (NIM/OpenRouter, 타임아웃·fallback·비용)
  pdf_to_graph/
    pdf_parser.py                   # 페이지 분류 + MIN/TYP/MAX 열 태깅
    hardware_kg_builder.py          # HKG 추출 (페이지별 병렬, 근거 인용)
    pdg_extractor.py                # PDG + 시작 순서 σ(p) (origin/delay_source 기록)
    pdg_fidelity_verifier.py        # (A) 출처 감사, (B) 정답 비교
  circuit_synthesis/                # LLM netlist 합성 + validator (구조 보정)
  neuro_symbolic_verifier/
    spec_planner.py                 # spec → 측정 계획
    deterministic_testbench.py      # 고정 LTspice fixture + 컴파일 점검 + 평가
    units.py, verification_flow.py
  web_studio/studio_server.py       # Web Studio (경로 샌드박스, /api/llm_config)
benchmarks/pdg_groundtruth/         # 손으로 라벨링한 정답 8개
benchmark_results/                  # 모든 측정 결과 (PDG, 캘리브레이션, LLM, 브라우저 스크린샷)
tools/, tests/                      # 벤치마크·캘리브레이션 스크립트, Playwright 테스트
```

레거시 패키지 `agent_engine/`과 `analog_verifier/`는 정리가 필요합니다. `agent_engine/ltspice_runner.py`만 현재 코드에서 사용합니다.

---

## 📝 남은 과제
1. **합성 모델 비교 재실행:** 수정한 validator와 프롬프트로 다시 돌려, 첫 번째로 컴파일·수렴하는 LM358 netlist를 확보합니다. 그 결과로 실제 통과율을 기록합니다.
2. **합성 전략 개선:** 트랜지스터 수준 이전에, 행동 모델(behavioral, 매크로모델 수준)로 먼저 spec을 맞추는 2단계 전략을 검토합니다.
3. **fixture 확장:** LM386(오디오 앰프)과 INA128(계측 앰프) fixture를 완성합니다(현재 일부만 구현).
4. **저장소 정리:** git-ignored인데 추적 중인 산출물 170개(`*.raw`, `*.db`, `*.log`)와 레거시 패키지를 정리하고 단위 테스트를 추가합니다.
