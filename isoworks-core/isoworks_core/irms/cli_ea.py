#!/usr/bin/env python3
"""
irms/cli_ea.py — Command-line interface for the IRMS EA (continuous-flow) normaliser.
Loads an EA CSV/TXT export via irms.api.load, writes the normalised vendor table
to CSV and Parquet in the specified output folder, and prints a summary.
"""
import argparse
from pathlib import Path
def main():
    ap = argparse.ArgumentParser(description="IRMS EA (CF) normalizer")
    ap.add_argument("--data", required=True, help="Path to EA CSV/TXT export")
    ap.add_argument("--out", default="reports", help="Output folder")
    args = ap.parse_args()
    from irms.api import load as irms_load
    import pandas as pd
    result = irms_load(args.data)
    df = result.vendor_table
    outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)
    df.to_csv(outdir / "ea_normalized.csv", index=False)
    try: df.to_parquet(outdir / "ea_normalized.parquet", index=False)
    except Exception as e:

        logging.warning(f"Exception caught: {e}")
    cols = [c for c in ["sample_id","d13C","d15N","ampl_28","ampl_45","rR_45_44","rR_46_44"] if c in df.columns]
    print("Saved normalized EA table to:", outdir)
    print("Columns summary:", cols if cols else list(df.columns)[:12])
    print("First 5 rows:"); print(df[cols].head() if cols else df.head())
if __name__ == "__main__": main()
