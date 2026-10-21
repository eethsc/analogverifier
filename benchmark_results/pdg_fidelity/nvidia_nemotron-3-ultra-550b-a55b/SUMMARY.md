# PDG Fidelity Benchmark — model `nvidia/nemotron-3-ultra-550b-a55b`

Generated 2026-10-21 15:54. Ground truth: `benchmarks/pdg_groundtruth/` (hand-labelled, page-cited).

| IC | GT score | Pin P/R | Role acc | Dep P/R (full PDG) | Dep R (datasheet layer) | Sched. order | Elec. spec recall | Value acc | Category acc | AbsMax/Rec leak | Timing recall | Provenance score | Pins grounded | Specs grounded | LLM s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ina128 | **88.5** | 1.00/0.88 | 1.00 | 0.60/0.86 | 0.86 | 1.00 | 0.84 | 0.87 | 1.00 | 0.00 | 0.90 | 87.5 | 100.0% | 95.9% | 74.3 |
| lm317 | **90.4** | 1.00/1.00 | 1.00 | 0.67/1.00 | 1.00 | 1.00 | 0.95 | 0.96 | 1.00 | 0.00 | - | 83.6 | 100.0% | 94.4% | 60.8 |
| lm358 | **98.5** | 1.00/1.00 | 1.00 | 1.00/1.00 | 1.00 | 1.00 | 1.00 | 0.96 | 0.96 | 0.09 | 1.00 | 86.4 | 100.0% | 95.6% | 103.1 |
| lm386 | **92.4** | 1.00/0.88 | 1.00 | 0.60/0.75 | 0.75 | 1.00 | 1.00 | 0.91 | 0.96 | 0.00 | 1.00 | 88.6 | 100.0% | 100.0% | 47.5 |
| lm393 | **98.2** | 1.00/1.00 | 1.00 | 1.00/1.00 | 1.00 | 1.00 | 1.00 | 0.88 | 1.00 | 0.00 | 1.00 | 86.2 | 100.0% | 94.7% | 81.4 |
| ne555 | **79.8** | 1.00/1.00 | 1.00 | 1.00/0.77 | 0.77 | 1.00 | 0.19 | 0.87 | 1.00 | 0.00 | 1.00 | 84.1 | 100.0% | 96.4% | 90.5 |
| opa2134 | ERROR: LLM call failed on all models ['nvidia/nemotron-3-ultra-550b | | | | | | | | | | | | | | | |
| tl431 | ERROR: LLM call failed on all models ['nvidia/nemotron-3-ultra-550b | | | | | | | | | | | | | | | |

**Mean GT score: 91.3 / 100** over 6 ICs.

Metric definitions: see `benchmarks/README.md`.