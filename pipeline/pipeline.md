```python
!pip install pymongo
```

    Requirement already satisfied: pymongo in /opt/anaconda3/envs/virtual_env/lib/python3.13/site-packages (4.16.0)
    Requirement already satisfied: dnspython<3.0.0,>=2.6.1 in /opt/anaconda3/envs/virtual_env/lib/python3.13/site-packages (from pymongo) (2.8.0)
    
    [1m[[0m[34;49mnotice[0m[1;39;49m][0m[39;49m A new release of pip is available: [0m[31;49m25.0.1[0m[39;49m -> [0m[32;49m26.0.1[0m
    [1m[[0m[34;49mnotice[0m[1;39;49m][0m[39;49m To update, run: [0m[32;49mpip install --upgrade pip[0m


## Setup
- Connecting to Mongo
- Adding data from file to mongo
- Redundancy checker to ensure new files are different
- Error Handling and Logging


```python
import os
import json
import logging
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus

from pymongo import MongoClient
from pymongo.errors import PyMongoError

# -----------------------------
# Logging setup
# -----------------------------

# Reset existing logging handlers (needed in notebooks so config applies properly)
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

# Define log file path (relative to notebook location)
# Moves up one directory from /pipeline into /logs
LOG_FILE = Path("../logs/mongo_upload.log")

# Ensure the logs folder exists before writing to it
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Configure logging to write to file and also print to console
logging.basicConfig(
    level=logging.INFO,  # capture INFO and above
    format="%(asctime)s | %(levelname)s | %(message)s",  # timestamp + level + message
    handlers=[
        logging.FileHandler(LOG_FILE),  # write logs to file
        logging.StreamHandler()         # also print logs to terminal
    ]
)

# Print full resolved path so you know exactly where logs are going
print("Logging to:", LOG_FILE.resolve())

# Create logger instance
logger = logging.getLogger(__name__)

# -----------------------------
# Mongo connection
# -----------------------------
def get_mongo_client():
    # Pull credentials from environment variables (.bashrc)
    username = os.getenv("MONGOUSER")
    password_raw = os.getenv("MONGOPASS")

    # Basic validation to ensure credentials exist
    if not username or not password_raw:
        raise ValueError("Missing MONGOUSER or MONGOPASS environment variable.")

    # Encode password to handle special characters safely in URI
    password = quote_plus(password_raw)

    # Build MongoDB connection string
    uri = f"mongodb+srv://{username}:{password}@dp2.0pmvbm0.mongodb.net/?appName=dp2"

    logger.info("Connecting to MongoDB cluster...")

    # Create client and test connection
    client = MongoClient(uri)
    client.admin.command("ping")  # verifies connection is working

    logger.info("MongoDB connection successful.")
    return client

# -----------------------------
# File loader
# -----------------------------
def load_json_file(file_path: Path):
    # Open and parse JSON file
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)

# -----------------------------
# Upload logic
# -----------------------------
def upload_folder_to_mongo(
    folder_path="../data",                   # folder containing JSON files
    db_name="project_db",                   # target database
    data_collection_name="raw_data",        # collection for actual data
    tracking_collection_name="uploaded_files"  # collection to track uploaded files
):
    folder = Path(folder_path)

    # Validate folder exists
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder.resolve()}")

    # Connect to MongoDB
    client = get_mongo_client()
    db = client[db_name]

    # Main data collection
    data_collection = db[data_collection_name]

    # Tracking collection for redundancy check
    tracking_collection = db[tracking_collection_name]

    # Ensure each file name is unique in tracking collection
    tracking_collection.create_index("file_name", unique=True)

    # Get all JSON files in folder
    json_files = sorted(folder.glob("*.json"))

    # If no files found, log and exit
    if not json_files:
        logger.warning("No JSON files found in folder: %s", folder.resolve())
        return

    logger.info("Found %d JSON files in %s", len(json_files), folder.resolve())

    # Counters for summary logging
    uploaded_count = 0
    skipped_count = 0
    failed_count = 0

    # Process each file one by one
    for file_path in json_files:
        file_name = file_path.name
        logger.info("Processing file: %s", file_name)

        try:
            # -----------------------------
            # Redundancy check
            # -----------------------------
            # Skip file if it was already uploaded before
            already_uploaded = tracking_collection.find_one({"file_name": file_name})
            if already_uploaded:
                logger.info("Skipping %s because it is already uploaded.", file_name)
                skipped_count += 1
                continue

            # Load JSON content
            data = load_json_file(file_path)

            # -----------------------------
            # Insert data into MongoDB
            # -----------------------------

            # Case 1: JSON file contains a list of documents
            if isinstance(data, list):

                # Skip empty arrays
                if not data:
                    logger.warning("Skipping %s because JSON array is empty.", file_name)
                    skipped_count += 1
                    continue

                docs_to_insert = []

                # Iterate through list and validate each document
                for doc in data:
                    if isinstance(doc, dict):
                        # Add metadata to track source file
                        doc["_source_file"] = file_name
                        docs_to_insert.append(doc)
                    else:
                        # Skip invalid records (Mongo requires dict documents)
                        logger.warning(
                            "Skipping one non-dict record in %s because Mongo expects documents.",
                            file_name
                        )

                # Insert valid documents
                if docs_to_insert:
                    result = data_collection.insert_many(docs_to_insert)
                    inserted_docs = len(result.inserted_ids)
                else:
                    logger.warning("No valid documents to insert from %s.", file_name)
                    skipped_count += 1
                    continue

            # Case 2: JSON file contains a single document
            elif isinstance(data, dict):
                data["_source_file"] = file_name
                result = data_collection.insert_one(data)
                inserted_docs = 1 if result.inserted_id else 0

            # Case 3: Invalid JSON structure
            else:
                logger.error(
                    "Skipping %s because top-level JSON is neither an object nor an array.",
                    file_name
                )
                failed_count += 1
                continue

            # -----------------------------
            # Track successful upload
            # -----------------------------
            tracking_collection.insert_one({
                "file_name": file_name,
                "uploaded_at": datetime.utcnow(),
                "inserted_docs": inserted_docs
            })

            logger.info("Uploaded %s successfully with %d document(s).", file_name, inserted_docs)
            uploaded_count += 1

        # -----------------------------
        # Error handling
        # -----------------------------
        except json.JSONDecodeError as e:
            logger.exception("JSON parsing failed for %s: %s", file_name, e)
            failed_count += 1

        except PyMongoError as e:
            logger.exception("MongoDB error while uploading %s: %s", file_name, e)
            failed_count += 1

        except Exception as e:
            logger.exception("Unexpected error while processing %s: %s", file_name, e)
            failed_count += 1

    # -----------------------------
    # Final summary
    # -----------------------------
    logger.info(
        "Upload complete | uploaded=%d | skipped=%d | failed=%d",
        uploaded_count,
        skipped_count,
        failed_count
    )

# -----------------------------
# Run it
# -----------------------------
upload_folder_to_mongo(
    folder_path="../data",
    db_name="project_db",
    data_collection_name="raw_data",
    tracking_collection_name="uploaded_files"
)
```

    2026-04-15 21:06:28,417 | INFO | Connecting to MongoDB cluster...


    Logging to: /Users/rameezrauf/Desktop/ds_4320/ds4320_dp2/logs/mongo_upload.log


    2026-04-15 21:06:28,885 | INFO | MongoDB connection successful.
    2026-04-15 21:06:28,945 | INFO | Found 1839 JSON files in /Users/rameezrauf/Desktop/ds_4320/ds4320_dp2/data
    2026-04-15 21:06:28,946 | INFO | Processing file: weekly_record_1000_2010-02-15T00-00-00.000.json
    2026-04-15 21:06:28,972 | INFO | Skipping weekly_record_1000_2010-02-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:28,973 | INFO | Processing file: weekly_record_1001_2010-02-22T00-00-00.000.json
    2026-04-15 21:06:29,002 | INFO | Skipping weekly_record_1001_2010-02-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,003 | INFO | Processing file: weekly_record_1002_2010-03-01T00-00-00.000.json
    2026-04-15 21:06:29,038 | INFO | Skipping weekly_record_1002_2010-03-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,039 | INFO | Processing file: weekly_record_1003_2010-03-08T00-00-00.000.json
    2026-04-15 21:06:29,066 | INFO | Skipping weekly_record_1003_2010-03-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,067 | INFO | Processing file: weekly_record_1004_2010-03-15T00-00-00.000.json
    2026-04-15 21:06:29,092 | INFO | Skipping weekly_record_1004_2010-03-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,094 | INFO | Processing file: weekly_record_1005_2010-03-22T00-00-00.000.json
    2026-04-15 21:06:29,116 | INFO | Skipping weekly_record_1005_2010-03-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,117 | INFO | Processing file: weekly_record_1006_2010-03-29T00-00-00.000.json
    2026-04-15 21:06:29,140 | INFO | Skipping weekly_record_1006_2010-03-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,141 | INFO | Processing file: weekly_record_1007_2010-04-05T00-00-00.000.json
    2026-04-15 21:06:29,155 | INFO | Skipping weekly_record_1007_2010-04-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,156 | INFO | Processing file: weekly_record_1008_2010-04-12T00-00-00.000.json
    2026-04-15 21:06:29,182 | INFO | Skipping weekly_record_1008_2010-04-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,182 | INFO | Processing file: weekly_record_1009_2010-04-19T00-00-00.000.json
    2026-04-15 21:06:29,200 | INFO | Skipping weekly_record_1009_2010-04-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,200 | INFO | Processing file: weekly_record_100_1992-11-16T00-00-00.000.json
    2026-04-15 21:06:29,227 | INFO | Skipping weekly_record_100_1992-11-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,228 | INFO | Processing file: weekly_record_1010_2010-04-26T00-00-00.000.json
    2026-04-15 21:06:29,257 | INFO | Skipping weekly_record_1010_2010-04-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,258 | INFO | Processing file: weekly_record_1011_2010-05-03T00-00-00.000.json
    2026-04-15 21:06:29,275 | INFO | Skipping weekly_record_1011_2010-05-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,275 | INFO | Processing file: weekly_record_1012_2010-05-10T00-00-00.000.json
    2026-04-15 21:06:29,294 | INFO | Skipping weekly_record_1012_2010-05-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,295 | INFO | Processing file: weekly_record_1013_2010-05-17T00-00-00.000.json
    2026-04-15 21:06:29,321 | INFO | Skipping weekly_record_1013_2010-05-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,322 | INFO | Processing file: weekly_record_1014_2010-05-24T00-00-00.000.json
    2026-04-15 21:06:29,347 | INFO | Skipping weekly_record_1014_2010-05-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,348 | INFO | Processing file: weekly_record_1015_2010-05-31T00-00-00.000.json
    2026-04-15 21:06:29,375 | INFO | Skipping weekly_record_1015_2010-05-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,376 | INFO | Processing file: weekly_record_1016_2010-06-07T00-00-00.000.json
    2026-04-15 21:06:29,408 | INFO | Skipping weekly_record_1016_2010-06-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,409 | INFO | Processing file: weekly_record_1017_2010-06-14T00-00-00.000.json
    2026-04-15 21:06:29,433 | INFO | Skipping weekly_record_1017_2010-06-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,433 | INFO | Processing file: weekly_record_1018_2010-06-21T00-00-00.000.json
    2026-04-15 21:06:29,470 | INFO | Skipping weekly_record_1018_2010-06-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,472 | INFO | Processing file: weekly_record_1019_2010-06-28T00-00-00.000.json
    2026-04-15 21:06:29,494 | INFO | Skipping weekly_record_1019_2010-06-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,498 | INFO | Processing file: weekly_record_101_1992-11-23T00-00-00.000.json
    2026-04-15 21:06:29,529 | INFO | Skipping weekly_record_101_1992-11-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,532 | INFO | Processing file: weekly_record_1020_2010-07-05T00-00-00.000.json
    2026-04-15 21:06:29,551 | INFO | Skipping weekly_record_1020_2010-07-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,553 | INFO | Processing file: weekly_record_1021_2010-07-12T00-00-00.000.json
    2026-04-15 21:06:29,589 | INFO | Skipping weekly_record_1021_2010-07-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,590 | INFO | Processing file: weekly_record_1022_2010-07-19T00-00-00.000.json
    2026-04-15 21:06:29,617 | INFO | Skipping weekly_record_1022_2010-07-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,617 | INFO | Processing file: weekly_record_1023_2010-07-26T00-00-00.000.json
    2026-04-15 21:06:29,646 | INFO | Skipping weekly_record_1023_2010-07-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,648 | INFO | Processing file: weekly_record_1024_2010-08-02T00-00-00.000.json
    2026-04-15 21:06:29,665 | INFO | Skipping weekly_record_1024_2010-08-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,666 | INFO | Processing file: weekly_record_1025_2010-08-09T00-00-00.000.json
    2026-04-15 21:06:29,684 | INFO | Skipping weekly_record_1025_2010-08-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,685 | INFO | Processing file: weekly_record_1026_2010-08-16T00-00-00.000.json
    2026-04-15 21:06:29,706 | INFO | Skipping weekly_record_1026_2010-08-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,707 | INFO | Processing file: weekly_record_1027_2010-08-23T00-00-00.000.json
    2026-04-15 21:06:29,725 | INFO | Skipping weekly_record_1027_2010-08-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,725 | INFO | Processing file: weekly_record_1028_2010-08-30T00-00-00.000.json
    2026-04-15 21:06:29,754 | INFO | Skipping weekly_record_1028_2010-08-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,756 | INFO | Processing file: weekly_record_1029_2010-09-06T00-00-00.000.json
    2026-04-15 21:06:29,770 | INFO | Skipping weekly_record_1029_2010-09-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,774 | INFO | Processing file: weekly_record_102_1992-11-30T00-00-00.000.json
    2026-04-15 21:06:29,790 | INFO | Skipping weekly_record_102_1992-11-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,791 | INFO | Processing file: weekly_record_1030_2010-09-13T00-00-00.000.json
    2026-04-15 21:06:29,811 | INFO | Skipping weekly_record_1030_2010-09-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,813 | INFO | Processing file: weekly_record_1031_2010-09-20T00-00-00.000.json
    2026-04-15 21:06:29,833 | INFO | Skipping weekly_record_1031_2010-09-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,834 | INFO | Processing file: weekly_record_1032_2010-09-27T00-00-00.000.json
    2026-04-15 21:06:29,853 | INFO | Skipping weekly_record_1032_2010-09-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,856 | INFO | Processing file: weekly_record_1033_2010-10-04T00-00-00.000.json
    2026-04-15 21:06:29,879 | INFO | Skipping weekly_record_1033_2010-10-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,880 | INFO | Processing file: weekly_record_1034_2010-10-11T00-00-00.000.json
    2026-04-15 21:06:29,901 | INFO | Skipping weekly_record_1034_2010-10-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,902 | INFO | Processing file: weekly_record_1035_2010-10-18T00-00-00.000.json
    2026-04-15 21:06:29,942 | INFO | Skipping weekly_record_1035_2010-10-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,944 | INFO | Processing file: weekly_record_1036_2010-10-25T00-00-00.000.json
    2026-04-15 21:06:29,965 | INFO | Skipping weekly_record_1036_2010-10-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,966 | INFO | Processing file: weekly_record_1037_2010-11-01T00-00-00.000.json
    2026-04-15 21:06:29,987 | INFO | Skipping weekly_record_1037_2010-11-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:29,989 | INFO | Processing file: weekly_record_1038_2010-11-08T00-00-00.000.json
    2026-04-15 21:06:30,011 | INFO | Skipping weekly_record_1038_2010-11-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,012 | INFO | Processing file: weekly_record_1039_2010-11-15T00-00-00.000.json
    2026-04-15 21:06:30,040 | INFO | Skipping weekly_record_1039_2010-11-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,041 | INFO | Processing file: weekly_record_103_1992-12-07T00-00-00.000.json
    2026-04-15 21:06:30,072 | INFO | Skipping weekly_record_103_1992-12-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,073 | INFO | Processing file: weekly_record_1040_2010-11-22T00-00-00.000.json
    2026-04-15 21:06:30,093 | INFO | Skipping weekly_record_1040_2010-11-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,094 | INFO | Processing file: weekly_record_1041_2010-11-29T00-00-00.000.json
    2026-04-15 21:06:30,119 | INFO | Skipping weekly_record_1041_2010-11-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,120 | INFO | Processing file: weekly_record_1042_2010-12-06T00-00-00.000.json
    2026-04-15 21:06:30,148 | INFO | Skipping weekly_record_1042_2010-12-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,149 | INFO | Processing file: weekly_record_1043_2010-12-13T00-00-00.000.json
    2026-04-15 21:06:30,175 | INFO | Skipping weekly_record_1043_2010-12-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,176 | INFO | Processing file: weekly_record_1044_2010-12-20T00-00-00.000.json
    2026-04-15 21:06:30,204 | INFO | Skipping weekly_record_1044_2010-12-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,206 | INFO | Processing file: weekly_record_1045_2010-12-27T00-00-00.000.json
    2026-04-15 21:06:30,232 | INFO | Skipping weekly_record_1045_2010-12-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,233 | INFO | Processing file: weekly_record_1046_2011-01-03T00-00-00.000.json
    2026-04-15 21:06:30,260 | INFO | Skipping weekly_record_1046_2011-01-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,261 | INFO | Processing file: weekly_record_1047_2011-01-10T00-00-00.000.json
    2026-04-15 21:06:30,307 | INFO | Skipping weekly_record_1047_2011-01-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,308 | INFO | Processing file: weekly_record_1048_2011-01-17T00-00-00.000.json
    2026-04-15 21:06:30,328 | INFO | Skipping weekly_record_1048_2011-01-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,331 | INFO | Processing file: weekly_record_1049_2011-01-24T00-00-00.000.json
    2026-04-15 21:06:30,348 | INFO | Skipping weekly_record_1049_2011-01-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,350 | INFO | Processing file: weekly_record_104_1992-12-14T00-00-00.000.json
    2026-04-15 21:06:30,375 | INFO | Skipping weekly_record_104_1992-12-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,376 | INFO | Processing file: weekly_record_1050_2011-01-31T00-00-00.000.json
    2026-04-15 21:06:30,391 | INFO | Skipping weekly_record_1050_2011-01-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,392 | INFO | Processing file: weekly_record_1051_2011-02-07T00-00-00.000.json
    2026-04-15 21:06:30,418 | INFO | Skipping weekly_record_1051_2011-02-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,418 | INFO | Processing file: weekly_record_1052_2011-02-14T00-00-00.000.json
    2026-04-15 21:06:30,440 | INFO | Skipping weekly_record_1052_2011-02-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,441 | INFO | Processing file: weekly_record_1053_2011-02-21T00-00-00.000.json
    2026-04-15 21:06:30,467 | INFO | Skipping weekly_record_1053_2011-02-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,471 | INFO | Processing file: weekly_record_1054_2011-02-28T00-00-00.000.json
    2026-04-15 21:06:30,499 | INFO | Skipping weekly_record_1054_2011-02-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,500 | INFO | Processing file: weekly_record_1055_2011-03-07T00-00-00.000.json
    2026-04-15 21:06:30,531 | INFO | Skipping weekly_record_1055_2011-03-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,533 | INFO | Processing file: weekly_record_1056_2011-03-14T00-00-00.000.json
    2026-04-15 21:06:30,564 | INFO | Skipping weekly_record_1056_2011-03-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,565 | INFO | Processing file: weekly_record_1057_2011-03-21T00-00-00.000.json
    2026-04-15 21:06:30,594 | INFO | Skipping weekly_record_1057_2011-03-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,595 | INFO | Processing file: weekly_record_1058_2011-03-28T00-00-00.000.json
    2026-04-15 21:06:30,630 | INFO | Skipping weekly_record_1058_2011-03-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,631 | INFO | Processing file: weekly_record_1059_2011-04-04T00-00-00.000.json
    2026-04-15 21:06:30,659 | INFO | Skipping weekly_record_1059_2011-04-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,660 | INFO | Processing file: weekly_record_105_1992-12-21T00-00-00.000.json
    2026-04-15 21:06:30,692 | INFO | Skipping weekly_record_105_1992-12-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,693 | INFO | Processing file: weekly_record_1060_2011-04-11T00-00-00.000.json
    2026-04-15 21:06:30,719 | INFO | Skipping weekly_record_1060_2011-04-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,720 | INFO | Processing file: weekly_record_1061_2011-04-18T00-00-00.000.json
    2026-04-15 21:06:30,741 | INFO | Skipping weekly_record_1061_2011-04-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,742 | INFO | Processing file: weekly_record_1062_2011-04-25T00-00-00.000.json
    2026-04-15 21:06:30,765 | INFO | Skipping weekly_record_1062_2011-04-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,766 | INFO | Processing file: weekly_record_1063_2011-05-02T00-00-00.000.json
    2026-04-15 21:06:30,791 | INFO | Skipping weekly_record_1063_2011-05-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,793 | INFO | Processing file: weekly_record_1064_2011-05-09T00-00-00.000.json
    2026-04-15 21:06:30,818 | INFO | Skipping weekly_record_1064_2011-05-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,820 | INFO | Processing file: weekly_record_1065_2011-05-16T00-00-00.000.json
    2026-04-15 21:06:30,843 | INFO | Skipping weekly_record_1065_2011-05-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,845 | INFO | Processing file: weekly_record_1066_2011-05-23T00-00-00.000.json
    2026-04-15 21:06:30,872 | INFO | Skipping weekly_record_1066_2011-05-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,873 | INFO | Processing file: weekly_record_1067_2011-05-30T00-00-00.000.json
    2026-04-15 21:06:30,903 | INFO | Skipping weekly_record_1067_2011-05-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,909 | INFO | Processing file: weekly_record_1068_2011-06-06T00-00-00.000.json
    2026-04-15 21:06:30,935 | INFO | Skipping weekly_record_1068_2011-06-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,936 | INFO | Processing file: weekly_record_1069_2011-06-13T00-00-00.000.json
    2026-04-15 21:06:30,968 | INFO | Skipping weekly_record_1069_2011-06-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,971 | INFO | Processing file: weekly_record_106_1992-12-28T00-00-00.000.json
    2026-04-15 21:06:30,993 | INFO | Skipping weekly_record_106_1992-12-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:30,994 | INFO | Processing file: weekly_record_1070_2011-06-20T00-00-00.000.json
    2026-04-15 21:06:31,020 | INFO | Skipping weekly_record_1070_2011-06-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,022 | INFO | Processing file: weekly_record_1071_2011-06-27T00-00-00.000.json
    2026-04-15 21:06:31,047 | INFO | Skipping weekly_record_1071_2011-06-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,048 | INFO | Processing file: weekly_record_1072_2011-07-04T00-00-00.000.json
    2026-04-15 21:06:31,077 | INFO | Skipping weekly_record_1072_2011-07-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,078 | INFO | Processing file: weekly_record_1073_2011-07-11T00-00-00.000.json
    2026-04-15 21:06:31,109 | INFO | Skipping weekly_record_1073_2011-07-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,110 | INFO | Processing file: weekly_record_1074_2011-07-18T00-00-00.000.json
    2026-04-15 21:06:31,139 | INFO | Skipping weekly_record_1074_2011-07-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,140 | INFO | Processing file: weekly_record_1075_2011-07-25T00-00-00.000.json
    2026-04-15 21:06:31,163 | INFO | Skipping weekly_record_1075_2011-07-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,166 | INFO | Processing file: weekly_record_1076_2011-08-01T00-00-00.000.json
    2026-04-15 21:06:31,189 | INFO | Skipping weekly_record_1076_2011-08-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,190 | INFO | Processing file: weekly_record_1077_2011-08-08T00-00-00.000.json
    2026-04-15 21:06:31,229 | INFO | Skipping weekly_record_1077_2011-08-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,229 | INFO | Processing file: weekly_record_1078_2011-08-15T00-00-00.000.json
    2026-04-15 21:06:31,256 | INFO | Skipping weekly_record_1078_2011-08-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,256 | INFO | Processing file: weekly_record_1079_2011-08-22T00-00-00.000.json
    2026-04-15 21:06:31,289 | INFO | Skipping weekly_record_1079_2011-08-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,290 | INFO | Processing file: weekly_record_107_1993-01-04T00-00-00.000.json
    2026-04-15 21:06:31,326 | INFO | Skipping weekly_record_107_1993-01-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,327 | INFO | Processing file: weekly_record_1080_2011-08-29T00-00-00.000.json
    2026-04-15 21:06:31,355 | INFO | Skipping weekly_record_1080_2011-08-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,355 | INFO | Processing file: weekly_record_1081_2011-09-05T00-00-00.000.json
    2026-04-15 21:06:31,384 | INFO | Skipping weekly_record_1081_2011-09-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,384 | INFO | Processing file: weekly_record_1082_2011-09-12T00-00-00.000.json
    2026-04-15 21:06:31,400 | INFO | Skipping weekly_record_1082_2011-09-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,402 | INFO | Processing file: weekly_record_1083_2011-09-19T00-00-00.000.json
    2026-04-15 21:06:31,426 | INFO | Skipping weekly_record_1083_2011-09-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,427 | INFO | Processing file: weekly_record_1084_2011-09-26T00-00-00.000.json
    2026-04-15 21:06:31,444 | INFO | Skipping weekly_record_1084_2011-09-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,446 | INFO | Processing file: weekly_record_1085_2011-10-03T00-00-00.000.json
    2026-04-15 21:06:31,470 | INFO | Skipping weekly_record_1085_2011-10-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,471 | INFO | Processing file: weekly_record_1086_2011-10-10T00-00-00.000.json
    2026-04-15 21:06:31,494 | INFO | Skipping weekly_record_1086_2011-10-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,495 | INFO | Processing file: weekly_record_1087_2011-10-17T00-00-00.000.json
    2026-04-15 21:06:31,511 | INFO | Skipping weekly_record_1087_2011-10-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,512 | INFO | Processing file: weekly_record_1088_2011-10-24T00-00-00.000.json
    2026-04-15 21:06:31,537 | INFO | Skipping weekly_record_1088_2011-10-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,539 | INFO | Processing file: weekly_record_1089_2011-10-31T00-00-00.000.json
    2026-04-15 21:06:31,568 | INFO | Skipping weekly_record_1089_2011-10-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,570 | INFO | Processing file: weekly_record_108_1993-01-11T00-00-00.000.json
    2026-04-15 21:06:31,600 | INFO | Skipping weekly_record_108_1993-01-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,600 | INFO | Processing file: weekly_record_1090_2011-11-07T00-00-00.000.json
    2026-04-15 21:06:31,622 | INFO | Skipping weekly_record_1090_2011-11-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,624 | INFO | Processing file: weekly_record_1091_2011-11-14T00-00-00.000.json
    2026-04-15 21:06:31,640 | INFO | Skipping weekly_record_1091_2011-11-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,641 | INFO | Processing file: weekly_record_1092_2011-11-21T00-00-00.000.json
    2026-04-15 21:06:31,671 | INFO | Skipping weekly_record_1092_2011-11-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,673 | INFO | Processing file: weekly_record_1093_2011-11-28T00-00-00.000.json
    2026-04-15 21:06:31,699 | INFO | Skipping weekly_record_1093_2011-11-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,700 | INFO | Processing file: weekly_record_1094_2011-12-05T00-00-00.000.json
    2026-04-15 21:06:31,725 | INFO | Skipping weekly_record_1094_2011-12-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,726 | INFO | Processing file: weekly_record_1095_2011-12-12T00-00-00.000.json
    2026-04-15 21:06:31,763 | INFO | Skipping weekly_record_1095_2011-12-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,764 | INFO | Processing file: weekly_record_1096_2011-12-19T00-00-00.000.json
    2026-04-15 21:06:31,787 | INFO | Skipping weekly_record_1096_2011-12-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,788 | INFO | Processing file: weekly_record_1097_2011-12-26T00-00-00.000.json
    2026-04-15 21:06:31,811 | INFO | Skipping weekly_record_1097_2011-12-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,812 | INFO | Processing file: weekly_record_1098_2012-01-02T00-00-00.000.json
    2026-04-15 21:06:31,843 | INFO | Skipping weekly_record_1098_2012-01-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,844 | INFO | Processing file: weekly_record_1099_2012-01-09T00-00-00.000.json
    2026-04-15 21:06:31,870 | INFO | Skipping weekly_record_1099_2012-01-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,872 | INFO | Processing file: weekly_record_109_1993-01-18T00-00-00.000.json
    2026-04-15 21:06:31,909 | INFO | Skipping weekly_record_109_1993-01-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,911 | INFO | Processing file: weekly_record_10_1991-02-25T00-00-00.000.json
    2026-04-15 21:06:31,934 | INFO | Skipping weekly_record_10_1991-02-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,935 | INFO | Processing file: weekly_record_1100_2012-01-16T00-00-00.000.json
    2026-04-15 21:06:31,953 | INFO | Skipping weekly_record_1100_2012-01-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,955 | INFO | Processing file: weekly_record_1101_2012-01-23T00-00-00.000.json
    2026-04-15 21:06:31,981 | INFO | Skipping weekly_record_1101_2012-01-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:31,982 | INFO | Processing file: weekly_record_1102_2012-01-30T00-00-00.000.json
    2026-04-15 21:06:31,999 | INFO | Skipping weekly_record_1102_2012-01-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,000 | INFO | Processing file: weekly_record_1103_2012-02-06T00-00-00.000.json
    2026-04-15 21:06:32,025 | INFO | Skipping weekly_record_1103_2012-02-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,026 | INFO | Processing file: weekly_record_1104_2012-02-13T00-00-00.000.json
    2026-04-15 21:06:32,045 | INFO | Skipping weekly_record_1104_2012-02-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,049 | INFO | Processing file: weekly_record_1105_2012-02-20T00-00-00.000.json
    2026-04-15 21:06:32,074 | INFO | Skipping weekly_record_1105_2012-02-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,076 | INFO | Processing file: weekly_record_1106_2012-02-27T00-00-00.000.json
    2026-04-15 21:06:32,102 | INFO | Skipping weekly_record_1106_2012-02-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,103 | INFO | Processing file: weekly_record_1107_2012-03-05T00-00-00.000.json
    2026-04-15 21:06:32,130 | INFO | Skipping weekly_record_1107_2012-03-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,131 | INFO | Processing file: weekly_record_1108_2012-03-12T00-00-00.000.json
    2026-04-15 21:06:32,148 | INFO | Skipping weekly_record_1108_2012-03-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,148 | INFO | Processing file: weekly_record_1109_2012-03-19T00-00-00.000.json
    2026-04-15 21:06:32,175 | INFO | Skipping weekly_record_1109_2012-03-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,176 | INFO | Processing file: weekly_record_110_1993-01-25T00-00-00.000.json
    2026-04-15 21:06:32,192 | INFO | Skipping weekly_record_110_1993-01-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,192 | INFO | Processing file: weekly_record_1110_2012-03-26T00-00-00.000.json
    2026-04-15 21:06:32,215 | INFO | Skipping weekly_record_1110_2012-03-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,216 | INFO | Processing file: weekly_record_1111_2012-04-02T00-00-00.000.json
    2026-04-15 21:06:32,242 | INFO | Skipping weekly_record_1111_2012-04-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,243 | INFO | Processing file: weekly_record_1112_2012-04-09T00-00-00.000.json
    2026-04-15 21:06:32,256 | INFO | Skipping weekly_record_1112_2012-04-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,257 | INFO | Processing file: weekly_record_1113_2012-04-16T00-00-00.000.json
    2026-04-15 21:06:32,278 | INFO | Skipping weekly_record_1113_2012-04-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,279 | INFO | Processing file: weekly_record_1114_2012-04-23T00-00-00.000.json
    2026-04-15 21:06:32,346 | INFO | Skipping weekly_record_1114_2012-04-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,352 | INFO | Processing file: weekly_record_1115_2012-04-30T00-00-00.000.json
    2026-04-15 21:06:32,417 | INFO | Skipping weekly_record_1115_2012-04-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,420 | INFO | Processing file: weekly_record_1116_2012-05-07T00-00-00.000.json
    2026-04-15 21:06:32,466 | INFO | Skipping weekly_record_1116_2012-05-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,491 | INFO | Processing file: weekly_record_1117_2012-05-14T00-00-00.000.json
    2026-04-15 21:06:32,534 | INFO | Skipping weekly_record_1117_2012-05-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,537 | INFO | Processing file: weekly_record_1118_2012-05-21T00-00-00.000.json
    2026-04-15 21:06:32,560 | INFO | Skipping weekly_record_1118_2012-05-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,567 | INFO | Processing file: weekly_record_1119_2012-05-28T00-00-00.000.json
    2026-04-15 21:06:32,588 | INFO | Skipping weekly_record_1119_2012-05-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,604 | INFO | Processing file: weekly_record_111_1993-02-01T00-00-00.000.json
    2026-04-15 21:06:32,632 | INFO | Skipping weekly_record_111_1993-02-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,633 | INFO | Processing file: weekly_record_1120_2012-06-04T00-00-00.000.json
    2026-04-15 21:06:32,669 | INFO | Skipping weekly_record_1120_2012-06-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,670 | INFO | Processing file: weekly_record_1121_2012-06-11T00-00-00.000.json
    2026-04-15 21:06:32,694 | INFO | Skipping weekly_record_1121_2012-06-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,696 | INFO | Processing file: weekly_record_1122_2012-06-18T00-00-00.000.json
    2026-04-15 21:06:32,732 | INFO | Skipping weekly_record_1122_2012-06-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,737 | INFO | Processing file: weekly_record_1123_2012-06-25T00-00-00.000.json
    2026-04-15 21:06:32,762 | INFO | Skipping weekly_record_1123_2012-06-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,764 | INFO | Processing file: weekly_record_1124_2012-07-02T00-00-00.000.json
    2026-04-15 21:06:32,791 | INFO | Skipping weekly_record_1124_2012-07-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,794 | INFO | Processing file: weekly_record_1125_2012-07-09T00-00-00.000.json
    2026-04-15 21:06:32,825 | INFO | Skipping weekly_record_1125_2012-07-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,826 | INFO | Processing file: weekly_record_1126_2012-07-16T00-00-00.000.json
    2026-04-15 21:06:32,850 | INFO | Skipping weekly_record_1126_2012-07-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,852 | INFO | Processing file: weekly_record_1127_2012-07-23T00-00-00.000.json
    2026-04-15 21:06:32,878 | INFO | Skipping weekly_record_1127_2012-07-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,884 | INFO | Processing file: weekly_record_1128_2012-07-30T00-00-00.000.json
    2026-04-15 21:06:32,908 | INFO | Skipping weekly_record_1128_2012-07-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,910 | INFO | Processing file: weekly_record_1129_2012-08-06T00-00-00.000.json
    2026-04-15 21:06:32,929 | INFO | Skipping weekly_record_1129_2012-08-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,932 | INFO | Processing file: weekly_record_112_1993-02-08T00-00-00.000.json
    2026-04-15 21:06:32,945 | INFO | Skipping weekly_record_112_1993-02-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,949 | INFO | Processing file: weekly_record_1130_2012-08-13T00-00-00.000.json
    2026-04-15 21:06:32,971 | INFO | Skipping weekly_record_1130_2012-08-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:32,974 | INFO | Processing file: weekly_record_1131_2012-08-20T00-00-00.000.json
    2026-04-15 21:06:33,003 | INFO | Skipping weekly_record_1131_2012-08-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,004 | INFO | Processing file: weekly_record_1132_2012-08-27T00-00-00.000.json
    2026-04-15 21:06:33,031 | INFO | Skipping weekly_record_1132_2012-08-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,032 | INFO | Processing file: weekly_record_1133_2012-09-03T00-00-00.000.json
    2026-04-15 21:06:33,058 | INFO | Skipping weekly_record_1133_2012-09-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,059 | INFO | Processing file: weekly_record_1134_2012-09-10T00-00-00.000.json
    2026-04-15 21:06:33,077 | INFO | Skipping weekly_record_1134_2012-09-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,077 | INFO | Processing file: weekly_record_1135_2012-09-17T00-00-00.000.json
    2026-04-15 21:06:33,105 | INFO | Skipping weekly_record_1135_2012-09-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,109 | INFO | Processing file: weekly_record_1136_2012-09-24T00-00-00.000.json
    2026-04-15 21:06:33,131 | INFO | Skipping weekly_record_1136_2012-09-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,133 | INFO | Processing file: weekly_record_1137_2012-10-01T00-00-00.000.json
    2026-04-15 21:06:33,156 | INFO | Skipping weekly_record_1137_2012-10-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,157 | INFO | Processing file: weekly_record_1138_2012-10-08T00-00-00.000.json
    2026-04-15 21:06:33,172 | INFO | Skipping weekly_record_1138_2012-10-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,174 | INFO | Processing file: weekly_record_1139_2012-10-15T00-00-00.000.json
    2026-04-15 21:06:33,200 | INFO | Skipping weekly_record_1139_2012-10-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,200 | INFO | Processing file: weekly_record_113_1993-02-15T00-00-00.000.json
    2026-04-15 21:06:33,231 | INFO | Skipping weekly_record_113_1993-02-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,232 | INFO | Processing file: weekly_record_1140_2012-10-22T00-00-00.000.json
    2026-04-15 21:06:33,249 | INFO | Skipping weekly_record_1140_2012-10-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,250 | INFO | Processing file: weekly_record_1141_2012-10-29T00-00-00.000.json
    2026-04-15 21:06:33,275 | INFO | Skipping weekly_record_1141_2012-10-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,276 | INFO | Processing file: weekly_record_1142_2012-11-05T00-00-00.000.json
    2026-04-15 21:06:33,301 | INFO | Skipping weekly_record_1142_2012-11-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,302 | INFO | Processing file: weekly_record_1143_2012-11-12T00-00-00.000.json
    2026-04-15 21:06:33,330 | INFO | Skipping weekly_record_1143_2012-11-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,331 | INFO | Processing file: weekly_record_1144_2012-11-19T00-00-00.000.json
    2026-04-15 21:06:33,346 | INFO | Skipping weekly_record_1144_2012-11-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,348 | INFO | Processing file: weekly_record_1145_2012-11-26T00-00-00.000.json
    2026-04-15 21:06:33,374 | INFO | Skipping weekly_record_1145_2012-11-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,375 | INFO | Processing file: weekly_record_1146_2012-12-03T00-00-00.000.json
    2026-04-15 21:06:33,391 | INFO | Skipping weekly_record_1146_2012-12-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,392 | INFO | Processing file: weekly_record_1147_2012-12-10T00-00-00.000.json
    2026-04-15 21:06:33,417 | INFO | Skipping weekly_record_1147_2012-12-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,418 | INFO | Processing file: weekly_record_1148_2012-12-17T00-00-00.000.json
    2026-04-15 21:06:33,436 | INFO | Skipping weekly_record_1148_2012-12-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,438 | INFO | Processing file: weekly_record_1149_2012-12-24T00-00-00.000.json
    2026-04-15 21:06:33,462 | INFO | Skipping weekly_record_1149_2012-12-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,463 | INFO | Processing file: weekly_record_114_1993-02-22T00-00-00.000.json
    2026-04-15 21:06:33,480 | INFO | Skipping weekly_record_114_1993-02-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,483 | INFO | Processing file: weekly_record_1150_2012-12-31T00-00-00.000.json
    2026-04-15 21:06:33,507 | INFO | Skipping weekly_record_1150_2012-12-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,508 | INFO | Processing file: weekly_record_1151_2013-01-07T00-00-00.000.json
    2026-04-15 21:06:33,526 | INFO | Skipping weekly_record_1151_2013-01-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,528 | INFO | Processing file: weekly_record_1152_2013-01-14T00-00-00.000.json
    2026-04-15 21:06:33,553 | INFO | Skipping weekly_record_1152_2013-01-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,555 | INFO | Processing file: weekly_record_1153_2013-01-21T00-00-00.000.json
    2026-04-15 21:06:33,570 | INFO | Skipping weekly_record_1153_2013-01-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,571 | INFO | Processing file: weekly_record_1154_2013-01-28T00-00-00.000.json
    2026-04-15 21:06:33,597 | INFO | Skipping weekly_record_1154_2013-01-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,598 | INFO | Processing file: weekly_record_1155_2013-02-04T00-00-00.000.json
    2026-04-15 21:06:33,616 | INFO | Skipping weekly_record_1155_2013-02-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,617 | INFO | Processing file: weekly_record_1156_2013-02-11T00-00-00.000.json
    2026-04-15 21:06:33,647 | INFO | Skipping weekly_record_1156_2013-02-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,648 | INFO | Processing file: weekly_record_1157_2013-02-18T00-00-00.000.json
    2026-04-15 21:06:33,667 | INFO | Skipping weekly_record_1157_2013-02-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,668 | INFO | Processing file: weekly_record_1158_2013-02-25T00-00-00.000.json
    2026-04-15 21:06:33,693 | INFO | Skipping weekly_record_1158_2013-02-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,694 | INFO | Processing file: weekly_record_1159_2013-03-04T00-00-00.000.json
    2026-04-15 21:06:33,724 | INFO | Skipping weekly_record_1159_2013-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,726 | INFO | Processing file: weekly_record_115_1993-03-01T00-00-00.000.json
    2026-04-15 21:06:33,775 | INFO | Skipping weekly_record_115_1993-03-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,775 | INFO | Processing file: weekly_record_1160_2013-03-11T00-00-00.000.json
    2026-04-15 21:06:33,800 | INFO | Skipping weekly_record_1160_2013-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,801 | INFO | Processing file: weekly_record_1161_2013-03-18T00-00-00.000.json
    2026-04-15 21:06:33,836 | INFO | Skipping weekly_record_1161_2013-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,837 | INFO | Processing file: weekly_record_1162_2013-03-25T00-00-00.000.json
    2026-04-15 21:06:33,866 | INFO | Skipping weekly_record_1162_2013-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,867 | INFO | Processing file: weekly_record_1163_2013-04-01T00-00-00.000.json
    2026-04-15 21:06:33,885 | INFO | Skipping weekly_record_1163_2013-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,886 | INFO | Processing file: weekly_record_1164_2013-04-08T00-00-00.000.json
    2026-04-15 21:06:33,914 | INFO | Skipping weekly_record_1164_2013-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,916 | INFO | Processing file: weekly_record_1165_2013-04-15T00-00-00.000.json
    2026-04-15 21:06:33,936 | INFO | Skipping weekly_record_1165_2013-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,937 | INFO | Processing file: weekly_record_1166_2013-04-22T00-00-00.000.json
    2026-04-15 21:06:33,962 | INFO | Skipping weekly_record_1166_2013-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:33,963 | INFO | Processing file: weekly_record_1167_2013-04-29T00-00-00.000.json
    2026-04-15 21:06:33,998 | INFO | Skipping weekly_record_1167_2013-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,000 | INFO | Processing file: weekly_record_1168_2013-05-06T00-00-00.000.json
    2026-04-15 21:06:34,036 | INFO | Skipping weekly_record_1168_2013-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,037 | INFO | Processing file: weekly_record_1169_2013-05-13T00-00-00.000.json
    2026-04-15 21:06:34,061 | INFO | Skipping weekly_record_1169_2013-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,062 | INFO | Processing file: weekly_record_116_1993-03-08T00-00-00.000.json
    2026-04-15 21:06:34,088 | INFO | Skipping weekly_record_116_1993-03-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,089 | INFO | Processing file: weekly_record_1170_2013-05-20T00-00-00.000.json
    2026-04-15 21:06:34,107 | INFO | Skipping weekly_record_1170_2013-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,108 | INFO | Processing file: weekly_record_1171_2013-05-27T00-00-00.000.json
    2026-04-15 21:06:34,133 | INFO | Skipping weekly_record_1171_2013-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,135 | INFO | Processing file: weekly_record_1172_2013-06-03T00-00-00.000.json
    2026-04-15 21:06:34,164 | INFO | Skipping weekly_record_1172_2013-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,169 | INFO | Processing file: weekly_record_1173_2013-06-10T00-00-00.000.json
    2026-04-15 21:06:34,191 | INFO | Skipping weekly_record_1173_2013-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,192 | INFO | Processing file: weekly_record_1174_2013-06-17T00-00-00.000.json
    2026-04-15 21:06:34,212 | INFO | Skipping weekly_record_1174_2013-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,216 | INFO | Processing file: weekly_record_1175_2013-06-24T00-00-00.000.json
    2026-04-15 21:06:34,236 | INFO | Skipping weekly_record_1175_2013-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,240 | INFO | Processing file: weekly_record_1176_2013-07-01T00-00-00.000.json
    2026-04-15 21:06:34,255 | INFO | Skipping weekly_record_1176_2013-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,258 | INFO | Processing file: weekly_record_1177_2013-07-08T00-00-00.000.json
    2026-04-15 21:06:34,281 | INFO | Skipping weekly_record_1177_2013-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,283 | INFO | Processing file: weekly_record_1178_2013-07-15T00-00-00.000.json
    2026-04-15 21:06:34,302 | INFO | Skipping weekly_record_1178_2013-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,304 | INFO | Processing file: weekly_record_1179_2013-07-22T00-00-00.000.json
    2026-04-15 21:06:34,326 | INFO | Skipping weekly_record_1179_2013-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,331 | INFO | Processing file: weekly_record_117_1993-03-15T00-00-00.000.json
    2026-04-15 21:06:34,353 | INFO | Skipping weekly_record_117_1993-03-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,357 | INFO | Processing file: weekly_record_1180_2013-07-29T00-00-00.000.json
    2026-04-15 21:06:34,380 | INFO | Skipping weekly_record_1180_2013-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,381 | INFO | Processing file: weekly_record_1181_2013-08-05T00-00-00.000.json
    2026-04-15 21:06:34,406 | INFO | Skipping weekly_record_1181_2013-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,410 | INFO | Processing file: weekly_record_1182_2013-08-12T00-00-00.000.json
    2026-04-15 21:06:34,438 | INFO | Skipping weekly_record_1182_2013-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,443 | INFO | Processing file: weekly_record_1183_2013-08-19T00-00-00.000.json
    2026-04-15 21:06:34,465 | INFO | Skipping weekly_record_1183_2013-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,466 | INFO | Processing file: weekly_record_1184_2013-08-26T00-00-00.000.json
    2026-04-15 21:06:34,490 | INFO | Skipping weekly_record_1184_2013-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,491 | INFO | Processing file: weekly_record_1185_2013-09-02T00-00-00.000.json
    2026-04-15 21:06:34,506 | INFO | Skipping weekly_record_1185_2013-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,508 | INFO | Processing file: weekly_record_1186_2013-09-09T00-00-00.000.json
    2026-04-15 21:06:34,533 | INFO | Skipping weekly_record_1186_2013-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,534 | INFO | Processing file: weekly_record_1187_2013-09-16T00-00-00.000.json
    2026-04-15 21:06:34,558 | INFO | Skipping weekly_record_1187_2013-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,559 | INFO | Processing file: weekly_record_1188_2013-09-23T00-00-00.000.json
    2026-04-15 21:06:34,576 | INFO | Skipping weekly_record_1188_2013-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,577 | INFO | Processing file: weekly_record_1189_2013-09-30T00-00-00.000.json
    2026-04-15 21:06:34,626 | INFO | Skipping weekly_record_1189_2013-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,628 | INFO | Processing file: weekly_record_118_1993-03-22T00-00-00.000.json
    2026-04-15 21:06:34,649 | INFO | Skipping weekly_record_118_1993-03-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,652 | INFO | Processing file: weekly_record_1190_2013-10-07T00-00-00.000.json
    2026-04-15 21:06:34,674 | INFO | Skipping weekly_record_1190_2013-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,675 | INFO | Processing file: weekly_record_1191_2013-10-14T00-00-00.000.json
    2026-04-15 21:06:34,702 | INFO | Skipping weekly_record_1191_2013-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,705 | INFO | Processing file: weekly_record_1192_2013-10-21T00-00-00.000.json
    2026-04-15 21:06:34,733 | INFO | Skipping weekly_record_1192_2013-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,734 | INFO | Processing file: weekly_record_1193_2013-10-28T00-00-00.000.json
    2026-04-15 21:06:34,759 | INFO | Skipping weekly_record_1193_2013-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,760 | INFO | Processing file: weekly_record_1194_2013-11-04T00-00-00.000.json
    2026-04-15 21:06:34,784 | INFO | Skipping weekly_record_1194_2013-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,785 | INFO | Processing file: weekly_record_1195_2013-11-11T00-00-00.000.json
    2026-04-15 21:06:34,801 | INFO | Skipping weekly_record_1195_2013-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,802 | INFO | Processing file: weekly_record_1196_2013-11-18T00-00-00.000.json
    2026-04-15 21:06:34,827 | INFO | Skipping weekly_record_1196_2013-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,828 | INFO | Processing file: weekly_record_1197_2013-11-25T00-00-00.000.json
    2026-04-15 21:06:34,845 | INFO | Skipping weekly_record_1197_2013-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,845 | INFO | Processing file: weekly_record_1198_2013-12-02T00-00-00.000.json
    2026-04-15 21:06:34,871 | INFO | Skipping weekly_record_1198_2013-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,871 | INFO | Processing file: weekly_record_1199_2013-12-09T00-00-00.000.json
    2026-04-15 21:06:34,895 | INFO | Skipping weekly_record_1199_2013-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,896 | INFO | Processing file: weekly_record_119_1993-03-29T00-00-00.000.json
    2026-04-15 21:06:34,913 | INFO | Skipping weekly_record_119_1993-03-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,915 | INFO | Processing file: weekly_record_11_1991-03-04T00-00-00.000.json
    2026-04-15 21:06:34,939 | INFO | Skipping weekly_record_11_1991-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,940 | INFO | Processing file: weekly_record_1200_2013-12-16T00-00-00.000.json
    2026-04-15 21:06:34,961 | INFO | Skipping weekly_record_1200_2013-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,961 | INFO | Processing file: weekly_record_1201_2013-12-23T00-00-00.000.json
    2026-04-15 21:06:34,988 | INFO | Skipping weekly_record_1201_2013-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:34,989 | INFO | Processing file: weekly_record_1202_2013-12-30T00-00-00.000.json
    2026-04-15 21:06:35,004 | INFO | Skipping weekly_record_1202_2013-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,010 | INFO | Processing file: weekly_record_1203_2014-01-06T00-00-00.000.json
    2026-04-15 21:06:35,031 | INFO | Skipping weekly_record_1203_2014-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,032 | INFO | Processing file: weekly_record_1204_2014-01-13T00-00-00.000.json
    2026-04-15 21:06:35,057 | INFO | Skipping weekly_record_1204_2014-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,058 | INFO | Processing file: weekly_record_1205_2014-01-20T00-00-00.000.json
    2026-04-15 21:06:35,083 | INFO | Skipping weekly_record_1205_2014-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,084 | INFO | Processing file: weekly_record_1206_2014-01-27T00-00-00.000.json
    2026-04-15 21:06:35,100 | INFO | Skipping weekly_record_1206_2014-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,101 | INFO | Processing file: weekly_record_1207_2014-02-03T00-00-00.000.json
    2026-04-15 21:06:35,130 | INFO | Skipping weekly_record_1207_2014-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,131 | INFO | Processing file: weekly_record_1208_2014-02-10T00-00-00.000.json
    2026-04-15 21:06:35,148 | INFO | Skipping weekly_record_1208_2014-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,149 | INFO | Processing file: weekly_record_1209_2014-02-17T00-00-00.000.json
    2026-04-15 21:06:35,174 | INFO | Skipping weekly_record_1209_2014-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,175 | INFO | Processing file: weekly_record_120_1993-04-05T00-00-00.000.json
    2026-04-15 21:06:35,194 | INFO | Skipping weekly_record_120_1993-04-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,195 | INFO | Processing file: weekly_record_1210_2014-02-24T00-00-00.000.json
    2026-04-15 21:06:35,221 | INFO | Skipping weekly_record_1210_2014-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,221 | INFO | Processing file: weekly_record_1211_2014-03-03T00-00-00.000.json
    2026-04-15 21:06:35,240 | INFO | Skipping weekly_record_1211_2014-03-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,241 | INFO | Processing file: weekly_record_1212_2014-03-10T00-00-00.000.json
    2026-04-15 21:06:35,269 | INFO | Skipping weekly_record_1212_2014-03-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,270 | INFO | Processing file: weekly_record_1213_2014-03-17T00-00-00.000.json
    2026-04-15 21:06:35,288 | INFO | Skipping weekly_record_1213_2014-03-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,289 | INFO | Processing file: weekly_record_1214_2014-03-24T00-00-00.000.json
    2026-04-15 21:06:35,315 | INFO | Skipping weekly_record_1214_2014-03-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,316 | INFO | Processing file: weekly_record_1215_2014-03-31T00-00-00.000.json
    2026-04-15 21:06:35,334 | INFO | Skipping weekly_record_1215_2014-03-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,336 | INFO | Processing file: weekly_record_1216_2014-04-07T00-00-00.000.json
    2026-04-15 21:06:35,361 | INFO | Skipping weekly_record_1216_2014-04-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,362 | INFO | Processing file: weekly_record_1217_2014-04-14T00-00-00.000.json
    2026-04-15 21:06:35,383 | INFO | Skipping weekly_record_1217_2014-04-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,384 | INFO | Processing file: weekly_record_1218_2014-04-21T00-00-00.000.json
    2026-04-15 21:06:35,408 | INFO | Skipping weekly_record_1218_2014-04-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,409 | INFO | Processing file: weekly_record_1219_2014-04-28T00-00-00.000.json
    2026-04-15 21:06:35,427 | INFO | Skipping weekly_record_1219_2014-04-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,428 | INFO | Processing file: weekly_record_121_1993-04-12T00-00-00.000.json
    2026-04-15 21:06:35,454 | INFO | Skipping weekly_record_121_1993-04-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,455 | INFO | Processing file: weekly_record_1220_2014-05-05T00-00-00.000.json
    2026-04-15 21:06:35,473 | INFO | Skipping weekly_record_1220_2014-05-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,474 | INFO | Processing file: weekly_record_1221_2014-05-12T00-00-00.000.json
    2026-04-15 21:06:35,564 | INFO | Skipping weekly_record_1221_2014-05-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,565 | INFO | Processing file: weekly_record_1222_2014-05-19T00-00-00.000.json
    2026-04-15 21:06:35,590 | INFO | Skipping weekly_record_1222_2014-05-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,591 | INFO | Processing file: weekly_record_1223_2014-05-26T00-00-00.000.json
    2026-04-15 21:06:35,612 | INFO | Skipping weekly_record_1223_2014-05-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,614 | INFO | Processing file: weekly_record_1224_2014-06-02T00-00-00.000.json
    2026-04-15 21:06:35,629 | INFO | Skipping weekly_record_1224_2014-06-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,631 | INFO | Processing file: weekly_record_1225_2014-06-09T00-00-00.000.json
    2026-04-15 21:06:35,656 | INFO | Skipping weekly_record_1225_2014-06-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,657 | INFO | Processing file: weekly_record_1226_2014-06-16T00-00-00.000.json
    2026-04-15 21:06:35,686 | INFO | Skipping weekly_record_1226_2014-06-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,687 | INFO | Processing file: weekly_record_1227_2014-06-23T00-00-00.000.json
    2026-04-15 21:06:35,711 | INFO | Skipping weekly_record_1227_2014-06-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,712 | INFO | Processing file: weekly_record_1228_2014-06-30T00-00-00.000.json
    2026-04-15 21:06:35,728 | INFO | Skipping weekly_record_1228_2014-06-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,729 | INFO | Processing file: weekly_record_1229_2014-07-07T00-00-00.000.json
    2026-04-15 21:06:35,750 | INFO | Skipping weekly_record_1229_2014-07-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,751 | INFO | Processing file: weekly_record_122_1993-04-19T00-00-00.000.json
    2026-04-15 21:06:35,768 | INFO | Skipping weekly_record_122_1993-04-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,769 | INFO | Processing file: weekly_record_1230_2014-07-14T00-00-00.000.json
    2026-04-15 21:06:35,786 | INFO | Skipping weekly_record_1230_2014-07-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,787 | INFO | Processing file: weekly_record_1231_2014-07-21T00-00-00.000.json
    2026-04-15 21:06:35,814 | INFO | Skipping weekly_record_1231_2014-07-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,815 | INFO | Processing file: weekly_record_1232_2014-07-28T00-00-00.000.json
    2026-04-15 21:06:35,832 | INFO | Skipping weekly_record_1232_2014-07-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,833 | INFO | Processing file: weekly_record_1233_2014-08-04T00-00-00.000.json
    2026-04-15 21:06:35,860 | INFO | Skipping weekly_record_1233_2014-08-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,860 | INFO | Processing file: weekly_record_1234_2014-08-11T00-00-00.000.json
    2026-04-15 21:06:35,885 | INFO | Skipping weekly_record_1234_2014-08-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,886 | INFO | Processing file: weekly_record_1235_2014-08-18T00-00-00.000.json
    2026-04-15 21:06:35,903 | INFO | Skipping weekly_record_1235_2014-08-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,904 | INFO | Processing file: weekly_record_1236_2014-08-25T00-00-00.000.json
    2026-04-15 21:06:35,926 | INFO | Skipping weekly_record_1236_2014-08-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,927 | INFO | Processing file: weekly_record_1237_2014-09-01T00-00-00.000.json
    2026-04-15 21:06:35,945 | INFO | Skipping weekly_record_1237_2014-09-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,945 | INFO | Processing file: weekly_record_1238_2014-09-08T00-00-00.000.json
    2026-04-15 21:06:35,975 | INFO | Skipping weekly_record_1238_2014-09-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,976 | INFO | Processing file: weekly_record_1239_2014-09-15T00-00-00.000.json
    2026-04-15 21:06:35,993 | INFO | Skipping weekly_record_1239_2014-09-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:35,993 | INFO | Processing file: weekly_record_123_1993-04-26T00-00-00.000.json
    2026-04-15 21:06:36,016 | INFO | Skipping weekly_record_123_1993-04-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,017 | INFO | Processing file: weekly_record_1240_2014-09-22T00-00-00.000.json
    2026-04-15 21:06:36,036 | INFO | Skipping weekly_record_1240_2014-09-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,037 | INFO | Processing file: weekly_record_1241_2014-09-29T00-00-00.000.json
    2026-04-15 21:06:36,061 | INFO | Skipping weekly_record_1241_2014-09-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,062 | INFO | Processing file: weekly_record_1242_2014-10-06T00-00-00.000.json
    2026-04-15 21:06:36,078 | INFO | Skipping weekly_record_1242_2014-10-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,079 | INFO | Processing file: weekly_record_1243_2014-10-13T00-00-00.000.json
    2026-04-15 21:06:36,105 | INFO | Skipping weekly_record_1243_2014-10-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,106 | INFO | Processing file: weekly_record_1244_2014-10-20T00-00-00.000.json
    2026-04-15 21:06:36,124 | INFO | Skipping weekly_record_1244_2014-10-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,125 | INFO | Processing file: weekly_record_1245_2014-10-27T00-00-00.000.json
    2026-04-15 21:06:36,151 | INFO | Skipping weekly_record_1245_2014-10-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,152 | INFO | Processing file: weekly_record_1246_2014-11-03T00-00-00.000.json
    2026-04-15 21:06:36,172 | INFO | Skipping weekly_record_1246_2014-11-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,172 | INFO | Processing file: weekly_record_1247_2014-11-10T00-00-00.000.json
    2026-04-15 21:06:36,198 | INFO | Skipping weekly_record_1247_2014-11-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,199 | INFO | Processing file: weekly_record_1248_2014-11-17T00-00-00.000.json
    2026-04-15 21:06:36,216 | INFO | Skipping weekly_record_1248_2014-11-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,217 | INFO | Processing file: weekly_record_1249_2014-11-24T00-00-00.000.json
    2026-04-15 21:06:36,242 | INFO | Skipping weekly_record_1249_2014-11-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,243 | INFO | Processing file: weekly_record_124_1993-05-03T00-00-00.000.json
    2026-04-15 21:06:36,260 | INFO | Skipping weekly_record_124_1993-05-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,261 | INFO | Processing file: weekly_record_1250_2014-12-01T00-00-00.000.json
    2026-04-15 21:06:36,287 | INFO | Skipping weekly_record_1250_2014-12-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,288 | INFO | Processing file: weekly_record_1251_2014-12-08T00-00-00.000.json
    2026-04-15 21:06:36,307 | INFO | Skipping weekly_record_1251_2014-12-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,307 | INFO | Processing file: weekly_record_1252_2014-12-15T00-00-00.000.json
    2026-04-15 21:06:36,333 | INFO | Skipping weekly_record_1252_2014-12-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,333 | INFO | Processing file: weekly_record_1253_2014-12-22T00-00-00.000.json
    2026-04-15 21:06:36,350 | INFO | Skipping weekly_record_1253_2014-12-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,351 | INFO | Processing file: weekly_record_1254_2014-12-29T00-00-00.000.json
    2026-04-15 21:06:36,377 | INFO | Skipping weekly_record_1254_2014-12-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,378 | INFO | Processing file: weekly_record_1255_2015-01-05T00-00-00.000.json
    2026-04-15 21:06:36,397 | INFO | Skipping weekly_record_1255_2015-01-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,398 | INFO | Processing file: weekly_record_1256_2015-01-12T00-00-00.000.json
    2026-04-15 21:06:36,428 | INFO | Skipping weekly_record_1256_2015-01-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,429 | INFO | Processing file: weekly_record_1257_2015-01-19T00-00-00.000.json
    2026-04-15 21:06:36,450 | INFO | Skipping weekly_record_1257_2015-01-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,453 | INFO | Processing file: weekly_record_1258_2015-01-26T00-00-00.000.json
    2026-04-15 21:06:36,480 | INFO | Skipping weekly_record_1258_2015-01-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,481 | INFO | Processing file: weekly_record_1259_2015-02-02T00-00-00.000.json
    2026-04-15 21:06:36,499 | INFO | Skipping weekly_record_1259_2015-02-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,499 | INFO | Processing file: weekly_record_125_1993-05-10T00-00-00.000.json
    2026-04-15 21:06:36,520 | INFO | Skipping weekly_record_125_1993-05-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,521 | INFO | Processing file: weekly_record_1260_2015-02-09T00-00-00.000.json
    2026-04-15 21:06:36,549 | INFO | Skipping weekly_record_1260_2015-02-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,550 | INFO | Processing file: weekly_record_1261_2015-02-16T00-00-00.000.json
    2026-04-15 21:06:36,588 | INFO | Skipping weekly_record_1261_2015-02-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,589 | INFO | Processing file: weekly_record_1262_2015-02-23T00-00-00.000.json
    2026-04-15 21:06:36,606 | INFO | Skipping weekly_record_1262_2015-02-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,607 | INFO | Processing file: weekly_record_1263_2015-03-02T00-00-00.000.json
    2026-04-15 21:06:36,628 | INFO | Skipping weekly_record_1263_2015-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,630 | INFO | Processing file: weekly_record_1264_2015-03-09T00-00-00.000.json
    2026-04-15 21:06:36,645 | INFO | Skipping weekly_record_1264_2015-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,646 | INFO | Processing file: weekly_record_1265_2015-03-16T00-00-00.000.json
    2026-04-15 21:06:36,672 | INFO | Skipping weekly_record_1265_2015-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,673 | INFO | Processing file: weekly_record_1266_2015-03-23T00-00-00.000.json
    2026-04-15 21:06:36,710 | INFO | Skipping weekly_record_1266_2015-03-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,711 | INFO | Processing file: weekly_record_1267_2015-03-30T00-00-00.000.json
    2026-04-15 21:06:36,743 | INFO | Skipping weekly_record_1267_2015-03-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,745 | INFO | Processing file: weekly_record_1268_2015-04-06T00-00-00.000.json
    2026-04-15 21:06:36,761 | INFO | Skipping weekly_record_1268_2015-04-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,762 | INFO | Processing file: weekly_record_1269_2015-04-13T00-00-00.000.json
    2026-04-15 21:06:36,788 | INFO | Skipping weekly_record_1269_2015-04-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,789 | INFO | Processing file: weekly_record_126_1993-05-17T00-00-00.000.json
    2026-04-15 21:06:36,813 | INFO | Skipping weekly_record_126_1993-05-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,813 | INFO | Processing file: weekly_record_1270_2015-04-20T00-00-00.000.json
    2026-04-15 21:06:36,840 | INFO | Skipping weekly_record_1270_2015-04-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,841 | INFO | Processing file: weekly_record_1271_2015-04-27T00-00-00.000.json
    2026-04-15 21:06:36,856 | INFO | Skipping weekly_record_1271_2015-04-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,857 | INFO | Processing file: weekly_record_1272_2015-05-04T00-00-00.000.json
    2026-04-15 21:06:36,920 | INFO | Skipping weekly_record_1272_2015-05-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,921 | INFO | Processing file: weekly_record_1273_2015-05-11T00-00-00.000.json
    2026-04-15 21:06:36,944 | INFO | Skipping weekly_record_1273_2015-05-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,945 | INFO | Processing file: weekly_record_1274_2015-05-18T00-00-00.000.json
    2026-04-15 21:06:36,977 | INFO | Skipping weekly_record_1274_2015-05-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:36,987 | INFO | Processing file: weekly_record_1275_2015-05-25T00-00-00.000.json
    2026-04-15 21:06:37,008 | INFO | Skipping weekly_record_1275_2015-05-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,009 | INFO | Processing file: weekly_record_1276_2015-06-01T00-00-00.000.json
    2026-04-15 21:06:37,034 | INFO | Skipping weekly_record_1276_2015-06-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,036 | INFO | Processing file: weekly_record_1277_2015-06-08T00-00-00.000.json
    2026-04-15 21:06:37,055 | INFO | Skipping weekly_record_1277_2015-06-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,057 | INFO | Processing file: weekly_record_1278_2015-06-15T00-00-00.000.json
    2026-04-15 21:06:37,079 | INFO | Skipping weekly_record_1278_2015-06-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,081 | INFO | Processing file: weekly_record_1279_2015-06-22T00-00-00.000.json
    2026-04-15 21:06:37,103 | INFO | Skipping weekly_record_1279_2015-06-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,105 | INFO | Processing file: weekly_record_127_1993-05-24T00-00-00.000.json
    2026-04-15 21:06:37,129 | INFO | Skipping weekly_record_127_1993-05-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,131 | INFO | Processing file: weekly_record_1280_2015-06-29T00-00-00.000.json
    2026-04-15 21:06:37,150 | INFO | Skipping weekly_record_1280_2015-06-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,155 | INFO | Processing file: weekly_record_1281_2015-07-06T00-00-00.000.json
    2026-04-15 21:06:37,174 | INFO | Skipping weekly_record_1281_2015-07-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,175 | INFO | Processing file: weekly_record_1282_2015-07-13T00-00-00.000.json
    2026-04-15 21:06:37,192 | INFO | Skipping weekly_record_1282_2015-07-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,193 | INFO | Processing file: weekly_record_1283_2015-07-20T00-00-00.000.json
    2026-04-15 21:06:37,217 | INFO | Skipping weekly_record_1283_2015-07-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,218 | INFO | Processing file: weekly_record_1284_2015-07-27T00-00-00.000.json
    2026-04-15 21:06:37,235 | INFO | Skipping weekly_record_1284_2015-07-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,236 | INFO | Processing file: weekly_record_1285_2015-08-03T00-00-00.000.json
    2026-04-15 21:06:37,263 | INFO | Skipping weekly_record_1285_2015-08-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,265 | INFO | Processing file: weekly_record_1286_2015-08-10T00-00-00.000.json
    2026-04-15 21:06:37,280 | INFO | Skipping weekly_record_1286_2015-08-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,282 | INFO | Processing file: weekly_record_1287_2015-08-17T00-00-00.000.json
    2026-04-15 21:06:37,307 | INFO | Skipping weekly_record_1287_2015-08-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,308 | INFO | Processing file: weekly_record_1288_2015-08-24T00-00-00.000.json
    2026-04-15 21:06:37,325 | INFO | Skipping weekly_record_1288_2015-08-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,326 | INFO | Processing file: weekly_record_1289_2015-08-31T00-00-00.000.json
    2026-04-15 21:06:37,348 | INFO | Skipping weekly_record_1289_2015-08-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,349 | INFO | Processing file: weekly_record_128_1993-05-31T00-00-00.000.json
    2026-04-15 21:06:37,366 | INFO | Skipping weekly_record_128_1993-05-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,367 | INFO | Processing file: weekly_record_1290_2015-09-07T00-00-00.000.json
    2026-04-15 21:06:37,395 | INFO | Skipping weekly_record_1290_2015-09-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,396 | INFO | Processing file: weekly_record_1291_2015-09-14T00-00-00.000.json
    2026-04-15 21:06:37,412 | INFO | Skipping weekly_record_1291_2015-09-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,413 | INFO | Processing file: weekly_record_1292_2015-09-21T00-00-00.000.json
    2026-04-15 21:06:37,438 | INFO | Skipping weekly_record_1292_2015-09-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,439 | INFO | Processing file: weekly_record_1293_2015-09-28T00-00-00.000.json
    2026-04-15 21:06:37,456 | INFO | Skipping weekly_record_1293_2015-09-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,458 | INFO | Processing file: weekly_record_1294_2015-10-05T00-00-00.000.json
    2026-04-15 21:06:37,486 | INFO | Skipping weekly_record_1294_2015-10-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,487 | INFO | Processing file: weekly_record_1295_2015-10-12T00-00-00.000.json
    2026-04-15 21:06:37,502 | INFO | Skipping weekly_record_1295_2015-10-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,503 | INFO | Processing file: weekly_record_1296_2015-10-19T00-00-00.000.json
    2026-04-15 21:06:37,528 | INFO | Skipping weekly_record_1296_2015-10-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,529 | INFO | Processing file: weekly_record_1297_2015-10-26T00-00-00.000.json
    2026-04-15 21:06:37,546 | INFO | Skipping weekly_record_1297_2015-10-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,548 | INFO | Processing file: weekly_record_1298_2015-11-02T00-00-00.000.json
    2026-04-15 21:06:37,573 | INFO | Skipping weekly_record_1298_2015-11-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,574 | INFO | Processing file: weekly_record_1299_2015-11-09T00-00-00.000.json
    2026-04-15 21:06:37,604 | INFO | Skipping weekly_record_1299_2015-11-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,604 | INFO | Processing file: weekly_record_129_1993-06-07T00-00-00.000.json
    2026-04-15 21:06:37,632 | INFO | Skipping weekly_record_129_1993-06-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,633 | INFO | Processing file: weekly_record_12_1991-03-11T00-00-00.000.json
    2026-04-15 21:06:37,649 | INFO | Skipping weekly_record_12_1991-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,650 | INFO | Processing file: weekly_record_1300_2015-11-16T00-00-00.000.json
    2026-04-15 21:06:37,676 | INFO | Skipping weekly_record_1300_2015-11-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,677 | INFO | Processing file: weekly_record_1301_2015-11-23T00-00-00.000.json
    2026-04-15 21:06:37,690 | INFO | Skipping weekly_record_1301_2015-11-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,691 | INFO | Processing file: weekly_record_1302_2015-11-30T00-00-00.000.json
    2026-04-15 21:06:37,717 | INFO | Skipping weekly_record_1302_2015-11-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,717 | INFO | Processing file: weekly_record_1303_2015-12-07T00-00-00.000.json
    2026-04-15 21:06:37,736 | INFO | Skipping weekly_record_1303_2015-12-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,738 | INFO | Processing file: weekly_record_1304_2015-12-14T00-00-00.000.json
    2026-04-15 21:06:37,762 | INFO | Skipping weekly_record_1304_2015-12-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,763 | INFO | Processing file: weekly_record_1305_2015-12-21T00-00-00.000.json
    2026-04-15 21:06:37,789 | INFO | Skipping weekly_record_1305_2015-12-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,790 | INFO | Processing file: weekly_record_1306_2015-12-28T00-00-00.000.json
    2026-04-15 21:06:37,815 | INFO | Skipping weekly_record_1306_2015-12-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,817 | INFO | Processing file: weekly_record_1307_2016-01-04T00-00-00.000.json
    2026-04-15 21:06:37,845 | INFO | Skipping weekly_record_1307_2016-01-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,848 | INFO | Processing file: weekly_record_1308_2016-01-11T00-00-00.000.json
    2026-04-15 21:06:37,877 | INFO | Skipping weekly_record_1308_2016-01-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,878 | INFO | Processing file: weekly_record_1309_2016-01-18T00-00-00.000.json
    2026-04-15 21:06:37,900 | INFO | Skipping weekly_record_1309_2016-01-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,901 | INFO | Processing file: weekly_record_130_1993-06-14T00-00-00.000.json
    2026-04-15 21:06:37,924 | INFO | Skipping weekly_record_130_1993-06-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,925 | INFO | Processing file: weekly_record_1310_2016-01-25T00-00-00.000.json
    2026-04-15 21:06:37,949 | INFO | Skipping weekly_record_1310_2016-01-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,950 | INFO | Processing file: weekly_record_1311_2016-02-01T00-00-00.000.json
    2026-04-15 21:06:37,977 | INFO | Skipping weekly_record_1311_2016-02-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:37,978 | INFO | Processing file: weekly_record_1312_2016-02-08T00-00-00.000.json
    2026-04-15 21:06:38,004 | INFO | Skipping weekly_record_1312_2016-02-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,004 | INFO | Processing file: weekly_record_1313_2016-02-15T00-00-00.000.json
    2026-04-15 21:06:38,034 | INFO | Skipping weekly_record_1313_2016-02-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,035 | INFO | Processing file: weekly_record_1314_2016-02-22T00-00-00.000.json
    2026-04-15 21:06:38,059 | INFO | Skipping weekly_record_1314_2016-02-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,059 | INFO | Processing file: weekly_record_1315_2016-02-29T00-00-00.000.json
    2026-04-15 21:06:38,077 | INFO | Skipping weekly_record_1315_2016-02-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,077 | INFO | Processing file: weekly_record_1316_2016-03-07T00-00-00.000.json
    2026-04-15 21:06:38,103 | INFO | Skipping weekly_record_1316_2016-03-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,104 | INFO | Processing file: weekly_record_1317_2016-03-14T00-00-00.000.json
    2026-04-15 21:06:38,121 | INFO | Skipping weekly_record_1317_2016-03-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,122 | INFO | Processing file: weekly_record_1318_2016-03-21T00-00-00.000.json
    2026-04-15 21:06:38,150 | INFO | Skipping weekly_record_1318_2016-03-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,151 | INFO | Processing file: weekly_record_1319_2016-03-28T00-00-00.000.json
    2026-04-15 21:06:38,168 | INFO | Skipping weekly_record_1319_2016-03-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,168 | INFO | Processing file: weekly_record_131_1993-06-21T00-00-00.000.json
    2026-04-15 21:06:38,188 | INFO | Skipping weekly_record_131_1993-06-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,189 | INFO | Processing file: weekly_record_1320_2016-04-04T00-00-00.000.json
    2026-04-15 21:06:38,216 | INFO | Skipping weekly_record_1320_2016-04-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,217 | INFO | Processing file: weekly_record_1321_2016-04-11T00-00-00.000.json
    2026-04-15 21:06:38,241 | INFO | Skipping weekly_record_1321_2016-04-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,243 | INFO | Processing file: weekly_record_1322_2016-04-18T00-00-00.000.json
    2026-04-15 21:06:38,271 | INFO | Skipping weekly_record_1322_2016-04-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,274 | INFO | Processing file: weekly_record_1323_2016-04-25T00-00-00.000.json
    2026-04-15 21:06:38,295 | INFO | Skipping weekly_record_1323_2016-04-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,299 | INFO | Processing file: weekly_record_1324_2016-05-02T00-00-00.000.json
    2026-04-15 21:06:38,323 | INFO | Skipping weekly_record_1324_2016-05-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,324 | INFO | Processing file: weekly_record_1325_2016-05-09T00-00-00.000.json
    2026-04-15 21:06:38,343 | INFO | Skipping weekly_record_1325_2016-05-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,345 | INFO | Processing file: weekly_record_1326_2016-05-16T00-00-00.000.json
    2026-04-15 21:06:38,372 | INFO | Skipping weekly_record_1326_2016-05-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,374 | INFO | Processing file: weekly_record_1327_2016-05-23T00-00-00.000.json
    2026-04-15 21:06:38,404 | INFO | Skipping weekly_record_1327_2016-05-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,453 | INFO | Processing file: weekly_record_1328_2016-05-30T00-00-00.000.json
    2026-04-15 21:06:38,502 | INFO | Skipping weekly_record_1328_2016-05-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,508 | INFO | Processing file: weekly_record_1329_2016-06-06T00-00-00.000.json
    2026-04-15 21:06:38,534 | INFO | Skipping weekly_record_1329_2016-06-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,537 | INFO | Processing file: weekly_record_132_1993-06-28T00-00-00.000.json
    2026-04-15 21:06:38,583 | INFO | Skipping weekly_record_132_1993-06-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,584 | INFO | Processing file: weekly_record_1330_2016-06-13T00-00-00.000.json
    2026-04-15 21:06:38,614 | INFO | Skipping weekly_record_1330_2016-06-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,616 | INFO | Processing file: weekly_record_1331_2016-06-20T00-00-00.000.json
    2026-04-15 21:06:38,657 | INFO | Skipping weekly_record_1331_2016-06-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,660 | INFO | Processing file: weekly_record_1332_2016-06-27T00-00-00.000.json
    2026-04-15 21:06:38,701 | INFO | Skipping weekly_record_1332_2016-06-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,704 | INFO | Processing file: weekly_record_1333_2016-07-04T00-00-00.000.json
    2026-04-15 21:06:38,740 | INFO | Skipping weekly_record_1333_2016-07-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,744 | INFO | Processing file: weekly_record_1334_2016-07-11T00-00-00.000.json
    2026-04-15 21:06:38,772 | INFO | Skipping weekly_record_1334_2016-07-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,777 | INFO | Processing file: weekly_record_1335_2016-07-18T00-00-00.000.json
    2026-04-15 21:06:38,813 | INFO | Skipping weekly_record_1335_2016-07-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,815 | INFO | Processing file: weekly_record_1336_2016-07-25T00-00-00.000.json
    2026-04-15 21:06:38,844 | INFO | Skipping weekly_record_1336_2016-07-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,844 | INFO | Processing file: weekly_record_1337_2016-08-01T00-00-00.000.json
    2026-04-15 21:06:38,861 | INFO | Skipping weekly_record_1337_2016-08-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,863 | INFO | Processing file: weekly_record_1338_2016-08-08T00-00-00.000.json
    2026-04-15 21:06:38,884 | INFO | Skipping weekly_record_1338_2016-08-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,885 | INFO | Processing file: weekly_record_1339_2016-08-15T00-00-00.000.json
    2026-04-15 21:06:38,908 | INFO | Skipping weekly_record_1339_2016-08-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,909 | INFO | Processing file: weekly_record_133_1993-07-05T00-00-00.000.json
    2026-04-15 21:06:38,933 | INFO | Skipping weekly_record_133_1993-07-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,934 | INFO | Processing file: weekly_record_1340_2016-08-22T00-00-00.000.json
    2026-04-15 21:06:38,969 | INFO | Skipping weekly_record_1340_2016-08-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,970 | INFO | Processing file: weekly_record_1341_2016-08-29T00-00-00.000.json
    2026-04-15 21:06:38,996 | INFO | Skipping weekly_record_1341_2016-08-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:38,996 | INFO | Processing file: weekly_record_1342_2016-09-05T00-00-00.000.json
    2026-04-15 21:06:39,018 | INFO | Skipping weekly_record_1342_2016-09-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,019 | INFO | Processing file: weekly_record_1343_2016-09-12T00-00-00.000.json
    2026-04-15 21:06:39,049 | INFO | Skipping weekly_record_1343_2016-09-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,050 | INFO | Processing file: weekly_record_1344_2016-09-19T00-00-00.000.json
    2026-04-15 21:06:39,075 | INFO | Skipping weekly_record_1344_2016-09-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,076 | INFO | Processing file: weekly_record_1345_2016-09-26T00-00-00.000.json
    2026-04-15 21:06:39,103 | INFO | Skipping weekly_record_1345_2016-09-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,103 | INFO | Processing file: weekly_record_1346_2016-10-03T00-00-00.000.json
    2026-04-15 21:06:39,129 | INFO | Skipping weekly_record_1346_2016-10-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,130 | INFO | Processing file: weekly_record_1347_2016-10-10T00-00-00.000.json
    2026-04-15 21:06:39,152 | INFO | Skipping weekly_record_1347_2016-10-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,153 | INFO | Processing file: weekly_record_1348_2016-10-17T00-00-00.000.json
    2026-04-15 21:06:39,178 | INFO | Skipping weekly_record_1348_2016-10-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,179 | INFO | Processing file: weekly_record_1349_2016-10-24T00-00-00.000.json
    2026-04-15 21:06:39,202 | INFO | Skipping weekly_record_1349_2016-10-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,204 | INFO | Processing file: weekly_record_134_1993-07-12T00-00-00.000.json
    2026-04-15 21:06:39,230 | INFO | Skipping weekly_record_134_1993-07-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,231 | INFO | Processing file: weekly_record_1350_2016-10-31T00-00-00.000.json
    2026-04-15 21:06:39,248 | INFO | Skipping weekly_record_1350_2016-10-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,250 | INFO | Processing file: weekly_record_1351_2016-11-07T00-00-00.000.json
    2026-04-15 21:06:39,287 | INFO | Skipping weekly_record_1351_2016-11-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,289 | INFO | Processing file: weekly_record_1352_2016-11-14T00-00-00.000.json
    2026-04-15 21:06:39,311 | INFO | Skipping weekly_record_1352_2016-11-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,311 | INFO | Processing file: weekly_record_1353_2016-11-21T00-00-00.000.json
    2026-04-15 21:06:39,340 | INFO | Skipping weekly_record_1353_2016-11-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,341 | INFO | Processing file: weekly_record_1354_2016-11-28T00-00-00.000.json
    2026-04-15 21:06:39,368 | INFO | Skipping weekly_record_1354_2016-11-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,369 | INFO | Processing file: weekly_record_1355_2016-12-05T00-00-00.000.json
    2026-04-15 21:06:39,392 | INFO | Skipping weekly_record_1355_2016-12-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,394 | INFO | Processing file: weekly_record_1356_2016-12-12T00-00-00.000.json
    2026-04-15 21:06:39,418 | INFO | Skipping weekly_record_1356_2016-12-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,419 | INFO | Processing file: weekly_record_1357_2016-12-19T00-00-00.000.json
    2026-04-15 21:06:39,447 | INFO | Skipping weekly_record_1357_2016-12-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,448 | INFO | Processing file: weekly_record_1358_2016-12-26T00-00-00.000.json
    2026-04-15 21:06:39,468 | INFO | Skipping weekly_record_1358_2016-12-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,469 | INFO | Processing file: weekly_record_1359_2017-01-02T00-00-00.000.json
    2026-04-15 21:06:39,485 | INFO | Skipping weekly_record_1359_2017-01-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,485 | INFO | Processing file: weekly_record_135_1993-07-19T00-00-00.000.json
    2026-04-15 21:06:39,507 | INFO | Skipping weekly_record_135_1993-07-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,509 | INFO | Processing file: weekly_record_1360_2017-01-09T00-00-00.000.json
    2026-04-15 21:06:39,535 | INFO | Skipping weekly_record_1360_2017-01-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,536 | INFO | Processing file: weekly_record_1361_2017-01-16T00-00-00.000.json
    2026-04-15 21:06:39,554 | INFO | Skipping weekly_record_1361_2017-01-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,554 | INFO | Processing file: weekly_record_1362_2017-01-23T00-00-00.000.json
    2026-04-15 21:06:39,580 | INFO | Skipping weekly_record_1362_2017-01-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,582 | INFO | Processing file: weekly_record_1363_2017-01-30T00-00-00.000.json
    2026-04-15 21:06:39,607 | INFO | Skipping weekly_record_1363_2017-01-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,608 | INFO | Processing file: weekly_record_1364_2017-02-06T00-00-00.000.json
    2026-04-15 21:06:39,625 | INFO | Skipping weekly_record_1364_2017-02-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,626 | INFO | Processing file: weekly_record_1365_2017-02-13T00-00-00.000.json
    2026-04-15 21:06:39,657 | INFO | Skipping weekly_record_1365_2017-02-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,658 | INFO | Processing file: weekly_record_1366_2017-02-20T00-00-00.000.json
    2026-04-15 21:06:39,684 | INFO | Skipping weekly_record_1366_2017-02-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,685 | INFO | Processing file: weekly_record_1367_2017-02-27T00-00-00.000.json
    2026-04-15 21:06:39,715 | INFO | Skipping weekly_record_1367_2017-02-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,715 | INFO | Processing file: weekly_record_1368_2017-03-06T00-00-00.000.json
    2026-04-15 21:06:39,749 | INFO | Skipping weekly_record_1368_2017-03-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,749 | INFO | Processing file: weekly_record_1369_2017-03-13T00-00-00.000.json
    2026-04-15 21:06:39,793 | INFO | Skipping weekly_record_1369_2017-03-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,793 | INFO | Processing file: weekly_record_136_1993-07-26T00-00-00.000.json
    2026-04-15 21:06:39,838 | INFO | Skipping weekly_record_136_1993-07-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,839 | INFO | Processing file: weekly_record_1370_2017-03-20T00-00-00.000.json
    2026-04-15 21:06:39,887 | INFO | Skipping weekly_record_1370_2017-03-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,889 | INFO | Processing file: weekly_record_1371_2017-03-27T00-00-00.000.json
    2026-04-15 21:06:39,923 | INFO | Skipping weekly_record_1371_2017-03-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,924 | INFO | Processing file: weekly_record_1372_2017-04-03T00-00-00.000.json
    2026-04-15 21:06:39,946 | INFO | Skipping weekly_record_1372_2017-04-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,948 | INFO | Processing file: weekly_record_1373_2017-04-10T00-00-00.000.json
    2026-04-15 21:06:39,988 | INFO | Skipping weekly_record_1373_2017-04-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:39,989 | INFO | Processing file: weekly_record_1374_2017-04-17T00-00-00.000.json
    2026-04-15 21:06:40,051 | INFO | Skipping weekly_record_1374_2017-04-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,053 | INFO | Processing file: weekly_record_1375_2017-04-24T00-00-00.000.json
    2026-04-15 21:06:40,090 | INFO | Skipping weekly_record_1375_2017-04-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,092 | INFO | Processing file: weekly_record_1376_2017-05-01T00-00-00.000.json
    2026-04-15 21:06:40,157 | INFO | Skipping weekly_record_1376_2017-05-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,158 | INFO | Processing file: weekly_record_1377_2017-05-08T00-00-00.000.json
    2026-04-15 21:06:40,181 | INFO | Skipping weekly_record_1377_2017-05-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,182 | INFO | Processing file: weekly_record_1378_2017-05-15T00-00-00.000.json
    2026-04-15 21:06:40,209 | INFO | Skipping weekly_record_1378_2017-05-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,210 | INFO | Processing file: weekly_record_1379_2017-05-22T00-00-00.000.json
    2026-04-15 21:06:40,230 | INFO | Skipping weekly_record_1379_2017-05-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,232 | INFO | Processing file: weekly_record_137_1993-08-02T00-00-00.000.json
    2026-04-15 21:06:40,250 | INFO | Skipping weekly_record_137_1993-08-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,251 | INFO | Processing file: weekly_record_1380_2017-05-29T00-00-00.000.json
    2026-04-15 21:06:40,275 | INFO | Skipping weekly_record_1380_2017-05-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,276 | INFO | Processing file: weekly_record_1381_2017-06-05T00-00-00.000.json
    2026-04-15 21:06:40,293 | INFO | Skipping weekly_record_1381_2017-06-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,295 | INFO | Processing file: weekly_record_1382_2017-06-12T00-00-00.000.json
    2026-04-15 21:06:40,325 | INFO | Skipping weekly_record_1382_2017-06-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,326 | INFO | Processing file: weekly_record_1383_2017-06-19T00-00-00.000.json
    2026-04-15 21:06:40,343 | INFO | Skipping weekly_record_1383_2017-06-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,344 | INFO | Processing file: weekly_record_1384_2017-06-26T00-00-00.000.json
    2026-04-15 21:06:40,370 | INFO | Skipping weekly_record_1384_2017-06-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,371 | INFO | Processing file: weekly_record_1385_2017-07-03T00-00-00.000.json
    2026-04-15 21:06:40,389 | INFO | Skipping weekly_record_1385_2017-07-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,390 | INFO | Processing file: weekly_record_1386_2017-07-10T00-00-00.000.json
    2026-04-15 21:06:40,458 | INFO | Skipping weekly_record_1386_2017-07-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,459 | INFO | Processing file: weekly_record_1387_2017-07-17T00-00-00.000.json
    2026-04-15 21:06:40,492 | INFO | Skipping weekly_record_1387_2017-07-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,493 | INFO | Processing file: weekly_record_1388_2017-07-24T00-00-00.000.json
    2026-04-15 21:06:40,536 | INFO | Skipping weekly_record_1388_2017-07-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,537 | INFO | Processing file: weekly_record_1389_2017-07-31T00-00-00.000.json
    2026-04-15 21:06:40,562 | INFO | Skipping weekly_record_1389_2017-07-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,563 | INFO | Processing file: weekly_record_138_1993-08-09T00-00-00.000.json
    2026-04-15 21:06:40,599 | INFO | Skipping weekly_record_138_1993-08-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,601 | INFO | Processing file: weekly_record_1390_2017-08-07T00-00-00.000.json
    2026-04-15 21:06:40,631 | INFO | Skipping weekly_record_1390_2017-08-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,632 | INFO | Processing file: weekly_record_1391_2017-08-14T00-00-00.000.json
    2026-04-15 21:06:40,665 | INFO | Skipping weekly_record_1391_2017-08-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,667 | INFO | Processing file: weekly_record_1392_2017-08-21T00-00-00.000.json
    2026-04-15 21:06:40,694 | INFO | Skipping weekly_record_1392_2017-08-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,697 | INFO | Processing file: weekly_record_1393_2017-08-28T00-00-00.000.json
    2026-04-15 21:06:40,724 | INFO | Skipping weekly_record_1393_2017-08-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,725 | INFO | Processing file: weekly_record_1394_2017-09-04T00-00-00.000.json
    2026-04-15 21:06:40,769 | INFO | Skipping weekly_record_1394_2017-09-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,771 | INFO | Processing file: weekly_record_1395_2017-09-11T00-00-00.000.json
    2026-04-15 21:06:40,797 | INFO | Skipping weekly_record_1395_2017-09-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,798 | INFO | Processing file: weekly_record_1396_2017-09-18T00-00-00.000.json
    2026-04-15 21:06:40,831 | INFO | Skipping weekly_record_1396_2017-09-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,832 | INFO | Processing file: weekly_record_1397_2017-09-25T00-00-00.000.json
    2026-04-15 21:06:40,858 | INFO | Skipping weekly_record_1397_2017-09-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,859 | INFO | Processing file: weekly_record_1398_2017-10-02T00-00-00.000.json
    2026-04-15 21:06:40,883 | INFO | Skipping weekly_record_1398_2017-10-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,884 | INFO | Processing file: weekly_record_1399_2017-10-09T00-00-00.000.json
    2026-04-15 21:06:40,917 | INFO | Skipping weekly_record_1399_2017-10-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,918 | INFO | Processing file: weekly_record_139_1993-08-16T00-00-00.000.json
    2026-04-15 21:06:40,940 | INFO | Skipping weekly_record_139_1993-08-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,941 | INFO | Processing file: weekly_record_13_1991-03-18T00-00-00.000.json
    2026-04-15 21:06:40,967 | INFO | Skipping weekly_record_13_1991-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:40,968 | INFO | Processing file: weekly_record_1400_2017-10-16T00-00-00.000.json
    2026-04-15 21:06:41,009 | INFO | Skipping weekly_record_1400_2017-10-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,010 | INFO | Processing file: weekly_record_1401_2017-10-23T00-00-00.000.json
    2026-04-15 21:06:41,035 | INFO | Skipping weekly_record_1401_2017-10-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,036 | INFO | Processing file: weekly_record_1402_2017-10-30T00-00-00.000.json
    2026-04-15 21:06:41,053 | INFO | Skipping weekly_record_1402_2017-10-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,054 | INFO | Processing file: weekly_record_1403_2017-11-06T00-00-00.000.json
    2026-04-15 21:06:41,082 | INFO | Skipping weekly_record_1403_2017-11-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,082 | INFO | Processing file: weekly_record_1404_2017-11-13T00-00-00.000.json
    2026-04-15 21:06:41,104 | INFO | Skipping weekly_record_1404_2017-11-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,106 | INFO | Processing file: weekly_record_1405_2017-11-20T00-00-00.000.json
    2026-04-15 21:06:41,122 | INFO | Skipping weekly_record_1405_2017-11-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,123 | INFO | Processing file: weekly_record_1406_2017-11-27T00-00-00.000.json
    2026-04-15 21:06:41,148 | INFO | Skipping weekly_record_1406_2017-11-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,149 | INFO | Processing file: weekly_record_1407_2017-12-04T00-00-00.000.json
    2026-04-15 21:06:41,168 | INFO | Skipping weekly_record_1407_2017-12-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,169 | INFO | Processing file: weekly_record_1408_2017-12-11T00-00-00.000.json
    2026-04-15 21:06:41,194 | INFO | Skipping weekly_record_1408_2017-12-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,196 | INFO | Processing file: weekly_record_1409_2017-12-18T00-00-00.000.json
    2026-04-15 21:06:41,220 | INFO | Skipping weekly_record_1409_2017-12-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,221 | INFO | Processing file: weekly_record_140_1993-08-23T00-00-00.000.json
    2026-04-15 21:06:41,245 | INFO | Skipping weekly_record_140_1993-08-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,246 | INFO | Processing file: weekly_record_1410_2017-12-25T00-00-00.000.json
    2026-04-15 21:06:41,271 | INFO | Skipping weekly_record_1410_2017-12-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,272 | INFO | Processing file: weekly_record_1411_2018-01-01T00-00-00.000.json
    2026-04-15 21:06:41,288 | INFO | Skipping weekly_record_1411_2018-01-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,288 | INFO | Processing file: weekly_record_1412_2018-01-08T00-00-00.000.json
    2026-04-15 21:06:41,311 | INFO | Skipping weekly_record_1412_2018-01-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,312 | INFO | Processing file: weekly_record_1413_2018-01-15T00-00-00.000.json
    2026-04-15 21:06:41,337 | INFO | Skipping weekly_record_1413_2018-01-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,338 | INFO | Processing file: weekly_record_1414_2018-01-22T00-00-00.000.json
    2026-04-15 21:06:41,373 | INFO | Skipping weekly_record_1414_2018-01-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,374 | INFO | Processing file: weekly_record_1415_2018-01-29T00-00-00.000.json
    2026-04-15 21:06:41,401 | INFO | Skipping weekly_record_1415_2018-01-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,401 | INFO | Processing file: weekly_record_1416_2018-02-05T00-00-00.000.json
    2026-04-15 21:06:41,428 | INFO | Skipping weekly_record_1416_2018-02-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,429 | INFO | Processing file: weekly_record_1417_2018-02-12T00-00-00.000.json
    2026-04-15 21:06:41,445 | INFO | Skipping weekly_record_1417_2018-02-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,446 | INFO | Processing file: weekly_record_1418_2018-02-19T00-00-00.000.json
    2026-04-15 21:06:41,475 | INFO | Skipping weekly_record_1418_2018-02-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,476 | INFO | Processing file: weekly_record_1419_2018-02-26T00-00-00.000.json
    2026-04-15 21:06:41,504 | INFO | Skipping weekly_record_1419_2018-02-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,505 | INFO | Processing file: weekly_record_141_1993-08-30T00-00-00.000.json
    2026-04-15 21:06:41,529 | INFO | Skipping weekly_record_141_1993-08-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,530 | INFO | Processing file: weekly_record_1420_2018-03-05T00-00-00.000.json
    2026-04-15 21:06:41,584 | INFO | Skipping weekly_record_1420_2018-03-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,586 | INFO | Processing file: weekly_record_1421_2018-03-12T00-00-00.000.json
    2026-04-15 21:06:41,607 | INFO | Skipping weekly_record_1421_2018-03-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,608 | INFO | Processing file: weekly_record_1422_2018-03-19T00-00-00.000.json
    2026-04-15 21:06:41,626 | INFO | Skipping weekly_record_1422_2018-03-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,627 | INFO | Processing file: weekly_record_1423_2018-03-26T00-00-00.000.json
    2026-04-15 21:06:41,661 | INFO | Skipping weekly_record_1423_2018-03-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,661 | INFO | Processing file: weekly_record_1424_2018-04-02T00-00-00.000.json
    2026-04-15 21:06:41,682 | INFO | Skipping weekly_record_1424_2018-04-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,683 | INFO | Processing file: weekly_record_1425_2018-04-09T00-00-00.000.json
    2026-04-15 21:06:41,711 | INFO | Skipping weekly_record_1425_2018-04-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,711 | INFO | Processing file: weekly_record_1426_2018-04-16T00-00-00.000.json
    2026-04-15 21:06:41,728 | INFO | Skipping weekly_record_1426_2018-04-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,729 | INFO | Processing file: weekly_record_1427_2018-04-23T00-00-00.000.json
    2026-04-15 21:06:41,750 | INFO | Skipping weekly_record_1427_2018-04-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,751 | INFO | Processing file: weekly_record_1428_2018-04-30T00-00-00.000.json
    2026-04-15 21:06:41,764 | INFO | Skipping weekly_record_1428_2018-04-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,765 | INFO | Processing file: weekly_record_1429_2018-05-07T00-00-00.000.json
    2026-04-15 21:06:41,791 | INFO | Skipping weekly_record_1429_2018-05-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,792 | INFO | Processing file: weekly_record_142_1993-09-06T00-00-00.000.json
    2026-04-15 21:06:41,805 | INFO | Skipping weekly_record_142_1993-09-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,806 | INFO | Processing file: weekly_record_1430_2018-05-14T00-00-00.000.json
    2026-04-15 21:06:41,832 | INFO | Skipping weekly_record_1430_2018-05-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,833 | INFO | Processing file: weekly_record_1431_2018-05-21T00-00-00.000.json
    2026-04-15 21:06:41,850 | INFO | Skipping weekly_record_1431_2018-05-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,851 | INFO | Processing file: weekly_record_1432_2018-05-28T00-00-00.000.json
    2026-04-15 21:06:41,875 | INFO | Skipping weekly_record_1432_2018-05-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,876 | INFO | Processing file: weekly_record_1433_2018-06-04T00-00-00.000.json
    2026-04-15 21:06:41,904 | INFO | Skipping weekly_record_1433_2018-06-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,906 | INFO | Processing file: weekly_record_1434_2018-06-11T00-00-00.000.json
    2026-04-15 21:06:41,931 | INFO | Skipping weekly_record_1434_2018-06-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,932 | INFO | Processing file: weekly_record_1435_2018-06-18T00-00-00.000.json
    2026-04-15 21:06:41,957 | INFO | Skipping weekly_record_1435_2018-06-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,958 | INFO | Processing file: weekly_record_1436_2018-06-25T00-00-00.000.json
    2026-04-15 21:06:41,975 | INFO | Skipping weekly_record_1436_2018-06-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:41,976 | INFO | Processing file: weekly_record_1437_2018-07-02T00-00-00.000.json
    2026-04-15 21:06:42,004 | INFO | Skipping weekly_record_1437_2018-07-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,004 | INFO | Processing file: weekly_record_1438_2018-07-09T00-00-00.000.json
    2026-04-15 21:06:42,030 | INFO | Skipping weekly_record_1438_2018-07-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,031 | INFO | Processing file: weekly_record_1439_2018-07-16T00-00-00.000.json
    2026-04-15 21:06:42,060 | INFO | Skipping weekly_record_1439_2018-07-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,061 | INFO | Processing file: weekly_record_143_1993-09-13T00-00-00.000.json
    2026-04-15 21:06:42,083 | INFO | Skipping weekly_record_143_1993-09-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,084 | INFO | Processing file: weekly_record_1440_2018-07-23T00-00-00.000.json
    2026-04-15 21:06:42,102 | INFO | Skipping weekly_record_1440_2018-07-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,103 | INFO | Processing file: weekly_record_1441_2018-07-30T00-00-00.000.json
    2026-04-15 21:06:42,120 | INFO | Skipping weekly_record_1441_2018-07-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,121 | INFO | Processing file: weekly_record_1442_2018-08-06T00-00-00.000.json
    2026-04-15 21:06:42,146 | INFO | Skipping weekly_record_1442_2018-08-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,147 | INFO | Processing file: weekly_record_1443_2018-08-13T00-00-00.000.json
    2026-04-15 21:06:42,169 | INFO | Skipping weekly_record_1443_2018-08-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,170 | INFO | Processing file: weekly_record_1444_2018-08-20T00-00-00.000.json
    2026-04-15 21:06:42,196 | INFO | Skipping weekly_record_1444_2018-08-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,197 | INFO | Processing file: weekly_record_1445_2018-08-27T00-00-00.000.json
    2026-04-15 21:06:42,222 | INFO | Skipping weekly_record_1445_2018-08-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,223 | INFO | Processing file: weekly_record_1446_2018-09-03T00-00-00.000.json
    2026-04-15 21:06:42,248 | INFO | Skipping weekly_record_1446_2018-09-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,249 | INFO | Processing file: weekly_record_1447_2018-09-10T00-00-00.000.json
    2026-04-15 21:06:42,275 | INFO | Skipping weekly_record_1447_2018-09-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,276 | INFO | Processing file: weekly_record_1448_2018-09-17T00-00-00.000.json
    2026-04-15 21:06:42,321 | INFO | Skipping weekly_record_1448_2018-09-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,322 | INFO | Processing file: weekly_record_1449_2018-09-24T00-00-00.000.json
    2026-04-15 21:06:42,378 | INFO | Skipping weekly_record_1449_2018-09-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,380 | INFO | Processing file: weekly_record_144_1993-09-20T00-00-00.000.json
    2026-04-15 21:06:42,412 | INFO | Skipping weekly_record_144_1993-09-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,416 | INFO | Processing file: weekly_record_1450_2018-10-01T00-00-00.000.json
    2026-04-15 21:06:42,558 | INFO | Skipping weekly_record_1450_2018-10-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,560 | INFO | Processing file: weekly_record_1451_2018-10-08T00-00-00.000.json
    2026-04-15 21:06:42,614 | INFO | Skipping weekly_record_1451_2018-10-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,616 | INFO | Processing file: weekly_record_1452_2018-10-15T00-00-00.000.json
    2026-04-15 21:06:42,654 | INFO | Skipping weekly_record_1452_2018-10-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,655 | INFO | Processing file: weekly_record_1453_2018-10-22T00-00-00.000.json
    2026-04-15 21:06:42,687 | INFO | Skipping weekly_record_1453_2018-10-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,688 | INFO | Processing file: weekly_record_1454_2018-10-29T00-00-00.000.json
    2026-04-15 21:06:42,715 | INFO | Skipping weekly_record_1454_2018-10-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,716 | INFO | Processing file: weekly_record_1455_2018-11-05T00-00-00.000.json
    2026-04-15 21:06:42,757 | INFO | Skipping weekly_record_1455_2018-11-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,761 | INFO | Processing file: weekly_record_1456_2018-11-12T00-00-00.000.json
    2026-04-15 21:06:42,783 | INFO | Skipping weekly_record_1456_2018-11-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,785 | INFO | Processing file: weekly_record_1457_2018-11-19T00-00-00.000.json
    2026-04-15 21:06:42,810 | INFO | Skipping weekly_record_1457_2018-11-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,841 | INFO | Processing file: weekly_record_1458_2018-11-26T00-00-00.000.json
    2026-04-15 21:06:42,870 | INFO | Skipping weekly_record_1458_2018-11-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,875 | INFO | Processing file: weekly_record_1459_2018-12-03T00-00-00.000.json
    2026-04-15 21:06:42,904 | INFO | Skipping weekly_record_1459_2018-12-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,905 | INFO | Processing file: weekly_record_145_1993-09-27T00-00-00.000.json
    2026-04-15 21:06:42,929 | INFO | Skipping weekly_record_145_1993-09-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:42,931 | INFO | Processing file: weekly_record_1460_2018-12-10T00-00-00.000.json
    2026-04-15 21:06:42,961 | INFO | Skipping weekly_record_1460_2018-12-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,006 | INFO | Processing file: weekly_record_1461_2018-12-17T00-00-00.000.json
    2026-04-15 21:06:43,034 | INFO | Skipping weekly_record_1461_2018-12-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,035 | INFO | Processing file: weekly_record_1462_2018-12-24T00-00-00.000.json
    2026-04-15 21:06:43,059 | INFO | Skipping weekly_record_1462_2018-12-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,061 | INFO | Processing file: weekly_record_1463_2018-12-31T00-00-00.000.json
    2026-04-15 21:06:43,103 | INFO | Skipping weekly_record_1463_2018-12-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,106 | INFO | Processing file: weekly_record_1464_2019-01-07T00-00-00.000.json
    2026-04-15 21:06:43,147 | INFO | Skipping weekly_record_1464_2019-01-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,148 | INFO | Processing file: weekly_record_1465_2019-01-14T00-00-00.000.json
    2026-04-15 21:06:43,201 | INFO | Skipping weekly_record_1465_2019-01-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,202 | INFO | Processing file: weekly_record_1466_2019-01-21T00-00-00.000.json
    2026-04-15 21:06:43,312 | INFO | Skipping weekly_record_1466_2019-01-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,314 | INFO | Processing file: weekly_record_1467_2019-01-28T00-00-00.000.json
    2026-04-15 21:06:43,349 | INFO | Skipping weekly_record_1467_2019-01-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,350 | INFO | Processing file: weekly_record_1468_2019-02-04T00-00-00.000.json
    2026-04-15 21:06:43,418 | INFO | Skipping weekly_record_1468_2019-02-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,420 | INFO | Processing file: weekly_record_1469_2019-02-11T00-00-00.000.json
    2026-04-15 21:06:43,477 | INFO | Skipping weekly_record_1469_2019-02-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,478 | INFO | Processing file: weekly_record_146_1993-10-04T00-00-00.000.json
    2026-04-15 21:06:43,516 | INFO | Skipping weekly_record_146_1993-10-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,517 | INFO | Processing file: weekly_record_1470_2019-02-18T00-00-00.000.json
    2026-04-15 21:06:43,548 | INFO | Skipping weekly_record_1470_2019-02-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,549 | INFO | Processing file: weekly_record_1471_2019-02-25T00-00-00.000.json
    2026-04-15 21:06:43,580 | INFO | Skipping weekly_record_1471_2019-02-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,581 | INFO | Processing file: weekly_record_1472_2019-03-04T00-00-00.000.json
    2026-04-15 21:06:43,619 | INFO | Skipping weekly_record_1472_2019-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,620 | INFO | Processing file: weekly_record_1473_2019-03-11T00-00-00.000.json
    2026-04-15 21:06:43,648 | INFO | Skipping weekly_record_1473_2019-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,649 | INFO | Processing file: weekly_record_1474_2019-03-18T00-00-00.000.json
    2026-04-15 21:06:43,679 | INFO | Skipping weekly_record_1474_2019-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,680 | INFO | Processing file: weekly_record_1475_2019-03-25T00-00-00.000.json
    2026-04-15 21:06:43,707 | INFO | Skipping weekly_record_1475_2019-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,707 | INFO | Processing file: weekly_record_1476_2019-04-01T00-00-00.000.json
    2026-04-15 21:06:43,741 | INFO | Skipping weekly_record_1476_2019-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,741 | INFO | Processing file: weekly_record_1477_2019-04-08T00-00-00.000.json
    2026-04-15 21:06:43,781 | INFO | Skipping weekly_record_1477_2019-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,782 | INFO | Processing file: weekly_record_1478_2019-04-15T00-00-00.000.json
    2026-04-15 21:06:43,825 | INFO | Skipping weekly_record_1478_2019-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,826 | INFO | Processing file: weekly_record_1479_2019-04-22T00-00-00.000.json
    2026-04-15 21:06:43,858 | INFO | Skipping weekly_record_1479_2019-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,859 | INFO | Processing file: weekly_record_147_1993-10-11T00-00-00.000.json
    2026-04-15 21:06:43,886 | INFO | Skipping weekly_record_147_1993-10-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,886 | INFO | Processing file: weekly_record_1480_2019-04-29T00-00-00.000.json
    2026-04-15 21:06:43,907 | INFO | Skipping weekly_record_1480_2019-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,908 | INFO | Processing file: weekly_record_1481_2019-05-06T00-00-00.000.json
    2026-04-15 21:06:43,930 | INFO | Skipping weekly_record_1481_2019-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,933 | INFO | Processing file: weekly_record_1482_2019-05-13T00-00-00.000.json
    2026-04-15 21:06:43,949 | INFO | Skipping weekly_record_1482_2019-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,951 | INFO | Processing file: weekly_record_1483_2019-05-20T00-00-00.000.json
    2026-04-15 21:06:43,979 | INFO | Skipping weekly_record_1483_2019-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,980 | INFO | Processing file: weekly_record_1484_2019-05-27T00-00-00.000.json
    2026-04-15 21:06:43,997 | INFO | Skipping weekly_record_1484_2019-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:43,998 | INFO | Processing file: weekly_record_1485_2019-06-03T00-00-00.000.json
    2026-04-15 21:06:44,021 | INFO | Skipping weekly_record_1485_2019-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,022 | INFO | Processing file: weekly_record_1486_2019-06-10T00-00-00.000.json
    2026-04-15 21:06:44,040 | INFO | Skipping weekly_record_1486_2019-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,041 | INFO | Processing file: weekly_record_1487_2019-06-17T00-00-00.000.json
    2026-04-15 21:06:44,061 | INFO | Skipping weekly_record_1487_2019-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,062 | INFO | Processing file: weekly_record_1488_2019-06-24T00-00-00.000.json
    2026-04-15 21:06:44,088 | INFO | Skipping weekly_record_1488_2019-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,089 | INFO | Processing file: weekly_record_1489_2019-07-01T00-00-00.000.json
    2026-04-15 21:06:44,112 | INFO | Skipping weekly_record_1489_2019-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,113 | INFO | Processing file: weekly_record_148_1993-10-18T00-00-00.000.json
    2026-04-15 21:06:44,137 | INFO | Skipping weekly_record_148_1993-10-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,139 | INFO | Processing file: weekly_record_1490_2019-07-08T00-00-00.000.json
    2026-04-15 21:06:44,164 | INFO | Skipping weekly_record_1490_2019-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,165 | INFO | Processing file: weekly_record_1491_2019-07-15T00-00-00.000.json
    2026-04-15 21:06:44,184 | INFO | Skipping weekly_record_1491_2019-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,184 | INFO | Processing file: weekly_record_1492_2019-07-22T00-00-00.000.json
    2026-04-15 21:06:44,209 | INFO | Skipping weekly_record_1492_2019-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,210 | INFO | Processing file: weekly_record_1493_2019-07-29T00-00-00.000.json
    2026-04-15 21:06:44,226 | INFO | Skipping weekly_record_1493_2019-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,227 | INFO | Processing file: weekly_record_1494_2019-08-05T00-00-00.000.json
    2026-04-15 21:06:44,257 | INFO | Skipping weekly_record_1494_2019-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,258 | INFO | Processing file: weekly_record_1495_2019-08-12T00-00-00.000.json
    2026-04-15 21:06:44,285 | INFO | Skipping weekly_record_1495_2019-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,286 | INFO | Processing file: weekly_record_1496_2019-08-19T00-00-00.000.json
    2026-04-15 21:06:44,303 | INFO | Skipping weekly_record_1496_2019-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,304 | INFO | Processing file: weekly_record_1497_2019-08-26T00-00-00.000.json
    2026-04-15 21:06:44,330 | INFO | Skipping weekly_record_1497_2019-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,331 | INFO | Processing file: weekly_record_1498_2019-09-02T00-00-00.000.json
    2026-04-15 21:06:44,360 | INFO | Skipping weekly_record_1498_2019-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,361 | INFO | Processing file: weekly_record_1499_2019-09-09T00-00-00.000.json
    2026-04-15 21:06:44,384 | INFO | Skipping weekly_record_1499_2019-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,385 | INFO | Processing file: weekly_record_149_1993-10-25T00-00-00.000.json
    2026-04-15 21:06:44,408 | INFO | Skipping weekly_record_149_1993-10-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,409 | INFO | Processing file: weekly_record_14_1991-03-25T00-00-00.000.json
    2026-04-15 21:06:44,460 | INFO | Skipping weekly_record_14_1991-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,463 | INFO | Processing file: weekly_record_1500_2019-09-16T00-00-00.000.json
    2026-04-15 21:06:44,504 | INFO | Skipping weekly_record_1500_2019-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,505 | INFO | Processing file: weekly_record_1501_2019-09-23T00-00-00.000.json
    2026-04-15 21:06:44,530 | INFO | Skipping weekly_record_1501_2019-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,531 | INFO | Processing file: weekly_record_1502_2019-09-30T00-00-00.000.json
    2026-04-15 21:06:44,557 | INFO | Skipping weekly_record_1502_2019-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,558 | INFO | Processing file: weekly_record_1503_2019-10-07T00-00-00.000.json
    2026-04-15 21:06:44,575 | INFO | Skipping weekly_record_1503_2019-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,576 | INFO | Processing file: weekly_record_1504_2019-10-14T00-00-00.000.json
    2026-04-15 21:06:44,605 | INFO | Skipping weekly_record_1504_2019-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,607 | INFO | Processing file: weekly_record_1505_2019-10-21T00-00-00.000.json
    2026-04-15 21:06:44,631 | INFO | Skipping weekly_record_1505_2019-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,633 | INFO | Processing file: weekly_record_1506_2019-10-28T00-00-00.000.json
    2026-04-15 21:06:44,655 | INFO | Skipping weekly_record_1506_2019-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,657 | INFO | Processing file: weekly_record_1507_2019-11-04T00-00-00.000.json
    2026-04-15 21:06:44,671 | INFO | Skipping weekly_record_1507_2019-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,672 | INFO | Processing file: weekly_record_1508_2019-11-11T00-00-00.000.json
    2026-04-15 21:06:44,701 | INFO | Skipping weekly_record_1508_2019-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,702 | INFO | Processing file: weekly_record_1509_2019-11-18T00-00-00.000.json
    2026-04-15 21:06:44,732 | INFO | Skipping weekly_record_1509_2019-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,734 | INFO | Processing file: weekly_record_150_1993-11-01T00-00-00.000.json
    2026-04-15 21:06:44,758 | INFO | Skipping weekly_record_150_1993-11-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,759 | INFO | Processing file: weekly_record_1510_2019-11-25T00-00-00.000.json
    2026-04-15 21:06:44,783 | INFO | Skipping weekly_record_1510_2019-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,784 | INFO | Processing file: weekly_record_1511_2019-12-02T00-00-00.000.json
    2026-04-15 21:06:44,809 | INFO | Skipping weekly_record_1511_2019-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,811 | INFO | Processing file: weekly_record_1512_2019-12-09T00-00-00.000.json
    2026-04-15 21:06:44,836 | INFO | Skipping weekly_record_1512_2019-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,837 | INFO | Processing file: weekly_record_1513_2019-12-16T00-00-00.000.json
    2026-04-15 21:06:44,864 | INFO | Skipping weekly_record_1513_2019-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,869 | INFO | Processing file: weekly_record_1514_2019-12-23T00-00-00.000.json
    2026-04-15 21:06:44,900 | INFO | Skipping weekly_record_1514_2019-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,902 | INFO | Processing file: weekly_record_1515_2019-12-30T00-00-00.000.json
    2026-04-15 21:06:44,926 | INFO | Skipping weekly_record_1515_2019-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,928 | INFO | Processing file: weekly_record_1516_2020-01-06T00-00-00.000.json
    2026-04-15 21:06:44,952 | INFO | Skipping weekly_record_1516_2020-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,953 | INFO | Processing file: weekly_record_1517_2020-01-13T00-00-00.000.json
    2026-04-15 21:06:44,983 | INFO | Skipping weekly_record_1517_2020-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:44,984 | INFO | Processing file: weekly_record_1518_2020-01-20T00-00-00.000.json
    2026-04-15 21:06:45,015 | INFO | Skipping weekly_record_1518_2020-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,016 | INFO | Processing file: weekly_record_1519_2020-01-27T00-00-00.000.json
    2026-04-15 21:06:45,044 | INFO | Skipping weekly_record_1519_2020-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,045 | INFO | Processing file: weekly_record_151_1993-11-08T00-00-00.000.json
    2026-04-15 21:06:45,071 | INFO | Skipping weekly_record_151_1993-11-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,072 | INFO | Processing file: weekly_record_1520_2020-02-03T00-00-00.000.json
    2026-04-15 21:06:45,099 | INFO | Skipping weekly_record_1520_2020-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,100 | INFO | Processing file: weekly_record_1521_2020-02-10T00-00-00.000.json
    2026-04-15 21:06:45,126 | INFO | Skipping weekly_record_1521_2020-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,129 | INFO | Processing file: weekly_record_1522_2020-02-17T00-00-00.000.json
    2026-04-15 21:06:45,158 | INFO | Skipping weekly_record_1522_2020-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,159 | INFO | Processing file: weekly_record_1523_2020-02-24T00-00-00.000.json
    2026-04-15 21:06:45,178 | INFO | Skipping weekly_record_1523_2020-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,179 | INFO | Processing file: weekly_record_1524_2020-03-02T00-00-00.000.json
    2026-04-15 21:06:45,203 | INFO | Skipping weekly_record_1524_2020-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,204 | INFO | Processing file: weekly_record_1525_2020-03-09T00-00-00.000.json
    2026-04-15 21:06:45,225 | INFO | Skipping weekly_record_1525_2020-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,226 | INFO | Processing file: weekly_record_1526_2020-03-16T00-00-00.000.json
    2026-04-15 21:06:45,255 | INFO | Skipping weekly_record_1526_2020-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,258 | INFO | Processing file: weekly_record_1527_2020-03-23T00-00-00.000.json
    2026-04-15 21:06:45,288 | INFO | Skipping weekly_record_1527_2020-03-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,289 | INFO | Processing file: weekly_record_1528_2020-03-30T00-00-00.000.json
    2026-04-15 21:06:45,311 | INFO | Skipping weekly_record_1528_2020-03-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,312 | INFO | Processing file: weekly_record_1529_2020-04-06T00-00-00.000.json
    2026-04-15 21:06:45,337 | INFO | Skipping weekly_record_1529_2020-04-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,338 | INFO | Processing file: weekly_record_152_1993-11-15T00-00-00.000.json
    2026-04-15 21:06:45,367 | INFO | Skipping weekly_record_152_1993-11-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,418 | INFO | Processing file: weekly_record_1530_2020-04-13T00-00-00.000.json
    2026-04-15 21:06:45,447 | INFO | Skipping weekly_record_1530_2020-04-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,448 | INFO | Processing file: weekly_record_1531_2020-04-20T00-00-00.000.json
    2026-04-15 21:06:45,474 | INFO | Skipping weekly_record_1531_2020-04-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,475 | INFO | Processing file: weekly_record_1532_2020-04-27T00-00-00.000.json
    2026-04-15 21:06:45,571 | INFO | Skipping weekly_record_1532_2020-04-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,573 | INFO | Processing file: weekly_record_1533_2020-05-04T00-00-00.000.json
    2026-04-15 21:06:45,598 | INFO | Skipping weekly_record_1533_2020-05-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,599 | INFO | Processing file: weekly_record_1534_2020-05-11T00-00-00.000.json
    2026-04-15 21:06:45,621 | INFO | Skipping weekly_record_1534_2020-05-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,622 | INFO | Processing file: weekly_record_1535_2020-05-18T00-00-00.000.json
    2026-04-15 21:06:45,649 | INFO | Skipping weekly_record_1535_2020-05-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,650 | INFO | Processing file: weekly_record_1536_2020-05-25T00-00-00.000.json
    2026-04-15 21:06:45,720 | INFO | Skipping weekly_record_1536_2020-05-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,721 | INFO | Processing file: weekly_record_1537_2020-06-01T00-00-00.000.json
    2026-04-15 21:06:45,751 | INFO | Skipping weekly_record_1537_2020-06-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,752 | INFO | Processing file: weekly_record_1538_2020-06-08T00-00-00.000.json
    2026-04-15 21:06:45,782 | INFO | Skipping weekly_record_1538_2020-06-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,783 | INFO | Processing file: weekly_record_1539_2020-06-15T00-00-00.000.json
    2026-04-15 21:06:45,811 | INFO | Skipping weekly_record_1539_2020-06-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,812 | INFO | Processing file: weekly_record_153_1993-11-22T00-00-00.000.json
    2026-04-15 21:06:45,857 | INFO | Skipping weekly_record_153_1993-11-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,858 | INFO | Processing file: weekly_record_1540_2020-06-22T00-00-00.000.json
    2026-04-15 21:06:45,902 | INFO | Skipping weekly_record_1540_2020-06-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,902 | INFO | Processing file: weekly_record_1541_2020-06-29T00-00-00.000.json
    2026-04-15 21:06:45,929 | INFO | Skipping weekly_record_1541_2020-06-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,930 | INFO | Processing file: weekly_record_1542_2020-07-06T00-00-00.000.json
    2026-04-15 21:06:45,955 | INFO | Skipping weekly_record_1542_2020-07-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,956 | INFO | Processing file: weekly_record_1543_2020-07-13T00-00-00.000.json
    2026-04-15 21:06:45,982 | INFO | Skipping weekly_record_1543_2020-07-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:45,983 | INFO | Processing file: weekly_record_1544_2020-07-20T00-00-00.000.json
    2026-04-15 21:06:45,998 | INFO | Skipping weekly_record_1544_2020-07-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,000 | INFO | Processing file: weekly_record_1545_2020-07-27T00-00-00.000.json
    2026-04-15 21:06:46,021 | INFO | Skipping weekly_record_1545_2020-07-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,022 | INFO | Processing file: weekly_record_1546_2020-08-03T00-00-00.000.json
    2026-04-15 21:06:46,051 | INFO | Skipping weekly_record_1546_2020-08-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,052 | INFO | Processing file: weekly_record_1547_2020-08-10T00-00-00.000.json
    2026-04-15 21:06:46,075 | INFO | Skipping weekly_record_1547_2020-08-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,077 | INFO | Processing file: weekly_record_1548_2020-08-17T00-00-00.000.json
    2026-04-15 21:06:46,104 | INFO | Skipping weekly_record_1548_2020-08-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,105 | INFO | Processing file: weekly_record_1549_2020-08-24T00-00-00.000.json
    2026-04-15 21:06:46,129 | INFO | Skipping weekly_record_1549_2020-08-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,130 | INFO | Processing file: weekly_record_154_1993-11-29T00-00-00.000.json
    2026-04-15 21:06:46,156 | INFO | Skipping weekly_record_154_1993-11-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,157 | INFO | Processing file: weekly_record_1550_2020-08-31T00-00-00.000.json
    2026-04-15 21:06:46,179 | INFO | Skipping weekly_record_1550_2020-08-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,180 | INFO | Processing file: weekly_record_1551_2020-09-07T00-00-00.000.json
    2026-04-15 21:06:46,197 | INFO | Skipping weekly_record_1551_2020-09-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,198 | INFO | Processing file: weekly_record_1552_2020-09-14T00-00-00.000.json
    2026-04-15 21:06:46,213 | INFO | Skipping weekly_record_1552_2020-09-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,215 | INFO | Processing file: weekly_record_1553_2020-09-21T00-00-00.000.json
    2026-04-15 21:06:46,239 | INFO | Skipping weekly_record_1553_2020-09-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,240 | INFO | Processing file: weekly_record_1554_2020-09-28T00-00-00.000.json
    2026-04-15 21:06:46,262 | INFO | Skipping weekly_record_1554_2020-09-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,263 | INFO | Processing file: weekly_record_1555_2020-10-05T00-00-00.000.json
    2026-04-15 21:06:46,285 | INFO | Skipping weekly_record_1555_2020-10-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,286 | INFO | Processing file: weekly_record_1556_2020-10-12T00-00-00.000.json
    2026-04-15 21:06:46,308 | INFO | Skipping weekly_record_1556_2020-10-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,309 | INFO | Processing file: weekly_record_1557_2020-10-19T00-00-00.000.json
    2026-04-15 21:06:46,332 | INFO | Skipping weekly_record_1557_2020-10-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,334 | INFO | Processing file: weekly_record_1558_2020-10-26T00-00-00.000.json
    2026-04-15 21:06:46,364 | INFO | Skipping weekly_record_1558_2020-10-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,365 | INFO | Processing file: weekly_record_1559_2020-11-02T00-00-00.000.json
    2026-04-15 21:06:46,398 | INFO | Skipping weekly_record_1559_2020-11-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,399 | INFO | Processing file: weekly_record_155_1993-12-06T00-00-00.000.json
    2026-04-15 21:06:46,434 | INFO | Skipping weekly_record_155_1993-12-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,435 | INFO | Processing file: weekly_record_1560_2020-11-09T00-00-00.000.json
    2026-04-15 21:06:46,467 | INFO | Skipping weekly_record_1560_2020-11-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,469 | INFO | Processing file: weekly_record_1561_2020-11-16T00-00-00.000.json
    2026-04-15 21:06:46,505 | INFO | Skipping weekly_record_1561_2020-11-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,506 | INFO | Processing file: weekly_record_1562_2020-11-23T00-00-00.000.json
    2026-04-15 21:06:46,546 | INFO | Skipping weekly_record_1562_2020-11-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,547 | INFO | Processing file: weekly_record_1563_2020-11-30T00-00-00.000.json
    2026-04-15 21:06:46,565 | INFO | Skipping weekly_record_1563_2020-11-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,566 | INFO | Processing file: weekly_record_1564_2020-12-07T00-00-00.000.json
    2026-04-15 21:06:46,590 | INFO | Skipping weekly_record_1564_2020-12-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,591 | INFO | Processing file: weekly_record_1565_2020-12-14T00-00-00.000.json
    2026-04-15 21:06:46,624 | INFO | Skipping weekly_record_1565_2020-12-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,624 | INFO | Processing file: weekly_record_1566_2020-12-21T00-00-00.000.json
    2026-04-15 21:06:46,649 | INFO | Skipping weekly_record_1566_2020-12-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,650 | INFO | Processing file: weekly_record_1567_2020-12-28T00-00-00.000.json
    2026-04-15 21:06:46,663 | INFO | Skipping weekly_record_1567_2020-12-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,663 | INFO | Processing file: weekly_record_1568_2021-01-04T00-00-00.000.json
    2026-04-15 21:06:46,690 | INFO | Skipping weekly_record_1568_2021-01-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,690 | INFO | Processing file: weekly_record_1569_2021-01-11T00-00-00.000.json
    2026-04-15 21:06:46,715 | INFO | Skipping weekly_record_1569_2021-01-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,716 | INFO | Processing file: weekly_record_156_1993-12-13T00-00-00.000.json
    2026-04-15 21:06:46,744 | INFO | Skipping weekly_record_156_1993-12-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,745 | INFO | Processing file: weekly_record_1570_2021-01-18T00-00-00.000.json
    2026-04-15 21:06:46,780 | INFO | Skipping weekly_record_1570_2021-01-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,805 | INFO | Processing file: weekly_record_1571_2021-01-25T00-00-00.000.json
    2026-04-15 21:06:46,837 | INFO | Skipping weekly_record_1571_2021-01-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,839 | INFO | Processing file: weekly_record_1572_2021-02-01T00-00-00.000.json
    2026-04-15 21:06:46,865 | INFO | Skipping weekly_record_1572_2021-02-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,866 | INFO | Processing file: weekly_record_1573_2021-02-08T00-00-00.000.json
    2026-04-15 21:06:46,883 | INFO | Skipping weekly_record_1573_2021-02-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,885 | INFO | Processing file: weekly_record_1574_2021-02-15T00-00-00.000.json
    2026-04-15 21:06:46,910 | INFO | Skipping weekly_record_1574_2021-02-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,911 | INFO | Processing file: weekly_record_1575_2021-02-22T00-00-00.000.json
    2026-04-15 21:06:46,931 | INFO | Skipping weekly_record_1575_2021-02-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,932 | INFO | Processing file: weekly_record_1576_2021-03-01T00-00-00.000.json
    2026-04-15 21:06:46,956 | INFO | Skipping weekly_record_1576_2021-03-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,956 | INFO | Processing file: weekly_record_1577_2021-03-08T00-00-00.000.json
    2026-04-15 21:06:46,973 | INFO | Skipping weekly_record_1577_2021-03-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:46,975 | INFO | Processing file: weekly_record_1578_2021-03-15T00-00-00.000.json
    2026-04-15 21:06:47,000 | INFO | Skipping weekly_record_1578_2021-03-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,001 | INFO | Processing file: weekly_record_1579_2021-03-22T00-00-00.000.json
    2026-04-15 21:06:47,030 | INFO | Skipping weekly_record_1579_2021-03-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,031 | INFO | Processing file: weekly_record_157_1993-12-20T00-00-00.000.json
    2026-04-15 21:06:47,092 | INFO | Skipping weekly_record_157_1993-12-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,093 | INFO | Processing file: weekly_record_1580_2021-03-29T00-00-00.000.json
    2026-04-15 21:06:47,144 | INFO | Skipping weekly_record_1580_2021-03-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,145 | INFO | Processing file: weekly_record_1581_2021-04-05T00-00-00.000.json
    2026-04-15 21:06:47,201 | INFO | Skipping weekly_record_1581_2021-04-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,202 | INFO | Processing file: weekly_record_1582_2021-04-12T00-00-00.000.json
    2026-04-15 21:06:47,226 | INFO | Skipping weekly_record_1582_2021-04-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,227 | INFO | Processing file: weekly_record_1583_2021-04-19T00-00-00.000.json
    2026-04-15 21:06:47,242 | INFO | Skipping weekly_record_1583_2021-04-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,243 | INFO | Processing file: weekly_record_1584_2021-04-26T00-00-00.000.json
    2026-04-15 21:06:47,268 | INFO | Skipping weekly_record_1584_2021-04-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,270 | INFO | Processing file: weekly_record_1585_2021-05-03T00-00-00.000.json
    2026-04-15 21:06:47,295 | INFO | Skipping weekly_record_1585_2021-05-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,296 | INFO | Processing file: weekly_record_1586_2021-05-10T00-00-00.000.json
    2026-04-15 21:06:47,317 | INFO | Skipping weekly_record_1586_2021-05-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,318 | INFO | Processing file: weekly_record_1587_2021-05-17T00-00-00.000.json
    2026-04-15 21:06:47,345 | INFO | Skipping weekly_record_1587_2021-05-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,346 | INFO | Processing file: weekly_record_1588_2021-05-24T00-00-00.000.json
    2026-04-15 21:06:47,362 | INFO | Skipping weekly_record_1588_2021-05-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,363 | INFO | Processing file: weekly_record_1589_2021-05-31T00-00-00.000.json
    2026-04-15 21:06:47,402 | INFO | Skipping weekly_record_1589_2021-05-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,403 | INFO | Processing file: weekly_record_158_1993-12-27T00-00-00.000.json
    2026-04-15 21:06:47,445 | INFO | Skipping weekly_record_158_1993-12-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,446 | INFO | Processing file: weekly_record_1590_2021-06-07T00-00-00.000.json
    2026-04-15 21:06:47,472 | INFO | Skipping weekly_record_1590_2021-06-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,472 | INFO | Processing file: weekly_record_1591_2021-06-14T00-00-00.000.json
    2026-04-15 21:06:47,498 | INFO | Skipping weekly_record_1591_2021-06-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,498 | INFO | Processing file: weekly_record_1592_2021-06-21T00-00-00.000.json
    2026-04-15 21:06:47,524 | INFO | Skipping weekly_record_1592_2021-06-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,525 | INFO | Processing file: weekly_record_1593_2021-06-28T00-00-00.000.json
    2026-04-15 21:06:47,548 | INFO | Skipping weekly_record_1593_2021-06-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,549 | INFO | Processing file: weekly_record_1594_2021-07-05T00-00-00.000.json
    2026-04-15 21:06:47,573 | INFO | Skipping weekly_record_1594_2021-07-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,577 | INFO | Processing file: weekly_record_1595_2021-07-12T00-00-00.000.json
    2026-04-15 21:06:47,595 | INFO | Skipping weekly_record_1595_2021-07-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,596 | INFO | Processing file: weekly_record_1596_2021-07-19T00-00-00.000.json
    2026-04-15 21:06:47,613 | INFO | Skipping weekly_record_1596_2021-07-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,614 | INFO | Processing file: weekly_record_1597_2021-07-26T00-00-00.000.json
    2026-04-15 21:06:47,653 | INFO | Skipping weekly_record_1597_2021-07-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,654 | INFO | Processing file: weekly_record_1598_2021-08-02T00-00-00.000.json
    2026-04-15 21:06:47,680 | INFO | Skipping weekly_record_1598_2021-08-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,681 | INFO | Processing file: weekly_record_1599_2021-08-09T00-00-00.000.json
    2026-04-15 21:06:47,697 | INFO | Skipping weekly_record_1599_2021-08-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,698 | INFO | Processing file: weekly_record_159_1994-01-03T00-00-00.000.json
    2026-04-15 21:06:47,726 | INFO | Skipping weekly_record_159_1994-01-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,727 | INFO | Processing file: weekly_record_15_1991-04-01T00-00-00.000.json
    2026-04-15 21:06:47,744 | INFO | Skipping weekly_record_15_1991-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,745 | INFO | Processing file: weekly_record_1600_2021-08-16T00-00-00.000.json
    2026-04-15 21:06:47,771 | INFO | Skipping weekly_record_1600_2021-08-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,772 | INFO | Processing file: weekly_record_1601_2021-08-23T00-00-00.000.json
    2026-04-15 21:06:47,789 | INFO | Skipping weekly_record_1601_2021-08-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,790 | INFO | Processing file: weekly_record_1602_2021-08-30T00-00-00.000.json
    2026-04-15 21:06:47,807 | INFO | Skipping weekly_record_1602_2021-08-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,808 | INFO | Processing file: weekly_record_1603_2021-09-06T00-00-00.000.json
    2026-04-15 21:06:47,826 | INFO | Skipping weekly_record_1603_2021-09-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,828 | INFO | Processing file: weekly_record_1604_2021-09-13T00-00-00.000.json
    2026-04-15 21:06:47,879 | INFO | Skipping weekly_record_1604_2021-09-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,881 | INFO | Processing file: weekly_record_1605_2021-09-20T00-00-00.000.json
    2026-04-15 21:06:47,908 | INFO | Skipping weekly_record_1605_2021-09-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,910 | INFO | Processing file: weekly_record_1606_2021-09-27T00-00-00.000.json
    2026-04-15 21:06:47,935 | INFO | Skipping weekly_record_1606_2021-09-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,936 | INFO | Processing file: weekly_record_1607_2021-10-04T00-00-00.000.json
    2026-04-15 21:06:47,953 | INFO | Skipping weekly_record_1607_2021-10-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,954 | INFO | Processing file: weekly_record_1608_2021-10-11T00-00-00.000.json
    2026-04-15 21:06:47,976 | INFO | Skipping weekly_record_1608_2021-10-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,977 | INFO | Processing file: weekly_record_1609_2021-10-18T00-00-00.000.json
    2026-04-15 21:06:47,995 | INFO | Skipping weekly_record_1609_2021-10-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:47,996 | INFO | Processing file: weekly_record_160_1994-01-10T00-00-00.000.json
    2026-04-15 21:06:48,022 | INFO | Skipping weekly_record_160_1994-01-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,023 | INFO | Processing file: weekly_record_1610_2021-10-25T00-00-00.000.json
    2026-04-15 21:06:48,045 | INFO | Skipping weekly_record_1610_2021-10-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,046 | INFO | Processing file: weekly_record_1611_2021-11-01T00-00-00.000.json
    2026-04-15 21:06:48,071 | INFO | Skipping weekly_record_1611_2021-11-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,072 | INFO | Processing file: weekly_record_1612_2021-11-08T00-00-00.000.json
    2026-04-15 21:06:48,093 | INFO | Skipping weekly_record_1612_2021-11-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,094 | INFO | Processing file: weekly_record_1613_2021-11-15T00-00-00.000.json
    2026-04-15 21:06:48,114 | INFO | Skipping weekly_record_1613_2021-11-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,115 | INFO | Processing file: weekly_record_1614_2021-11-22T00-00-00.000.json
    2026-04-15 21:06:48,138 | INFO | Skipping weekly_record_1614_2021-11-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,139 | INFO | Processing file: weekly_record_1615_2021-11-29T00-00-00.000.json
    2026-04-15 21:06:48,164 | INFO | Skipping weekly_record_1615_2021-11-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,165 | INFO | Processing file: weekly_record_1616_2021-12-06T00-00-00.000.json
    2026-04-15 21:06:48,182 | INFO | Skipping weekly_record_1616_2021-12-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,183 | INFO | Processing file: weekly_record_1617_2021-12-13T00-00-00.000.json
    2026-04-15 21:06:48,208 | INFO | Skipping weekly_record_1617_2021-12-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,210 | INFO | Processing file: weekly_record_1618_2021-12-20T00-00-00.000.json
    2026-04-15 21:06:48,234 | INFO | Skipping weekly_record_1618_2021-12-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,236 | INFO | Processing file: weekly_record_1619_2021-12-27T00-00-00.000.json
    2026-04-15 21:06:48,261 | INFO | Skipping weekly_record_1619_2021-12-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,261 | INFO | Processing file: weekly_record_161_1994-01-17T00-00-00.000.json
    2026-04-15 21:06:48,287 | INFO | Skipping weekly_record_161_1994-01-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,288 | INFO | Processing file: weekly_record_1620_2022-01-03T00-00-00.000.json
    2026-04-15 21:06:48,306 | INFO | Skipping weekly_record_1620_2022-01-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,308 | INFO | Processing file: weekly_record_1621_2022-01-10T00-00-00.000.json
    2026-04-15 21:06:48,336 | INFO | Skipping weekly_record_1621_2022-01-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,337 | INFO | Processing file: weekly_record_1622_2022-01-17T00-00-00.000.json
    2026-04-15 21:06:48,364 | INFO | Skipping weekly_record_1622_2022-01-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,365 | INFO | Processing file: weekly_record_1623_2022-01-24T00-00-00.000.json
    2026-04-15 21:06:48,382 | INFO | Skipping weekly_record_1623_2022-01-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,383 | INFO | Processing file: weekly_record_1624_2022-01-31T00-00-00.000.json
    2026-04-15 21:06:48,409 | INFO | Skipping weekly_record_1624_2022-01-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,410 | INFO | Processing file: weekly_record_1625_2022-02-07T00-00-00.000.json
    2026-04-15 21:06:48,435 | INFO | Skipping weekly_record_1625_2022-02-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,436 | INFO | Processing file: weekly_record_1626_2022-02-14T00-00-00.000.json
    2026-04-15 21:06:48,463 | INFO | Skipping weekly_record_1626_2022-02-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,464 | INFO | Processing file: weekly_record_1627_2022-02-21T00-00-00.000.json
    2026-04-15 21:06:48,486 | INFO | Skipping weekly_record_1627_2022-02-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,487 | INFO | Processing file: weekly_record_1628_2022-02-28T00-00-00.000.json
    2026-04-15 21:06:48,514 | INFO | Skipping weekly_record_1628_2022-02-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,515 | INFO | Processing file: weekly_record_1629_2022-03-07T00-00-00.000.json
    2026-04-15 21:06:48,530 | INFO | Skipping weekly_record_1629_2022-03-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,531 | INFO | Processing file: weekly_record_162_1994-01-24T00-00-00.000.json
    2026-04-15 21:06:48,548 | INFO | Skipping weekly_record_162_1994-01-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,550 | INFO | Processing file: weekly_record_1630_2022-03-14T00-00-00.000.json
    2026-04-15 21:06:48,575 | INFO | Skipping weekly_record_1630_2022-03-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,576 | INFO | Processing file: weekly_record_1631_2022-03-21T00-00-00.000.json
    2026-04-15 21:06:48,596 | INFO | Skipping weekly_record_1631_2022-03-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,597 | INFO | Processing file: weekly_record_1632_2022-03-28T00-00-00.000.json
    2026-04-15 21:06:48,612 | INFO | Skipping weekly_record_1632_2022-03-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,613 | INFO | Processing file: weekly_record_1633_2022-04-04T00-00-00.000.json
    2026-04-15 21:06:48,643 | INFO | Skipping weekly_record_1633_2022-04-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,644 | INFO | Processing file: weekly_record_1634_2022-04-11T00-00-00.000.json
    2026-04-15 21:06:48,663 | INFO | Skipping weekly_record_1634_2022-04-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,664 | INFO | Processing file: weekly_record_1635_2022-04-18T00-00-00.000.json
    2026-04-15 21:06:48,690 | INFO | Skipping weekly_record_1635_2022-04-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,691 | INFO | Processing file: weekly_record_1636_2022-04-25T00-00-00.000.json
    2026-04-15 21:06:48,711 | INFO | Skipping weekly_record_1636_2022-04-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,713 | INFO | Processing file: weekly_record_1637_2022-05-02T00-00-00.000.json
    2026-04-15 21:06:48,734 | INFO | Skipping weekly_record_1637_2022-05-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,741 | INFO | Processing file: weekly_record_1638_2022-05-09T00-00-00.000.json
    2026-04-15 21:06:48,804 | INFO | Skipping weekly_record_1638_2022-05-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,805 | INFO | Processing file: weekly_record_1639_2022-05-16T00-00-00.000.json
    2026-04-15 21:06:48,825 | INFO | Skipping weekly_record_1639_2022-05-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,826 | INFO | Processing file: weekly_record_163_1994-01-31T00-00-00.000.json
    2026-04-15 21:06:48,843 | INFO | Skipping weekly_record_163_1994-01-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,844 | INFO | Processing file: weekly_record_1640_2022-05-23T00-00-00.000.json
    2026-04-15 21:06:48,870 | INFO | Skipping weekly_record_1640_2022-05-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,871 | INFO | Processing file: weekly_record_1641_2022-05-30T00-00-00.000.json
    2026-04-15 21:06:48,898 | INFO | Skipping weekly_record_1641_2022-05-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,898 | INFO | Processing file: weekly_record_1642_2022-06-06T00-00-00.000.json
    2026-04-15 21:06:48,924 | INFO | Skipping weekly_record_1642_2022-06-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,925 | INFO | Processing file: weekly_record_1643_2022-06-13T00-00-00.000.json
    2026-04-15 21:06:48,955 | INFO | Skipping weekly_record_1643_2022-06-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,955 | INFO | Processing file: weekly_record_1644_2022-06-20T00-00-00.000.json
    2026-04-15 21:06:48,982 | INFO | Skipping weekly_record_1644_2022-06-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:48,983 | INFO | Processing file: weekly_record_1645_2022-06-27T00-00-00.000.json
    2026-04-15 21:06:49,000 | INFO | Skipping weekly_record_1645_2022-06-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,001 | INFO | Processing file: weekly_record_1646_2022-07-04T00-00-00.000.json
    2026-04-15 21:06:49,027 | INFO | Skipping weekly_record_1646_2022-07-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,027 | INFO | Processing file: weekly_record_1647_2022-07-11T00-00-00.000.json
    2026-04-15 21:06:49,046 | INFO | Skipping weekly_record_1647_2022-07-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,047 | INFO | Processing file: weekly_record_1648_2022-07-18T00-00-00.000.json
    2026-04-15 21:06:49,072 | INFO | Skipping weekly_record_1648_2022-07-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,073 | INFO | Processing file: weekly_record_1649_2022-07-25T00-00-00.000.json
    2026-04-15 21:06:49,099 | INFO | Skipping weekly_record_1649_2022-07-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,100 | INFO | Processing file: weekly_record_164_1994-02-07T00-00-00.000.json
    2026-04-15 21:06:49,117 | INFO | Skipping weekly_record_164_1994-02-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,119 | INFO | Processing file: weekly_record_1650_2022-08-01T00-00-00.000.json
    2026-04-15 21:06:49,140 | INFO | Skipping weekly_record_1650_2022-08-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,141 | INFO | Processing file: weekly_record_1651_2022-08-08T00-00-00.000.json
    2026-04-15 21:06:49,158 | INFO | Skipping weekly_record_1651_2022-08-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,159 | INFO | Processing file: weekly_record_1652_2022-08-15T00-00-00.000.json
    2026-04-15 21:06:49,183 | INFO | Skipping weekly_record_1652_2022-08-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,185 | INFO | Processing file: weekly_record_1653_2022-08-22T00-00-00.000.json
    2026-04-15 21:06:49,203 | INFO | Skipping weekly_record_1653_2022-08-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,204 | INFO | Processing file: weekly_record_1654_2022-08-29T00-00-00.000.json
    2026-04-15 21:06:49,221 | INFO | Skipping weekly_record_1654_2022-08-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,222 | INFO | Processing file: weekly_record_1655_2022-09-05T00-00-00.000.json
    2026-04-15 21:06:49,238 | INFO | Skipping weekly_record_1655_2022-09-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,239 | INFO | Processing file: weekly_record_1656_2022-09-12T00-00-00.000.json
    2026-04-15 21:06:49,265 | INFO | Skipping weekly_record_1656_2022-09-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,266 | INFO | Processing file: weekly_record_1657_2022-09-19T00-00-00.000.json
    2026-04-15 21:06:49,289 | INFO | Skipping weekly_record_1657_2022-09-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,291 | INFO | Processing file: weekly_record_1658_2022-09-26T00-00-00.000.json
    2026-04-15 21:06:49,306 | INFO | Skipping weekly_record_1658_2022-09-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,307 | INFO | Processing file: weekly_record_1659_2022-10-03T00-00-00.000.json
    2026-04-15 21:06:49,325 | INFO | Skipping weekly_record_1659_2022-10-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,326 | INFO | Processing file: weekly_record_165_1994-02-14T00-00-00.000.json
    2026-04-15 21:06:49,341 | INFO | Skipping weekly_record_165_1994-02-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,342 | INFO | Processing file: weekly_record_1660_2022-10-10T00-00-00.000.json
    2026-04-15 21:06:49,369 | INFO | Skipping weekly_record_1660_2022-10-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,370 | INFO | Processing file: weekly_record_1661_2022-10-17T00-00-00.000.json
    2026-04-15 21:06:49,387 | INFO | Skipping weekly_record_1661_2022-10-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,388 | INFO | Processing file: weekly_record_1662_2022-10-24T00-00-00.000.json
    2026-04-15 21:06:49,406 | INFO | Skipping weekly_record_1662_2022-10-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,407 | INFO | Processing file: weekly_record_1663_2022-10-31T00-00-00.000.json
    2026-04-15 21:06:49,433 | INFO | Skipping weekly_record_1663_2022-10-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,435 | INFO | Processing file: weekly_record_1664_2022-11-07T00-00-00.000.json
    2026-04-15 21:06:49,451 | INFO | Skipping weekly_record_1664_2022-11-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,453 | INFO | Processing file: weekly_record_1665_2022-11-14T00-00-00.000.json
    2026-04-15 21:06:49,480 | INFO | Skipping weekly_record_1665_2022-11-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,480 | INFO | Processing file: weekly_record_1666_2022-11-21T00-00-00.000.json
    2026-04-15 21:06:49,506 | INFO | Skipping weekly_record_1666_2022-11-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,506 | INFO | Processing file: weekly_record_1667_2022-11-28T00-00-00.000.json
    2026-04-15 21:06:49,528 | INFO | Skipping weekly_record_1667_2022-11-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,529 | INFO | Processing file: weekly_record_1668_2022-12-05T00-00-00.000.json
    2026-04-15 21:06:49,551 | INFO | Skipping weekly_record_1668_2022-12-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,552 | INFO | Processing file: weekly_record_1669_2022-12-12T00-00-00.000.json
    2026-04-15 21:06:49,581 | INFO | Skipping weekly_record_1669_2022-12-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,582 | INFO | Processing file: weekly_record_166_1994-02-21T00-00-00.000.json
    2026-04-15 21:06:49,626 | INFO | Skipping weekly_record_166_1994-02-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,629 | INFO | Processing file: weekly_record_1670_2022-12-19T00-00-00.000.json
    2026-04-15 21:06:49,657 | INFO | Skipping weekly_record_1670_2022-12-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,658 | INFO | Processing file: weekly_record_1671_2022-12-26T00-00-00.000.json
    2026-04-15 21:06:49,686 | INFO | Skipping weekly_record_1671_2022-12-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,687 | INFO | Processing file: weekly_record_1672_2023-01-02T00-00-00.000.json
    2026-04-15 21:06:49,715 | INFO | Skipping weekly_record_1672_2023-01-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,716 | INFO | Processing file: weekly_record_1673_2023-01-09T00-00-00.000.json
    2026-04-15 21:06:49,743 | INFO | Skipping weekly_record_1673_2023-01-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,745 | INFO | Processing file: weekly_record_1674_2023-01-16T00-00-00.000.json
    2026-04-15 21:06:49,777 | INFO | Skipping weekly_record_1674_2023-01-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,778 | INFO | Processing file: weekly_record_1675_2023-01-23T00-00-00.000.json
    2026-04-15 21:06:49,812 | INFO | Skipping weekly_record_1675_2023-01-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,813 | INFO | Processing file: weekly_record_1676_2023-01-30T00-00-00.000.json
    2026-04-15 21:06:49,832 | INFO | Skipping weekly_record_1676_2023-01-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,833 | INFO | Processing file: weekly_record_1677_2023-02-06T00-00-00.000.json
    2026-04-15 21:06:49,858 | INFO | Skipping weekly_record_1677_2023-02-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,860 | INFO | Processing file: weekly_record_1678_2023-02-13T00-00-00.000.json
    2026-04-15 21:06:49,885 | INFO | Skipping weekly_record_1678_2023-02-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,886 | INFO | Processing file: weekly_record_1679_2023-02-20T00-00-00.000.json
    2026-04-15 21:06:49,911 | INFO | Skipping weekly_record_1679_2023-02-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,912 | INFO | Processing file: weekly_record_167_1994-02-28T00-00-00.000.json
    2026-04-15 21:06:49,929 | INFO | Skipping weekly_record_167_1994-02-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,930 | INFO | Processing file: weekly_record_1680_2023-02-27T00-00-00.000.json
    2026-04-15 21:06:49,954 | INFO | Skipping weekly_record_1680_2023-02-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,955 | INFO | Processing file: weekly_record_1681_2023-03-06T00-00-00.000.json
    2026-04-15 21:06:49,991 | INFO | Skipping weekly_record_1681_2023-03-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:49,992 | INFO | Processing file: weekly_record_1682_2023-03-13T00-00-00.000.json
    2026-04-15 21:06:50,020 | INFO | Skipping weekly_record_1682_2023-03-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,021 | INFO | Processing file: weekly_record_1683_2023-03-20T00-00-00.000.json
    2026-04-15 21:06:50,039 | INFO | Skipping weekly_record_1683_2023-03-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,039 | INFO | Processing file: weekly_record_1684_2023-03-27T00-00-00.000.json
    2026-04-15 21:06:50,065 | INFO | Skipping weekly_record_1684_2023-03-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,066 | INFO | Processing file: weekly_record_1685_2023-04-03T00-00-00.000.json
    2026-04-15 21:06:50,088 | INFO | Skipping weekly_record_1685_2023-04-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,089 | INFO | Processing file: weekly_record_1686_2023-04-10T00-00-00.000.json
    2026-04-15 21:06:50,117 | INFO | Skipping weekly_record_1686_2023-04-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,118 | INFO | Processing file: weekly_record_1687_2023-04-17T00-00-00.000.json
    2026-04-15 21:06:50,142 | INFO | Skipping weekly_record_1687_2023-04-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,143 | INFO | Processing file: weekly_record_1688_2023-04-24T00-00-00.000.json
    2026-04-15 21:06:50,172 | INFO | Skipping weekly_record_1688_2023-04-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,173 | INFO | Processing file: weekly_record_1689_2023-05-01T00-00-00.000.json
    2026-04-15 21:06:50,197 | INFO | Skipping weekly_record_1689_2023-05-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,198 | INFO | Processing file: weekly_record_168_1994-03-07T00-00-00.000.json
    2026-04-15 21:06:50,213 | INFO | Skipping weekly_record_168_1994-03-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,214 | INFO | Processing file: weekly_record_1690_2023-05-08T00-00-00.000.json
    2026-04-15 21:06:50,234 | INFO | Skipping weekly_record_1690_2023-05-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,236 | INFO | Processing file: weekly_record_1691_2023-05-15T00-00-00.000.json
    2026-04-15 21:06:50,251 | INFO | Skipping weekly_record_1691_2023-05-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,252 | INFO | Processing file: weekly_record_1692_2023-05-22T00-00-00.000.json
    2026-04-15 21:06:50,276 | INFO | Skipping weekly_record_1692_2023-05-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,278 | INFO | Processing file: weekly_record_1693_2023-05-29T00-00-00.000.json
    2026-04-15 21:06:50,295 | INFO | Skipping weekly_record_1693_2023-05-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,296 | INFO | Processing file: weekly_record_1694_2023-06-05T00-00-00.000.json
    2026-04-15 21:06:50,328 | INFO | Skipping weekly_record_1694_2023-06-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,329 | INFO | Processing file: weekly_record_1695_2023-06-12T00-00-00.000.json
    2026-04-15 21:06:50,352 | INFO | Skipping weekly_record_1695_2023-06-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,353 | INFO | Processing file: weekly_record_1696_2023-06-19T00-00-00.000.json
    2026-04-15 21:06:50,368 | INFO | Skipping weekly_record_1696_2023-06-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,371 | INFO | Processing file: weekly_record_1697_2023-06-26T00-00-00.000.json
    2026-04-15 21:06:50,390 | INFO | Skipping weekly_record_1697_2023-06-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,391 | INFO | Processing file: weekly_record_1698_2023-07-03T00-00-00.000.json
    2026-04-15 21:06:50,413 | INFO | Skipping weekly_record_1698_2023-07-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,414 | INFO | Processing file: weekly_record_1699_2023-07-10T00-00-00.000.json
    2026-04-15 21:06:50,450 | INFO | Skipping weekly_record_1699_2023-07-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,451 | INFO | Processing file: weekly_record_169_1994-03-14T00-00-00.000.json
    2026-04-15 21:06:50,476 | INFO | Skipping weekly_record_169_1994-03-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,477 | INFO | Processing file: weekly_record_16_1991-04-08T00-00-00.000.json
    2026-04-15 21:06:50,531 | INFO | Skipping weekly_record_16_1991-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,532 | INFO | Processing file: weekly_record_1700_2023-07-17T00-00-00.000.json
    2026-04-15 21:06:50,558 | INFO | Skipping weekly_record_1700_2023-07-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,559 | INFO | Processing file: weekly_record_1701_2023-07-24T00-00-00.000.json
    2026-04-15 21:06:50,588 | INFO | Skipping weekly_record_1701_2023-07-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,589 | INFO | Processing file: weekly_record_1702_2023-07-31T00-00-00.000.json
    2026-04-15 21:06:50,620 | INFO | Skipping weekly_record_1702_2023-07-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,621 | INFO | Processing file: weekly_record_1703_2023-08-07T00-00-00.000.json
    2026-04-15 21:06:50,648 | INFO | Skipping weekly_record_1703_2023-08-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,648 | INFO | Processing file: weekly_record_1704_2023-08-14T00-00-00.000.json
    2026-04-15 21:06:50,675 | INFO | Skipping weekly_record_1704_2023-08-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,676 | INFO | Processing file: weekly_record_1705_2023-08-21T00-00-00.000.json
    2026-04-15 21:06:50,702 | INFO | Skipping weekly_record_1705_2023-08-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,703 | INFO | Processing file: weekly_record_1706_2023-08-28T00-00-00.000.json
    2026-04-15 21:06:50,730 | INFO | Skipping weekly_record_1706_2023-08-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,731 | INFO | Processing file: weekly_record_1707_2023-09-04T00-00-00.000.json
    2026-04-15 21:06:50,752 | INFO | Skipping weekly_record_1707_2023-09-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,753 | INFO | Processing file: weekly_record_1708_2023-09-11T00-00-00.000.json
    2026-04-15 21:06:50,777 | INFO | Skipping weekly_record_1708_2023-09-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,778 | INFO | Processing file: weekly_record_1709_2023-09-18T00-00-00.000.json
    2026-04-15 21:06:50,805 | INFO | Skipping weekly_record_1709_2023-09-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,807 | INFO | Processing file: weekly_record_170_1994-03-21T00-00-00.000.json
    2026-04-15 21:06:50,828 | INFO | Skipping weekly_record_170_1994-03-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,828 | INFO | Processing file: weekly_record_1710_2023-09-25T00-00-00.000.json
    2026-04-15 21:06:50,846 | INFO | Skipping weekly_record_1710_2023-09-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,847 | INFO | Processing file: weekly_record_1711_2023-10-02T00-00-00.000.json
    2026-04-15 21:06:50,863 | INFO | Skipping weekly_record_1711_2023-10-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,863 | INFO | Processing file: weekly_record_1712_2023-10-09T00-00-00.000.json
    2026-04-15 21:06:50,890 | INFO | Skipping weekly_record_1712_2023-10-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,891 | INFO | Processing file: weekly_record_1713_2023-10-16T00-00-00.000.json
    2026-04-15 21:06:50,913 | INFO | Skipping weekly_record_1713_2023-10-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,914 | INFO | Processing file: weekly_record_1714_2023-10-23T00-00-00.000.json
    2026-04-15 21:06:50,943 | INFO | Skipping weekly_record_1714_2023-10-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,944 | INFO | Processing file: weekly_record_1715_2023-10-30T00-00-00.000.json
    2026-04-15 21:06:50,967 | INFO | Skipping weekly_record_1715_2023-10-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,968 | INFO | Processing file: weekly_record_1716_2023-11-06T00-00-00.000.json
    2026-04-15 21:06:50,984 | INFO | Skipping weekly_record_1716_2023-11-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:50,985 | INFO | Processing file: weekly_record_1717_2023-11-13T00-00-00.000.json
    2026-04-15 21:06:51,008 | INFO | Skipping weekly_record_1717_2023-11-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,009 | INFO | Processing file: weekly_record_1718_2023-11-20T00-00-00.000.json
    2026-04-15 21:06:51,025 | INFO | Skipping weekly_record_1718_2023-11-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,026 | INFO | Processing file: weekly_record_1719_2023-11-27T00-00-00.000.json
    2026-04-15 21:06:51,048 | INFO | Skipping weekly_record_1719_2023-11-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,048 | INFO | Processing file: weekly_record_171_1994-03-28T00-00-00.000.json
    2026-04-15 21:06:51,066 | INFO | Skipping weekly_record_171_1994-03-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,067 | INFO | Processing file: weekly_record_1720_2023-12-04T00-00-00.000.json
    2026-04-15 21:06:51,093 | INFO | Skipping weekly_record_1720_2023-12-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,094 | INFO | Processing file: weekly_record_1721_2023-12-11T00-00-00.000.json
    2026-04-15 21:06:51,124 | INFO | Skipping weekly_record_1721_2023-12-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,125 | INFO | Processing file: weekly_record_1722_2023-12-18T00-00-00.000.json
    2026-04-15 21:06:51,151 | INFO | Skipping weekly_record_1722_2023-12-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,153 | INFO | Processing file: weekly_record_1723_2023-12-25T00-00-00.000.json
    2026-04-15 21:06:51,178 | INFO | Skipping weekly_record_1723_2023-12-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,179 | INFO | Processing file: weekly_record_1724_2024-01-01T00-00-00.000.json
    2026-04-15 21:06:51,202 | INFO | Skipping weekly_record_1724_2024-01-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,203 | INFO | Processing file: weekly_record_1725_2024-01-08T00-00-00.000.json
    2026-04-15 21:06:51,220 | INFO | Skipping weekly_record_1725_2024-01-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,221 | INFO | Processing file: weekly_record_1726_2024-01-15T00-00-00.000.json
    2026-04-15 21:06:51,243 | INFO | Skipping weekly_record_1726_2024-01-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,244 | INFO | Processing file: weekly_record_1727_2024-01-22T00-00-00.000.json
    2026-04-15 21:06:51,268 | INFO | Skipping weekly_record_1727_2024-01-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,269 | INFO | Processing file: weekly_record_1728_2024-01-29T00-00-00.000.json
    2026-04-15 21:06:51,338 | INFO | Skipping weekly_record_1728_2024-01-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,339 | INFO | Processing file: weekly_record_1729_2024-02-05T00-00-00.000.json
    2026-04-15 21:06:51,429 | INFO | Skipping weekly_record_1729_2024-02-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,437 | INFO | Processing file: weekly_record_172_1994-04-04T00-00-00.000.json
    2026-04-15 21:06:51,469 | INFO | Skipping weekly_record_172_1994-04-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,470 | INFO | Processing file: weekly_record_1730_2024-02-12T00-00-00.000.json
    2026-04-15 21:06:51,496 | INFO | Skipping weekly_record_1730_2024-02-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,496 | INFO | Processing file: weekly_record_1731_2024-02-19T00-00-00.000.json
    2026-04-15 21:06:51,521 | INFO | Skipping weekly_record_1731_2024-02-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,522 | INFO | Processing file: weekly_record_1732_2024-02-26T00-00-00.000.json
    2026-04-15 21:06:51,540 | INFO | Skipping weekly_record_1732_2024-02-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,545 | INFO | Processing file: weekly_record_1733_2024-03-04T00-00-00.000.json
    2026-04-15 21:06:51,564 | INFO | Skipping weekly_record_1733_2024-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,565 | INFO | Processing file: weekly_record_1734_2024-03-11T00-00-00.000.json
    2026-04-15 21:06:51,583 | INFO | Skipping weekly_record_1734_2024-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,584 | INFO | Processing file: weekly_record_1735_2024-03-18T00-00-00.000.json
    2026-04-15 21:06:51,614 | INFO | Skipping weekly_record_1735_2024-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,615 | INFO | Processing file: weekly_record_1736_2024-03-25T00-00-00.000.json
    2026-04-15 21:06:51,647 | INFO | Skipping weekly_record_1736_2024-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,648 | INFO | Processing file: weekly_record_1737_2024-04-01T00-00-00.000.json
    2026-04-15 21:06:51,685 | INFO | Skipping weekly_record_1737_2024-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,686 | INFO | Processing file: weekly_record_1738_2024-04-08T00-00-00.000.json
    2026-04-15 21:06:51,720 | INFO | Skipping weekly_record_1738_2024-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,720 | INFO | Processing file: weekly_record_1739_2024-04-15T00-00-00.000.json
    2026-04-15 21:06:51,746 | INFO | Skipping weekly_record_1739_2024-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,747 | INFO | Processing file: weekly_record_173_1994-04-11T00-00-00.000.json
    2026-04-15 21:06:51,803 | INFO | Skipping weekly_record_173_1994-04-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,804 | INFO | Processing file: weekly_record_1740_2024-04-22T00-00-00.000.json
    2026-04-15 21:06:51,831 | INFO | Skipping weekly_record_1740_2024-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,832 | INFO | Processing file: weekly_record_1741_2024-04-29T00-00-00.000.json
    2026-04-15 21:06:51,858 | INFO | Skipping weekly_record_1741_2024-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,859 | INFO | Processing file: weekly_record_1742_2024-05-06T00-00-00.000.json
    2026-04-15 21:06:51,901 | INFO | Skipping weekly_record_1742_2024-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,902 | INFO | Processing file: weekly_record_1743_2024-05-13T00-00-00.000.json
    2026-04-15 21:06:51,927 | INFO | Skipping weekly_record_1743_2024-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,928 | INFO | Processing file: weekly_record_1744_2024-05-20T00-00-00.000.json
    2026-04-15 21:06:51,946 | INFO | Skipping weekly_record_1744_2024-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,947 | INFO | Processing file: weekly_record_1745_2024-05-27T00-00-00.000.json
    2026-04-15 21:06:51,968 | INFO | Skipping weekly_record_1745_2024-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,969 | INFO | Processing file: weekly_record_1746_2024-06-03T00-00-00.000.json
    2026-04-15 21:06:51,997 | INFO | Skipping weekly_record_1746_2024-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:51,998 | INFO | Processing file: weekly_record_1747_2024-06-10T00-00-00.000.json
    2026-04-15 21:06:52,035 | INFO | Skipping weekly_record_1747_2024-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,037 | INFO | Processing file: weekly_record_1748_2024-06-17T00-00-00.000.json
    2026-04-15 21:06:52,076 | INFO | Skipping weekly_record_1748_2024-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,077 | INFO | Processing file: weekly_record_1749_2024-06-24T00-00-00.000.json
    2026-04-15 21:06:52,111 | INFO | Skipping weekly_record_1749_2024-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,112 | INFO | Processing file: weekly_record_174_1994-04-18T00-00-00.000.json
    2026-04-15 21:06:52,200 | INFO | Skipping weekly_record_174_1994-04-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,201 | INFO | Processing file: weekly_record_1750_2024-07-01T00-00-00.000.json
    2026-04-15 21:06:52,244 | INFO | Skipping weekly_record_1750_2024-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,246 | INFO | Processing file: weekly_record_1751_2024-07-08T00-00-00.000.json
    2026-04-15 21:06:52,338 | INFO | Skipping weekly_record_1751_2024-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,339 | INFO | Processing file: weekly_record_1752_2024-07-15T00-00-00.000.json
    2026-04-15 21:06:52,474 | INFO | Skipping weekly_record_1752_2024-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,476 | INFO | Processing file: weekly_record_1753_2024-07-22T00-00-00.000.json
    2026-04-15 21:06:52,604 | INFO | Skipping weekly_record_1753_2024-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,606 | INFO | Processing file: weekly_record_1754_2024-07-29T00-00-00.000.json
    2026-04-15 21:06:52,655 | INFO | Skipping weekly_record_1754_2024-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,657 | INFO | Processing file: weekly_record_1755_2024-08-05T00-00-00.000.json
    2026-04-15 21:06:52,805 | INFO | Skipping weekly_record_1755_2024-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,806 | INFO | Processing file: weekly_record_1756_2024-08-12T00-00-00.000.json
    2026-04-15 21:06:52,839 | INFO | Skipping weekly_record_1756_2024-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,840 | INFO | Processing file: weekly_record_1757_2024-08-19T00-00-00.000.json
    2026-04-15 21:06:52,867 | INFO | Skipping weekly_record_1757_2024-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,868 | INFO | Processing file: weekly_record_1758_2024-08-26T00-00-00.000.json
    2026-04-15 21:06:52,896 | INFO | Skipping weekly_record_1758_2024-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,897 | INFO | Processing file: weekly_record_1759_2024-09-02T00-00-00.000.json
    2026-04-15 21:06:52,931 | INFO | Skipping weekly_record_1759_2024-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,932 | INFO | Processing file: weekly_record_175_1994-04-25T00-00-00.000.json
    2026-04-15 21:06:52,958 | INFO | Skipping weekly_record_175_1994-04-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,960 | INFO | Processing file: weekly_record_1760_2024-09-09T00-00-00.000.json
    2026-04-15 21:06:52,998 | INFO | Skipping weekly_record_1760_2024-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:52,998 | INFO | Processing file: weekly_record_1761_2024-09-16T00-00-00.000.json
    2026-04-15 21:06:53,031 | INFO | Skipping weekly_record_1761_2024-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,032 | INFO | Processing file: weekly_record_1762_2024-09-23T00-00-00.000.json
    2026-04-15 21:06:53,086 | INFO | Skipping weekly_record_1762_2024-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,087 | INFO | Processing file: weekly_record_1763_2024-09-30T00-00-00.000.json
    2026-04-15 21:06:53,156 | INFO | Skipping weekly_record_1763_2024-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,157 | INFO | Processing file: weekly_record_1764_2024-10-07T00-00-00.000.json
    2026-04-15 21:06:53,231 | INFO | Skipping weekly_record_1764_2024-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,232 | INFO | Processing file: weekly_record_1765_2024-10-14T00-00-00.000.json
    2026-04-15 21:06:53,280 | INFO | Skipping weekly_record_1765_2024-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,281 | INFO | Processing file: weekly_record_1766_2024-10-21T00-00-00.000.json
    2026-04-15 21:06:53,313 | INFO | Skipping weekly_record_1766_2024-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,314 | INFO | Processing file: weekly_record_1767_2024-10-28T00-00-00.000.json
    2026-04-15 21:06:53,348 | INFO | Skipping weekly_record_1767_2024-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,350 | INFO | Processing file: weekly_record_1768_2024-11-04T00-00-00.000.json
    2026-04-15 21:06:53,378 | INFO | Skipping weekly_record_1768_2024-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,379 | INFO | Processing file: weekly_record_1769_2024-11-11T00-00-00.000.json
    2026-04-15 21:06:53,405 | INFO | Skipping weekly_record_1769_2024-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,406 | INFO | Processing file: weekly_record_176_1994-05-02T00-00-00.000.json
    2026-04-15 21:06:53,437 | INFO | Skipping weekly_record_176_1994-05-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,439 | INFO | Processing file: weekly_record_1770_2024-11-18T00-00-00.000.json
    2026-04-15 21:06:53,464 | INFO | Skipping weekly_record_1770_2024-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,464 | INFO | Processing file: weekly_record_1771_2024-11-25T00-00-00.000.json
    2026-04-15 21:06:53,503 | INFO | Skipping weekly_record_1771_2024-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,503 | INFO | Processing file: weekly_record_1772_2024-12-02T00-00-00.000.json
    2026-04-15 21:06:53,537 | INFO | Skipping weekly_record_1772_2024-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,537 | INFO | Processing file: weekly_record_1773_2024-12-09T00-00-00.000.json
    2026-04-15 21:06:53,566 | INFO | Skipping weekly_record_1773_2024-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,567 | INFO | Processing file: weekly_record_1774_2024-12-16T00-00-00.000.json
    2026-04-15 21:06:53,596 | INFO | Skipping weekly_record_1774_2024-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,597 | INFO | Processing file: weekly_record_1775_2024-12-23T00-00-00.000.json
    2026-04-15 21:06:53,621 | INFO | Skipping weekly_record_1775_2024-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,624 | INFO | Processing file: weekly_record_1776_2024-12-30T00-00-00.000.json
    2026-04-15 21:06:53,647 | INFO | Skipping weekly_record_1776_2024-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,649 | INFO | Processing file: weekly_record_1777_2025-01-06T00-00-00.000.json
    2026-04-15 21:06:53,680 | INFO | Skipping weekly_record_1777_2025-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,681 | INFO | Processing file: weekly_record_1778_2025-01-13T00-00-00.000.json
    2026-04-15 21:06:53,709 | INFO | Skipping weekly_record_1778_2025-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,711 | INFO | Processing file: weekly_record_1779_2025-01-20T00-00-00.000.json
    2026-04-15 21:06:53,746 | INFO | Skipping weekly_record_1779_2025-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,747 | INFO | Processing file: weekly_record_177_1994-05-09T00-00-00.000.json
    2026-04-15 21:06:53,764 | INFO | Skipping weekly_record_177_1994-05-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,765 | INFO | Processing file: weekly_record_1780_2025-01-27T00-00-00.000.json
    2026-04-15 21:06:53,786 | INFO | Skipping weekly_record_1780_2025-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,788 | INFO | Processing file: weekly_record_1781_2025-02-03T00-00-00.000.json
    2026-04-15 21:06:53,814 | INFO | Skipping weekly_record_1781_2025-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,815 | INFO | Processing file: weekly_record_1782_2025-02-10T00-00-00.000.json
    2026-04-15 21:06:53,836 | INFO | Skipping weekly_record_1782_2025-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,847 | INFO | Processing file: weekly_record_1783_2025-02-17T00-00-00.000.json
    2026-04-15 21:06:53,906 | INFO | Skipping weekly_record_1783_2025-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,907 | INFO | Processing file: weekly_record_1784_2025-02-24T00-00-00.000.json
    2026-04-15 21:06:53,929 | INFO | Skipping weekly_record_1784_2025-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,930 | INFO | Processing file: weekly_record_1785_2025-03-03T00-00-00.000.json
    2026-04-15 21:06:53,953 | INFO | Skipping weekly_record_1785_2025-03-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,954 | INFO | Processing file: weekly_record_1786_2025-03-10T00-00-00.000.json
    2026-04-15 21:06:53,980 | INFO | Skipping weekly_record_1786_2025-03-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:53,980 | INFO | Processing file: weekly_record_1787_2025-03-17T00-00-00.000.json
    2026-04-15 21:06:54,008 | INFO | Skipping weekly_record_1787_2025-03-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,010 | INFO | Processing file: weekly_record_1788_2025-03-24T00-00-00.000.json
    2026-04-15 21:06:54,039 | INFO | Skipping weekly_record_1788_2025-03-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,039 | INFO | Processing file: weekly_record_1789_2025-03-31T00-00-00.000.json
    2026-04-15 21:06:54,066 | INFO | Skipping weekly_record_1789_2025-03-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,068 | INFO | Processing file: weekly_record_178_1994-05-16T00-00-00.000.json
    2026-04-15 21:06:54,088 | INFO | Skipping weekly_record_178_1994-05-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,089 | INFO | Processing file: weekly_record_1790_2025-04-07T00-00-00.000.json
    2026-04-15 21:06:54,116 | INFO | Skipping weekly_record_1790_2025-04-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,117 | INFO | Processing file: weekly_record_1791_2025-04-14T00-00-00.000.json
    2026-04-15 21:06:54,137 | INFO | Skipping weekly_record_1791_2025-04-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,137 | INFO | Processing file: weekly_record_1792_2025-04-21T00-00-00.000.json
    2026-04-15 21:06:54,170 | INFO | Skipping weekly_record_1792_2025-04-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,170 | INFO | Processing file: weekly_record_1793_2025-04-28T00-00-00.000.json
    2026-04-15 21:06:54,186 | INFO | Skipping weekly_record_1793_2025-04-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,187 | INFO | Processing file: weekly_record_1794_2025-05-05T00-00-00.000.json
    2026-04-15 21:06:54,204 | INFO | Skipping weekly_record_1794_2025-05-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,205 | INFO | Processing file: weekly_record_1795_2025-05-12T00-00-00.000.json
    2026-04-15 21:06:54,231 | INFO | Skipping weekly_record_1795_2025-05-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,232 | INFO | Processing file: weekly_record_1796_2025-05-19T00-00-00.000.json
    2026-04-15 21:06:54,249 | INFO | Skipping weekly_record_1796_2025-05-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,250 | INFO | Processing file: weekly_record_1797_2025-05-26T00-00-00.000.json
    2026-04-15 21:06:54,274 | INFO | Skipping weekly_record_1797_2025-05-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,275 | INFO | Processing file: weekly_record_1798_2025-06-02T00-00-00.000.json
    2026-04-15 21:06:54,298 | INFO | Skipping weekly_record_1798_2025-06-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,298 | INFO | Processing file: weekly_record_1799_2025-06-09T00-00-00.000.json
    2026-04-15 21:06:54,321 | INFO | Skipping weekly_record_1799_2025-06-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,322 | INFO | Processing file: weekly_record_179_1994-05-23T00-00-00.000.json
    2026-04-15 21:06:54,339 | INFO | Skipping weekly_record_179_1994-05-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,340 | INFO | Processing file: weekly_record_17_1991-04-15T00-00-00.000.json
    2026-04-15 21:06:54,365 | INFO | Skipping weekly_record_17_1991-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,366 | INFO | Processing file: weekly_record_1800_2025-06-16T00-00-00.000.json
    2026-04-15 21:06:54,395 | INFO | Skipping weekly_record_1800_2025-06-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,396 | INFO | Processing file: weekly_record_1801_2025-06-23T00-00-00.000.json
    2026-04-15 21:06:54,412 | INFO | Skipping weekly_record_1801_2025-06-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,414 | INFO | Processing file: weekly_record_1802_2025-06-30T00-00-00.000.json
    2026-04-15 21:06:54,439 | INFO | Skipping weekly_record_1802_2025-06-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,440 | INFO | Processing file: weekly_record_1803_2025-07-07T00-00-00.000.json
    2026-04-15 21:06:54,456 | INFO | Skipping weekly_record_1803_2025-07-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,462 | INFO | Processing file: weekly_record_1804_2025-07-14T00-00-00.000.json
    2026-04-15 21:06:54,485 | INFO | Skipping weekly_record_1804_2025-07-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,486 | INFO | Processing file: weekly_record_1805_2025-07-21T00-00-00.000.json
    2026-04-15 21:06:54,502 | INFO | Skipping weekly_record_1805_2025-07-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,504 | INFO | Processing file: weekly_record_1806_2025-07-28T00-00-00.000.json
    2026-04-15 21:06:54,528 | INFO | Skipping weekly_record_1806_2025-07-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,529 | INFO | Processing file: weekly_record_1807_2025-08-04T00-00-00.000.json
    2026-04-15 21:06:54,546 | INFO | Skipping weekly_record_1807_2025-08-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,547 | INFO | Processing file: weekly_record_1808_2025-08-11T00-00-00.000.json
    2026-04-15 21:06:54,573 | INFO | Skipping weekly_record_1808_2025-08-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,574 | INFO | Processing file: weekly_record_1809_2025-08-18T00-00-00.000.json
    2026-04-15 21:06:54,593 | INFO | Skipping weekly_record_1809_2025-08-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,636 | INFO | Processing file: weekly_record_180_1994-05-30T00-00-00.000.json
    2026-04-15 21:06:54,655 | INFO | Skipping weekly_record_180_1994-05-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,656 | INFO | Processing file: weekly_record_1810_2025-08-25T00-00-00.000.json
    2026-04-15 21:06:54,672 | INFO | Skipping weekly_record_1810_2025-08-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,673 | INFO | Processing file: weekly_record_1811_2025-09-01T00-00-00.000.json
    2026-04-15 21:06:54,701 | INFO | Skipping weekly_record_1811_2025-09-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,702 | INFO | Processing file: weekly_record_1812_2025-09-08T00-00-00.000.json
    2026-04-15 21:06:54,716 | INFO | Skipping weekly_record_1812_2025-09-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,724 | INFO | Processing file: weekly_record_1813_2025-09-15T00-00-00.000.json
    2026-04-15 21:06:54,743 | INFO | Skipping weekly_record_1813_2025-09-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,744 | INFO | Processing file: weekly_record_1814_2025-09-22T00-00-00.000.json
    2026-04-15 21:06:54,762 | INFO | Skipping weekly_record_1814_2025-09-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,762 | INFO | Processing file: weekly_record_1815_2025-09-29T00-00-00.000.json
    2026-04-15 21:06:54,788 | INFO | Skipping weekly_record_1815_2025-09-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,788 | INFO | Processing file: weekly_record_1816_2025-10-06T00-00-00.000.json
    2026-04-15 21:06:54,807 | INFO | Skipping weekly_record_1816_2025-10-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,807 | INFO | Processing file: weekly_record_1817_2025-10-13T00-00-00.000.json
    2026-04-15 21:06:54,834 | INFO | Skipping weekly_record_1817_2025-10-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,835 | INFO | Processing file: weekly_record_1818_2025-10-20T00-00-00.000.json
    2026-04-15 21:06:54,852 | INFO | Skipping weekly_record_1818_2025-10-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,853 | INFO | Processing file: weekly_record_1819_2025-10-27T00-00-00.000.json
    2026-04-15 21:06:54,880 | INFO | Skipping weekly_record_1819_2025-10-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,881 | INFO | Processing file: weekly_record_181_1994-06-06T00-00-00.000.json
    2026-04-15 21:06:54,897 | INFO | Skipping weekly_record_181_1994-06-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,898 | INFO | Processing file: weekly_record_1820_2025-11-03T00-00-00.000.json
    2026-04-15 21:06:54,924 | INFO | Skipping weekly_record_1820_2025-11-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,924 | INFO | Processing file: weekly_record_1821_2025-11-10T00-00-00.000.json
    2026-04-15 21:06:54,948 | INFO | Skipping weekly_record_1821_2025-11-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,950 | INFO | Processing file: weekly_record_1822_2025-11-17T00-00-00.000.json
    2026-04-15 21:06:54,969 | INFO | Skipping weekly_record_1822_2025-11-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,970 | INFO | Processing file: weekly_record_1823_2025-11-24T00-00-00.000.json
    2026-04-15 21:06:54,988 | INFO | Skipping weekly_record_1823_2025-11-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:54,989 | INFO | Processing file: weekly_record_1824_2025-12-01T00-00-00.000.json
    2026-04-15 21:06:55,018 | INFO | Skipping weekly_record_1824_2025-12-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,019 | INFO | Processing file: weekly_record_1825_2025-12-08T00-00-00.000.json
    2026-04-15 21:06:55,040 | INFO | Skipping weekly_record_1825_2025-12-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,041 | INFO | Processing file: weekly_record_1826_2025-12-15T00-00-00.000.json
    2026-04-15 21:06:55,063 | INFO | Skipping weekly_record_1826_2025-12-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,064 | INFO | Processing file: weekly_record_1827_2025-12-22T00-00-00.000.json
    2026-04-15 21:06:55,081 | INFO | Skipping weekly_record_1827_2025-12-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,081 | INFO | Processing file: weekly_record_1828_2025-12-29T00-00-00.000.json
    2026-04-15 21:06:55,107 | INFO | Skipping weekly_record_1828_2025-12-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,108 | INFO | Processing file: weekly_record_1829_2026-01-05T00-00-00.000.json
    2026-04-15 21:06:55,127 | INFO | Skipping weekly_record_1829_2026-01-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,127 | INFO | Processing file: weekly_record_182_1994-06-13T00-00-00.000.json
    2026-04-15 21:06:55,152 | INFO | Skipping weekly_record_182_1994-06-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,153 | INFO | Processing file: weekly_record_1830_2026-01-12T00-00-00.000.json
    2026-04-15 21:06:55,173 | INFO | Skipping weekly_record_1830_2026-01-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,174 | INFO | Processing file: weekly_record_1831_2026-01-19T00-00-00.000.json
    2026-04-15 21:06:55,198 | INFO | Skipping weekly_record_1831_2026-01-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,199 | INFO | Processing file: weekly_record_1832_2026-01-26T00-00-00.000.json
    2026-04-15 21:06:55,217 | INFO | Skipping weekly_record_1832_2026-01-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,218 | INFO | Processing file: weekly_record_1833_2026-02-02T00-00-00.000.json
    2026-04-15 21:06:55,243 | INFO | Skipping weekly_record_1833_2026-02-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,244 | INFO | Processing file: weekly_record_1834_2026-02-09T00-00-00.000.json
    2026-04-15 21:06:55,303 | INFO | Skipping weekly_record_1834_2026-02-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,304 | INFO | Processing file: weekly_record_1835_2026-02-16T00-00-00.000.json
    2026-04-15 21:06:55,329 | INFO | Skipping weekly_record_1835_2026-02-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,330 | INFO | Processing file: weekly_record_1836_2026-02-23T00-00-00.000.json
    2026-04-15 21:06:55,347 | INFO | Skipping weekly_record_1836_2026-02-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,348 | INFO | Processing file: weekly_record_1837_2026-03-02T00-00-00.000.json
    2026-04-15 21:06:55,373 | INFO | Skipping weekly_record_1837_2026-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,374 | INFO | Processing file: weekly_record_1838_2026-03-09T00-00-00.000.json
    2026-04-15 21:06:55,391 | INFO | Skipping weekly_record_1838_2026-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,392 | INFO | Processing file: weekly_record_1839_2026-03-16T00-00-00.000.json
    2026-04-15 21:06:55,409 | INFO | Skipping weekly_record_1839_2026-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,410 | INFO | Processing file: weekly_record_183_1994-06-20T00-00-00.000.json
    2026-04-15 21:06:55,437 | INFO | Skipping weekly_record_183_1994-06-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,438 | INFO | Processing file: weekly_record_184_1994-06-27T00-00-00.000.json
    2026-04-15 21:06:55,458 | INFO | Skipping weekly_record_184_1994-06-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,459 | INFO | Processing file: weekly_record_185_1994-07-04T00-00-00.000.json
    2026-04-15 21:06:55,484 | INFO | Skipping weekly_record_185_1994-07-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,485 | INFO | Processing file: weekly_record_186_1994-07-11T00-00-00.000.json
    2026-04-15 21:06:55,798 | INFO | Skipping weekly_record_186_1994-07-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,801 | INFO | Processing file: weekly_record_187_1994-07-18T00-00-00.000.json
    2026-04-15 21:06:55,835 | INFO | Skipping weekly_record_187_1994-07-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,836 | INFO | Processing file: weekly_record_188_1994-07-25T00-00-00.000.json
    2026-04-15 21:06:55,861 | INFO | Skipping weekly_record_188_1994-07-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,862 | INFO | Processing file: weekly_record_189_1994-08-01T00-00-00.000.json
    2026-04-15 21:06:55,879 | INFO | Skipping weekly_record_189_1994-08-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,879 | INFO | Processing file: weekly_record_18_1991-04-22T00-00-00.000.json
    2026-04-15 21:06:55,907 | INFO | Skipping weekly_record_18_1991-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,908 | INFO | Processing file: weekly_record_190_1994-08-08T00-00-00.000.json
    2026-04-15 21:06:55,925 | INFO | Skipping weekly_record_190_1994-08-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,926 | INFO | Processing file: weekly_record_191_1994-08-15T00-00-00.000.json
    2026-04-15 21:06:55,951 | INFO | Skipping weekly_record_191_1994-08-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,953 | INFO | Processing file: weekly_record_192_1994-08-22T00-00-00.000.json
    2026-04-15 21:06:55,969 | INFO | Skipping weekly_record_192_1994-08-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,970 | INFO | Processing file: weekly_record_193_1994-08-29T00-00-00.000.json
    2026-04-15 21:06:55,996 | INFO | Skipping weekly_record_193_1994-08-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:55,997 | INFO | Processing file: weekly_record_194_1994-09-05T00-00-00.000.json
    2026-04-15 21:06:56,016 | INFO | Skipping weekly_record_194_1994-09-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,017 | INFO | Processing file: weekly_record_195_1994-09-12T00-00-00.000.json
    2026-04-15 21:06:56,044 | INFO | Skipping weekly_record_195_1994-09-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,045 | INFO | Processing file: weekly_record_196_1994-09-19T00-00-00.000.json
    2026-04-15 21:06:56,063 | INFO | Skipping weekly_record_196_1994-09-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,064 | INFO | Processing file: weekly_record_197_1994-09-26T00-00-00.000.json
    2026-04-15 21:06:56,089 | INFO | Skipping weekly_record_197_1994-09-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,090 | INFO | Processing file: weekly_record_198_1994-10-03T00-00-00.000.json
    2026-04-15 21:06:56,107 | INFO | Skipping weekly_record_198_1994-10-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,108 | INFO | Processing file: weekly_record_199_1994-10-10T00-00-00.000.json
    2026-04-15 21:06:56,131 | INFO | Skipping weekly_record_199_1994-10-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,132 | INFO | Processing file: weekly_record_19_1991-04-29T00-00-00.000.json
    2026-04-15 21:06:56,153 | INFO | Skipping weekly_record_19_1991-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,153 | INFO | Processing file: weekly_record_1_1990-11-12T00-00-00.000.json
    2026-04-15 21:06:56,184 | INFO | Skipping weekly_record_1_1990-11-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,185 | INFO | Processing file: weekly_record_200_1994-10-17T00-00-00.000.json
    2026-04-15 21:06:56,203 | INFO | Skipping weekly_record_200_1994-10-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,204 | INFO | Processing file: weekly_record_201_1994-10-24T00-00-00.000.json
    2026-04-15 21:06:56,273 | INFO | Skipping weekly_record_201_1994-10-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,274 | INFO | Processing file: weekly_record_202_1994-10-31T00-00-00.000.json
    2026-04-15 21:06:56,293 | INFO | Skipping weekly_record_202_1994-10-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,294 | INFO | Processing file: weekly_record_203_1994-11-07T00-00-00.000.json
    2026-04-15 21:06:56,312 | INFO | Skipping weekly_record_203_1994-11-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,313 | INFO | Processing file: weekly_record_204_1994-11-14T00-00-00.000.json
    2026-04-15 21:06:56,341 | INFO | Skipping weekly_record_204_1994-11-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,342 | INFO | Processing file: weekly_record_205_1994-11-21T00-00-00.000.json
    2026-04-15 21:06:56,358 | INFO | Skipping weekly_record_205_1994-11-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,358 | INFO | Processing file: weekly_record_206_1994-11-28T00-00-00.000.json
    2026-04-15 21:06:56,378 | INFO | Skipping weekly_record_206_1994-11-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,378 | INFO | Processing file: weekly_record_207_1994-12-05T00-00-00.000.json
    2026-04-15 21:06:56,397 | INFO | Skipping weekly_record_207_1994-12-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,398 | INFO | Processing file: weekly_record_208_1994-12-12T00-00-00.000.json
    2026-04-15 21:06:56,423 | INFO | Skipping weekly_record_208_1994-12-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,424 | INFO | Processing file: weekly_record_209_1994-12-19T00-00-00.000.json
    2026-04-15 21:06:56,446 | INFO | Skipping weekly_record_209_1994-12-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,447 | INFO | Processing file: weekly_record_20_1991-05-06T00-00-00.000.json
    2026-04-15 21:06:56,475 | INFO | Skipping weekly_record_20_1991-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,477 | INFO | Processing file: weekly_record_210_1994-12-26T00-00-00.000.json
    2026-04-15 21:06:56,505 | INFO | Skipping weekly_record_210_1994-12-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,506 | INFO | Processing file: weekly_record_211_1995-01-02T00-00-00.000.json
    2026-04-15 21:06:56,551 | INFO | Skipping weekly_record_211_1995-01-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,552 | INFO | Processing file: weekly_record_212_1995-01-09T00-00-00.000.json
    2026-04-15 21:06:56,578 | INFO | Skipping weekly_record_212_1995-01-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,579 | INFO | Processing file: weekly_record_213_1995-01-16T00-00-00.000.json
    2026-04-15 21:06:56,606 | INFO | Skipping weekly_record_213_1995-01-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,607 | INFO | Processing file: weekly_record_214_1995-01-23T00-00-00.000.json
    2026-04-15 21:06:56,625 | INFO | Skipping weekly_record_214_1995-01-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,626 | INFO | Processing file: weekly_record_215_1995-01-30T00-00-00.000.json
    2026-04-15 21:06:56,653 | INFO | Skipping weekly_record_215_1995-01-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,653 | INFO | Processing file: weekly_record_216_1995-02-06T00-00-00.000.json
    2026-04-15 21:06:56,673 | INFO | Skipping weekly_record_216_1995-02-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,674 | INFO | Processing file: weekly_record_217_1995-02-13T00-00-00.000.json
    2026-04-15 21:06:56,702 | INFO | Skipping weekly_record_217_1995-02-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,703 | INFO | Processing file: weekly_record_218_1995-02-20T00-00-00.000.json
    2026-04-15 21:06:56,719 | INFO | Skipping weekly_record_218_1995-02-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,721 | INFO | Processing file: weekly_record_219_1995-02-27T00-00-00.000.json
    2026-04-15 21:06:56,747 | INFO | Skipping weekly_record_219_1995-02-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,748 | INFO | Processing file: weekly_record_21_1991-05-13T00-00-00.000.json
    2026-04-15 21:06:56,767 | INFO | Skipping weekly_record_21_1991-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,768 | INFO | Processing file: weekly_record_220_1995-03-06T00-00-00.000.json
    2026-04-15 21:06:56,796 | INFO | Skipping weekly_record_220_1995-03-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,797 | INFO | Processing file: weekly_record_221_1995-03-13T00-00-00.000.json
    2026-04-15 21:06:56,814 | INFO | Skipping weekly_record_221_1995-03-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,815 | INFO | Processing file: weekly_record_222_1995-03-20T00-00-00.000.json
    2026-04-15 21:06:56,836 | INFO | Skipping weekly_record_222_1995-03-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,837 | INFO | Processing file: weekly_record_223_1995-03-27T00-00-00.000.json
    2026-04-15 21:06:56,854 | INFO | Skipping weekly_record_223_1995-03-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,855 | INFO | Processing file: weekly_record_224_1995-04-03T00-00-00.000.json
    2026-04-15 21:06:56,882 | INFO | Skipping weekly_record_224_1995-04-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,883 | INFO | Processing file: weekly_record_225_1995-04-10T00-00-00.000.json
    2026-04-15 21:06:56,941 | INFO | Skipping weekly_record_225_1995-04-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,942 | INFO | Processing file: weekly_record_226_1995-04-17T00-00-00.000.json
    2026-04-15 21:06:56,959 | INFO | Skipping weekly_record_226_1995-04-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,960 | INFO | Processing file: weekly_record_227_1995-04-24T00-00-00.000.json
    2026-04-15 21:06:56,976 | INFO | Skipping weekly_record_227_1995-04-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:56,977 | INFO | Processing file: weekly_record_228_1995-05-01T00-00-00.000.json
    2026-04-15 21:06:57,004 | INFO | Skipping weekly_record_228_1995-05-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,005 | INFO | Processing file: weekly_record_229_1995-05-08T00-00-00.000.json
    2026-04-15 21:06:57,023 | INFO | Skipping weekly_record_229_1995-05-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,025 | INFO | Processing file: weekly_record_22_1991-05-20T00-00-00.000.json
    2026-04-15 21:06:57,047 | INFO | Skipping weekly_record_22_1991-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,048 | INFO | Processing file: weekly_record_230_1995-05-15T00-00-00.000.json
    2026-04-15 21:06:57,064 | INFO | Skipping weekly_record_230_1995-05-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,064 | INFO | Processing file: weekly_record_231_1995-05-22T00-00-00.000.json
    2026-04-15 21:06:57,096 | INFO | Skipping weekly_record_231_1995-05-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,096 | INFO | Processing file: weekly_record_232_1995-05-29T00-00-00.000.json
    2026-04-15 21:06:57,113 | INFO | Skipping weekly_record_232_1995-05-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,114 | INFO | Processing file: weekly_record_233_1995-06-05T00-00-00.000.json
    2026-04-15 21:06:57,140 | INFO | Skipping weekly_record_233_1995-06-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,141 | INFO | Processing file: weekly_record_234_1995-06-12T00-00-00.000.json
    2026-04-15 21:06:57,160 | INFO | Skipping weekly_record_234_1995-06-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,162 | INFO | Processing file: weekly_record_235_1995-06-19T00-00-00.000.json
    2026-04-15 21:06:57,186 | INFO | Skipping weekly_record_235_1995-06-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,187 | INFO | Processing file: weekly_record_236_1995-06-26T00-00-00.000.json
    2026-04-15 21:06:57,205 | INFO | Skipping weekly_record_236_1995-06-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,205 | INFO | Processing file: weekly_record_237_1995-07-03T00-00-00.000.json
    2026-04-15 21:06:57,230 | INFO | Skipping weekly_record_237_1995-07-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,231 | INFO | Processing file: weekly_record_238_1995-07-10T00-00-00.000.json
    2026-04-15 21:06:57,249 | INFO | Skipping weekly_record_238_1995-07-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,249 | INFO | Processing file: weekly_record_239_1995-07-17T00-00-00.000.json
    2026-04-15 21:06:57,275 | INFO | Skipping weekly_record_239_1995-07-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,277 | INFO | Processing file: weekly_record_23_1991-05-27T00-00-00.000.json
    2026-04-15 21:06:57,302 | INFO | Skipping weekly_record_23_1991-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,303 | INFO | Processing file: weekly_record_240_1995-07-24T00-00-00.000.json
    2026-04-15 21:06:57,326 | INFO | Skipping weekly_record_240_1995-07-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,327 | INFO | Processing file: weekly_record_241_1995-07-31T00-00-00.000.json
    2026-04-15 21:06:57,352 | INFO | Skipping weekly_record_241_1995-07-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,352 | INFO | Processing file: weekly_record_242_1995-08-07T00-00-00.000.json
    2026-04-15 21:06:57,369 | INFO | Skipping weekly_record_242_1995-08-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,370 | INFO | Processing file: weekly_record_243_1995-08-14T00-00-00.000.json
    2026-04-15 21:06:57,397 | INFO | Skipping weekly_record_243_1995-08-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,399 | INFO | Processing file: weekly_record_244_1995-08-21T00-00-00.000.json
    2026-04-15 21:06:57,416 | INFO | Skipping weekly_record_244_1995-08-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,418 | INFO | Processing file: weekly_record_245_1995-08-28T00-00-00.000.json
    2026-04-15 21:06:57,440 | INFO | Skipping weekly_record_245_1995-08-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,442 | INFO | Processing file: weekly_record_246_1995-09-04T00-00-00.000.json
    2026-04-15 21:06:57,463 | INFO | Skipping weekly_record_246_1995-09-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,464 | INFO | Processing file: weekly_record_247_1995-09-11T00-00-00.000.json
    2026-04-15 21:06:57,491 | INFO | Skipping weekly_record_247_1995-09-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,492 | INFO | Processing file: weekly_record_248_1995-09-18T00-00-00.000.json
    2026-04-15 21:06:57,554 | INFO | Skipping weekly_record_248_1995-09-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,556 | INFO | Processing file: weekly_record_249_1995-09-25T00-00-00.000.json
    2026-04-15 21:06:57,575 | INFO | Skipping weekly_record_249_1995-09-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,577 | INFO | Processing file: weekly_record_24_1991-06-03T00-00-00.000.json
    2026-04-15 21:06:57,603 | INFO | Skipping weekly_record_24_1991-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,604 | INFO | Processing file: weekly_record_250_1995-10-02T00-00-00.000.json
    2026-04-15 21:06:57,625 | INFO | Skipping weekly_record_250_1995-10-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,626 | INFO | Processing file: weekly_record_251_1995-10-09T00-00-00.000.json
    2026-04-15 21:06:57,656 | INFO | Skipping weekly_record_251_1995-10-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,657 | INFO | Processing file: weekly_record_252_1995-10-16T00-00-00.000.json
    2026-04-15 21:06:57,675 | INFO | Skipping weekly_record_252_1995-10-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,675 | INFO | Processing file: weekly_record_253_1995-10-23T00-00-00.000.json
    2026-04-15 21:06:57,702 | INFO | Skipping weekly_record_253_1995-10-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,703 | INFO | Processing file: weekly_record_254_1995-10-30T00-00-00.000.json
    2026-04-15 21:06:57,720 | INFO | Skipping weekly_record_254_1995-10-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,721 | INFO | Processing file: weekly_record_255_1995-11-06T00-00-00.000.json
    2026-04-15 21:06:57,747 | INFO | Skipping weekly_record_255_1995-11-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,748 | INFO | Processing file: weekly_record_256_1995-11-13T00-00-00.000.json
    2026-04-15 21:06:57,764 | INFO | Skipping weekly_record_256_1995-11-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,775 | INFO | Processing file: weekly_record_257_1995-11-20T00-00-00.000.json
    2026-04-15 21:06:57,800 | INFO | Skipping weekly_record_257_1995-11-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,801 | INFO | Processing file: weekly_record_258_1995-11-27T00-00-00.000.json
    2026-04-15 21:06:57,819 | INFO | Skipping weekly_record_258_1995-11-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,820 | INFO | Processing file: weekly_record_259_1995-12-04T00-00-00.000.json
    2026-04-15 21:06:57,844 | INFO | Skipping weekly_record_259_1995-12-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,845 | INFO | Processing file: weekly_record_25_1991-06-10T00-00-00.000.json
    2026-04-15 21:06:57,864 | INFO | Skipping weekly_record_25_1991-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,865 | INFO | Processing file: weekly_record_260_1995-12-11T00-00-00.000.json
    2026-04-15 21:06:57,890 | INFO | Skipping weekly_record_260_1995-12-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,891 | INFO | Processing file: weekly_record_261_1995-12-18T00-00-00.000.json
    2026-04-15 21:06:57,913 | INFO | Skipping weekly_record_261_1995-12-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,914 | INFO | Processing file: weekly_record_262_1995-12-25T00-00-00.000.json
    2026-04-15 21:06:57,938 | INFO | Skipping weekly_record_262_1995-12-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,939 | INFO | Processing file: weekly_record_263_1996-01-01T00-00-00.000.json
    2026-04-15 21:06:57,956 | INFO | Skipping weekly_record_263_1996-01-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,957 | INFO | Processing file: weekly_record_264_1996-01-08T00-00-00.000.json
    2026-04-15 21:06:57,984 | INFO | Skipping weekly_record_264_1996-01-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:57,985 | INFO | Processing file: weekly_record_265_1996-01-15T00-00-00.000.json
    2026-04-15 21:06:58,003 | INFO | Skipping weekly_record_265_1996-01-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,003 | INFO | Processing file: weekly_record_266_1996-01-22T00-00-00.000.json
    2026-04-15 21:06:58,025 | INFO | Skipping weekly_record_266_1996-01-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,026 | INFO | Processing file: weekly_record_267_1996-01-29T00-00-00.000.json
    2026-04-15 21:06:58,043 | INFO | Skipping weekly_record_267_1996-01-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,044 | INFO | Processing file: weekly_record_268_1996-02-05T00-00-00.000.json
    2026-04-15 21:06:58,070 | INFO | Skipping weekly_record_268_1996-02-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,071 | INFO | Processing file: weekly_record_269_1996-02-12T00-00-00.000.json
    2026-04-15 21:06:58,088 | INFO | Skipping weekly_record_269_1996-02-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,089 | INFO | Processing file: weekly_record_26_1991-06-17T00-00-00.000.json
    2026-04-15 21:06:58,151 | INFO | Skipping weekly_record_26_1991-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,152 | INFO | Processing file: weekly_record_270_1996-02-19T00-00-00.000.json
    2026-04-15 21:06:58,179 | INFO | Skipping weekly_record_270_1996-02-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,182 | INFO | Processing file: weekly_record_271_1996-02-26T00-00-00.000.json
    2026-04-15 21:06:58,218 | INFO | Skipping weekly_record_271_1996-02-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,220 | INFO | Processing file: weekly_record_272_1996-03-04T00-00-00.000.json
    2026-04-15 21:06:58,269 | INFO | Skipping weekly_record_272_1996-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,271 | INFO | Processing file: weekly_record_273_1996-03-11T00-00-00.000.json
    2026-04-15 21:06:58,316 | INFO | Skipping weekly_record_273_1996-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,318 | INFO | Processing file: weekly_record_274_1996-03-18T00-00-00.000.json
    2026-04-15 21:06:58,341 | INFO | Skipping weekly_record_274_1996-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,342 | INFO | Processing file: weekly_record_275_1996-03-25T00-00-00.000.json
    2026-04-15 21:06:58,376 | INFO | Skipping weekly_record_275_1996-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,381 | INFO | Processing file: weekly_record_276_1996-04-01T00-00-00.000.json
    2026-04-15 21:06:58,413 | INFO | Skipping weekly_record_276_1996-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,414 | INFO | Processing file: weekly_record_277_1996-04-08T00-00-00.000.json
    2026-04-15 21:06:58,438 | INFO | Skipping weekly_record_277_1996-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,440 | INFO | Processing file: weekly_record_278_1996-04-15T00-00-00.000.json
    2026-04-15 21:06:58,461 | INFO | Skipping weekly_record_278_1996-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,463 | INFO | Processing file: weekly_record_279_1996-04-22T00-00-00.000.json
    2026-04-15 21:06:58,490 | INFO | Skipping weekly_record_279_1996-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,491 | INFO | Processing file: weekly_record_27_1991-06-24T00-00-00.000.json
    2026-04-15 21:06:58,521 | INFO | Skipping weekly_record_27_1991-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,523 | INFO | Processing file: weekly_record_280_1996-04-29T00-00-00.000.json
    2026-04-15 21:06:58,551 | INFO | Skipping weekly_record_280_1996-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,553 | INFO | Processing file: weekly_record_281_1996-05-06T00-00-00.000.json
    2026-04-15 21:06:58,580 | INFO | Skipping weekly_record_281_1996-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,581 | INFO | Processing file: weekly_record_282_1996-05-13T00-00-00.000.json
    2026-04-15 21:06:58,606 | INFO | Skipping weekly_record_282_1996-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,606 | INFO | Processing file: weekly_record_283_1996-05-20T00-00-00.000.json
    2026-04-15 21:06:58,629 | INFO | Skipping weekly_record_283_1996-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,630 | INFO | Processing file: weekly_record_284_1996-05-27T00-00-00.000.json
    2026-04-15 21:06:58,654 | INFO | Skipping weekly_record_284_1996-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,656 | INFO | Processing file: weekly_record_285_1996-06-03T00-00-00.000.json
    2026-04-15 21:06:58,682 | INFO | Skipping weekly_record_285_1996-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,683 | INFO | Processing file: weekly_record_286_1996-06-10T00-00-00.000.json
    2026-04-15 21:06:58,700 | INFO | Skipping weekly_record_286_1996-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,702 | INFO | Processing file: weekly_record_287_1996-06-17T00-00-00.000.json
    2026-04-15 21:06:58,726 | INFO | Skipping weekly_record_287_1996-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,727 | INFO | Processing file: weekly_record_288_1996-06-24T00-00-00.000.json
    2026-04-15 21:06:58,754 | INFO | Skipping weekly_record_288_1996-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,756 | INFO | Processing file: weekly_record_289_1996-07-01T00-00-00.000.json
    2026-04-15 21:06:58,782 | INFO | Skipping weekly_record_289_1996-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,783 | INFO | Processing file: weekly_record_28_1991-07-01T00-00-00.000.json
    2026-04-15 21:06:58,819 | INFO | Skipping weekly_record_28_1991-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,820 | INFO | Processing file: weekly_record_290_1996-07-08T00-00-00.000.json
    2026-04-15 21:06:58,863 | INFO | Skipping weekly_record_290_1996-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,864 | INFO | Processing file: weekly_record_291_1996-07-15T00-00-00.000.json
    2026-04-15 21:06:58,902 | INFO | Skipping weekly_record_291_1996-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,927 | INFO | Processing file: weekly_record_292_1996-07-22T00-00-00.000.json
    2026-04-15 21:06:58,963 | INFO | Skipping weekly_record_292_1996-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:58,964 | INFO | Processing file: weekly_record_293_1996-07-29T00-00-00.000.json
    2026-04-15 21:06:59,011 | INFO | Skipping weekly_record_293_1996-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,012 | INFO | Processing file: weekly_record_294_1996-08-05T00-00-00.000.json
    2026-04-15 21:06:59,051 | INFO | Skipping weekly_record_294_1996-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,052 | INFO | Processing file: weekly_record_295_1996-08-12T00-00-00.000.json
    2026-04-15 21:06:59,085 | INFO | Skipping weekly_record_295_1996-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,087 | INFO | Processing file: weekly_record_296_1996-08-19T00-00-00.000.json
    2026-04-15 21:06:59,117 | INFO | Skipping weekly_record_296_1996-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,119 | INFO | Processing file: weekly_record_297_1996-08-26T00-00-00.000.json
    2026-04-15 21:06:59,154 | INFO | Skipping weekly_record_297_1996-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,156 | INFO | Processing file: weekly_record_298_1996-09-02T00-00-00.000.json
    2026-04-15 21:06:59,224 | INFO | Skipping weekly_record_298_1996-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,225 | INFO | Processing file: weekly_record_299_1996-09-09T00-00-00.000.json
    2026-04-15 21:06:59,275 | INFO | Skipping weekly_record_299_1996-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,276 | INFO | Processing file: weekly_record_29_1991-07-08T00-00-00.000.json
    2026-04-15 21:06:59,301 | INFO | Skipping weekly_record_29_1991-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,303 | INFO | Processing file: weekly_record_2_1990-11-19T00-00-00.000.json
    2026-04-15 21:06:59,328 | INFO | Skipping weekly_record_2_1990-11-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,331 | INFO | Processing file: weekly_record_300_1996-09-16T00-00-00.000.json
    2026-04-15 21:06:59,373 | INFO | Skipping weekly_record_300_1996-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,378 | INFO | Processing file: weekly_record_301_1996-09-23T00-00-00.000.json
    2026-04-15 21:06:59,421 | INFO | Skipping weekly_record_301_1996-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,423 | INFO | Processing file: weekly_record_302_1996-09-30T00-00-00.000.json
    2026-04-15 21:06:59,451 | INFO | Skipping weekly_record_302_1996-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,452 | INFO | Processing file: weekly_record_303_1996-10-07T00-00-00.000.json
    2026-04-15 21:06:59,476 | INFO | Skipping weekly_record_303_1996-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,478 | INFO | Processing file: weekly_record_304_1996-10-14T00-00-00.000.json
    2026-04-15 21:06:59,511 | INFO | Skipping weekly_record_304_1996-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,512 | INFO | Processing file: weekly_record_305_1996-10-21T00-00-00.000.json
    2026-04-15 21:06:59,549 | INFO | Skipping weekly_record_305_1996-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,550 | INFO | Processing file: weekly_record_306_1996-10-28T00-00-00.000.json
    2026-04-15 21:06:59,581 | INFO | Skipping weekly_record_306_1996-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,582 | INFO | Processing file: weekly_record_307_1996-11-04T00-00-00.000.json
    2026-04-15 21:06:59,615 | INFO | Skipping weekly_record_307_1996-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,616 | INFO | Processing file: weekly_record_308_1996-11-11T00-00-00.000.json
    2026-04-15 21:06:59,704 | INFO | Skipping weekly_record_308_1996-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,705 | INFO | Processing file: weekly_record_309_1996-11-18T00-00-00.000.json
    2026-04-15 21:06:59,756 | INFO | Skipping weekly_record_309_1996-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,757 | INFO | Processing file: weekly_record_30_1991-07-15T00-00-00.000.json
    2026-04-15 21:06:59,780 | INFO | Skipping weekly_record_30_1991-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,780 | INFO | Processing file: weekly_record_310_1996-11-25T00-00-00.000.json
    2026-04-15 21:06:59,803 | INFO | Skipping weekly_record_310_1996-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,805 | INFO | Processing file: weekly_record_311_1996-12-02T00-00-00.000.json
    2026-04-15 21:06:59,871 | INFO | Skipping weekly_record_311_1996-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,873 | INFO | Processing file: weekly_record_312_1996-12-09T00-00-00.000.json
    2026-04-15 21:06:59,897 | INFO | Skipping weekly_record_312_1996-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,898 | INFO | Processing file: weekly_record_313_1996-12-16T00-00-00.000.json
    2026-04-15 21:06:59,925 | INFO | Skipping weekly_record_313_1996-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,926 | INFO | Processing file: weekly_record_314_1996-12-23T00-00-00.000.json
    2026-04-15 21:06:59,953 | INFO | Skipping weekly_record_314_1996-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,954 | INFO | Processing file: weekly_record_315_1996-12-30T00-00-00.000.json
    2026-04-15 21:06:59,978 | INFO | Skipping weekly_record_315_1996-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:06:59,979 | INFO | Processing file: weekly_record_316_1997-01-06T00-00-00.000.json
    2026-04-15 21:07:00,005 | INFO | Skipping weekly_record_316_1997-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,006 | INFO | Processing file: weekly_record_317_1997-01-13T00-00-00.000.json
    2026-04-15 21:07:00,031 | INFO | Skipping weekly_record_317_1997-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,032 | INFO | Processing file: weekly_record_318_1997-01-20T00-00-00.000.json
    2026-04-15 21:07:00,067 | INFO | Skipping weekly_record_318_1997-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,068 | INFO | Processing file: weekly_record_319_1997-01-27T00-00-00.000.json
    2026-04-15 21:07:00,102 | INFO | Skipping weekly_record_319_1997-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,103 | INFO | Processing file: weekly_record_31_1991-07-22T00-00-00.000.json
    2026-04-15 21:07:00,130 | INFO | Skipping weekly_record_31_1991-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,131 | INFO | Processing file: weekly_record_320_1997-02-03T00-00-00.000.json
    2026-04-15 21:07:00,154 | INFO | Skipping weekly_record_320_1997-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,155 | INFO | Processing file: weekly_record_321_1997-02-10T00-00-00.000.json
    2026-04-15 21:07:00,186 | INFO | Skipping weekly_record_321_1997-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,187 | INFO | Processing file: weekly_record_322_1997-02-17T00-00-00.000.json
    2026-04-15 21:07:00,218 | INFO | Skipping weekly_record_322_1997-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,219 | INFO | Processing file: weekly_record_323_1997-02-24T00-00-00.000.json
    2026-04-15 21:07:00,245 | INFO | Skipping weekly_record_323_1997-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,246 | INFO | Processing file: weekly_record_324_1997-03-03T00-00-00.000.json
    2026-04-15 21:07:00,271 | INFO | Skipping weekly_record_324_1997-03-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,272 | INFO | Processing file: weekly_record_325_1997-03-10T00-00-00.000.json
    2026-04-15 21:07:00,297 | INFO | Skipping weekly_record_325_1997-03-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,298 | INFO | Processing file: weekly_record_326_1997-03-17T00-00-00.000.json
    2026-04-15 21:07:00,323 | INFO | Skipping weekly_record_326_1997-03-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,324 | INFO | Processing file: weekly_record_327_1997-03-24T00-00-00.000.json
    2026-04-15 21:07:00,351 | INFO | Skipping weekly_record_327_1997-03-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,363 | INFO | Processing file: weekly_record_328_1997-03-31T00-00-00.000.json
    2026-04-15 21:07:00,383 | INFO | Skipping weekly_record_328_1997-03-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,384 | INFO | Processing file: weekly_record_329_1997-04-07T00-00-00.000.json
    2026-04-15 21:07:00,409 | INFO | Skipping weekly_record_329_1997-04-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,410 | INFO | Processing file: weekly_record_32_1991-07-29T00-00-00.000.json
    2026-04-15 21:07:00,436 | INFO | Skipping weekly_record_32_1991-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,436 | INFO | Processing file: weekly_record_330_1997-04-14T00-00-00.000.json
    2026-04-15 21:07:00,451 | INFO | Skipping weekly_record_330_1997-04-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,452 | INFO | Processing file: weekly_record_331_1997-04-21T00-00-00.000.json
    2026-04-15 21:07:00,476 | INFO | Skipping weekly_record_331_1997-04-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,477 | INFO | Processing file: weekly_record_332_1997-04-28T00-00-00.000.json
    2026-04-15 21:07:00,517 | INFO | Skipping weekly_record_332_1997-04-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,520 | INFO | Processing file: weekly_record_333_1997-05-05T00-00-00.000.json
    2026-04-15 21:07:00,545 | INFO | Skipping weekly_record_333_1997-05-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,548 | INFO | Processing file: weekly_record_334_1997-05-12T00-00-00.000.json
    2026-04-15 21:07:00,570 | INFO | Skipping weekly_record_334_1997-05-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,573 | INFO | Processing file: weekly_record_335_1997-05-19T00-00-00.000.json
    2026-04-15 21:07:00,619 | INFO | Skipping weekly_record_335_1997-05-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,621 | INFO | Processing file: weekly_record_336_1997-05-26T00-00-00.000.json
    2026-04-15 21:07:00,646 | INFO | Skipping weekly_record_336_1997-05-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,654 | INFO | Processing file: weekly_record_337_1997-06-02T00-00-00.000.json
    2026-04-15 21:07:00,679 | INFO | Skipping weekly_record_337_1997-06-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,680 | INFO | Processing file: weekly_record_338_1997-06-09T00-00-00.000.json
    2026-04-15 21:07:00,697 | INFO | Skipping weekly_record_338_1997-06-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,732 | INFO | Processing file: weekly_record_339_1997-06-16T00-00-00.000.json
    2026-04-15 21:07:00,774 | INFO | Skipping weekly_record_339_1997-06-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,775 | INFO | Processing file: weekly_record_33_1991-08-05T00-00-00.000.json
    2026-04-15 21:07:00,799 | INFO | Skipping weekly_record_33_1991-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,799 | INFO | Processing file: weekly_record_340_1997-06-23T00-00-00.000.json
    2026-04-15 21:07:00,822 | INFO | Skipping weekly_record_340_1997-06-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,822 | INFO | Processing file: weekly_record_341_1997-06-30T00-00-00.000.json
    2026-04-15 21:07:00,849 | INFO | Skipping weekly_record_341_1997-06-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,853 | INFO | Processing file: weekly_record_342_1997-07-07T00-00-00.000.json
    2026-04-15 21:07:00,884 | INFO | Skipping weekly_record_342_1997-07-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,884 | INFO | Processing file: weekly_record_343_1997-07-14T00-00-00.000.json
    2026-04-15 21:07:00,908 | INFO | Skipping weekly_record_343_1997-07-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,909 | INFO | Processing file: weekly_record_344_1997-07-21T00-00-00.000.json
    2026-04-15 21:07:00,935 | INFO | Skipping weekly_record_344_1997-07-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,936 | INFO | Processing file: weekly_record_345_1997-07-28T00-00-00.000.json
    2026-04-15 21:07:00,953 | INFO | Skipping weekly_record_345_1997-07-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,954 | INFO | Processing file: weekly_record_346_1997-08-04T00-00-00.000.json
    2026-04-15 21:07:00,978 | INFO | Skipping weekly_record_346_1997-08-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:00,979 | INFO | Processing file: weekly_record_347_1997-08-11T00-00-00.000.json
    2026-04-15 21:07:01,004 | INFO | Skipping weekly_record_347_1997-08-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,004 | INFO | Processing file: weekly_record_348_1997-08-18T00-00-00.000.json
    2026-04-15 21:07:01,030 | INFO | Skipping weekly_record_348_1997-08-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,031 | INFO | Processing file: weekly_record_349_1997-08-25T00-00-00.000.json
    2026-04-15 21:07:01,066 | INFO | Skipping weekly_record_349_1997-08-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,067 | INFO | Processing file: weekly_record_34_1991-08-12T00-00-00.000.json
    2026-04-15 21:07:01,098 | INFO | Skipping weekly_record_34_1991-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,099 | INFO | Processing file: weekly_record_350_1997-09-01T00-00-00.000.json
    2026-04-15 21:07:01,128 | INFO | Skipping weekly_record_350_1997-09-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,129 | INFO | Processing file: weekly_record_351_1997-09-08T00-00-00.000.json
    2026-04-15 21:07:01,156 | INFO | Skipping weekly_record_351_1997-09-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,156 | INFO | Processing file: weekly_record_352_1997-09-15T00-00-00.000.json
    2026-04-15 21:07:01,180 | INFO | Skipping weekly_record_352_1997-09-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,181 | INFO | Processing file: weekly_record_353_1997-09-22T00-00-00.000.json
    2026-04-15 21:07:01,211 | INFO | Skipping weekly_record_353_1997-09-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,211 | INFO | Processing file: weekly_record_354_1997-09-29T00-00-00.000.json
    2026-04-15 21:07:01,242 | INFO | Skipping weekly_record_354_1997-09-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,243 | INFO | Processing file: weekly_record_355_1997-10-06T00-00-00.000.json
    2026-04-15 21:07:01,263 | INFO | Skipping weekly_record_355_1997-10-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,264 | INFO | Processing file: weekly_record_356_1997-10-13T00-00-00.000.json
    2026-04-15 21:07:01,331 | INFO | Skipping weekly_record_356_1997-10-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,333 | INFO | Processing file: weekly_record_357_1997-10-20T00-00-00.000.json
    2026-04-15 21:07:01,359 | INFO | Skipping weekly_record_357_1997-10-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,360 | INFO | Processing file: weekly_record_358_1997-10-27T00-00-00.000.json
    2026-04-15 21:07:01,390 | INFO | Skipping weekly_record_358_1997-10-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,391 | INFO | Processing file: weekly_record_359_1997-11-03T00-00-00.000.json
    2026-04-15 21:07:01,409 | INFO | Skipping weekly_record_359_1997-11-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,411 | INFO | Processing file: weekly_record_35_1991-08-19T00-00-00.000.json
    2026-04-15 21:07:01,445 | INFO | Skipping weekly_record_35_1991-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,447 | INFO | Processing file: weekly_record_360_1997-11-10T00-00-00.000.json
    2026-04-15 21:07:01,470 | INFO | Skipping weekly_record_360_1997-11-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,473 | INFO | Processing file: weekly_record_361_1997-11-17T00-00-00.000.json
    2026-04-15 21:07:01,492 | INFO | Skipping weekly_record_361_1997-11-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,495 | INFO | Processing file: weekly_record_362_1997-11-24T00-00-00.000.json
    2026-04-15 21:07:01,517 | INFO | Skipping weekly_record_362_1997-11-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,522 | INFO | Processing file: weekly_record_363_1997-12-01T00-00-00.000.json
    2026-04-15 21:07:01,556 | INFO | Skipping weekly_record_363_1997-12-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,557 | INFO | Processing file: weekly_record_364_1997-12-08T00-00-00.000.json
    2026-04-15 21:07:01,588 | INFO | Skipping weekly_record_364_1997-12-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,589 | INFO | Processing file: weekly_record_365_1997-12-15T00-00-00.000.json
    2026-04-15 21:07:01,609 | INFO | Skipping weekly_record_365_1997-12-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,611 | INFO | Processing file: weekly_record_366_1997-12-22T00-00-00.000.json
    2026-04-15 21:07:01,629 | INFO | Skipping weekly_record_366_1997-12-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,630 | INFO | Processing file: weekly_record_367_1997-12-29T00-00-00.000.json
    2026-04-15 21:07:01,655 | INFO | Skipping weekly_record_367_1997-12-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,656 | INFO | Processing file: weekly_record_368_1998-01-05T00-00-00.000.json
    2026-04-15 21:07:01,681 | INFO | Skipping weekly_record_368_1998-01-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,682 | INFO | Processing file: weekly_record_369_1998-01-12T00-00-00.000.json
    2026-04-15 21:07:01,710 | INFO | Skipping weekly_record_369_1998-01-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,711 | INFO | Processing file: weekly_record_36_1991-08-26T00-00-00.000.json
    2026-04-15 21:07:01,737 | INFO | Skipping weekly_record_36_1991-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,738 | INFO | Processing file: weekly_record_370_1998-01-19T00-00-00.000.json
    2026-04-15 21:07:01,755 | INFO | Skipping weekly_record_370_1998-01-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,757 | INFO | Processing file: weekly_record_371_1998-01-26T00-00-00.000.json
    2026-04-15 21:07:01,781 | INFO | Skipping weekly_record_371_1998-01-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,782 | INFO | Processing file: weekly_record_372_1998-02-02T00-00-00.000.json
    2026-04-15 21:07:01,798 | INFO | Skipping weekly_record_372_1998-02-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,799 | INFO | Processing file: weekly_record_373_1998-02-09T00-00-00.000.json
    2026-04-15 21:07:01,822 | INFO | Skipping weekly_record_373_1998-02-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,824 | INFO | Processing file: weekly_record_374_1998-02-16T00-00-00.000.json
    2026-04-15 21:07:01,848 | INFO | Skipping weekly_record_374_1998-02-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,849 | INFO | Processing file: weekly_record_375_1998-02-23T00-00-00.000.json
    2026-04-15 21:07:01,875 | INFO | Skipping weekly_record_375_1998-02-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,876 | INFO | Processing file: weekly_record_376_1998-03-02T00-00-00.000.json
    2026-04-15 21:07:01,902 | INFO | Skipping weekly_record_376_1998-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,903 | INFO | Processing file: weekly_record_377_1998-03-09T00-00-00.000.json
    2026-04-15 21:07:01,929 | INFO | Skipping weekly_record_377_1998-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,931 | INFO | Processing file: weekly_record_378_1998-03-16T00-00-00.000.json
    2026-04-15 21:07:01,997 | INFO | Skipping weekly_record_378_1998-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:01,999 | INFO | Processing file: weekly_record_379_1998-03-23T00-00-00.000.json
    2026-04-15 21:07:02,032 | INFO | Skipping weekly_record_379_1998-03-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,034 | INFO | Processing file: weekly_record_37_1991-09-02T00-00-00.000.json
    2026-04-15 21:07:02,058 | INFO | Skipping weekly_record_37_1991-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,060 | INFO | Processing file: weekly_record_380_1998-03-30T00-00-00.000.json
    2026-04-15 21:07:02,083 | INFO | Skipping weekly_record_380_1998-03-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,084 | INFO | Processing file: weekly_record_381_1998-04-06T00-00-00.000.json
    2026-04-15 21:07:02,130 | INFO | Skipping weekly_record_381_1998-04-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,131 | INFO | Processing file: weekly_record_382_1998-04-13T00-00-00.000.json
    2026-04-15 21:07:02,152 | INFO | Skipping weekly_record_382_1998-04-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,153 | INFO | Processing file: weekly_record_383_1998-04-20T00-00-00.000.json
    2026-04-15 21:07:02,179 | INFO | Skipping weekly_record_383_1998-04-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,180 | INFO | Processing file: weekly_record_384_1998-04-27T00-00-00.000.json
    2026-04-15 21:07:02,206 | INFO | Skipping weekly_record_384_1998-04-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,207 | INFO | Processing file: weekly_record_385_1998-05-04T00-00-00.000.json
    2026-04-15 21:07:02,229 | INFO | Skipping weekly_record_385_1998-05-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,230 | INFO | Processing file: weekly_record_386_1998-05-11T00-00-00.000.json
    2026-04-15 21:07:02,268 | INFO | Skipping weekly_record_386_1998-05-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,268 | INFO | Processing file: weekly_record_387_1998-05-18T00-00-00.000.json
    2026-04-15 21:07:02,294 | INFO | Skipping weekly_record_387_1998-05-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,295 | INFO | Processing file: weekly_record_388_1998-05-25T00-00-00.000.json
    2026-04-15 21:07:02,320 | INFO | Skipping weekly_record_388_1998-05-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,321 | INFO | Processing file: weekly_record_389_1998-06-01T00-00-00.000.json
    2026-04-15 21:07:02,342 | INFO | Skipping weekly_record_389_1998-06-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,343 | INFO | Processing file: weekly_record_38_1991-09-09T00-00-00.000.json
    2026-04-15 21:07:02,382 | INFO | Skipping weekly_record_38_1991-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,384 | INFO | Processing file: weekly_record_390_1998-06-08T00-00-00.000.json
    2026-04-15 21:07:02,411 | INFO | Skipping weekly_record_390_1998-06-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,412 | INFO | Processing file: weekly_record_391_1998-06-15T00-00-00.000.json
    2026-04-15 21:07:02,427 | INFO | Skipping weekly_record_391_1998-06-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,428 | INFO | Processing file: weekly_record_392_1998-06-22T00-00-00.000.json
    2026-04-15 21:07:02,454 | INFO | Skipping weekly_record_392_1998-06-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,455 | INFO | Processing file: weekly_record_393_1998-06-29T00-00-00.000.json
    2026-04-15 21:07:02,485 | INFO | Skipping weekly_record_393_1998-06-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,485 | INFO | Processing file: weekly_record_394_1998-07-06T00-00-00.000.json
    2026-04-15 21:07:02,508 | INFO | Skipping weekly_record_394_1998-07-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,509 | INFO | Processing file: weekly_record_395_1998-07-13T00-00-00.000.json
    2026-04-15 21:07:02,537 | INFO | Skipping weekly_record_395_1998-07-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,538 | INFO | Processing file: weekly_record_396_1998-07-20T00-00-00.000.json
    2026-04-15 21:07:02,578 | INFO | Skipping weekly_record_396_1998-07-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,580 | INFO | Processing file: weekly_record_397_1998-07-27T00-00-00.000.json
    2026-04-15 21:07:02,613 | INFO | Skipping weekly_record_397_1998-07-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,614 | INFO | Processing file: weekly_record_398_1998-08-03T00-00-00.000.json
    2026-04-15 21:07:02,657 | INFO | Skipping weekly_record_398_1998-08-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,659 | INFO | Processing file: weekly_record_399_1998-08-10T00-00-00.000.json
    2026-04-15 21:07:02,685 | INFO | Skipping weekly_record_399_1998-08-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,689 | INFO | Processing file: weekly_record_39_1991-09-16T00-00-00.000.json
    2026-04-15 21:07:02,704 | INFO | Skipping weekly_record_39_1991-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,705 | INFO | Processing file: weekly_record_3_1990-11-26T00-00-00.000.json
    2026-04-15 21:07:02,729 | INFO | Skipping weekly_record_3_1990-11-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,730 | INFO | Processing file: weekly_record_400_1998-08-17T00-00-00.000.json
    2026-04-15 21:07:02,748 | INFO | Skipping weekly_record_400_1998-08-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,750 | INFO | Processing file: weekly_record_401_1998-08-24T00-00-00.000.json
    2026-04-15 21:07:02,774 | INFO | Skipping weekly_record_401_1998-08-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,775 | INFO | Processing file: weekly_record_402_1998-08-31T00-00-00.000.json
    2026-04-15 21:07:02,793 | INFO | Skipping weekly_record_402_1998-08-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,794 | INFO | Processing file: weekly_record_403_1998-09-07T00-00-00.000.json
    2026-04-15 21:07:02,824 | INFO | Skipping weekly_record_403_1998-09-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,826 | INFO | Processing file: weekly_record_404_1998-09-14T00-00-00.000.json
    2026-04-15 21:07:02,844 | INFO | Skipping weekly_record_404_1998-09-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,845 | INFO | Processing file: weekly_record_405_1998-09-21T00-00-00.000.json
    2026-04-15 21:07:02,861 | INFO | Skipping weekly_record_405_1998-09-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,862 | INFO | Processing file: weekly_record_406_1998-09-28T00-00-00.000.json
    2026-04-15 21:07:02,888 | INFO | Skipping weekly_record_406_1998-09-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,888 | INFO | Processing file: weekly_record_407_1998-10-05T00-00-00.000.json
    2026-04-15 21:07:02,907 | INFO | Skipping weekly_record_407_1998-10-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,909 | INFO | Processing file: weekly_record_408_1998-10-12T00-00-00.000.json
    2026-04-15 21:07:02,935 | INFO | Skipping weekly_record_408_1998-10-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,935 | INFO | Processing file: weekly_record_409_1998-10-19T00-00-00.000.json
    2026-04-15 21:07:02,960 | INFO | Skipping weekly_record_409_1998-10-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,961 | INFO | Processing file: weekly_record_40_1991-09-23T00-00-00.000.json
    2026-04-15 21:07:02,980 | INFO | Skipping weekly_record_40_1991-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:02,980 | INFO | Processing file: weekly_record_410_1998-10-26T00-00-00.000.json
    2026-04-15 21:07:03,003 | INFO | Skipping weekly_record_410_1998-10-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,004 | INFO | Processing file: weekly_record_411_1998-11-02T00-00-00.000.json
    2026-04-15 21:07:03,021 | INFO | Skipping weekly_record_411_1998-11-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,022 | INFO | Processing file: weekly_record_412_1998-11-09T00-00-00.000.json
    2026-04-15 21:07:03,042 | INFO | Skipping weekly_record_412_1998-11-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,043 | INFO | Processing file: weekly_record_413_1998-11-16T00-00-00.000.json
    2026-04-15 21:07:03,066 | INFO | Skipping weekly_record_413_1998-11-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,066 | INFO | Processing file: weekly_record_414_1998-11-23T00-00-00.000.json
    2026-04-15 21:07:03,092 | INFO | Skipping weekly_record_414_1998-11-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,093 | INFO | Processing file: weekly_record_415_1998-11-30T00-00-00.000.json
    2026-04-15 21:07:03,110 | INFO | Skipping weekly_record_415_1998-11-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,111 | INFO | Processing file: weekly_record_416_1998-12-07T00-00-00.000.json
    2026-04-15 21:07:03,137 | INFO | Skipping weekly_record_416_1998-12-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,138 | INFO | Processing file: weekly_record_417_1998-12-14T00-00-00.000.json
    2026-04-15 21:07:03,165 | INFO | Skipping weekly_record_417_1998-12-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,165 | INFO | Processing file: weekly_record_418_1998-12-21T00-00-00.000.json
    2026-04-15 21:07:03,189 | INFO | Skipping weekly_record_418_1998-12-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,189 | INFO | Processing file: weekly_record_419_1998-12-28T00-00-00.000.json
    2026-04-15 21:07:03,215 | INFO | Skipping weekly_record_419_1998-12-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,216 | INFO | Processing file: weekly_record_41_1991-09-30T00-00-00.000.json
    2026-04-15 21:07:03,235 | INFO | Skipping weekly_record_41_1991-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,236 | INFO | Processing file: weekly_record_420_1999-01-04T00-00-00.000.json
    2026-04-15 21:07:03,296 | INFO | Skipping weekly_record_420_1999-01-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,297 | INFO | Processing file: weekly_record_421_1999-01-11T00-00-00.000.json
    2026-04-15 21:07:03,311 | INFO | Skipping weekly_record_421_1999-01-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,312 | INFO | Processing file: weekly_record_422_1999-01-18T00-00-00.000.json
    2026-04-15 21:07:03,334 | INFO | Skipping weekly_record_422_1999-01-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,335 | INFO | Processing file: weekly_record_423_1999-01-25T00-00-00.000.json
    2026-04-15 21:07:03,364 | INFO | Skipping weekly_record_423_1999-01-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,364 | INFO | Processing file: weekly_record_424_1999-02-01T00-00-00.000.json
    2026-04-15 21:07:03,389 | INFO | Skipping weekly_record_424_1999-02-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,390 | INFO | Processing file: weekly_record_425_1999-02-08T00-00-00.000.json
    2026-04-15 21:07:03,414 | INFO | Skipping weekly_record_425_1999-02-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,414 | INFO | Processing file: weekly_record_426_1999-02-15T00-00-00.000.json
    2026-04-15 21:07:03,453 | INFO | Skipping weekly_record_426_1999-02-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,454 | INFO | Processing file: weekly_record_427_1999-02-22T00-00-00.000.json
    2026-04-15 21:07:03,477 | INFO | Skipping weekly_record_427_1999-02-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,478 | INFO | Processing file: weekly_record_428_1999-03-01T00-00-00.000.json
    2026-04-15 21:07:03,502 | INFO | Skipping weekly_record_428_1999-03-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,503 | INFO | Processing file: weekly_record_429_1999-03-08T00-00-00.000.json
    2026-04-15 21:07:03,521 | INFO | Skipping weekly_record_429_1999-03-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,522 | INFO | Processing file: weekly_record_42_1991-10-07T00-00-00.000.json
    2026-04-15 21:07:03,548 | INFO | Skipping weekly_record_42_1991-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,548 | INFO | Processing file: weekly_record_430_1999-03-15T00-00-00.000.json
    2026-04-15 21:07:03,569 | INFO | Skipping weekly_record_430_1999-03-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,570 | INFO | Processing file: weekly_record_431_1999-03-22T00-00-00.000.json
    2026-04-15 21:07:03,590 | INFO | Skipping weekly_record_431_1999-03-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,591 | INFO | Processing file: weekly_record_432_1999-03-29T00-00-00.000.json
    2026-04-15 21:07:03,607 | INFO | Skipping weekly_record_432_1999-03-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,608 | INFO | Processing file: weekly_record_433_1999-04-05T00-00-00.000.json
    2026-04-15 21:07:03,627 | INFO | Skipping weekly_record_433_1999-04-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,628 | INFO | Processing file: weekly_record_434_1999-04-12T00-00-00.000.json
    2026-04-15 21:07:03,652 | INFO | Skipping weekly_record_434_1999-04-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,652 | INFO | Processing file: weekly_record_435_1999-04-19T00-00-00.000.json
    2026-04-15 21:07:03,670 | INFO | Skipping weekly_record_435_1999-04-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,671 | INFO | Processing file: weekly_record_436_1999-04-26T00-00-00.000.json
    2026-04-15 21:07:03,692 | INFO | Skipping weekly_record_436_1999-04-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,693 | INFO | Processing file: weekly_record_437_1999-05-03T00-00-00.000.json
    2026-04-15 21:07:03,719 | INFO | Skipping weekly_record_437_1999-05-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,719 | INFO | Processing file: weekly_record_438_1999-05-10T00-00-00.000.json
    2026-04-15 21:07:03,759 | INFO | Skipping weekly_record_438_1999-05-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,760 | INFO | Processing file: weekly_record_439_1999-05-17T00-00-00.000.json
    2026-04-15 21:07:03,778 | INFO | Skipping weekly_record_439_1999-05-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,778 | INFO | Processing file: weekly_record_43_1991-10-14T00-00-00.000.json
    2026-04-15 21:07:03,804 | INFO | Skipping weekly_record_43_1991-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,829 | INFO | Processing file: weekly_record_440_1999-05-24T00-00-00.000.json
    2026-04-15 21:07:03,852 | INFO | Skipping weekly_record_440_1999-05-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,852 | INFO | Processing file: weekly_record_441_1999-05-31T00-00-00.000.json
    2026-04-15 21:07:03,877 | INFO | Skipping weekly_record_441_1999-05-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,877 | INFO | Processing file: weekly_record_442_1999-06-07T00-00-00.000.json
    2026-04-15 21:07:03,918 | INFO | Skipping weekly_record_442_1999-06-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,919 | INFO | Processing file: weekly_record_443_1999-06-14T00-00-00.000.json
    2026-04-15 21:07:03,948 | INFO | Skipping weekly_record_443_1999-06-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,949 | INFO | Processing file: weekly_record_444_1999-06-21T00-00-00.000.json
    2026-04-15 21:07:03,979 | INFO | Skipping weekly_record_444_1999-06-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:03,979 | INFO | Processing file: weekly_record_445_1999-06-28T00-00-00.000.json
    2026-04-15 21:07:04,007 | INFO | Skipping weekly_record_445_1999-06-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,008 | INFO | Processing file: weekly_record_446_1999-07-05T00-00-00.000.json
    2026-04-15 21:07:04,023 | INFO | Skipping weekly_record_446_1999-07-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,024 | INFO | Processing file: weekly_record_447_1999-07-12T00-00-00.000.json
    2026-04-15 21:07:04,049 | INFO | Skipping weekly_record_447_1999-07-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,050 | INFO | Processing file: weekly_record_448_1999-07-19T00-00-00.000.json
    2026-04-15 21:07:04,067 | INFO | Skipping weekly_record_448_1999-07-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,068 | INFO | Processing file: weekly_record_449_1999-07-26T00-00-00.000.json
    2026-04-15 21:07:04,084 | INFO | Skipping weekly_record_449_1999-07-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,085 | INFO | Processing file: weekly_record_44_1991-10-21T00-00-00.000.json
    2026-04-15 21:07:04,107 | INFO | Skipping weekly_record_44_1991-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,107 | INFO | Processing file: weekly_record_450_1999-08-02T00-00-00.000.json
    2026-04-15 21:07:04,126 | INFO | Skipping weekly_record_450_1999-08-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,127 | INFO | Processing file: weekly_record_451_1999-08-09T00-00-00.000.json
    2026-04-15 21:07:04,152 | INFO | Skipping weekly_record_451_1999-08-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,153 | INFO | Processing file: weekly_record_452_1999-08-16T00-00-00.000.json
    2026-04-15 21:07:04,170 | INFO | Skipping weekly_record_452_1999-08-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,170 | INFO | Processing file: weekly_record_453_1999-08-23T00-00-00.000.json
    2026-04-15 21:07:04,193 | INFO | Skipping weekly_record_453_1999-08-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,194 | INFO | Processing file: weekly_record_454_1999-08-30T00-00-00.000.json
    2026-04-15 21:07:04,212 | INFO | Skipping weekly_record_454_1999-08-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,212 | INFO | Processing file: weekly_record_455_1999-09-06T00-00-00.000.json
    2026-04-15 21:07:04,239 | INFO | Skipping weekly_record_455_1999-09-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,240 | INFO | Processing file: weekly_record_456_1999-09-13T00-00-00.000.json
    2026-04-15 21:07:04,265 | INFO | Skipping weekly_record_456_1999-09-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,266 | INFO | Processing file: weekly_record_457_1999-09-20T00-00-00.000.json
    2026-04-15 21:07:04,292 | INFO | Skipping weekly_record_457_1999-09-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,293 | INFO | Processing file: weekly_record_458_1999-09-27T00-00-00.000.json
    2026-04-15 21:07:04,319 | INFO | Skipping weekly_record_458_1999-09-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,320 | INFO | Processing file: weekly_record_459_1999-10-04T00-00-00.000.json
    2026-04-15 21:07:04,374 | INFO | Skipping weekly_record_459_1999-10-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,384 | INFO | Processing file: weekly_record_45_1991-10-28T00-00-00.000.json
    2026-04-15 21:07:04,406 | INFO | Skipping weekly_record_45_1991-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,406 | INFO | Processing file: weekly_record_460_1999-10-11T00-00-00.000.json
    2026-04-15 21:07:04,431 | INFO | Skipping weekly_record_460_1999-10-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,432 | INFO | Processing file: weekly_record_461_1999-10-18T00-00-00.000.json
    2026-04-15 21:07:04,459 | INFO | Skipping weekly_record_461_1999-10-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,460 | INFO | Processing file: weekly_record_462_1999-10-25T00-00-00.000.json
    2026-04-15 21:07:04,475 | INFO | Skipping weekly_record_462_1999-10-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,476 | INFO | Processing file: weekly_record_463_1999-11-01T00-00-00.000.json
    2026-04-15 21:07:04,502 | INFO | Skipping weekly_record_463_1999-11-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,503 | INFO | Processing file: weekly_record_464_1999-11-08T00-00-00.000.json
    2026-04-15 21:07:04,521 | INFO | Skipping weekly_record_464_1999-11-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,521 | INFO | Processing file: weekly_record_465_1999-11-15T00-00-00.000.json
    2026-04-15 21:07:04,547 | INFO | Skipping weekly_record_465_1999-11-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,548 | INFO | Processing file: weekly_record_466_1999-11-22T00-00-00.000.json
    2026-04-15 21:07:04,566 | INFO | Skipping weekly_record_466_1999-11-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,567 | INFO | Processing file: weekly_record_467_1999-11-29T00-00-00.000.json
    2026-04-15 21:07:04,592 | INFO | Skipping weekly_record_467_1999-11-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,593 | INFO | Processing file: weekly_record_468_1999-12-06T00-00-00.000.json
    2026-04-15 21:07:04,610 | INFO | Skipping weekly_record_468_1999-12-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,610 | INFO | Processing file: weekly_record_469_1999-12-13T00-00-00.000.json
    2026-04-15 21:07:04,641 | INFO | Skipping weekly_record_469_1999-12-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,641 | INFO | Processing file: weekly_record_46_1991-11-04T00-00-00.000.json
    2026-04-15 21:07:04,658 | INFO | Skipping weekly_record_46_1991-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,659 | INFO | Processing file: weekly_record_470_1999-12-20T00-00-00.000.json
    2026-04-15 21:07:04,687 | INFO | Skipping weekly_record_470_1999-12-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,687 | INFO | Processing file: weekly_record_471_1999-12-27T00-00-00.000.json
    2026-04-15 21:07:04,704 | INFO | Skipping weekly_record_471_1999-12-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,704 | INFO | Processing file: weekly_record_472_2000-01-03T00-00-00.000.json
    2026-04-15 21:07:04,732 | INFO | Skipping weekly_record_472_2000-01-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,732 | INFO | Processing file: weekly_record_473_2000-01-10T00-00-00.000.json
    2026-04-15 21:07:04,749 | INFO | Skipping weekly_record_473_2000-01-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,750 | INFO | Processing file: weekly_record_474_2000-01-17T00-00-00.000.json
    2026-04-15 21:07:04,777 | INFO | Skipping weekly_record_474_2000-01-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,778 | INFO | Processing file: weekly_record_475_2000-01-24T00-00-00.000.json
    2026-04-15 21:07:04,796 | INFO | Skipping weekly_record_475_2000-01-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,797 | INFO | Processing file: weekly_record_476_2000-01-31T00-00-00.000.json
    2026-04-15 21:07:04,817 | INFO | Skipping weekly_record_476_2000-01-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,818 | INFO | Processing file: weekly_record_477_2000-02-07T00-00-00.000.json
    2026-04-15 21:07:04,840 | INFO | Skipping weekly_record_477_2000-02-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,841 | INFO | Processing file: weekly_record_478_2000-02-14T00-00-00.000.json
    2026-04-15 21:07:04,871 | INFO | Skipping weekly_record_478_2000-02-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,897 | INFO | Processing file: weekly_record_479_2000-02-21T00-00-00.000.json
    2026-04-15 21:07:04,917 | INFO | Skipping weekly_record_479_2000-02-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,918 | INFO | Processing file: weekly_record_47_1991-11-11T00-00-00.000.json
    2026-04-15 21:07:04,948 | INFO | Skipping weekly_record_47_1991-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,949 | INFO | Processing file: weekly_record_480_2000-02-28T00-00-00.000.json
    2026-04-15 21:07:04,967 | INFO | Skipping weekly_record_480_2000-02-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,968 | INFO | Processing file: weekly_record_481_2000-03-06T00-00-00.000.json
    2026-04-15 21:07:04,985 | INFO | Skipping weekly_record_481_2000-03-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:04,986 | INFO | Processing file: weekly_record_482_2000-03-13T00-00-00.000.json
    2026-04-15 21:07:05,003 | INFO | Skipping weekly_record_482_2000-03-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,004 | INFO | Processing file: weekly_record_483_2000-03-20T00-00-00.000.json
    2026-04-15 21:07:05,031 | INFO | Skipping weekly_record_483_2000-03-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,032 | INFO | Processing file: weekly_record_484_2000-03-27T00-00-00.000.json
    2026-04-15 21:07:05,047 | INFO | Skipping weekly_record_484_2000-03-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,048 | INFO | Processing file: weekly_record_485_2000-04-03T00-00-00.000.json
    2026-04-15 21:07:05,073 | INFO | Skipping weekly_record_485_2000-04-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,074 | INFO | Processing file: weekly_record_486_2000-04-10T00-00-00.000.json
    2026-04-15 21:07:05,104 | INFO | Skipping weekly_record_486_2000-04-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,105 | INFO | Processing file: weekly_record_487_2000-04-17T00-00-00.000.json
    2026-04-15 21:07:05,119 | INFO | Skipping weekly_record_487_2000-04-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,123 | INFO | Processing file: weekly_record_488_2000-04-24T00-00-00.000.json
    2026-04-15 21:07:05,148 | INFO | Skipping weekly_record_488_2000-04-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,149 | INFO | Processing file: weekly_record_489_2000-05-01T00-00-00.000.json
    2026-04-15 21:07:05,165 | INFO | Skipping weekly_record_489_2000-05-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,166 | INFO | Processing file: weekly_record_48_1991-11-18T00-00-00.000.json
    2026-04-15 21:07:05,191 | INFO | Skipping weekly_record_48_1991-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,192 | INFO | Processing file: weekly_record_490_2000-05-08T00-00-00.000.json
    2026-04-15 21:07:05,209 | INFO | Skipping weekly_record_490_2000-05-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,210 | INFO | Processing file: weekly_record_491_2000-05-15T00-00-00.000.json
    2026-04-15 21:07:05,236 | INFO | Skipping weekly_record_491_2000-05-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,236 | INFO | Processing file: weekly_record_492_2000-05-22T00-00-00.000.json
    2026-04-15 21:07:05,254 | INFO | Skipping weekly_record_492_2000-05-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,254 | INFO | Processing file: weekly_record_493_2000-05-29T00-00-00.000.json
    2026-04-15 21:07:05,273 | INFO | Skipping weekly_record_493_2000-05-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,274 | INFO | Processing file: weekly_record_494_2000-06-05T00-00-00.000.json
    2026-04-15 21:07:05,296 | INFO | Skipping weekly_record_494_2000-06-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,296 | INFO | Processing file: weekly_record_495_2000-06-12T00-00-00.000.json
    2026-04-15 21:07:05,321 | INFO | Skipping weekly_record_495_2000-06-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,322 | INFO | Processing file: weekly_record_496_2000-06-19T00-00-00.000.json
    2026-04-15 21:07:05,342 | INFO | Skipping weekly_record_496_2000-06-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,343 | INFO | Processing file: weekly_record_497_2000-06-26T00-00-00.000.json
    2026-04-15 21:07:05,360 | INFO | Skipping weekly_record_497_2000-06-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,361 | INFO | Processing file: weekly_record_498_2000-07-03T00-00-00.000.json
    2026-04-15 21:07:05,403 | INFO | Skipping weekly_record_498_2000-07-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,404 | INFO | Processing file: weekly_record_499_2000-07-10T00-00-00.000.json
    2026-04-15 21:07:05,420 | INFO | Skipping weekly_record_499_2000-07-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,421 | INFO | Processing file: weekly_record_49_1991-11-25T00-00-00.000.json
    2026-04-15 21:07:05,441 | INFO | Skipping weekly_record_49_1991-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,442 | INFO | Processing file: weekly_record_4_1990-12-03T00-00-00.000.json
    2026-04-15 21:07:05,468 | INFO | Skipping weekly_record_4_1990-12-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,469 | INFO | Processing file: weekly_record_500_2000-07-17T00-00-00.000.json
    2026-04-15 21:07:05,484 | INFO | Skipping weekly_record_500_2000-07-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,485 | INFO | Processing file: weekly_record_501_2000-07-24T00-00-00.000.json
    2026-04-15 21:07:05,799 | INFO | Skipping weekly_record_501_2000-07-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,813 | INFO | Processing file: weekly_record_502_2000-07-31T00-00-00.000.json
    2026-04-15 21:07:05,857 | INFO | Skipping weekly_record_502_2000-07-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,857 | INFO | Processing file: weekly_record_503_2000-08-07T00-00-00.000.json
    2026-04-15 21:07:05,873 | INFO | Skipping weekly_record_503_2000-08-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,874 | INFO | Processing file: weekly_record_504_2000-08-14T00-00-00.000.json
    2026-04-15 21:07:05,899 | INFO | Skipping weekly_record_504_2000-08-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,900 | INFO | Processing file: weekly_record_505_2000-08-21T00-00-00.000.json
    2026-04-15 21:07:05,923 | INFO | Skipping weekly_record_505_2000-08-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,924 | INFO | Processing file: weekly_record_506_2000-08-28T00-00-00.000.json
    2026-04-15 21:07:05,939 | INFO | Skipping weekly_record_506_2000-08-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,940 | INFO | Processing file: weekly_record_507_2000-09-04T00-00-00.000.json
    2026-04-15 21:07:05,966 | INFO | Skipping weekly_record_507_2000-09-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,967 | INFO | Processing file: weekly_record_508_2000-09-11T00-00-00.000.json
    2026-04-15 21:07:05,984 | INFO | Skipping weekly_record_508_2000-09-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:05,984 | INFO | Processing file: weekly_record_509_2000-09-18T00-00-00.000.json
    2026-04-15 21:07:06,003 | INFO | Skipping weekly_record_509_2000-09-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,003 | INFO | Processing file: weekly_record_50_1991-12-02T00-00-00.000.json
    2026-04-15 21:07:06,020 | INFO | Skipping weekly_record_50_1991-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,021 | INFO | Processing file: weekly_record_510_2000-09-25T00-00-00.000.json
    2026-04-15 21:07:06,047 | INFO | Skipping weekly_record_510_2000-09-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,048 | INFO | Processing file: weekly_record_511_2000-10-02T00-00-00.000.json
    2026-04-15 21:07:06,068 | INFO | Skipping weekly_record_511_2000-10-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,069 | INFO | Processing file: weekly_record_512_2000-10-09T00-00-00.000.json
    2026-04-15 21:07:06,097 | INFO | Skipping weekly_record_512_2000-10-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,097 | INFO | Processing file: weekly_record_513_2000-10-16T00-00-00.000.json
    2026-04-15 21:07:06,124 | INFO | Skipping weekly_record_513_2000-10-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,124 | INFO | Processing file: weekly_record_514_2000-10-23T00-00-00.000.json
    2026-04-15 21:07:06,144 | INFO | Skipping weekly_record_514_2000-10-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,145 | INFO | Processing file: weekly_record_515_2000-10-30T00-00-00.000.json
    2026-04-15 21:07:06,163 | INFO | Skipping weekly_record_515_2000-10-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,163 | INFO | Processing file: weekly_record_516_2000-11-06T00-00-00.000.json
    2026-04-15 21:07:06,219 | INFO | Skipping weekly_record_516_2000-11-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,222 | INFO | Processing file: weekly_record_517_2000-11-13T00-00-00.000.json
    2026-04-15 21:07:06,250 | INFO | Skipping weekly_record_517_2000-11-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,250 | INFO | Processing file: weekly_record_518_2000-11-20T00-00-00.000.json
    2026-04-15 21:07:06,270 | INFO | Skipping weekly_record_518_2000-11-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,271 | INFO | Processing file: weekly_record_519_2000-11-27T00-00-00.000.json
    2026-04-15 21:07:06,295 | INFO | Skipping weekly_record_519_2000-11-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,296 | INFO | Processing file: weekly_record_51_1991-12-09T00-00-00.000.json
    2026-04-15 21:07:06,320 | INFO | Skipping weekly_record_51_1991-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,321 | INFO | Processing file: weekly_record_520_2000-12-04T00-00-00.000.json
    2026-04-15 21:07:06,339 | INFO | Skipping weekly_record_520_2000-12-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,340 | INFO | Processing file: weekly_record_521_2000-12-11T00-00-00.000.json
    2026-04-15 21:07:06,365 | INFO | Skipping weekly_record_521_2000-12-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,366 | INFO | Processing file: weekly_record_522_2000-12-18T00-00-00.000.json
    2026-04-15 21:07:06,387 | INFO | Skipping weekly_record_522_2000-12-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,388 | INFO | Processing file: weekly_record_523_2000-12-25T00-00-00.000.json
    2026-04-15 21:07:06,405 | INFO | Skipping weekly_record_523_2000-12-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,405 | INFO | Processing file: weekly_record_524_2001-01-01T00-00-00.000.json
    2026-04-15 21:07:06,419 | INFO | Skipping weekly_record_524_2001-01-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,420 | INFO | Processing file: weekly_record_525_2001-01-08T00-00-00.000.json
    2026-04-15 21:07:06,446 | INFO | Skipping weekly_record_525_2001-01-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,447 | INFO | Processing file: weekly_record_526_2001-01-15T00-00-00.000.json
    2026-04-15 21:07:06,478 | INFO | Skipping weekly_record_526_2001-01-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,478 | INFO | Processing file: weekly_record_527_2001-01-22T00-00-00.000.json
    2026-04-15 21:07:06,522 | INFO | Skipping weekly_record_527_2001-01-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,522 | INFO | Processing file: weekly_record_528_2001-01-29T00-00-00.000.json
    2026-04-15 21:07:06,547 | INFO | Skipping weekly_record_528_2001-01-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,548 | INFO | Processing file: weekly_record_529_2001-02-05T00-00-00.000.json
    2026-04-15 21:07:06,574 | INFO | Skipping weekly_record_529_2001-02-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,574 | INFO | Processing file: weekly_record_52_1991-12-16T00-00-00.000.json
    2026-04-15 21:07:06,589 | INFO | Skipping weekly_record_52_1991-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,590 | INFO | Processing file: weekly_record_530_2001-02-12T00-00-00.000.json
    2026-04-15 21:07:06,616 | INFO | Skipping weekly_record_530_2001-02-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,617 | INFO | Processing file: weekly_record_531_2001-02-19T00-00-00.000.json
    2026-04-15 21:07:06,659 | INFO | Skipping weekly_record_531_2001-02-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,659 | INFO | Processing file: weekly_record_532_2001-02-26T00-00-00.000.json
    2026-04-15 21:07:06,685 | INFO | Skipping weekly_record_532_2001-02-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,686 | INFO | Processing file: weekly_record_533_2001-03-05T00-00-00.000.json
    2026-04-15 21:07:06,708 | INFO | Skipping weekly_record_533_2001-03-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,709 | INFO | Processing file: weekly_record_534_2001-03-12T00-00-00.000.json
    2026-04-15 21:07:06,735 | INFO | Skipping weekly_record_534_2001-03-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,735 | INFO | Processing file: weekly_record_535_2001-03-19T00-00-00.000.json
    2026-04-15 21:07:06,790 | INFO | Skipping weekly_record_535_2001-03-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,817 | INFO | Processing file: weekly_record_536_2001-03-26T00-00-00.000.json
    2026-04-15 21:07:06,843 | INFO | Skipping weekly_record_536_2001-03-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,843 | INFO | Processing file: weekly_record_537_2001-04-02T00-00-00.000.json
    2026-04-15 21:07:06,870 | INFO | Skipping weekly_record_537_2001-04-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,871 | INFO | Processing file: weekly_record_538_2001-04-09T00-00-00.000.json
    2026-04-15 21:07:06,901 | INFO | Skipping weekly_record_538_2001-04-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,902 | INFO | Processing file: weekly_record_539_2001-04-16T00-00-00.000.json
    2026-04-15 21:07:06,957 | INFO | Skipping weekly_record_539_2001-04-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:06,958 | INFO | Processing file: weekly_record_53_1991-12-23T00-00-00.000.json
    2026-04-15 21:07:07,011 | INFO | Skipping weekly_record_53_1991-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,011 | INFO | Processing file: weekly_record_540_2001-04-23T00-00-00.000.json
    2026-04-15 21:07:07,059 | INFO | Skipping weekly_record_540_2001-04-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,059 | INFO | Processing file: weekly_record_541_2001-04-30T00-00-00.000.json
    2026-04-15 21:07:07,086 | INFO | Skipping weekly_record_541_2001-04-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,087 | INFO | Processing file: weekly_record_542_2001-05-07T00-00-00.000.json
    2026-04-15 21:07:07,110 | INFO | Skipping weekly_record_542_2001-05-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,112 | INFO | Processing file: weekly_record_543_2001-05-14T00-00-00.000.json
    2026-04-15 21:07:07,144 | INFO | Skipping weekly_record_543_2001-05-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,145 | INFO | Processing file: weekly_record_544_2001-05-21T00-00-00.000.json
    2026-04-15 21:07:07,189 | INFO | Skipping weekly_record_544_2001-05-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,190 | INFO | Processing file: weekly_record_545_2001-05-28T00-00-00.000.json
    2026-04-15 21:07:07,230 | INFO | Skipping weekly_record_545_2001-05-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,231 | INFO | Processing file: weekly_record_546_2001-06-04T00-00-00.000.json
    2026-04-15 21:07:07,257 | INFO | Skipping weekly_record_546_2001-06-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,257 | INFO | Processing file: weekly_record_547_2001-06-11T00-00-00.000.json
    2026-04-15 21:07:07,280 | INFO | Skipping weekly_record_547_2001-06-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,281 | INFO | Processing file: weekly_record_548_2001-06-18T00-00-00.000.json
    2026-04-15 21:07:07,316 | INFO | Skipping weekly_record_548_2001-06-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,317 | INFO | Processing file: weekly_record_549_2001-06-25T00-00-00.000.json
    2026-04-15 21:07:07,337 | INFO | Skipping weekly_record_549_2001-06-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,338 | INFO | Processing file: weekly_record_54_1991-12-30T00-00-00.000.json
    2026-04-15 21:07:07,364 | INFO | Skipping weekly_record_54_1991-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,365 | INFO | Processing file: weekly_record_550_2001-07-02T00-00-00.000.json
    2026-04-15 21:07:07,382 | INFO | Skipping weekly_record_550_2001-07-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,383 | INFO | Processing file: weekly_record_551_2001-07-09T00-00-00.000.json
    2026-04-15 21:07:07,408 | INFO | Skipping weekly_record_551_2001-07-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,409 | INFO | Processing file: weekly_record_552_2001-07-16T00-00-00.000.json
    2026-04-15 21:07:07,429 | INFO | Skipping weekly_record_552_2001-07-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,430 | INFO | Processing file: weekly_record_553_2001-07-23T00-00-00.000.json
    2026-04-15 21:07:07,459 | INFO | Skipping weekly_record_553_2001-07-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,459 | INFO | Processing file: weekly_record_554_2001-07-30T00-00-00.000.json
    2026-04-15 21:07:07,474 | INFO | Skipping weekly_record_554_2001-07-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,474 | INFO | Processing file: weekly_record_555_2001-08-06T00-00-00.000.json
    2026-04-15 21:07:07,502 | INFO | Skipping weekly_record_555_2001-08-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,503 | INFO | Processing file: weekly_record_556_2001-08-13T00-00-00.000.json
    2026-04-15 21:07:07,522 | INFO | Skipping weekly_record_556_2001-08-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,524 | INFO | Processing file: weekly_record_557_2001-08-20T00-00-00.000.json
    2026-04-15 21:07:07,548 | INFO | Skipping weekly_record_557_2001-08-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,579 | INFO | Processing file: weekly_record_558_2001-08-27T00-00-00.000.json
    2026-04-15 21:07:07,605 | INFO | Skipping weekly_record_558_2001-08-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,606 | INFO | Processing file: weekly_record_559_2001-09-03T00-00-00.000.json
    2026-04-15 21:07:07,640 | INFO | Skipping weekly_record_559_2001-09-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,640 | INFO | Processing file: weekly_record_55_1992-01-06T00-00-00.000.json
    2026-04-15 21:07:07,659 | INFO | Skipping weekly_record_55_1992-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,660 | INFO | Processing file: weekly_record_560_2001-09-10T00-00-00.000.json
    2026-04-15 21:07:07,686 | INFO | Skipping weekly_record_560_2001-09-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,686 | INFO | Processing file: weekly_record_561_2001-09-17T00-00-00.000.json
    2026-04-15 21:07:07,711 | INFO | Skipping weekly_record_561_2001-09-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,712 | INFO | Processing file: weekly_record_562_2001-09-24T00-00-00.000.json
    2026-04-15 21:07:07,730 | INFO | Skipping weekly_record_562_2001-09-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,730 | INFO | Processing file: weekly_record_563_2001-10-01T00-00-00.000.json
    2026-04-15 21:07:07,755 | INFO | Skipping weekly_record_563_2001-10-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,756 | INFO | Processing file: weekly_record_564_2001-10-08T00-00-00.000.json
    2026-04-15 21:07:07,774 | INFO | Skipping weekly_record_564_2001-10-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,775 | INFO | Processing file: weekly_record_565_2001-10-15T00-00-00.000.json
    2026-04-15 21:07:07,801 | INFO | Skipping weekly_record_565_2001-10-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,801 | INFO | Processing file: weekly_record_566_2001-10-22T00-00-00.000.json
    2026-04-15 21:07:07,827 | INFO | Skipping weekly_record_566_2001-10-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,828 | INFO | Processing file: weekly_record_567_2001-10-29T00-00-00.000.json
    2026-04-15 21:07:07,854 | INFO | Skipping weekly_record_567_2001-10-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,855 | INFO | Processing file: weekly_record_568_2001-11-05T00-00-00.000.json
    2026-04-15 21:07:07,882 | INFO | Skipping weekly_record_568_2001-11-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,883 | INFO | Processing file: weekly_record_569_2001-11-12T00-00-00.000.json
    2026-04-15 21:07:07,900 | INFO | Skipping weekly_record_569_2001-11-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,900 | INFO | Processing file: weekly_record_56_1992-01-13T00-00-00.000.json
    2026-04-15 21:07:07,925 | INFO | Skipping weekly_record_56_1992-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,925 | INFO | Processing file: weekly_record_570_2001-11-19T00-00-00.000.json
    2026-04-15 21:07:07,943 | INFO | Skipping weekly_record_570_2001-11-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,944 | INFO | Processing file: weekly_record_571_2001-11-26T00-00-00.000.json
    2026-04-15 21:07:07,962 | INFO | Skipping weekly_record_571_2001-11-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,963 | INFO | Processing file: weekly_record_572_2001-12-03T00-00-00.000.json
    2026-04-15 21:07:07,990 | INFO | Skipping weekly_record_572_2001-12-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:07,990 | INFO | Processing file: weekly_record_573_2001-12-10T00-00-00.000.json
    2026-04-15 21:07:08,007 | INFO | Skipping weekly_record_573_2001-12-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,008 | INFO | Processing file: weekly_record_574_2001-12-17T00-00-00.000.json
    2026-04-15 21:07:08,036 | INFO | Skipping weekly_record_574_2001-12-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,036 | INFO | Processing file: weekly_record_575_2001-12-24T00-00-00.000.json
    2026-04-15 21:07:08,061 | INFO | Skipping weekly_record_575_2001-12-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,061 | INFO | Processing file: weekly_record_576_2001-12-31T00-00-00.000.json
    2026-04-15 21:07:08,079 | INFO | Skipping weekly_record_576_2001-12-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,080 | INFO | Processing file: weekly_record_577_2002-01-07T00-00-00.000.json
    2026-04-15 21:07:08,107 | INFO | Skipping weekly_record_577_2002-01-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,115 | INFO | Processing file: weekly_record_578_2002-01-14T00-00-00.000.json
    2026-04-15 21:07:08,154 | INFO | Skipping weekly_record_578_2002-01-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,155 | INFO | Processing file: weekly_record_579_2002-01-21T00-00-00.000.json
    2026-04-15 21:07:08,176 | INFO | Skipping weekly_record_579_2002-01-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,177 | INFO | Processing file: weekly_record_57_1992-01-20T00-00-00.000.json
    2026-04-15 21:07:08,194 | INFO | Skipping weekly_record_57_1992-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,195 | INFO | Processing file: weekly_record_580_2002-01-28T00-00-00.000.json
    2026-04-15 21:07:08,222 | INFO | Skipping weekly_record_580_2002-01-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,223 | INFO | Processing file: weekly_record_581_2002-02-04T00-00-00.000.json
    2026-04-15 21:07:08,250 | INFO | Skipping weekly_record_581_2002-02-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,251 | INFO | Processing file: weekly_record_582_2002-02-11T00-00-00.000.json
    2026-04-15 21:07:08,267 | INFO | Skipping weekly_record_582_2002-02-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,268 | INFO | Processing file: weekly_record_583_2002-02-18T00-00-00.000.json
    2026-04-15 21:07:08,293 | INFO | Skipping weekly_record_583_2002-02-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,293 | INFO | Processing file: weekly_record_584_2002-02-25T00-00-00.000.json
    2026-04-15 21:07:08,311 | INFO | Skipping weekly_record_584_2002-02-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,311 | INFO | Processing file: weekly_record_585_2002-03-04T00-00-00.000.json
    2026-04-15 21:07:08,330 | INFO | Skipping weekly_record_585_2002-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,331 | INFO | Processing file: weekly_record_586_2002-03-11T00-00-00.000.json
    2026-04-15 21:07:08,356 | INFO | Skipping weekly_record_586_2002-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,357 | INFO | Processing file: weekly_record_587_2002-03-18T00-00-00.000.json
    2026-04-15 21:07:08,381 | INFO | Skipping weekly_record_587_2002-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,381 | INFO | Processing file: weekly_record_588_2002-03-25T00-00-00.000.json
    2026-04-15 21:07:08,402 | INFO | Skipping weekly_record_588_2002-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,403 | INFO | Processing file: weekly_record_589_2002-04-01T00-00-00.000.json
    2026-04-15 21:07:08,430 | INFO | Skipping weekly_record_589_2002-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,431 | INFO | Processing file: weekly_record_58_1992-01-27T00-00-00.000.json
    2026-04-15 21:07:08,447 | INFO | Skipping weekly_record_58_1992-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,448 | INFO | Processing file: weekly_record_590_2002-04-08T00-00-00.000.json
    2026-04-15 21:07:08,475 | INFO | Skipping weekly_record_590_2002-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,476 | INFO | Processing file: weekly_record_591_2002-04-15T00-00-00.000.json
    2026-04-15 21:07:08,493 | INFO | Skipping weekly_record_591_2002-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,493 | INFO | Processing file: weekly_record_592_2002-04-22T00-00-00.000.json
    2026-04-15 21:07:08,520 | INFO | Skipping weekly_record_592_2002-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,520 | INFO | Processing file: weekly_record_593_2002-04-29T00-00-00.000.json
    2026-04-15 21:07:08,540 | INFO | Skipping weekly_record_593_2002-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,541 | INFO | Processing file: weekly_record_594_2002-05-06T00-00-00.000.json
    2026-04-15 21:07:08,580 | INFO | Skipping weekly_record_594_2002-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,600 | INFO | Processing file: weekly_record_595_2002-05-13T00-00-00.000.json
    2026-04-15 21:07:08,620 | INFO | Skipping weekly_record_595_2002-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,620 | INFO | Processing file: weekly_record_596_2002-05-20T00-00-00.000.json
    2026-04-15 21:07:08,637 | INFO | Skipping weekly_record_596_2002-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,638 | INFO | Processing file: weekly_record_597_2002-05-27T00-00-00.000.json
    2026-04-15 21:07:08,664 | INFO | Skipping weekly_record_597_2002-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,664 | INFO | Processing file: weekly_record_598_2002-06-03T00-00-00.000.json
    2026-04-15 21:07:08,680 | INFO | Skipping weekly_record_598_2002-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,680 | INFO | Processing file: weekly_record_599_2002-06-10T00-00-00.000.json
    2026-04-15 21:07:08,704 | INFO | Skipping weekly_record_599_2002-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,705 | INFO | Processing file: weekly_record_59_1992-02-03T00-00-00.000.json
    2026-04-15 21:07:08,730 | INFO | Skipping weekly_record_59_1992-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,731 | INFO | Processing file: weekly_record_5_1991-01-21T00-00-00.000.json
    2026-04-15 21:07:08,757 | INFO | Skipping weekly_record_5_1991-01-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,757 | INFO | Processing file: weekly_record_600_2002-06-17T00-00-00.000.json
    2026-04-15 21:07:08,784 | INFO | Skipping weekly_record_600_2002-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,785 | INFO | Processing file: weekly_record_601_2002-06-24T00-00-00.000.json
    2026-04-15 21:07:08,811 | INFO | Skipping weekly_record_601_2002-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,811 | INFO | Processing file: weekly_record_602_2002-07-01T00-00-00.000.json
    2026-04-15 21:07:08,835 | INFO | Skipping weekly_record_602_2002-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,835 | INFO | Processing file: weekly_record_603_2002-07-08T00-00-00.000.json
    2026-04-15 21:07:08,862 | INFO | Skipping weekly_record_603_2002-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,863 | INFO | Processing file: weekly_record_604_2002-07-15T00-00-00.000.json
    2026-04-15 21:07:08,883 | INFO | Skipping weekly_record_604_2002-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,883 | INFO | Processing file: weekly_record_605_2002-07-22T00-00-00.000.json
    2026-04-15 21:07:08,906 | INFO | Skipping weekly_record_605_2002-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,906 | INFO | Processing file: weekly_record_606_2002-07-29T00-00-00.000.json
    2026-04-15 21:07:08,924 | INFO | Skipping weekly_record_606_2002-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,925 | INFO | Processing file: weekly_record_607_2002-08-05T00-00-00.000.json
    2026-04-15 21:07:08,950 | INFO | Skipping weekly_record_607_2002-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,951 | INFO | Processing file: weekly_record_608_2002-08-12T00-00-00.000.json
    2026-04-15 21:07:08,964 | INFO | Skipping weekly_record_608_2002-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,965 | INFO | Processing file: weekly_record_609_2002-08-19T00-00-00.000.json
    2026-04-15 21:07:08,991 | INFO | Skipping weekly_record_609_2002-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:08,991 | INFO | Processing file: weekly_record_60_1992-02-10T00-00-00.000.json
    2026-04-15 21:07:09,017 | INFO | Skipping weekly_record_60_1992-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,025 | INFO | Processing file: weekly_record_610_2002-08-26T00-00-00.000.json
    2026-04-15 21:07:09,057 | INFO | Skipping weekly_record_610_2002-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,059 | INFO | Processing file: weekly_record_611_2002-09-02T00-00-00.000.json
    2026-04-15 21:07:09,085 | INFO | Skipping weekly_record_611_2002-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,086 | INFO | Processing file: weekly_record_612_2002-09-09T00-00-00.000.json
    2026-04-15 21:07:09,103 | INFO | Skipping weekly_record_612_2002-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,103 | INFO | Processing file: weekly_record_613_2002-09-16T00-00-00.000.json
    2026-04-15 21:07:09,129 | INFO | Skipping weekly_record_613_2002-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,130 | INFO | Processing file: weekly_record_614_2002-09-23T00-00-00.000.json
    2026-04-15 21:07:09,146 | INFO | Skipping weekly_record_614_2002-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,146 | INFO | Processing file: weekly_record_615_2002-09-30T00-00-00.000.json
    2026-04-15 21:07:09,169 | INFO | Skipping weekly_record_615_2002-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,169 | INFO | Processing file: weekly_record_616_2002-10-07T00-00-00.000.json
    2026-04-15 21:07:09,196 | INFO | Skipping weekly_record_616_2002-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,197 | INFO | Processing file: weekly_record_617_2002-10-14T00-00-00.000.json
    2026-04-15 21:07:09,222 | INFO | Skipping weekly_record_617_2002-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,222 | INFO | Processing file: weekly_record_618_2002-10-21T00-00-00.000.json
    2026-04-15 21:07:09,251 | INFO | Skipping weekly_record_618_2002-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,252 | INFO | Processing file: weekly_record_619_2002-10-28T00-00-00.000.json
    2026-04-15 21:07:09,279 | INFO | Skipping weekly_record_619_2002-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,279 | INFO | Processing file: weekly_record_61_1992-02-17T00-00-00.000.json
    2026-04-15 21:07:09,308 | INFO | Skipping weekly_record_61_1992-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,309 | INFO | Processing file: weekly_record_620_2002-11-04T00-00-00.000.json
    2026-04-15 21:07:09,334 | INFO | Skipping weekly_record_620_2002-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,334 | INFO | Processing file: weekly_record_621_2002-11-11T00-00-00.000.json
    2026-04-15 21:07:09,360 | INFO | Skipping weekly_record_621_2002-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,360 | INFO | Processing file: weekly_record_622_2002-11-18T00-00-00.000.json
    2026-04-15 21:07:09,382 | INFO | Skipping weekly_record_622_2002-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,384 | INFO | Processing file: weekly_record_623_2002-11-25T00-00-00.000.json
    2026-04-15 21:07:09,409 | INFO | Skipping weekly_record_623_2002-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,410 | INFO | Processing file: weekly_record_624_2002-12-02T00-00-00.000.json
    2026-04-15 21:07:09,434 | INFO | Skipping weekly_record_624_2002-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,434 | INFO | Processing file: weekly_record_625_2002-12-09T00-00-00.000.json
    2026-04-15 21:07:09,451 | INFO | Skipping weekly_record_625_2002-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,451 | INFO | Processing file: weekly_record_626_2002-12-16T00-00-00.000.json
    2026-04-15 21:07:09,477 | INFO | Skipping weekly_record_626_2002-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,477 | INFO | Processing file: weekly_record_627_2002-12-23T00-00-00.000.json
    2026-04-15 21:07:09,493 | INFO | Skipping weekly_record_627_2002-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,494 | INFO | Processing file: weekly_record_628_2002-12-30T00-00-00.000.json
    2026-04-15 21:07:09,519 | INFO | Skipping weekly_record_628_2002-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,520 | INFO | Processing file: weekly_record_629_2003-01-06T00-00-00.000.json
    2026-04-15 21:07:09,542 | INFO | Skipping weekly_record_629_2003-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,543 | INFO | Processing file: weekly_record_62_1992-02-24T00-00-00.000.json
    2026-04-15 21:07:09,576 | INFO | Skipping weekly_record_62_1992-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,579 | INFO | Processing file: weekly_record_630_2003-01-13T00-00-00.000.json
    2026-04-15 21:07:09,608 | INFO | Skipping weekly_record_630_2003-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,609 | INFO | Processing file: weekly_record_631_2003-01-20T00-00-00.000.json
    2026-04-15 21:07:09,630 | INFO | Skipping weekly_record_631_2003-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,631 | INFO | Processing file: weekly_record_632_2003-01-27T00-00-00.000.json
    2026-04-15 21:07:09,657 | INFO | Skipping weekly_record_632_2003-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,658 | INFO | Processing file: weekly_record_633_2003-02-03T00-00-00.000.json
    2026-04-15 21:07:09,675 | INFO | Skipping weekly_record_633_2003-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,676 | INFO | Processing file: weekly_record_634_2003-02-10T00-00-00.000.json
    2026-04-15 21:07:09,704 | INFO | Skipping weekly_record_634_2003-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,704 | INFO | Processing file: weekly_record_635_2003-02-17T00-00-00.000.json
    2026-04-15 21:07:09,729 | INFO | Skipping weekly_record_635_2003-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,730 | INFO | Processing file: weekly_record_636_2003-02-24T00-00-00.000.json
    2026-04-15 21:07:09,746 | INFO | Skipping weekly_record_636_2003-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,747 | INFO | Processing file: weekly_record_637_2003-03-03T00-00-00.000.json
    2026-04-15 21:07:09,776 | INFO | Skipping weekly_record_637_2003-03-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,777 | INFO | Processing file: weekly_record_638_2003-03-10T00-00-00.000.json
    2026-04-15 21:07:09,793 | INFO | Skipping weekly_record_638_2003-03-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,794 | INFO | Processing file: weekly_record_639_2003-03-17T00-00-00.000.json
    2026-04-15 21:07:09,816 | INFO | Skipping weekly_record_639_2003-03-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,817 | INFO | Processing file: weekly_record_63_1992-03-02T00-00-00.000.json
    2026-04-15 21:07:09,833 | INFO | Skipping weekly_record_63_1992-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,834 | INFO | Processing file: weekly_record_640_2003-03-24T00-00-00.000.json
    2026-04-15 21:07:09,852 | INFO | Skipping weekly_record_640_2003-03-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,852 | INFO | Processing file: weekly_record_641_2003-03-31T00-00-00.000.json
    2026-04-15 21:07:09,879 | INFO | Skipping weekly_record_641_2003-03-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,879 | INFO | Processing file: weekly_record_642_2003-04-07T00-00-00.000.json
    2026-04-15 21:07:09,897 | INFO | Skipping weekly_record_642_2003-04-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,898 | INFO | Processing file: weekly_record_643_2003-04-14T00-00-00.000.json
    2026-04-15 21:07:09,923 | INFO | Skipping weekly_record_643_2003-04-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,924 | INFO | Processing file: weekly_record_644_2003-04-21T00-00-00.000.json
    2026-04-15 21:07:09,950 | INFO | Skipping weekly_record_644_2003-04-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,951 | INFO | Processing file: weekly_record_645_2003-04-28T00-00-00.000.json
    2026-04-15 21:07:09,978 | INFO | Skipping weekly_record_645_2003-04-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:09,979 | INFO | Processing file: weekly_record_646_2003-05-05T00-00-00.000.json
    2026-04-15 21:07:10,002 | INFO | Skipping weekly_record_646_2003-05-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,003 | INFO | Processing file: weekly_record_647_2003-05-12T00-00-00.000.json
    2026-04-15 21:07:10,023 | INFO | Skipping weekly_record_647_2003-05-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,026 | INFO | Processing file: weekly_record_648_2003-05-19T00-00-00.000.json
    2026-04-15 21:07:10,077 | INFO | Skipping weekly_record_648_2003-05-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,078 | INFO | Processing file: weekly_record_649_2003-05-26T00-00-00.000.json
    2026-04-15 21:07:10,099 | INFO | Skipping weekly_record_649_2003-05-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,100 | INFO | Processing file: weekly_record_64_1992-03-09T00-00-00.000.json
    2026-04-15 21:07:10,125 | INFO | Skipping weekly_record_64_1992-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,127 | INFO | Processing file: weekly_record_650_2003-06-02T00-00-00.000.json
    2026-04-15 21:07:10,153 | INFO | Skipping weekly_record_650_2003-06-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,153 | INFO | Processing file: weekly_record_651_2003-06-09T00-00-00.000.json
    2026-04-15 21:07:10,171 | INFO | Skipping weekly_record_651_2003-06-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,172 | INFO | Processing file: weekly_record_652_2003-06-16T00-00-00.000.json
    2026-04-15 21:07:10,190 | INFO | Skipping weekly_record_652_2003-06-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,191 | INFO | Processing file: weekly_record_653_2003-06-23T00-00-00.000.json
    2026-04-15 21:07:10,212 | INFO | Skipping weekly_record_653_2003-06-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,212 | INFO | Processing file: weekly_record_654_2003-06-30T00-00-00.000.json
    2026-04-15 21:07:10,226 | INFO | Skipping weekly_record_654_2003-06-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,227 | INFO | Processing file: weekly_record_655_2003-07-07T00-00-00.000.json
    2026-04-15 21:07:10,254 | INFO | Skipping weekly_record_655_2003-07-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,254 | INFO | Processing file: weekly_record_656_2003-07-14T00-00-00.000.json
    2026-04-15 21:07:10,280 | INFO | Skipping weekly_record_656_2003-07-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,280 | INFO | Processing file: weekly_record_657_2003-07-21T00-00-00.000.json
    2026-04-15 21:07:10,297 | INFO | Skipping weekly_record_657_2003-07-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,298 | INFO | Processing file: weekly_record_658_2003-07-28T00-00-00.000.json
    2026-04-15 21:07:10,324 | INFO | Skipping weekly_record_658_2003-07-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,325 | INFO | Processing file: weekly_record_659_2003-08-04T00-00-00.000.json
    2026-04-15 21:07:10,342 | INFO | Skipping weekly_record_659_2003-08-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,343 | INFO | Processing file: weekly_record_65_1992-03-16T00-00-00.000.json
    2026-04-15 21:07:10,369 | INFO | Skipping weekly_record_65_1992-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,370 | INFO | Processing file: weekly_record_660_2003-08-11T00-00-00.000.json
    2026-04-15 21:07:10,389 | INFO | Skipping weekly_record_660_2003-08-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,390 | INFO | Processing file: weekly_record_661_2003-08-18T00-00-00.000.json
    2026-04-15 21:07:10,417 | INFO | Skipping weekly_record_661_2003-08-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,418 | INFO | Processing file: weekly_record_662_2003-08-25T00-00-00.000.json
    2026-04-15 21:07:10,436 | INFO | Skipping weekly_record_662_2003-08-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,436 | INFO | Processing file: weekly_record_663_2003-09-01T00-00-00.000.json
    2026-04-15 21:07:10,464 | INFO | Skipping weekly_record_663_2003-09-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,464 | INFO | Processing file: weekly_record_664_2003-09-08T00-00-00.000.json
    2026-04-15 21:07:10,481 | INFO | Skipping weekly_record_664_2003-09-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,481 | INFO | Processing file: weekly_record_665_2003-09-15T00-00-00.000.json
    2026-04-15 21:07:10,509 | INFO | Skipping weekly_record_665_2003-09-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,533 | INFO | Processing file: weekly_record_666_2003-09-22T00-00-00.000.json
    2026-04-15 21:07:10,552 | INFO | Skipping weekly_record_666_2003-09-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,553 | INFO | Processing file: weekly_record_667_2003-09-29T00-00-00.000.json
    2026-04-15 21:07:10,580 | INFO | Skipping weekly_record_667_2003-09-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,580 | INFO | Processing file: weekly_record_668_2003-10-06T00-00-00.000.json
    2026-04-15 21:07:10,597 | INFO | Skipping weekly_record_668_2003-10-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,598 | INFO | Processing file: weekly_record_669_2003-10-13T00-00-00.000.json
    2026-04-15 21:07:10,618 | INFO | Skipping weekly_record_669_2003-10-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,619 | INFO | Processing file: weekly_record_66_1992-03-23T00-00-00.000.json
    2026-04-15 21:07:10,635 | INFO | Skipping weekly_record_66_1992-03-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,636 | INFO | Processing file: weekly_record_670_2003-10-20T00-00-00.000.json
    2026-04-15 21:07:10,663 | INFO | Skipping weekly_record_670_2003-10-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,664 | INFO | Processing file: weekly_record_671_2003-10-27T00-00-00.000.json
    2026-04-15 21:07:10,681 | INFO | Skipping weekly_record_671_2003-10-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,682 | INFO | Processing file: weekly_record_672_2003-11-03T00-00-00.000.json
    2026-04-15 21:07:10,706 | INFO | Skipping weekly_record_672_2003-11-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,706 | INFO | Processing file: weekly_record_673_2003-11-10T00-00-00.000.json
    2026-04-15 21:07:10,728 | INFO | Skipping weekly_record_673_2003-11-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,730 | INFO | Processing file: weekly_record_674_2003-11-17T00-00-00.000.json
    2026-04-15 21:07:10,752 | INFO | Skipping weekly_record_674_2003-11-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,752 | INFO | Processing file: weekly_record_675_2003-11-24T00-00-00.000.json
    2026-04-15 21:07:10,770 | INFO | Skipping weekly_record_675_2003-11-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,770 | INFO | Processing file: weekly_record_676_2003-12-01T00-00-00.000.json
    2026-04-15 21:07:10,797 | INFO | Skipping weekly_record_676_2003-12-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,797 | INFO | Processing file: weekly_record_677_2003-12-08T00-00-00.000.json
    2026-04-15 21:07:10,818 | INFO | Skipping weekly_record_677_2003-12-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,818 | INFO | Processing file: weekly_record_678_2003-12-15T00-00-00.000.json
    2026-04-15 21:07:10,844 | INFO | Skipping weekly_record_678_2003-12-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,845 | INFO | Processing file: weekly_record_679_2003-12-22T00-00-00.000.json
    2026-04-15 21:07:10,864 | INFO | Skipping weekly_record_679_2003-12-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,864 | INFO | Processing file: weekly_record_67_1992-03-30T00-00-00.000.json
    2026-04-15 21:07:10,889 | INFO | Skipping weekly_record_67_1992-03-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,890 | INFO | Processing file: weekly_record_680_2003-12-29T00-00-00.000.json
    2026-04-15 21:07:10,911 | INFO | Skipping weekly_record_680_2003-12-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,911 | INFO | Processing file: weekly_record_681_2004-01-05T00-00-00.000.json
    2026-04-15 21:07:10,936 | INFO | Skipping weekly_record_681_2004-01-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,943 | INFO | Processing file: weekly_record_682_2004-01-12T00-00-00.000.json
    2026-04-15 21:07:10,989 | INFO | Skipping weekly_record_682_2004-01-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:10,989 | INFO | Processing file: weekly_record_683_2004-01-19T00-00-00.000.json
    2026-04-15 21:07:11,013 | INFO | Skipping weekly_record_683_2004-01-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,014 | INFO | Processing file: weekly_record_684_2004-01-26T00-00-00.000.json
    2026-04-15 21:07:11,034 | INFO | Skipping weekly_record_684_2004-01-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,034 | INFO | Processing file: weekly_record_685_2004-02-02T00-00-00.000.json
    2026-04-15 21:07:11,061 | INFO | Skipping weekly_record_685_2004-02-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,062 | INFO | Processing file: weekly_record_686_2004-02-09T00-00-00.000.json
    2026-04-15 21:07:11,082 | INFO | Skipping weekly_record_686_2004-02-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,083 | INFO | Processing file: weekly_record_687_2004-02-16T00-00-00.000.json
    2026-04-15 21:07:11,108 | INFO | Skipping weekly_record_687_2004-02-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,108 | INFO | Processing file: weekly_record_688_2004-02-23T00-00-00.000.json
    2026-04-15 21:07:11,124 | INFO | Skipping weekly_record_688_2004-02-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,124 | INFO | Processing file: weekly_record_689_2004-03-01T00-00-00.000.json
    2026-04-15 21:07:11,153 | INFO | Skipping weekly_record_689_2004-03-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,153 | INFO | Processing file: weekly_record_68_1992-04-06T00-00-00.000.json
    2026-04-15 21:07:11,168 | INFO | Skipping weekly_record_68_1992-04-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,169 | INFO | Processing file: weekly_record_690_2004-03-08T00-00-00.000.json
    2026-04-15 21:07:11,195 | INFO | Skipping weekly_record_690_2004-03-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,196 | INFO | Processing file: weekly_record_691_2004-03-15T00-00-00.000.json
    2026-04-15 21:07:11,214 | INFO | Skipping weekly_record_691_2004-03-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,220 | INFO | Processing file: weekly_record_692_2004-03-22T00-00-00.000.json
    2026-04-15 21:07:11,241 | INFO | Skipping weekly_record_692_2004-03-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,241 | INFO | Processing file: weekly_record_693_2004-03-29T00-00-00.000.json
    2026-04-15 21:07:11,259 | INFO | Skipping weekly_record_693_2004-03-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,260 | INFO | Processing file: weekly_record_694_2004-04-05T00-00-00.000.json
    2026-04-15 21:07:11,288 | INFO | Skipping weekly_record_694_2004-04-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,288 | INFO | Processing file: weekly_record_695_2004-04-12T00-00-00.000.json
    2026-04-15 21:07:11,308 | INFO | Skipping weekly_record_695_2004-04-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,309 | INFO | Processing file: weekly_record_696_2004-04-19T00-00-00.000.json
    2026-04-15 21:07:11,339 | INFO | Skipping weekly_record_696_2004-04-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,339 | INFO | Processing file: weekly_record_697_2004-04-26T00-00-00.000.json
    2026-04-15 21:07:11,358 | INFO | Skipping weekly_record_697_2004-04-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,358 | INFO | Processing file: weekly_record_698_2004-05-03T00-00-00.000.json
    2026-04-15 21:07:11,384 | INFO | Skipping weekly_record_698_2004-05-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,385 | INFO | Processing file: weekly_record_699_2004-05-10T00-00-00.000.json
    2026-04-15 21:07:11,402 | INFO | Skipping weekly_record_699_2004-05-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,403 | INFO | Processing file: weekly_record_69_1992-04-13T00-00-00.000.json
    2026-04-15 21:07:11,449 | INFO | Skipping weekly_record_69_1992-04-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,454 | INFO | Processing file: weekly_record_6_1991-01-28T00-00-00.000.json
    2026-04-15 21:07:11,474 | INFO | Skipping weekly_record_6_1991-01-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,474 | INFO | Processing file: weekly_record_700_2004-05-17T00-00-00.000.json
    2026-04-15 21:07:11,492 | INFO | Skipping weekly_record_700_2004-05-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,492 | INFO | Processing file: weekly_record_701_2004-05-24T00-00-00.000.json
    2026-04-15 21:07:11,509 | INFO | Skipping weekly_record_701_2004-05-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,510 | INFO | Processing file: weekly_record_702_2004-05-31T00-00-00.000.json
    2026-04-15 21:07:11,527 | INFO | Skipping weekly_record_702_2004-05-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,528 | INFO | Processing file: weekly_record_703_2004-06-07T00-00-00.000.json
    2026-04-15 21:07:11,555 | INFO | Skipping weekly_record_703_2004-06-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,555 | INFO | Processing file: weekly_record_704_2004-06-14T00-00-00.000.json
    2026-04-15 21:07:11,571 | INFO | Skipping weekly_record_704_2004-06-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,571 | INFO | Processing file: weekly_record_705_2004-06-21T00-00-00.000.json
    2026-04-15 21:07:11,598 | INFO | Skipping weekly_record_705_2004-06-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,599 | INFO | Processing file: weekly_record_706_2004-06-28T00-00-00.000.json
    2026-04-15 21:07:11,616 | INFO | Skipping weekly_record_706_2004-06-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,617 | INFO | Processing file: weekly_record_707_2004-07-05T00-00-00.000.json
    2026-04-15 21:07:11,635 | INFO | Skipping weekly_record_707_2004-07-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,636 | INFO | Processing file: weekly_record_708_2004-07-12T00-00-00.000.json
    2026-04-15 21:07:11,658 | INFO | Skipping weekly_record_708_2004-07-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,658 | INFO | Processing file: weekly_record_709_2004-07-19T00-00-00.000.json
    2026-04-15 21:07:11,680 | INFO | Skipping weekly_record_709_2004-07-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,681 | INFO | Processing file: weekly_record_70_1992-04-20T00-00-00.000.json
    2026-04-15 21:07:11,707 | INFO | Skipping weekly_record_70_1992-04-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,708 | INFO | Processing file: weekly_record_710_2004-07-26T00-00-00.000.json
    2026-04-15 21:07:11,726 | INFO | Skipping weekly_record_710_2004-07-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,727 | INFO | Processing file: weekly_record_711_2004-08-02T00-00-00.000.json
    2026-04-15 21:07:11,743 | INFO | Skipping weekly_record_711_2004-08-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,744 | INFO | Processing file: weekly_record_712_2004-08-09T00-00-00.000.json
    2026-04-15 21:07:11,761 | INFO | Skipping weekly_record_712_2004-08-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,761 | INFO | Processing file: weekly_record_713_2004-08-16T00-00-00.000.json
    2026-04-15 21:07:11,792 | INFO | Skipping weekly_record_713_2004-08-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,793 | INFO | Processing file: weekly_record_714_2004-08-23T00-00-00.000.json
    2026-04-15 21:07:11,808 | INFO | Skipping weekly_record_714_2004-08-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,808 | INFO | Processing file: weekly_record_715_2004-08-30T00-00-00.000.json
    2026-04-15 21:07:11,857 | INFO | Skipping weekly_record_715_2004-08-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,868 | INFO | Processing file: weekly_record_716_2004-09-06T00-00-00.000.json
    2026-04-15 21:07:11,888 | INFO | Skipping weekly_record_716_2004-09-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,888 | INFO | Processing file: weekly_record_717_2004-09-13T00-00-00.000.json
    2026-04-15 21:07:11,916 | INFO | Skipping weekly_record_717_2004-09-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,916 | INFO | Processing file: weekly_record_718_2004-09-20T00-00-00.000.json
    2026-04-15 21:07:11,934 | INFO | Skipping weekly_record_718_2004-09-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,935 | INFO | Processing file: weekly_record_719_2004-09-27T00-00-00.000.json
    2026-04-15 21:07:11,960 | INFO | Skipping weekly_record_719_2004-09-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,960 | INFO | Processing file: weekly_record_71_1992-04-27T00-00-00.000.json
    2026-04-15 21:07:11,978 | INFO | Skipping weekly_record_71_1992-04-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:11,978 | INFO | Processing file: weekly_record_720_2004-10-04T00-00-00.000.json
    2026-04-15 21:07:12,004 | INFO | Skipping weekly_record_720_2004-10-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,005 | INFO | Processing file: weekly_record_721_2004-10-11T00-00-00.000.json
    2026-04-15 21:07:12,041 | INFO | Skipping weekly_record_721_2004-10-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,041 | INFO | Processing file: weekly_record_722_2004-10-18T00-00-00.000.json
    2026-04-15 21:07:12,068 | INFO | Skipping weekly_record_722_2004-10-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,068 | INFO | Processing file: weekly_record_723_2004-10-25T00-00-00.000.json
    2026-04-15 21:07:12,085 | INFO | Skipping weekly_record_723_2004-10-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,086 | INFO | Processing file: weekly_record_724_2004-11-01T00-00-00.000.json
    2026-04-15 21:07:12,113 | INFO | Skipping weekly_record_724_2004-11-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,113 | INFO | Processing file: weekly_record_725_2004-11-08T00-00-00.000.json
    2026-04-15 21:07:12,132 | INFO | Skipping weekly_record_725_2004-11-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,132 | INFO | Processing file: weekly_record_726_2004-11-15T00-00-00.000.json
    2026-04-15 21:07:12,171 | INFO | Skipping weekly_record_726_2004-11-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,172 | INFO | Processing file: weekly_record_727_2004-11-22T00-00-00.000.json
    2026-04-15 21:07:12,189 | INFO | Skipping weekly_record_727_2004-11-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,191 | INFO | Processing file: weekly_record_728_2004-11-29T00-00-00.000.json
    2026-04-15 21:07:12,214 | INFO | Skipping weekly_record_728_2004-11-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,215 | INFO | Processing file: weekly_record_729_2004-12-06T00-00-00.000.json
    2026-04-15 21:07:12,235 | INFO | Skipping weekly_record_729_2004-12-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,236 | INFO | Processing file: weekly_record_72_1992-05-04T00-00-00.000.json
    2026-04-15 21:07:12,262 | INFO | Skipping weekly_record_72_1992-05-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,263 | INFO | Processing file: weekly_record_730_2004-12-13T00-00-00.000.json
    2026-04-15 21:07:12,281 | INFO | Skipping weekly_record_730_2004-12-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,282 | INFO | Processing file: weekly_record_731_2004-12-20T00-00-00.000.json
    2026-04-15 21:07:12,308 | INFO | Skipping weekly_record_731_2004-12-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,308 | INFO | Processing file: weekly_record_732_2004-12-27T00-00-00.000.json
    2026-04-15 21:07:12,326 | INFO | Skipping weekly_record_732_2004-12-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,362 | INFO | Processing file: weekly_record_733_2005-01-03T00-00-00.000.json
    2026-04-15 21:07:12,383 | INFO | Skipping weekly_record_733_2005-01-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,385 | INFO | Processing file: weekly_record_734_2005-01-10T00-00-00.000.json
    2026-04-15 21:07:12,406 | INFO | Skipping weekly_record_734_2005-01-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,407 | INFO | Processing file: weekly_record_735_2005-01-17T00-00-00.000.json
    2026-04-15 21:07:12,443 | INFO | Skipping weekly_record_735_2005-01-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,444 | INFO | Processing file: weekly_record_736_2005-01-24T00-00-00.000.json
    2026-04-15 21:07:12,463 | INFO | Skipping weekly_record_736_2005-01-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,464 | INFO | Processing file: weekly_record_737_2005-01-31T00-00-00.000.json
    2026-04-15 21:07:12,490 | INFO | Skipping weekly_record_737_2005-01-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,490 | INFO | Processing file: weekly_record_738_2005-02-07T00-00-00.000.json
    2026-04-15 21:07:12,511 | INFO | Skipping weekly_record_738_2005-02-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,512 | INFO | Processing file: weekly_record_739_2005-02-14T00-00-00.000.json
    2026-04-15 21:07:12,538 | INFO | Skipping weekly_record_739_2005-02-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,538 | INFO | Processing file: weekly_record_73_1992-05-11T00-00-00.000.json
    2026-04-15 21:07:12,560 | INFO | Skipping weekly_record_73_1992-05-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,560 | INFO | Processing file: weekly_record_740_2005-02-21T00-00-00.000.json
    2026-04-15 21:07:12,587 | INFO | Skipping weekly_record_740_2005-02-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,587 | INFO | Processing file: weekly_record_741_2005-02-28T00-00-00.000.json
    2026-04-15 21:07:12,610 | INFO | Skipping weekly_record_741_2005-02-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,610 | INFO | Processing file: weekly_record_742_2005-03-07T00-00-00.000.json
    2026-04-15 21:07:12,629 | INFO | Skipping weekly_record_742_2005-03-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,629 | INFO | Processing file: weekly_record_743_2005-03-14T00-00-00.000.json
    2026-04-15 21:07:12,648 | INFO | Skipping weekly_record_743_2005-03-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,648 | INFO | Processing file: weekly_record_744_2005-03-21T00-00-00.000.json
    2026-04-15 21:07:12,669 | INFO | Skipping weekly_record_744_2005-03-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,670 | INFO | Processing file: weekly_record_745_2005-03-28T00-00-00.000.json
    2026-04-15 21:07:12,688 | INFO | Skipping weekly_record_745_2005-03-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,691 | INFO | Processing file: weekly_record_746_2005-04-04T00-00-00.000.json
    2026-04-15 21:07:12,713 | INFO | Skipping weekly_record_746_2005-04-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,713 | INFO | Processing file: weekly_record_747_2005-04-11T00-00-00.000.json
    2026-04-15 21:07:12,742 | INFO | Skipping weekly_record_747_2005-04-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,742 | INFO | Processing file: weekly_record_748_2005-04-18T00-00-00.000.json
    2026-04-15 21:07:12,803 | INFO | Skipping weekly_record_748_2005-04-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,804 | INFO | Processing file: weekly_record_749_2005-04-25T00-00-00.000.json
    2026-04-15 21:07:12,834 | INFO | Skipping weekly_record_749_2005-04-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,835 | INFO | Processing file: weekly_record_74_1992-05-18T00-00-00.000.json
    2026-04-15 21:07:12,862 | INFO | Skipping weekly_record_74_1992-05-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,863 | INFO | Processing file: weekly_record_750_2005-05-02T00-00-00.000.json
    2026-04-15 21:07:12,882 | INFO | Skipping weekly_record_750_2005-05-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,883 | INFO | Processing file: weekly_record_751_2005-05-09T00-00-00.000.json
    2026-04-15 21:07:12,900 | INFO | Skipping weekly_record_751_2005-05-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,900 | INFO | Processing file: weekly_record_752_2005-05-16T00-00-00.000.json
    2026-04-15 21:07:12,925 | INFO | Skipping weekly_record_752_2005-05-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,926 | INFO | Processing file: weekly_record_753_2005-05-23T00-00-00.000.json
    2026-04-15 21:07:12,944 | INFO | Skipping weekly_record_753_2005-05-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,945 | INFO | Processing file: weekly_record_754_2005-05-30T00-00-00.000.json
    2026-04-15 21:07:12,960 | INFO | Skipping weekly_record_754_2005-05-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,961 | INFO | Processing file: weekly_record_755_2005-06-06T00-00-00.000.json
    2026-04-15 21:07:12,985 | INFO | Skipping weekly_record_755_2005-06-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:12,985 | INFO | Processing file: weekly_record_756_2005-06-13T00-00-00.000.json
    2026-04-15 21:07:13,004 | INFO | Skipping weekly_record_756_2005-06-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,004 | INFO | Processing file: weekly_record_757_2005-06-20T00-00-00.000.json
    2026-04-15 21:07:13,033 | INFO | Skipping weekly_record_757_2005-06-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,034 | INFO | Processing file: weekly_record_758_2005-06-27T00-00-00.000.json
    2026-04-15 21:07:13,062 | INFO | Skipping weekly_record_758_2005-06-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,062 | INFO | Processing file: weekly_record_759_2005-07-04T00-00-00.000.json
    2026-04-15 21:07:13,078 | INFO | Skipping weekly_record_759_2005-07-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,079 | INFO | Processing file: weekly_record_75_1992-05-25T00-00-00.000.json
    2026-04-15 21:07:13,106 | INFO | Skipping weekly_record_75_1992-05-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,107 | INFO | Processing file: weekly_record_760_2005-07-11T00-00-00.000.json
    2026-04-15 21:07:13,127 | INFO | Skipping weekly_record_760_2005-07-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,128 | INFO | Processing file: weekly_record_761_2005-07-18T00-00-00.000.json
    2026-04-15 21:07:13,157 | INFO | Skipping weekly_record_761_2005-07-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,157 | INFO | Processing file: weekly_record_762_2005-07-25T00-00-00.000.json
    2026-04-15 21:07:13,177 | INFO | Skipping weekly_record_762_2005-07-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,177 | INFO | Processing file: weekly_record_763_2005-08-01T00-00-00.000.json
    2026-04-15 21:07:13,203 | INFO | Skipping weekly_record_763_2005-08-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,246 | INFO | Processing file: weekly_record_764_2005-08-08T00-00-00.000.json
    2026-04-15 21:07:13,266 | INFO | Skipping weekly_record_764_2005-08-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,267 | INFO | Processing file: weekly_record_765_2005-08-15T00-00-00.000.json
    2026-04-15 21:07:13,285 | INFO | Skipping weekly_record_765_2005-08-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,286 | INFO | Processing file: weekly_record_766_2005-08-22T00-00-00.000.json
    2026-04-15 21:07:13,311 | INFO | Skipping weekly_record_766_2005-08-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,311 | INFO | Processing file: weekly_record_767_2005-08-29T00-00-00.000.json
    2026-04-15 21:07:13,327 | INFO | Skipping weekly_record_767_2005-08-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,327 | INFO | Processing file: weekly_record_768_2005-09-05T00-00-00.000.json
    2026-04-15 21:07:13,354 | INFO | Skipping weekly_record_768_2005-09-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,354 | INFO | Processing file: weekly_record_769_2005-09-12T00-00-00.000.json
    2026-04-15 21:07:13,371 | INFO | Skipping weekly_record_769_2005-09-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,372 | INFO | Processing file: weekly_record_76_1992-06-01T00-00-00.000.json
    2026-04-15 21:07:13,399 | INFO | Skipping weekly_record_76_1992-06-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,400 | INFO | Processing file: weekly_record_770_2005-09-19T00-00-00.000.json
    2026-04-15 21:07:13,416 | INFO | Skipping weekly_record_770_2005-09-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,417 | INFO | Processing file: weekly_record_771_2005-09-26T00-00-00.000.json
    2026-04-15 21:07:13,443 | INFO | Skipping weekly_record_771_2005-09-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,444 | INFO | Processing file: weekly_record_772_2005-10-03T00-00-00.000.json
    2026-04-15 21:07:13,464 | INFO | Skipping weekly_record_772_2005-10-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,465 | INFO | Processing file: weekly_record_773_2005-10-10T00-00-00.000.json
    2026-04-15 21:07:13,493 | INFO | Skipping weekly_record_773_2005-10-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,494 | INFO | Processing file: weekly_record_774_2005-10-17T00-00-00.000.json
    2026-04-15 21:07:13,516 | INFO | Skipping weekly_record_774_2005-10-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,517 | INFO | Processing file: weekly_record_775_2005-10-24T00-00-00.000.json
    2026-04-15 21:07:13,544 | INFO | Skipping weekly_record_775_2005-10-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,545 | INFO | Processing file: weekly_record_776_2005-10-31T00-00-00.000.json
    2026-04-15 21:07:13,574 | INFO | Skipping weekly_record_776_2005-10-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,575 | INFO | Processing file: weekly_record_777_2005-11-07T00-00-00.000.json
    2026-04-15 21:07:13,593 | INFO | Skipping weekly_record_777_2005-11-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,593 | INFO | Processing file: weekly_record_778_2005-11-14T00-00-00.000.json
    2026-04-15 21:07:13,614 | INFO | Skipping weekly_record_778_2005-11-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,615 | INFO | Processing file: weekly_record_779_2005-11-21T00-00-00.000.json
    2026-04-15 21:07:13,640 | INFO | Skipping weekly_record_779_2005-11-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,664 | INFO | Processing file: weekly_record_77_1992-06-08T00-00-00.000.json
    2026-04-15 21:07:13,691 | INFO | Skipping weekly_record_77_1992-06-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,692 | INFO | Processing file: weekly_record_780_2005-11-28T00-00-00.000.json
    2026-04-15 21:07:13,717 | INFO | Skipping weekly_record_780_2005-11-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,718 | INFO | Processing file: weekly_record_781_2005-12-05T00-00-00.000.json
    2026-04-15 21:07:13,745 | INFO | Skipping weekly_record_781_2005-12-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,746 | INFO | Processing file: weekly_record_782_2005-12-12T00-00-00.000.json
    2026-04-15 21:07:13,762 | INFO | Skipping weekly_record_782_2005-12-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,763 | INFO | Processing file: weekly_record_783_2005-12-19T00-00-00.000.json
    2026-04-15 21:07:13,789 | INFO | Skipping weekly_record_783_2005-12-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,789 | INFO | Processing file: weekly_record_784_2005-12-26T00-00-00.000.json
    2026-04-15 21:07:13,808 | INFO | Skipping weekly_record_784_2005-12-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,809 | INFO | Processing file: weekly_record_785_2006-01-02T00-00-00.000.json
    2026-04-15 21:07:13,835 | INFO | Skipping weekly_record_785_2006-01-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,836 | INFO | Processing file: weekly_record_786_2006-01-09T00-00-00.000.json
    2026-04-15 21:07:13,856 | INFO | Skipping weekly_record_786_2006-01-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,857 | INFO | Processing file: weekly_record_787_2006-01-16T00-00-00.000.json
    2026-04-15 21:07:13,876 | INFO | Skipping weekly_record_787_2006-01-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,876 | INFO | Processing file: weekly_record_788_2006-01-23T00-00-00.000.json
    2026-04-15 21:07:13,896 | INFO | Skipping weekly_record_788_2006-01-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,897 | INFO | Processing file: weekly_record_789_2006-01-30T00-00-00.000.json
    2026-04-15 21:07:13,921 | INFO | Skipping weekly_record_789_2006-01-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,922 | INFO | Processing file: weekly_record_78_1992-06-15T00-00-00.000.json
    2026-04-15 21:07:13,944 | INFO | Skipping weekly_record_78_1992-06-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,946 | INFO | Processing file: weekly_record_790_2006-02-06T00-00-00.000.json
    2026-04-15 21:07:13,969 | INFO | Skipping weekly_record_790_2006-02-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,970 | INFO | Processing file: weekly_record_791_2006-02-13T00-00-00.000.json
    2026-04-15 21:07:13,995 | INFO | Skipping weekly_record_791_2006-02-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:13,996 | INFO | Processing file: weekly_record_792_2006-02-20T00-00-00.000.json
    2026-04-15 21:07:14,013 | INFO | Skipping weekly_record_792_2006-02-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,013 | INFO | Processing file: weekly_record_793_2006-02-27T00-00-00.000.json
    2026-04-15 21:07:14,036 | INFO | Skipping weekly_record_793_2006-02-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,041 | INFO | Processing file: weekly_record_794_2006-03-06T00-00-00.000.json
    2026-04-15 21:07:14,068 | INFO | Skipping weekly_record_794_2006-03-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,082 | INFO | Processing file: weekly_record_795_2006-03-13T00-00-00.000.json
    2026-04-15 21:07:14,106 | INFO | Skipping weekly_record_795_2006-03-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,108 | INFO | Processing file: weekly_record_796_2006-03-20T00-00-00.000.json
    2026-04-15 21:07:14,120 | INFO | Skipping weekly_record_796_2006-03-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,120 | INFO | Processing file: weekly_record_797_2006-03-27T00-00-00.000.json
    2026-04-15 21:07:14,145 | INFO | Skipping weekly_record_797_2006-03-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,145 | INFO | Processing file: weekly_record_798_2006-04-03T00-00-00.000.json
    2026-04-15 21:07:14,160 | INFO | Skipping weekly_record_798_2006-04-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,161 | INFO | Processing file: weekly_record_799_2006-04-10T00-00-00.000.json
    2026-04-15 21:07:14,180 | INFO | Skipping weekly_record_799_2006-04-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,180 | INFO | Processing file: weekly_record_79_1992-06-22T00-00-00.000.json
    2026-04-15 21:07:14,203 | INFO | Skipping weekly_record_79_1992-06-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,204 | INFO | Processing file: weekly_record_7_1991-02-04T00-00-00.000.json
    2026-04-15 21:07:14,222 | INFO | Skipping weekly_record_7_1991-02-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,224 | INFO | Processing file: weekly_record_800_2006-04-17T00-00-00.000.json
    2026-04-15 21:07:14,247 | INFO | Skipping weekly_record_800_2006-04-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,248 | INFO | Processing file: weekly_record_801_2006-04-24T00-00-00.000.json
    2026-04-15 21:07:14,265 | INFO | Skipping weekly_record_801_2006-04-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,266 | INFO | Processing file: weekly_record_802_2006-05-01T00-00-00.000.json
    2026-04-15 21:07:14,292 | INFO | Skipping weekly_record_802_2006-05-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,293 | INFO | Processing file: weekly_record_803_2006-05-08T00-00-00.000.json
    2026-04-15 21:07:14,311 | INFO | Skipping weekly_record_803_2006-05-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,312 | INFO | Processing file: weekly_record_804_2006-05-15T00-00-00.000.json
    2026-04-15 21:07:14,338 | INFO | Skipping weekly_record_804_2006-05-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,339 | INFO | Processing file: weekly_record_805_2006-05-22T00-00-00.000.json
    2026-04-15 21:07:14,355 | INFO | Skipping weekly_record_805_2006-05-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,356 | INFO | Processing file: weekly_record_806_2006-05-29T00-00-00.000.json
    2026-04-15 21:07:14,385 | INFO | Skipping weekly_record_806_2006-05-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,386 | INFO | Processing file: weekly_record_807_2006-06-05T00-00-00.000.json
    2026-04-15 21:07:14,404 | INFO | Skipping weekly_record_807_2006-06-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,404 | INFO | Processing file: weekly_record_808_2006-06-12T00-00-00.000.json
    2026-04-15 21:07:14,428 | INFO | Skipping weekly_record_808_2006-06-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,428 | INFO | Processing file: weekly_record_809_2006-06-19T00-00-00.000.json
    2026-04-15 21:07:14,446 | INFO | Skipping weekly_record_809_2006-06-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,447 | INFO | Processing file: weekly_record_80_1992-06-29T00-00-00.000.json
    2026-04-15 21:07:14,474 | INFO | Skipping weekly_record_80_1992-06-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,477 | INFO | Processing file: weekly_record_810_2006-06-26T00-00-00.000.json
    2026-04-15 21:07:14,508 | INFO | Skipping weekly_record_810_2006-06-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,510 | INFO | Processing file: weekly_record_811_2006-07-03T00-00-00.000.json
    2026-04-15 21:07:14,535 | INFO | Skipping weekly_record_811_2006-07-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,536 | INFO | Processing file: weekly_record_812_2006-07-10T00-00-00.000.json
    2026-04-15 21:07:14,556 | INFO | Skipping weekly_record_812_2006-07-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,557 | INFO | Processing file: weekly_record_813_2006-07-17T00-00-00.000.json
    2026-04-15 21:07:14,580 | INFO | Skipping weekly_record_813_2006-07-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,580 | INFO | Processing file: weekly_record_814_2006-07-24T00-00-00.000.json
    2026-04-15 21:07:14,597 | INFO | Skipping weekly_record_814_2006-07-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,598 | INFO | Processing file: weekly_record_815_2006-07-31T00-00-00.000.json
    2026-04-15 21:07:14,627 | INFO | Skipping weekly_record_815_2006-07-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,627 | INFO | Processing file: weekly_record_816_2006-08-07T00-00-00.000.json
    2026-04-15 21:07:14,653 | INFO | Skipping weekly_record_816_2006-08-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,653 | INFO | Processing file: weekly_record_817_2006-08-14T00-00-00.000.json
    2026-04-15 21:07:14,672 | INFO | Skipping weekly_record_817_2006-08-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,672 | INFO | Processing file: weekly_record_818_2006-08-21T00-00-00.000.json
    2026-04-15 21:07:14,699 | INFO | Skipping weekly_record_818_2006-08-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,699 | INFO | Processing file: weekly_record_819_2006-08-28T00-00-00.000.json
    2026-04-15 21:07:14,715 | INFO | Skipping weekly_record_819_2006-08-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,716 | INFO | Processing file: weekly_record_81_1992-07-06T00-00-00.000.json
    2026-04-15 21:07:14,743 | INFO | Skipping weekly_record_81_1992-07-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,744 | INFO | Processing file: weekly_record_820_2006-09-04T00-00-00.000.json
    2026-04-15 21:07:14,759 | INFO | Skipping weekly_record_820_2006-09-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,760 | INFO | Processing file: weekly_record_821_2006-09-11T00-00-00.000.json
    2026-04-15 21:07:14,778 | INFO | Skipping weekly_record_821_2006-09-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,779 | INFO | Processing file: weekly_record_822_2006-09-18T00-00-00.000.json
    2026-04-15 21:07:14,805 | INFO | Skipping weekly_record_822_2006-09-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,805 | INFO | Processing file: weekly_record_823_2006-09-25T00-00-00.000.json
    2026-04-15 21:07:14,824 | INFO | Skipping weekly_record_823_2006-09-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,825 | INFO | Processing file: weekly_record_824_2006-10-02T00-00-00.000.json
    2026-04-15 21:07:14,851 | INFO | Skipping weekly_record_824_2006-10-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,851 | INFO | Processing file: weekly_record_825_2006-10-09T00-00-00.000.json
    2026-04-15 21:07:14,867 | INFO | Skipping weekly_record_825_2006-10-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,868 | INFO | Processing file: weekly_record_826_2006-10-16T00-00-00.000.json
    2026-04-15 21:07:14,899 | INFO | Skipping weekly_record_826_2006-10-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,903 | INFO | Processing file: weekly_record_827_2006-10-23T00-00-00.000.json
    2026-04-15 21:07:14,925 | INFO | Skipping weekly_record_827_2006-10-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,926 | INFO | Processing file: weekly_record_828_2006-10-30T00-00-00.000.json
    2026-04-15 21:07:14,953 | INFO | Skipping weekly_record_828_2006-10-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,954 | INFO | Processing file: weekly_record_829_2006-11-06T00-00-00.000.json
    2026-04-15 21:07:14,980 | INFO | Skipping weekly_record_829_2006-11-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:14,980 | INFO | Processing file: weekly_record_82_1992-07-13T00-00-00.000.json
    2026-04-15 21:07:15,003 | INFO | Skipping weekly_record_82_1992-07-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,003 | INFO | Processing file: weekly_record_830_2006-11-13T00-00-00.000.json
    2026-04-15 21:07:15,020 | INFO | Skipping weekly_record_830_2006-11-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,021 | INFO | Processing file: weekly_record_831_2006-11-20T00-00-00.000.json
    2026-04-15 21:07:15,047 | INFO | Skipping weekly_record_831_2006-11-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,048 | INFO | Processing file: weekly_record_832_2006-11-27T00-00-00.000.json
    2026-04-15 21:07:15,067 | INFO | Skipping weekly_record_832_2006-11-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,068 | INFO | Processing file: weekly_record_833_2006-12-04T00-00-00.000.json
    2026-04-15 21:07:15,095 | INFO | Skipping weekly_record_833_2006-12-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,095 | INFO | Processing file: weekly_record_834_2006-12-11T00-00-00.000.json
    2026-04-15 21:07:15,119 | INFO | Skipping weekly_record_834_2006-12-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,119 | INFO | Processing file: weekly_record_835_2006-12-18T00-00-00.000.json
    2026-04-15 21:07:15,145 | INFO | Skipping weekly_record_835_2006-12-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,146 | INFO | Processing file: weekly_record_836_2006-12-25T00-00-00.000.json
    2026-04-15 21:07:15,169 | INFO | Skipping weekly_record_836_2006-12-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,170 | INFO | Processing file: weekly_record_837_2007-01-01T00-00-00.000.json
    2026-04-15 21:07:15,195 | INFO | Skipping weekly_record_837_2007-01-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,196 | INFO | Processing file: weekly_record_838_2007-01-08T00-00-00.000.json
    2026-04-15 21:07:15,211 | INFO | Skipping weekly_record_838_2007-01-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,212 | INFO | Processing file: weekly_record_839_2007-01-15T00-00-00.000.json
    2026-04-15 21:07:15,238 | INFO | Skipping weekly_record_839_2007-01-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,239 | INFO | Processing file: weekly_record_83_1992-07-20T00-00-00.000.json
    2026-04-15 21:07:15,255 | INFO | Skipping weekly_record_83_1992-07-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,256 | INFO | Processing file: weekly_record_840_2007-01-22T00-00-00.000.json
    2026-04-15 21:07:15,285 | INFO | Skipping weekly_record_840_2007-01-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,286 | INFO | Processing file: weekly_record_841_2007-01-29T00-00-00.000.json
    2026-04-15 21:07:15,306 | INFO | Skipping weekly_record_841_2007-01-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,307 | INFO | Processing file: weekly_record_842_2007-02-05T00-00-00.000.json
    2026-04-15 21:07:15,332 | INFO | Skipping weekly_record_842_2007-02-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,332 | INFO | Processing file: weekly_record_843_2007-02-12T00-00-00.000.json
    2026-04-15 21:07:15,390 | INFO | Skipping weekly_record_843_2007-02-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,392 | INFO | Processing file: weekly_record_844_2007-02-19T00-00-00.000.json
    2026-04-15 21:07:15,418 | INFO | Skipping weekly_record_844_2007-02-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,419 | INFO | Processing file: weekly_record_845_2007-02-26T00-00-00.000.json
    2026-04-15 21:07:15,436 | INFO | Skipping weekly_record_845_2007-02-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,437 | INFO | Processing file: weekly_record_846_2007-03-05T00-00-00.000.json
    2026-04-15 21:07:15,462 | INFO | Skipping weekly_record_846_2007-03-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,463 | INFO | Processing file: weekly_record_847_2007-03-12T00-00-00.000.json
    2026-04-15 21:07:15,480 | INFO | Skipping weekly_record_847_2007-03-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,480 | INFO | Processing file: weekly_record_848_2007-03-19T00-00-00.000.json
    2026-04-15 21:07:15,795 | INFO | Skipping weekly_record_848_2007-03-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,796 | INFO | Processing file: weekly_record_849_2007-03-26T00-00-00.000.json
    2026-04-15 21:07:15,826 | INFO | Skipping weekly_record_849_2007-03-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,827 | INFO | Processing file: weekly_record_84_1992-07-27T00-00-00.000.json
    2026-04-15 21:07:15,845 | INFO | Skipping weekly_record_84_1992-07-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,846 | INFO | Processing file: weekly_record_850_2007-04-02T00-00-00.000.json
    2026-04-15 21:07:15,869 | INFO | Skipping weekly_record_850_2007-04-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,870 | INFO | Processing file: weekly_record_851_2007-04-09T00-00-00.000.json
    2026-04-15 21:07:15,895 | INFO | Skipping weekly_record_851_2007-04-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,896 | INFO | Processing file: weekly_record_852_2007-04-16T00-00-00.000.json
    2026-04-15 21:07:15,914 | INFO | Skipping weekly_record_852_2007-04-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,915 | INFO | Processing file: weekly_record_853_2007-04-23T00-00-00.000.json
    2026-04-15 21:07:15,945 | INFO | Skipping weekly_record_853_2007-04-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,946 | INFO | Processing file: weekly_record_854_2007-04-30T00-00-00.000.json
    2026-04-15 21:07:15,964 | INFO | Skipping weekly_record_854_2007-04-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,964 | INFO | Processing file: weekly_record_855_2007-05-07T00-00-00.000.json
    2026-04-15 21:07:15,981 | INFO | Skipping weekly_record_855_2007-05-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:15,981 | INFO | Processing file: weekly_record_856_2007-05-14T00-00-00.000.json
    2026-04-15 21:07:16,009 | INFO | Skipping weekly_record_856_2007-05-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,009 | INFO | Processing file: weekly_record_857_2007-05-21T00-00-00.000.json
    2026-04-15 21:07:16,026 | INFO | Skipping weekly_record_857_2007-05-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,026 | INFO | Processing file: weekly_record_858_2007-05-28T00-00-00.000.json
    2026-04-15 21:07:16,074 | INFO | Skipping weekly_record_858_2007-05-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,082 | INFO | Processing file: weekly_record_859_2007-06-04T00-00-00.000.json
    2026-04-15 21:07:16,105 | INFO | Skipping weekly_record_859_2007-06-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,106 | INFO | Processing file: weekly_record_85_1992-08-03T00-00-00.000.json
    2026-04-15 21:07:16,128 | INFO | Skipping weekly_record_85_1992-08-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,128 | INFO | Processing file: weekly_record_860_2007-06-11T00-00-00.000.json
    2026-04-15 21:07:16,146 | INFO | Skipping weekly_record_860_2007-06-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,147 | INFO | Processing file: weekly_record_861_2007-06-18T00-00-00.000.json
    2026-04-15 21:07:16,172 | INFO | Skipping weekly_record_861_2007-06-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,173 | INFO | Processing file: weekly_record_862_2007-06-25T00-00-00.000.json
    2026-04-15 21:07:16,195 | INFO | Skipping weekly_record_862_2007-06-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,196 | INFO | Processing file: weekly_record_863_2007-07-02T00-00-00.000.json
    2026-04-15 21:07:16,222 | INFO | Skipping weekly_record_863_2007-07-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,223 | INFO | Processing file: weekly_record_864_2007-07-09T00-00-00.000.json
    2026-04-15 21:07:16,240 | INFO | Skipping weekly_record_864_2007-07-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,240 | INFO | Processing file: weekly_record_865_2007-07-16T00-00-00.000.json
    2026-04-15 21:07:16,259 | INFO | Skipping weekly_record_865_2007-07-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,261 | INFO | Processing file: weekly_record_866_2007-07-23T00-00-00.000.json
    2026-04-15 21:07:16,285 | INFO | Skipping weekly_record_866_2007-07-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,286 | INFO | Processing file: weekly_record_867_2007-07-30T00-00-00.000.json
    2026-04-15 21:07:16,302 | INFO | Skipping weekly_record_867_2007-07-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,303 | INFO | Processing file: weekly_record_868_2007-08-06T00-00-00.000.json
    2026-04-15 21:07:16,321 | INFO | Skipping weekly_record_868_2007-08-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,321 | INFO | Processing file: weekly_record_869_2007-08-13T00-00-00.000.json
    2026-04-15 21:07:16,347 | INFO | Skipping weekly_record_869_2007-08-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,348 | INFO | Processing file: weekly_record_86_1992-08-10T00-00-00.000.json
    2026-04-15 21:07:16,361 | INFO | Skipping weekly_record_86_1992-08-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,362 | INFO | Processing file: weekly_record_870_2007-08-20T00-00-00.000.json
    2026-04-15 21:07:16,388 | INFO | Skipping weekly_record_870_2007-08-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,388 | INFO | Processing file: weekly_record_871_2007-08-27T00-00-00.000.json
    2026-04-15 21:07:16,404 | INFO | Skipping weekly_record_871_2007-08-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,406 | INFO | Processing file: weekly_record_872_2007-09-03T00-00-00.000.json
    2026-04-15 21:07:16,433 | INFO | Skipping weekly_record_872_2007-09-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,434 | INFO | Processing file: weekly_record_873_2007-09-10T00-00-00.000.json
    2026-04-15 21:07:16,450 | INFO | Skipping weekly_record_873_2007-09-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,494 | INFO | Processing file: weekly_record_874_2007-09-17T00-00-00.000.json
    2026-04-15 21:07:16,525 | INFO | Skipping weekly_record_874_2007-09-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,526 | INFO | Processing file: weekly_record_875_2007-09-24T00-00-00.000.json
    2026-04-15 21:07:16,543 | INFO | Skipping weekly_record_875_2007-09-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,543 | INFO | Processing file: weekly_record_876_2007-10-01T00-00-00.000.json
    2026-04-15 21:07:16,569 | INFO | Skipping weekly_record_876_2007-10-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,570 | INFO | Processing file: weekly_record_877_2007-10-08T00-00-00.000.json
    2026-04-15 21:07:16,616 | INFO | Skipping weekly_record_877_2007-10-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,616 | INFO | Processing file: weekly_record_878_2007-10-15T00-00-00.000.json
    2026-04-15 21:07:16,642 | INFO | Skipping weekly_record_878_2007-10-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,643 | INFO | Processing file: weekly_record_879_2007-10-22T00-00-00.000.json
    2026-04-15 21:07:16,662 | INFO | Skipping weekly_record_879_2007-10-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,663 | INFO | Processing file: weekly_record_87_1992-08-17T00-00-00.000.json
    2026-04-15 21:07:16,683 | INFO | Skipping weekly_record_87_1992-08-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,683 | INFO | Processing file: weekly_record_880_2007-10-29T00-00-00.000.json
    2026-04-15 21:07:16,719 | INFO | Skipping weekly_record_880_2007-10-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,719 | INFO | Processing file: weekly_record_881_2007-11-05T00-00-00.000.json
    2026-04-15 21:07:16,747 | INFO | Skipping weekly_record_881_2007-11-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,748 | INFO | Processing file: weekly_record_882_2007-11-12T00-00-00.000.json
    2026-04-15 21:07:16,778 | INFO | Skipping weekly_record_882_2007-11-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,778 | INFO | Processing file: weekly_record_883_2007-11-19T00-00-00.000.json
    2026-04-15 21:07:16,793 | INFO | Skipping weekly_record_883_2007-11-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,794 | INFO | Processing file: weekly_record_884_2007-11-26T00-00-00.000.json
    2026-04-15 21:07:16,820 | INFO | Skipping weekly_record_884_2007-11-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,821 | INFO | Processing file: weekly_record_885_2007-12-03T00-00-00.000.json
    2026-04-15 21:07:16,838 | INFO | Skipping weekly_record_885_2007-12-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,839 | INFO | Processing file: weekly_record_886_2007-12-10T00-00-00.000.json
    2026-04-15 21:07:16,858 | INFO | Skipping weekly_record_886_2007-12-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,859 | INFO | Processing file: weekly_record_887_2007-12-17T00-00-00.000.json
    2026-04-15 21:07:16,875 | INFO | Skipping weekly_record_887_2007-12-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,876 | INFO | Processing file: weekly_record_888_2007-12-24T00-00-00.000.json
    2026-04-15 21:07:16,910 | INFO | Skipping weekly_record_888_2007-12-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,922 | INFO | Processing file: weekly_record_889_2007-12-31T00-00-00.000.json
    2026-04-15 21:07:16,964 | INFO | Skipping weekly_record_889_2007-12-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,968 | INFO | Processing file: weekly_record_88_1992-08-24T00-00-00.000.json
    2026-04-15 21:07:16,986 | INFO | Skipping weekly_record_88_1992-08-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:16,987 | INFO | Processing file: weekly_record_890_2008-01-07T00-00-00.000.json
    2026-04-15 21:07:17,003 | INFO | Skipping weekly_record_890_2008-01-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,004 | INFO | Processing file: weekly_record_891_2008-01-14T00-00-00.000.json
    2026-04-15 21:07:17,030 | INFO | Skipping weekly_record_891_2008-01-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,031 | INFO | Processing file: weekly_record_892_2008-01-21T00-00-00.000.json
    2026-04-15 21:07:17,049 | INFO | Skipping weekly_record_892_2008-01-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,052 | INFO | Processing file: weekly_record_893_2008-01-28T00-00-00.000.json
    2026-04-15 21:07:17,081 | INFO | Skipping weekly_record_893_2008-01-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,081 | INFO | Processing file: weekly_record_894_2008-02-04T00-00-00.000.json
    2026-04-15 21:07:17,103 | INFO | Skipping weekly_record_894_2008-02-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,104 | INFO | Processing file: weekly_record_895_2008-02-11T00-00-00.000.json
    2026-04-15 21:07:17,129 | INFO | Skipping weekly_record_895_2008-02-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,129 | INFO | Processing file: weekly_record_896_2008-02-18T00-00-00.000.json
    2026-04-15 21:07:17,149 | INFO | Skipping weekly_record_896_2008-02-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,149 | INFO | Processing file: weekly_record_897_2008-02-25T00-00-00.000.json
    2026-04-15 21:07:17,173 | INFO | Skipping weekly_record_897_2008-02-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,174 | INFO | Processing file: weekly_record_898_2008-03-03T00-00-00.000.json
    2026-04-15 21:07:17,195 | INFO | Skipping weekly_record_898_2008-03-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,196 | INFO | Processing file: weekly_record_899_2008-03-10T00-00-00.000.json
    2026-04-15 21:07:17,213 | INFO | Skipping weekly_record_899_2008-03-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,213 | INFO | Processing file: weekly_record_89_1992-08-31T00-00-00.000.json
    2026-04-15 21:07:17,239 | INFO | Skipping weekly_record_89_1992-08-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,239 | INFO | Processing file: weekly_record_8_1991-02-11T00-00-00.000.json
    2026-04-15 21:07:17,259 | INFO | Skipping weekly_record_8_1991-02-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,260 | INFO | Processing file: weekly_record_900_2008-03-17T00-00-00.000.json
    2026-04-15 21:07:17,289 | INFO | Skipping weekly_record_900_2008-03-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,290 | INFO | Processing file: weekly_record_901_2008-03-24T00-00-00.000.json
    2026-04-15 21:07:17,308 | INFO | Skipping weekly_record_901_2008-03-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,310 | INFO | Processing file: weekly_record_902_2008-03-31T00-00-00.000.json
    2026-04-15 21:07:17,333 | INFO | Skipping weekly_record_902_2008-03-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,356 | INFO | Processing file: weekly_record_903_2008-04-07T00-00-00.000.json
    2026-04-15 21:07:17,377 | INFO | Skipping weekly_record_903_2008-04-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,378 | INFO | Processing file: weekly_record_904_2008-04-14T00-00-00.000.json
    2026-04-15 21:07:17,405 | INFO | Skipping weekly_record_904_2008-04-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,406 | INFO | Processing file: weekly_record_905_2008-04-21T00-00-00.000.json
    2026-04-15 21:07:17,422 | INFO | Skipping weekly_record_905_2008-04-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,423 | INFO | Processing file: weekly_record_906_2008-04-28T00-00-00.000.json
    2026-04-15 21:07:17,449 | INFO | Skipping weekly_record_906_2008-04-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,450 | INFO | Processing file: weekly_record_907_2008-05-05T00-00-00.000.json
    2026-04-15 21:07:17,469 | INFO | Skipping weekly_record_907_2008-05-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,470 | INFO | Processing file: weekly_record_908_2008-05-12T00-00-00.000.json
    2026-04-15 21:07:17,495 | INFO | Skipping weekly_record_908_2008-05-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,496 | INFO | Processing file: weekly_record_909_2008-05-19T00-00-00.000.json
    2026-04-15 21:07:17,522 | INFO | Skipping weekly_record_909_2008-05-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,522 | INFO | Processing file: weekly_record_90_1992-09-07T00-00-00.000.json
    2026-04-15 21:07:17,541 | INFO | Skipping weekly_record_90_1992-09-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,541 | INFO | Processing file: weekly_record_910_2008-05-26T00-00-00.000.json
    2026-04-15 21:07:17,567 | INFO | Skipping weekly_record_910_2008-05-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,568 | INFO | Processing file: weekly_record_911_2008-06-02T00-00-00.000.json
    2026-04-15 21:07:17,589 | INFO | Skipping weekly_record_911_2008-06-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,590 | INFO | Processing file: weekly_record_912_2008-06-09T00-00-00.000.json
    2026-04-15 21:07:17,612 | INFO | Skipping weekly_record_912_2008-06-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,613 | INFO | Processing file: weekly_record_913_2008-06-16T00-00-00.000.json
    2026-04-15 21:07:17,632 | INFO | Skipping weekly_record_913_2008-06-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,632 | INFO | Processing file: weekly_record_914_2008-06-23T00-00-00.000.json
    2026-04-15 21:07:17,658 | INFO | Skipping weekly_record_914_2008-06-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,658 | INFO | Processing file: weekly_record_915_2008-06-30T00-00-00.000.json
    2026-04-15 21:07:17,679 | INFO | Skipping weekly_record_915_2008-06-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,679 | INFO | Processing file: weekly_record_916_2008-07-07T00-00-00.000.json
    2026-04-15 21:07:17,706 | INFO | Skipping weekly_record_916_2008-07-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,707 | INFO | Processing file: weekly_record_917_2008-07-14T00-00-00.000.json
    2026-04-15 21:07:17,725 | INFO | Skipping weekly_record_917_2008-07-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,725 | INFO | Processing file: weekly_record_918_2008-07-21T00-00-00.000.json
    2026-04-15 21:07:17,760 | INFO | Skipping weekly_record_918_2008-07-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,769 | INFO | Processing file: weekly_record_919_2008-07-28T00-00-00.000.json
    2026-04-15 21:07:17,787 | INFO | Skipping weekly_record_919_2008-07-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,787 | INFO | Processing file: weekly_record_91_1992-09-14T00-00-00.000.json
    2026-04-15 21:07:17,813 | INFO | Skipping weekly_record_91_1992-09-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,813 | INFO | Processing file: weekly_record_920_2008-08-04T00-00-00.000.json
    2026-04-15 21:07:17,831 | INFO | Skipping weekly_record_920_2008-08-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,832 | INFO | Processing file: weekly_record_921_2008-08-11T00-00-00.000.json
    2026-04-15 21:07:17,859 | INFO | Skipping weekly_record_921_2008-08-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,860 | INFO | Processing file: weekly_record_922_2008-08-18T00-00-00.000.json
    2026-04-15 21:07:17,885 | INFO | Skipping weekly_record_922_2008-08-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,885 | INFO | Processing file: weekly_record_923_2008-08-25T00-00-00.000.json
    2026-04-15 21:07:17,909 | INFO | Skipping weekly_record_923_2008-08-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,909 | INFO | Processing file: weekly_record_924_2008-09-01T00-00-00.000.json
    2026-04-15 21:07:17,935 | INFO | Skipping weekly_record_924_2008-09-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,936 | INFO | Processing file: weekly_record_925_2008-09-08T00-00-00.000.json
    2026-04-15 21:07:17,951 | INFO | Skipping weekly_record_925_2008-09-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,952 | INFO | Processing file: weekly_record_926_2008-09-15T00-00-00.000.json
    2026-04-15 21:07:17,979 | INFO | Skipping weekly_record_926_2008-09-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,979 | INFO | Processing file: weekly_record_927_2008-09-22T00-00-00.000.json
    2026-04-15 21:07:17,996 | INFO | Skipping weekly_record_927_2008-09-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:17,997 | INFO | Processing file: weekly_record_928_2008-09-29T00-00-00.000.json
    2026-04-15 21:07:18,021 | INFO | Skipping weekly_record_928_2008-09-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,022 | INFO | Processing file: weekly_record_929_2008-10-06T00-00-00.000.json
    2026-04-15 21:07:18,045 | INFO | Skipping weekly_record_929_2008-10-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,046 | INFO | Processing file: weekly_record_92_1992-09-21T00-00-00.000.json
    2026-04-15 21:07:18,071 | INFO | Skipping weekly_record_92_1992-09-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,072 | INFO | Processing file: weekly_record_930_2008-10-13T00-00-00.000.json
    2026-04-15 21:07:18,090 | INFO | Skipping weekly_record_930_2008-10-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,090 | INFO | Processing file: weekly_record_931_2008-10-20T00-00-00.000.json
    2026-04-15 21:07:18,110 | INFO | Skipping weekly_record_931_2008-10-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,111 | INFO | Processing file: weekly_record_932_2008-10-27T00-00-00.000.json
    2026-04-15 21:07:18,129 | INFO | Skipping weekly_record_932_2008-10-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,130 | INFO | Processing file: weekly_record_933_2008-11-03T00-00-00.000.json
    2026-04-15 21:07:18,148 | INFO | Skipping weekly_record_933_2008-11-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,149 | INFO | Processing file: weekly_record_934_2008-11-10T00-00-00.000.json
    2026-04-15 21:07:18,175 | INFO | Skipping weekly_record_934_2008-11-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,178 | INFO | Processing file: weekly_record_935_2008-11-17T00-00-00.000.json
    2026-04-15 21:07:18,225 | INFO | Skipping weekly_record_935_2008-11-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,225 | INFO | Processing file: weekly_record_936_2008-11-24T00-00-00.000.json
    2026-04-15 21:07:18,244 | INFO | Skipping weekly_record_936_2008-11-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,244 | INFO | Processing file: weekly_record_937_2008-12-01T00-00-00.000.json
    2026-04-15 21:07:18,271 | INFO | Skipping weekly_record_937_2008-12-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,272 | INFO | Processing file: weekly_record_938_2008-12-08T00-00-00.000.json
    2026-04-15 21:07:18,293 | INFO | Skipping weekly_record_938_2008-12-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,293 | INFO | Processing file: weekly_record_939_2008-12-15T00-00-00.000.json
    2026-04-15 21:07:18,319 | INFO | Skipping weekly_record_939_2008-12-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,319 | INFO | Processing file: weekly_record_93_1992-09-28T00-00-00.000.json
    2026-04-15 21:07:18,346 | INFO | Skipping weekly_record_93_1992-09-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,346 | INFO | Processing file: weekly_record_940_2008-12-22T00-00-00.000.json
    2026-04-15 21:07:18,371 | INFO | Skipping weekly_record_940_2008-12-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,372 | INFO | Processing file: weekly_record_941_2008-12-29T00-00-00.000.json
    2026-04-15 21:07:18,399 | INFO | Skipping weekly_record_941_2008-12-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,400 | INFO | Processing file: weekly_record_942_2009-01-05T00-00-00.000.json
    2026-04-15 21:07:18,426 | INFO | Skipping weekly_record_942_2009-01-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,426 | INFO | Processing file: weekly_record_943_2009-01-12T00-00-00.000.json
    2026-04-15 21:07:18,443 | INFO | Skipping weekly_record_943_2009-01-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,444 | INFO | Processing file: weekly_record_944_2009-01-19T00-00-00.000.json
    2026-04-15 21:07:18,472 | INFO | Skipping weekly_record_944_2009-01-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,473 | INFO | Processing file: weekly_record_945_2009-01-26T00-00-00.000.json
    2026-04-15 21:07:18,497 | INFO | Skipping weekly_record_945_2009-01-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,498 | INFO | Processing file: weekly_record_946_2009-02-02T00-00-00.000.json
    2026-04-15 21:07:18,523 | INFO | Skipping weekly_record_946_2009-02-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,524 | INFO | Processing file: weekly_record_947_2009-02-09T00-00-00.000.json
    2026-04-15 21:07:18,541 | INFO | Skipping weekly_record_947_2009-02-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,541 | INFO | Processing file: weekly_record_948_2009-02-16T00-00-00.000.json
    2026-04-15 21:07:18,569 | INFO | Skipping weekly_record_948_2009-02-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,570 | INFO | Processing file: weekly_record_949_2009-02-23T00-00-00.000.json
    2026-04-15 21:07:18,621 | INFO | Skipping weekly_record_949_2009-02-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,621 | INFO | Processing file: weekly_record_94_1992-10-05T00-00-00.000.json
    2026-04-15 21:07:18,641 | INFO | Skipping weekly_record_94_1992-10-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,641 | INFO | Processing file: weekly_record_950_2009-03-02T00-00-00.000.json
    2026-04-15 21:07:18,658 | INFO | Skipping weekly_record_950_2009-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,659 | INFO | Processing file: weekly_record_951_2009-03-09T00-00-00.000.json
    2026-04-15 21:07:18,687 | INFO | Skipping weekly_record_951_2009-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,687 | INFO | Processing file: weekly_record_952_2009-03-16T00-00-00.000.json
    2026-04-15 21:07:18,706 | INFO | Skipping weekly_record_952_2009-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,707 | INFO | Processing file: weekly_record_953_2009-03-23T00-00-00.000.json
    2026-04-15 21:07:18,733 | INFO | Skipping weekly_record_953_2009-03-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,733 | INFO | Processing file: weekly_record_954_2009-03-30T00-00-00.000.json
    2026-04-15 21:07:18,756 | INFO | Skipping weekly_record_954_2009-03-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,756 | INFO | Processing file: weekly_record_955_2009-04-06T00-00-00.000.json
    2026-04-15 21:07:18,778 | INFO | Skipping weekly_record_955_2009-04-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,778 | INFO | Processing file: weekly_record_956_2009-04-13T00-00-00.000.json
    2026-04-15 21:07:18,805 | INFO | Skipping weekly_record_956_2009-04-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,806 | INFO | Processing file: weekly_record_957_2009-04-20T00-00-00.000.json
    2026-04-15 21:07:18,818 | INFO | Skipping weekly_record_957_2009-04-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,819 | INFO | Processing file: weekly_record_958_2009-04-27T00-00-00.000.json
    2026-04-15 21:07:18,845 | INFO | Skipping weekly_record_958_2009-04-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,845 | INFO | Processing file: weekly_record_959_2009-05-04T00-00-00.000.json
    2026-04-15 21:07:18,865 | INFO | Skipping weekly_record_959_2009-05-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,867 | INFO | Processing file: weekly_record_95_1992-10-12T00-00-00.000.json
    2026-04-15 21:07:18,890 | INFO | Skipping weekly_record_95_1992-10-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,891 | INFO | Processing file: weekly_record_960_2009-05-11T00-00-00.000.json
    2026-04-15 21:07:18,909 | INFO | Skipping weekly_record_960_2009-05-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,909 | INFO | Processing file: weekly_record_961_2009-05-18T00-00-00.000.json
    2026-04-15 21:07:18,936 | INFO | Skipping weekly_record_961_2009-05-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,936 | INFO | Processing file: weekly_record_962_2009-05-25T00-00-00.000.json
    2026-04-15 21:07:18,963 | INFO | Skipping weekly_record_962_2009-05-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:18,966 | INFO | Processing file: weekly_record_963_2009-06-01T00-00-00.000.json
    2026-04-15 21:07:19,006 | INFO | Skipping weekly_record_963_2009-06-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,007 | INFO | Processing file: weekly_record_964_2009-06-08T00-00-00.000.json
    2026-04-15 21:07:19,033 | INFO | Skipping weekly_record_964_2009-06-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,034 | INFO | Processing file: weekly_record_965_2009-06-15T00-00-00.000.json
    2026-04-15 21:07:19,067 | INFO | Skipping weekly_record_965_2009-06-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,068 | INFO | Processing file: weekly_record_966_2009-06-22T00-00-00.000.json
    2026-04-15 21:07:19,085 | INFO | Skipping weekly_record_966_2009-06-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,086 | INFO | Processing file: weekly_record_967_2009-06-29T00-00-00.000.json
    2026-04-15 21:07:19,107 | INFO | Skipping weekly_record_967_2009-06-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,108 | INFO | Processing file: weekly_record_968_2009-07-06T00-00-00.000.json
    2026-04-15 21:07:19,125 | INFO | Skipping weekly_record_968_2009-07-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,126 | INFO | Processing file: weekly_record_969_2009-07-13T00-00-00.000.json
    2026-04-15 21:07:19,153 | INFO | Skipping weekly_record_969_2009-07-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,154 | INFO | Processing file: weekly_record_96_1992-10-19T00-00-00.000.json
    2026-04-15 21:07:19,172 | INFO | Skipping weekly_record_96_1992-10-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,172 | INFO | Processing file: weekly_record_970_2009-07-20T00-00-00.000.json
    2026-04-15 21:07:19,193 | INFO | Skipping weekly_record_970_2009-07-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,193 | INFO | Processing file: weekly_record_971_2009-07-27T00-00-00.000.json
    2026-04-15 21:07:19,212 | INFO | Skipping weekly_record_971_2009-07-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,213 | INFO | Processing file: weekly_record_972_2009-08-03T00-00-00.000.json
    2026-04-15 21:07:19,238 | INFO | Skipping weekly_record_972_2009-08-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,239 | INFO | Processing file: weekly_record_973_2009-08-10T00-00-00.000.json
    2026-04-15 21:07:19,258 | INFO | Skipping weekly_record_973_2009-08-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,261 | INFO | Processing file: weekly_record_974_2009-08-17T00-00-00.000.json
    2026-04-15 21:07:19,283 | INFO | Skipping weekly_record_974_2009-08-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,284 | INFO | Processing file: weekly_record_975_2009-08-24T00-00-00.000.json
    2026-04-15 21:07:19,301 | INFO | Skipping weekly_record_975_2009-08-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,302 | INFO | Processing file: weekly_record_976_2009-08-31T00-00-00.000.json
    2026-04-15 21:07:19,319 | INFO | Skipping weekly_record_976_2009-08-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,319 | INFO | Processing file: weekly_record_977_2009-09-07T00-00-00.000.json
    2026-04-15 21:07:19,338 | INFO | Skipping weekly_record_977_2009-09-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,339 | INFO | Processing file: weekly_record_978_2009-09-14T00-00-00.000.json
    2026-04-15 21:07:19,356 | INFO | Skipping weekly_record_978_2009-09-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,356 | INFO | Processing file: weekly_record_979_2009-09-21T00-00-00.000.json
    2026-04-15 21:07:19,403 | INFO | Skipping weekly_record_979_2009-09-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,409 | INFO | Processing file: weekly_record_97_1992-10-26T00-00-00.000.json
    2026-04-15 21:07:19,436 | INFO | Skipping weekly_record_97_1992-10-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,437 | INFO | Processing file: weekly_record_980_2009-09-28T00-00-00.000.json
    2026-04-15 21:07:19,462 | INFO | Skipping weekly_record_980_2009-09-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,463 | INFO | Processing file: weekly_record_981_2009-10-05T00-00-00.000.json
    2026-04-15 21:07:19,483 | INFO | Skipping weekly_record_981_2009-10-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,484 | INFO | Processing file: weekly_record_982_2009-10-12T00-00-00.000.json
    2026-04-15 21:07:19,509 | INFO | Skipping weekly_record_982_2009-10-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,510 | INFO | Processing file: weekly_record_983_2009-10-19T00-00-00.000.json
    2026-04-15 21:07:19,536 | INFO | Skipping weekly_record_983_2009-10-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,536 | INFO | Processing file: weekly_record_984_2009-10-26T00-00-00.000.json
    2026-04-15 21:07:19,553 | INFO | Skipping weekly_record_984_2009-10-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,553 | INFO | Processing file: weekly_record_985_2009-11-02T00-00-00.000.json
    2026-04-15 21:07:19,576 | INFO | Skipping weekly_record_985_2009-11-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,577 | INFO | Processing file: weekly_record_986_2009-11-09T00-00-00.000.json
    2026-04-15 21:07:19,595 | INFO | Skipping weekly_record_986_2009-11-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,595 | INFO | Processing file: weekly_record_987_2009-11-16T00-00-00.000.json
    2026-04-15 21:07:19,622 | INFO | Skipping weekly_record_987_2009-11-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,623 | INFO | Processing file: weekly_record_988_2009-11-23T00-00-00.000.json
    2026-04-15 21:07:19,639 | INFO | Skipping weekly_record_988_2009-11-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,639 | INFO | Processing file: weekly_record_989_2009-11-30T00-00-00.000.json
    2026-04-15 21:07:19,667 | INFO | Skipping weekly_record_989_2009-11-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,668 | INFO | Processing file: weekly_record_98_1992-11-02T00-00-00.000.json
    2026-04-15 21:07:19,692 | INFO | Skipping weekly_record_98_1992-11-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,693 | INFO | Processing file: weekly_record_990_2009-12-07T00-00-00.000.json
    2026-04-15 21:07:19,712 | INFO | Skipping weekly_record_990_2009-12-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,712 | INFO | Processing file: weekly_record_991_2009-12-14T00-00-00.000.json
    2026-04-15 21:07:19,735 | INFO | Skipping weekly_record_991_2009-12-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,736 | INFO | Processing file: weekly_record_992_2009-12-21T00-00-00.000.json
    2026-04-15 21:07:19,750 | INFO | Skipping weekly_record_992_2009-12-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,751 | INFO | Processing file: weekly_record_993_2009-12-28T00-00-00.000.json
    2026-04-15 21:07:19,777 | INFO | Skipping weekly_record_993_2009-12-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,784 | INFO | Processing file: weekly_record_994_2010-01-04T00-00-00.000.json
    2026-04-15 21:07:19,820 | INFO | Skipping weekly_record_994_2010-01-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,820 | INFO | Processing file: weekly_record_995_2010-01-11T00-00-00.000.json
    2026-04-15 21:07:19,837 | INFO | Skipping weekly_record_995_2010-01-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,837 | INFO | Processing file: weekly_record_996_2010-01-18T00-00-00.000.json
    2026-04-15 21:07:19,854 | INFO | Skipping weekly_record_996_2010-01-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,854 | INFO | Processing file: weekly_record_997_2010-01-25T00-00-00.000.json
    2026-04-15 21:07:19,892 | INFO | Skipping weekly_record_997_2010-01-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,893 | INFO | Processing file: weekly_record_998_2010-02-01T00-00-00.000.json
    2026-04-15 21:07:19,916 | INFO | Skipping weekly_record_998_2010-02-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,917 | INFO | Processing file: weekly_record_999_2010-02-08T00-00-00.000.json
    2026-04-15 21:07:19,944 | INFO | Skipping weekly_record_999_2010-02-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,944 | INFO | Processing file: weekly_record_99_1992-11-09T00-00-00.000.json
    2026-04-15 21:07:19,961 | INFO | Skipping weekly_record_99_1992-11-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,961 | INFO | Processing file: weekly_record_9_1991-02-18T00-00-00.000.json
    2026-04-15 21:07:19,983 | INFO | Skipping weekly_record_9_1991-02-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 21:07:19,984 | INFO | Upload complete | uploaded=0 | skipped=1839 | failed=0


## Connect to MongoDB and pull data


```python
from pymongo import MongoClient
import os
from urllib.parse import quote_plus

# Get credentials from environment variables
username = os.getenv("MONGOUSER")
password_raw = os.getenv("MONGOPASS")

if not username or not password_raw:
    raise ValueError("Missing MongoDB credentials in environment variables.")

# Encode password 
password = quote_plus(password_raw)

# Build URI
uri = f"mongodb+srv://{username}:{password}@dp2.0pmvbm0.mongodb.net/?appName=dp2"

# Connect
client = MongoClient(uri)

# Select database
db = client["project_db"]

# Quick test
print("Collections:", db.list_collection_names())
```

    Collections: ['uploaded_files', 'raw_data']


## Data Preperation query from MongoDB into a DataFrame, data cleaning and train/test split


```python
# Problem: Data preparation query from MongoDB to a DataFrame

import pandas as pd

# Pull only the fields needed for modeling
cursor = db["raw_data"].find(
    {},
    {
        "_id": 0,
        "Date": 1,
        "gas_price": 1,
        "wti_price": 1,
        "recession": 1,
        "wti_pct_change": 1,
        "gas_lag1": 1,
        "gas_lag2": 1,
        "gas_lag3": 1,
        "target_gas_4w": 1
    }
)

df = pd.DataFrame(list(cursor))

# Convert Date to datetime and sort
df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
df = df.sort_values("Date").reset_index(drop=True)

print(df.head())
print("\nShape:", df.shape)
print("\nColumns:", df.columns.tolist())

# Keep only rows with complete modeling data
model_df = df.dropna().copy()

features = [
    "wti_price",
    "recession",
    "wti_pct_change"
]

target = "target_gas_4w"

X = model_df[features]
y = model_df[target]

# Time-based split so the model trains on earlier periods and tests on later periods
split_index = int(len(model_df) * 0.8)

X_train = X.iloc[:split_index]
X_test = X.iloc[split_index:]
y_train = y.iloc[:split_index]
y_test = y.iloc[split_index:]

dates_train = model_df["Date"].iloc[:split_index]
dates_test = model_df["Date"].iloc[split_index:]

print("Train size:", len(X_train))
print("Test size:", len(X_test))
```

            Date  gas_price  wti_price  recession  wti_pct_change  gas_lag1  \
    0 1990-11-12      1.328     33.892          1         -0.1502     1.339   
    1 1990-11-19      1.323     31.504          1         -0.1449     1.345   
    2 1990-11-26      1.311     30.692          1         -0.0200     1.339   
    3 1990-12-03      1.341     32.324          1         -0.0752     1.334   
    4 1991-01-21      1.192     26.852          1         -0.2077     1.328   
    
       gas_lag2  gas_lag3  target_gas_4w  
    0     1.266     1.191          1.192  
    1     1.272     1.245          1.168  
    2     1.321     1.242          1.139  
    3     1.333     1.252          1.106  
    4     1.339     1.266          1.078  
    
    Shape: (1839, 9)
    
    Columns: ['Date', 'gas_price', 'wti_price', 'recession', 'wti_pct_change', 'gas_lag1', 'gas_lag2', 'gas_lag3', 'target_gas_4w']
    Train size: 1471
    Test size: 368


## Solution analysis, implement the model


```python
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
import numpy as np

# Random forest can capture nonlinear patterns between oil prices and future gas prices
rf = RandomForestRegressor(
    n_estimators=300,
    max_depth=8,
    min_samples_leaf=3,
    random_state=42
)

rf.fit(X_train, y_train)

y_pred = rf.predict(X_test)

mae = mean_absolute_error(y_test, y_pred)
rmse = np.sqrt(mean_squared_error(y_test, y_pred))
r2 = r2_score(y_test, y_pred)

print("MAE:", round(mae, 4))
print("RMSE:", round(rmse, 4))
print("R^2:", round(r2, 4))
```

    MAE: 0.4482
    RMSE: 0.5253
    R^2: 0.2277


## Analysis complexity, feature importance


```python
# Feature importance helps show which inputs matter most in the model
importance_df = pd.DataFrame({
    "feature": features,
    "importance": rf.feature_importances_
}).sort_values("importance", ascending=False)

print(importance_df)
```

              feature  importance
    0       wti_price    0.984410
    2  wti_pct_change    0.012982
    1       recession    0.002608


## Create a results DataFrame


```python
results_df = pd.DataFrame({
    "Date": dates_test.values,
    "actual_gas_4w": y_test.values,
    "predicted_gas_4w": y_pred
})

results_df["error"] = results_df["actual_gas_4w"] - results_df["predicted_gas_4w"]

print(results_df.head())
```

            Date  actual_gas_4w  predicted_gas_4w     error
    0 2019-03-04          2.691          2.496443  0.194557
    1 2019-03-11          2.745          2.503299  0.241701
    2 2019-03-18          2.828          2.525569  0.302431
    3 2019-03-25          2.841          2.569938  0.271062
    4 2019-04-01          2.887          2.545684  0.341316


## Visualize results, actual vs predicted over time


```python
import matplotlib.pyplot as plt

plt.figure(figsize=(10, 6))

# Professional color palette
actual_color = "#1f3a5f"      # deep navy
predicted_color = "#d95f02"   # muted orange

plt.plot(
    results_df["Date"],
    results_df["actual_gas_4w"],
    label="Actual",
    linewidth=2,
    color=actual_color
)

plt.plot(
    results_df["Date"],
    results_df["predicted_gas_4w"],
    label="Predicted",
    linewidth=2,
    linestyle="--",
    color=predicted_color
)

plt.xlabel("Date")
plt.ylabel("Gas Price")
plt.title("Actual vs Predicted 4-Week-Ahead Gas Prices")

# Remove top and right borders for cleaner look
ax = plt.gca()
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)

plt.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.show()
```


    
![png](pipeline_files/pipeline_14_0.png)
    


*Comparison of actual and predicted U.S. gasoline prices four weeks ahead over time. The model’s predictions closely track overall trends and major movements in gas prices, though some deviations appear during periods of higher volatility*

## Visualization Rationale
This visualization was designed to compare the model’s predicted four-week-ahead gas prices against the actual observed values over time. A line chart was chosen because the data is time-based, and this format makes it easy to evaluate whether the model captures major trends, turning points, and short-term fluctuations in gas prices. Using two separate lines allows direct comparison between actual and predicted values, which is more informative for a forecasting problem than a single summary statistic alone. The actual values were shown in deep navy and the predicted values in muted orange to create clear contrast while still maintaining a professional appearance.

Several formatting decisions were made to improve readability and presentation quality. The predicted series was shown with a dashed line so it could be distinguished from the actual series even if the two lines overlap closely. The figure size was enlarged to make the time pattern easier to read, and the x-axis labels were rotated to prevent dates from overlapping. The top and right borders were removed to reduce visual clutter and create a cleaner, more modern presentation style. Overall, these choices were intended to make the graph easier to interpret while clearly communicating how well the model tracks actual gas price movements over time.



```python
!jupyter nbconvert --to markdown pipeline.ipynb
```
