import os
import json
import logging
import pandas as pd

# -----------------------------
# Setup logging
# -----------------------------
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, "data_download.log"),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger()

# -----------------------------
# Create folders
# -----------------------------
DATA_DIR = "data"  # individual documents
COMBINED_DIR = "combined_data"  # full combined outputs
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(COMBINED_DIR, exist_ok=True)


# -----------------------------
# Helper: save combined CSV and JSON
# -----------------------------
def save_combined(df: pd.DataFrame, base_name: str) -> None:
    csv_path = os.path.join(COMBINED_DIR, f"{base_name}.csv")
    json_path = os.path.join(COMBINED_DIR, f"{base_name}.json")

    df.to_csv(csv_path, index=False)
    df.to_json(json_path, orient="records", date_format="iso")

    logger.info(f"Saved combined {base_name} to {csv_path} and {json_path}")


# -----------------------------
# Helper: save one JSON document per row
# -----------------------------
def save_individual_documents(df: pd.DataFrame, prefix: str = "doc") -> None:
    try:
        logger.info("Saving individual documents")

        # Clear existing json docs in data folder
        for filename in os.listdir(DATA_DIR):
            if filename.endswith(".json"):
                os.remove(os.path.join(DATA_DIR, filename))

        records = json.loads(df.to_json(orient="records", date_format="iso"))

        for i, record in enumerate(records):
            safe_date = str(record.get("Date", f"{i}")).replace(":", "-")
            filename = f"{prefix}_{i+1}_{safe_date}.json"
            filepath = os.path.join(DATA_DIR, filename)

            with open(filepath, "w") as f:
                json.dump(record, f, indent=2)

        logger.info(f"Saved {len(records)} individual JSON documents to {DATA_DIR}")

    except Exception as e:
        logger.error(f"Saving individual documents failed: {e}")
        raise


# -----------------------------
# Download WTI oil data (daily)
# -----------------------------
def download_oil_data(start: str = "1986-01-01") -> pd.DataFrame:
    try:
        logger.info("Downloading daily WTI oil data from FRED")

        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILWTICO"
        oil = pd.read_csv(url)

        oil.columns = ["Date", "wti_price"]
        oil["Date"] = pd.to_datetime(oil["Date"])
        oil["wti_price"] = pd.to_numeric(oil["wti_price"], errors="coerce")

        oil = oil.dropna()
        oil = oil[oil["Date"] >= pd.to_datetime(start)].reset_index(drop=True)

        save_combined(oil, "oil_prices_daily")
        logger.info(f"Daily oil data rows: {len(oil)}")

        return oil

    except Exception as e:
        logger.error(f"Oil data download failed: {e}")
        raise


# -----------------------------
# Download gas price data (weekly)
# -----------------------------
def download_gas_data(start: str = "1986-01-01") -> pd.DataFrame:
    try:
        logger.info("Downloading weekly gas price data from FRED")

        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GASREGW"
        gas = pd.read_csv(url)

        gas.columns = ["Date", "gas_price"]
        gas["Date"] = pd.to_datetime(gas["Date"])
        gas["gas_price"] = pd.to_numeric(gas["gas_price"], errors="coerce")

        gas = gas.dropna()
        gas = gas[gas["Date"] >= pd.to_datetime(start)].reset_index(drop=True)

        save_combined(gas, "gas_prices_weekly_raw")
        logger.info(f"Weekly gas data rows: {len(gas)}")

        return gas

    except Exception as e:
        logger.error(f"Gas data download failed: {e}")
        raise


# -----------------------------
# Download recession data (monthly)
# -----------------------------
def download_recession_data(start: str = "1986-01-01") -> pd.DataFrame:
    try:
        logger.info("Downloading recession data from FRED")

        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=USREC"
        rec = pd.read_csv(url)

        rec.columns = ["Date", "recession"]
        rec["Date"] = pd.to_datetime(rec["Date"])
        rec["recession"] = pd.to_numeric(rec["recession"], errors="coerce")

        rec = rec.dropna()
        rec = rec[rec["Date"] >= pd.to_datetime(start)].reset_index(drop=True)

        save_combined(rec, "recession_data_raw")
        logger.info(f"Recession data rows: {len(rec)}")

        return rec

    except Exception as e:
        logger.error(f"Recession data download failed: {e}")
        raise


