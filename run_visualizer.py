"""
Command-line driver to compile and launch the Autonomous Analog Circuit Verifier Interactive Visual Dashboard.
Scans benchmark_results/ across all 10 ICs and outputs self-contained HTML dashboards.
"""

import os
import sys
import argparse
import webbrowser
import shutil
import logging

from ai_analog_designer.visualization import ProcessVisualizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("VisualizerRunner")

def main():
    parser = argparse.ArgumentParser(description="Generate and open the Autonomous Analog Verifier Interactive Visual Dashboard.")
    parser.add_argument("--benchmark-dir", default="benchmark_results", help="Directory containing IC benchmark runs.")
    parser.add_argument("--output-html", default=os.path.join("benchmark_results", "ANALOG_VERIFIER_PROCESS_VISUALIZER.html"), help="Master visualizer output HTML path.")
    parser.add_argument("--open", action="store_true", help="Automatically open generated HTML in web browser.")
    args = parser.parse_args()

    benchmark_dir = os.path.abspath(args.benchmark_dir)
    logger.info(f"Loading benchmark suite from: {benchmark_dir}")

    ic_records = ProcessVisualizer.load_benchmark_suite(benchmark_dir)
    logger.info(f"Successfully loaded {len(ic_records)} IC design records from {benchmark_dir}")

    if not ic_records:
        logger.error("No valid IC design records found to visualize.")
        sys.exit(1)

    # 1. Export Master Visualizer
    out_master = os.path.abspath(args.output_html)
    ProcessVisualizer.export_dashboard(ic_records, out_master)
    logger.info(f"Master Multi-IC Dashboard exported to: {out_master}")

    # 2. Also export individual dashboards for each IC
    for ic in ic_records:
        folder = os.path.join(benchmark_dir, ic["folder_name"])
        indiv_out = os.path.join(folder, f"{ic['folder_name']}_VISUAL_DASHBOARD.html")
        ProcessVisualizer.export_dashboard([ic], indiv_out)

    # 3. Copy to Antigravity Artifact directory if available
    artifact_dir = r"C:\Users\Sera\.gemini\antigravity\brain\5ed477be-d087-4e28-a4f4-596a304931e3"
    if os.path.isdir(artifact_dir):
        art_path = os.path.join(artifact_dir, "ANALOG_VERIFIER_PROCESS_VISUALIZER.html")
        shutil.copyfile(out_master, art_path)
        logger.info(f"Master Dashboard copied to Artifacts: {art_path}")

    print("\n" + "="*80)
    print("SUCCESS: Autonomous Analog Verifier Interactive Visual Dashboard Ready!")
    print(f"Master HTML: file:///{out_master.replace(os.sep, '/')}")
    print("="*80 + "\n")

    if args.open:
        webbrowser.open(f"file:///{out_master}")

if __name__ == "__main__":
    main()
