import os
import logging
import pandas as pd

# Setup logging
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, "data_download.log"),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger()

# Create Data folder
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)


# Download WTI Oil Data (FRED)
def download_oil_data(start="1986-01-01"):
    try:
        logger.info("Downloading WTI oil data from FRED")

        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILWTICO"
        oil = pd.read_csv(url)

        oil.columns = ["Date", "Oil_Price"]
        oil["Date"] = pd.to_datetime(oil["Date"])

        oil["Oil_Price"] = pd.to_numeric(oil["Oil_Price"], errors="coerce")
        oil = oil.dropna()

        oil = oil[oil["Date"] >= start]

        # Save as JSON
        oil.to_json(f"{DATA_DIR}/oil_prices.json", orient="records", date_format="iso")

        logger.info(f"Saved oil data: {len(oil)} rows")

        return oil

    except Exception as e:
        logger.error(f"Oil data download failed: {e}")
        raise


# Download Gas Price Data (FRED)
def download_gas_data(start="1986-01-01"):
    try:
        logger.info("Downloading gas price data from FRED")

        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=GASREGW"
        gas = pd.read_csv(url)

        gas.columns = ["Date", "Gas_Price"]
        gas["Date"] = pd.to_datetime(gas["Date"])

        gas["Gas_Price"] = pd.to_numeric(gas["Gas_Price"], errors="coerce")
        gas = gas.dropna()

        gas = gas[gas["Date"] >= start]

        # Save as JSON
        gas.to_json(f"{DATA_DIR}/gas_prices.json", orient="records", date_format="iso")

        logger.info(f"Saved gas data: {len(gas)} rows")

        return gas

    except Exception as e:
        logger.error(f"Gas data download failed: {e}")
        raise


# Merge datasets
def merge_data(oil, gas):
    try:
        logger.info("Merging oil and gas datasets")

        df = pd.merge(oil, gas, on="Date", how="inner")

        # Save merged JSON
        df.to_json(f"{DATA_DIR}/merged_data.json", orient="records", date_format="iso")

        logger.info(f"Merged dataset saved: {len(df)} rows")

        return df

    except Exception as e:
        logger.error(f"Merge failed: {e}")
        raise


# Main script
if __name__ == "__main__":
    try:
        oil_df = download_oil_data()
        gas_df = download_gas_data()
        merged_df = merge_data(oil_df, gas_df)

        print("Sample Data:")
        print(merged_df.head())

        logger.info("Script completed successfully")

    except Exception as e:
        logger.critical(f"Script failed: {e}")
        print("Script failed. Check logs.")