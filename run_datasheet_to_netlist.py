import os
import sys
import argparse
import logging
from ai_analog_designer import AutonomousAnalogDesignAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("DatasheetToNetlistApp")

def main():
    parser = argparse.ArgumentParser(
        description="Autonomous AI Analog Circuit Designer: Ingests IC Datasheet PDF, constructs Knowledge Graph / PDG, synthesizes SPICE netlist from scratch, and verifies 100% specs via LTspice closed loop."
    )
    parser.add_argument(
        "--pdf",
        type=str,
        default="testcases/cdcvf2505-q1.pdf",
        help="Path to IC datasheet PDF document (e.g. testcases/cdcvf2505-q1.pdf, testcases/tlc2932a.pdf)"
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=8,
        help="Maximum pages of datasheet to parse (focusing on pinout and electrical specs tables)"
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Maximum closed-loop simulation repair iterations"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="generated_designs",
        help="Output directory to store synthesized netlists, testbenches, and reports"
    )
    parser.add_argument(
        "--model",
        type=str,
        default="google/gemini-2.5-flash",
        help="OpenRouter LLM model identifier"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="OpenRouter API Key (defaults to OPENROUTER_API_KEY environment variable or configured user key)"
    )

    args = parser.parse_args()

    if not os.path.exists(args.pdf):
        logger.error(f"Datasheet PDF not found at: {args.pdf}")
        sys.exit(1)

    logger.info("Initializing Autonomous AI Analog Design Agent...")
    agent = AutonomousAnalogDesignAgent(
        api_key=args.api_key,
        model=args.model,
        output_dir=args.output_dir,
        max_iterations=args.max_iterations
    )

    logger.info(f"Processing datasheet: {args.pdf}")
    result = agent.run(datasheet_pdf_path=args.pdf, max_pages=args.max_pages)

    summary = result.get("summary", {})
    print("\n" + "="*80)
    print("                     AUTONOMOUS DESIGN SUMMARY")
    print("="*80)
    print(f"  Target Part Name      : {summary.get('part_name')}")
    print(f"  Subcircuit Name       : {summary.get('subckt_name')}")
    print(f"  Total Extracted Specs : {summary.get('spec_count')}")
    print(f"  Specification Pass %  : {summary.get('pass_rate_percent', 0.0):.1f}%")
    print(f"  Synthesized Netlist   : {summary.get('final_netlist_path')}")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()
