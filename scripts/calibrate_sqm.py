"""
calibrate_sqm.py

CLI utility to fit an empirical model between aggregated DVNL radiance 
and ground-truth SQM measurements.
"""

import argparse
import pandas as pd
import numpy as np
from TerraLab.light_pollution.calibration import SQMCalibrationModel

def main():
    parser = argparse.ArgumentParser(description="Calibrate DVNL to SQM model.")
    parser.add_argument("csv", help="CSV with columns: lat, lon, agg_dvnl, elevation_m, sqm")
    parser.add_argument("output", help="Path to save .joblib model")
    args = parser.parse_args()

    print(f"Loading data from {args.csv}...")
    df = pd.read_csv(args.csv)
    
    required = ["agg_dvnl", "elevation_m", "sqm"]
    for col in required:
        if col not in df.columns:
            print(f"Error: Missing column {col} in CSV.")
            return

    model = SQMCalibrationModel()
    print("Fitting robust Huber model...")
    model.fit(df["agg_dvnl"].values, df["elevation_m"].values, df["sqm"].values)
    
    # Simple evaluation
    pred = model.predict(df["agg_dvnl"].values, df["elevation_m"].values)
    mae = np.mean(np.abs(pred - df["sqm"].values))
    rmse = np.sqrt(np.mean((pred - df["sqm"].values)**2))
    
    print(f"Calibration metrics:")
    print(f"  MAE: {mae:.3f} mag/arcsec^2")
    print(f"  RMSE: {rmse:.3f} mag/arcsec^2")
    
    model.save(args.output)
    print(f"Model saved to {args.output}")

if __name__ == "__main__":
    main()
