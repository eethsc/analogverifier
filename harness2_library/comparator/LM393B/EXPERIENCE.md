# Experience Record: LM393B (comparator)
**Score Delta:** 31.0% -> 51.5% (+20.5%)

## Cue
Unsatisfied specs on LM393B: ['VCC', 'VCC', 'VID', 'VID']

## Lesson
Topology: dual independent four-stage bipolar comparator channels sharing one bias core, following the LM393B blueprint mandates.

Bias core: a two-junction NPN reference stack (Q_BG1/Q_BG2 style VBE stack) sets a ~1.24 V reference. A 360k resistor from VCC supplies a master current of roughly 10 µA into an emitter-resistor-set NPN master sink (PTAT-style: the unequal-area reference pair generates ΔVBE across the emitter resistor). A diode-connected PNP mirror reference distributes the bias (BIAS_P) to each channel's PNP tail source. A 150k startup/feed resistor from VCC to the reference node guarantees the zero-current state is escaped at power-up; it also adds a supply-dependent term to total IQ, which is why IQ varies with VS.

Per channel:
- Input stage: large-area (4x) PNP differential pair (Q1, Q2) with 500 Ω emitter degeneration, low base current from the high-beta vertical PNP.
- Load: NPN current mirror on the collectors, diode-connected reference device with 500 Ω degeneration for matching.
- Gain stage: NPN common-emitter (Q3, 8x area) with a PNP active load mirroring ~150 µA from the bias reference. The 3 pF Miller capacitor from the gain-stage collector to base provides the dominant pole; a 1.6 kΩ series nulling resistor in that path places the RHP zero out of the unity-gain band.
- Output: PNP level-shift/drive device feeding the open-collector NPN output (50× area, power device) with a Baker-clamp Schottky from base to collector for fast turn-off. A 20k base pull-down to emitter ensures Q_out is OFF with low leakage; a 1 GΩ output-to-ground resistor models the substrate leakage path. A 50 Ω output-emitter degeneration reduces VOL under 4 mA sink.

Offset/balance: input emitters are matched (1:1 area, common-centroid layout implied); BAL pins couple to the input emitter nodes through 1 kΩ resistors for trimming.

Compensation and modelling caveats: the BJT models are generic illustrative parameters, not a qualified LM393B vendor model. Phase margin, production offset, PVT limits, ESD, and absolute-maximum ratings cannot be certified by this netlist alone; the values are sized to the mandates and should be verified by simulation against the target specifications.

Single-supply operation: negative supply is node GND (0 V). No external node '0' is referenced inside the subcircuit.

## Evidence
Pass rate improved from 31.0% to 51.5%.