# -----------------------------
# Convert daily oil to weekly average
# -----------------------------
def make_weekly_oil(oil: pd.DataFrame) -> pd.DataFrame:
    try:
        logger.info("Converting daily oil data to weekly average")

        weekly_oil = (
            oil.set_index("Date")
            .resample("W-FRI")
            .agg({"wti_price": "mean"})
            .dropna()
            .reset_index()
        )

        save_combined(weekly_oil, "oil_prices_weekly")
        logger.info(f"Weekly oil rows: {len(weekly_oil)}")

        return weekly_oil

    except Exception as e:
        logger.error(f"Weekly oil conversion failed: {e}")
        raise


# -----------------------------
# Convert recession data to weekly via asof alignment
# -----------------------------
def align_recession_weekly(rec: pd.DataFrame, weekly_dates: pd.DataFrame) -> pd.DataFrame:
    try:
        logger.info("Aligning recession data to weekly dates")

        rec = rec.sort_values("Date").reset_index(drop=True)
        weekly_dates = weekly_dates.sort_values("Date").reset_index(drop=True)

        weekly_rec = pd.merge_asof(
            weekly_dates,
            rec,
            on="Date",
            direction="backward"
        )

        weekly_rec["recession"] = weekly_rec["recession"].fillna(0).astype(int)

        save_combined(weekly_rec, "recession_data_weekly")
        logger.info(f"Weekly recession rows: {len(weekly_rec)}")

        return weekly_rec

    except Exception as e:
        logger.error(f"Weekly recession alignment failed: {e}")
        raise


# -----------------------------
# Merge + weekly feature engineering
# -----------------------------
def merge_and_engineer(
    weekly_oil: pd.DataFrame,
    gas: pd.DataFrame,
    weekly_rec: pd.DataFrame
) -> pd.DataFrame:
    try:
        logger.info("Merging weekly datasets and engineering features")

        weekly_oil = weekly_oil.sort_values("Date").reset_index(drop=True)
        gas = gas.sort_values("Date").reset_index(drop=True)
        weekly_rec = weekly_rec.sort_values("Date").reset_index(drop=True)

        # Align weekly gas to most recent weekly oil date
        df = pd.merge_asof(
            gas,
            weekly_oil,
            on="Date",
            direction="backward"
        )

        # Align recession data
        df = pd.merge_asof(
            df.sort_values("Date"),
            weekly_rec.sort_values("Date"),
            on="Date",
            direction="backward"
        )

        # Weekly features
        df["wti_pct_change"] = df["wti_price"].pct_change(4)
        df["gas_lag1"] = df["gas_price"].shift(4)
        df["gas_lag2"] = df["gas_price"].shift(8)
        df["gas_lag3"] = df["gas_price"].shift(12)

        # Target: gas price 4 weeks ahead
        df["target_gas_4w"] = df["gas_price"].shift(-4)

        df["recession"] = df["recession"].fillna(0).astype(int)

        df = df.dropna().reset_index(drop=True)

        # Round float columns
        float_cols = df.select_dtypes(include=["float"]).columns
        df[float_cols] = df[float_cols].round(4)

        save_combined(df, "final_dataset_weekly")
        save_individual_documents(df, prefix="weekly_record")

        logger.info(f"Final weekly dataset rows: {len(df)}")

        return df

    except Exception as e:
        logger.error(f"Weekly merge/feature engineering failed: {e}")
        raise


# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    try:
        oil_df = download_oil_data()
        gas_df = download_gas_data()
        rec_df = download_recession_data()

        weekly_oil_df = make_weekly_oil(oil_df)
        weekly_rec_df = align_recession_weekly(rec_df, weekly_oil_df[["Date"]])

        final_df = merge_and_engineer(weekly_oil_df, gas_df, weekly_rec_df)

        print("Sample weekly data:")
        print(final_df.head())
        print("\nColumns:")
        print(final_df.columns.tolist())
        print(f"\nIndividual documents saved to: {DATA_DIR}")
        print(f"Combined files saved to: {COMBINED_DIR}")

        logger.info("Script completed successfully")

    except Exception as e:
        logger.critical(f"Script failed: {e}")
        print("Script failed. Check logs/data_download.log")