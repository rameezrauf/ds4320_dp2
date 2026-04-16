```python
!pip install pymongo
```

    Collecting pymongo
      Downloading pymongo-4.16.0-cp313-cp313-macosx_11_0_arm64.whl.metadata (10.0 kB)
    Collecting dnspython<3.0.0,>=2.6.1 (from pymongo)
      Downloading dnspython-2.8.0-py3-none-any.whl.metadata (5.7 kB)
    Downloading pymongo-4.16.0-cp313-cp313-macosx_11_0_arm64.whl (971 kB)
    [2K   [90m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[0m [32m971.8/971.8 kB[0m [31m13.9 MB/s[0m eta [36m0:00:00[0m
    [?25hDownloading dnspython-2.8.0-py3-none-any.whl (331 kB)
    Installing collected packages: dnspython, pymongo
    Successfully installed dnspython-2.8.0 pymongo-4.16.0
    
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

    2026-04-15 19:24:26,678 | INFO | Connecting to MongoDB cluster...


    Logging to: /Users/rameezrauf/Desktop/ds_4320/ds4320_dp2/logs/mongo_upload.log


    2026-04-15 19:24:27,336 | INFO | MongoDB connection successful.
    2026-04-15 19:24:27,387 | INFO | Found 1839 JSON files in /Users/rameezrauf/Desktop/ds_4320/ds4320_dp2/data
    2026-04-15 19:24:27,388 | INFO | Processing file: weekly_record_1000_2010-02-15T00-00-00.000.json
    2026-04-15 19:24:27,408 | INFO | Skipping weekly_record_1000_2010-02-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,409 | INFO | Processing file: weekly_record_1001_2010-02-22T00-00-00.000.json
    2026-04-15 19:24:27,425 | INFO | Skipping weekly_record_1001_2010-02-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,426 | INFO | Processing file: weekly_record_1002_2010-03-01T00-00-00.000.json
    2026-04-15 19:24:27,448 | INFO | Skipping weekly_record_1002_2010-03-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,449 | INFO | Processing file: weekly_record_1003_2010-03-08T00-00-00.000.json
    2026-04-15 19:24:27,465 | INFO | Skipping weekly_record_1003_2010-03-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,466 | INFO | Processing file: weekly_record_1004_2010-03-15T00-00-00.000.json
    2026-04-15 19:24:27,492 | INFO | Skipping weekly_record_1004_2010-03-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,493 | INFO | Processing file: weekly_record_1005_2010-03-22T00-00-00.000.json
    2026-04-15 19:24:27,512 | INFO | Skipping weekly_record_1005_2010-03-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,513 | INFO | Processing file: weekly_record_1006_2010-03-29T00-00-00.000.json
    2026-04-15 19:24:27,537 | INFO | Skipping weekly_record_1006_2010-03-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,538 | INFO | Processing file: weekly_record_1007_2010-04-05T00-00-00.000.json
    2026-04-15 19:24:27,556 | INFO | Skipping weekly_record_1007_2010-04-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,557 | INFO | Processing file: weekly_record_1008_2010-04-12T00-00-00.000.json
    2026-04-15 19:24:27,582 | INFO | Skipping weekly_record_1008_2010-04-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,583 | INFO | Processing file: weekly_record_1009_2010-04-19T00-00-00.000.json
    2026-04-15 19:24:27,600 | INFO | Skipping weekly_record_1009_2010-04-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,610 | INFO | Processing file: weekly_record_100_1992-11-16T00-00-00.000.json
    2026-04-15 19:24:27,629 | INFO | Skipping weekly_record_100_1992-11-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,630 | INFO | Processing file: weekly_record_1010_2010-04-26T00-00-00.000.json
    2026-04-15 19:24:27,658 | INFO | Skipping weekly_record_1010_2010-04-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,658 | INFO | Processing file: weekly_record_1011_2010-05-03T00-00-00.000.json
    2026-04-15 19:24:27,686 | INFO | Skipping weekly_record_1011_2010-05-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,686 | INFO | Processing file: weekly_record_1012_2010-05-10T00-00-00.000.json
    2026-04-15 19:24:27,703 | INFO | Skipping weekly_record_1012_2010-05-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,704 | INFO | Processing file: weekly_record_1013_2010-05-17T00-00-00.000.json
    2026-04-15 19:24:27,731 | INFO | Skipping weekly_record_1013_2010-05-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,731 | INFO | Processing file: weekly_record_1014_2010-05-24T00-00-00.000.json
    2026-04-15 19:24:27,756 | INFO | Skipping weekly_record_1014_2010-05-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,756 | INFO | Processing file: weekly_record_1015_2010-05-31T00-00-00.000.json
    2026-04-15 19:24:27,782 | INFO | Skipping weekly_record_1015_2010-05-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,783 | INFO | Processing file: weekly_record_1016_2010-06-07T00-00-00.000.json
    2026-04-15 19:24:27,811 | INFO | Skipping weekly_record_1016_2010-06-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,812 | INFO | Processing file: weekly_record_1017_2010-06-14T00-00-00.000.json
    2026-04-15 19:24:27,829 | INFO | Skipping weekly_record_1017_2010-06-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,830 | INFO | Processing file: weekly_record_1018_2010-06-21T00-00-00.000.json
    2026-04-15 19:24:27,856 | INFO | Skipping weekly_record_1018_2010-06-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,857 | INFO | Processing file: weekly_record_1019_2010-06-28T00-00-00.000.json
    2026-04-15 19:24:27,874 | INFO | Skipping weekly_record_1019_2010-06-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,874 | INFO | Processing file: weekly_record_101_1992-11-23T00-00-00.000.json
    2026-04-15 19:24:27,897 | INFO | Skipping weekly_record_101_1992-11-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,898 | INFO | Processing file: weekly_record_1020_2010-07-05T00-00-00.000.json
    2026-04-15 19:24:27,916 | INFO | Skipping weekly_record_1020_2010-07-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,917 | INFO | Processing file: weekly_record_1021_2010-07-12T00-00-00.000.json
    2026-04-15 19:24:27,943 | INFO | Skipping weekly_record_1021_2010-07-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,943 | INFO | Processing file: weekly_record_1022_2010-07-19T00-00-00.000.json
    2026-04-15 19:24:27,960 | INFO | Skipping weekly_record_1022_2010-07-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,961 | INFO | Processing file: weekly_record_1023_2010-07-26T00-00-00.000.json
    2026-04-15 19:24:27,987 | INFO | Skipping weekly_record_1023_2010-07-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:27,987 | INFO | Processing file: weekly_record_1024_2010-08-02T00-00-00.000.json
    2026-04-15 19:24:28,004 | INFO | Skipping weekly_record_1024_2010-08-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,004 | INFO | Processing file: weekly_record_1025_2010-08-09T00-00-00.000.json
    2026-04-15 19:24:28,032 | INFO | Skipping weekly_record_1025_2010-08-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,032 | INFO | Processing file: weekly_record_1026_2010-08-16T00-00-00.000.json
    2026-04-15 19:24:28,049 | INFO | Skipping weekly_record_1026_2010-08-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,050 | INFO | Processing file: weekly_record_1027_2010-08-23T00-00-00.000.json
    2026-04-15 19:24:28,076 | INFO | Skipping weekly_record_1027_2010-08-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,076 | INFO | Processing file: weekly_record_1028_2010-08-30T00-00-00.000.json
    2026-04-15 19:24:28,093 | INFO | Skipping weekly_record_1028_2010-08-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,094 | INFO | Processing file: weekly_record_1029_2010-09-06T00-00-00.000.json
    2026-04-15 19:24:28,120 | INFO | Skipping weekly_record_1029_2010-09-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,121 | INFO | Processing file: weekly_record_102_1992-11-30T00-00-00.000.json
    2026-04-15 19:24:28,138 | INFO | Skipping weekly_record_102_1992-11-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,138 | INFO | Processing file: weekly_record_1030_2010-09-13T00-00-00.000.json
    2026-04-15 19:24:28,160 | INFO | Skipping weekly_record_1030_2010-09-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,161 | INFO | Processing file: weekly_record_1031_2010-09-20T00-00-00.000.json
    2026-04-15 19:24:28,177 | INFO | Skipping weekly_record_1031_2010-09-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,178 | INFO | Processing file: weekly_record_1032_2010-09-27T00-00-00.000.json
    2026-04-15 19:24:28,198 | INFO | Skipping weekly_record_1032_2010-09-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,199 | INFO | Processing file: weekly_record_1033_2010-10-04T00-00-00.000.json
    2026-04-15 19:24:28,225 | INFO | Skipping weekly_record_1033_2010-10-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,231 | INFO | Processing file: weekly_record_1034_2010-10-11T00-00-00.000.json
    2026-04-15 19:24:28,250 | INFO | Skipping weekly_record_1034_2010-10-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,251 | INFO | Processing file: weekly_record_1035_2010-10-18T00-00-00.000.json
    2026-04-15 19:24:28,275 | INFO | Skipping weekly_record_1035_2010-10-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,275 | INFO | Processing file: weekly_record_1036_2010-10-25T00-00-00.000.json
    2026-04-15 19:24:28,298 | INFO | Skipping weekly_record_1036_2010-10-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,298 | INFO | Processing file: weekly_record_1037_2010-11-01T00-00-00.000.json
    2026-04-15 19:24:28,320 | INFO | Skipping weekly_record_1037_2010-11-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,321 | INFO | Processing file: weekly_record_1038_2010-11-08T00-00-00.000.json
    2026-04-15 19:24:28,342 | INFO | Skipping weekly_record_1038_2010-11-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,343 | INFO | Processing file: weekly_record_1039_2010-11-15T00-00-00.000.json
    2026-04-15 19:24:28,365 | INFO | Skipping weekly_record_1039_2010-11-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,365 | INFO | Processing file: weekly_record_103_1992-12-07T00-00-00.000.json
    2026-04-15 19:24:28,385 | INFO | Skipping weekly_record_103_1992-12-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,386 | INFO | Processing file: weekly_record_1040_2010-11-22T00-00-00.000.json
    2026-04-15 19:24:28,408 | INFO | Skipping weekly_record_1040_2010-11-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,409 | INFO | Processing file: weekly_record_1041_2010-11-29T00-00-00.000.json
    2026-04-15 19:24:28,433 | INFO | Skipping weekly_record_1041_2010-11-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,434 | INFO | Processing file: weekly_record_1042_2010-12-06T00-00-00.000.json
    2026-04-15 19:24:28,459 | INFO | Skipping weekly_record_1042_2010-12-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,459 | INFO | Processing file: weekly_record_1043_2010-12-13T00-00-00.000.json
    2026-04-15 19:24:28,476 | INFO | Skipping weekly_record_1043_2010-12-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,477 | INFO | Processing file: weekly_record_1044_2010-12-20T00-00-00.000.json
    2026-04-15 19:24:28,505 | INFO | Skipping weekly_record_1044_2010-12-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,506 | INFO | Processing file: weekly_record_1045_2010-12-27T00-00-00.000.json
    2026-04-15 19:24:28,531 | INFO | Skipping weekly_record_1045_2010-12-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,531 | INFO | Processing file: weekly_record_1046_2011-01-03T00-00-00.000.json
    2026-04-15 19:24:28,548 | INFO | Skipping weekly_record_1046_2011-01-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,548 | INFO | Processing file: weekly_record_1047_2011-01-10T00-00-00.000.json
    2026-04-15 19:24:28,570 | INFO | Skipping weekly_record_1047_2011-01-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,570 | INFO | Processing file: weekly_record_1048_2011-01-17T00-00-00.000.json
    2026-04-15 19:24:28,598 | INFO | Skipping weekly_record_1048_2011-01-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,598 | INFO | Processing file: weekly_record_1049_2011-01-24T00-00-00.000.json
    2026-04-15 19:24:28,616 | INFO | Skipping weekly_record_1049_2011-01-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,617 | INFO | Processing file: weekly_record_104_1992-12-14T00-00-00.000.json
    2026-04-15 19:24:28,643 | INFO | Skipping weekly_record_104_1992-12-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,643 | INFO | Processing file: weekly_record_1050_2011-01-31T00-00-00.000.json
    2026-04-15 19:24:28,663 | INFO | Skipping weekly_record_1050_2011-01-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,664 | INFO | Processing file: weekly_record_1051_2011-02-07T00-00-00.000.json
    2026-04-15 19:24:28,714 | INFO | Skipping weekly_record_1051_2011-02-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,715 | INFO | Processing file: weekly_record_1052_2011-02-14T00-00-00.000.json
    2026-04-15 19:24:28,734 | INFO | Skipping weekly_record_1052_2011-02-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,734 | INFO | Processing file: weekly_record_1053_2011-02-21T00-00-00.000.json
    2026-04-15 19:24:28,765 | INFO | Skipping weekly_record_1053_2011-02-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,765 | INFO | Processing file: weekly_record_1054_2011-02-28T00-00-00.000.json
    2026-04-15 19:24:28,790 | INFO | Skipping weekly_record_1054_2011-02-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,791 | INFO | Processing file: weekly_record_1055_2011-03-07T00-00-00.000.json
    2026-04-15 19:24:28,818 | INFO | Skipping weekly_record_1055_2011-03-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,818 | INFO | Processing file: weekly_record_1056_2011-03-14T00-00-00.000.json
    2026-04-15 19:24:28,845 | INFO | Skipping weekly_record_1056_2011-03-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,845 | INFO | Processing file: weekly_record_1057_2011-03-21T00-00-00.000.json
    2026-04-15 19:24:28,871 | INFO | Skipping weekly_record_1057_2011-03-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,871 | INFO | Processing file: weekly_record_1058_2011-03-28T00-00-00.000.json
    2026-04-15 19:24:28,903 | INFO | Skipping weekly_record_1058_2011-03-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,904 | INFO | Processing file: weekly_record_1059_2011-04-04T00-00-00.000.json
    2026-04-15 19:24:28,934 | INFO | Skipping weekly_record_1059_2011-04-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,934 | INFO | Processing file: weekly_record_105_1992-12-21T00-00-00.000.json
    2026-04-15 19:24:28,955 | INFO | Skipping weekly_record_105_1992-12-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,955 | INFO | Processing file: weekly_record_1060_2011-04-11T00-00-00.000.json
    2026-04-15 19:24:28,977 | INFO | Skipping weekly_record_1060_2011-04-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:28,978 | INFO | Processing file: weekly_record_1061_2011-04-18T00-00-00.000.json
    2026-04-15 19:24:29,006 | INFO | Skipping weekly_record_1061_2011-04-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,007 | INFO | Processing file: weekly_record_1062_2011-04-25T00-00-00.000.json
    2026-04-15 19:24:29,034 | INFO | Skipping weekly_record_1062_2011-04-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,034 | INFO | Processing file: weekly_record_1063_2011-05-02T00-00-00.000.json
    2026-04-15 19:24:29,060 | INFO | Skipping weekly_record_1063_2011-05-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,060 | INFO | Processing file: weekly_record_1064_2011-05-09T00-00-00.000.json
    2026-04-15 19:24:29,087 | INFO | Skipping weekly_record_1064_2011-05-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,087 | INFO | Processing file: weekly_record_1065_2011-05-16T00-00-00.000.json
    2026-04-15 19:24:29,105 | INFO | Skipping weekly_record_1065_2011-05-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,105 | INFO | Processing file: weekly_record_1066_2011-05-23T00-00-00.000.json
    2026-04-15 19:24:29,131 | INFO | Skipping weekly_record_1066_2011-05-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,132 | INFO | Processing file: weekly_record_1067_2011-05-30T00-00-00.000.json
    2026-04-15 19:24:29,149 | INFO | Skipping weekly_record_1067_2011-05-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,150 | INFO | Processing file: weekly_record_1068_2011-06-06T00-00-00.000.json
    2026-04-15 19:24:29,175 | INFO | Skipping weekly_record_1068_2011-06-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,176 | INFO | Processing file: weekly_record_1069_2011-06-13T00-00-00.000.json
    2026-04-15 19:24:29,190 | INFO | Skipping weekly_record_1069_2011-06-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,190 | INFO | Processing file: weekly_record_106_1992-12-28T00-00-00.000.json
    2026-04-15 19:24:29,216 | INFO | Skipping weekly_record_106_1992-12-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,217 | INFO | Processing file: weekly_record_1070_2011-06-20T00-00-00.000.json
    2026-04-15 19:24:29,244 | INFO | Skipping weekly_record_1070_2011-06-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,244 | INFO | Processing file: weekly_record_1071_2011-06-27T00-00-00.000.json
    2026-04-15 19:24:29,261 | INFO | Skipping weekly_record_1071_2011-06-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,262 | INFO | Processing file: weekly_record_1072_2011-07-04T00-00-00.000.json
    2026-04-15 19:24:29,287 | INFO | Skipping weekly_record_1072_2011-07-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,288 | INFO | Processing file: weekly_record_1073_2011-07-11T00-00-00.000.json
    2026-04-15 19:24:29,305 | INFO | Skipping weekly_record_1073_2011-07-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,305 | INFO | Processing file: weekly_record_1074_2011-07-18T00-00-00.000.json
    2026-04-15 19:24:29,324 | INFO | Skipping weekly_record_1074_2011-07-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,325 | INFO | Processing file: weekly_record_1075_2011-07-25T00-00-00.000.json
    2026-04-15 19:24:29,342 | INFO | Skipping weekly_record_1075_2011-07-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,343 | INFO | Processing file: weekly_record_1076_2011-08-01T00-00-00.000.json
    2026-04-15 19:24:29,368 | INFO | Skipping weekly_record_1076_2011-08-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,369 | INFO | Processing file: weekly_record_1077_2011-08-08T00-00-00.000.json
    2026-04-15 19:24:29,389 | INFO | Skipping weekly_record_1077_2011-08-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,389 | INFO | Processing file: weekly_record_1078_2011-08-15T00-00-00.000.json
    2026-04-15 19:24:29,414 | INFO | Skipping weekly_record_1078_2011-08-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,415 | INFO | Processing file: weekly_record_1079_2011-08-22T00-00-00.000.json
    2026-04-15 19:24:29,434 | INFO | Skipping weekly_record_1079_2011-08-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,434 | INFO | Processing file: weekly_record_107_1993-01-04T00-00-00.000.json
    2026-04-15 19:24:29,459 | INFO | Skipping weekly_record_107_1993-01-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,459 | INFO | Processing file: weekly_record_1080_2011-08-29T00-00-00.000.json
    2026-04-15 19:24:29,479 | INFO | Skipping weekly_record_1080_2011-08-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,480 | INFO | Processing file: weekly_record_1081_2011-09-05T00-00-00.000.json
    2026-04-15 19:24:29,505 | INFO | Skipping weekly_record_1081_2011-09-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,506 | INFO | Processing file: weekly_record_1082_2011-09-12T00-00-00.000.json
    2026-04-15 19:24:29,522 | INFO | Skipping weekly_record_1082_2011-09-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,523 | INFO | Processing file: weekly_record_1083_2011-09-19T00-00-00.000.json
    2026-04-15 19:24:29,549 | INFO | Skipping weekly_record_1083_2011-09-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,550 | INFO | Processing file: weekly_record_1084_2011-09-26T00-00-00.000.json
    2026-04-15 19:24:29,575 | INFO | Skipping weekly_record_1084_2011-09-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,576 | INFO | Processing file: weekly_record_1085_2011-10-03T00-00-00.000.json
    2026-04-15 19:24:29,598 | INFO | Skipping weekly_record_1085_2011-10-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,598 | INFO | Processing file: weekly_record_1086_2011-10-10T00-00-00.000.json
    2026-04-15 19:24:29,626 | INFO | Skipping weekly_record_1086_2011-10-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,626 | INFO | Processing file: weekly_record_1087_2011-10-17T00-00-00.000.json
    2026-04-15 19:24:29,648 | INFO | Skipping weekly_record_1087_2011-10-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,648 | INFO | Processing file: weekly_record_1088_2011-10-24T00-00-00.000.json
    2026-04-15 19:24:29,674 | INFO | Skipping weekly_record_1088_2011-10-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,675 | INFO | Processing file: weekly_record_1089_2011-10-31T00-00-00.000.json
    2026-04-15 19:24:29,699 | INFO | Skipping weekly_record_1089_2011-10-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,699 | INFO | Processing file: weekly_record_108_1993-01-11T00-00-00.000.json
    2026-04-15 19:24:29,729 | INFO | Skipping weekly_record_108_1993-01-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,729 | INFO | Processing file: weekly_record_1090_2011-11-07T00-00-00.000.json
    2026-04-15 19:24:29,751 | INFO | Skipping weekly_record_1090_2011-11-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,752 | INFO | Processing file: weekly_record_1091_2011-11-14T00-00-00.000.json
    2026-04-15 19:24:29,769 | INFO | Skipping weekly_record_1091_2011-11-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,769 | INFO | Processing file: weekly_record_1092_2011-11-21T00-00-00.000.json
    2026-04-15 19:24:29,791 | INFO | Skipping weekly_record_1092_2011-11-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,792 | INFO | Processing file: weekly_record_1093_2011-11-28T00-00-00.000.json
    2026-04-15 19:24:29,809 | INFO | Skipping weekly_record_1093_2011-11-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,809 | INFO | Processing file: weekly_record_1094_2011-12-05T00-00-00.000.json
    2026-04-15 19:24:29,836 | INFO | Skipping weekly_record_1094_2011-12-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,837 | INFO | Processing file: weekly_record_1095_2011-12-12T00-00-00.000.json
    2026-04-15 19:24:29,863 | INFO | Skipping weekly_record_1095_2011-12-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,863 | INFO | Processing file: weekly_record_1096_2011-12-19T00-00-00.000.json
    2026-04-15 19:24:29,890 | INFO | Skipping weekly_record_1096_2011-12-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,891 | INFO | Processing file: weekly_record_1097_2011-12-26T00-00-00.000.json
    2026-04-15 19:24:29,908 | INFO | Skipping weekly_record_1097_2011-12-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,908 | INFO | Processing file: weekly_record_1098_2012-01-02T00-00-00.000.json
    2026-04-15 19:24:29,934 | INFO | Skipping weekly_record_1098_2012-01-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,935 | INFO | Processing file: weekly_record_1099_2012-01-09T00-00-00.000.json
    2026-04-15 19:24:29,953 | INFO | Skipping weekly_record_1099_2012-01-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,954 | INFO | Processing file: weekly_record_109_1993-01-18T00-00-00.000.json
    2026-04-15 19:24:29,982 | INFO | Skipping weekly_record_109_1993-01-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:29,983 | INFO | Processing file: weekly_record_10_1991-02-25T00-00-00.000.json
    2026-04-15 19:24:30,017 | INFO | Skipping weekly_record_10_1991-02-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,017 | INFO | Processing file: weekly_record_1100_2012-01-16T00-00-00.000.json
    2026-04-15 19:24:30,045 | INFO | Skipping weekly_record_1100_2012-01-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,046 | INFO | Processing file: weekly_record_1101_2012-01-23T00-00-00.000.json
    2026-04-15 19:24:30,069 | INFO | Skipping weekly_record_1101_2012-01-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,070 | INFO | Processing file: weekly_record_1102_2012-01-30T00-00-00.000.json
    2026-04-15 19:24:30,087 | INFO | Skipping weekly_record_1102_2012-01-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,088 | INFO | Processing file: weekly_record_1103_2012-02-06T00-00-00.000.json
    2026-04-15 19:24:30,114 | INFO | Skipping weekly_record_1103_2012-02-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,114 | INFO | Processing file: weekly_record_1104_2012-02-13T00-00-00.000.json
    2026-04-15 19:24:30,131 | INFO | Skipping weekly_record_1104_2012-02-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,132 | INFO | Processing file: weekly_record_1105_2012-02-20T00-00-00.000.json
    2026-04-15 19:24:30,153 | INFO | Skipping weekly_record_1105_2012-02-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,154 | INFO | Processing file: weekly_record_1106_2012-02-27T00-00-00.000.json
    2026-04-15 19:24:30,182 | INFO | Skipping weekly_record_1106_2012-02-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,183 | INFO | Processing file: weekly_record_1107_2012-03-05T00-00-00.000.json
    2026-04-15 19:24:30,200 | INFO | Skipping weekly_record_1107_2012-03-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,200 | INFO | Processing file: weekly_record_1108_2012-03-12T00-00-00.000.json
    2026-04-15 19:24:30,225 | INFO | Skipping weekly_record_1108_2012-03-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,225 | INFO | Processing file: weekly_record_1109_2012-03-19T00-00-00.000.json
    2026-04-15 19:24:30,253 | INFO | Skipping weekly_record_1109_2012-03-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,255 | INFO | Processing file: weekly_record_110_1993-01-25T00-00-00.000.json
    2026-04-15 19:24:30,270 | INFO | Skipping weekly_record_110_1993-01-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,273 | INFO | Processing file: weekly_record_1110_2012-03-26T00-00-00.000.json
    2026-04-15 19:24:30,299 | INFO | Skipping weekly_record_1110_2012-03-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,300 | INFO | Processing file: weekly_record_1111_2012-04-02T00-00-00.000.json
    2026-04-15 19:24:30,324 | INFO | Skipping weekly_record_1111_2012-04-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,325 | INFO | Processing file: weekly_record_1112_2012-04-09T00-00-00.000.json
    2026-04-15 19:24:30,342 | INFO | Skipping weekly_record_1112_2012-04-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,343 | INFO | Processing file: weekly_record_1113_2012-04-16T00-00-00.000.json
    2026-04-15 19:24:30,365 | INFO | Skipping weekly_record_1113_2012-04-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,365 | INFO | Processing file: weekly_record_1114_2012-04-23T00-00-00.000.json
    2026-04-15 19:24:30,383 | INFO | Skipping weekly_record_1114_2012-04-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,383 | INFO | Processing file: weekly_record_1115_2012-04-30T00-00-00.000.json
    2026-04-15 19:24:30,408 | INFO | Skipping weekly_record_1115_2012-04-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,409 | INFO | Processing file: weekly_record_1116_2012-05-07T00-00-00.000.json
    2026-04-15 19:24:30,428 | INFO | Skipping weekly_record_1116_2012-05-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,429 | INFO | Processing file: weekly_record_1117_2012-05-14T00-00-00.000.json
    2026-04-15 19:24:30,452 | INFO | Skipping weekly_record_1117_2012-05-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,453 | INFO | Processing file: weekly_record_1118_2012-05-21T00-00-00.000.json
    2026-04-15 19:24:30,476 | INFO | Skipping weekly_record_1118_2012-05-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,477 | INFO | Processing file: weekly_record_1119_2012-05-28T00-00-00.000.json
    2026-04-15 19:24:30,493 | INFO | Skipping weekly_record_1119_2012-05-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,494 | INFO | Processing file: weekly_record_111_1993-02-01T00-00-00.000.json
    2026-04-15 19:24:30,520 | INFO | Skipping weekly_record_111_1993-02-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,521 | INFO | Processing file: weekly_record_1120_2012-06-04T00-00-00.000.json
    2026-04-15 19:24:30,537 | INFO | Skipping weekly_record_1120_2012-06-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,538 | INFO | Processing file: weekly_record_1121_2012-06-11T00-00-00.000.json
    2026-04-15 19:24:30,565 | INFO | Skipping weekly_record_1121_2012-06-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,566 | INFO | Processing file: weekly_record_1122_2012-06-18T00-00-00.000.json
    2026-04-15 19:24:30,586 | INFO | Skipping weekly_record_1122_2012-06-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,587 | INFO | Processing file: weekly_record_1123_2012-06-25T00-00-00.000.json
    2026-04-15 19:24:30,611 | INFO | Skipping weekly_record_1123_2012-06-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,612 | INFO | Processing file: weekly_record_1124_2012-07-02T00-00-00.000.json
    2026-04-15 19:24:30,634 | INFO | Skipping weekly_record_1124_2012-07-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,634 | INFO | Processing file: weekly_record_1125_2012-07-09T00-00-00.000.json
    2026-04-15 19:24:30,653 | INFO | Skipping weekly_record_1125_2012-07-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,654 | INFO | Processing file: weekly_record_1126_2012-07-16T00-00-00.000.json
    2026-04-15 19:24:30,679 | INFO | Skipping weekly_record_1126_2012-07-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,680 | INFO | Processing file: weekly_record_1127_2012-07-23T00-00-00.000.json
    2026-04-15 19:24:30,701 | INFO | Skipping weekly_record_1127_2012-07-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,701 | INFO | Processing file: weekly_record_1128_2012-07-30T00-00-00.000.json
    2026-04-15 19:24:30,725 | INFO | Skipping weekly_record_1128_2012-07-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,725 | INFO | Processing file: weekly_record_1129_2012-08-06T00-00-00.000.json
    2026-04-15 19:24:30,751 | INFO | Skipping weekly_record_1129_2012-08-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,752 | INFO | Processing file: weekly_record_112_1993-02-08T00-00-00.000.json
    2026-04-15 19:24:30,772 | INFO | Skipping weekly_record_112_1993-02-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,773 | INFO | Processing file: weekly_record_1130_2012-08-13T00-00-00.000.json
    2026-04-15 19:24:30,791 | INFO | Skipping weekly_record_1130_2012-08-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,792 | INFO | Processing file: weekly_record_1131_2012-08-20T00-00-00.000.json
    2026-04-15 19:24:30,817 | INFO | Skipping weekly_record_1131_2012-08-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,818 | INFO | Processing file: weekly_record_1132_2012-08-27T00-00-00.000.json
    2026-04-15 19:24:30,840 | INFO | Skipping weekly_record_1132_2012-08-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,841 | INFO | Processing file: weekly_record_1133_2012-09-03T00-00-00.000.json
    2026-04-15 19:24:30,866 | INFO | Skipping weekly_record_1133_2012-09-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,867 | INFO | Processing file: weekly_record_1134_2012-09-10T00-00-00.000.json
    2026-04-15 19:24:30,889 | INFO | Skipping weekly_record_1134_2012-09-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,889 | INFO | Processing file: weekly_record_1135_2012-09-17T00-00-00.000.json
    2026-04-15 19:24:30,917 | INFO | Skipping weekly_record_1135_2012-09-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,918 | INFO | Processing file: weekly_record_1136_2012-09-24T00-00-00.000.json
    2026-04-15 19:24:30,944 | INFO | Skipping weekly_record_1136_2012-09-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,946 | INFO | Processing file: weekly_record_1137_2012-10-01T00-00-00.000.json
    2026-04-15 19:24:30,975 | INFO | Skipping weekly_record_1137_2012-10-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:30,977 | INFO | Processing file: weekly_record_1138_2012-10-08T00-00-00.000.json
    2026-04-15 19:24:31,002 | INFO | Skipping weekly_record_1138_2012-10-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,003 | INFO | Processing file: weekly_record_1139_2012-10-15T00-00-00.000.json
    2026-04-15 19:24:31,020 | INFO | Skipping weekly_record_1139_2012-10-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,022 | INFO | Processing file: weekly_record_113_1993-02-15T00-00-00.000.json
    2026-04-15 19:24:31,046 | INFO | Skipping weekly_record_113_1993-02-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,048 | INFO | Processing file: weekly_record_1140_2012-10-22T00-00-00.000.json
    2026-04-15 19:24:31,102 | INFO | Skipping weekly_record_1140_2012-10-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,104 | INFO | Processing file: weekly_record_1141_2012-10-29T00-00-00.000.json
    2026-04-15 19:24:31,130 | INFO | Skipping weekly_record_1141_2012-10-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,130 | INFO | Processing file: weekly_record_1142_2012-11-05T00-00-00.000.json
    2026-04-15 19:24:31,149 | INFO | Skipping weekly_record_1142_2012-11-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,149 | INFO | Processing file: weekly_record_1143_2012-11-12T00-00-00.000.json
    2026-04-15 19:24:31,177 | INFO | Skipping weekly_record_1143_2012-11-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,178 | INFO | Processing file: weekly_record_1144_2012-11-19T00-00-00.000.json
    2026-04-15 19:24:31,203 | INFO | Skipping weekly_record_1144_2012-11-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,204 | INFO | Processing file: weekly_record_1145_2012-11-26T00-00-00.000.json
    2026-04-15 19:24:31,223 | INFO | Skipping weekly_record_1145_2012-11-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,225 | INFO | Processing file: weekly_record_1146_2012-12-03T00-00-00.000.json
    2026-04-15 19:24:31,249 | INFO | Skipping weekly_record_1146_2012-12-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,250 | INFO | Processing file: weekly_record_1147_2012-12-10T00-00-00.000.json
    2026-04-15 19:24:31,275 | INFO | Skipping weekly_record_1147_2012-12-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,276 | INFO | Processing file: weekly_record_1148_2012-12-17T00-00-00.000.json
    2026-04-15 19:24:31,303 | INFO | Skipping weekly_record_1148_2012-12-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,304 | INFO | Processing file: weekly_record_1149_2012-12-24T00-00-00.000.json
    2026-04-15 19:24:31,324 | INFO | Skipping weekly_record_1149_2012-12-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,325 | INFO | Processing file: weekly_record_114_1993-02-22T00-00-00.000.json
    2026-04-15 19:24:31,351 | INFO | Skipping weekly_record_114_1993-02-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,356 | INFO | Processing file: weekly_record_1150_2012-12-31T00-00-00.000.json
    2026-04-15 19:24:31,379 | INFO | Skipping weekly_record_1150_2012-12-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,380 | INFO | Processing file: weekly_record_1151_2013-01-07T00-00-00.000.json
    2026-04-15 19:24:31,404 | INFO | Skipping weekly_record_1151_2013-01-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,405 | INFO | Processing file: weekly_record_1152_2013-01-14T00-00-00.000.json
    2026-04-15 19:24:31,426 | INFO | Skipping weekly_record_1152_2013-01-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,427 | INFO | Processing file: weekly_record_1153_2013-01-21T00-00-00.000.json
    2026-04-15 19:24:31,453 | INFO | Skipping weekly_record_1153_2013-01-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,454 | INFO | Processing file: weekly_record_1154_2013-01-28T00-00-00.000.json
    2026-04-15 19:24:31,469 | INFO | Skipping weekly_record_1154_2013-01-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,471 | INFO | Processing file: weekly_record_1155_2013-02-04T00-00-00.000.json
    2026-04-15 19:24:31,495 | INFO | Skipping weekly_record_1155_2013-02-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,495 | INFO | Processing file: weekly_record_1156_2013-02-11T00-00-00.000.json
    2026-04-15 19:24:31,513 | INFO | Skipping weekly_record_1156_2013-02-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,514 | INFO | Processing file: weekly_record_1157_2013-02-18T00-00-00.000.json
    2026-04-15 19:24:31,537 | INFO | Skipping weekly_record_1157_2013-02-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,539 | INFO | Processing file: weekly_record_1158_2013-02-25T00-00-00.000.json
    2026-04-15 19:24:31,554 | INFO | Skipping weekly_record_1158_2013-02-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,555 | INFO | Processing file: weekly_record_1159_2013-03-04T00-00-00.000.json
    2026-04-15 19:24:31,580 | INFO | Skipping weekly_record_1159_2013-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,582 | INFO | Processing file: weekly_record_115_1993-03-01T00-00-00.000.json
    2026-04-15 19:24:31,600 | INFO | Skipping weekly_record_115_1993-03-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,603 | INFO | Processing file: weekly_record_1160_2013-03-11T00-00-00.000.json
    2026-04-15 19:24:31,629 | INFO | Skipping weekly_record_1160_2013-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,631 | INFO | Processing file: weekly_record_1161_2013-03-18T00-00-00.000.json
    2026-04-15 19:24:31,656 | INFO | Skipping weekly_record_1161_2013-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,657 | INFO | Processing file: weekly_record_1162_2013-03-25T00-00-00.000.json
    2026-04-15 19:24:31,675 | INFO | Skipping weekly_record_1162_2013-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,678 | INFO | Processing file: weekly_record_1163_2013-04-01T00-00-00.000.json
    2026-04-15 19:24:31,701 | INFO | Skipping weekly_record_1163_2013-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,703 | INFO | Processing file: weekly_record_1164_2013-04-08T00-00-00.000.json
    2026-04-15 19:24:31,719 | INFO | Skipping weekly_record_1164_2013-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,720 | INFO | Processing file: weekly_record_1165_2013-04-15T00-00-00.000.json
    2026-04-15 19:24:31,738 | INFO | Skipping weekly_record_1165_2013-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,739 | INFO | Processing file: weekly_record_1166_2013-04-22T00-00-00.000.json
    2026-04-15 19:24:31,756 | INFO | Skipping weekly_record_1166_2013-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,758 | INFO | Processing file: weekly_record_1167_2013-04-29T00-00-00.000.json
    2026-04-15 19:24:31,781 | INFO | Skipping weekly_record_1167_2013-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,782 | INFO | Processing file: weekly_record_1168_2013-05-06T00-00-00.000.json
    2026-04-15 19:24:31,799 | INFO | Skipping weekly_record_1168_2013-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,800 | INFO | Processing file: weekly_record_1169_2013-05-13T00-00-00.000.json
    2026-04-15 19:24:31,827 | INFO | Skipping weekly_record_1169_2013-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,828 | INFO | Processing file: weekly_record_116_1993-03-08T00-00-00.000.json
    2026-04-15 19:24:31,845 | INFO | Skipping weekly_record_116_1993-03-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,846 | INFO | Processing file: weekly_record_1170_2013-05-20T00-00-00.000.json
    2026-04-15 19:24:31,872 | INFO | Skipping weekly_record_1170_2013-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,872 | INFO | Processing file: weekly_record_1171_2013-05-27T00-00-00.000.json
    2026-04-15 19:24:31,890 | INFO | Skipping weekly_record_1171_2013-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,891 | INFO | Processing file: weekly_record_1172_2013-06-03T00-00-00.000.json
    2026-04-15 19:24:31,927 | INFO | Skipping weekly_record_1172_2013-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,928 | INFO | Processing file: weekly_record_1173_2013-06-10T00-00-00.000.json
    2026-04-15 19:24:31,947 | INFO | Skipping weekly_record_1173_2013-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,947 | INFO | Processing file: weekly_record_1174_2013-06-17T00-00-00.000.json
    2026-04-15 19:24:31,970 | INFO | Skipping weekly_record_1174_2013-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,971 | INFO | Processing file: weekly_record_1175_2013-06-24T00-00-00.000.json
    2026-04-15 19:24:31,994 | INFO | Skipping weekly_record_1175_2013-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:31,994 | INFO | Processing file: weekly_record_1176_2013-07-01T00-00-00.000.json
    2026-04-15 19:24:32,020 | INFO | Skipping weekly_record_1176_2013-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,021 | INFO | Processing file: weekly_record_1177_2013-07-08T00-00-00.000.json
    2026-04-15 19:24:32,040 | INFO | Skipping weekly_record_1177_2013-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,040 | INFO | Processing file: weekly_record_1178_2013-07-15T00-00-00.000.json
    2026-04-15 19:24:32,065 | INFO | Skipping weekly_record_1178_2013-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,066 | INFO | Processing file: weekly_record_1179_2013-07-22T00-00-00.000.json
    2026-04-15 19:24:32,083 | INFO | Skipping weekly_record_1179_2013-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,084 | INFO | Processing file: weekly_record_117_1993-03-15T00-00-00.000.json
    2026-04-15 19:24:32,111 | INFO | Skipping weekly_record_117_1993-03-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,112 | INFO | Processing file: weekly_record_1180_2013-07-29T00-00-00.000.json
    2026-04-15 19:24:32,128 | INFO | Skipping weekly_record_1180_2013-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,129 | INFO | Processing file: weekly_record_1181_2013-08-05T00-00-00.000.json
    2026-04-15 19:24:32,155 | INFO | Skipping weekly_record_1181_2013-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,155 | INFO | Processing file: weekly_record_1182_2013-08-12T00-00-00.000.json
    2026-04-15 19:24:32,172 | INFO | Skipping weekly_record_1182_2013-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,173 | INFO | Processing file: weekly_record_1183_2013-08-19T00-00-00.000.json
    2026-04-15 19:24:32,199 | INFO | Skipping weekly_record_1183_2013-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,200 | INFO | Processing file: weekly_record_1184_2013-08-26T00-00-00.000.json
    2026-04-15 19:24:32,218 | INFO | Skipping weekly_record_1184_2013-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,219 | INFO | Processing file: weekly_record_1185_2013-09-02T00-00-00.000.json
    2026-04-15 19:24:32,244 | INFO | Skipping weekly_record_1185_2013-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,246 | INFO | Processing file: weekly_record_1186_2013-09-09T00-00-00.000.json
    2026-04-15 19:24:32,263 | INFO | Skipping weekly_record_1186_2013-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,264 | INFO | Processing file: weekly_record_1187_2013-09-16T00-00-00.000.json
    2026-04-15 19:24:32,288 | INFO | Skipping weekly_record_1187_2013-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,289 | INFO | Processing file: weekly_record_1188_2013-09-23T00-00-00.000.json
    2026-04-15 19:24:32,306 | INFO | Skipping weekly_record_1188_2013-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,307 | INFO | Processing file: weekly_record_1189_2013-09-30T00-00-00.000.json
    2026-04-15 19:24:32,335 | INFO | Skipping weekly_record_1189_2013-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,335 | INFO | Processing file: weekly_record_118_1993-03-22T00-00-00.000.json
    2026-04-15 19:24:32,352 | INFO | Skipping weekly_record_118_1993-03-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,352 | INFO | Processing file: weekly_record_1190_2013-10-07T00-00-00.000.json
    2026-04-15 19:24:32,382 | INFO | Skipping weekly_record_1190_2013-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,382 | INFO | Processing file: weekly_record_1191_2013-10-14T00-00-00.000.json
    2026-04-15 19:24:32,399 | INFO | Skipping weekly_record_1191_2013-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,400 | INFO | Processing file: weekly_record_1192_2013-10-21T00-00-00.000.json
    2026-04-15 19:24:32,426 | INFO | Skipping weekly_record_1192_2013-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,427 | INFO | Processing file: weekly_record_1193_2013-10-28T00-00-00.000.json
    2026-04-15 19:24:32,448 | INFO | Skipping weekly_record_1193_2013-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,449 | INFO | Processing file: weekly_record_1194_2013-11-04T00-00-00.000.json
    2026-04-15 19:24:32,471 | INFO | Skipping weekly_record_1194_2013-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,472 | INFO | Processing file: weekly_record_1195_2013-11-11T00-00-00.000.json
    2026-04-15 19:24:32,499 | INFO | Skipping weekly_record_1195_2013-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,500 | INFO | Processing file: weekly_record_1196_2013-11-18T00-00-00.000.json
    2026-04-15 19:24:32,525 | INFO | Skipping weekly_record_1196_2013-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,525 | INFO | Processing file: weekly_record_1197_2013-11-25T00-00-00.000.json
    2026-04-15 19:24:32,551 | INFO | Skipping weekly_record_1197_2013-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,552 | INFO | Processing file: weekly_record_1198_2013-12-02T00-00-00.000.json
    2026-04-15 19:24:32,576 | INFO | Skipping weekly_record_1198_2013-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,577 | INFO | Processing file: weekly_record_1199_2013-12-09T00-00-00.000.json
    2026-04-15 19:24:32,648 | INFO | Skipping weekly_record_1199_2013-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,649 | INFO | Processing file: weekly_record_119_1993-03-29T00-00-00.000.json
    2026-04-15 19:24:32,670 | INFO | Skipping weekly_record_119_1993-03-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,671 | INFO | Processing file: weekly_record_11_1991-03-04T00-00-00.000.json
    2026-04-15 19:24:32,692 | INFO | Skipping weekly_record_11_1991-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,693 | INFO | Processing file: weekly_record_1200_2013-12-16T00-00-00.000.json
    2026-04-15 19:24:32,712 | INFO | Skipping weekly_record_1200_2013-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,713 | INFO | Processing file: weekly_record_1201_2013-12-23T00-00-00.000.json
    2026-04-15 19:24:32,737 | INFO | Skipping weekly_record_1201_2013-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,738 | INFO | Processing file: weekly_record_1202_2013-12-30T00-00-00.000.json
    2026-04-15 19:24:32,756 | INFO | Skipping weekly_record_1202_2013-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,757 | INFO | Processing file: weekly_record_1203_2014-01-06T00-00-00.000.json
    2026-04-15 19:24:32,783 | INFO | Skipping weekly_record_1203_2014-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,784 | INFO | Processing file: weekly_record_1204_2014-01-13T00-00-00.000.json
    2026-04-15 19:24:32,799 | INFO | Skipping weekly_record_1204_2014-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,800 | INFO | Processing file: weekly_record_1205_2014-01-20T00-00-00.000.json
    2026-04-15 19:24:32,827 | INFO | Skipping weekly_record_1205_2014-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,828 | INFO | Processing file: weekly_record_1206_2014-01-27T00-00-00.000.json
    2026-04-15 19:24:32,843 | INFO | Skipping weekly_record_1206_2014-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,844 | INFO | Processing file: weekly_record_1207_2014-02-03T00-00-00.000.json
    2026-04-15 19:24:32,874 | INFO | Skipping weekly_record_1207_2014-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,874 | INFO | Processing file: weekly_record_1208_2014-02-10T00-00-00.000.json
    2026-04-15 19:24:32,898 | INFO | Skipping weekly_record_1208_2014-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,900 | INFO | Processing file: weekly_record_1209_2014-02-17T00-00-00.000.json
    2026-04-15 19:24:32,925 | INFO | Skipping weekly_record_1209_2014-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,926 | INFO | Processing file: weekly_record_120_1993-04-05T00-00-00.000.json
    2026-04-15 19:24:32,943 | INFO | Skipping weekly_record_120_1993-04-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,944 | INFO | Processing file: weekly_record_1210_2014-02-24T00-00-00.000.json
    2026-04-15 19:24:32,972 | INFO | Skipping weekly_record_1210_2014-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,973 | INFO | Processing file: weekly_record_1211_2014-03-03T00-00-00.000.json
    2026-04-15 19:24:32,988 | INFO | Skipping weekly_record_1211_2014-03-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:32,989 | INFO | Processing file: weekly_record_1212_2014-03-10T00-00-00.000.json
    2026-04-15 19:24:33,019 | INFO | Skipping weekly_record_1212_2014-03-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,020 | INFO | Processing file: weekly_record_1213_2014-03-17T00-00-00.000.json
    2026-04-15 19:24:33,041 | INFO | Skipping weekly_record_1213_2014-03-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,041 | INFO | Processing file: weekly_record_1214_2014-03-24T00-00-00.000.json
    2026-04-15 19:24:33,070 | INFO | Skipping weekly_record_1214_2014-03-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,071 | INFO | Processing file: weekly_record_1215_2014-03-31T00-00-00.000.json
    2026-04-15 19:24:33,089 | INFO | Skipping weekly_record_1215_2014-03-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,090 | INFO | Processing file: weekly_record_1216_2014-04-07T00-00-00.000.json
    2026-04-15 19:24:33,109 | INFO | Skipping weekly_record_1216_2014-04-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,110 | INFO | Processing file: weekly_record_1217_2014-04-14T00-00-00.000.json
    2026-04-15 19:24:33,131 | INFO | Skipping weekly_record_1217_2014-04-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,132 | INFO | Processing file: weekly_record_1218_2014-04-21T00-00-00.000.json
    2026-04-15 19:24:33,159 | INFO | Skipping weekly_record_1218_2014-04-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,160 | INFO | Processing file: weekly_record_1219_2014-04-28T00-00-00.000.json
    2026-04-15 19:24:33,177 | INFO | Skipping weekly_record_1219_2014-04-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,178 | INFO | Processing file: weekly_record_121_1993-04-12T00-00-00.000.json
    2026-04-15 19:24:33,203 | INFO | Skipping weekly_record_121_1993-04-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,204 | INFO | Processing file: weekly_record_1220_2014-05-05T00-00-00.000.json
    2026-04-15 19:24:33,218 | INFO | Skipping weekly_record_1220_2014-05-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,219 | INFO | Processing file: weekly_record_1221_2014-05-12T00-00-00.000.json
    2026-04-15 19:24:33,246 | INFO | Skipping weekly_record_1221_2014-05-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,246 | INFO | Processing file: weekly_record_1222_2014-05-19T00-00-00.000.json
    2026-04-15 19:24:33,273 | INFO | Skipping weekly_record_1222_2014-05-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,273 | INFO | Processing file: weekly_record_1223_2014-05-26T00-00-00.000.json
    2026-04-15 19:24:33,298 | INFO | Skipping weekly_record_1223_2014-05-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,299 | INFO | Processing file: weekly_record_1224_2014-06-02T00-00-00.000.json
    2026-04-15 19:24:33,316 | INFO | Skipping weekly_record_1224_2014-06-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,322 | INFO | Processing file: weekly_record_1225_2014-06-09T00-00-00.000.json
    2026-04-15 19:24:33,344 | INFO | Skipping weekly_record_1225_2014-06-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,345 | INFO | Processing file: weekly_record_1226_2014-06-16T00-00-00.000.json
    2026-04-15 19:24:33,363 | INFO | Skipping weekly_record_1226_2014-06-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,364 | INFO | Processing file: weekly_record_1227_2014-06-23T00-00-00.000.json
    2026-04-15 19:24:33,388 | INFO | Skipping weekly_record_1227_2014-06-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,388 | INFO | Processing file: weekly_record_1228_2014-06-30T00-00-00.000.json
    2026-04-15 19:24:33,406 | INFO | Skipping weekly_record_1228_2014-06-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,407 | INFO | Processing file: weekly_record_1229_2014-07-07T00-00-00.000.json
    2026-04-15 19:24:33,433 | INFO | Skipping weekly_record_1229_2014-07-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,434 | INFO | Processing file: weekly_record_122_1993-04-19T00-00-00.000.json
    2026-04-15 19:24:33,463 | INFO | Skipping weekly_record_122_1993-04-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,464 | INFO | Processing file: weekly_record_1230_2014-07-14T00-00-00.000.json
    2026-04-15 19:24:33,487 | INFO | Skipping weekly_record_1230_2014-07-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,488 | INFO | Processing file: weekly_record_1231_2014-07-21T00-00-00.000.json
    2026-04-15 19:24:33,505 | INFO | Skipping weekly_record_1231_2014-07-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,506 | INFO | Processing file: weekly_record_1232_2014-07-28T00-00-00.000.json
    2026-04-15 19:24:33,531 | INFO | Skipping weekly_record_1232_2014-07-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,532 | INFO | Processing file: weekly_record_1233_2014-08-04T00-00-00.000.json
    2026-04-15 19:24:33,549 | INFO | Skipping weekly_record_1233_2014-08-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,550 | INFO | Processing file: weekly_record_1234_2014-08-11T00-00-00.000.json
    2026-04-15 19:24:33,578 | INFO | Skipping weekly_record_1234_2014-08-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,579 | INFO | Processing file: weekly_record_1235_2014-08-18T00-00-00.000.json
    2026-04-15 19:24:33,595 | INFO | Skipping weekly_record_1235_2014-08-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,596 | INFO | Processing file: weekly_record_1236_2014-08-25T00-00-00.000.json
    2026-04-15 19:24:33,631 | INFO | Skipping weekly_record_1236_2014-08-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,631 | INFO | Processing file: weekly_record_1237_2014-09-01T00-00-00.000.json
    2026-04-15 19:24:33,648 | INFO | Skipping weekly_record_1237_2014-09-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,649 | INFO | Processing file: weekly_record_1238_2014-09-08T00-00-00.000.json
    2026-04-15 19:24:33,679 | INFO | Skipping weekly_record_1238_2014-09-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,680 | INFO | Processing file: weekly_record_1239_2014-09-15T00-00-00.000.json
    2026-04-15 19:24:33,694 | INFO | Skipping weekly_record_1239_2014-09-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,695 | INFO | Processing file: weekly_record_123_1993-04-26T00-00-00.000.json
    2026-04-15 19:24:33,721 | INFO | Skipping weekly_record_123_1993-04-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,722 | INFO | Processing file: weekly_record_1240_2014-09-22T00-00-00.000.json
    2026-04-15 19:24:33,739 | INFO | Skipping weekly_record_1240_2014-09-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,740 | INFO | Processing file: weekly_record_1241_2014-09-29T00-00-00.000.json
    2026-04-15 19:24:33,766 | INFO | Skipping weekly_record_1241_2014-09-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,767 | INFO | Processing file: weekly_record_1242_2014-10-06T00-00-00.000.json
    2026-04-15 19:24:33,793 | INFO | Skipping weekly_record_1242_2014-10-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,795 | INFO | Processing file: weekly_record_1243_2014-10-13T00-00-00.000.json
    2026-04-15 19:24:33,820 | INFO | Skipping weekly_record_1243_2014-10-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,821 | INFO | Processing file: weekly_record_1244_2014-10-20T00-00-00.000.json
    2026-04-15 19:24:33,838 | INFO | Skipping weekly_record_1244_2014-10-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,839 | INFO | Processing file: weekly_record_1245_2014-10-27T00-00-00.000.json
    2026-04-15 19:24:33,865 | INFO | Skipping weekly_record_1245_2014-10-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,866 | INFO | Processing file: weekly_record_1246_2014-11-03T00-00-00.000.json
    2026-04-15 19:24:33,885 | INFO | Skipping weekly_record_1246_2014-11-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,886 | INFO | Processing file: weekly_record_1247_2014-11-10T00-00-00.000.json
    2026-04-15 19:24:33,911 | INFO | Skipping weekly_record_1247_2014-11-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,912 | INFO | Processing file: weekly_record_1248_2014-11-17T00-00-00.000.json
    2026-04-15 19:24:33,930 | INFO | Skipping weekly_record_1248_2014-11-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,930 | INFO | Processing file: weekly_record_1249_2014-11-24T00-00-00.000.json
    2026-04-15 19:24:33,956 | INFO | Skipping weekly_record_1249_2014-11-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,957 | INFO | Processing file: weekly_record_124_1993-05-03T00-00-00.000.json
    2026-04-15 19:24:33,972 | INFO | Skipping weekly_record_124_1993-05-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:33,973 | INFO | Processing file: weekly_record_1250_2014-12-01T00-00-00.000.json
    2026-04-15 19:24:33,999 | INFO | Skipping weekly_record_1250_2014-12-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,000 | INFO | Processing file: weekly_record_1251_2014-12-08T00-00-00.000.json
    2026-04-15 19:24:34,017 | INFO | Skipping weekly_record_1251_2014-12-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,018 | INFO | Processing file: weekly_record_1252_2014-12-15T00-00-00.000.json
    2026-04-15 19:24:34,044 | INFO | Skipping weekly_record_1252_2014-12-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,044 | INFO | Processing file: weekly_record_1253_2014-12-22T00-00-00.000.json
    2026-04-15 19:24:34,061 | INFO | Skipping weekly_record_1253_2014-12-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,062 | INFO | Processing file: weekly_record_1254_2014-12-29T00-00-00.000.json
    2026-04-15 19:24:34,089 | INFO | Skipping weekly_record_1254_2014-12-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,090 | INFO | Processing file: weekly_record_1255_2015-01-05T00-00-00.000.json
    2026-04-15 19:24:34,106 | INFO | Skipping weekly_record_1255_2015-01-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,107 | INFO | Processing file: weekly_record_1256_2015-01-12T00-00-00.000.json
    2026-04-15 19:24:34,138 | INFO | Skipping weekly_record_1256_2015-01-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,166 | INFO | Processing file: weekly_record_1257_2015-01-19T00-00-00.000.json
    2026-04-15 19:24:34,190 | INFO | Skipping weekly_record_1257_2015-01-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,191 | INFO | Processing file: weekly_record_1258_2015-01-26T00-00-00.000.json
    2026-04-15 19:24:34,215 | INFO | Skipping weekly_record_1258_2015-01-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,216 | INFO | Processing file: weekly_record_1259_2015-02-02T00-00-00.000.json
    2026-04-15 19:24:34,234 | INFO | Skipping weekly_record_1259_2015-02-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,234 | INFO | Processing file: weekly_record_125_1993-05-10T00-00-00.000.json
    2026-04-15 19:24:34,262 | INFO | Skipping weekly_record_125_1993-05-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,263 | INFO | Processing file: weekly_record_1260_2015-02-09T00-00-00.000.json
    2026-04-15 19:24:34,279 | INFO | Skipping weekly_record_1260_2015-02-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,279 | INFO | Processing file: weekly_record_1261_2015-02-16T00-00-00.000.json
    2026-04-15 19:24:34,305 | INFO | Skipping weekly_record_1261_2015-02-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,306 | INFO | Processing file: weekly_record_1262_2015-02-23T00-00-00.000.json
    2026-04-15 19:24:34,338 | INFO | Skipping weekly_record_1262_2015-02-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,338 | INFO | Processing file: weekly_record_1263_2015-03-02T00-00-00.000.json
    2026-04-15 19:24:34,366 | INFO | Skipping weekly_record_1263_2015-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,367 | INFO | Processing file: weekly_record_1264_2015-03-09T00-00-00.000.json
    2026-04-15 19:24:34,384 | INFO | Skipping weekly_record_1264_2015-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,385 | INFO | Processing file: weekly_record_1265_2015-03-16T00-00-00.000.json
    2026-04-15 19:24:34,411 | INFO | Skipping weekly_record_1265_2015-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,412 | INFO | Processing file: weekly_record_1266_2015-03-23T00-00-00.000.json
    2026-04-15 19:24:34,429 | INFO | Skipping weekly_record_1266_2015-03-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,430 | INFO | Processing file: weekly_record_1267_2015-03-30T00-00-00.000.json
    2026-04-15 19:24:34,455 | INFO | Skipping weekly_record_1267_2015-03-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,456 | INFO | Processing file: weekly_record_1268_2015-04-06T00-00-00.000.json
    2026-04-15 19:24:34,473 | INFO | Skipping weekly_record_1268_2015-04-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,474 | INFO | Processing file: weekly_record_1269_2015-04-13T00-00-00.000.json
    2026-04-15 19:24:34,498 | INFO | Skipping weekly_record_1269_2015-04-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,499 | INFO | Processing file: weekly_record_126_1993-05-17T00-00-00.000.json
    2026-04-15 19:24:34,515 | INFO | Skipping weekly_record_126_1993-05-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,516 | INFO | Processing file: weekly_record_1270_2015-04-20T00-00-00.000.json
    2026-04-15 19:24:34,543 | INFO | Skipping weekly_record_1270_2015-04-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,543 | INFO | Processing file: weekly_record_1271_2015-04-27T00-00-00.000.json
    2026-04-15 19:24:34,569 | INFO | Skipping weekly_record_1271_2015-04-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,571 | INFO | Processing file: weekly_record_1272_2015-05-04T00-00-00.000.json
    2026-04-15 19:24:34,593 | INFO | Skipping weekly_record_1272_2015-05-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,594 | INFO | Processing file: weekly_record_1273_2015-05-11T00-00-00.000.json
    2026-04-15 19:24:34,624 | INFO | Skipping weekly_record_1273_2015-05-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,626 | INFO | Processing file: weekly_record_1274_2015-05-18T00-00-00.000.json
    2026-04-15 19:24:34,655 | INFO | Skipping weekly_record_1274_2015-05-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,656 | INFO | Processing file: weekly_record_1275_2015-05-25T00-00-00.000.json
    2026-04-15 19:24:34,685 | INFO | Skipping weekly_record_1275_2015-05-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,685 | INFO | Processing file: weekly_record_1276_2015-06-01T00-00-00.000.json
    2026-04-15 19:24:34,704 | INFO | Skipping weekly_record_1276_2015-06-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,704 | INFO | Processing file: weekly_record_1277_2015-06-08T00-00-00.000.json
    2026-04-15 19:24:34,728 | INFO | Skipping weekly_record_1277_2015-06-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,729 | INFO | Processing file: weekly_record_1278_2015-06-15T00-00-00.000.json
    2026-04-15 19:24:34,745 | INFO | Skipping weekly_record_1278_2015-06-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,746 | INFO | Processing file: weekly_record_1279_2015-06-22T00-00-00.000.json
    2026-04-15 19:24:34,773 | INFO | Skipping weekly_record_1279_2015-06-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,774 | INFO | Processing file: weekly_record_127_1993-05-24T00-00-00.000.json
    2026-04-15 19:24:34,794 | INFO | Skipping weekly_record_127_1993-05-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,795 | INFO | Processing file: weekly_record_1280_2015-06-29T00-00-00.000.json
    2026-04-15 19:24:34,818 | INFO | Skipping weekly_record_1280_2015-06-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,818 | INFO | Processing file: weekly_record_1281_2015-07-06T00-00-00.000.json
    2026-04-15 19:24:34,836 | INFO | Skipping weekly_record_1281_2015-07-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,836 | INFO | Processing file: weekly_record_1282_2015-07-13T00-00-00.000.json
    2026-04-15 19:24:34,854 | INFO | Skipping weekly_record_1282_2015-07-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,854 | INFO | Processing file: weekly_record_1283_2015-07-20T00-00-00.000.json
    2026-04-15 19:24:34,881 | INFO | Skipping weekly_record_1283_2015-07-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,882 | INFO | Processing file: weekly_record_1284_2015-07-27T00-00-00.000.json
    2026-04-15 19:24:34,908 | INFO | Skipping weekly_record_1284_2015-07-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,909 | INFO | Processing file: weekly_record_1285_2015-08-03T00-00-00.000.json
    2026-04-15 19:24:34,925 | INFO | Skipping weekly_record_1285_2015-08-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,926 | INFO | Processing file: weekly_record_1286_2015-08-10T00-00-00.000.json
    2026-04-15 19:24:34,953 | INFO | Skipping weekly_record_1286_2015-08-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,954 | INFO | Processing file: weekly_record_1287_2015-08-17T00-00-00.000.json
    2026-04-15 19:24:34,977 | INFO | Skipping weekly_record_1287_2015-08-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:34,978 | INFO | Processing file: weekly_record_1288_2015-08-24T00-00-00.000.json
    2026-04-15 19:24:35,002 | INFO | Skipping weekly_record_1288_2015-08-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,002 | INFO | Processing file: weekly_record_1289_2015-08-31T00-00-00.000.json
    2026-04-15 19:24:35,021 | INFO | Skipping weekly_record_1289_2015-08-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,021 | INFO | Processing file: weekly_record_128_1993-05-31T00-00-00.000.json
    2026-04-15 19:24:35,047 | INFO | Skipping weekly_record_128_1993-05-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,047 | INFO | Processing file: weekly_record_1290_2015-09-07T00-00-00.000.json
    2026-04-15 19:24:35,066 | INFO | Skipping weekly_record_1290_2015-09-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,066 | INFO | Processing file: weekly_record_1291_2015-09-14T00-00-00.000.json
    2026-04-15 19:24:35,088 | INFO | Skipping weekly_record_1291_2015-09-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,088 | INFO | Processing file: weekly_record_1292_2015-09-21T00-00-00.000.json
    2026-04-15 19:24:35,106 | INFO | Skipping weekly_record_1292_2015-09-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,107 | INFO | Processing file: weekly_record_1293_2015-09-28T00-00-00.000.json
    2026-04-15 19:24:35,138 | INFO | Skipping weekly_record_1293_2015-09-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,138 | INFO | Processing file: weekly_record_1294_2015-10-05T00-00-00.000.json
    2026-04-15 19:24:35,156 | INFO | Skipping weekly_record_1294_2015-10-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,156 | INFO | Processing file: weekly_record_1295_2015-10-12T00-00-00.000.json
    2026-04-15 19:24:35,184 | INFO | Skipping weekly_record_1295_2015-10-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,186 | INFO | Processing file: weekly_record_1296_2015-10-19T00-00-00.000.json
    2026-04-15 19:24:35,201 | INFO | Skipping weekly_record_1296_2015-10-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,203 | INFO | Processing file: weekly_record_1297_2015-10-26T00-00-00.000.json
    2026-04-15 19:24:35,224 | INFO | Skipping weekly_record_1297_2015-10-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,225 | INFO | Processing file: weekly_record_1298_2015-11-02T00-00-00.000.json
    2026-04-15 19:24:35,243 | INFO | Skipping weekly_record_1298_2015-11-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,244 | INFO | Processing file: weekly_record_1299_2015-11-09T00-00-00.000.json
    2026-04-15 19:24:35,299 | INFO | Skipping weekly_record_1299_2015-11-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,300 | INFO | Processing file: weekly_record_129_1993-06-07T00-00-00.000.json
    2026-04-15 19:24:35,319 | INFO | Skipping weekly_record_129_1993-06-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,320 | INFO | Processing file: weekly_record_12_1991-03-11T00-00-00.000.json
    2026-04-15 19:24:35,347 | INFO | Skipping weekly_record_12_1991-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,348 | INFO | Processing file: weekly_record_1300_2015-11-16T00-00-00.000.json
    2026-04-15 19:24:35,365 | INFO | Skipping weekly_record_1300_2015-11-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,366 | INFO | Processing file: weekly_record_1301_2015-11-23T00-00-00.000.json
    2026-04-15 19:24:35,386 | INFO | Skipping weekly_record_1301_2015-11-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,387 | INFO | Processing file: weekly_record_1302_2015-11-30T00-00-00.000.json
    2026-04-15 19:24:35,402 | INFO | Skipping weekly_record_1302_2015-11-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,404 | INFO | Processing file: weekly_record_1303_2015-12-07T00-00-00.000.json
    2026-04-15 19:24:35,429 | INFO | Skipping weekly_record_1303_2015-12-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,431 | INFO | Processing file: weekly_record_1304_2015-12-14T00-00-00.000.json
    2026-04-15 19:24:35,448 | INFO | Skipping weekly_record_1304_2015-12-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,449 | INFO | Processing file: weekly_record_1305_2015-12-21T00-00-00.000.json
    2026-04-15 19:24:35,542 | INFO | Skipping weekly_record_1305_2015-12-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,542 | INFO | Processing file: weekly_record_1306_2015-12-28T00-00-00.000.json
    2026-04-15 19:24:35,570 | INFO | Skipping weekly_record_1306_2015-12-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,571 | INFO | Processing file: weekly_record_1307_2016-01-04T00-00-00.000.json
    2026-04-15 19:24:35,593 | INFO | Skipping weekly_record_1307_2016-01-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,594 | INFO | Processing file: weekly_record_1308_2016-01-11T00-00-00.000.json
    2026-04-15 19:24:35,622 | INFO | Skipping weekly_record_1308_2016-01-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,623 | INFO | Processing file: weekly_record_1309_2016-01-18T00-00-00.000.json
    2026-04-15 19:24:35,638 | INFO | Skipping weekly_record_1309_2016-01-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,639 | INFO | Processing file: weekly_record_130_1993-06-14T00-00-00.000.json
    2026-04-15 19:24:35,667 | INFO | Skipping weekly_record_130_1993-06-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,668 | INFO | Processing file: weekly_record_1310_2016-01-25T00-00-00.000.json
    2026-04-15 19:24:35,691 | INFO | Skipping weekly_record_1310_2016-01-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,692 | INFO | Processing file: weekly_record_1311_2016-02-01T00-00-00.000.json
    2026-04-15 19:24:35,719 | INFO | Skipping weekly_record_1311_2016-02-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,719 | INFO | Processing file: weekly_record_1312_2016-02-08T00-00-00.000.json
    2026-04-15 19:24:35,750 | INFO | Skipping weekly_record_1312_2016-02-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,751 | INFO | Processing file: weekly_record_1313_2016-02-15T00-00-00.000.json
    2026-04-15 19:24:35,777 | INFO | Skipping weekly_record_1313_2016-02-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,778 | INFO | Processing file: weekly_record_1314_2016-02-22T00-00-00.000.json
    2026-04-15 19:24:35,794 | INFO | Skipping weekly_record_1314_2016-02-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,795 | INFO | Processing file: weekly_record_1315_2016-02-29T00-00-00.000.json
    2026-04-15 19:24:35,822 | INFO | Skipping weekly_record_1315_2016-02-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,822 | INFO | Processing file: weekly_record_1316_2016-03-07T00-00-00.000.json
    2026-04-15 19:24:35,845 | INFO | Skipping weekly_record_1316_2016-03-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,846 | INFO | Processing file: weekly_record_1317_2016-03-14T00-00-00.000.json
    2026-04-15 19:24:35,871 | INFO | Skipping weekly_record_1317_2016-03-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,872 | INFO | Processing file: weekly_record_1318_2016-03-21T00-00-00.000.json
    2026-04-15 19:24:35,890 | INFO | Skipping weekly_record_1318_2016-03-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,891 | INFO | Processing file: weekly_record_1319_2016-03-28T00-00-00.000.json
    2026-04-15 19:24:35,917 | INFO | Skipping weekly_record_1319_2016-03-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,918 | INFO | Processing file: weekly_record_131_1993-06-21T00-00-00.000.json
    2026-04-15 19:24:35,933 | INFO | Skipping weekly_record_131_1993-06-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,934 | INFO | Processing file: weekly_record_1320_2016-04-04T00-00-00.000.json
    2026-04-15 19:24:35,960 | INFO | Skipping weekly_record_1320_2016-04-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,961 | INFO | Processing file: weekly_record_1321_2016-04-11T00-00-00.000.json
    2026-04-15 19:24:35,978 | INFO | Skipping weekly_record_1321_2016-04-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:35,979 | INFO | Processing file: weekly_record_1322_2016-04-18T00-00-00.000.json
    2026-04-15 19:24:36,003 | INFO | Skipping weekly_record_1322_2016-04-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,004 | INFO | Processing file: weekly_record_1323_2016-04-25T00-00-00.000.json
    2026-04-15 19:24:36,021 | INFO | Skipping weekly_record_1323_2016-04-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,022 | INFO | Processing file: weekly_record_1324_2016-05-02T00-00-00.000.json
    2026-04-15 19:24:36,050 | INFO | Skipping weekly_record_1324_2016-05-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,050 | INFO | Processing file: weekly_record_1325_2016-05-09T00-00-00.000.json
    2026-04-15 19:24:36,070 | INFO | Skipping weekly_record_1325_2016-05-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,071 | INFO | Processing file: weekly_record_1326_2016-05-16T00-00-00.000.json
    2026-04-15 19:24:36,098 | INFO | Skipping weekly_record_1326_2016-05-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,099 | INFO | Processing file: weekly_record_1327_2016-05-23T00-00-00.000.json
    2026-04-15 19:24:36,127 | INFO | Skipping weekly_record_1327_2016-05-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,148 | INFO | Processing file: weekly_record_1328_2016-05-30T00-00-00.000.json
    2026-04-15 19:24:36,166 | INFO | Skipping weekly_record_1328_2016-05-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,167 | INFO | Processing file: weekly_record_1329_2016-06-06T00-00-00.000.json
    2026-04-15 19:24:36,193 | INFO | Skipping weekly_record_1329_2016-06-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,194 | INFO | Processing file: weekly_record_132_1993-06-28T00-00-00.000.json
    2026-04-15 19:24:36,211 | INFO | Skipping weekly_record_132_1993-06-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,212 | INFO | Processing file: weekly_record_1330_2016-06-13T00-00-00.000.json
    2026-04-15 19:24:36,239 | INFO | Skipping weekly_record_1330_2016-06-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,240 | INFO | Processing file: weekly_record_1331_2016-06-20T00-00-00.000.json
    2026-04-15 19:24:36,259 | INFO | Skipping weekly_record_1331_2016-06-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,259 | INFO | Processing file: weekly_record_1332_2016-06-27T00-00-00.000.json
    2026-04-15 19:24:36,281 | INFO | Skipping weekly_record_1332_2016-06-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,281 | INFO | Processing file: weekly_record_1333_2016-07-04T00-00-00.000.json
    2026-04-15 19:24:36,305 | INFO | Skipping weekly_record_1333_2016-07-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,305 | INFO | Processing file: weekly_record_1334_2016-07-11T00-00-00.000.json
    2026-04-15 19:24:36,323 | INFO | Skipping weekly_record_1334_2016-07-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,324 | INFO | Processing file: weekly_record_1335_2016-07-18T00-00-00.000.json
    2026-04-15 19:24:36,351 | INFO | Skipping weekly_record_1335_2016-07-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,352 | INFO | Processing file: weekly_record_1336_2016-07-25T00-00-00.000.json
    2026-04-15 19:24:36,368 | INFO | Skipping weekly_record_1336_2016-07-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,369 | INFO | Processing file: weekly_record_1337_2016-08-01T00-00-00.000.json
    2026-04-15 19:24:36,396 | INFO | Skipping weekly_record_1337_2016-08-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,396 | INFO | Processing file: weekly_record_1338_2016-08-08T00-00-00.000.json
    2026-04-15 19:24:36,412 | INFO | Skipping weekly_record_1338_2016-08-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,413 | INFO | Processing file: weekly_record_1339_2016-08-15T00-00-00.000.json
    2026-04-15 19:24:36,442 | INFO | Skipping weekly_record_1339_2016-08-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,443 | INFO | Processing file: weekly_record_133_1993-07-05T00-00-00.000.json
    2026-04-15 19:24:36,459 | INFO | Skipping weekly_record_133_1993-07-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,459 | INFO | Processing file: weekly_record_1340_2016-08-22T00-00-00.000.json
    2026-04-15 19:24:36,487 | INFO | Skipping weekly_record_1340_2016-08-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,487 | INFO | Processing file: weekly_record_1341_2016-08-29T00-00-00.000.json
    2026-04-15 19:24:36,504 | INFO | Skipping weekly_record_1341_2016-08-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,505 | INFO | Processing file: weekly_record_1342_2016-09-05T00-00-00.000.json
    2026-04-15 19:24:36,531 | INFO | Skipping weekly_record_1342_2016-09-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,532 | INFO | Processing file: weekly_record_1343_2016-09-12T00-00-00.000.json
    2026-04-15 19:24:36,548 | INFO | Skipping weekly_record_1343_2016-09-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,548 | INFO | Processing file: weekly_record_1344_2016-09-19T00-00-00.000.json
    2026-04-15 19:24:36,590 | INFO | Skipping weekly_record_1344_2016-09-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,590 | INFO | Processing file: weekly_record_1345_2016-09-26T00-00-00.000.json
    2026-04-15 19:24:36,615 | INFO | Skipping weekly_record_1345_2016-09-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,617 | INFO | Processing file: weekly_record_1346_2016-10-03T00-00-00.000.json
    2026-04-15 19:24:36,633 | INFO | Skipping weekly_record_1346_2016-10-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,634 | INFO | Processing file: weekly_record_1347_2016-10-10T00-00-00.000.json
    2026-04-15 19:24:36,660 | INFO | Skipping weekly_record_1347_2016-10-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,661 | INFO | Processing file: weekly_record_1348_2016-10-17T00-00-00.000.json
    2026-04-15 19:24:36,697 | INFO | Skipping weekly_record_1348_2016-10-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,697 | INFO | Processing file: weekly_record_1349_2016-10-24T00-00-00.000.json
    2026-04-15 19:24:36,719 | INFO | Skipping weekly_record_1349_2016-10-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,720 | INFO | Processing file: weekly_record_134_1993-07-12T00-00-00.000.json
    2026-04-15 19:24:36,737 | INFO | Skipping weekly_record_134_1993-07-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,737 | INFO | Processing file: weekly_record_1350_2016-10-31T00-00-00.000.json
    2026-04-15 19:24:36,764 | INFO | Skipping weekly_record_1350_2016-10-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,765 | INFO | Processing file: weekly_record_1351_2016-11-07T00-00-00.000.json
    2026-04-15 19:24:36,790 | INFO | Skipping weekly_record_1351_2016-11-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,790 | INFO | Processing file: weekly_record_1352_2016-11-14T00-00-00.000.json
    2026-04-15 19:24:36,811 | INFO | Skipping weekly_record_1352_2016-11-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,812 | INFO | Processing file: weekly_record_1353_2016-11-21T00-00-00.000.json
    2026-04-15 19:24:36,828 | INFO | Skipping weekly_record_1353_2016-11-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,828 | INFO | Processing file: weekly_record_1354_2016-11-28T00-00-00.000.json
    2026-04-15 19:24:36,855 | INFO | Skipping weekly_record_1354_2016-11-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,856 | INFO | Processing file: weekly_record_1355_2016-12-05T00-00-00.000.json
    2026-04-15 19:24:36,882 | INFO | Skipping weekly_record_1355_2016-12-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,883 | INFO | Processing file: weekly_record_1356_2016-12-12T00-00-00.000.json
    2026-04-15 19:24:36,895 | INFO | Skipping weekly_record_1356_2016-12-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,896 | INFO | Processing file: weekly_record_1357_2016-12-19T00-00-00.000.json
    2026-04-15 19:24:36,922 | INFO | Skipping weekly_record_1357_2016-12-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,924 | INFO | Processing file: weekly_record_1358_2016-12-26T00-00-00.000.json
    2026-04-15 19:24:36,943 | INFO | Skipping weekly_record_1358_2016-12-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,943 | INFO | Processing file: weekly_record_1359_2017-01-02T00-00-00.000.json
    2026-04-15 19:24:36,967 | INFO | Skipping weekly_record_1359_2017-01-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,968 | INFO | Processing file: weekly_record_135_1993-07-19T00-00-00.000.json
    2026-04-15 19:24:36,986 | INFO | Skipping weekly_record_135_1993-07-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:36,987 | INFO | Processing file: weekly_record_1360_2017-01-09T00-00-00.000.json
    2026-04-15 19:24:37,012 | INFO | Skipping weekly_record_1360_2017-01-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,013 | INFO | Processing file: weekly_record_1361_2017-01-16T00-00-00.000.json
    2026-04-15 19:24:37,039 | INFO | Skipping weekly_record_1361_2017-01-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,040 | INFO | Processing file: weekly_record_1362_2017-01-23T00-00-00.000.json
    2026-04-15 19:24:37,068 | INFO | Skipping weekly_record_1362_2017-01-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,069 | INFO | Processing file: weekly_record_1363_2017-01-30T00-00-00.000.json
    2026-04-15 19:24:37,090 | INFO | Skipping weekly_record_1363_2017-01-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,090 | INFO | Processing file: weekly_record_1364_2017-02-06T00-00-00.000.json
    2026-04-15 19:24:37,107 | INFO | Skipping weekly_record_1364_2017-02-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,107 | INFO | Processing file: weekly_record_1365_2017-02-13T00-00-00.000.json
    2026-04-15 19:24:37,133 | INFO | Skipping weekly_record_1365_2017-02-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,134 | INFO | Processing file: weekly_record_1366_2017-02-20T00-00-00.000.json
    2026-04-15 19:24:37,160 | INFO | Skipping weekly_record_1366_2017-02-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,160 | INFO | Processing file: weekly_record_1367_2017-02-27T00-00-00.000.json
    2026-04-15 19:24:37,176 | INFO | Skipping weekly_record_1367_2017-02-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,177 | INFO | Processing file: weekly_record_1368_2017-03-06T00-00-00.000.json
    2026-04-15 19:24:37,203 | INFO | Skipping weekly_record_1368_2017-03-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,204 | INFO | Processing file: weekly_record_1369_2017-03-13T00-00-00.000.json
    2026-04-15 19:24:37,220 | INFO | Skipping weekly_record_1369_2017-03-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,220 | INFO | Processing file: weekly_record_136_1993-07-26T00-00-00.000.json
    2026-04-15 19:24:37,248 | INFO | Skipping weekly_record_136_1993-07-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,248 | INFO | Processing file: weekly_record_1370_2017-03-20T00-00-00.000.json
    2026-04-15 19:24:37,272 | INFO | Skipping weekly_record_1370_2017-03-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,272 | INFO | Processing file: weekly_record_1371_2017-03-27T00-00-00.000.json
    2026-04-15 19:24:37,297 | INFO | Skipping weekly_record_1371_2017-03-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,297 | INFO | Processing file: weekly_record_1372_2017-04-03T00-00-00.000.json
    2026-04-15 19:24:37,328 | INFO | Skipping weekly_record_1372_2017-04-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,342 | INFO | Processing file: weekly_record_1373_2017-04-10T00-00-00.000.json
    2026-04-15 19:24:37,368 | INFO | Skipping weekly_record_1373_2017-04-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,368 | INFO | Processing file: weekly_record_1374_2017-04-17T00-00-00.000.json
    2026-04-15 19:24:37,394 | INFO | Skipping weekly_record_1374_2017-04-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,395 | INFO | Processing file: weekly_record_1375_2017-04-24T00-00-00.000.json
    2026-04-15 19:24:37,412 | INFO | Skipping weekly_record_1375_2017-04-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,414 | INFO | Processing file: weekly_record_1376_2017-05-01T00-00-00.000.json
    2026-04-15 19:24:37,439 | INFO | Skipping weekly_record_1376_2017-05-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,440 | INFO | Processing file: weekly_record_1377_2017-05-08T00-00-00.000.json
    2026-04-15 19:24:37,466 | INFO | Skipping weekly_record_1377_2017-05-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,467 | INFO | Processing file: weekly_record_1378_2017-05-15T00-00-00.000.json
    2026-04-15 19:24:37,491 | INFO | Skipping weekly_record_1378_2017-05-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,492 | INFO | Processing file: weekly_record_1379_2017-05-22T00-00-00.000.json
    2026-04-15 19:24:37,510 | INFO | Skipping weekly_record_1379_2017-05-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,510 | INFO | Processing file: weekly_record_137_1993-08-02T00-00-00.000.json
    2026-04-15 19:24:37,528 | INFO | Skipping weekly_record_137_1993-08-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,528 | INFO | Processing file: weekly_record_1380_2017-05-29T00-00-00.000.json
    2026-04-15 19:24:37,555 | INFO | Skipping weekly_record_1380_2017-05-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,556 | INFO | Processing file: weekly_record_1381_2017-06-05T00-00-00.000.json
    2026-04-15 19:24:37,571 | INFO | Skipping weekly_record_1381_2017-06-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,572 | INFO | Processing file: weekly_record_1382_2017-06-12T00-00-00.000.json
    2026-04-15 19:24:37,599 | INFO | Skipping weekly_record_1382_2017-06-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,600 | INFO | Processing file: weekly_record_1383_2017-06-19T00-00-00.000.json
    2026-04-15 19:24:37,617 | INFO | Skipping weekly_record_1383_2017-06-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,617 | INFO | Processing file: weekly_record_1384_2017-06-26T00-00-00.000.json
    2026-04-15 19:24:37,645 | INFO | Skipping weekly_record_1384_2017-06-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,646 | INFO | Processing file: weekly_record_1385_2017-07-03T00-00-00.000.json
    2026-04-15 19:24:37,663 | INFO | Skipping weekly_record_1385_2017-07-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,664 | INFO | Processing file: weekly_record_1386_2017-07-10T00-00-00.000.json
    2026-04-15 19:24:37,689 | INFO | Skipping weekly_record_1386_2017-07-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,690 | INFO | Processing file: weekly_record_1387_2017-07-17T00-00-00.000.json
    2026-04-15 19:24:37,706 | INFO | Skipping weekly_record_1387_2017-07-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,707 | INFO | Processing file: weekly_record_1388_2017-07-24T00-00-00.000.json
    2026-04-15 19:24:37,733 | INFO | Skipping weekly_record_1388_2017-07-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,734 | INFO | Processing file: weekly_record_1389_2017-07-31T00-00-00.000.json
    2026-04-15 19:24:37,751 | INFO | Skipping weekly_record_1389_2017-07-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,751 | INFO | Processing file: weekly_record_138_1993-08-09T00-00-00.000.json
    2026-04-15 19:24:37,776 | INFO | Skipping weekly_record_138_1993-08-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,776 | INFO | Processing file: weekly_record_1390_2017-08-07T00-00-00.000.json
    2026-04-15 19:24:37,795 | INFO | Skipping weekly_record_1390_2017-08-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,796 | INFO | Processing file: weekly_record_1391_2017-08-14T00-00-00.000.json
    2026-04-15 19:24:37,820 | INFO | Skipping weekly_record_1391_2017-08-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,820 | INFO | Processing file: weekly_record_1392_2017-08-21T00-00-00.000.json
    2026-04-15 19:24:37,842 | INFO | Skipping weekly_record_1392_2017-08-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,843 | INFO | Processing file: weekly_record_1393_2017-08-28T00-00-00.000.json
    2026-04-15 19:24:37,870 | INFO | Skipping weekly_record_1393_2017-08-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,871 | INFO | Processing file: weekly_record_1394_2017-09-04T00-00-00.000.json
    2026-04-15 19:24:37,901 | INFO | Skipping weekly_record_1394_2017-09-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,902 | INFO | Processing file: weekly_record_1395_2017-09-11T00-00-00.000.json
    2026-04-15 19:24:37,920 | INFO | Skipping weekly_record_1395_2017-09-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,920 | INFO | Processing file: weekly_record_1396_2017-09-18T00-00-00.000.json
    2026-04-15 19:24:37,946 | INFO | Skipping weekly_record_1396_2017-09-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,946 | INFO | Processing file: weekly_record_1397_2017-09-25T00-00-00.000.json
    2026-04-15 19:24:37,972 | INFO | Skipping weekly_record_1397_2017-09-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:37,973 | INFO | Processing file: weekly_record_1398_2017-10-02T00-00-00.000.json
    2026-04-15 19:24:37,999 | INFO | Skipping weekly_record_1398_2017-10-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,000 | INFO | Processing file: weekly_record_1399_2017-10-09T00-00-00.000.json
    2026-04-15 19:24:38,021 | INFO | Skipping weekly_record_1399_2017-10-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,022 | INFO | Processing file: weekly_record_139_1993-08-16T00-00-00.000.json
    2026-04-15 19:24:38,039 | INFO | Skipping weekly_record_139_1993-08-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,039 | INFO | Processing file: weekly_record_13_1991-03-18T00-00-00.000.json
    2026-04-15 19:24:38,067 | INFO | Skipping weekly_record_13_1991-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,067 | INFO | Processing file: weekly_record_1400_2017-10-16T00-00-00.000.json
    2026-04-15 19:24:38,086 | INFO | Skipping weekly_record_1400_2017-10-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,086 | INFO | Processing file: weekly_record_1401_2017-10-23T00-00-00.000.json
    2026-04-15 19:24:38,112 | INFO | Skipping weekly_record_1401_2017-10-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,113 | INFO | Processing file: weekly_record_1402_2017-10-30T00-00-00.000.json
    2026-04-15 19:24:38,138 | INFO | Skipping weekly_record_1402_2017-10-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,138 | INFO | Processing file: weekly_record_1403_2017-11-06T00-00-00.000.json
    2026-04-15 19:24:38,166 | INFO | Skipping weekly_record_1403_2017-11-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,166 | INFO | Processing file: weekly_record_1404_2017-11-13T00-00-00.000.json
    2026-04-15 19:24:38,197 | INFO | Skipping weekly_record_1404_2017-11-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,197 | INFO | Processing file: weekly_record_1405_2017-11-20T00-00-00.000.json
    2026-04-15 19:24:38,219 | INFO | Skipping weekly_record_1405_2017-11-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,220 | INFO | Processing file: weekly_record_1406_2017-11-27T00-00-00.000.json
    2026-04-15 19:24:38,239 | INFO | Skipping weekly_record_1406_2017-11-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,241 | INFO | Processing file: weekly_record_1407_2017-12-04T00-00-00.000.json
    2026-04-15 19:24:38,263 | INFO | Skipping weekly_record_1407_2017-12-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,264 | INFO | Processing file: weekly_record_1408_2017-12-11T00-00-00.000.json
    2026-04-15 19:24:38,280 | INFO | Skipping weekly_record_1408_2017-12-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,280 | INFO | Processing file: weekly_record_1409_2017-12-18T00-00-00.000.json
    2026-04-15 19:24:38,299 | INFO | Skipping weekly_record_1409_2017-12-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,300 | INFO | Processing file: weekly_record_140_1993-08-23T00-00-00.000.json
    2026-04-15 19:24:38,325 | INFO | Skipping weekly_record_140_1993-08-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,326 | INFO | Processing file: weekly_record_1410_2017-12-25T00-00-00.000.json
    2026-04-15 19:24:38,352 | INFO | Skipping weekly_record_1410_2017-12-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,353 | INFO | Processing file: weekly_record_1411_2018-01-01T00-00-00.000.json
    2026-04-15 19:24:38,376 | INFO | Skipping weekly_record_1411_2018-01-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,376 | INFO | Processing file: weekly_record_1412_2018-01-08T00-00-00.000.json
    2026-04-15 19:24:38,393 | INFO | Skipping weekly_record_1412_2018-01-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,394 | INFO | Processing file: weekly_record_1413_2018-01-15T00-00-00.000.json
    2026-04-15 19:24:38,440 | INFO | Skipping weekly_record_1413_2018-01-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,441 | INFO | Processing file: weekly_record_1414_2018-01-22T00-00-00.000.json
    2026-04-15 19:24:38,466 | INFO | Skipping weekly_record_1414_2018-01-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,466 | INFO | Processing file: weekly_record_1415_2018-01-29T00-00-00.000.json
    2026-04-15 19:24:38,490 | INFO | Skipping weekly_record_1415_2018-01-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,491 | INFO | Processing file: weekly_record_1416_2018-02-05T00-00-00.000.json
    2026-04-15 19:24:38,510 | INFO | Skipping weekly_record_1416_2018-02-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,511 | INFO | Processing file: weekly_record_1417_2018-02-12T00-00-00.000.json
    2026-04-15 19:24:38,536 | INFO | Skipping weekly_record_1417_2018-02-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,537 | INFO | Processing file: weekly_record_1418_2018-02-19T00-00-00.000.json
    2026-04-15 19:24:38,554 | INFO | Skipping weekly_record_1418_2018-02-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,555 | INFO | Processing file: weekly_record_1419_2018-02-26T00-00-00.000.json
    2026-04-15 19:24:38,581 | INFO | Skipping weekly_record_1419_2018-02-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,582 | INFO | Processing file: weekly_record_141_1993-08-30T00-00-00.000.json
    2026-04-15 19:24:38,599 | INFO | Skipping weekly_record_141_1993-08-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,599 | INFO | Processing file: weekly_record_1420_2018-03-05T00-00-00.000.json
    2026-04-15 19:24:38,627 | INFO | Skipping weekly_record_1420_2018-03-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,628 | INFO | Processing file: weekly_record_1421_2018-03-12T00-00-00.000.json
    2026-04-15 19:24:38,645 | INFO | Skipping weekly_record_1421_2018-03-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,646 | INFO | Processing file: weekly_record_1422_2018-03-19T00-00-00.000.json
    2026-04-15 19:24:38,681 | INFO | Skipping weekly_record_1422_2018-03-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,686 | INFO | Processing file: weekly_record_1423_2018-03-26T00-00-00.000.json
    2026-04-15 19:24:38,700 | INFO | Skipping weekly_record_1423_2018-03-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,701 | INFO | Processing file: weekly_record_1424_2018-04-02T00-00-00.000.json
    2026-04-15 19:24:38,732 | INFO | Skipping weekly_record_1424_2018-04-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,733 | INFO | Processing file: weekly_record_1425_2018-04-09T00-00-00.000.json
    2026-04-15 19:24:38,757 | INFO | Skipping weekly_record_1425_2018-04-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,758 | INFO | Processing file: weekly_record_1426_2018-04-16T00-00-00.000.json
    2026-04-15 19:24:38,775 | INFO | Skipping weekly_record_1426_2018-04-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,775 | INFO | Processing file: weekly_record_1427_2018-04-23T00-00-00.000.json
    2026-04-15 19:24:38,802 | INFO | Skipping weekly_record_1427_2018-04-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,803 | INFO | Processing file: weekly_record_1428_2018-04-30T00-00-00.000.json
    2026-04-15 19:24:38,825 | INFO | Skipping weekly_record_1428_2018-04-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,827 | INFO | Processing file: weekly_record_1429_2018-05-07T00-00-00.000.json
    2026-04-15 19:24:38,847 | INFO | Skipping weekly_record_1429_2018-05-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,848 | INFO | Processing file: weekly_record_142_1993-09-06T00-00-00.000.json
    2026-04-15 19:24:38,864 | INFO | Skipping weekly_record_142_1993-09-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,866 | INFO | Processing file: weekly_record_1430_2018-05-14T00-00-00.000.json
    2026-04-15 19:24:38,888 | INFO | Skipping weekly_record_1430_2018-05-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,889 | INFO | Processing file: weekly_record_1431_2018-05-21T00-00-00.000.json
    2026-04-15 19:24:38,905 | INFO | Skipping weekly_record_1431_2018-05-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,906 | INFO | Processing file: weekly_record_1432_2018-05-28T00-00-00.000.json
    2026-04-15 19:24:38,932 | INFO | Skipping weekly_record_1432_2018-05-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,932 | INFO | Processing file: weekly_record_1433_2018-06-04T00-00-00.000.json
    2026-04-15 19:24:38,951 | INFO | Skipping weekly_record_1433_2018-06-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,951 | INFO | Processing file: weekly_record_1434_2018-06-11T00-00-00.000.json
    2026-04-15 19:24:38,979 | INFO | Skipping weekly_record_1434_2018-06-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,979 | INFO | Processing file: weekly_record_1435_2018-06-18T00-00-00.000.json
    2026-04-15 19:24:38,997 | INFO | Skipping weekly_record_1435_2018-06-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:38,997 | INFO | Processing file: weekly_record_1436_2018-06-25T00-00-00.000.json
    2026-04-15 19:24:39,023 | INFO | Skipping weekly_record_1436_2018-06-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,024 | INFO | Processing file: weekly_record_1437_2018-07-02T00-00-00.000.json
    2026-04-15 19:24:39,042 | INFO | Skipping weekly_record_1437_2018-07-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,043 | INFO | Processing file: weekly_record_1438_2018-07-09T00-00-00.000.json
    2026-04-15 19:24:39,068 | INFO | Skipping weekly_record_1438_2018-07-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,069 | INFO | Processing file: weekly_record_1439_2018-07-16T00-00-00.000.json
    2026-04-15 19:24:39,095 | INFO | Skipping weekly_record_1439_2018-07-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,096 | INFO | Processing file: weekly_record_143_1993-09-13T00-00-00.000.json
    2026-04-15 19:24:39,115 | INFO | Skipping weekly_record_143_1993-09-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,115 | INFO | Processing file: weekly_record_1440_2018-07-23T00-00-00.000.json
    2026-04-15 19:24:39,140 | INFO | Skipping weekly_record_1440_2018-07-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,141 | INFO | Processing file: weekly_record_1441_2018-07-30T00-00-00.000.json
    2026-04-15 19:24:39,160 | INFO | Skipping weekly_record_1441_2018-07-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,161 | INFO | Processing file: weekly_record_1442_2018-08-06T00-00-00.000.json
    2026-04-15 19:24:39,186 | INFO | Skipping weekly_record_1442_2018-08-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,187 | INFO | Processing file: weekly_record_1443_2018-08-13T00-00-00.000.json
    2026-04-15 19:24:39,212 | INFO | Skipping weekly_record_1443_2018-08-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,212 | INFO | Processing file: weekly_record_1444_2018-08-20T00-00-00.000.json
    2026-04-15 19:24:39,239 | INFO | Skipping weekly_record_1444_2018-08-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,240 | INFO | Processing file: weekly_record_1445_2018-08-27T00-00-00.000.json
    2026-04-15 19:24:39,257 | INFO | Skipping weekly_record_1445_2018-08-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,258 | INFO | Processing file: weekly_record_1446_2018-09-03T00-00-00.000.json
    2026-04-15 19:24:39,279 | INFO | Skipping weekly_record_1446_2018-09-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,279 | INFO | Processing file: weekly_record_1447_2018-09-10T00-00-00.000.json
    2026-04-15 19:24:39,297 | INFO | Skipping weekly_record_1447_2018-09-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,298 | INFO | Processing file: weekly_record_1448_2018-09-17T00-00-00.000.json
    2026-04-15 19:24:39,324 | INFO | Skipping weekly_record_1448_2018-09-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,324 | INFO | Processing file: weekly_record_1449_2018-09-24T00-00-00.000.json
    2026-04-15 19:24:39,342 | INFO | Skipping weekly_record_1449_2018-09-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,343 | INFO | Processing file: weekly_record_144_1993-09-20T00-00-00.000.json
    2026-04-15 19:24:39,369 | INFO | Skipping weekly_record_144_1993-09-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,370 | INFO | Processing file: weekly_record_1450_2018-10-01T00-00-00.000.json
    2026-04-15 19:24:39,387 | INFO | Skipping weekly_record_1450_2018-10-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,388 | INFO | Processing file: weekly_record_1451_2018-10-08T00-00-00.000.json
    2026-04-15 19:24:39,415 | INFO | Skipping weekly_record_1451_2018-10-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,416 | INFO | Processing file: weekly_record_1452_2018-10-15T00-00-00.000.json
    2026-04-15 19:24:39,453 | INFO | Skipping weekly_record_1452_2018-10-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,454 | INFO | Processing file: weekly_record_1453_2018-10-22T00-00-00.000.json
    2026-04-15 19:24:39,471 | INFO | Skipping weekly_record_1453_2018-10-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,471 | INFO | Processing file: weekly_record_1454_2018-10-29T00-00-00.000.json
    2026-04-15 19:24:39,489 | INFO | Skipping weekly_record_1454_2018-10-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,490 | INFO | Processing file: weekly_record_1455_2018-11-05T00-00-00.000.json
    2026-04-15 19:24:39,516 | INFO | Skipping weekly_record_1455_2018-11-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,517 | INFO | Processing file: weekly_record_1456_2018-11-12T00-00-00.000.json
    2026-04-15 19:24:39,538 | INFO | Skipping weekly_record_1456_2018-11-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,538 | INFO | Processing file: weekly_record_1457_2018-11-19T00-00-00.000.json
    2026-04-15 19:24:39,566 | INFO | Skipping weekly_record_1457_2018-11-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,566 | INFO | Processing file: weekly_record_1458_2018-11-26T00-00-00.000.json
    2026-04-15 19:24:39,584 | INFO | Skipping weekly_record_1458_2018-11-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,584 | INFO | Processing file: weekly_record_1459_2018-12-03T00-00-00.000.json
    2026-04-15 19:24:39,612 | INFO | Skipping weekly_record_1459_2018-12-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,612 | INFO | Processing file: weekly_record_145_1993-09-27T00-00-00.000.json
    2026-04-15 19:24:39,633 | INFO | Skipping weekly_record_145_1993-09-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,634 | INFO | Processing file: weekly_record_1460_2018-12-10T00-00-00.000.json
    2026-04-15 19:24:39,652 | INFO | Skipping weekly_record_1460_2018-12-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,653 | INFO | Processing file: weekly_record_1461_2018-12-17T00-00-00.000.json
    2026-04-15 19:24:39,680 | INFO | Skipping weekly_record_1461_2018-12-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,680 | INFO | Processing file: weekly_record_1462_2018-12-24T00-00-00.000.json
    2026-04-15 19:24:39,698 | INFO | Skipping weekly_record_1462_2018-12-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,699 | INFO | Processing file: weekly_record_1463_2018-12-31T00-00-00.000.json
    2026-04-15 19:24:39,723 | INFO | Skipping weekly_record_1463_2018-12-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,724 | INFO | Processing file: weekly_record_1464_2019-01-07T00-00-00.000.json
    2026-04-15 19:24:39,741 | INFO | Skipping weekly_record_1464_2019-01-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,742 | INFO | Processing file: weekly_record_1465_2019-01-14T00-00-00.000.json
    2026-04-15 19:24:39,768 | INFO | Skipping weekly_record_1465_2019-01-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,769 | INFO | Processing file: weekly_record_1466_2019-01-21T00-00-00.000.json
    2026-04-15 19:24:39,789 | INFO | Skipping weekly_record_1466_2019-01-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,790 | INFO | Processing file: weekly_record_1467_2019-01-28T00-00-00.000.json
    2026-04-15 19:24:39,809 | INFO | Skipping weekly_record_1467_2019-01-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,810 | INFO | Processing file: weekly_record_1468_2019-02-04T00-00-00.000.json
    2026-04-15 19:24:39,835 | INFO | Skipping weekly_record_1468_2019-02-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,836 | INFO | Processing file: weekly_record_1469_2019-02-11T00-00-00.000.json
    2026-04-15 19:24:39,865 | INFO | Skipping weekly_record_1469_2019-02-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,867 | INFO | Processing file: weekly_record_146_1993-10-04T00-00-00.000.json
    2026-04-15 19:24:39,880 | INFO | Skipping weekly_record_146_1993-10-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,882 | INFO | Processing file: weekly_record_1470_2019-02-18T00-00-00.000.json
    2026-04-15 19:24:39,907 | INFO | Skipping weekly_record_1470_2019-02-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,907 | INFO | Processing file: weekly_record_1471_2019-02-25T00-00-00.000.json
    2026-04-15 19:24:39,927 | INFO | Skipping weekly_record_1471_2019-02-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,927 | INFO | Processing file: weekly_record_1472_2019-03-04T00-00-00.000.json
    2026-04-15 19:24:39,951 | INFO | Skipping weekly_record_1472_2019-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,951 | INFO | Processing file: weekly_record_1473_2019-03-11T00-00-00.000.json
    2026-04-15 19:24:39,970 | INFO | Skipping weekly_record_1473_2019-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,970 | INFO | Processing file: weekly_record_1474_2019-03-18T00-00-00.000.json
    2026-04-15 19:24:39,998 | INFO | Skipping weekly_record_1474_2019-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:39,999 | INFO | Processing file: weekly_record_1475_2019-03-25T00-00-00.000.json
    2026-04-15 19:24:40,017 | INFO | Skipping weekly_record_1475_2019-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,017 | INFO | Processing file: weekly_record_1476_2019-04-01T00-00-00.000.json
    2026-04-15 19:24:40,044 | INFO | Skipping weekly_record_1476_2019-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,046 | INFO | Processing file: weekly_record_1477_2019-04-08T00-00-00.000.json
    2026-04-15 19:24:40,065 | INFO | Skipping weekly_record_1477_2019-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,065 | INFO | Processing file: weekly_record_1478_2019-04-15T00-00-00.000.json
    2026-04-15 19:24:40,093 | INFO | Skipping weekly_record_1478_2019-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,094 | INFO | Processing file: weekly_record_1479_2019-04-22T00-00-00.000.json
    2026-04-15 19:24:40,119 | INFO | Skipping weekly_record_1479_2019-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,120 | INFO | Processing file: weekly_record_147_1993-10-11T00-00-00.000.json
    2026-04-15 19:24:40,135 | INFO | Skipping weekly_record_147_1993-10-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,136 | INFO | Processing file: weekly_record_1480_2019-04-29T00-00-00.000.json
    2026-04-15 19:24:40,162 | INFO | Skipping weekly_record_1480_2019-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,163 | INFO | Processing file: weekly_record_1481_2019-05-06T00-00-00.000.json
    2026-04-15 19:24:40,185 | INFO | Skipping weekly_record_1481_2019-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,186 | INFO | Processing file: weekly_record_1482_2019-05-13T00-00-00.000.json
    2026-04-15 19:24:40,211 | INFO | Skipping weekly_record_1482_2019-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,212 | INFO | Processing file: weekly_record_1483_2019-05-20T00-00-00.000.json
    2026-04-15 19:24:40,242 | INFO | Skipping weekly_record_1483_2019-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,243 | INFO | Processing file: weekly_record_1484_2019-05-27T00-00-00.000.json
    2026-04-15 19:24:40,271 | INFO | Skipping weekly_record_1484_2019-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,272 | INFO | Processing file: weekly_record_1485_2019-06-03T00-00-00.000.json
    2026-04-15 19:24:40,293 | INFO | Skipping weekly_record_1485_2019-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,293 | INFO | Processing file: weekly_record_1486_2019-06-10T00-00-00.000.json
    2026-04-15 19:24:40,317 | INFO | Skipping weekly_record_1486_2019-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,318 | INFO | Processing file: weekly_record_1487_2019-06-17T00-00-00.000.json
    2026-04-15 19:24:40,339 | INFO | Skipping weekly_record_1487_2019-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,340 | INFO | Processing file: weekly_record_1488_2019-06-24T00-00-00.000.json
    2026-04-15 19:24:40,366 | INFO | Skipping weekly_record_1488_2019-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,367 | INFO | Processing file: weekly_record_1489_2019-07-01T00-00-00.000.json
    2026-04-15 19:24:40,386 | INFO | Skipping weekly_record_1489_2019-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,386 | INFO | Processing file: weekly_record_148_1993-10-18T00-00-00.000.json
    2026-04-15 19:24:40,417 | INFO | Skipping weekly_record_148_1993-10-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,420 | INFO | Processing file: weekly_record_1490_2019-07-08T00-00-00.000.json
    2026-04-15 19:24:40,456 | INFO | Skipping weekly_record_1490_2019-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,457 | INFO | Processing file: weekly_record_1491_2019-07-15T00-00-00.000.json
    2026-04-15 19:24:40,482 | INFO | Skipping weekly_record_1491_2019-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,483 | INFO | Processing file: weekly_record_1492_2019-07-22T00-00-00.000.json
    2026-04-15 19:24:40,511 | INFO | Skipping weekly_record_1492_2019-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,511 | INFO | Processing file: weekly_record_1493_2019-07-29T00-00-00.000.json
    2026-04-15 19:24:40,527 | INFO | Skipping weekly_record_1493_2019-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,528 | INFO | Processing file: weekly_record_1494_2019-08-05T00-00-00.000.json
    2026-04-15 19:24:40,554 | INFO | Skipping weekly_record_1494_2019-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,554 | INFO | Processing file: weekly_record_1495_2019-08-12T00-00-00.000.json
    2026-04-15 19:24:40,582 | INFO | Skipping weekly_record_1495_2019-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,582 | INFO | Processing file: weekly_record_1496_2019-08-19T00-00-00.000.json
    2026-04-15 19:24:40,608 | INFO | Skipping weekly_record_1496_2019-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,608 | INFO | Processing file: weekly_record_1497_2019-08-26T00-00-00.000.json
    2026-04-15 19:24:40,643 | INFO | Skipping weekly_record_1497_2019-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,644 | INFO | Processing file: weekly_record_1498_2019-09-02T00-00-00.000.json
    2026-04-15 19:24:40,666 | INFO | Skipping weekly_record_1498_2019-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,667 | INFO | Processing file: weekly_record_1499_2019-09-09T00-00-00.000.json
    2026-04-15 19:24:40,690 | INFO | Skipping weekly_record_1499_2019-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,691 | INFO | Processing file: weekly_record_149_1993-10-25T00-00-00.000.json
    2026-04-15 19:24:40,707 | INFO | Skipping weekly_record_149_1993-10-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,708 | INFO | Processing file: weekly_record_14_1991-03-25T00-00-00.000.json
    2026-04-15 19:24:40,733 | INFO | Skipping weekly_record_14_1991-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,734 | INFO | Processing file: weekly_record_1500_2019-09-16T00-00-00.000.json
    2026-04-15 19:24:40,754 | INFO | Skipping weekly_record_1500_2019-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,754 | INFO | Processing file: weekly_record_1501_2019-09-23T00-00-00.000.json
    2026-04-15 19:24:40,780 | INFO | Skipping weekly_record_1501_2019-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,781 | INFO | Processing file: weekly_record_1502_2019-09-30T00-00-00.000.json
    2026-04-15 19:24:40,807 | INFO | Skipping weekly_record_1502_2019-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,808 | INFO | Processing file: weekly_record_1503_2019-10-07T00-00-00.000.json
    2026-04-15 19:24:40,829 | INFO | Skipping weekly_record_1503_2019-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,829 | INFO | Processing file: weekly_record_1504_2019-10-14T00-00-00.000.json
    2026-04-15 19:24:40,848 | INFO | Skipping weekly_record_1504_2019-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,849 | INFO | Processing file: weekly_record_1505_2019-10-21T00-00-00.000.json
    2026-04-15 19:24:40,875 | INFO | Skipping weekly_record_1505_2019-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,875 | INFO | Processing file: weekly_record_1506_2019-10-28T00-00-00.000.json
    2026-04-15 19:24:40,897 | INFO | Skipping weekly_record_1506_2019-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,898 | INFO | Processing file: weekly_record_1507_2019-11-04T00-00-00.000.json
    2026-04-15 19:24:40,926 | INFO | Skipping weekly_record_1507_2019-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,927 | INFO | Processing file: weekly_record_1508_2019-11-11T00-00-00.000.json
    2026-04-15 19:24:40,942 | INFO | Skipping weekly_record_1508_2019-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,942 | INFO | Processing file: weekly_record_1509_2019-11-18T00-00-00.000.json
    2026-04-15 19:24:40,960 | INFO | Skipping weekly_record_1509_2019-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,961 | INFO | Processing file: weekly_record_150_1993-11-01T00-00-00.000.json
    2026-04-15 19:24:40,976 | INFO | Skipping weekly_record_150_1993-11-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,977 | INFO | Processing file: weekly_record_1510_2019-11-25T00-00-00.000.json
    2026-04-15 19:24:40,993 | INFO | Skipping weekly_record_1510_2019-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:40,993 | INFO | Processing file: weekly_record_1511_2019-12-02T00-00-00.000.json
    2026-04-15 19:24:41,020 | INFO | Skipping weekly_record_1511_2019-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,021 | INFO | Processing file: weekly_record_1512_2019-12-09T00-00-00.000.json
    2026-04-15 19:24:41,043 | INFO | Skipping weekly_record_1512_2019-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,043 | INFO | Processing file: weekly_record_1513_2019-12-16T00-00-00.000.json
    2026-04-15 19:24:41,065 | INFO | Skipping weekly_record_1513_2019-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,066 | INFO | Processing file: weekly_record_1514_2019-12-23T00-00-00.000.json
    2026-04-15 19:24:41,085 | INFO | Skipping weekly_record_1514_2019-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,085 | INFO | Processing file: weekly_record_1515_2019-12-30T00-00-00.000.json
    2026-04-15 19:24:41,110 | INFO | Skipping weekly_record_1515_2019-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,110 | INFO | Processing file: weekly_record_1516_2020-01-06T00-00-00.000.json
    2026-04-15 19:24:41,139 | INFO | Skipping weekly_record_1516_2020-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,139 | INFO | Processing file: weekly_record_1517_2020-01-13T00-00-00.000.json
    2026-04-15 19:24:41,169 | INFO | Skipping weekly_record_1517_2020-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,169 | INFO | Processing file: weekly_record_1518_2020-01-20T00-00-00.000.json
    2026-04-15 19:24:41,197 | INFO | Skipping weekly_record_1518_2020-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,197 | INFO | Processing file: weekly_record_1519_2020-01-27T00-00-00.000.json
    2026-04-15 19:24:41,224 | INFO | Skipping weekly_record_1519_2020-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,225 | INFO | Processing file: weekly_record_151_1993-11-08T00-00-00.000.json
    2026-04-15 19:24:41,248 | INFO | Skipping weekly_record_151_1993-11-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,248 | INFO | Processing file: weekly_record_1520_2020-02-03T00-00-00.000.json
    2026-04-15 19:24:41,277 | INFO | Skipping weekly_record_1520_2020-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,277 | INFO | Processing file: weekly_record_1521_2020-02-10T00-00-00.000.json
    2026-04-15 19:24:41,303 | INFO | Skipping weekly_record_1521_2020-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,304 | INFO | Processing file: weekly_record_1522_2020-02-17T00-00-00.000.json
    2026-04-15 19:24:41,321 | INFO | Skipping weekly_record_1522_2020-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,321 | INFO | Processing file: weekly_record_1523_2020-02-24T00-00-00.000.json
    2026-04-15 19:24:41,354 | INFO | Skipping weekly_record_1523_2020-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,383 | INFO | Processing file: weekly_record_1524_2020-03-02T00-00-00.000.json
    2026-04-15 19:24:41,415 | INFO | Skipping weekly_record_1524_2020-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,415 | INFO | Processing file: weekly_record_1525_2020-03-09T00-00-00.000.json
    2026-04-15 19:24:41,447 | INFO | Skipping weekly_record_1525_2020-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,447 | INFO | Processing file: weekly_record_1526_2020-03-16T00-00-00.000.json
    2026-04-15 19:24:41,464 | INFO | Skipping weekly_record_1526_2020-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,465 | INFO | Processing file: weekly_record_1527_2020-03-23T00-00-00.000.json
    2026-04-15 19:24:41,492 | INFO | Skipping weekly_record_1527_2020-03-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,493 | INFO | Processing file: weekly_record_1528_2020-03-30T00-00-00.000.json
    2026-04-15 19:24:41,511 | INFO | Skipping weekly_record_1528_2020-03-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,511 | INFO | Processing file: weekly_record_1529_2020-04-06T00-00-00.000.json
    2026-04-15 19:24:41,537 | INFO | Skipping weekly_record_1529_2020-04-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,537 | INFO | Processing file: weekly_record_152_1993-11-15T00-00-00.000.json
    2026-04-15 19:24:41,555 | INFO | Skipping weekly_record_152_1993-11-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,556 | INFO | Processing file: weekly_record_1530_2020-04-13T00-00-00.000.json
    2026-04-15 19:24:41,581 | INFO | Skipping weekly_record_1530_2020-04-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,582 | INFO | Processing file: weekly_record_1531_2020-04-20T00-00-00.000.json
    2026-04-15 19:24:41,599 | INFO | Skipping weekly_record_1531_2020-04-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,599 | INFO | Processing file: weekly_record_1532_2020-04-27T00-00-00.000.json
    2026-04-15 19:24:41,626 | INFO | Skipping weekly_record_1532_2020-04-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,626 | INFO | Processing file: weekly_record_1533_2020-05-04T00-00-00.000.json
    2026-04-15 19:24:41,667 | INFO | Skipping weekly_record_1533_2020-05-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,667 | INFO | Processing file: weekly_record_1534_2020-05-11T00-00-00.000.json
    2026-04-15 19:24:41,693 | INFO | Skipping weekly_record_1534_2020-05-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,694 | INFO | Processing file: weekly_record_1535_2020-05-18T00-00-00.000.json
    2026-04-15 19:24:41,722 | INFO | Skipping weekly_record_1535_2020-05-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,723 | INFO | Processing file: weekly_record_1536_2020-05-25T00-00-00.000.json
    2026-04-15 19:24:41,738 | INFO | Skipping weekly_record_1536_2020-05-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,739 | INFO | Processing file: weekly_record_1537_2020-06-01T00-00-00.000.json
    2026-04-15 19:24:41,771 | INFO | Skipping weekly_record_1537_2020-06-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,771 | INFO | Processing file: weekly_record_1538_2020-06-08T00-00-00.000.json
    2026-04-15 19:24:41,797 | INFO | Skipping weekly_record_1538_2020-06-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,797 | INFO | Processing file: weekly_record_1539_2020-06-15T00-00-00.000.json
    2026-04-15 19:24:41,818 | INFO | Skipping weekly_record_1539_2020-06-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,819 | INFO | Processing file: weekly_record_153_1993-11-22T00-00-00.000.json
    2026-04-15 19:24:41,837 | INFO | Skipping weekly_record_153_1993-11-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,838 | INFO | Processing file: weekly_record_1540_2020-06-22T00-00-00.000.json
    2026-04-15 19:24:41,858 | INFO | Skipping weekly_record_1540_2020-06-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,859 | INFO | Processing file: weekly_record_1541_2020-06-29T00-00-00.000.json
    2026-04-15 19:24:41,881 | INFO | Skipping weekly_record_1541_2020-06-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,882 | INFO | Processing file: weekly_record_1542_2020-07-06T00-00-00.000.json
    2026-04-15 19:24:41,905 | INFO | Skipping weekly_record_1542_2020-07-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,906 | INFO | Processing file: weekly_record_1543_2020-07-13T00-00-00.000.json
    2026-04-15 19:24:41,918 | INFO | Skipping weekly_record_1543_2020-07-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,919 | INFO | Processing file: weekly_record_1544_2020-07-20T00-00-00.000.json
    2026-04-15 19:24:41,946 | INFO | Skipping weekly_record_1544_2020-07-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,947 | INFO | Processing file: weekly_record_1545_2020-07-27T00-00-00.000.json
    2026-04-15 19:24:41,964 | INFO | Skipping weekly_record_1545_2020-07-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,965 | INFO | Processing file: weekly_record_1546_2020-08-03T00-00-00.000.json
    2026-04-15 19:24:41,990 | INFO | Skipping weekly_record_1546_2020-08-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:41,991 | INFO | Processing file: weekly_record_1547_2020-08-10T00-00-00.000.json
    2026-04-15 19:24:42,017 | INFO | Skipping weekly_record_1547_2020-08-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,017 | INFO | Processing file: weekly_record_1548_2020-08-17T00-00-00.000.json
    2026-04-15 19:24:42,044 | INFO | Skipping weekly_record_1548_2020-08-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,045 | INFO | Processing file: weekly_record_1549_2020-08-24T00-00-00.000.json
    2026-04-15 19:24:42,067 | INFO | Skipping weekly_record_1549_2020-08-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,067 | INFO | Processing file: weekly_record_154_1993-11-29T00-00-00.000.json
    2026-04-15 19:24:42,086 | INFO | Skipping weekly_record_154_1993-11-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,086 | INFO | Processing file: weekly_record_1550_2020-08-31T00-00-00.000.json
    2026-04-15 19:24:42,112 | INFO | Skipping weekly_record_1550_2020-08-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,113 | INFO | Processing file: weekly_record_1551_2020-09-07T00-00-00.000.json
    2026-04-15 19:24:42,134 | INFO | Skipping weekly_record_1551_2020-09-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,135 | INFO | Processing file: weekly_record_1552_2020-09-14T00-00-00.000.json
    2026-04-15 19:24:42,163 | INFO | Skipping weekly_record_1552_2020-09-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,164 | INFO | Processing file: weekly_record_1553_2020-09-21T00-00-00.000.json
    2026-04-15 19:24:42,180 | INFO | Skipping weekly_record_1553_2020-09-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,181 | INFO | Processing file: weekly_record_1554_2020-09-28T00-00-00.000.json
    2026-04-15 19:24:42,208 | INFO | Skipping weekly_record_1554_2020-09-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,209 | INFO | Processing file: weekly_record_1555_2020-10-05T00-00-00.000.json
    2026-04-15 19:24:42,225 | INFO | Skipping weekly_record_1555_2020-10-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,225 | INFO | Processing file: weekly_record_1556_2020-10-12T00-00-00.000.json
    2026-04-15 19:24:42,247 | INFO | Skipping weekly_record_1556_2020-10-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,248 | INFO | Processing file: weekly_record_1557_2020-10-19T00-00-00.000.json
    2026-04-15 19:24:42,287 | INFO | Skipping weekly_record_1557_2020-10-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,288 | INFO | Processing file: weekly_record_1558_2020-10-26T00-00-00.000.json
    2026-04-15 19:24:42,315 | INFO | Skipping weekly_record_1558_2020-10-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,316 | INFO | Processing file: weekly_record_1559_2020-11-02T00-00-00.000.json
    2026-04-15 19:24:42,332 | INFO | Skipping weekly_record_1559_2020-11-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,333 | INFO | Processing file: weekly_record_155_1993-12-06T00-00-00.000.json
    2026-04-15 19:24:42,370 | INFO | Skipping weekly_record_155_1993-12-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,371 | INFO | Processing file: weekly_record_1560_2020-11-09T00-00-00.000.json
    2026-04-15 19:24:42,385 | INFO | Skipping weekly_record_1560_2020-11-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,387 | INFO | Processing file: weekly_record_1561_2020-11-16T00-00-00.000.json
    2026-04-15 19:24:42,414 | INFO | Skipping weekly_record_1561_2020-11-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,414 | INFO | Processing file: weekly_record_1562_2020-11-23T00-00-00.000.json
    2026-04-15 19:24:42,431 | INFO | Skipping weekly_record_1562_2020-11-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,432 | INFO | Processing file: weekly_record_1563_2020-11-30T00-00-00.000.json
    2026-04-15 19:24:42,457 | INFO | Skipping weekly_record_1563_2020-11-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,458 | INFO | Processing file: weekly_record_1564_2020-12-07T00-00-00.000.json
    2026-04-15 19:24:42,475 | INFO | Skipping weekly_record_1564_2020-12-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,476 | INFO | Processing file: weekly_record_1565_2020-12-14T00-00-00.000.json
    2026-04-15 19:24:42,502 | INFO | Skipping weekly_record_1565_2020-12-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,503 | INFO | Processing file: weekly_record_1566_2020-12-21T00-00-00.000.json
    2026-04-15 19:24:42,522 | INFO | Skipping weekly_record_1566_2020-12-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,523 | INFO | Processing file: weekly_record_1567_2020-12-28T00-00-00.000.json
    2026-04-15 19:24:42,544 | INFO | Skipping weekly_record_1567_2020-12-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,544 | INFO | Processing file: weekly_record_1568_2021-01-04T00-00-00.000.json
    2026-04-15 19:24:42,558 | INFO | Skipping weekly_record_1568_2021-01-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,560 | INFO | Processing file: weekly_record_1569_2021-01-11T00-00-00.000.json
    2026-04-15 19:24:42,587 | INFO | Skipping weekly_record_1569_2021-01-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,588 | INFO | Processing file: weekly_record_156_1993-12-13T00-00-00.000.json
    2026-04-15 19:24:42,604 | INFO | Skipping weekly_record_156_1993-12-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,604 | INFO | Processing file: weekly_record_1570_2021-01-18T00-00-00.000.json
    2026-04-15 19:24:42,630 | INFO | Skipping weekly_record_1570_2021-01-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,631 | INFO | Processing file: weekly_record_1571_2021-01-25T00-00-00.000.json
    2026-04-15 19:24:42,650 | INFO | Skipping weekly_record_1571_2021-01-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,651 | INFO | Processing file: weekly_record_1572_2021-02-01T00-00-00.000.json
    2026-04-15 19:24:42,666 | INFO | Skipping weekly_record_1572_2021-02-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,667 | INFO | Processing file: weekly_record_1573_2021-02-08T00-00-00.000.json
    2026-04-15 19:24:42,694 | INFO | Skipping weekly_record_1573_2021-02-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,694 | INFO | Processing file: weekly_record_1574_2021-02-15T00-00-00.000.json
    2026-04-15 19:24:42,711 | INFO | Skipping weekly_record_1574_2021-02-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,711 | INFO | Processing file: weekly_record_1575_2021-02-22T00-00-00.000.json
    2026-04-15 19:24:42,739 | INFO | Skipping weekly_record_1575_2021-02-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,739 | INFO | Processing file: weekly_record_1576_2021-03-01T00-00-00.000.json
    2026-04-15 19:24:42,756 | INFO | Skipping weekly_record_1576_2021-03-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,757 | INFO | Processing file: weekly_record_1577_2021-03-08T00-00-00.000.json
    2026-04-15 19:24:42,785 | INFO | Skipping weekly_record_1577_2021-03-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,786 | INFO | Processing file: weekly_record_1578_2021-03-15T00-00-00.000.json
    2026-04-15 19:24:42,811 | INFO | Skipping weekly_record_1578_2021-03-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,812 | INFO | Processing file: weekly_record_1579_2021-03-22T00-00-00.000.json
    2026-04-15 19:24:42,828 | INFO | Skipping weekly_record_1579_2021-03-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,829 | INFO | Processing file: weekly_record_157_1993-12-20T00-00-00.000.json
    2026-04-15 19:24:42,857 | INFO | Skipping weekly_record_157_1993-12-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,858 | INFO | Processing file: weekly_record_1580_2021-03-29T00-00-00.000.json
    2026-04-15 19:24:42,884 | INFO | Skipping weekly_record_1580_2021-03-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,884 | INFO | Processing file: weekly_record_1581_2021-04-05T00-00-00.000.json
    2026-04-15 19:24:42,910 | INFO | Skipping weekly_record_1581_2021-04-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,911 | INFO | Processing file: weekly_record_1582_2021-04-12T00-00-00.000.json
    2026-04-15 19:24:42,937 | INFO | Skipping weekly_record_1582_2021-04-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,938 | INFO | Processing file: weekly_record_1583_2021-04-19T00-00-00.000.json
    2026-04-15 19:24:42,955 | INFO | Skipping weekly_record_1583_2021-04-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,955 | INFO | Processing file: weekly_record_1584_2021-04-26T00-00-00.000.json
    2026-04-15 19:24:42,982 | INFO | Skipping weekly_record_1584_2021-04-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,983 | INFO | Processing file: weekly_record_1585_2021-05-03T00-00-00.000.json
    2026-04-15 19:24:42,999 | INFO | Skipping weekly_record_1585_2021-05-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:42,999 | INFO | Processing file: weekly_record_1586_2021-05-10T00-00-00.000.json
    2026-04-15 19:24:43,026 | INFO | Skipping weekly_record_1586_2021-05-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,027 | INFO | Processing file: weekly_record_1587_2021-05-17T00-00-00.000.json
    2026-04-15 19:24:43,043 | INFO | Skipping weekly_record_1587_2021-05-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,044 | INFO | Processing file: weekly_record_1588_2021-05-24T00-00-00.000.json
    2026-04-15 19:24:43,071 | INFO | Skipping weekly_record_1588_2021-05-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,072 | INFO | Processing file: weekly_record_1589_2021-05-31T00-00-00.000.json
    2026-04-15 19:24:43,089 | INFO | Skipping weekly_record_1589_2021-05-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,090 | INFO | Processing file: weekly_record_158_1993-12-27T00-00-00.000.json
    2026-04-15 19:24:43,116 | INFO | Skipping weekly_record_158_1993-12-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,117 | INFO | Processing file: weekly_record_1590_2021-06-07T00-00-00.000.json
    2026-04-15 19:24:43,135 | INFO | Skipping weekly_record_1590_2021-06-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,136 | INFO | Processing file: weekly_record_1591_2021-06-14T00-00-00.000.json
    2026-04-15 19:24:43,161 | INFO | Skipping weekly_record_1591_2021-06-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,168 | INFO | Processing file: weekly_record_1592_2021-06-21T00-00-00.000.json
    2026-04-15 19:24:43,217 | INFO | Skipping weekly_record_1592_2021-06-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,217 | INFO | Processing file: weekly_record_1593_2021-06-28T00-00-00.000.json
    2026-04-15 19:24:43,244 | INFO | Skipping weekly_record_1593_2021-06-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,244 | INFO | Processing file: weekly_record_1594_2021-07-05T00-00-00.000.json
    2026-04-15 19:24:43,270 | INFO | Skipping weekly_record_1594_2021-07-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,270 | INFO | Processing file: weekly_record_1595_2021-07-12T00-00-00.000.json
    2026-04-15 19:24:43,292 | INFO | Skipping weekly_record_1595_2021-07-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,293 | INFO | Processing file: weekly_record_1596_2021-07-19T00-00-00.000.json
    2026-04-15 19:24:43,325 | INFO | Skipping weekly_record_1596_2021-07-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,325 | INFO | Processing file: weekly_record_1597_2021-07-26T00-00-00.000.json
    2026-04-15 19:24:43,351 | INFO | Skipping weekly_record_1597_2021-07-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,351 | INFO | Processing file: weekly_record_1598_2021-08-02T00-00-00.000.json
    2026-04-15 19:24:43,377 | INFO | Skipping weekly_record_1598_2021-08-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,378 | INFO | Processing file: weekly_record_1599_2021-08-09T00-00-00.000.json
    2026-04-15 19:24:43,408 | INFO | Skipping weekly_record_1599_2021-08-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,408 | INFO | Processing file: weekly_record_159_1994-01-03T00-00-00.000.json
    2026-04-15 19:24:43,437 | INFO | Skipping weekly_record_159_1994-01-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,437 | INFO | Processing file: weekly_record_15_1991-04-01T00-00-00.000.json
    2026-04-15 19:24:43,453 | INFO | Skipping weekly_record_15_1991-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,453 | INFO | Processing file: weekly_record_1600_2021-08-16T00-00-00.000.json
    2026-04-15 19:24:43,475 | INFO | Skipping weekly_record_1600_2021-08-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,476 | INFO | Processing file: weekly_record_1601_2021-08-23T00-00-00.000.json
    2026-04-15 19:24:43,493 | INFO | Skipping weekly_record_1601_2021-08-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,493 | INFO | Processing file: weekly_record_1602_2021-08-30T00-00-00.000.json
    2026-04-15 19:24:43,511 | INFO | Skipping weekly_record_1602_2021-08-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,511 | INFO | Processing file: weekly_record_1603_2021-09-06T00-00-00.000.json
    2026-04-15 19:24:43,538 | INFO | Skipping weekly_record_1603_2021-09-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,539 | INFO | Processing file: weekly_record_1604_2021-09-13T00-00-00.000.json
    2026-04-15 19:24:43,555 | INFO | Skipping weekly_record_1604_2021-09-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,556 | INFO | Processing file: weekly_record_1605_2021-09-20T00-00-00.000.json
    2026-04-15 19:24:43,574 | INFO | Skipping weekly_record_1605_2021-09-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,575 | INFO | Processing file: weekly_record_1606_2021-09-27T00-00-00.000.json
    2026-04-15 19:24:43,592 | INFO | Skipping weekly_record_1606_2021-09-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,593 | INFO | Processing file: weekly_record_1607_2021-10-04T00-00-00.000.json
    2026-04-15 19:24:43,621 | INFO | Skipping weekly_record_1607_2021-10-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,621 | INFO | Processing file: weekly_record_1608_2021-10-11T00-00-00.000.json
    2026-04-15 19:24:43,637 | INFO | Skipping weekly_record_1608_2021-10-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,638 | INFO | Processing file: weekly_record_1609_2021-10-18T00-00-00.000.json
    2026-04-15 19:24:43,661 | INFO | Skipping weekly_record_1609_2021-10-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,661 | INFO | Processing file: weekly_record_160_1994-01-10T00-00-00.000.json
    2026-04-15 19:24:43,681 | INFO | Skipping weekly_record_160_1994-01-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,682 | INFO | Processing file: weekly_record_1610_2021-10-25T00-00-00.000.json
    2026-04-15 19:24:43,705 | INFO | Skipping weekly_record_1610_2021-10-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,706 | INFO | Processing file: weekly_record_1611_2021-11-01T00-00-00.000.json
    2026-04-15 19:24:43,723 | INFO | Skipping weekly_record_1611_2021-11-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,726 | INFO | Processing file: weekly_record_1612_2021-11-08T00-00-00.000.json
    2026-04-15 19:24:43,754 | INFO | Skipping weekly_record_1612_2021-11-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,754 | INFO | Processing file: weekly_record_1613_2021-11-15T00-00-00.000.json
    2026-04-15 19:24:43,772 | INFO | Skipping weekly_record_1613_2021-11-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,773 | INFO | Processing file: weekly_record_1614_2021-11-22T00-00-00.000.json
    2026-04-15 19:24:43,799 | INFO | Skipping weekly_record_1614_2021-11-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,800 | INFO | Processing file: weekly_record_1615_2021-11-29T00-00-00.000.json
    2026-04-15 19:24:43,817 | INFO | Skipping weekly_record_1615_2021-11-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,818 | INFO | Processing file: weekly_record_1616_2021-12-06T00-00-00.000.json
    2026-04-15 19:24:43,844 | INFO | Skipping weekly_record_1616_2021-12-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,845 | INFO | Processing file: weekly_record_1617_2021-12-13T00-00-00.000.json
    2026-04-15 19:24:43,864 | INFO | Skipping weekly_record_1617_2021-12-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,864 | INFO | Processing file: weekly_record_1618_2021-12-20T00-00-00.000.json
    2026-04-15 19:24:43,884 | INFO | Skipping weekly_record_1618_2021-12-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,885 | INFO | Processing file: weekly_record_1619_2021-12-27T00-00-00.000.json
    2026-04-15 19:24:43,911 | INFO | Skipping weekly_record_1619_2021-12-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,912 | INFO | Processing file: weekly_record_161_1994-01-17T00-00-00.000.json
    2026-04-15 19:24:43,938 | INFO | Skipping weekly_record_161_1994-01-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,969 | INFO | Processing file: weekly_record_1620_2022-01-03T00-00-00.000.json
    2026-04-15 19:24:43,994 | INFO | Skipping weekly_record_1620_2022-01-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:43,994 | INFO | Processing file: weekly_record_1621_2022-01-10T00-00-00.000.json
    2026-04-15 19:24:44,008 | INFO | Skipping weekly_record_1621_2022-01-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,008 | INFO | Processing file: weekly_record_1622_2022-01-17T00-00-00.000.json
    2026-04-15 19:24:44,031 | INFO | Skipping weekly_record_1622_2022-01-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,032 | INFO | Processing file: weekly_record_1623_2022-01-24T00-00-00.000.json
    2026-04-15 19:24:44,058 | INFO | Skipping weekly_record_1623_2022-01-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,058 | INFO | Processing file: weekly_record_1624_2022-01-31T00-00-00.000.json
    2026-04-15 19:24:44,085 | INFO | Skipping weekly_record_1624_2022-01-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,085 | INFO | Processing file: weekly_record_1625_2022-02-07T00-00-00.000.json
    2026-04-15 19:24:44,104 | INFO | Skipping weekly_record_1625_2022-02-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,105 | INFO | Processing file: weekly_record_1626_2022-02-14T00-00-00.000.json
    2026-04-15 19:24:44,130 | INFO | Skipping weekly_record_1626_2022-02-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,131 | INFO | Processing file: weekly_record_1627_2022-02-21T00-00-00.000.json
    2026-04-15 19:24:44,149 | INFO | Skipping weekly_record_1627_2022-02-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,149 | INFO | Processing file: weekly_record_1628_2022-02-28T00-00-00.000.json
    2026-04-15 19:24:44,175 | INFO | Skipping weekly_record_1628_2022-02-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,176 | INFO | Processing file: weekly_record_1629_2022-03-07T00-00-00.000.json
    2026-04-15 19:24:44,192 | INFO | Skipping weekly_record_1629_2022-03-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,192 | INFO | Processing file: weekly_record_162_1994-01-24T00-00-00.000.json
    2026-04-15 19:24:44,219 | INFO | Skipping weekly_record_162_1994-01-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,220 | INFO | Processing file: weekly_record_1630_2022-03-14T00-00-00.000.json
    2026-04-15 19:24:44,236 | INFO | Skipping weekly_record_1630_2022-03-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,237 | INFO | Processing file: weekly_record_1631_2022-03-21T00-00-00.000.json
    2026-04-15 19:24:44,263 | INFO | Skipping weekly_record_1631_2022-03-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,264 | INFO | Processing file: weekly_record_1632_2022-03-28T00-00-00.000.json
    2026-04-15 19:24:44,281 | INFO | Skipping weekly_record_1632_2022-03-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,282 | INFO | Processing file: weekly_record_1633_2022-04-04T00-00-00.000.json
    2026-04-15 19:24:44,308 | INFO | Skipping weekly_record_1633_2022-04-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,309 | INFO | Processing file: weekly_record_1634_2022-04-11T00-00-00.000.json
    2026-04-15 19:24:44,331 | INFO | Skipping weekly_record_1634_2022-04-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,332 | INFO | Processing file: weekly_record_1635_2022-04-18T00-00-00.000.json
    2026-04-15 19:24:44,357 | INFO | Skipping weekly_record_1635_2022-04-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,358 | INFO | Processing file: weekly_record_1636_2022-04-25T00-00-00.000.json
    2026-04-15 19:24:44,388 | INFO | Skipping weekly_record_1636_2022-04-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,388 | INFO | Processing file: weekly_record_1637_2022-05-02T00-00-00.000.json
    2026-04-15 19:24:44,404 | INFO | Skipping weekly_record_1637_2022-05-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,404 | INFO | Processing file: weekly_record_1638_2022-05-09T00-00-00.000.json
    2026-04-15 19:24:44,431 | INFO | Skipping weekly_record_1638_2022-05-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,431 | INFO | Processing file: weekly_record_1639_2022-05-16T00-00-00.000.json
    2026-04-15 19:24:44,457 | INFO | Skipping weekly_record_1639_2022-05-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,457 | INFO | Processing file: weekly_record_163_1994-01-31T00-00-00.000.json
    2026-04-15 19:24:44,474 | INFO | Skipping weekly_record_163_1994-01-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,475 | INFO | Processing file: weekly_record_1640_2022-05-23T00-00-00.000.json
    2026-04-15 19:24:44,501 | INFO | Skipping weekly_record_1640_2022-05-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,501 | INFO | Processing file: weekly_record_1641_2022-05-30T00-00-00.000.json
    2026-04-15 19:24:44,519 | INFO | Skipping weekly_record_1641_2022-05-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,520 | INFO | Processing file: weekly_record_1642_2022-06-06T00-00-00.000.json
    2026-04-15 19:24:44,542 | INFO | Skipping weekly_record_1642_2022-06-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,543 | INFO | Processing file: weekly_record_1643_2022-06-13T00-00-00.000.json
    2026-04-15 19:24:44,561 | INFO | Skipping weekly_record_1643_2022-06-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,562 | INFO | Processing file: weekly_record_1644_2022-06-20T00-00-00.000.json
    2026-04-15 19:24:44,579 | INFO | Skipping weekly_record_1644_2022-06-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,579 | INFO | Processing file: weekly_record_1645_2022-06-27T00-00-00.000.json
    2026-04-15 19:24:44,602 | INFO | Skipping weekly_record_1645_2022-06-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,603 | INFO | Processing file: weekly_record_1646_2022-07-04T00-00-00.000.json
    2026-04-15 19:24:44,634 | INFO | Skipping weekly_record_1646_2022-07-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,635 | INFO | Processing file: weekly_record_1647_2022-07-11T00-00-00.000.json
    2026-04-15 19:24:44,653 | INFO | Skipping weekly_record_1647_2022-07-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,654 | INFO | Processing file: weekly_record_1648_2022-07-18T00-00-00.000.json
    2026-04-15 19:24:44,685 | INFO | Skipping weekly_record_1648_2022-07-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,703 | INFO | Processing file: weekly_record_1649_2022-07-25T00-00-00.000.json
    2026-04-15 19:24:44,724 | INFO | Skipping weekly_record_1649_2022-07-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,724 | INFO | Processing file: weekly_record_164_1994-02-07T00-00-00.000.json
    2026-04-15 19:24:44,750 | INFO | Skipping weekly_record_164_1994-02-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,751 | INFO | Processing file: weekly_record_1650_2022-08-01T00-00-00.000.json
    2026-04-15 19:24:44,776 | INFO | Skipping weekly_record_1650_2022-08-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,776 | INFO | Processing file: weekly_record_1651_2022-08-08T00-00-00.000.json
    2026-04-15 19:24:44,793 | INFO | Skipping weekly_record_1651_2022-08-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,793 | INFO | Processing file: weekly_record_1652_2022-08-15T00-00-00.000.json
    2026-04-15 19:24:44,820 | INFO | Skipping weekly_record_1652_2022-08-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,820 | INFO | Processing file: weekly_record_1653_2022-08-22T00-00-00.000.json
    2026-04-15 19:24:44,842 | INFO | Skipping weekly_record_1653_2022-08-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,843 | INFO | Processing file: weekly_record_1654_2022-08-29T00-00-00.000.json
    2026-04-15 19:24:44,869 | INFO | Skipping weekly_record_1654_2022-08-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,870 | INFO | Processing file: weekly_record_1655_2022-09-05T00-00-00.000.json
    2026-04-15 19:24:44,888 | INFO | Skipping weekly_record_1655_2022-09-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,889 | INFO | Processing file: weekly_record_1656_2022-09-12T00-00-00.000.json
    2026-04-15 19:24:44,911 | INFO | Skipping weekly_record_1656_2022-09-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,911 | INFO | Processing file: weekly_record_1657_2022-09-19T00-00-00.000.json
    2026-04-15 19:24:44,928 | INFO | Skipping weekly_record_1657_2022-09-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,929 | INFO | Processing file: weekly_record_1658_2022-09-26T00-00-00.000.json
    2026-04-15 19:24:44,945 | INFO | Skipping weekly_record_1658_2022-09-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,946 | INFO | Processing file: weekly_record_1659_2022-10-03T00-00-00.000.json
    2026-04-15 19:24:44,972 | INFO | Skipping weekly_record_1659_2022-10-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,973 | INFO | Processing file: weekly_record_165_1994-02-14T00-00-00.000.json
    2026-04-15 19:24:44,991 | INFO | Skipping weekly_record_165_1994-02-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:44,992 | INFO | Processing file: weekly_record_1660_2022-10-10T00-00-00.000.json
    2026-04-15 19:24:45,017 | INFO | Skipping weekly_record_1660_2022-10-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,018 | INFO | Processing file: weekly_record_1661_2022-10-17T00-00-00.000.json
    2026-04-15 19:24:45,035 | INFO | Skipping weekly_record_1661_2022-10-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,035 | INFO | Processing file: weekly_record_1662_2022-10-24T00-00-00.000.json
    2026-04-15 19:24:45,062 | INFO | Skipping weekly_record_1662_2022-10-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,063 | INFO | Processing file: weekly_record_1663_2022-10-31T00-00-00.000.json
    2026-04-15 19:24:45,079 | INFO | Skipping weekly_record_1663_2022-10-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,080 | INFO | Processing file: weekly_record_1664_2022-11-07T00-00-00.000.json
    2026-04-15 19:24:45,107 | INFO | Skipping weekly_record_1664_2022-11-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,108 | INFO | Processing file: weekly_record_1665_2022-11-14T00-00-00.000.json
    2026-04-15 19:24:45,125 | INFO | Skipping weekly_record_1665_2022-11-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,125 | INFO | Processing file: weekly_record_1666_2022-11-21T00-00-00.000.json
    2026-04-15 19:24:45,151 | INFO | Skipping weekly_record_1666_2022-11-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,152 | INFO | Processing file: weekly_record_1667_2022-11-28T00-00-00.000.json
    2026-04-15 19:24:45,169 | INFO | Skipping weekly_record_1667_2022-11-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,170 | INFO | Processing file: weekly_record_1668_2022-12-05T00-00-00.000.json
    2026-04-15 19:24:45,193 | INFO | Skipping weekly_record_1668_2022-12-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,193 | INFO | Processing file: weekly_record_1669_2022-12-12T00-00-00.000.json
    2026-04-15 19:24:45,214 | INFO | Skipping weekly_record_1669_2022-12-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,214 | INFO | Processing file: weekly_record_166_1994-02-21T00-00-00.000.json
    2026-04-15 19:24:45,235 | INFO | Skipping weekly_record_166_1994-02-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,236 | INFO | Processing file: weekly_record_1670_2022-12-19T00-00-00.000.json
    2026-04-15 19:24:45,254 | INFO | Skipping weekly_record_1670_2022-12-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,254 | INFO | Processing file: weekly_record_1671_2022-12-26T00-00-00.000.json
    2026-04-15 19:24:45,280 | INFO | Skipping weekly_record_1671_2022-12-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,281 | INFO | Processing file: weekly_record_1672_2023-01-02T00-00-00.000.json
    2026-04-15 19:24:45,297 | INFO | Skipping weekly_record_1672_2023-01-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,298 | INFO | Processing file: weekly_record_1673_2023-01-09T00-00-00.000.json
    2026-04-15 19:24:45,318 | INFO | Skipping weekly_record_1673_2023-01-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,319 | INFO | Processing file: weekly_record_1674_2023-01-16T00-00-00.000.json
    2026-04-15 19:24:45,333 | INFO | Skipping weekly_record_1674_2023-01-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,334 | INFO | Processing file: weekly_record_1675_2023-01-23T00-00-00.000.json
    2026-04-15 19:24:45,362 | INFO | Skipping weekly_record_1675_2023-01-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,362 | INFO | Processing file: weekly_record_1676_2023-01-30T00-00-00.000.json
    2026-04-15 19:24:45,387 | INFO | Skipping weekly_record_1676_2023-01-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,395 | INFO | Processing file: weekly_record_1677_2023-02-06T00-00-00.000.json
    2026-04-15 19:24:45,417 | INFO | Skipping weekly_record_1677_2023-02-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,418 | INFO | Processing file: weekly_record_1678_2023-02-13T00-00-00.000.json
    2026-04-15 19:24:45,455 | INFO | Skipping weekly_record_1678_2023-02-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,455 | INFO | Processing file: weekly_record_1679_2023-02-20T00-00-00.000.json
    2026-04-15 19:24:45,775 | INFO | Skipping weekly_record_1679_2023-02-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,777 | INFO | Processing file: weekly_record_167_1994-02-28T00-00-00.000.json
    2026-04-15 19:24:45,806 | INFO | Skipping weekly_record_167_1994-02-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,807 | INFO | Processing file: weekly_record_1680_2023-02-27T00-00-00.000.json
    2026-04-15 19:24:45,824 | INFO | Skipping weekly_record_1680_2023-02-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,825 | INFO | Processing file: weekly_record_1681_2023-03-06T00-00-00.000.json
    2026-04-15 19:24:45,851 | INFO | Skipping weekly_record_1681_2023-03-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,852 | INFO | Processing file: weekly_record_1682_2023-03-13T00-00-00.000.json
    2026-04-15 19:24:45,869 | INFO | Skipping weekly_record_1682_2023-03-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,870 | INFO | Processing file: weekly_record_1683_2023-03-20T00-00-00.000.json
    2026-04-15 19:24:45,891 | INFO | Skipping weekly_record_1683_2023-03-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,892 | INFO | Processing file: weekly_record_1684_2023-03-27T00-00-00.000.json
    2026-04-15 19:24:45,906 | INFO | Skipping weekly_record_1684_2023-03-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,907 | INFO | Processing file: weekly_record_1685_2023-04-03T00-00-00.000.json
    2026-04-15 19:24:45,933 | INFO | Skipping weekly_record_1685_2023-04-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,933 | INFO | Processing file: weekly_record_1686_2023-04-10T00-00-00.000.json
    2026-04-15 19:24:45,952 | INFO | Skipping weekly_record_1686_2023-04-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,952 | INFO | Processing file: weekly_record_1687_2023-04-17T00-00-00.000.json
    2026-04-15 19:24:45,976 | INFO | Skipping weekly_record_1687_2023-04-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,977 | INFO | Processing file: weekly_record_1688_2023-04-24T00-00-00.000.json
    2026-04-15 19:24:45,994 | INFO | Skipping weekly_record_1688_2023-04-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:45,995 | INFO | Processing file: weekly_record_1689_2023-05-01T00-00-00.000.json
    2026-04-15 19:24:46,021 | INFO | Skipping weekly_record_1689_2023-05-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,022 | INFO | Processing file: weekly_record_168_1994-03-07T00-00-00.000.json
    2026-04-15 19:24:46,038 | INFO | Skipping weekly_record_168_1994-03-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,039 | INFO | Processing file: weekly_record_1690_2023-05-08T00-00-00.000.json
    2026-04-15 19:24:46,069 | INFO | Skipping weekly_record_1690_2023-05-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,069 | INFO | Processing file: weekly_record_1691_2023-05-15T00-00-00.000.json
    2026-04-15 19:24:46,087 | INFO | Skipping weekly_record_1691_2023-05-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,087 | INFO | Processing file: weekly_record_1692_2023-05-22T00-00-00.000.json
    2026-04-15 19:24:46,115 | INFO | Skipping weekly_record_1692_2023-05-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,115 | INFO | Processing file: weekly_record_1693_2023-05-29T00-00-00.000.json
    2026-04-15 19:24:46,132 | INFO | Skipping weekly_record_1693_2023-05-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,135 | INFO | Processing file: weekly_record_1694_2023-06-05T00-00-00.000.json
    2026-04-15 19:24:46,159 | INFO | Skipping weekly_record_1694_2023-06-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,159 | INFO | Processing file: weekly_record_1695_2023-06-12T00-00-00.000.json
    2026-04-15 19:24:46,177 | INFO | Skipping weekly_record_1695_2023-06-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,177 | INFO | Processing file: weekly_record_1696_2023-06-19T00-00-00.000.json
    2026-04-15 19:24:46,195 | INFO | Skipping weekly_record_1696_2023-06-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,195 | INFO | Processing file: weekly_record_1697_2023-06-26T00-00-00.000.json
    2026-04-15 19:24:46,222 | INFO | Skipping weekly_record_1697_2023-06-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,223 | INFO | Processing file: weekly_record_1698_2023-07-03T00-00-00.000.json
    2026-04-15 19:24:46,240 | INFO | Skipping weekly_record_1698_2023-07-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,241 | INFO | Processing file: weekly_record_1699_2023-07-10T00-00-00.000.json
    2026-04-15 19:24:46,267 | INFO | Skipping weekly_record_1699_2023-07-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,267 | INFO | Processing file: weekly_record_169_1994-03-14T00-00-00.000.json
    2026-04-15 19:24:46,284 | INFO | Skipping weekly_record_169_1994-03-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,285 | INFO | Processing file: weekly_record_16_1991-04-08T00-00-00.000.json
    2026-04-15 19:24:46,311 | INFO | Skipping weekly_record_16_1991-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,312 | INFO | Processing file: weekly_record_1700_2023-07-17T00-00-00.000.json
    2026-04-15 19:24:46,330 | INFO | Skipping weekly_record_1700_2023-07-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,331 | INFO | Processing file: weekly_record_1701_2023-07-24T00-00-00.000.json
    2026-04-15 19:24:46,357 | INFO | Skipping weekly_record_1701_2023-07-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,357 | INFO | Processing file: weekly_record_1702_2023-07-31T00-00-00.000.json
    2026-04-15 19:24:46,385 | INFO | Skipping weekly_record_1702_2023-07-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,386 | INFO | Processing file: weekly_record_1703_2023-08-07T00-00-00.000.json
    2026-04-15 19:24:46,403 | INFO | Skipping weekly_record_1703_2023-08-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,410 | INFO | Processing file: weekly_record_1704_2023-08-14T00-00-00.000.json
    2026-04-15 19:24:46,436 | INFO | Skipping weekly_record_1704_2023-08-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,437 | INFO | Processing file: weekly_record_1705_2023-08-21T00-00-00.000.json
    2026-04-15 19:24:46,453 | INFO | Skipping weekly_record_1705_2023-08-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,454 | INFO | Processing file: weekly_record_1706_2023-08-28T00-00-00.000.json
    2026-04-15 19:24:46,479 | INFO | Skipping weekly_record_1706_2023-08-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,480 | INFO | Processing file: weekly_record_1707_2023-09-04T00-00-00.000.json
    2026-04-15 19:24:46,498 | INFO | Skipping weekly_record_1707_2023-09-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,499 | INFO | Processing file: weekly_record_1708_2023-09-11T00-00-00.000.json
    2026-04-15 19:24:46,525 | INFO | Skipping weekly_record_1708_2023-09-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,526 | INFO | Processing file: weekly_record_1709_2023-09-18T00-00-00.000.json
    2026-04-15 19:24:46,543 | INFO | Skipping weekly_record_1709_2023-09-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,544 | INFO | Processing file: weekly_record_170_1994-03-21T00-00-00.000.json
    2026-04-15 19:24:46,570 | INFO | Skipping weekly_record_170_1994-03-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,570 | INFO | Processing file: weekly_record_1710_2023-09-25T00-00-00.000.json
    2026-04-15 19:24:46,600 | INFO | Skipping weekly_record_1710_2023-09-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,600 | INFO | Processing file: weekly_record_1711_2023-10-02T00-00-00.000.json
    2026-04-15 19:24:46,624 | INFO | Skipping weekly_record_1711_2023-10-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,625 | INFO | Processing file: weekly_record_1712_2023-10-09T00-00-00.000.json
    2026-04-15 19:24:46,642 | INFO | Skipping weekly_record_1712_2023-10-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,643 | INFO | Processing file: weekly_record_1713_2023-10-16T00-00-00.000.json
    2026-04-15 19:24:46,694 | INFO | Skipping weekly_record_1713_2023-10-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,694 | INFO | Processing file: weekly_record_1714_2023-10-23T00-00-00.000.json
    2026-04-15 19:24:46,717 | INFO | Skipping weekly_record_1714_2023-10-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,718 | INFO | Processing file: weekly_record_1715_2023-10-30T00-00-00.000.json
    2026-04-15 19:24:46,740 | INFO | Skipping weekly_record_1715_2023-10-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,741 | INFO | Processing file: weekly_record_1716_2023-11-06T00-00-00.000.json
    2026-04-15 19:24:46,758 | INFO | Skipping weekly_record_1716_2023-11-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,758 | INFO | Processing file: weekly_record_1717_2023-11-13T00-00-00.000.json
    2026-04-15 19:24:46,787 | INFO | Skipping weekly_record_1717_2023-11-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,788 | INFO | Processing file: weekly_record_1718_2023-11-20T00-00-00.000.json
    2026-04-15 19:24:46,804 | INFO | Skipping weekly_record_1718_2023-11-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,804 | INFO | Processing file: weekly_record_1719_2023-11-27T00-00-00.000.json
    2026-04-15 19:24:46,830 | INFO | Skipping weekly_record_1719_2023-11-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,831 | INFO | Processing file: weekly_record_171_1994-03-28T00-00-00.000.json
    2026-04-15 19:24:46,849 | INFO | Skipping weekly_record_171_1994-03-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,850 | INFO | Processing file: weekly_record_1720_2023-12-04T00-00-00.000.json
    2026-04-15 19:24:46,875 | INFO | Skipping weekly_record_1720_2023-12-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,875 | INFO | Processing file: weekly_record_1721_2023-12-11T00-00-00.000.json
    2026-04-15 19:24:46,893 | INFO | Skipping weekly_record_1721_2023-12-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,894 | INFO | Processing file: weekly_record_1722_2023-12-18T00-00-00.000.json
    2026-04-15 19:24:46,910 | INFO | Skipping weekly_record_1722_2023-12-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,911 | INFO | Processing file: weekly_record_1723_2023-12-25T00-00-00.000.json
    2026-04-15 19:24:46,935 | INFO | Skipping weekly_record_1723_2023-12-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,936 | INFO | Processing file: weekly_record_1724_2024-01-01T00-00-00.000.json
    2026-04-15 19:24:46,959 | INFO | Skipping weekly_record_1724_2024-01-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,960 | INFO | Processing file: weekly_record_1725_2024-01-08T00-00-00.000.json
    2026-04-15 19:24:46,976 | INFO | Skipping weekly_record_1725_2024-01-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:46,976 | INFO | Processing file: weekly_record_1726_2024-01-15T00-00-00.000.json
    2026-04-15 19:24:47,002 | INFO | Skipping weekly_record_1726_2024-01-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,003 | INFO | Processing file: weekly_record_1727_2024-01-22T00-00-00.000.json
    2026-04-15 19:24:47,020 | INFO | Skipping weekly_record_1727_2024-01-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,021 | INFO | Processing file: weekly_record_1728_2024-01-29T00-00-00.000.json
    2026-04-15 19:24:47,047 | INFO | Skipping weekly_record_1728_2024-01-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,047 | INFO | Processing file: weekly_record_1729_2024-02-05T00-00-00.000.json
    2026-04-15 19:24:47,065 | INFO | Skipping weekly_record_1729_2024-02-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,065 | INFO | Processing file: weekly_record_172_1994-04-04T00-00-00.000.json
    2026-04-15 19:24:47,092 | INFO | Skipping weekly_record_172_1994-04-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,092 | INFO | Processing file: weekly_record_1730_2024-02-12T00-00-00.000.json
    2026-04-15 19:24:47,113 | INFO | Skipping weekly_record_1730_2024-02-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,143 | INFO | Processing file: weekly_record_1731_2024-02-19T00-00-00.000.json
    2026-04-15 19:24:47,163 | INFO | Skipping weekly_record_1731_2024-02-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,163 | INFO | Processing file: weekly_record_1732_2024-02-26T00-00-00.000.json
    2026-04-15 19:24:47,180 | INFO | Skipping weekly_record_1732_2024-02-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,181 | INFO | Processing file: weekly_record_1733_2024-03-04T00-00-00.000.json
    2026-04-15 19:24:47,207 | INFO | Skipping weekly_record_1733_2024-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,208 | INFO | Processing file: weekly_record_1734_2024-03-11T00-00-00.000.json
    2026-04-15 19:24:47,238 | INFO | Skipping weekly_record_1734_2024-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,239 | INFO | Processing file: weekly_record_1735_2024-03-18T00-00-00.000.json
    2026-04-15 19:24:47,262 | INFO | Skipping weekly_record_1735_2024-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,262 | INFO | Processing file: weekly_record_1736_2024-03-25T00-00-00.000.json
    2026-04-15 19:24:47,279 | INFO | Skipping weekly_record_1736_2024-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,280 | INFO | Processing file: weekly_record_1737_2024-04-01T00-00-00.000.json
    2026-04-15 19:24:47,307 | INFO | Skipping weekly_record_1737_2024-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,308 | INFO | Processing file: weekly_record_1738_2024-04-08T00-00-00.000.json
    2026-04-15 19:24:47,325 | INFO | Skipping weekly_record_1738_2024-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,326 | INFO | Processing file: weekly_record_1739_2024-04-15T00-00-00.000.json
    2026-04-15 19:24:47,353 | INFO | Skipping weekly_record_1739_2024-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,354 | INFO | Processing file: weekly_record_173_1994-04-11T00-00-00.000.json
    2026-04-15 19:24:47,376 | INFO | Skipping weekly_record_173_1994-04-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,376 | INFO | Processing file: weekly_record_1740_2024-04-22T00-00-00.000.json
    2026-04-15 19:24:47,395 | INFO | Skipping weekly_record_1740_2024-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,395 | INFO | Processing file: weekly_record_1741_2024-04-29T00-00-00.000.json
    2026-04-15 19:24:47,421 | INFO | Skipping weekly_record_1741_2024-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,421 | INFO | Processing file: weekly_record_1742_2024-05-06T00-00-00.000.json
    2026-04-15 19:24:47,438 | INFO | Skipping weekly_record_1742_2024-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,439 | INFO | Processing file: weekly_record_1743_2024-05-13T00-00-00.000.json
    2026-04-15 19:24:47,469 | INFO | Skipping weekly_record_1743_2024-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,470 | INFO | Processing file: weekly_record_1744_2024-05-20T00-00-00.000.json
    2026-04-15 19:24:47,505 | INFO | Skipping weekly_record_1744_2024-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,505 | INFO | Processing file: weekly_record_1745_2024-05-27T00-00-00.000.json
    2026-04-15 19:24:47,528 | INFO | Skipping weekly_record_1745_2024-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,529 | INFO | Processing file: weekly_record_1746_2024-06-03T00-00-00.000.json
    2026-04-15 19:24:47,551 | INFO | Skipping weekly_record_1746_2024-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,552 | INFO | Processing file: weekly_record_1747_2024-06-10T00-00-00.000.json
    2026-04-15 19:24:47,570 | INFO | Skipping weekly_record_1747_2024-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,570 | INFO | Processing file: weekly_record_1748_2024-06-17T00-00-00.000.json
    2026-04-15 19:24:47,614 | INFO | Skipping weekly_record_1748_2024-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,614 | INFO | Processing file: weekly_record_1749_2024-06-24T00-00-00.000.json
    2026-04-15 19:24:47,633 | INFO | Skipping weekly_record_1749_2024-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,634 | INFO | Processing file: weekly_record_174_1994-04-18T00-00-00.000.json
    2026-04-15 19:24:47,674 | INFO | Skipping weekly_record_174_1994-04-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,674 | INFO | Processing file: weekly_record_1750_2024-07-01T00-00-00.000.json
    2026-04-15 19:24:47,692 | INFO | Skipping weekly_record_1750_2024-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,693 | INFO | Processing file: weekly_record_1751_2024-07-08T00-00-00.000.json
    2026-04-15 19:24:47,719 | INFO | Skipping weekly_record_1751_2024-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,719 | INFO | Processing file: weekly_record_1752_2024-07-15T00-00-00.000.json
    2026-04-15 19:24:47,737 | INFO | Skipping weekly_record_1752_2024-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,737 | INFO | Processing file: weekly_record_1753_2024-07-22T00-00-00.000.json
    2026-04-15 19:24:47,758 | INFO | Skipping weekly_record_1753_2024-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,759 | INFO | Processing file: weekly_record_1754_2024-07-29T00-00-00.000.json
    2026-04-15 19:24:47,778 | INFO | Skipping weekly_record_1754_2024-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,779 | INFO | Processing file: weekly_record_1755_2024-08-05T00-00-00.000.json
    2026-04-15 19:24:47,816 | INFO | Skipping weekly_record_1755_2024-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,817 | INFO | Processing file: weekly_record_1756_2024-08-12T00-00-00.000.json
    2026-04-15 19:24:47,846 | INFO | Skipping weekly_record_1756_2024-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,847 | INFO | Processing file: weekly_record_1757_2024-08-19T00-00-00.000.json
    2026-04-15 19:24:47,872 | INFO | Skipping weekly_record_1757_2024-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,874 | INFO | Processing file: weekly_record_1758_2024-08-26T00-00-00.000.json
    2026-04-15 19:24:47,899 | INFO | Skipping weekly_record_1758_2024-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,900 | INFO | Processing file: weekly_record_1759_2024-09-02T00-00-00.000.json
    2026-04-15 19:24:47,930 | INFO | Skipping weekly_record_1759_2024-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,930 | INFO | Processing file: weekly_record_175_1994-04-25T00-00-00.000.json
    2026-04-15 19:24:47,947 | INFO | Skipping weekly_record_175_1994-04-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,947 | INFO | Processing file: weekly_record_1760_2024-09-09T00-00-00.000.json
    2026-04-15 19:24:47,975 | INFO | Skipping weekly_record_1760_2024-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,976 | INFO | Processing file: weekly_record_1761_2024-09-16T00-00-00.000.json
    2026-04-15 19:24:47,990 | INFO | Skipping weekly_record_1761_2024-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:47,991 | INFO | Processing file: weekly_record_1762_2024-09-23T00-00-00.000.json
    2026-04-15 19:24:48,025 | INFO | Skipping weekly_record_1762_2024-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,025 | INFO | Processing file: weekly_record_1763_2024-09-30T00-00-00.000.json
    2026-04-15 19:24:48,043 | INFO | Skipping weekly_record_1763_2024-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,044 | INFO | Processing file: weekly_record_1764_2024-10-07T00-00-00.000.json
    2026-04-15 19:24:48,073 | INFO | Skipping weekly_record_1764_2024-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,074 | INFO | Processing file: weekly_record_1765_2024-10-14T00-00-00.000.json
    2026-04-15 19:24:48,088 | INFO | Skipping weekly_record_1765_2024-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,088 | INFO | Processing file: weekly_record_1766_2024-10-21T00-00-00.000.json
    2026-04-15 19:24:48,116 | INFO | Skipping weekly_record_1766_2024-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,116 | INFO | Processing file: weekly_record_1767_2024-10-28T00-00-00.000.json
    2026-04-15 19:24:48,134 | INFO | Skipping weekly_record_1767_2024-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,135 | INFO | Processing file: weekly_record_1768_2024-11-04T00-00-00.000.json
    2026-04-15 19:24:48,160 | INFO | Skipping weekly_record_1768_2024-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,161 | INFO | Processing file: weekly_record_1769_2024-11-11T00-00-00.000.json
    2026-04-15 19:24:48,179 | INFO | Skipping weekly_record_1769_2024-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,180 | INFO | Processing file: weekly_record_176_1994-05-02T00-00-00.000.json
    2026-04-15 19:24:48,219 | INFO | Skipping weekly_record_176_1994-05-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,220 | INFO | Processing file: weekly_record_1770_2024-11-18T00-00-00.000.json
    2026-04-15 19:24:48,237 | INFO | Skipping weekly_record_1770_2024-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,237 | INFO | Processing file: weekly_record_1771_2024-11-25T00-00-00.000.json
    2026-04-15 19:24:48,263 | INFO | Skipping weekly_record_1771_2024-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,263 | INFO | Processing file: weekly_record_1772_2024-12-02T00-00-00.000.json
    2026-04-15 19:24:48,285 | INFO | Skipping weekly_record_1772_2024-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,285 | INFO | Processing file: weekly_record_1773_2024-12-09T00-00-00.000.json
    2026-04-15 19:24:48,309 | INFO | Skipping weekly_record_1773_2024-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,309 | INFO | Processing file: weekly_record_1774_2024-12-16T00-00-00.000.json
    2026-04-15 19:24:48,334 | INFO | Skipping weekly_record_1774_2024-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,335 | INFO | Processing file: weekly_record_1775_2024-12-23T00-00-00.000.json
    2026-04-15 19:24:48,365 | INFO | Skipping weekly_record_1775_2024-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,366 | INFO | Processing file: weekly_record_1776_2024-12-30T00-00-00.000.json
    2026-04-15 19:24:48,388 | INFO | Skipping weekly_record_1776_2024-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,389 | INFO | Processing file: weekly_record_1777_2025-01-06T00-00-00.000.json
    2026-04-15 19:24:48,423 | INFO | Skipping weekly_record_1777_2025-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,423 | INFO | Processing file: weekly_record_1778_2025-01-13T00-00-00.000.json
    2026-04-15 19:24:48,444 | INFO | Skipping weekly_record_1778_2025-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,445 | INFO | Processing file: weekly_record_1779_2025-01-20T00-00-00.000.json
    2026-04-15 19:24:48,471 | INFO | Skipping weekly_record_1779_2025-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,471 | INFO | Processing file: weekly_record_177_1994-05-09T00-00-00.000.json
    2026-04-15 19:24:48,499 | INFO | Skipping weekly_record_177_1994-05-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,500 | INFO | Processing file: weekly_record_1780_2025-01-27T00-00-00.000.json
    2026-04-15 19:24:48,528 | INFO | Skipping weekly_record_1780_2025-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,536 | INFO | Processing file: weekly_record_1781_2025-02-03T00-00-00.000.json
    2026-04-15 19:24:48,559 | INFO | Skipping weekly_record_1781_2025-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,560 | INFO | Processing file: weekly_record_1782_2025-02-10T00-00-00.000.json
    2026-04-15 19:24:48,578 | INFO | Skipping weekly_record_1782_2025-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,579 | INFO | Processing file: weekly_record_1783_2025-02-17T00-00-00.000.json
    2026-04-15 19:24:48,604 | INFO | Skipping weekly_record_1783_2025-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,604 | INFO | Processing file: weekly_record_1784_2025-02-24T00-00-00.000.json
    2026-04-15 19:24:48,621 | INFO | Skipping weekly_record_1784_2025-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,621 | INFO | Processing file: weekly_record_1785_2025-03-03T00-00-00.000.json
    2026-04-15 19:24:48,645 | INFO | Skipping weekly_record_1785_2025-03-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,645 | INFO | Processing file: weekly_record_1786_2025-03-10T00-00-00.000.json
    2026-04-15 19:24:48,671 | INFO | Skipping weekly_record_1786_2025-03-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,672 | INFO | Processing file: weekly_record_1787_2025-03-17T00-00-00.000.json
    2026-04-15 19:24:48,698 | INFO | Skipping weekly_record_1787_2025-03-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,699 | INFO | Processing file: weekly_record_1788_2025-03-24T00-00-00.000.json
    2026-04-15 19:24:48,715 | INFO | Skipping weekly_record_1788_2025-03-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,716 | INFO | Processing file: weekly_record_1789_2025-03-31T00-00-00.000.json
    2026-04-15 19:24:48,743 | INFO | Skipping weekly_record_1789_2025-03-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,743 | INFO | Processing file: weekly_record_178_1994-05-16T00-00-00.000.json
    2026-04-15 19:24:48,761 | INFO | Skipping weekly_record_178_1994-05-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,761 | INFO | Processing file: weekly_record_1790_2025-04-07T00-00-00.000.json
    2026-04-15 19:24:48,788 | INFO | Skipping weekly_record_1790_2025-04-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,789 | INFO | Processing file: weekly_record_1791_2025-04-14T00-00-00.000.json
    2026-04-15 19:24:48,807 | INFO | Skipping weekly_record_1791_2025-04-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,807 | INFO | Processing file: weekly_record_1792_2025-04-21T00-00-00.000.json
    2026-04-15 19:24:48,834 | INFO | Skipping weekly_record_1792_2025-04-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,834 | INFO | Processing file: weekly_record_1793_2025-04-28T00-00-00.000.json
    2026-04-15 19:24:48,852 | INFO | Skipping weekly_record_1793_2025-04-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,853 | INFO | Processing file: weekly_record_1794_2025-05-05T00-00-00.000.json
    2026-04-15 19:24:48,879 | INFO | Skipping weekly_record_1794_2025-05-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,880 | INFO | Processing file: weekly_record_1795_2025-05-12T00-00-00.000.json
    2026-04-15 19:24:48,897 | INFO | Skipping weekly_record_1795_2025-05-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,897 | INFO | Processing file: weekly_record_1796_2025-05-19T00-00-00.000.json
    2026-04-15 19:24:48,925 | INFO | Skipping weekly_record_1796_2025-05-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,925 | INFO | Processing file: weekly_record_1797_2025-05-26T00-00-00.000.json
    2026-04-15 19:24:48,941 | INFO | Skipping weekly_record_1797_2025-05-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,942 | INFO | Processing file: weekly_record_1798_2025-06-02T00-00-00.000.json
    2026-04-15 19:24:48,966 | INFO | Skipping weekly_record_1798_2025-06-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,967 | INFO | Processing file: weekly_record_1799_2025-06-09T00-00-00.000.json
    2026-04-15 19:24:48,982 | INFO | Skipping weekly_record_1799_2025-06-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:48,983 | INFO | Processing file: weekly_record_179_1994-05-23T00-00-00.000.json
    2026-04-15 19:24:49,009 | INFO | Skipping weekly_record_179_1994-05-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,009 | INFO | Processing file: weekly_record_17_1991-04-15T00-00-00.000.json
    2026-04-15 19:24:49,035 | INFO | Skipping weekly_record_17_1991-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,036 | INFO | Processing file: weekly_record_1800_2025-06-16T00-00-00.000.json
    2026-04-15 19:24:49,063 | INFO | Skipping weekly_record_1800_2025-06-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,064 | INFO | Processing file: weekly_record_1801_2025-06-23T00-00-00.000.json
    2026-04-15 19:24:49,087 | INFO | Skipping weekly_record_1801_2025-06-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,089 | INFO | Processing file: weekly_record_1802_2025-06-30T00-00-00.000.json
    2026-04-15 19:24:49,105 | INFO | Skipping weekly_record_1802_2025-06-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,106 | INFO | Processing file: weekly_record_1803_2025-07-07T00-00-00.000.json
    2026-04-15 19:24:49,126 | INFO | Skipping weekly_record_1803_2025-07-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,127 | INFO | Processing file: weekly_record_1804_2025-07-14T00-00-00.000.json
    2026-04-15 19:24:49,157 | INFO | Skipping weekly_record_1804_2025-07-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,164 | INFO | Processing file: weekly_record_1805_2025-07-21T00-00-00.000.json
    2026-04-15 19:24:49,183 | INFO | Skipping weekly_record_1805_2025-07-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,184 | INFO | Processing file: weekly_record_1806_2025-07-28T00-00-00.000.json
    2026-04-15 19:24:49,199 | INFO | Skipping weekly_record_1806_2025-07-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,199 | INFO | Processing file: weekly_record_1807_2025-08-04T00-00-00.000.json
    2026-04-15 19:24:49,226 | INFO | Skipping weekly_record_1807_2025-08-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,226 | INFO | Processing file: weekly_record_1808_2025-08-11T00-00-00.000.json
    2026-04-15 19:24:49,244 | INFO | Skipping weekly_record_1808_2025-08-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,245 | INFO | Processing file: weekly_record_1809_2025-08-18T00-00-00.000.json
    2026-04-15 19:24:49,271 | INFO | Skipping weekly_record_1809_2025-08-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,272 | INFO | Processing file: weekly_record_180_1994-05-30T00-00-00.000.json
    2026-04-15 19:24:49,289 | INFO | Skipping weekly_record_180_1994-05-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,289 | INFO | Processing file: weekly_record_1810_2025-08-25T00-00-00.000.json
    2026-04-15 19:24:49,316 | INFO | Skipping weekly_record_1810_2025-08-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,316 | INFO | Processing file: weekly_record_1811_2025-09-01T00-00-00.000.json
    2026-04-15 19:24:49,334 | INFO | Skipping weekly_record_1811_2025-09-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,334 | INFO | Processing file: weekly_record_1812_2025-09-08T00-00-00.000.json
    2026-04-15 19:24:49,362 | INFO | Skipping weekly_record_1812_2025-09-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,362 | INFO | Processing file: weekly_record_1813_2025-09-15T00-00-00.000.json
    2026-04-15 19:24:49,380 | INFO | Skipping weekly_record_1813_2025-09-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,380 | INFO | Processing file: weekly_record_1814_2025-09-22T00-00-00.000.json
    2026-04-15 19:24:49,406 | INFO | Skipping weekly_record_1814_2025-09-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,407 | INFO | Processing file: weekly_record_1815_2025-09-29T00-00-00.000.json
    2026-04-15 19:24:49,424 | INFO | Skipping weekly_record_1815_2025-09-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,425 | INFO | Processing file: weekly_record_1816_2025-10-06T00-00-00.000.json
    2026-04-15 19:24:49,452 | INFO | Skipping weekly_record_1816_2025-10-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,453 | INFO | Processing file: weekly_record_1817_2025-10-13T00-00-00.000.json
    2026-04-15 19:24:49,470 | INFO | Skipping weekly_record_1817_2025-10-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,471 | INFO | Processing file: weekly_record_1818_2025-10-20T00-00-00.000.json
    2026-04-15 19:24:49,496 | INFO | Skipping weekly_record_1818_2025-10-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,496 | INFO | Processing file: weekly_record_1819_2025-10-27T00-00-00.000.json
    2026-04-15 19:24:49,515 | INFO | Skipping weekly_record_1819_2025-10-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,517 | INFO | Processing file: weekly_record_181_1994-06-06T00-00-00.000.json
    2026-04-15 19:24:49,541 | INFO | Skipping weekly_record_181_1994-06-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,542 | INFO | Processing file: weekly_record_1820_2025-11-03T00-00-00.000.json
    2026-04-15 19:24:49,568 | INFO | Skipping weekly_record_1820_2025-11-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,569 | INFO | Processing file: weekly_record_1821_2025-11-10T00-00-00.000.json
    2026-04-15 19:24:49,595 | INFO | Skipping weekly_record_1821_2025-11-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,595 | INFO | Processing file: weekly_record_1822_2025-11-17T00-00-00.000.json
    2026-04-15 19:24:49,623 | INFO | Skipping weekly_record_1822_2025-11-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,623 | INFO | Processing file: weekly_record_1823_2025-11-24T00-00-00.000.json
    2026-04-15 19:24:49,655 | INFO | Skipping weekly_record_1823_2025-11-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,656 | INFO | Processing file: weekly_record_1824_2025-12-01T00-00-00.000.json
    2026-04-15 19:24:49,680 | INFO | Skipping weekly_record_1824_2025-12-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,681 | INFO | Processing file: weekly_record_1825_2025-12-08T00-00-00.000.json
    2026-04-15 19:24:49,701 | INFO | Skipping weekly_record_1825_2025-12-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,702 | INFO | Processing file: weekly_record_1826_2025-12-15T00-00-00.000.json
    2026-04-15 19:24:49,730 | INFO | Skipping weekly_record_1826_2025-12-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,730 | INFO | Processing file: weekly_record_1827_2025-12-22T00-00-00.000.json
    2026-04-15 19:24:49,750 | INFO | Skipping weekly_record_1827_2025-12-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,765 | INFO | Processing file: weekly_record_1828_2025-12-29T00-00-00.000.json
    2026-04-15 19:24:49,793 | INFO | Skipping weekly_record_1828_2025-12-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,793 | INFO | Processing file: weekly_record_1829_2026-01-05T00-00-00.000.json
    2026-04-15 19:24:49,819 | INFO | Skipping weekly_record_1829_2026-01-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,819 | INFO | Processing file: weekly_record_182_1994-06-13T00-00-00.000.json
    2026-04-15 19:24:49,848 | INFO | Skipping weekly_record_182_1994-06-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,849 | INFO | Processing file: weekly_record_1830_2026-01-12T00-00-00.000.json
    2026-04-15 19:24:49,867 | INFO | Skipping weekly_record_1830_2026-01-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,868 | INFO | Processing file: weekly_record_1831_2026-01-19T00-00-00.000.json
    2026-04-15 19:24:49,894 | INFO | Skipping weekly_record_1831_2026-01-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,895 | INFO | Processing file: weekly_record_1832_2026-01-26T00-00-00.000.json
    2026-04-15 19:24:49,909 | INFO | Skipping weekly_record_1832_2026-01-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,910 | INFO | Processing file: weekly_record_1833_2026-02-02T00-00-00.000.json
    2026-04-15 19:24:49,946 | INFO | Skipping weekly_record_1833_2026-02-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,946 | INFO | Processing file: weekly_record_1834_2026-02-09T00-00-00.000.json
    2026-04-15 19:24:49,965 | INFO | Skipping weekly_record_1834_2026-02-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,966 | INFO | Processing file: weekly_record_1835_2026-02-16T00-00-00.000.json
    2026-04-15 19:24:49,991 | INFO | Skipping weekly_record_1835_2026-02-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:49,992 | INFO | Processing file: weekly_record_1836_2026-02-23T00-00-00.000.json
    2026-04-15 19:24:50,028 | INFO | Skipping weekly_record_1836_2026-02-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,028 | INFO | Processing file: weekly_record_1837_2026-03-02T00-00-00.000.json
    2026-04-15 19:24:50,043 | INFO | Skipping weekly_record_1837_2026-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,044 | INFO | Processing file: weekly_record_1838_2026-03-09T00-00-00.000.json
    2026-04-15 19:24:50,072 | INFO | Skipping weekly_record_1838_2026-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,073 | INFO | Processing file: weekly_record_1839_2026-03-16T00-00-00.000.json
    2026-04-15 19:24:50,089 | INFO | Skipping weekly_record_1839_2026-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,089 | INFO | Processing file: weekly_record_183_1994-06-20T00-00-00.000.json
    2026-04-15 19:24:50,114 | INFO | Skipping weekly_record_183_1994-06-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,115 | INFO | Processing file: weekly_record_184_1994-06-27T00-00-00.000.json
    2026-04-15 19:24:50,132 | INFO | Skipping weekly_record_184_1994-06-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,132 | INFO | Processing file: weekly_record_185_1994-07-04T00-00-00.000.json
    2026-04-15 19:24:50,158 | INFO | Skipping weekly_record_185_1994-07-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,158 | INFO | Processing file: weekly_record_186_1994-07-11T00-00-00.000.json
    2026-04-15 19:24:50,177 | INFO | Skipping weekly_record_186_1994-07-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,177 | INFO | Processing file: weekly_record_187_1994-07-18T00-00-00.000.json
    2026-04-15 19:24:50,207 | INFO | Skipping weekly_record_187_1994-07-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,208 | INFO | Processing file: weekly_record_188_1994-07-25T00-00-00.000.json
    2026-04-15 19:24:50,224 | INFO | Skipping weekly_record_188_1994-07-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,224 | INFO | Processing file: weekly_record_189_1994-08-01T00-00-00.000.json
    2026-04-15 19:24:50,251 | INFO | Skipping weekly_record_189_1994-08-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,251 | INFO | Processing file: weekly_record_18_1991-04-22T00-00-00.000.json
    2026-04-15 19:24:50,269 | INFO | Skipping weekly_record_18_1991-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,269 | INFO | Processing file: weekly_record_190_1994-08-08T00-00-00.000.json
    2026-04-15 19:24:50,296 | INFO | Skipping weekly_record_190_1994-08-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,297 | INFO | Processing file: weekly_record_191_1994-08-15T00-00-00.000.json
    2026-04-15 19:24:50,317 | INFO | Skipping weekly_record_191_1994-08-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,318 | INFO | Processing file: weekly_record_192_1994-08-22T00-00-00.000.json
    2026-04-15 19:24:50,339 | INFO | Skipping weekly_record_192_1994-08-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,340 | INFO | Processing file: weekly_record_193_1994-08-29T00-00-00.000.json
    2026-04-15 19:24:50,377 | INFO | Skipping weekly_record_193_1994-08-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,381 | INFO | Processing file: weekly_record_194_1994-09-05T00-00-00.000.json
    2026-04-15 19:24:50,412 | INFO | Skipping weekly_record_194_1994-09-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,413 | INFO | Processing file: weekly_record_195_1994-09-12T00-00-00.000.json
    2026-04-15 19:24:50,429 | INFO | Skipping weekly_record_195_1994-09-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,430 | INFO | Processing file: weekly_record_196_1994-09-19T00-00-00.000.json
    2026-04-15 19:24:50,462 | INFO | Skipping weekly_record_196_1994-09-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,462 | INFO | Processing file: weekly_record_197_1994-09-26T00-00-00.000.json
    2026-04-15 19:24:50,478 | INFO | Skipping weekly_record_197_1994-09-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,479 | INFO | Processing file: weekly_record_198_1994-10-03T00-00-00.000.json
    2026-04-15 19:24:50,505 | INFO | Skipping weekly_record_198_1994-10-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,506 | INFO | Processing file: weekly_record_199_1994-10-10T00-00-00.000.json
    2026-04-15 19:24:50,533 | INFO | Skipping weekly_record_199_1994-10-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,533 | INFO | Processing file: weekly_record_19_1991-04-29T00-00-00.000.json
    2026-04-15 19:24:50,555 | INFO | Skipping weekly_record_19_1991-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,555 | INFO | Processing file: weekly_record_1_1990-11-12T00-00-00.000.json
    2026-04-15 19:24:50,581 | INFO | Skipping weekly_record_1_1990-11-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,581 | INFO | Processing file: weekly_record_200_1994-10-17T00-00-00.000.json
    2026-04-15 19:24:50,598 | INFO | Skipping weekly_record_200_1994-10-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,599 | INFO | Processing file: weekly_record_201_1994-10-24T00-00-00.000.json
    2026-04-15 19:24:50,626 | INFO | Skipping weekly_record_201_1994-10-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,626 | INFO | Processing file: weekly_record_202_1994-10-31T00-00-00.000.json
    2026-04-15 19:24:50,644 | INFO | Skipping weekly_record_202_1994-10-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,645 | INFO | Processing file: weekly_record_203_1994-11-07T00-00-00.000.json
    2026-04-15 19:24:50,679 | INFO | Skipping weekly_record_203_1994-11-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,680 | INFO | Processing file: weekly_record_204_1994-11-14T00-00-00.000.json
    2026-04-15 19:24:50,698 | INFO | Skipping weekly_record_204_1994-11-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,698 | INFO | Processing file: weekly_record_205_1994-11-21T00-00-00.000.json
    2026-04-15 19:24:50,735 | INFO | Skipping weekly_record_205_1994-11-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,736 | INFO | Processing file: weekly_record_206_1994-11-28T00-00-00.000.json
    2026-04-15 19:24:50,752 | INFO | Skipping weekly_record_206_1994-11-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,753 | INFO | Processing file: weekly_record_207_1994-12-05T00-00-00.000.json
    2026-04-15 19:24:50,782 | INFO | Skipping weekly_record_207_1994-12-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,783 | INFO | Processing file: weekly_record_208_1994-12-12T00-00-00.000.json
    2026-04-15 19:24:50,800 | INFO | Skipping weekly_record_208_1994-12-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,801 | INFO | Processing file: weekly_record_209_1994-12-19T00-00-00.000.json
    2026-04-15 19:24:50,825 | INFO | Skipping weekly_record_209_1994-12-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,826 | INFO | Processing file: weekly_record_20_1991-05-06T00-00-00.000.json
    2026-04-15 19:24:50,843 | INFO | Skipping weekly_record_20_1991-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,844 | INFO | Processing file: weekly_record_210_1994-12-26T00-00-00.000.json
    2026-04-15 19:24:50,870 | INFO | Skipping weekly_record_210_1994-12-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,871 | INFO | Processing file: weekly_record_211_1995-01-02T00-00-00.000.json
    2026-04-15 19:24:50,887 | INFO | Skipping weekly_record_211_1995-01-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,890 | INFO | Processing file: weekly_record_212_1995-01-09T00-00-00.000.json
    2026-04-15 19:24:50,914 | INFO | Skipping weekly_record_212_1995-01-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,915 | INFO | Processing file: weekly_record_213_1995-01-16T00-00-00.000.json
    2026-04-15 19:24:50,932 | INFO | Skipping weekly_record_213_1995-01-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,947 | INFO | Processing file: weekly_record_214_1995-01-23T00-00-00.000.json
    2026-04-15 19:24:50,985 | INFO | Skipping weekly_record_214_1995-01-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:50,986 | INFO | Processing file: weekly_record_215_1995-01-30T00-00-00.000.json
    2026-04-15 19:24:51,002 | INFO | Skipping weekly_record_215_1995-01-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,003 | INFO | Processing file: weekly_record_216_1995-02-06T00-00-00.000.json
    2026-04-15 19:24:51,029 | INFO | Skipping weekly_record_216_1995-02-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,030 | INFO | Processing file: weekly_record_217_1995-02-13T00-00-00.000.json
    2026-04-15 19:24:51,047 | INFO | Skipping weekly_record_217_1995-02-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,047 | INFO | Processing file: weekly_record_218_1995-02-20T00-00-00.000.json
    2026-04-15 19:24:51,079 | INFO | Skipping weekly_record_218_1995-02-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,080 | INFO | Processing file: weekly_record_219_1995-02-27T00-00-00.000.json
    2026-04-15 19:24:51,101 | INFO | Skipping weekly_record_219_1995-02-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,102 | INFO | Processing file: weekly_record_21_1991-05-13T00-00-00.000.json
    2026-04-15 19:24:51,120 | INFO | Skipping weekly_record_21_1991-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,122 | INFO | Processing file: weekly_record_220_1995-03-06T00-00-00.000.json
    2026-04-15 19:24:51,146 | INFO | Skipping weekly_record_220_1995-03-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,147 | INFO | Processing file: weekly_record_221_1995-03-13T00-00-00.000.json
    2026-04-15 19:24:51,171 | INFO | Skipping weekly_record_221_1995-03-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,172 | INFO | Processing file: weekly_record_222_1995-03-20T00-00-00.000.json
    2026-04-15 19:24:51,190 | INFO | Skipping weekly_record_222_1995-03-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,191 | INFO | Processing file: weekly_record_223_1995-03-27T00-00-00.000.json
    2026-04-15 19:24:51,218 | INFO | Skipping weekly_record_223_1995-03-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,218 | INFO | Processing file: weekly_record_224_1995-04-03T00-00-00.000.json
    2026-04-15 19:24:51,235 | INFO | Skipping weekly_record_224_1995-04-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,236 | INFO | Processing file: weekly_record_225_1995-04-10T00-00-00.000.json
    2026-04-15 19:24:51,262 | INFO | Skipping weekly_record_225_1995-04-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,262 | INFO | Processing file: weekly_record_226_1995-04-17T00-00-00.000.json
    2026-04-15 19:24:51,285 | INFO | Skipping weekly_record_226_1995-04-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,285 | INFO | Processing file: weekly_record_227_1995-04-24T00-00-00.000.json
    2026-04-15 19:24:51,303 | INFO | Skipping weekly_record_227_1995-04-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,307 | INFO | Processing file: weekly_record_228_1995-05-01T00-00-00.000.json
    2026-04-15 19:24:51,330 | INFO | Skipping weekly_record_228_1995-05-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,330 | INFO | Processing file: weekly_record_229_1995-05-08T00-00-00.000.json
    2026-04-15 19:24:51,347 | INFO | Skipping weekly_record_229_1995-05-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,347 | INFO | Processing file: weekly_record_22_1991-05-20T00-00-00.000.json
    2026-04-15 19:24:51,375 | INFO | Skipping weekly_record_22_1991-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,376 | INFO | Processing file: weekly_record_230_1995-05-15T00-00-00.000.json
    2026-04-15 19:24:51,396 | INFO | Skipping weekly_record_230_1995-05-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,396 | INFO | Processing file: weekly_record_231_1995-05-22T00-00-00.000.json
    2026-04-15 19:24:51,415 | INFO | Skipping weekly_record_231_1995-05-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,416 | INFO | Processing file: weekly_record_232_1995-05-29T00-00-00.000.json
    2026-04-15 19:24:51,436 | INFO | Skipping weekly_record_232_1995-05-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,437 | INFO | Processing file: weekly_record_233_1995-06-05T00-00-00.000.json
    2026-04-15 19:24:51,470 | INFO | Skipping weekly_record_233_1995-06-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,471 | INFO | Processing file: weekly_record_234_1995-06-12T00-00-00.000.json
    2026-04-15 19:24:51,510 | INFO | Skipping weekly_record_234_1995-06-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,510 | INFO | Processing file: weekly_record_235_1995-06-19T00-00-00.000.json
    2026-04-15 19:24:51,540 | INFO | Skipping weekly_record_235_1995-06-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,541 | INFO | Processing file: weekly_record_236_1995-06-26T00-00-00.000.json
    2026-04-15 19:24:51,567 | INFO | Skipping weekly_record_236_1995-06-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,567 | INFO | Processing file: weekly_record_237_1995-07-03T00-00-00.000.json
    2026-04-15 19:24:51,590 | INFO | Skipping weekly_record_237_1995-07-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,591 | INFO | Processing file: weekly_record_238_1995-07-10T00-00-00.000.json
    2026-04-15 19:24:51,608 | INFO | Skipping weekly_record_238_1995-07-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,609 | INFO | Processing file: weekly_record_239_1995-07-17T00-00-00.000.json
    2026-04-15 19:24:51,635 | INFO | Skipping weekly_record_239_1995-07-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,635 | INFO | Processing file: weekly_record_23_1991-05-27T00-00-00.000.json
    2026-04-15 19:24:51,654 | INFO | Skipping weekly_record_23_1991-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,655 | INFO | Processing file: weekly_record_240_1995-07-24T00-00-00.000.json
    2026-04-15 19:24:51,675 | INFO | Skipping weekly_record_240_1995-07-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,675 | INFO | Processing file: weekly_record_241_1995-07-31T00-00-00.000.json
    2026-04-15 19:24:51,694 | INFO | Skipping weekly_record_241_1995-07-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,694 | INFO | Processing file: weekly_record_242_1995-08-07T00-00-00.000.json
    2026-04-15 19:24:51,722 | INFO | Skipping weekly_record_242_1995-08-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,722 | INFO | Processing file: weekly_record_243_1995-08-14T00-00-00.000.json
    2026-04-15 19:24:51,738 | INFO | Skipping weekly_record_243_1995-08-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,739 | INFO | Processing file: weekly_record_244_1995-08-21T00-00-00.000.json
    2026-04-15 19:24:51,766 | INFO | Skipping weekly_record_244_1995-08-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,767 | INFO | Processing file: weekly_record_245_1995-08-28T00-00-00.000.json
    2026-04-15 19:24:51,788 | INFO | Skipping weekly_record_245_1995-08-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,789 | INFO | Processing file: weekly_record_246_1995-09-04T00-00-00.000.json
    2026-04-15 19:24:51,816 | INFO | Skipping weekly_record_246_1995-09-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,817 | INFO | Processing file: weekly_record_247_1995-09-11T00-00-00.000.json
    2026-04-15 19:24:51,836 | INFO | Skipping weekly_record_247_1995-09-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,837 | INFO | Processing file: weekly_record_248_1995-09-18T00-00-00.000.json
    2026-04-15 19:24:51,861 | INFO | Skipping weekly_record_248_1995-09-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,861 | INFO | Processing file: weekly_record_249_1995-09-25T00-00-00.000.json
    2026-04-15 19:24:51,879 | INFO | Skipping weekly_record_249_1995-09-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,882 | INFO | Processing file: weekly_record_24_1991-06-03T00-00-00.000.json
    2026-04-15 19:24:51,906 | INFO | Skipping weekly_record_24_1991-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,907 | INFO | Processing file: weekly_record_250_1995-10-02T00-00-00.000.json
    2026-04-15 19:24:51,924 | INFO | Skipping weekly_record_250_1995-10-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,924 | INFO | Processing file: weekly_record_251_1995-10-09T00-00-00.000.json
    2026-04-15 19:24:51,951 | INFO | Skipping weekly_record_251_1995-10-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,952 | INFO | Processing file: weekly_record_252_1995-10-16T00-00-00.000.json
    2026-04-15 19:24:51,977 | INFO | Skipping weekly_record_252_1995-10-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,977 | INFO | Processing file: weekly_record_253_1995-10-23T00-00-00.000.json
    2026-04-15 19:24:51,996 | INFO | Skipping weekly_record_253_1995-10-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:51,996 | INFO | Processing file: weekly_record_254_1995-10-30T00-00-00.000.json
    2026-04-15 19:24:52,022 | INFO | Skipping weekly_record_254_1995-10-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,023 | INFO | Processing file: weekly_record_255_1995-11-06T00-00-00.000.json
    2026-04-15 19:24:52,051 | INFO | Skipping weekly_record_255_1995-11-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,052 | INFO | Processing file: weekly_record_256_1995-11-13T00-00-00.000.json
    2026-04-15 19:24:52,093 | INFO | Skipping weekly_record_256_1995-11-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,103 | INFO | Processing file: weekly_record_257_1995-11-20T00-00-00.000.json
    2026-04-15 19:24:52,126 | INFO | Skipping weekly_record_257_1995-11-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,127 | INFO | Processing file: weekly_record_258_1995-11-27T00-00-00.000.json
    2026-04-15 19:24:52,148 | INFO | Skipping weekly_record_258_1995-11-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,149 | INFO | Processing file: weekly_record_259_1995-12-04T00-00-00.000.json
    2026-04-15 19:24:52,165 | INFO | Skipping weekly_record_259_1995-12-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,166 | INFO | Processing file: weekly_record_25_1991-06-10T00-00-00.000.json
    2026-04-15 19:24:52,188 | INFO | Skipping weekly_record_25_1991-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,189 | INFO | Processing file: weekly_record_260_1995-12-11T00-00-00.000.json
    2026-04-15 19:24:52,213 | INFO | Skipping weekly_record_260_1995-12-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,213 | INFO | Processing file: weekly_record_261_1995-12-18T00-00-00.000.json
    2026-04-15 19:24:52,240 | INFO | Skipping weekly_record_261_1995-12-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,240 | INFO | Processing file: weekly_record_262_1995-12-25T00-00-00.000.json
    2026-04-15 19:24:52,256 | INFO | Skipping weekly_record_262_1995-12-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,257 | INFO | Processing file: weekly_record_263_1996-01-01T00-00-00.000.json
    2026-04-15 19:24:52,282 | INFO | Skipping weekly_record_263_1996-01-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,283 | INFO | Processing file: weekly_record_264_1996-01-08T00-00-00.000.json
    2026-04-15 19:24:52,315 | INFO | Skipping weekly_record_264_1996-01-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,316 | INFO | Processing file: weekly_record_265_1996-01-15T00-00-00.000.json
    2026-04-15 19:24:52,342 | INFO | Skipping weekly_record_265_1996-01-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,342 | INFO | Processing file: weekly_record_266_1996-01-22T00-00-00.000.json
    2026-04-15 19:24:52,359 | INFO | Skipping weekly_record_266_1996-01-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,360 | INFO | Processing file: weekly_record_267_1996-01-29T00-00-00.000.json
    2026-04-15 19:24:52,385 | INFO | Skipping weekly_record_267_1996-01-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,386 | INFO | Processing file: weekly_record_268_1996-02-05T00-00-00.000.json
    2026-04-15 19:24:52,403 | INFO | Skipping weekly_record_268_1996-02-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,403 | INFO | Processing file: weekly_record_269_1996-02-12T00-00-00.000.json
    2026-04-15 19:24:52,430 | INFO | Skipping weekly_record_269_1996-02-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,431 | INFO | Processing file: weekly_record_26_1991-06-17T00-00-00.000.json
    2026-04-15 19:24:52,449 | INFO | Skipping weekly_record_26_1991-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,449 | INFO | Processing file: weekly_record_270_1996-02-19T00-00-00.000.json
    2026-04-15 19:24:52,475 | INFO | Skipping weekly_record_270_1996-02-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,475 | INFO | Processing file: weekly_record_271_1996-02-26T00-00-00.000.json
    2026-04-15 19:24:52,502 | INFO | Skipping weekly_record_271_1996-02-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,503 | INFO | Processing file: weekly_record_272_1996-03-04T00-00-00.000.json
    2026-04-15 19:24:52,530 | INFO | Skipping weekly_record_272_1996-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,531 | INFO | Processing file: weekly_record_273_1996-03-11T00-00-00.000.json
    2026-04-15 19:24:52,546 | INFO | Skipping weekly_record_273_1996-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,547 | INFO | Processing file: weekly_record_274_1996-03-18T00-00-00.000.json
    2026-04-15 19:24:52,570 | INFO | Skipping weekly_record_274_1996-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,571 | INFO | Processing file: weekly_record_275_1996-03-25T00-00-00.000.json
    2026-04-15 19:24:52,589 | INFO | Skipping weekly_record_275_1996-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,590 | INFO | Processing file: weekly_record_276_1996-04-01T00-00-00.000.json
    2026-04-15 19:24:52,613 | INFO | Skipping weekly_record_276_1996-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,613 | INFO | Processing file: weekly_record_277_1996-04-08T00-00-00.000.json
    2026-04-15 19:24:52,632 | INFO | Skipping weekly_record_277_1996-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,633 | INFO | Processing file: weekly_record_278_1996-04-15T00-00-00.000.json
    2026-04-15 19:24:52,656 | INFO | Skipping weekly_record_278_1996-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,657 | INFO | Processing file: weekly_record_279_1996-04-22T00-00-00.000.json
    2026-04-15 19:24:52,687 | INFO | Skipping weekly_record_279_1996-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,690 | INFO | Processing file: weekly_record_27_1991-06-24T00-00-00.000.json
    2026-04-15 19:24:52,715 | INFO | Skipping weekly_record_27_1991-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,715 | INFO | Processing file: weekly_record_280_1996-04-29T00-00-00.000.json
    2026-04-15 19:24:52,742 | INFO | Skipping weekly_record_280_1996-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,743 | INFO | Processing file: weekly_record_281_1996-05-06T00-00-00.000.json
    2026-04-15 19:24:52,759 | INFO | Skipping weekly_record_281_1996-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,759 | INFO | Processing file: weekly_record_282_1996-05-13T00-00-00.000.json
    2026-04-15 19:24:52,789 | INFO | Skipping weekly_record_282_1996-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,789 | INFO | Processing file: weekly_record_283_1996-05-20T00-00-00.000.json
    2026-04-15 19:24:52,803 | INFO | Skipping weekly_record_283_1996-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,804 | INFO | Processing file: weekly_record_284_1996-05-27T00-00-00.000.json
    2026-04-15 19:24:52,831 | INFO | Skipping weekly_record_284_1996-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,832 | INFO | Processing file: weekly_record_285_1996-06-03T00-00-00.000.json
    2026-04-15 19:24:52,857 | INFO | Skipping weekly_record_285_1996-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,858 | INFO | Processing file: weekly_record_286_1996-06-10T00-00-00.000.json
    2026-04-15 19:24:52,875 | INFO | Skipping weekly_record_286_1996-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,875 | INFO | Processing file: weekly_record_287_1996-06-17T00-00-00.000.json
    2026-04-15 19:24:52,902 | INFO | Skipping weekly_record_287_1996-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,903 | INFO | Processing file: weekly_record_288_1996-06-24T00-00-00.000.json
    2026-04-15 19:24:52,919 | INFO | Skipping weekly_record_288_1996-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,920 | INFO | Processing file: weekly_record_289_1996-07-01T00-00-00.000.json
    2026-04-15 19:24:52,949 | INFO | Skipping weekly_record_289_1996-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,949 | INFO | Processing file: weekly_record_28_1991-07-01T00-00-00.000.json
    2026-04-15 19:24:52,964 | INFO | Skipping weekly_record_28_1991-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,965 | INFO | Processing file: weekly_record_290_1996-07-08T00-00-00.000.json
    2026-04-15 19:24:52,985 | INFO | Skipping weekly_record_290_1996-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:52,985 | INFO | Processing file: weekly_record_291_1996-07-15T00-00-00.000.json
    2026-04-15 19:24:53,002 | INFO | Skipping weekly_record_291_1996-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,003 | INFO | Processing file: weekly_record_292_1996-07-22T00-00-00.000.json
    2026-04-15 19:24:53,032 | INFO | Skipping weekly_record_292_1996-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,033 | INFO | Processing file: weekly_record_293_1996-07-29T00-00-00.000.json
    2026-04-15 19:24:53,049 | INFO | Skipping weekly_record_293_1996-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,050 | INFO | Processing file: weekly_record_294_1996-08-05T00-00-00.000.json
    2026-04-15 19:24:53,075 | INFO | Skipping weekly_record_294_1996-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,076 | INFO | Processing file: weekly_record_295_1996-08-12T00-00-00.000.json
    2026-04-15 19:24:53,093 | INFO | Skipping weekly_record_295_1996-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,094 | INFO | Processing file: weekly_record_296_1996-08-19T00-00-00.000.json
    2026-04-15 19:24:53,121 | INFO | Skipping weekly_record_296_1996-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,122 | INFO | Processing file: weekly_record_297_1996-08-26T00-00-00.000.json
    2026-04-15 19:24:53,138 | INFO | Skipping weekly_record_297_1996-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,138 | INFO | Processing file: weekly_record_298_1996-09-02T00-00-00.000.json
    2026-04-15 19:24:53,164 | INFO | Skipping weekly_record_298_1996-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,165 | INFO | Processing file: weekly_record_299_1996-09-09T00-00-00.000.json
    2026-04-15 19:24:53,180 | INFO | Skipping weekly_record_299_1996-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,180 | INFO | Processing file: weekly_record_29_1991-07-08T00-00-00.000.json
    2026-04-15 19:24:53,207 | INFO | Skipping weekly_record_29_1991-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,208 | INFO | Processing file: weekly_record_2_1990-11-19T00-00-00.000.json
    2026-04-15 19:24:53,225 | INFO | Skipping weekly_record_2_1990-11-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,226 | INFO | Processing file: weekly_record_300_1996-09-16T00-00-00.000.json
    2026-04-15 19:24:53,274 | INFO | Skipping weekly_record_300_1996-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,280 | INFO | Processing file: weekly_record_301_1996-09-23T00-00-00.000.json
    2026-04-15 19:24:53,296 | INFO | Skipping weekly_record_301_1996-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,297 | INFO | Processing file: weekly_record_302_1996-09-30T00-00-00.000.json
    2026-04-15 19:24:53,314 | INFO | Skipping weekly_record_302_1996-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,315 | INFO | Processing file: weekly_record_303_1996-10-07T00-00-00.000.json
    2026-04-15 19:24:53,342 | INFO | Skipping weekly_record_303_1996-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,343 | INFO | Processing file: weekly_record_304_1996-10-14T00-00-00.000.json
    2026-04-15 19:24:53,360 | INFO | Skipping weekly_record_304_1996-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,362 | INFO | Processing file: weekly_record_305_1996-10-21T00-00-00.000.json
    2026-04-15 19:24:53,386 | INFO | Skipping weekly_record_305_1996-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,387 | INFO | Processing file: weekly_record_306_1996-10-28T00-00-00.000.json
    2026-04-15 19:24:53,403 | INFO | Skipping weekly_record_306_1996-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,405 | INFO | Processing file: weekly_record_307_1996-11-04T00-00-00.000.json
    2026-04-15 19:24:53,431 | INFO | Skipping weekly_record_307_1996-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,431 | INFO | Processing file: weekly_record_308_1996-11-11T00-00-00.000.json
    2026-04-15 19:24:53,449 | INFO | Skipping weekly_record_308_1996-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,449 | INFO | Processing file: weekly_record_309_1996-11-18T00-00-00.000.json
    2026-04-15 19:24:53,475 | INFO | Skipping weekly_record_309_1996-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,476 | INFO | Processing file: weekly_record_30_1991-07-15T00-00-00.000.json
    2026-04-15 19:24:53,493 | INFO | Skipping weekly_record_30_1991-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,493 | INFO | Processing file: weekly_record_310_1996-11-25T00-00-00.000.json
    2026-04-15 19:24:53,516 | INFO | Skipping weekly_record_310_1996-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,516 | INFO | Processing file: weekly_record_311_1996-12-02T00-00-00.000.json
    2026-04-15 19:24:53,533 | INFO | Skipping weekly_record_311_1996-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,534 | INFO | Processing file: weekly_record_312_1996-12-09T00-00-00.000.json
    2026-04-15 19:24:53,561 | INFO | Skipping weekly_record_312_1996-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,562 | INFO | Processing file: weekly_record_313_1996-12-16T00-00-00.000.json
    2026-04-15 19:24:53,580 | INFO | Skipping weekly_record_313_1996-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,583 | INFO | Processing file: weekly_record_314_1996-12-23T00-00-00.000.json
    2026-04-15 19:24:53,606 | INFO | Skipping weekly_record_314_1996-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,606 | INFO | Processing file: weekly_record_315_1996-12-30T00-00-00.000.json
    2026-04-15 19:24:53,624 | INFO | Skipping weekly_record_315_1996-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,625 | INFO | Processing file: weekly_record_316_1997-01-06T00-00-00.000.json
    2026-04-15 19:24:53,651 | INFO | Skipping weekly_record_316_1997-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,651 | INFO | Processing file: weekly_record_317_1997-01-13T00-00-00.000.json
    2026-04-15 19:24:53,670 | INFO | Skipping weekly_record_317_1997-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,670 | INFO | Processing file: weekly_record_318_1997-01-20T00-00-00.000.json
    2026-04-15 19:24:53,695 | INFO | Skipping weekly_record_318_1997-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,696 | INFO | Processing file: weekly_record_319_1997-01-27T00-00-00.000.json
    2026-04-15 19:24:53,714 | INFO | Skipping weekly_record_319_1997-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,714 | INFO | Processing file: weekly_record_31_1991-07-22T00-00-00.000.json
    2026-04-15 19:24:53,736 | INFO | Skipping weekly_record_31_1991-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,737 | INFO | Processing file: weekly_record_320_1997-02-03T00-00-00.000.json
    2026-04-15 19:24:53,754 | INFO | Skipping weekly_record_320_1997-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,757 | INFO | Processing file: weekly_record_321_1997-02-10T00-00-00.000.json
    2026-04-15 19:24:53,782 | INFO | Skipping weekly_record_321_1997-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,783 | INFO | Processing file: weekly_record_322_1997-02-17T00-00-00.000.json
    2026-04-15 19:24:53,799 | INFO | Skipping weekly_record_322_1997-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,814 | INFO | Processing file: weekly_record_323_1997-02-24T00-00-00.000.json
    2026-04-15 19:24:53,836 | INFO | Skipping weekly_record_323_1997-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,837 | INFO | Processing file: weekly_record_324_1997-03-03T00-00-00.000.json
    2026-04-15 19:24:53,854 | INFO | Skipping weekly_record_324_1997-03-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,854 | INFO | Processing file: weekly_record_325_1997-03-10T00-00-00.000.json
    2026-04-15 19:24:53,880 | INFO | Skipping weekly_record_325_1997-03-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,880 | INFO | Processing file: weekly_record_326_1997-03-17T00-00-00.000.json
    2026-04-15 19:24:53,898 | INFO | Skipping weekly_record_326_1997-03-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,899 | INFO | Processing file: weekly_record_327_1997-03-24T00-00-00.000.json
    2026-04-15 19:24:53,926 | INFO | Skipping weekly_record_327_1997-03-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,926 | INFO | Processing file: weekly_record_328_1997-03-31T00-00-00.000.json
    2026-04-15 19:24:53,945 | INFO | Skipping weekly_record_328_1997-03-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,947 | INFO | Processing file: weekly_record_329_1997-04-07T00-00-00.000.json
    2026-04-15 19:24:53,970 | INFO | Skipping weekly_record_329_1997-04-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,970 | INFO | Processing file: weekly_record_32_1991-07-29T00-00-00.000.json
    2026-04-15 19:24:53,988 | INFO | Skipping weekly_record_32_1991-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:53,989 | INFO | Processing file: weekly_record_330_1997-04-14T00-00-00.000.json
    2026-04-15 19:24:54,011 | INFO | Skipping weekly_record_330_1997-04-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,011 | INFO | Processing file: weekly_record_331_1997-04-21T00-00-00.000.json
    2026-04-15 19:24:54,029 | INFO | Skipping weekly_record_331_1997-04-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,030 | INFO | Processing file: weekly_record_332_1997-04-28T00-00-00.000.json
    2026-04-15 19:24:54,056 | INFO | Skipping weekly_record_332_1997-04-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,057 | INFO | Processing file: weekly_record_333_1997-05-05T00-00-00.000.json
    2026-04-15 19:24:54,073 | INFO | Skipping weekly_record_333_1997-05-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,074 | INFO | Processing file: weekly_record_334_1997-05-12T00-00-00.000.json
    2026-04-15 19:24:54,098 | INFO | Skipping weekly_record_334_1997-05-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,099 | INFO | Processing file: weekly_record_335_1997-05-19T00-00-00.000.json
    2026-04-15 19:24:54,117 | INFO | Skipping weekly_record_335_1997-05-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,119 | INFO | Processing file: weekly_record_336_1997-05-26T00-00-00.000.json
    2026-04-15 19:24:54,144 | INFO | Skipping weekly_record_336_1997-05-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,144 | INFO | Processing file: weekly_record_337_1997-06-02T00-00-00.000.json
    2026-04-15 19:24:54,160 | INFO | Skipping weekly_record_337_1997-06-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,161 | INFO | Processing file: weekly_record_338_1997-06-09T00-00-00.000.json
    2026-04-15 19:24:54,184 | INFO | Skipping weekly_record_338_1997-06-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,184 | INFO | Processing file: weekly_record_339_1997-06-16T00-00-00.000.json
    2026-04-15 19:24:54,203 | INFO | Skipping weekly_record_339_1997-06-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,204 | INFO | Processing file: weekly_record_33_1991-08-05T00-00-00.000.json
    2026-04-15 19:24:54,230 | INFO | Skipping weekly_record_33_1991-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,231 | INFO | Processing file: weekly_record_340_1997-06-23T00-00-00.000.json
    2026-04-15 19:24:54,247 | INFO | Skipping weekly_record_340_1997-06-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,247 | INFO | Processing file: weekly_record_341_1997-06-30T00-00-00.000.json
    2026-04-15 19:24:54,275 | INFO | Skipping weekly_record_341_1997-06-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,275 | INFO | Processing file: weekly_record_342_1997-07-07T00-00-00.000.json
    2026-04-15 19:24:54,304 | INFO | Skipping weekly_record_342_1997-07-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,326 | INFO | Processing file: weekly_record_343_1997-07-14T00-00-00.000.json
    2026-04-15 19:24:54,350 | INFO | Skipping weekly_record_343_1997-07-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,351 | INFO | Processing file: weekly_record_344_1997-07-21T00-00-00.000.json
    2026-04-15 19:24:54,368 | INFO | Skipping weekly_record_344_1997-07-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,368 | INFO | Processing file: weekly_record_345_1997-07-28T00-00-00.000.json
    2026-04-15 19:24:54,395 | INFO | Skipping weekly_record_345_1997-07-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,395 | INFO | Processing file: weekly_record_346_1997-08-04T00-00-00.000.json
    2026-04-15 19:24:54,413 | INFO | Skipping weekly_record_346_1997-08-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,414 | INFO | Processing file: weekly_record_347_1997-08-11T00-00-00.000.json
    2026-04-15 19:24:54,440 | INFO | Skipping weekly_record_347_1997-08-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,440 | INFO | Processing file: weekly_record_348_1997-08-18T00-00-00.000.json
    2026-04-15 19:24:54,458 | INFO | Skipping weekly_record_348_1997-08-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,458 | INFO | Processing file: weekly_record_349_1997-08-25T00-00-00.000.json
    2026-04-15 19:24:54,476 | INFO | Skipping weekly_record_349_1997-08-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,476 | INFO | Processing file: weekly_record_34_1991-08-12T00-00-00.000.json
    2026-04-15 19:24:54,493 | INFO | Skipping weekly_record_34_1991-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,494 | INFO | Processing file: weekly_record_350_1997-09-01T00-00-00.000.json
    2026-04-15 19:24:54,522 | INFO | Skipping weekly_record_350_1997-09-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,522 | INFO | Processing file: weekly_record_351_1997-09-08T00-00-00.000.json
    2026-04-15 19:24:54,538 | INFO | Skipping weekly_record_351_1997-09-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,539 | INFO | Processing file: weekly_record_352_1997-09-15T00-00-00.000.json
    2026-04-15 19:24:54,569 | INFO | Skipping weekly_record_352_1997-09-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,569 | INFO | Processing file: weekly_record_353_1997-09-22T00-00-00.000.json
    2026-04-15 19:24:54,589 | INFO | Skipping weekly_record_353_1997-09-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,589 | INFO | Processing file: weekly_record_354_1997-09-29T00-00-00.000.json
    2026-04-15 19:24:54,615 | INFO | Skipping weekly_record_354_1997-09-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,616 | INFO | Processing file: weekly_record_355_1997-10-06T00-00-00.000.json
    2026-04-15 19:24:54,634 | INFO | Skipping weekly_record_355_1997-10-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,634 | INFO | Processing file: weekly_record_356_1997-10-13T00-00-00.000.json
    2026-04-15 19:24:54,661 | INFO | Skipping weekly_record_356_1997-10-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,661 | INFO | Processing file: weekly_record_357_1997-10-20T00-00-00.000.json
    2026-04-15 19:24:54,681 | INFO | Skipping weekly_record_357_1997-10-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,681 | INFO | Processing file: weekly_record_358_1997-10-27T00-00-00.000.json
    2026-04-15 19:24:54,705 | INFO | Skipping weekly_record_358_1997-10-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,705 | INFO | Processing file: weekly_record_359_1997-11-03T00-00-00.000.json
    2026-04-15 19:24:54,731 | INFO | Skipping weekly_record_359_1997-11-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,731 | INFO | Processing file: weekly_record_35_1991-08-19T00-00-00.000.json
    2026-04-15 19:24:54,759 | INFO | Skipping weekly_record_35_1991-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,760 | INFO | Processing file: weekly_record_360_1997-11-10T00-00-00.000.json
    2026-04-15 19:24:54,787 | INFO | Skipping weekly_record_360_1997-11-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,815 | INFO | Processing file: weekly_record_361_1997-11-17T00-00-00.000.json
    2026-04-15 19:24:54,835 | INFO | Skipping weekly_record_361_1997-11-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,835 | INFO | Processing file: weekly_record_362_1997-11-24T00-00-00.000.json
    2026-04-15 19:24:54,862 | INFO | Skipping weekly_record_362_1997-11-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,863 | INFO | Processing file: weekly_record_363_1997-12-01T00-00-00.000.json
    2026-04-15 19:24:54,890 | INFO | Skipping weekly_record_363_1997-12-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,891 | INFO | Processing file: weekly_record_364_1997-12-08T00-00-00.000.json
    2026-04-15 19:24:54,908 | INFO | Skipping weekly_record_364_1997-12-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,908 | INFO | Processing file: weekly_record_365_1997-12-15T00-00-00.000.json
    2026-04-15 19:24:54,933 | INFO | Skipping weekly_record_365_1997-12-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,933 | INFO | Processing file: weekly_record_366_1997-12-22T00-00-00.000.json
    2026-04-15 19:24:54,951 | INFO | Skipping weekly_record_366_1997-12-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,952 | INFO | Processing file: weekly_record_367_1997-12-29T00-00-00.000.json
    2026-04-15 19:24:54,976 | INFO | Skipping weekly_record_367_1997-12-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,977 | INFO | Processing file: weekly_record_368_1998-01-05T00-00-00.000.json
    2026-04-15 19:24:54,994 | INFO | Skipping weekly_record_368_1998-01-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:54,995 | INFO | Processing file: weekly_record_369_1998-01-12T00-00-00.000.json
    2026-04-15 19:24:55,020 | INFO | Skipping weekly_record_369_1998-01-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,020 | INFO | Processing file: weekly_record_36_1991-08-26T00-00-00.000.json
    2026-04-15 19:24:55,047 | INFO | Skipping weekly_record_36_1991-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,047 | INFO | Processing file: weekly_record_370_1998-01-19T00-00-00.000.json
    2026-04-15 19:24:55,074 | INFO | Skipping weekly_record_370_1998-01-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,074 | INFO | Processing file: weekly_record_371_1998-01-26T00-00-00.000.json
    2026-04-15 19:24:55,091 | INFO | Skipping weekly_record_371_1998-01-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,092 | INFO | Processing file: weekly_record_372_1998-02-02T00-00-00.000.json
    2026-04-15 19:24:55,113 | INFO | Skipping weekly_record_372_1998-02-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,114 | INFO | Processing file: weekly_record_373_1998-02-09T00-00-00.000.json
    2026-04-15 19:24:55,137 | INFO | Skipping weekly_record_373_1998-02-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,138 | INFO | Processing file: weekly_record_374_1998-02-16T00-00-00.000.json
    2026-04-15 19:24:55,154 | INFO | Skipping weekly_record_374_1998-02-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,155 | INFO | Processing file: weekly_record_375_1998-02-23T00-00-00.000.json
    2026-04-15 19:24:55,182 | INFO | Skipping weekly_record_375_1998-02-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,183 | INFO | Processing file: weekly_record_376_1998-03-02T00-00-00.000.json
    2026-04-15 19:24:55,199 | INFO | Skipping weekly_record_376_1998-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,200 | INFO | Processing file: weekly_record_377_1998-03-09T00-00-00.000.json
    2026-04-15 19:24:55,226 | INFO | Skipping weekly_record_377_1998-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,226 | INFO | Processing file: weekly_record_378_1998-03-16T00-00-00.000.json
    2026-04-15 19:24:55,243 | INFO | Skipping weekly_record_378_1998-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,244 | INFO | Processing file: weekly_record_379_1998-03-23T00-00-00.000.json
    2026-04-15 19:24:55,270 | INFO | Skipping weekly_record_379_1998-03-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,271 | INFO | Processing file: weekly_record_37_1991-09-02T00-00-00.000.json
    2026-04-15 19:24:55,297 | INFO | Skipping weekly_record_37_1991-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,298 | INFO | Processing file: weekly_record_380_1998-03-30T00-00-00.000.json
    2026-04-15 19:24:55,325 | INFO | Skipping weekly_record_380_1998-03-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,337 | INFO | Processing file: weekly_record_381_1998-04-06T00-00-00.000.json
    2026-04-15 19:24:55,359 | INFO | Skipping weekly_record_381_1998-04-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,360 | INFO | Processing file: weekly_record_382_1998-04-13T00-00-00.000.json
    2026-04-15 19:24:55,375 | INFO | Skipping weekly_record_382_1998-04-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,376 | INFO | Processing file: weekly_record_383_1998-04-20T00-00-00.000.json
    2026-04-15 19:24:55,393 | INFO | Skipping weekly_record_383_1998-04-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,393 | INFO | Processing file: weekly_record_384_1998-04-27T00-00-00.000.json
    2026-04-15 19:24:55,412 | INFO | Skipping weekly_record_384_1998-04-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,412 | INFO | Processing file: weekly_record_385_1998-05-04T00-00-00.000.json
    2026-04-15 19:24:55,436 | INFO | Skipping weekly_record_385_1998-05-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,436 | INFO | Processing file: weekly_record_386_1998-05-11T00-00-00.000.json
    2026-04-15 19:24:55,454 | INFO | Skipping weekly_record_386_1998-05-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,454 | INFO | Processing file: weekly_record_387_1998-05-18T00-00-00.000.json
    2026-04-15 19:24:55,764 | INFO | Skipping weekly_record_387_1998-05-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,765 | INFO | Processing file: weekly_record_388_1998-05-25T00-00-00.000.json
    2026-04-15 19:24:55,783 | INFO | Skipping weekly_record_388_1998-05-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,784 | INFO | Processing file: weekly_record_389_1998-06-01T00-00-00.000.json
    2026-04-15 19:24:55,810 | INFO | Skipping weekly_record_389_1998-06-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,811 | INFO | Processing file: weekly_record_38_1991-09-09T00-00-00.000.json
    2026-04-15 19:24:55,826 | INFO | Skipping weekly_record_38_1991-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,826 | INFO | Processing file: weekly_record_390_1998-06-08T00-00-00.000.json
    2026-04-15 19:24:55,856 | INFO | Skipping weekly_record_390_1998-06-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,857 | INFO | Processing file: weekly_record_391_1998-06-15T00-00-00.000.json
    2026-04-15 19:24:55,881 | INFO | Skipping weekly_record_391_1998-06-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,882 | INFO | Processing file: weekly_record_392_1998-06-22T00-00-00.000.json
    2026-04-15 19:24:55,910 | INFO | Skipping weekly_record_392_1998-06-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,911 | INFO | Processing file: weekly_record_393_1998-06-29T00-00-00.000.json
    2026-04-15 19:24:55,942 | INFO | Skipping weekly_record_393_1998-06-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,943 | INFO | Processing file: weekly_record_394_1998-07-06T00-00-00.000.json
    2026-04-15 19:24:55,960 | INFO | Skipping weekly_record_394_1998-07-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,961 | INFO | Processing file: weekly_record_395_1998-07-13T00-00-00.000.json
    2026-04-15 19:24:55,987 | INFO | Skipping weekly_record_395_1998-07-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:55,987 | INFO | Processing file: weekly_record_396_1998-07-20T00-00-00.000.json
    2026-04-15 19:24:56,007 | INFO | Skipping weekly_record_396_1998-07-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,008 | INFO | Processing file: weekly_record_397_1998-07-27T00-00-00.000.json
    2026-04-15 19:24:56,029 | INFO | Skipping weekly_record_397_1998-07-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,030 | INFO | Processing file: weekly_record_398_1998-08-03T00-00-00.000.json
    2026-04-15 19:24:56,047 | INFO | Skipping weekly_record_398_1998-08-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,048 | INFO | Processing file: weekly_record_399_1998-08-10T00-00-00.000.json
    2026-04-15 19:24:56,088 | INFO | Skipping weekly_record_399_1998-08-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,089 | INFO | Processing file: weekly_record_39_1991-09-16T00-00-00.000.json
    2026-04-15 19:24:56,106 | INFO | Skipping weekly_record_39_1991-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,109 | INFO | Processing file: weekly_record_3_1990-11-26T00-00-00.000.json
    2026-04-15 19:24:56,129 | INFO | Skipping weekly_record_3_1990-11-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,130 | INFO | Processing file: weekly_record_400_1998-08-17T00-00-00.000.json
    2026-04-15 19:24:56,147 | INFO | Skipping weekly_record_400_1998-08-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,147 | INFO | Processing file: weekly_record_401_1998-08-24T00-00-00.000.json
    2026-04-15 19:24:56,172 | INFO | Skipping weekly_record_401_1998-08-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,172 | INFO | Processing file: weekly_record_402_1998-08-31T00-00-00.000.json
    2026-04-15 19:24:56,193 | INFO | Skipping weekly_record_402_1998-08-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,194 | INFO | Processing file: weekly_record_403_1998-09-07T00-00-00.000.json
    2026-04-15 19:24:56,217 | INFO | Skipping weekly_record_403_1998-09-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,217 | INFO | Processing file: weekly_record_404_1998-09-14T00-00-00.000.json
    2026-04-15 19:24:56,236 | INFO | Skipping weekly_record_404_1998-09-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,236 | INFO | Processing file: weekly_record_405_1998-09-21T00-00-00.000.json
    2026-04-15 19:24:56,262 | INFO | Skipping weekly_record_405_1998-09-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,262 | INFO | Processing file: weekly_record_406_1998-09-28T00-00-00.000.json
    2026-04-15 19:24:56,289 | INFO | Skipping weekly_record_406_1998-09-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,289 | INFO | Processing file: weekly_record_407_1998-10-05T00-00-00.000.json
    2026-04-15 19:24:56,318 | INFO | Skipping weekly_record_407_1998-10-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,318 | INFO | Processing file: weekly_record_408_1998-10-12T00-00-00.000.json
    2026-04-15 19:24:56,344 | INFO | Skipping weekly_record_408_1998-10-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,344 | INFO | Processing file: weekly_record_409_1998-10-19T00-00-00.000.json
    2026-04-15 19:24:56,364 | INFO | Skipping weekly_record_409_1998-10-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,365 | INFO | Processing file: weekly_record_40_1991-09-23T00-00-00.000.json
    2026-04-15 19:24:56,393 | INFO | Skipping weekly_record_40_1991-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,393 | INFO | Processing file: weekly_record_410_1998-10-26T00-00-00.000.json
    2026-04-15 19:24:56,411 | INFO | Skipping weekly_record_410_1998-10-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,412 | INFO | Processing file: weekly_record_411_1998-11-02T00-00-00.000.json
    2026-04-15 19:24:56,438 | INFO | Skipping weekly_record_411_1998-11-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,438 | INFO | Processing file: weekly_record_412_1998-11-09T00-00-00.000.json
    2026-04-15 19:24:56,457 | INFO | Skipping weekly_record_412_1998-11-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,458 | INFO | Processing file: weekly_record_413_1998-11-16T00-00-00.000.json
    2026-04-15 19:24:56,484 | INFO | Skipping weekly_record_413_1998-11-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,485 | INFO | Processing file: weekly_record_414_1998-11-23T00-00-00.000.json
    2026-04-15 19:24:56,514 | INFO | Skipping weekly_record_414_1998-11-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,514 | INFO | Processing file: weekly_record_415_1998-11-30T00-00-00.000.json
    2026-04-15 19:24:56,538 | INFO | Skipping weekly_record_415_1998-11-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,539 | INFO | Processing file: weekly_record_416_1998-12-07T00-00-00.000.json
    2026-04-15 19:24:56,557 | INFO | Skipping weekly_record_416_1998-12-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,558 | INFO | Processing file: weekly_record_417_1998-12-14T00-00-00.000.json
    2026-04-15 19:24:56,598 | INFO | Skipping weekly_record_417_1998-12-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,599 | INFO | Processing file: weekly_record_418_1998-12-21T00-00-00.000.json
    2026-04-15 19:24:56,616 | INFO | Skipping weekly_record_418_1998-12-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,617 | INFO | Processing file: weekly_record_419_1998-12-28T00-00-00.000.json
    2026-04-15 19:24:56,652 | INFO | Skipping weekly_record_419_1998-12-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,652 | INFO | Processing file: weekly_record_41_1991-09-30T00-00-00.000.json
    2026-04-15 19:24:56,671 | INFO | Skipping weekly_record_41_1991-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,672 | INFO | Processing file: weekly_record_420_1999-01-04T00-00-00.000.json
    2026-04-15 19:24:56,695 | INFO | Skipping weekly_record_420_1999-01-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,696 | INFO | Processing file: weekly_record_421_1999-01-11T00-00-00.000.json
    2026-04-15 19:24:56,724 | INFO | Skipping weekly_record_421_1999-01-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,725 | INFO | Processing file: weekly_record_422_1999-01-18T00-00-00.000.json
    2026-04-15 19:24:56,745 | INFO | Skipping weekly_record_422_1999-01-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,745 | INFO | Processing file: weekly_record_423_1999-01-25T00-00-00.000.json
    2026-04-15 19:24:56,763 | INFO | Skipping weekly_record_423_1999-01-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,763 | INFO | Processing file: weekly_record_424_1999-02-01T00-00-00.000.json
    2026-04-15 19:24:56,791 | INFO | Skipping weekly_record_424_1999-02-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,791 | INFO | Processing file: weekly_record_425_1999-02-08T00-00-00.000.json
    2026-04-15 19:24:56,812 | INFO | Skipping weekly_record_425_1999-02-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,812 | INFO | Processing file: weekly_record_426_1999-02-15T00-00-00.000.json
    2026-04-15 19:24:56,837 | INFO | Skipping weekly_record_426_1999-02-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,838 | INFO | Processing file: weekly_record_427_1999-02-22T00-00-00.000.json
    2026-04-15 19:24:56,853 | INFO | Skipping weekly_record_427_1999-02-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,854 | INFO | Processing file: weekly_record_428_1999-03-01T00-00-00.000.json
    2026-04-15 19:24:56,880 | INFO | Skipping weekly_record_428_1999-03-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,881 | INFO | Processing file: weekly_record_429_1999-03-08T00-00-00.000.json
    2026-04-15 19:24:56,898 | INFO | Skipping weekly_record_429_1999-03-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,899 | INFO | Processing file: weekly_record_42_1991-10-07T00-00-00.000.json
    2026-04-15 19:24:56,925 | INFO | Skipping weekly_record_42_1991-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,925 | INFO | Processing file: weekly_record_430_1999-03-15T00-00-00.000.json
    2026-04-15 19:24:56,945 | INFO | Skipping weekly_record_430_1999-03-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,946 | INFO | Processing file: weekly_record_431_1999-03-22T00-00-00.000.json
    2026-04-15 19:24:56,960 | INFO | Skipping weekly_record_431_1999-03-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,961 | INFO | Processing file: weekly_record_432_1999-03-29T00-00-00.000.json
    2026-04-15 19:24:56,984 | INFO | Skipping weekly_record_432_1999-03-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:56,985 | INFO | Processing file: weekly_record_433_1999-04-05T00-00-00.000.json
    2026-04-15 19:24:57,011 | INFO | Skipping weekly_record_433_1999-04-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,012 | INFO | Processing file: weekly_record_434_1999-04-12T00-00-00.000.json
    2026-04-15 19:24:57,029 | INFO | Skipping weekly_record_434_1999-04-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,030 | INFO | Processing file: weekly_record_435_1999-04-19T00-00-00.000.json
    2026-04-15 19:24:57,048 | INFO | Skipping weekly_record_435_1999-04-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,048 | INFO | Processing file: weekly_record_436_1999-04-26T00-00-00.000.json
    2026-04-15 19:24:57,085 | INFO | Skipping weekly_record_436_1999-04-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,085 | INFO | Processing file: weekly_record_437_1999-05-03T00-00-00.000.json
    2026-04-15 19:24:57,100 | INFO | Skipping weekly_record_437_1999-05-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,101 | INFO | Processing file: weekly_record_438_1999-05-10T00-00-00.000.json
    2026-04-15 19:24:57,130 | INFO | Skipping weekly_record_438_1999-05-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,130 | INFO | Processing file: weekly_record_439_1999-05-17T00-00-00.000.json
    2026-04-15 19:24:57,146 | INFO | Skipping weekly_record_439_1999-05-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,146 | INFO | Processing file: weekly_record_43_1991-10-14T00-00-00.000.json
    2026-04-15 19:24:57,173 | INFO | Skipping weekly_record_43_1991-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,173 | INFO | Processing file: weekly_record_440_1999-05-24T00-00-00.000.json
    2026-04-15 19:24:57,190 | INFO | Skipping weekly_record_440_1999-05-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,190 | INFO | Processing file: weekly_record_441_1999-05-31T00-00-00.000.json
    2026-04-15 19:24:57,216 | INFO | Skipping weekly_record_441_1999-05-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,217 | INFO | Processing file: weekly_record_442_1999-06-07T00-00-00.000.json
    2026-04-15 19:24:57,244 | INFO | Skipping weekly_record_442_1999-06-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,244 | INFO | Processing file: weekly_record_443_1999-06-14T00-00-00.000.json
    2026-04-15 19:24:57,273 | INFO | Skipping weekly_record_443_1999-06-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,274 | INFO | Processing file: weekly_record_444_1999-06-21T00-00-00.000.json
    2026-04-15 19:24:57,297 | INFO | Skipping weekly_record_444_1999-06-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,297 | INFO | Processing file: weekly_record_445_1999-06-28T00-00-00.000.json
    2026-04-15 19:24:57,319 | INFO | Skipping weekly_record_445_1999-06-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,319 | INFO | Processing file: weekly_record_446_1999-07-05T00-00-00.000.json
    2026-04-15 19:24:57,338 | INFO | Skipping weekly_record_446_1999-07-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,339 | INFO | Processing file: weekly_record_447_1999-07-12T00-00-00.000.json
    2026-04-15 19:24:57,365 | INFO | Skipping weekly_record_447_1999-07-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,366 | INFO | Processing file: weekly_record_448_1999-07-19T00-00-00.000.json
    2026-04-15 19:24:57,384 | INFO | Skipping weekly_record_448_1999-07-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,385 | INFO | Processing file: weekly_record_449_1999-07-26T00-00-00.000.json
    2026-04-15 19:24:57,411 | INFO | Skipping weekly_record_449_1999-07-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,411 | INFO | Processing file: weekly_record_44_1991-10-21T00-00-00.000.json
    2026-04-15 19:24:57,429 | INFO | Skipping weekly_record_44_1991-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,430 | INFO | Processing file: weekly_record_450_1999-08-02T00-00-00.000.json
    2026-04-15 19:24:57,459 | INFO | Skipping weekly_record_450_1999-08-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,459 | INFO | Processing file: weekly_record_451_1999-08-09T00-00-00.000.json
    2026-04-15 19:24:57,474 | INFO | Skipping weekly_record_451_1999-08-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,475 | INFO | Processing file: weekly_record_452_1999-08-16T00-00-00.000.json
    2026-04-15 19:24:57,493 | INFO | Skipping weekly_record_452_1999-08-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,493 | INFO | Processing file: weekly_record_453_1999-08-23T00-00-00.000.json
    2026-04-15 19:24:57,511 | INFO | Skipping weekly_record_453_1999-08-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,514 | INFO | Processing file: weekly_record_454_1999-08-30T00-00-00.000.json
    2026-04-15 19:24:57,538 | INFO | Skipping weekly_record_454_1999-08-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,549 | INFO | Processing file: weekly_record_455_1999-09-06T00-00-00.000.json
    2026-04-15 19:24:57,582 | INFO | Skipping weekly_record_455_1999-09-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,583 | INFO | Processing file: weekly_record_456_1999-09-13T00-00-00.000.json
    2026-04-15 19:24:57,600 | INFO | Skipping weekly_record_456_1999-09-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,600 | INFO | Processing file: weekly_record_457_1999-09-20T00-00-00.000.json
    2026-04-15 19:24:57,626 | INFO | Skipping weekly_record_457_1999-09-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,626 | INFO | Processing file: weekly_record_458_1999-09-27T00-00-00.000.json
    2026-04-15 19:24:57,644 | INFO | Skipping weekly_record_458_1999-09-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,645 | INFO | Processing file: weekly_record_459_1999-10-04T00-00-00.000.json
    2026-04-15 19:24:57,671 | INFO | Skipping weekly_record_459_1999-10-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,672 | INFO | Processing file: weekly_record_45_1991-10-28T00-00-00.000.json
    2026-04-15 19:24:57,688 | INFO | Skipping weekly_record_45_1991-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,689 | INFO | Processing file: weekly_record_460_1999-10-11T00-00-00.000.json
    2026-04-15 19:24:57,716 | INFO | Skipping weekly_record_460_1999-10-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,717 | INFO | Processing file: weekly_record_461_1999-10-18T00-00-00.000.json
    2026-04-15 19:24:57,734 | INFO | Skipping weekly_record_461_1999-10-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,734 | INFO | Processing file: weekly_record_462_1999-10-25T00-00-00.000.json
    2026-04-15 19:24:57,764 | INFO | Skipping weekly_record_462_1999-10-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,765 | INFO | Processing file: weekly_record_463_1999-11-01T00-00-00.000.json
    2026-04-15 19:24:57,785 | INFO | Skipping weekly_record_463_1999-11-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,786 | INFO | Processing file: weekly_record_464_1999-11-08T00-00-00.000.json
    2026-04-15 19:24:57,811 | INFO | Skipping weekly_record_464_1999-11-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,811 | INFO | Processing file: weekly_record_465_1999-11-15T00-00-00.000.json
    2026-04-15 19:24:57,840 | INFO | Skipping weekly_record_465_1999-11-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,840 | INFO | Processing file: weekly_record_466_1999-11-22T00-00-00.000.json
    2026-04-15 19:24:57,861 | INFO | Skipping weekly_record_466_1999-11-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,862 | INFO | Processing file: weekly_record_467_1999-11-29T00-00-00.000.json
    2026-04-15 19:24:57,883 | INFO | Skipping weekly_record_467_1999-11-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,883 | INFO | Processing file: weekly_record_468_1999-12-06T00-00-00.000.json
    2026-04-15 19:24:57,900 | INFO | Skipping weekly_record_468_1999-12-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,901 | INFO | Processing file: weekly_record_469_1999-12-13T00-00-00.000.json
    2026-04-15 19:24:57,922 | INFO | Skipping weekly_record_469_1999-12-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,923 | INFO | Processing file: weekly_record_46_1991-11-04T00-00-00.000.json
    2026-04-15 19:24:57,943 | INFO | Skipping weekly_record_46_1991-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,943 | INFO | Processing file: weekly_record_470_1999-12-20T00-00-00.000.json
    2026-04-15 19:24:57,972 | INFO | Skipping weekly_record_470_1999-12-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:57,973 | INFO | Processing file: weekly_record_471_1999-12-27T00-00-00.000.json
    2026-04-15 19:24:57,999 | INFO | Skipping weekly_record_471_1999-12-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,015 | INFO | Processing file: weekly_record_472_2000-01-03T00-00-00.000.json
    2026-04-15 19:24:58,052 | INFO | Skipping weekly_record_472_2000-01-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,052 | INFO | Processing file: weekly_record_473_2000-01-10T00-00-00.000.json
    2026-04-15 19:24:58,072 | INFO | Skipping weekly_record_473_2000-01-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,073 | INFO | Processing file: weekly_record_474_2000-01-17T00-00-00.000.json
    2026-04-15 19:24:58,098 | INFO | Skipping weekly_record_474_2000-01-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,099 | INFO | Processing file: weekly_record_475_2000-01-24T00-00-00.000.json
    2026-04-15 19:24:58,116 | INFO | Skipping weekly_record_475_2000-01-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,116 | INFO | Processing file: weekly_record_476_2000-01-31T00-00-00.000.json
    2026-04-15 19:24:58,143 | INFO | Skipping weekly_record_476_2000-01-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,144 | INFO | Processing file: weekly_record_477_2000-02-07T00-00-00.000.json
    2026-04-15 19:24:58,171 | INFO | Skipping weekly_record_477_2000-02-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,171 | INFO | Processing file: weekly_record_478_2000-02-14T00-00-00.000.json
    2026-04-15 19:24:58,200 | INFO | Skipping weekly_record_478_2000-02-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,200 | INFO | Processing file: weekly_record_479_2000-02-21T00-00-00.000.json
    2026-04-15 19:24:58,224 | INFO | Skipping weekly_record_479_2000-02-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,225 | INFO | Processing file: weekly_record_47_1991-11-11T00-00-00.000.json
    2026-04-15 19:24:58,247 | INFO | Skipping weekly_record_47_1991-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,247 | INFO | Processing file: weekly_record_480_2000-02-28T00-00-00.000.json
    2026-04-15 19:24:58,265 | INFO | Skipping weekly_record_480_2000-02-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,266 | INFO | Processing file: weekly_record_481_2000-03-06T00-00-00.000.json
    2026-04-15 19:24:58,283 | INFO | Skipping weekly_record_481_2000-03-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,284 | INFO | Processing file: weekly_record_482_2000-03-13T00-00-00.000.json
    2026-04-15 19:24:58,309 | INFO | Skipping weekly_record_482_2000-03-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,310 | INFO | Processing file: weekly_record_483_2000-03-20T00-00-00.000.json
    2026-04-15 19:24:58,329 | INFO | Skipping weekly_record_483_2000-03-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,330 | INFO | Processing file: weekly_record_484_2000-03-27T00-00-00.000.json
    2026-04-15 19:24:58,356 | INFO | Skipping weekly_record_484_2000-03-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,357 | INFO | Processing file: weekly_record_485_2000-04-03T00-00-00.000.json
    2026-04-15 19:24:58,381 | INFO | Skipping weekly_record_485_2000-04-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,381 | INFO | Processing file: weekly_record_486_2000-04-10T00-00-00.000.json
    2026-04-15 19:24:58,409 | INFO | Skipping weekly_record_486_2000-04-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,409 | INFO | Processing file: weekly_record_487_2000-04-17T00-00-00.000.json
    2026-04-15 19:24:58,445 | INFO | Skipping weekly_record_487_2000-04-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,446 | INFO | Processing file: weekly_record_488_2000-04-24T00-00-00.000.json
    2026-04-15 19:24:58,467 | INFO | Skipping weekly_record_488_2000-04-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,467 | INFO | Processing file: weekly_record_489_2000-05-01T00-00-00.000.json
    2026-04-15 19:24:58,484 | INFO | Skipping weekly_record_489_2000-05-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,485 | INFO | Processing file: weekly_record_48_1991-11-18T00-00-00.000.json
    2026-04-15 19:24:58,513 | INFO | Skipping weekly_record_48_1991-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,523 | INFO | Processing file: weekly_record_490_2000-05-08T00-00-00.000.json
    2026-04-15 19:24:58,548 | INFO | Skipping weekly_record_490_2000-05-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,549 | INFO | Processing file: weekly_record_491_2000-05-15T00-00-00.000.json
    2026-04-15 19:24:58,565 | INFO | Skipping weekly_record_491_2000-05-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,565 | INFO | Processing file: weekly_record_492_2000-05-22T00-00-00.000.json
    2026-04-15 19:24:58,584 | INFO | Skipping weekly_record_492_2000-05-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,585 | INFO | Processing file: weekly_record_493_2000-05-29T00-00-00.000.json
    2026-04-15 19:24:58,612 | INFO | Skipping weekly_record_493_2000-05-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,612 | INFO | Processing file: weekly_record_494_2000-06-05T00-00-00.000.json
    2026-04-15 19:24:58,629 | INFO | Skipping weekly_record_494_2000-06-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,629 | INFO | Processing file: weekly_record_495_2000-06-12T00-00-00.000.json
    2026-04-15 19:24:58,656 | INFO | Skipping weekly_record_495_2000-06-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,657 | INFO | Processing file: weekly_record_496_2000-06-19T00-00-00.000.json
    2026-04-15 19:24:58,683 | INFO | Skipping weekly_record_496_2000-06-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,684 | INFO | Processing file: weekly_record_497_2000-06-26T00-00-00.000.json
    2026-04-15 19:24:58,702 | INFO | Skipping weekly_record_497_2000-06-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,702 | INFO | Processing file: weekly_record_498_2000-07-03T00-00-00.000.json
    2026-04-15 19:24:58,729 | INFO | Skipping weekly_record_498_2000-07-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,729 | INFO | Processing file: weekly_record_499_2000-07-10T00-00-00.000.json
    2026-04-15 19:24:58,755 | INFO | Skipping weekly_record_499_2000-07-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,756 | INFO | Processing file: weekly_record_49_1991-11-25T00-00-00.000.json
    2026-04-15 19:24:58,772 | INFO | Skipping weekly_record_49_1991-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,775 | INFO | Processing file: weekly_record_4_1990-12-03T00-00-00.000.json
    2026-04-15 19:24:58,799 | INFO | Skipping weekly_record_4_1990-12-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,800 | INFO | Processing file: weekly_record_500_2000-07-17T00-00-00.000.json
    2026-04-15 19:24:58,817 | INFO | Skipping weekly_record_500_2000-07-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,818 | INFO | Processing file: weekly_record_501_2000-07-24T00-00-00.000.json
    2026-04-15 19:24:58,843 | INFO | Skipping weekly_record_501_2000-07-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,844 | INFO | Processing file: weekly_record_502_2000-07-31T00-00-00.000.json
    2026-04-15 19:24:58,862 | INFO | Skipping weekly_record_502_2000-07-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,862 | INFO | Processing file: weekly_record_503_2000-08-07T00-00-00.000.json
    2026-04-15 19:24:58,889 | INFO | Skipping weekly_record_503_2000-08-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,890 | INFO | Processing file: weekly_record_504_2000-08-14T00-00-00.000.json
    2026-04-15 19:24:58,918 | INFO | Skipping weekly_record_504_2000-08-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,918 | INFO | Processing file: weekly_record_505_2000-08-21T00-00-00.000.json
    2026-04-15 19:24:58,944 | INFO | Skipping weekly_record_505_2000-08-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,944 | INFO | Processing file: weekly_record_506_2000-08-28T00-00-00.000.json
    2026-04-15 19:24:58,960 | INFO | Skipping weekly_record_506_2000-08-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,964 | INFO | Processing file: weekly_record_507_2000-09-04T00-00-00.000.json
    2026-04-15 19:24:58,984 | INFO | Skipping weekly_record_507_2000-09-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:58,985 | INFO | Processing file: weekly_record_508_2000-09-11T00-00-00.000.json
    2026-04-15 19:24:59,012 | INFO | Skipping weekly_record_508_2000-09-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,028 | INFO | Processing file: weekly_record_509_2000-09-18T00-00-00.000.json
    2026-04-15 19:24:59,053 | INFO | Skipping weekly_record_509_2000-09-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,058 | INFO | Processing file: weekly_record_50_1991-12-02T00-00-00.000.json
    2026-04-15 19:24:59,087 | INFO | Skipping weekly_record_50_1991-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,087 | INFO | Processing file: weekly_record_510_2000-09-25T00-00-00.000.json
    2026-04-15 19:24:59,116 | INFO | Skipping weekly_record_510_2000-09-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,117 | INFO | Processing file: weekly_record_511_2000-10-02T00-00-00.000.json
    2026-04-15 19:24:59,142 | INFO | Skipping weekly_record_511_2000-10-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,143 | INFO | Processing file: weekly_record_512_2000-10-09T00-00-00.000.json
    2026-04-15 19:24:59,169 | INFO | Skipping weekly_record_512_2000-10-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,170 | INFO | Processing file: weekly_record_513_2000-10-16T00-00-00.000.json
    2026-04-15 19:24:59,186 | INFO | Skipping weekly_record_513_2000-10-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,186 | INFO | Processing file: weekly_record_514_2000-10-23T00-00-00.000.json
    2026-04-15 19:24:59,203 | INFO | Skipping weekly_record_514_2000-10-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,204 | INFO | Processing file: weekly_record_515_2000-10-30T00-00-00.000.json
    2026-04-15 19:24:59,234 | INFO | Skipping weekly_record_515_2000-10-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,234 | INFO | Processing file: weekly_record_516_2000-11-06T00-00-00.000.json
    2026-04-15 19:24:59,253 | INFO | Skipping weekly_record_516_2000-11-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,254 | INFO | Processing file: weekly_record_517_2000-11-13T00-00-00.000.json
    2026-04-15 19:24:59,280 | INFO | Skipping weekly_record_517_2000-11-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,281 | INFO | Processing file: weekly_record_518_2000-11-20T00-00-00.000.json
    2026-04-15 19:24:59,307 | INFO | Skipping weekly_record_518_2000-11-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,308 | INFO | Processing file: weekly_record_519_2000-11-27T00-00-00.000.json
    2026-04-15 19:24:59,325 | INFO | Skipping weekly_record_519_2000-11-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,326 | INFO | Processing file: weekly_record_51_1991-12-09T00-00-00.000.json
    2026-04-15 19:24:59,352 | INFO | Skipping weekly_record_51_1991-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,354 | INFO | Processing file: weekly_record_520_2000-12-04T00-00-00.000.json
    2026-04-15 19:24:59,370 | INFO | Skipping weekly_record_520_2000-12-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,372 | INFO | Processing file: weekly_record_521_2000-12-11T00-00-00.000.json
    2026-04-15 19:24:59,399 | INFO | Skipping weekly_record_521_2000-12-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,400 | INFO | Processing file: weekly_record_522_2000-12-18T00-00-00.000.json
    2026-04-15 19:24:59,424 | INFO | Skipping weekly_record_522_2000-12-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,424 | INFO | Processing file: weekly_record_523_2000-12-25T00-00-00.000.json
    2026-04-15 19:24:59,454 | INFO | Skipping weekly_record_523_2000-12-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,454 | INFO | Processing file: weekly_record_524_2001-01-01T00-00-00.000.json
    2026-04-15 19:24:59,479 | INFO | Skipping weekly_record_524_2001-01-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,480 | INFO | Processing file: weekly_record_525_2001-01-08T00-00-00.000.json
    2026-04-15 19:24:59,508 | INFO | Skipping weekly_record_525_2001-01-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,511 | INFO | Processing file: weekly_record_526_2001-01-15T00-00-00.000.json
    2026-04-15 19:24:59,546 | INFO | Skipping weekly_record_526_2001-01-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,547 | INFO | Processing file: weekly_record_527_2001-01-22T00-00-00.000.json
    2026-04-15 19:24:59,572 | INFO | Skipping weekly_record_527_2001-01-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,572 | INFO | Processing file: weekly_record_528_2001-01-29T00-00-00.000.json
    2026-04-15 19:24:59,590 | INFO | Skipping weekly_record_528_2001-01-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,592 | INFO | Processing file: weekly_record_529_2001-02-05T00-00-00.000.json
    2026-04-15 19:24:59,609 | INFO | Skipping weekly_record_529_2001-02-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,610 | INFO | Processing file: weekly_record_52_1991-12-16T00-00-00.000.json
    2026-04-15 19:24:59,628 | INFO | Skipping weekly_record_52_1991-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,628 | INFO | Processing file: weekly_record_530_2001-02-12T00-00-00.000.json
    2026-04-15 19:24:59,653 | INFO | Skipping weekly_record_530_2001-02-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,653 | INFO | Processing file: weekly_record_531_2001-02-19T00-00-00.000.json
    2026-04-15 19:24:59,673 | INFO | Skipping weekly_record_531_2001-02-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,674 | INFO | Processing file: weekly_record_532_2001-02-26T00-00-00.000.json
    2026-04-15 19:24:59,699 | INFO | Skipping weekly_record_532_2001-02-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,699 | INFO | Processing file: weekly_record_533_2001-03-05T00-00-00.000.json
    2026-04-15 19:24:59,720 | INFO | Skipping weekly_record_533_2001-03-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,721 | INFO | Processing file: weekly_record_534_2001-03-12T00-00-00.000.json
    2026-04-15 19:24:59,747 | INFO | Skipping weekly_record_534_2001-03-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,747 | INFO | Processing file: weekly_record_535_2001-03-19T00-00-00.000.json
    2026-04-15 19:24:59,776 | INFO | Skipping weekly_record_535_2001-03-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,776 | INFO | Processing file: weekly_record_536_2001-03-26T00-00-00.000.json
    2026-04-15 19:24:59,794 | INFO | Skipping weekly_record_536_2001-03-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,794 | INFO | Processing file: weekly_record_537_2001-04-02T00-00-00.000.json
    2026-04-15 19:24:59,820 | INFO | Skipping weekly_record_537_2001-04-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,821 | INFO | Processing file: weekly_record_538_2001-04-09T00-00-00.000.json
    2026-04-15 19:24:59,838 | INFO | Skipping weekly_record_538_2001-04-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,838 | INFO | Processing file: weekly_record_539_2001-04-16T00-00-00.000.json
    2026-04-15 19:24:59,856 | INFO | Skipping weekly_record_539_2001-04-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,860 | INFO | Processing file: weekly_record_53_1991-12-23T00-00-00.000.json
    2026-04-15 19:24:59,874 | INFO | Skipping weekly_record_53_1991-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,875 | INFO | Processing file: weekly_record_540_2001-04-23T00-00-00.000.json
    2026-04-15 19:24:59,901 | INFO | Skipping weekly_record_540_2001-04-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,902 | INFO | Processing file: weekly_record_541_2001-04-30T00-00-00.000.json
    2026-04-15 19:24:59,928 | INFO | Skipping weekly_record_541_2001-04-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,929 | INFO | Processing file: weekly_record_542_2001-05-07T00-00-00.000.json
    2026-04-15 19:24:59,954 | INFO | Skipping weekly_record_542_2001-05-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:24:59,963 | INFO | Processing file: weekly_record_543_2001-05-14T00-00-00.000.json
    2026-04-15 19:25:00,029 | INFO | Skipping weekly_record_543_2001-05-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,029 | INFO | Processing file: weekly_record_544_2001-05-21T00-00-00.000.json
    2026-04-15 19:25:00,053 | INFO | Skipping weekly_record_544_2001-05-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,053 | INFO | Processing file: weekly_record_545_2001-05-28T00-00-00.000.json
    2026-04-15 19:25:00,075 | INFO | Skipping weekly_record_545_2001-05-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,075 | INFO | Processing file: weekly_record_546_2001-06-04T00-00-00.000.json
    2026-04-15 19:25:00,102 | INFO | Skipping weekly_record_546_2001-06-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,102 | INFO | Processing file: weekly_record_547_2001-06-11T00-00-00.000.json
    2026-04-15 19:25:00,130 | INFO | Skipping weekly_record_547_2001-06-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,131 | INFO | Processing file: weekly_record_548_2001-06-18T00-00-00.000.json
    2026-04-15 19:25:00,146 | INFO | Skipping weekly_record_548_2001-06-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,147 | INFO | Processing file: weekly_record_549_2001-06-25T00-00-00.000.json
    2026-04-15 19:25:00,175 | INFO | Skipping weekly_record_549_2001-06-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,176 | INFO | Processing file: weekly_record_54_1991-12-30T00-00-00.000.json
    2026-04-15 19:25:00,200 | INFO | Skipping weekly_record_54_1991-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,200 | INFO | Processing file: weekly_record_550_2001-07-02T00-00-00.000.json
    2026-04-15 19:25:00,224 | INFO | Skipping weekly_record_550_2001-07-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,225 | INFO | Processing file: weekly_record_551_2001-07-09T00-00-00.000.json
    2026-04-15 19:25:00,252 | INFO | Skipping weekly_record_551_2001-07-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,253 | INFO | Processing file: weekly_record_552_2001-07-16T00-00-00.000.json
    2026-04-15 19:25:00,269 | INFO | Skipping weekly_record_552_2001-07-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,270 | INFO | Processing file: weekly_record_553_2001-07-23T00-00-00.000.json
    2026-04-15 19:25:00,288 | INFO | Skipping weekly_record_553_2001-07-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,288 | INFO | Processing file: weekly_record_554_2001-07-30T00-00-00.000.json
    2026-04-15 19:25:00,313 | INFO | Skipping weekly_record_554_2001-07-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,314 | INFO | Processing file: weekly_record_555_2001-08-06T00-00-00.000.json
    2026-04-15 19:25:00,332 | INFO | Skipping weekly_record_555_2001-08-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,332 | INFO | Processing file: weekly_record_556_2001-08-13T00-00-00.000.json
    2026-04-15 19:25:00,360 | INFO | Skipping weekly_record_556_2001-08-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,361 | INFO | Processing file: weekly_record_557_2001-08-20T00-00-00.000.json
    2026-04-15 19:25:00,385 | INFO | Skipping weekly_record_557_2001-08-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,386 | INFO | Processing file: weekly_record_558_2001-08-27T00-00-00.000.json
    2026-04-15 19:25:00,412 | INFO | Skipping weekly_record_558_2001-08-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,413 | INFO | Processing file: weekly_record_559_2001-09-03T00-00-00.000.json
    2026-04-15 19:25:00,435 | INFO | Skipping weekly_record_559_2001-09-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,436 | INFO | Processing file: weekly_record_55_1992-01-06T00-00-00.000.json
    2026-04-15 19:25:00,452 | INFO | Skipping weekly_record_55_1992-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,457 | INFO | Processing file: weekly_record_560_2001-09-10T00-00-00.000.json
    2026-04-15 19:25:00,486 | INFO | Skipping weekly_record_560_2001-09-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,490 | INFO | Processing file: weekly_record_561_2001-09-17T00-00-00.000.json
    2026-04-15 19:25:00,511 | INFO | Skipping weekly_record_561_2001-09-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,512 | INFO | Processing file: weekly_record_562_2001-09-24T00-00-00.000.json
    2026-04-15 19:25:00,538 | INFO | Skipping weekly_record_562_2001-09-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,538 | INFO | Processing file: weekly_record_563_2001-10-01T00-00-00.000.json
    2026-04-15 19:25:00,556 | INFO | Skipping weekly_record_563_2001-10-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,557 | INFO | Processing file: weekly_record_564_2001-10-08T00-00-00.000.json
    2026-04-15 19:25:00,584 | INFO | Skipping weekly_record_564_2001-10-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,584 | INFO | Processing file: weekly_record_565_2001-10-15T00-00-00.000.json
    2026-04-15 19:25:00,602 | INFO | Skipping weekly_record_565_2001-10-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,604 | INFO | Processing file: weekly_record_566_2001-10-22T00-00-00.000.json
    2026-04-15 19:25:00,630 | INFO | Skipping weekly_record_566_2001-10-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,630 | INFO | Processing file: weekly_record_567_2001-10-29T00-00-00.000.json
    2026-04-15 19:25:00,652 | INFO | Skipping weekly_record_567_2001-10-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,652 | INFO | Processing file: weekly_record_568_2001-11-05T00-00-00.000.json
    2026-04-15 19:25:00,669 | INFO | Skipping weekly_record_568_2001-11-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,669 | INFO | Processing file: weekly_record_569_2001-11-12T00-00-00.000.json
    2026-04-15 19:25:00,705 | INFO | Skipping weekly_record_569_2001-11-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,706 | INFO | Processing file: weekly_record_56_1992-01-13T00-00-00.000.json
    2026-04-15 19:25:00,732 | INFO | Skipping weekly_record_56_1992-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,733 | INFO | Processing file: weekly_record_570_2001-11-19T00-00-00.000.json
    2026-04-15 19:25:00,759 | INFO | Skipping weekly_record_570_2001-11-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,759 | INFO | Processing file: weekly_record_571_2001-11-26T00-00-00.000.json
    2026-04-15 19:25:00,776 | INFO | Skipping weekly_record_571_2001-11-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,777 | INFO | Processing file: weekly_record_572_2001-12-03T00-00-00.000.json
    2026-04-15 19:25:00,804 | INFO | Skipping weekly_record_572_2001-12-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,804 | INFO | Processing file: weekly_record_573_2001-12-10T00-00-00.000.json
    2026-04-15 19:25:00,824 | INFO | Skipping weekly_record_573_2001-12-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,824 | INFO | Processing file: weekly_record_574_2001-12-17T00-00-00.000.json
    2026-04-15 19:25:00,839 | INFO | Skipping weekly_record_574_2001-12-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,840 | INFO | Processing file: weekly_record_575_2001-12-24T00-00-00.000.json
    2026-04-15 19:25:00,857 | INFO | Skipping weekly_record_575_2001-12-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,858 | INFO | Processing file: weekly_record_576_2001-12-31T00-00-00.000.json
    2026-04-15 19:25:00,884 | INFO | Skipping weekly_record_576_2001-12-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,884 | INFO | Processing file: weekly_record_577_2002-01-07T00-00-00.000.json
    2026-04-15 19:25:00,920 | INFO | Skipping weekly_record_577_2002-01-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,930 | INFO | Processing file: weekly_record_578_2002-01-14T00-00-00.000.json
    2026-04-15 19:25:00,960 | INFO | Skipping weekly_record_578_2002-01-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,961 | INFO | Processing file: weekly_record_579_2002-01-21T00-00-00.000.json
    2026-04-15 19:25:00,975 | INFO | Skipping weekly_record_579_2002-01-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:00,976 | INFO | Processing file: weekly_record_57_1992-01-20T00-00-00.000.json
    2026-04-15 19:25:01,002 | INFO | Skipping weekly_record_57_1992-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,003 | INFO | Processing file: weekly_record_580_2002-01-28T00-00-00.000.json
    2026-04-15 19:25:01,025 | INFO | Skipping weekly_record_580_2002-01-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,026 | INFO | Processing file: weekly_record_581_2002-02-04T00-00-00.000.json
    2026-04-15 19:25:01,044 | INFO | Skipping weekly_record_581_2002-02-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,046 | INFO | Processing file: weekly_record_582_2002-02-11T00-00-00.000.json
    2026-04-15 19:25:01,069 | INFO | Skipping weekly_record_582_2002-02-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,070 | INFO | Processing file: weekly_record_583_2002-02-18T00-00-00.000.json
    2026-04-15 19:25:01,087 | INFO | Skipping weekly_record_583_2002-02-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,088 | INFO | Processing file: weekly_record_584_2002-02-25T00-00-00.000.json
    2026-04-15 19:25:01,110 | INFO | Skipping weekly_record_584_2002-02-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,111 | INFO | Processing file: weekly_record_585_2002-03-04T00-00-00.000.json
    2026-04-15 19:25:01,128 | INFO | Skipping weekly_record_585_2002-03-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,129 | INFO | Processing file: weekly_record_586_2002-03-11T00-00-00.000.json
    2026-04-15 19:25:01,146 | INFO | Skipping weekly_record_586_2002-03-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,146 | INFO | Processing file: weekly_record_587_2002-03-18T00-00-00.000.json
    2026-04-15 19:25:01,165 | INFO | Skipping weekly_record_587_2002-03-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,165 | INFO | Processing file: weekly_record_588_2002-03-25T00-00-00.000.json
    2026-04-15 19:25:01,190 | INFO | Skipping weekly_record_588_2002-03-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,190 | INFO | Processing file: weekly_record_589_2002-04-01T00-00-00.000.json
    2026-04-15 19:25:01,217 | INFO | Skipping weekly_record_589_2002-04-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,217 | INFO | Processing file: weekly_record_58_1992-01-27T00-00-00.000.json
    2026-04-15 19:25:01,249 | INFO | Skipping weekly_record_58_1992-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,249 | INFO | Processing file: weekly_record_590_2002-04-08T00-00-00.000.json
    2026-04-15 19:25:01,276 | INFO | Skipping weekly_record_590_2002-04-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,276 | INFO | Processing file: weekly_record_591_2002-04-15T00-00-00.000.json
    2026-04-15 19:25:01,293 | INFO | Skipping weekly_record_591_2002-04-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,293 | INFO | Processing file: weekly_record_592_2002-04-22T00-00-00.000.json
    2026-04-15 19:25:01,325 | INFO | Skipping weekly_record_592_2002-04-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,326 | INFO | Processing file: weekly_record_593_2002-04-29T00-00-00.000.json
    2026-04-15 19:25:01,343 | INFO | Skipping weekly_record_593_2002-04-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,343 | INFO | Processing file: weekly_record_594_2002-05-06T00-00-00.000.json
    2026-04-15 19:25:01,370 | INFO | Skipping weekly_record_594_2002-05-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,373 | INFO | Processing file: weekly_record_595_2002-05-13T00-00-00.000.json
    2026-04-15 19:25:01,398 | INFO | Skipping weekly_record_595_2002-05-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,404 | INFO | Processing file: weekly_record_596_2002-05-20T00-00-00.000.json
    2026-04-15 19:25:01,429 | INFO | Skipping weekly_record_596_2002-05-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,429 | INFO | Processing file: weekly_record_597_2002-05-27T00-00-00.000.json
    2026-04-15 19:25:01,447 | INFO | Skipping weekly_record_597_2002-05-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,447 | INFO | Processing file: weekly_record_598_2002-06-03T00-00-00.000.json
    2026-04-15 19:25:01,484 | INFO | Skipping weekly_record_598_2002-06-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,485 | INFO | Processing file: weekly_record_599_2002-06-10T00-00-00.000.json
    2026-04-15 19:25:01,505 | INFO | Skipping weekly_record_599_2002-06-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,505 | INFO | Processing file: weekly_record_59_1992-02-03T00-00-00.000.json
    2026-04-15 19:25:01,524 | INFO | Skipping weekly_record_59_1992-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,524 | INFO | Processing file: weekly_record_5_1991-01-21T00-00-00.000.json
    2026-04-15 19:25:01,549 | INFO | Skipping weekly_record_5_1991-01-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,550 | INFO | Processing file: weekly_record_600_2002-06-17T00-00-00.000.json
    2026-04-15 19:25:01,567 | INFO | Skipping weekly_record_600_2002-06-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,568 | INFO | Processing file: weekly_record_601_2002-06-24T00-00-00.000.json
    2026-04-15 19:25:01,595 | INFO | Skipping weekly_record_601_2002-06-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,595 | INFO | Processing file: weekly_record_602_2002-07-01T00-00-00.000.json
    2026-04-15 19:25:01,613 | INFO | Skipping weekly_record_602_2002-07-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,616 | INFO | Processing file: weekly_record_603_2002-07-08T00-00-00.000.json
    2026-04-15 19:25:01,639 | INFO | Skipping weekly_record_603_2002-07-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,640 | INFO | Processing file: weekly_record_604_2002-07-15T00-00-00.000.json
    2026-04-15 19:25:01,665 | INFO | Skipping weekly_record_604_2002-07-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,665 | INFO | Processing file: weekly_record_605_2002-07-22T00-00-00.000.json
    2026-04-15 19:25:01,690 | INFO | Skipping weekly_record_605_2002-07-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,691 | INFO | Processing file: weekly_record_606_2002-07-29T00-00-00.000.json
    2026-04-15 19:25:01,707 | INFO | Skipping weekly_record_606_2002-07-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,708 | INFO | Processing file: weekly_record_607_2002-08-05T00-00-00.000.json
    2026-04-15 19:25:01,742 | INFO | Skipping weekly_record_607_2002-08-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,742 | INFO | Processing file: weekly_record_608_2002-08-12T00-00-00.000.json
    2026-04-15 19:25:01,761 | INFO | Skipping weekly_record_608_2002-08-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,762 | INFO | Processing file: weekly_record_609_2002-08-19T00-00-00.000.json
    2026-04-15 19:25:01,788 | INFO | Skipping weekly_record_609_2002-08-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,789 | INFO | Processing file: weekly_record_60_1992-02-10T00-00-00.000.json
    2026-04-15 19:25:01,808 | INFO | Skipping weekly_record_60_1992-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,810 | INFO | Processing file: weekly_record_610_2002-08-26T00-00-00.000.json
    2026-04-15 19:25:01,875 | INFO | Skipping weekly_record_610_2002-08-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,875 | INFO | Processing file: weekly_record_611_2002-09-02T00-00-00.000.json
    2026-04-15 19:25:01,900 | INFO | Skipping weekly_record_611_2002-09-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,901 | INFO | Processing file: weekly_record_612_2002-09-09T00-00-00.000.json
    2026-04-15 19:25:01,920 | INFO | Skipping weekly_record_612_2002-09-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,920 | INFO | Processing file: weekly_record_613_2002-09-16T00-00-00.000.json
    2026-04-15 19:25:01,945 | INFO | Skipping weekly_record_613_2002-09-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,946 | INFO | Processing file: weekly_record_614_2002-09-23T00-00-00.000.json
    2026-04-15 19:25:01,965 | INFO | Skipping weekly_record_614_2002-09-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,966 | INFO | Processing file: weekly_record_615_2002-09-30T00-00-00.000.json
    2026-04-15 19:25:01,993 | INFO | Skipping weekly_record_615_2002-09-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:01,993 | INFO | Processing file: weekly_record_616_2002-10-07T00-00-00.000.json
    2026-04-15 19:25:02,011 | INFO | Skipping weekly_record_616_2002-10-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,011 | INFO | Processing file: weekly_record_617_2002-10-14T00-00-00.000.json
    2026-04-15 19:25:02,044 | INFO | Skipping weekly_record_617_2002-10-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,045 | INFO | Processing file: weekly_record_618_2002-10-21T00-00-00.000.json
    2026-04-15 19:25:02,060 | INFO | Skipping weekly_record_618_2002-10-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,061 | INFO | Processing file: weekly_record_619_2002-10-28T00-00-00.000.json
    2026-04-15 19:25:02,102 | INFO | Skipping weekly_record_619_2002-10-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,102 | INFO | Processing file: weekly_record_61_1992-02-17T00-00-00.000.json
    2026-04-15 19:25:02,118 | INFO | Skipping weekly_record_61_1992-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,119 | INFO | Processing file: weekly_record_620_2002-11-04T00-00-00.000.json
    2026-04-15 19:25:02,147 | INFO | Skipping weekly_record_620_2002-11-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,148 | INFO | Processing file: weekly_record_621_2002-11-11T00-00-00.000.json
    2026-04-15 19:25:02,163 | INFO | Skipping weekly_record_621_2002-11-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,164 | INFO | Processing file: weekly_record_622_2002-11-18T00-00-00.000.json
    2026-04-15 19:25:02,190 | INFO | Skipping weekly_record_622_2002-11-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,191 | INFO | Processing file: weekly_record_623_2002-11-25T00-00-00.000.json
    2026-04-15 19:25:02,208 | INFO | Skipping weekly_record_623_2002-11-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,209 | INFO | Processing file: weekly_record_624_2002-12-02T00-00-00.000.json
    2026-04-15 19:25:02,235 | INFO | Skipping weekly_record_624_2002-12-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,236 | INFO | Processing file: weekly_record_625_2002-12-09T00-00-00.000.json
    2026-04-15 19:25:02,265 | INFO | Skipping weekly_record_625_2002-12-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,265 | INFO | Processing file: weekly_record_626_2002-12-16T00-00-00.000.json
    2026-04-15 19:25:02,291 | INFO | Skipping weekly_record_626_2002-12-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,304 | INFO | Processing file: weekly_record_627_2002-12-23T00-00-00.000.json
    2026-04-15 19:25:02,325 | INFO | Skipping weekly_record_627_2002-12-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,325 | INFO | Processing file: weekly_record_628_2002-12-30T00-00-00.000.json
    2026-04-15 19:25:02,352 | INFO | Skipping weekly_record_628_2002-12-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,353 | INFO | Processing file: weekly_record_629_2003-01-06T00-00-00.000.json
    2026-04-15 19:25:02,371 | INFO | Skipping weekly_record_629_2003-01-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,372 | INFO | Processing file: weekly_record_62_1992-02-24T00-00-00.000.json
    2026-04-15 19:25:02,394 | INFO | Skipping weekly_record_62_1992-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,394 | INFO | Processing file: weekly_record_630_2003-01-13T00-00-00.000.json
    2026-04-15 19:25:02,428 | INFO | Skipping weekly_record_630_2003-01-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,429 | INFO | Processing file: weekly_record_631_2003-01-20T00-00-00.000.json
    2026-04-15 19:25:02,458 | INFO | Skipping weekly_record_631_2003-01-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,458 | INFO | Processing file: weekly_record_632_2003-01-27T00-00-00.000.json
    2026-04-15 19:25:02,475 | INFO | Skipping weekly_record_632_2003-01-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,475 | INFO | Processing file: weekly_record_633_2003-02-03T00-00-00.000.json
    2026-04-15 19:25:02,501 | INFO | Skipping weekly_record_633_2003-02-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,501 | INFO | Processing file: weekly_record_634_2003-02-10T00-00-00.000.json
    2026-04-15 19:25:02,519 | INFO | Skipping weekly_record_634_2003-02-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,519 | INFO | Processing file: weekly_record_635_2003-02-17T00-00-00.000.json
    2026-04-15 19:25:02,545 | INFO | Skipping weekly_record_635_2003-02-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,545 | INFO | Processing file: weekly_record_636_2003-02-24T00-00-00.000.json
    2026-04-15 19:25:02,579 | INFO | Skipping weekly_record_636_2003-02-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,580 | INFO | Processing file: weekly_record_637_2003-03-03T00-00-00.000.json
    2026-04-15 19:25:02,604 | INFO | Skipping weekly_record_637_2003-03-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,604 | INFO | Processing file: weekly_record_638_2003-03-10T00-00-00.000.json
    2026-04-15 19:25:02,621 | INFO | Skipping weekly_record_638_2003-03-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,622 | INFO | Processing file: weekly_record_639_2003-03-17T00-00-00.000.json
    2026-04-15 19:25:02,649 | INFO | Skipping weekly_record_639_2003-03-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,650 | INFO | Processing file: weekly_record_63_1992-03-02T00-00-00.000.json
    2026-04-15 19:25:02,667 | INFO | Skipping weekly_record_63_1992-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,667 | INFO | Processing file: weekly_record_640_2003-03-24T00-00-00.000.json
    2026-04-15 19:25:02,683 | INFO | Skipping weekly_record_640_2003-03-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,684 | INFO | Processing file: weekly_record_641_2003-03-31T00-00-00.000.json
    2026-04-15 19:25:02,712 | INFO | Skipping weekly_record_641_2003-03-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,713 | INFO | Processing file: weekly_record_642_2003-04-07T00-00-00.000.json
    2026-04-15 19:25:02,739 | INFO | Skipping weekly_record_642_2003-04-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,739 | INFO | Processing file: weekly_record_643_2003-04-14T00-00-00.000.json
    2026-04-15 19:25:02,770 | INFO | Skipping weekly_record_643_2003-04-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,770 | INFO | Processing file: weekly_record_644_2003-04-21T00-00-00.000.json
    2026-04-15 19:25:02,804 | INFO | Skipping weekly_record_644_2003-04-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,811 | INFO | Processing file: weekly_record_645_2003-04-28T00-00-00.000.json
    2026-04-15 19:25:02,849 | INFO | Skipping weekly_record_645_2003-04-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,849 | INFO | Processing file: weekly_record_646_2003-05-05T00-00-00.000.json
    2026-04-15 19:25:02,878 | INFO | Skipping weekly_record_646_2003-05-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,879 | INFO | Processing file: weekly_record_647_2003-05-12T00-00-00.000.json
    2026-04-15 19:25:02,907 | INFO | Skipping weekly_record_647_2003-05-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,907 | INFO | Processing file: weekly_record_648_2003-05-19T00-00-00.000.json
    2026-04-15 19:25:02,929 | INFO | Skipping weekly_record_648_2003-05-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,929 | INFO | Processing file: weekly_record_649_2003-05-26T00-00-00.000.json
    2026-04-15 19:25:02,946 | INFO | Skipping weekly_record_649_2003-05-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,946 | INFO | Processing file: weekly_record_64_1992-03-09T00-00-00.000.json
    2026-04-15 19:25:02,968 | INFO | Skipping weekly_record_64_1992-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,968 | INFO | Processing file: weekly_record_650_2003-06-02T00-00-00.000.json
    2026-04-15 19:25:02,997 | INFO | Skipping weekly_record_650_2003-06-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:02,997 | INFO | Processing file: weekly_record_651_2003-06-09T00-00-00.000.json
    2026-04-15 19:25:03,022 | INFO | Skipping weekly_record_651_2003-06-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,023 | INFO | Processing file: weekly_record_652_2003-06-16T00-00-00.000.json
    2026-04-15 19:25:03,048 | INFO | Skipping weekly_record_652_2003-06-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,049 | INFO | Processing file: weekly_record_653_2003-06-23T00-00-00.000.json
    2026-04-15 19:25:03,071 | INFO | Skipping weekly_record_653_2003-06-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,071 | INFO | Processing file: weekly_record_654_2003-06-30T00-00-00.000.json
    2026-04-15 19:25:03,094 | INFO | Skipping weekly_record_654_2003-06-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,094 | INFO | Processing file: weekly_record_655_2003-07-07T00-00-00.000.json
    2026-04-15 19:25:03,124 | INFO | Skipping weekly_record_655_2003-07-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,125 | INFO | Processing file: weekly_record_656_2003-07-14T00-00-00.000.json
    2026-04-15 19:25:03,149 | INFO | Skipping weekly_record_656_2003-07-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,149 | INFO | Processing file: weekly_record_657_2003-07-21T00-00-00.000.json
    2026-04-15 19:25:03,171 | INFO | Skipping weekly_record_657_2003-07-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,171 | INFO | Processing file: weekly_record_658_2003-07-28T00-00-00.000.json
    2026-04-15 19:25:03,198 | INFO | Skipping weekly_record_658_2003-07-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,198 | INFO | Processing file: weekly_record_659_2003-08-04T00-00-00.000.json
    2026-04-15 19:25:03,226 | INFO | Skipping weekly_record_659_2003-08-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,227 | INFO | Processing file: weekly_record_65_1992-03-16T00-00-00.000.json
    2026-04-15 19:25:03,244 | INFO | Skipping weekly_record_65_1992-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,245 | INFO | Processing file: weekly_record_660_2003-08-11T00-00-00.000.json
    2026-04-15 19:25:03,260 | INFO | Skipping weekly_record_660_2003-08-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,261 | INFO | Processing file: weekly_record_661_2003-08-18T00-00-00.000.json
    2026-04-15 19:25:03,279 | INFO | Skipping weekly_record_661_2003-08-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,280 | INFO | Processing file: weekly_record_662_2003-08-25T00-00-00.000.json
    2026-04-15 19:25:03,297 | INFO | Skipping weekly_record_662_2003-08-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,298 | INFO | Processing file: weekly_record_663_2003-09-01T00-00-00.000.json
    2026-04-15 19:25:03,330 | INFO | Skipping weekly_record_663_2003-09-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,337 | INFO | Processing file: weekly_record_664_2003-09-08T00-00-00.000.json
    2026-04-15 19:25:03,361 | INFO | Skipping weekly_record_664_2003-09-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,362 | INFO | Processing file: weekly_record_665_2003-09-15T00-00-00.000.json
    2026-04-15 19:25:03,390 | INFO | Skipping weekly_record_665_2003-09-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,391 | INFO | Processing file: weekly_record_666_2003-09-22T00-00-00.000.json
    2026-04-15 19:25:03,417 | INFO | Skipping weekly_record_666_2003-09-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,418 | INFO | Processing file: weekly_record_667_2003-09-29T00-00-00.000.json
    2026-04-15 19:25:03,444 | INFO | Skipping weekly_record_667_2003-09-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,445 | INFO | Processing file: weekly_record_668_2003-10-06T00-00-00.000.json
    2026-04-15 19:25:03,478 | INFO | Skipping weekly_record_668_2003-10-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,478 | INFO | Processing file: weekly_record_669_2003-10-13T00-00-00.000.json
    2026-04-15 19:25:03,497 | INFO | Skipping weekly_record_669_2003-10-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,498 | INFO | Processing file: weekly_record_66_1992-03-23T00-00-00.000.json
    2026-04-15 19:25:03,525 | INFO | Skipping weekly_record_66_1992-03-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,526 | INFO | Processing file: weekly_record_670_2003-10-20T00-00-00.000.json
    2026-04-15 19:25:03,543 | INFO | Skipping weekly_record_670_2003-10-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,543 | INFO | Processing file: weekly_record_671_2003-10-27T00-00-00.000.json
    2026-04-15 19:25:03,570 | INFO | Skipping weekly_record_671_2003-10-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,571 | INFO | Processing file: weekly_record_672_2003-11-03T00-00-00.000.json
    2026-04-15 19:25:03,603 | INFO | Skipping weekly_record_672_2003-11-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,603 | INFO | Processing file: weekly_record_673_2003-11-10T00-00-00.000.json
    2026-04-15 19:25:03,630 | INFO | Skipping weekly_record_673_2003-11-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,630 | INFO | Processing file: weekly_record_674_2003-11-17T00-00-00.000.json
    2026-04-15 19:25:03,657 | INFO | Skipping weekly_record_674_2003-11-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,658 | INFO | Processing file: weekly_record_675_2003-11-24T00-00-00.000.json
    2026-04-15 19:25:03,683 | INFO | Skipping weekly_record_675_2003-11-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,684 | INFO | Processing file: weekly_record_676_2003-12-01T00-00-00.000.json
    2026-04-15 19:25:03,704 | INFO | Skipping weekly_record_676_2003-12-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,705 | INFO | Processing file: weekly_record_677_2003-12-08T00-00-00.000.json
    2026-04-15 19:25:03,728 | INFO | Skipping weekly_record_677_2003-12-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,728 | INFO | Processing file: weekly_record_678_2003-12-15T00-00-00.000.json
    2026-04-15 19:25:03,755 | INFO | Skipping weekly_record_678_2003-12-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,755 | INFO | Processing file: weekly_record_679_2003-12-22T00-00-00.000.json
    2026-04-15 19:25:03,776 | INFO | Skipping weekly_record_679_2003-12-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,777 | INFO | Processing file: weekly_record_67_1992-03-30T00-00-00.000.json
    2026-04-15 19:25:03,829 | INFO | Skipping weekly_record_67_1992-03-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,837 | INFO | Processing file: weekly_record_680_2003-12-29T00-00-00.000.json
    2026-04-15 19:25:03,867 | INFO | Skipping weekly_record_680_2003-12-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,867 | INFO | Processing file: weekly_record_681_2004-01-05T00-00-00.000.json
    2026-04-15 19:25:03,888 | INFO | Skipping weekly_record_681_2004-01-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,888 | INFO | Processing file: weekly_record_682_2004-01-12T00-00-00.000.json
    2026-04-15 19:25:03,912 | INFO | Skipping weekly_record_682_2004-01-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,912 | INFO | Processing file: weekly_record_683_2004-01-19T00-00-00.000.json
    2026-04-15 19:25:03,938 | INFO | Skipping weekly_record_683_2004-01-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,938 | INFO | Processing file: weekly_record_684_2004-01-26T00-00-00.000.json
    2026-04-15 19:25:03,955 | INFO | Skipping weekly_record_684_2004-01-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,956 | INFO | Processing file: weekly_record_685_2004-02-02T00-00-00.000.json
    2026-04-15 19:25:03,987 | INFO | Skipping weekly_record_685_2004-02-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:03,988 | INFO | Processing file: weekly_record_686_2004-02-09T00-00-00.000.json
    2026-04-15 19:25:04,016 | INFO | Skipping weekly_record_686_2004-02-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,016 | INFO | Processing file: weekly_record_687_2004-02-16T00-00-00.000.json
    2026-04-15 19:25:04,044 | INFO | Skipping weekly_record_687_2004-02-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,044 | INFO | Processing file: weekly_record_688_2004-02-23T00-00-00.000.json
    2026-04-15 19:25:04,070 | INFO | Skipping weekly_record_688_2004-02-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,071 | INFO | Processing file: weekly_record_689_2004-03-01T00-00-00.000.json
    2026-04-15 19:25:04,094 | INFO | Skipping weekly_record_689_2004-03-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,094 | INFO | Processing file: weekly_record_68_1992-04-06T00-00-00.000.json
    2026-04-15 19:25:04,123 | INFO | Skipping weekly_record_68_1992-04-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,124 | INFO | Processing file: weekly_record_690_2004-03-08T00-00-00.000.json
    2026-04-15 19:25:04,141 | INFO | Skipping weekly_record_690_2004-03-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,142 | INFO | Processing file: weekly_record_691_2004-03-15T00-00-00.000.json
    2026-04-15 19:25:04,173 | INFO | Skipping weekly_record_691_2004-03-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,174 | INFO | Processing file: weekly_record_692_2004-03-22T00-00-00.000.json
    2026-04-15 19:25:04,196 | INFO | Skipping weekly_record_692_2004-03-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,197 | INFO | Processing file: weekly_record_693_2004-03-29T00-00-00.000.json
    2026-04-15 19:25:04,225 | INFO | Skipping weekly_record_693_2004-03-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,226 | INFO | Processing file: weekly_record_694_2004-04-05T00-00-00.000.json
    2026-04-15 19:25:04,257 | INFO | Skipping weekly_record_694_2004-04-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,257 | INFO | Processing file: weekly_record_695_2004-04-12T00-00-00.000.json
    2026-04-15 19:25:04,275 | INFO | Skipping weekly_record_695_2004-04-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,276 | INFO | Processing file: weekly_record_696_2004-04-19T00-00-00.000.json
    2026-04-15 19:25:04,309 | INFO | Skipping weekly_record_696_2004-04-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,310 | INFO | Processing file: weekly_record_697_2004-04-26T00-00-00.000.json
    2026-04-15 19:25:04,336 | INFO | Skipping weekly_record_697_2004-04-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,339 | INFO | Processing file: weekly_record_698_2004-05-03T00-00-00.000.json
    2026-04-15 19:25:04,365 | INFO | Skipping weekly_record_698_2004-05-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,366 | INFO | Processing file: weekly_record_699_2004-05-10T00-00-00.000.json
    2026-04-15 19:25:04,388 | INFO | Skipping weekly_record_699_2004-05-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,389 | INFO | Processing file: weekly_record_69_1992-04-13T00-00-00.000.json
    2026-04-15 19:25:04,415 | INFO | Skipping weekly_record_69_1992-04-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,415 | INFO | Processing file: weekly_record_6_1991-01-28T00-00-00.000.json
    2026-04-15 19:25:04,442 | INFO | Skipping weekly_record_6_1991-01-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,442 | INFO | Processing file: weekly_record_700_2004-05-17T00-00-00.000.json
    2026-04-15 19:25:04,469 | INFO | Skipping weekly_record_700_2004-05-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,470 | INFO | Processing file: weekly_record_701_2004-05-24T00-00-00.000.json
    2026-04-15 19:25:04,507 | INFO | Skipping weekly_record_701_2004-05-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,508 | INFO | Processing file: weekly_record_702_2004-05-31T00-00-00.000.json
    2026-04-15 19:25:04,547 | INFO | Skipping weekly_record_702_2004-05-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,548 | INFO | Processing file: weekly_record_703_2004-06-07T00-00-00.000.json
    2026-04-15 19:25:04,572 | INFO | Skipping weekly_record_703_2004-06-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,572 | INFO | Processing file: weekly_record_704_2004-06-14T00-00-00.000.json
    2026-04-15 19:25:04,595 | INFO | Skipping weekly_record_704_2004-06-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,596 | INFO | Processing file: weekly_record_705_2004-06-21T00-00-00.000.json
    2026-04-15 19:25:04,621 | INFO | Skipping weekly_record_705_2004-06-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,622 | INFO | Processing file: weekly_record_706_2004-06-28T00-00-00.000.json
    2026-04-15 19:25:04,655 | INFO | Skipping weekly_record_706_2004-06-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,655 | INFO | Processing file: weekly_record_707_2004-07-05T00-00-00.000.json
    2026-04-15 19:25:04,676 | INFO | Skipping weekly_record_707_2004-07-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,677 | INFO | Processing file: weekly_record_708_2004-07-12T00-00-00.000.json
    2026-04-15 19:25:04,694 | INFO | Skipping weekly_record_708_2004-07-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,695 | INFO | Processing file: weekly_record_709_2004-07-19T00-00-00.000.json
    2026-04-15 19:25:04,723 | INFO | Skipping weekly_record_709_2004-07-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,724 | INFO | Processing file: weekly_record_70_1992-04-20T00-00-00.000.json
    2026-04-15 19:25:04,748 | INFO | Skipping weekly_record_70_1992-04-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,749 | INFO | Processing file: weekly_record_710_2004-07-26T00-00-00.000.json
    2026-04-15 19:25:04,775 | INFO | Skipping weekly_record_710_2004-07-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,776 | INFO | Processing file: weekly_record_711_2004-08-02T00-00-00.000.json
    2026-04-15 19:25:04,801 | INFO | Skipping weekly_record_711_2004-08-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,806 | INFO | Processing file: weekly_record_712_2004-08-09T00-00-00.000.json
    2026-04-15 19:25:04,845 | INFO | Skipping weekly_record_712_2004-08-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,846 | INFO | Processing file: weekly_record_713_2004-08-16T00-00-00.000.json
    2026-04-15 19:25:04,863 | INFO | Skipping weekly_record_713_2004-08-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,863 | INFO | Processing file: weekly_record_714_2004-08-23T00-00-00.000.json
    2026-04-15 19:25:04,889 | INFO | Skipping weekly_record_714_2004-08-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,890 | INFO | Processing file: weekly_record_715_2004-08-30T00-00-00.000.json
    2026-04-15 19:25:04,924 | INFO | Skipping weekly_record_715_2004-08-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,924 | INFO | Processing file: weekly_record_716_2004-09-06T00-00-00.000.json
    2026-04-15 19:25:04,940 | INFO | Skipping weekly_record_716_2004-09-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,941 | INFO | Processing file: weekly_record_717_2004-09-13T00-00-00.000.json
    2026-04-15 19:25:04,974 | INFO | Skipping weekly_record_717_2004-09-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,975 | INFO | Processing file: weekly_record_718_2004-09-20T00-00-00.000.json
    2026-04-15 19:25:04,992 | INFO | Skipping weekly_record_718_2004-09-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:04,993 | INFO | Processing file: weekly_record_719_2004-09-27T00-00-00.000.json
    2026-04-15 19:25:05,016 | INFO | Skipping weekly_record_719_2004-09-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,016 | INFO | Processing file: weekly_record_71_1992-04-27T00-00-00.000.json
    2026-04-15 19:25:05,037 | INFO | Skipping weekly_record_71_1992-04-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,038 | INFO | Processing file: weekly_record_720_2004-10-04T00-00-00.000.json
    2026-04-15 19:25:05,065 | INFO | Skipping weekly_record_720_2004-10-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,066 | INFO | Processing file: weekly_record_721_2004-10-11T00-00-00.000.json
    2026-04-15 19:25:05,082 | INFO | Skipping weekly_record_721_2004-10-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,087 | INFO | Processing file: weekly_record_722_2004-10-18T00-00-00.000.json
    2026-04-15 19:25:05,107 | INFO | Skipping weekly_record_722_2004-10-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,108 | INFO | Processing file: weekly_record_723_2004-10-25T00-00-00.000.json
    2026-04-15 19:25:05,134 | INFO | Skipping weekly_record_723_2004-10-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,134 | INFO | Processing file: weekly_record_724_2004-11-01T00-00-00.000.json
    2026-04-15 19:25:05,158 | INFO | Skipping weekly_record_724_2004-11-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,158 | INFO | Processing file: weekly_record_725_2004-11-08T00-00-00.000.json
    2026-04-15 19:25:05,177 | INFO | Skipping weekly_record_725_2004-11-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,178 | INFO | Processing file: weekly_record_726_2004-11-15T00-00-00.000.json
    2026-04-15 19:25:05,198 | INFO | Skipping weekly_record_726_2004-11-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,198 | INFO | Processing file: weekly_record_727_2004-11-22T00-00-00.000.json
    2026-04-15 19:25:05,235 | INFO | Skipping weekly_record_727_2004-11-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,254 | INFO | Processing file: weekly_record_728_2004-11-29T00-00-00.000.json
    2026-04-15 19:25:05,279 | INFO | Skipping weekly_record_728_2004-11-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,280 | INFO | Processing file: weekly_record_729_2004-12-06T00-00-00.000.json
    2026-04-15 19:25:05,307 | INFO | Skipping weekly_record_729_2004-12-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,307 | INFO | Processing file: weekly_record_72_1992-05-04T00-00-00.000.json
    2026-04-15 19:25:05,323 | INFO | Skipping weekly_record_72_1992-05-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,323 | INFO | Processing file: weekly_record_730_2004-12-13T00-00-00.000.json
    2026-04-15 19:25:05,345 | INFO | Skipping weekly_record_730_2004-12-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,346 | INFO | Processing file: weekly_record_731_2004-12-20T00-00-00.000.json
    2026-04-15 19:25:05,367 | INFO | Skipping weekly_record_731_2004-12-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,368 | INFO | Processing file: weekly_record_732_2004-12-27T00-00-00.000.json
    2026-04-15 19:25:05,396 | INFO | Skipping weekly_record_732_2004-12-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,397 | INFO | Processing file: weekly_record_733_2005-01-03T00-00-00.000.json
    2026-04-15 19:25:05,422 | INFO | Skipping weekly_record_733_2005-01-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,423 | INFO | Processing file: weekly_record_734_2005-01-10T00-00-00.000.json
    2026-04-15 19:25:05,447 | INFO | Skipping weekly_record_734_2005-01-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,447 | INFO | Processing file: weekly_record_735_2005-01-17T00-00-00.000.json
    2026-04-15 19:25:05,543 | INFO | Skipping weekly_record_735_2005-01-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,543 | INFO | Processing file: weekly_record_736_2005-01-24T00-00-00.000.json
    2026-04-15 19:25:05,567 | INFO | Skipping weekly_record_736_2005-01-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,568 | INFO | Processing file: weekly_record_737_2005-01-31T00-00-00.000.json
    2026-04-15 19:25:05,606 | INFO | Skipping weekly_record_737_2005-01-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,606 | INFO | Processing file: weekly_record_738_2005-02-07T00-00-00.000.json
    2026-04-15 19:25:05,633 | INFO | Skipping weekly_record_738_2005-02-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,634 | INFO | Processing file: weekly_record_739_2005-02-14T00-00-00.000.json
    2026-04-15 19:25:05,656 | INFO | Skipping weekly_record_739_2005-02-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,657 | INFO | Processing file: weekly_record_73_1992-05-11T00-00-00.000.json
    2026-04-15 19:25:05,683 | INFO | Skipping weekly_record_73_1992-05-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,683 | INFO | Processing file: weekly_record_740_2005-02-21T00-00-00.000.json
    2026-04-15 19:25:05,710 | INFO | Skipping weekly_record_740_2005-02-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,711 | INFO | Processing file: weekly_record_741_2005-02-28T00-00-00.000.json
    2026-04-15 19:25:05,745 | INFO | Skipping weekly_record_741_2005-02-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,747 | INFO | Processing file: weekly_record_742_2005-03-07T00-00-00.000.json
    2026-04-15 19:25:05,774 | INFO | Skipping weekly_record_742_2005-03-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,775 | INFO | Processing file: weekly_record_743_2005-03-14T00-00-00.000.json
    2026-04-15 19:25:05,800 | INFO | Skipping weekly_record_743_2005-03-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,801 | INFO | Processing file: weekly_record_744_2005-03-21T00-00-00.000.json
    2026-04-15 19:25:05,829 | INFO | Skipping weekly_record_744_2005-03-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,829 | INFO | Processing file: weekly_record_745_2005-03-28T00-00-00.000.json
    2026-04-15 19:25:05,857 | INFO | Skipping weekly_record_745_2005-03-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,858 | INFO | Processing file: weekly_record_746_2005-04-04T00-00-00.000.json
    2026-04-15 19:25:05,894 | INFO | Skipping weekly_record_746_2005-04-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,895 | INFO | Processing file: weekly_record_747_2005-04-11T00-00-00.000.json
    2026-04-15 19:25:05,911 | INFO | Skipping weekly_record_747_2005-04-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,912 | INFO | Processing file: weekly_record_748_2005-04-18T00-00-00.000.json
    2026-04-15 19:25:05,939 | INFO | Skipping weekly_record_748_2005-04-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,940 | INFO | Processing file: weekly_record_749_2005-04-25T00-00-00.000.json
    2026-04-15 19:25:05,970 | INFO | Skipping weekly_record_749_2005-04-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,970 | INFO | Processing file: weekly_record_74_1992-05-18T00-00-00.000.json
    2026-04-15 19:25:05,993 | INFO | Skipping weekly_record_74_1992-05-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:05,994 | INFO | Processing file: weekly_record_750_2005-05-02T00-00-00.000.json
    2026-04-15 19:25:06,026 | INFO | Skipping weekly_record_750_2005-05-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,027 | INFO | Processing file: weekly_record_751_2005-05-09T00-00-00.000.json
    2026-04-15 19:25:06,051 | INFO | Skipping weekly_record_751_2005-05-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,052 | INFO | Processing file: weekly_record_752_2005-05-16T00-00-00.000.json
    2026-04-15 19:25:06,074 | INFO | Skipping weekly_record_752_2005-05-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,075 | INFO | Processing file: weekly_record_753_2005-05-23T00-00-00.000.json
    2026-04-15 19:25:06,105 | INFO | Skipping weekly_record_753_2005-05-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,106 | INFO | Processing file: weekly_record_754_2005-05-30T00-00-00.000.json
    2026-04-15 19:25:06,125 | INFO | Skipping weekly_record_754_2005-05-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,125 | INFO | Processing file: weekly_record_755_2005-06-06T00-00-00.000.json
    2026-04-15 19:25:06,151 | INFO | Skipping weekly_record_755_2005-06-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,151 | INFO | Processing file: weekly_record_756_2005-06-13T00-00-00.000.json
    2026-04-15 19:25:06,176 | INFO | Skipping weekly_record_756_2005-06-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,177 | INFO | Processing file: weekly_record_757_2005-06-20T00-00-00.000.json
    2026-04-15 19:25:06,197 | INFO | Skipping weekly_record_757_2005-06-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,197 | INFO | Processing file: weekly_record_758_2005-06-27T00-00-00.000.json
    2026-04-15 19:25:06,221 | INFO | Skipping weekly_record_758_2005-06-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,222 | INFO | Processing file: weekly_record_759_2005-07-04T00-00-00.000.json
    2026-04-15 19:25:06,253 | INFO | Skipping weekly_record_759_2005-07-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,256 | INFO | Processing file: weekly_record_75_1992-05-25T00-00-00.000.json
    2026-04-15 19:25:06,279 | INFO | Skipping weekly_record_75_1992-05-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,280 | INFO | Processing file: weekly_record_760_2005-07-11T00-00-00.000.json
    2026-04-15 19:25:06,313 | INFO | Skipping weekly_record_760_2005-07-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,314 | INFO | Processing file: weekly_record_761_2005-07-18T00-00-00.000.json
    2026-04-15 19:25:06,338 | INFO | Skipping weekly_record_761_2005-07-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,339 | INFO | Processing file: weekly_record_762_2005-07-25T00-00-00.000.json
    2026-04-15 19:25:06,365 | INFO | Skipping weekly_record_762_2005-07-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,366 | INFO | Processing file: weekly_record_763_2005-08-01T00-00-00.000.json
    2026-04-15 19:25:06,388 | INFO | Skipping weekly_record_763_2005-08-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,389 | INFO | Processing file: weekly_record_764_2005-08-08T00-00-00.000.json
    2026-04-15 19:25:06,415 | INFO | Skipping weekly_record_764_2005-08-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,416 | INFO | Processing file: weekly_record_765_2005-08-15T00-00-00.000.json
    2026-04-15 19:25:06,438 | INFO | Skipping weekly_record_765_2005-08-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,439 | INFO | Processing file: weekly_record_766_2005-08-22T00-00-00.000.json
    2026-04-15 19:25:06,456 | INFO | Skipping weekly_record_766_2005-08-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,456 | INFO | Processing file: weekly_record_767_2005-08-29T00-00-00.000.json
    2026-04-15 19:25:06,490 | INFO | Skipping weekly_record_767_2005-08-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,491 | INFO | Processing file: weekly_record_768_2005-09-05T00-00-00.000.json
    2026-04-15 19:25:06,518 | INFO | Skipping weekly_record_768_2005-09-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,519 | INFO | Processing file: weekly_record_769_2005-09-12T00-00-00.000.json
    2026-04-15 19:25:06,548 | INFO | Skipping weekly_record_769_2005-09-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,549 | INFO | Processing file: weekly_record_76_1992-06-01T00-00-00.000.json
    2026-04-15 19:25:06,577 | INFO | Skipping weekly_record_76_1992-06-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,578 | INFO | Processing file: weekly_record_770_2005-09-19T00-00-00.000.json
    2026-04-15 19:25:06,599 | INFO | Skipping weekly_record_770_2005-09-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,599 | INFO | Processing file: weekly_record_771_2005-09-26T00-00-00.000.json
    2026-04-15 19:25:06,617 | INFO | Skipping weekly_record_771_2005-09-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,617 | INFO | Processing file: weekly_record_772_2005-10-03T00-00-00.000.json
    2026-04-15 19:25:06,651 | INFO | Skipping weekly_record_772_2005-10-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,652 | INFO | Processing file: weekly_record_773_2005-10-10T00-00-00.000.json
    2026-04-15 19:25:06,686 | INFO | Skipping weekly_record_773_2005-10-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,687 | INFO | Processing file: weekly_record_774_2005-10-17T00-00-00.000.json
    2026-04-15 19:25:06,711 | INFO | Skipping weekly_record_774_2005-10-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,712 | INFO | Processing file: weekly_record_775_2005-10-24T00-00-00.000.json
    2026-04-15 19:25:06,735 | INFO | Skipping weekly_record_775_2005-10-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,735 | INFO | Processing file: weekly_record_776_2005-10-31T00-00-00.000.json
    2026-04-15 19:25:06,777 | INFO | Skipping weekly_record_776_2005-10-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,779 | INFO | Processing file: weekly_record_777_2005-11-07T00-00-00.000.json
    2026-04-15 19:25:06,809 | INFO | Skipping weekly_record_777_2005-11-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,810 | INFO | Processing file: weekly_record_778_2005-11-14T00-00-00.000.json
    2026-04-15 19:25:06,833 | INFO | Skipping weekly_record_778_2005-11-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,834 | INFO | Processing file: weekly_record_779_2005-11-21T00-00-00.000.json
    2026-04-15 19:25:06,859 | INFO | Skipping weekly_record_779_2005-11-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,859 | INFO | Processing file: weekly_record_77_1992-06-08T00-00-00.000.json
    2026-04-15 19:25:06,885 | INFO | Skipping weekly_record_77_1992-06-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,886 | INFO | Processing file: weekly_record_780_2005-11-28T00-00-00.000.json
    2026-04-15 19:25:06,913 | INFO | Skipping weekly_record_780_2005-11-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,914 | INFO | Processing file: weekly_record_781_2005-12-05T00-00-00.000.json
    2026-04-15 19:25:06,938 | INFO | Skipping weekly_record_781_2005-12-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,939 | INFO | Processing file: weekly_record_782_2005-12-12T00-00-00.000.json
    2026-04-15 19:25:06,969 | INFO | Skipping weekly_record_782_2005-12-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,969 | INFO | Processing file: weekly_record_783_2005-12-19T00-00-00.000.json
    2026-04-15 19:25:06,995 | INFO | Skipping weekly_record_783_2005-12-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:06,996 | INFO | Processing file: weekly_record_784_2005-12-26T00-00-00.000.json
    2026-04-15 19:25:07,037 | INFO | Skipping weekly_record_784_2005-12-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,037 | INFO | Processing file: weekly_record_785_2006-01-02T00-00-00.000.json
    2026-04-15 19:25:07,074 | INFO | Skipping weekly_record_785_2006-01-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,075 | INFO | Processing file: weekly_record_786_2006-01-09T00-00-00.000.json
    2026-04-15 19:25:07,097 | INFO | Skipping weekly_record_786_2006-01-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,098 | INFO | Processing file: weekly_record_787_2006-01-16T00-00-00.000.json
    2026-04-15 19:25:07,119 | INFO | Skipping weekly_record_787_2006-01-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,120 | INFO | Processing file: weekly_record_788_2006-01-23T00-00-00.000.json
    2026-04-15 19:25:07,138 | INFO | Skipping weekly_record_788_2006-01-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,139 | INFO | Processing file: weekly_record_789_2006-01-30T00-00-00.000.json
    2026-04-15 19:25:07,164 | INFO | Skipping weekly_record_789_2006-01-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,165 | INFO | Processing file: weekly_record_78_1992-06-15T00-00-00.000.json
    2026-04-15 19:25:07,191 | INFO | Skipping weekly_record_78_1992-06-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,191 | INFO | Processing file: weekly_record_790_2006-02-06T00-00-00.000.json
    2026-04-15 19:25:07,218 | INFO | Skipping weekly_record_790_2006-02-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,219 | INFO | Processing file: weekly_record_791_2006-02-13T00-00-00.000.json
    2026-04-15 19:25:07,244 | INFO | Skipping weekly_record_791_2006-02-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,245 | INFO | Processing file: weekly_record_792_2006-02-20T00-00-00.000.json
    2026-04-15 19:25:07,282 | INFO | Skipping weekly_record_792_2006-02-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,283 | INFO | Processing file: weekly_record_793_2006-02-27T00-00-00.000.json
    2026-04-15 19:25:07,308 | INFO | Skipping weekly_record_793_2006-02-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,308 | INFO | Processing file: weekly_record_794_2006-03-06T00-00-00.000.json
    2026-04-15 19:25:07,326 | INFO | Skipping weekly_record_794_2006-03-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,327 | INFO | Processing file: weekly_record_795_2006-03-13T00-00-00.000.json
    2026-04-15 19:25:07,343 | INFO | Skipping weekly_record_795_2006-03-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,343 | INFO | Processing file: weekly_record_796_2006-03-20T00-00-00.000.json
    2026-04-15 19:25:07,373 | INFO | Skipping weekly_record_796_2006-03-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,373 | INFO | Processing file: weekly_record_797_2006-03-27T00-00-00.000.json
    2026-04-15 19:25:07,397 | INFO | Skipping weekly_record_797_2006-03-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,397 | INFO | Processing file: weekly_record_798_2006-04-03T00-00-00.000.json
    2026-04-15 19:25:07,426 | INFO | Skipping weekly_record_798_2006-04-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,426 | INFO | Processing file: weekly_record_799_2006-04-10T00-00-00.000.json
    2026-04-15 19:25:07,451 | INFO | Skipping weekly_record_799_2006-04-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,452 | INFO | Processing file: weekly_record_79_1992-06-22T00-00-00.000.json
    2026-04-15 19:25:07,481 | INFO | Skipping weekly_record_79_1992-06-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,481 | INFO | Processing file: weekly_record_7_1991-02-04T00-00-00.000.json
    2026-04-15 19:25:07,500 | INFO | Skipping weekly_record_7_1991-02-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,501 | INFO | Processing file: weekly_record_800_2006-04-17T00-00-00.000.json
    2026-04-15 19:25:07,528 | INFO | Skipping weekly_record_800_2006-04-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,528 | INFO | Processing file: weekly_record_801_2006-04-24T00-00-00.000.json
    2026-04-15 19:25:07,550 | INFO | Skipping weekly_record_801_2006-04-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,550 | INFO | Processing file: weekly_record_802_2006-05-01T00-00-00.000.json
    2026-04-15 19:25:07,577 | INFO | Skipping weekly_record_802_2006-05-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,578 | INFO | Processing file: weekly_record_803_2006-05-08T00-00-00.000.json
    2026-04-15 19:25:07,615 | INFO | Skipping weekly_record_803_2006-05-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,615 | INFO | Processing file: weekly_record_804_2006-05-15T00-00-00.000.json
    2026-04-15 19:25:07,643 | INFO | Skipping weekly_record_804_2006-05-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,644 | INFO | Processing file: weekly_record_805_2006-05-22T00-00-00.000.json
    2026-04-15 19:25:07,662 | INFO | Skipping weekly_record_805_2006-05-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,663 | INFO | Processing file: weekly_record_806_2006-05-29T00-00-00.000.json
    2026-04-15 19:25:07,680 | INFO | Skipping weekly_record_806_2006-05-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,681 | INFO | Processing file: weekly_record_807_2006-06-05T00-00-00.000.json
    2026-04-15 19:25:07,719 | INFO | Skipping weekly_record_807_2006-06-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,723 | INFO | Processing file: weekly_record_808_2006-06-12T00-00-00.000.json
    2026-04-15 19:25:07,757 | INFO | Skipping weekly_record_808_2006-06-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,757 | INFO | Processing file: weekly_record_809_2006-06-19T00-00-00.000.json
    2026-04-15 19:25:07,777 | INFO | Skipping weekly_record_809_2006-06-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,778 | INFO | Processing file: weekly_record_80_1992-06-29T00-00-00.000.json
    2026-04-15 19:25:07,802 | INFO | Skipping weekly_record_80_1992-06-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,803 | INFO | Processing file: weekly_record_810_2006-06-26T00-00-00.000.json
    2026-04-15 19:25:07,821 | INFO | Skipping weekly_record_810_2006-06-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,821 | INFO | Processing file: weekly_record_811_2006-07-03T00-00-00.000.json
    2026-04-15 19:25:07,847 | INFO | Skipping weekly_record_811_2006-07-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,847 | INFO | Processing file: weekly_record_812_2006-07-10T00-00-00.000.json
    2026-04-15 19:25:07,865 | INFO | Skipping weekly_record_812_2006-07-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,866 | INFO | Processing file: weekly_record_813_2006-07-17T00-00-00.000.json
    2026-04-15 19:25:07,884 | INFO | Skipping weekly_record_813_2006-07-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,885 | INFO | Processing file: weekly_record_814_2006-07-24T00-00-00.000.json
    2026-04-15 19:25:07,909 | INFO | Skipping weekly_record_814_2006-07-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,910 | INFO | Processing file: weekly_record_815_2006-07-31T00-00-00.000.json
    2026-04-15 19:25:07,938 | INFO | Skipping weekly_record_815_2006-07-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,939 | INFO | Processing file: weekly_record_816_2006-08-07T00-00-00.000.json
    2026-04-15 19:25:07,964 | INFO | Skipping weekly_record_816_2006-08-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,964 | INFO | Processing file: weekly_record_817_2006-08-14T00-00-00.000.json
    2026-04-15 19:25:07,982 | INFO | Skipping weekly_record_817_2006-08-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:07,983 | INFO | Processing file: weekly_record_818_2006-08-21T00-00-00.000.json
    2026-04-15 19:25:07,999 | INFO | Skipping weekly_record_818_2006-08-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,000 | INFO | Processing file: weekly_record_819_2006-08-28T00-00-00.000.json
    2026-04-15 19:25:08,017 | INFO | Skipping weekly_record_819_2006-08-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,018 | INFO | Processing file: weekly_record_81_1992-07-06T00-00-00.000.json
    2026-04-15 19:25:08,044 | INFO | Skipping weekly_record_81_1992-07-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,045 | INFO | Processing file: weekly_record_820_2006-09-04T00-00-00.000.json
    2026-04-15 19:25:08,078 | INFO | Skipping weekly_record_820_2006-09-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,080 | INFO | Processing file: weekly_record_821_2006-09-11T00-00-00.000.json
    2026-04-15 19:25:08,102 | INFO | Skipping weekly_record_821_2006-09-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,102 | INFO | Processing file: weekly_record_822_2006-09-18T00-00-00.000.json
    2026-04-15 19:25:08,121 | INFO | Skipping weekly_record_822_2006-09-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,121 | INFO | Processing file: weekly_record_823_2006-09-25T00-00-00.000.json
    2026-04-15 19:25:08,147 | INFO | Skipping weekly_record_823_2006-09-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,148 | INFO | Processing file: weekly_record_824_2006-10-02T00-00-00.000.json
    2026-04-15 19:25:08,165 | INFO | Skipping weekly_record_824_2006-10-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,166 | INFO | Processing file: weekly_record_825_2006-10-09T00-00-00.000.json
    2026-04-15 19:25:08,188 | INFO | Skipping weekly_record_825_2006-10-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,188 | INFO | Processing file: weekly_record_826_2006-10-16T00-00-00.000.json
    2026-04-15 19:25:08,212 | INFO | Skipping weekly_record_826_2006-10-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,213 | INFO | Processing file: weekly_record_827_2006-10-23T00-00-00.000.json
    2026-04-15 19:25:08,238 | INFO | Skipping weekly_record_827_2006-10-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,238 | INFO | Processing file: weekly_record_828_2006-10-30T00-00-00.000.json
    2026-04-15 19:25:08,265 | INFO | Skipping weekly_record_828_2006-10-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,266 | INFO | Processing file: weekly_record_829_2006-11-06T00-00-00.000.json
    2026-04-15 19:25:08,294 | INFO | Skipping weekly_record_829_2006-11-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,294 | INFO | Processing file: weekly_record_82_1992-07-13T00-00-00.000.json
    2026-04-15 19:25:08,332 | INFO | Skipping weekly_record_82_1992-07-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,333 | INFO | Processing file: weekly_record_830_2006-11-13T00-00-00.000.json
    2026-04-15 19:25:08,360 | INFO | Skipping weekly_record_830_2006-11-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,361 | INFO | Processing file: weekly_record_831_2006-11-20T00-00-00.000.json
    2026-04-15 19:25:08,392 | INFO | Skipping weekly_record_831_2006-11-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,392 | INFO | Processing file: weekly_record_832_2006-11-27T00-00-00.000.json
    2026-04-15 19:25:08,417 | INFO | Skipping weekly_record_832_2006-11-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,418 | INFO | Processing file: weekly_record_833_2006-12-04T00-00-00.000.json
    2026-04-15 19:25:08,436 | INFO | Skipping weekly_record_833_2006-12-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,436 | INFO | Processing file: weekly_record_834_2006-12-11T00-00-00.000.json
    2026-04-15 19:25:08,474 | INFO | Skipping weekly_record_834_2006-12-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,498 | INFO | Processing file: weekly_record_835_2006-12-18T00-00-00.000.json
    2026-04-15 19:25:08,521 | INFO | Skipping weekly_record_835_2006-12-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,522 | INFO | Processing file: weekly_record_836_2006-12-25T00-00-00.000.json
    2026-04-15 19:25:08,538 | INFO | Skipping weekly_record_836_2006-12-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,538 | INFO | Processing file: weekly_record_837_2007-01-01T00-00-00.000.json
    2026-04-15 19:25:08,565 | INFO | Skipping weekly_record_837_2007-01-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,565 | INFO | Processing file: weekly_record_838_2007-01-08T00-00-00.000.json
    2026-04-15 19:25:08,596 | INFO | Skipping weekly_record_838_2007-01-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,597 | INFO | Processing file: weekly_record_839_2007-01-15T00-00-00.000.json
    2026-04-15 19:25:08,615 | INFO | Skipping weekly_record_839_2007-01-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,615 | INFO | Processing file: weekly_record_83_1992-07-20T00-00-00.000.json
    2026-04-15 19:25:08,643 | INFO | Skipping weekly_record_83_1992-07-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,644 | INFO | Processing file: weekly_record_840_2007-01-22T00-00-00.000.json
    2026-04-15 19:25:08,661 | INFO | Skipping weekly_record_840_2007-01-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,662 | INFO | Processing file: weekly_record_841_2007-01-29T00-00-00.000.json
    2026-04-15 19:25:08,686 | INFO | Skipping weekly_record_841_2007-01-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,687 | INFO | Processing file: weekly_record_842_2007-02-05T00-00-00.000.json
    2026-04-15 19:25:08,713 | INFO | Skipping weekly_record_842_2007-02-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,713 | INFO | Processing file: weekly_record_843_2007-02-12T00-00-00.000.json
    2026-04-15 19:25:08,736 | INFO | Skipping weekly_record_843_2007-02-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,736 | INFO | Processing file: weekly_record_844_2007-02-19T00-00-00.000.json
    2026-04-15 19:25:08,766 | INFO | Skipping weekly_record_844_2007-02-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,766 | INFO | Processing file: weekly_record_845_2007-02-26T00-00-00.000.json
    2026-04-15 19:25:08,794 | INFO | Skipping weekly_record_845_2007-02-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,795 | INFO | Processing file: weekly_record_846_2007-03-05T00-00-00.000.json
    2026-04-15 19:25:08,818 | INFO | Skipping weekly_record_846_2007-03-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,819 | INFO | Processing file: weekly_record_847_2007-03-12T00-00-00.000.json
    2026-04-15 19:25:08,849 | INFO | Skipping weekly_record_847_2007-03-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,850 | INFO | Processing file: weekly_record_848_2007-03-19T00-00-00.000.json
    2026-04-15 19:25:08,883 | INFO | Skipping weekly_record_848_2007-03-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,900 | INFO | Processing file: weekly_record_849_2007-03-26T00-00-00.000.json
    2026-04-15 19:25:08,919 | INFO | Skipping weekly_record_849_2007-03-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,920 | INFO | Processing file: weekly_record_84_1992-07-27T00-00-00.000.json
    2026-04-15 19:25:08,939 | INFO | Skipping weekly_record_84_1992-07-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,939 | INFO | Processing file: weekly_record_850_2007-04-02T00-00-00.000.json
    2026-04-15 19:25:08,956 | INFO | Skipping weekly_record_850_2007-04-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,956 | INFO | Processing file: weekly_record_851_2007-04-09T00-00-00.000.json
    2026-04-15 19:25:08,973 | INFO | Skipping weekly_record_851_2007-04-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,974 | INFO | Processing file: weekly_record_852_2007-04-16T00-00-00.000.json
    2026-04-15 19:25:08,992 | INFO | Skipping weekly_record_852_2007-04-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:08,993 | INFO | Processing file: weekly_record_853_2007-04-23T00-00-00.000.json
    2026-04-15 19:25:09,023 | INFO | Skipping weekly_record_853_2007-04-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,023 | INFO | Processing file: weekly_record_854_2007-04-30T00-00-00.000.json
    2026-04-15 19:25:09,042 | INFO | Skipping weekly_record_854_2007-04-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,042 | INFO | Processing file: weekly_record_855_2007-05-07T00-00-00.000.json
    2026-04-15 19:25:09,066 | INFO | Skipping weekly_record_855_2007-05-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,067 | INFO | Processing file: weekly_record_856_2007-05-14T00-00-00.000.json
    2026-04-15 19:25:09,080 | INFO | Skipping weekly_record_856_2007-05-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,081 | INFO | Processing file: weekly_record_857_2007-05-21T00-00-00.000.json
    2026-04-15 19:25:09,120 | INFO | Skipping weekly_record_857_2007-05-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,121 | INFO | Processing file: weekly_record_858_2007-05-28T00-00-00.000.json
    2026-04-15 19:25:09,151 | INFO | Skipping weekly_record_858_2007-05-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,152 | INFO | Processing file: weekly_record_859_2007-06-04T00-00-00.000.json
    2026-04-15 19:25:09,176 | INFO | Skipping weekly_record_859_2007-06-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,177 | INFO | Processing file: weekly_record_85_1992-08-03T00-00-00.000.json
    2026-04-15 19:25:09,197 | INFO | Skipping weekly_record_85_1992-08-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,198 | INFO | Processing file: weekly_record_860_2007-06-11T00-00-00.000.json
    2026-04-15 19:25:09,226 | INFO | Skipping weekly_record_860_2007-06-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,226 | INFO | Processing file: weekly_record_861_2007-06-18T00-00-00.000.json
    2026-04-15 19:25:09,254 | INFO | Skipping weekly_record_861_2007-06-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,255 | INFO | Processing file: weekly_record_862_2007-06-25T00-00-00.000.json
    2026-04-15 19:25:09,293 | INFO | Skipping weekly_record_862_2007-06-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,309 | INFO | Processing file: weekly_record_863_2007-07-02T00-00-00.000.json
    2026-04-15 19:25:09,338 | INFO | Skipping weekly_record_863_2007-07-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,339 | INFO | Processing file: weekly_record_864_2007-07-09T00-00-00.000.json
    2026-04-15 19:25:09,356 | INFO | Skipping weekly_record_864_2007-07-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,357 | INFO | Processing file: weekly_record_865_2007-07-16T00-00-00.000.json
    2026-04-15 19:25:09,382 | INFO | Skipping weekly_record_865_2007-07-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,382 | INFO | Processing file: weekly_record_866_2007-07-23T00-00-00.000.json
    2026-04-15 19:25:09,402 | INFO | Skipping weekly_record_866_2007-07-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,402 | INFO | Processing file: weekly_record_867_2007-07-30T00-00-00.000.json
    2026-04-15 19:25:09,426 | INFO | Skipping weekly_record_867_2007-07-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,426 | INFO | Processing file: weekly_record_868_2007-08-06T00-00-00.000.json
    2026-04-15 19:25:09,444 | INFO | Skipping weekly_record_868_2007-08-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,445 | INFO | Processing file: weekly_record_869_2007-08-13T00-00-00.000.json
    2026-04-15 19:25:09,462 | INFO | Skipping weekly_record_869_2007-08-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,463 | INFO | Processing file: weekly_record_86_1992-08-10T00-00-00.000.json
    2026-04-15 19:25:09,484 | INFO | Skipping weekly_record_86_1992-08-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,485 | INFO | Processing file: weekly_record_870_2007-08-20T00-00-00.000.json
    2026-04-15 19:25:09,503 | INFO | Skipping weekly_record_870_2007-08-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,503 | INFO | Processing file: weekly_record_871_2007-08-27T00-00-00.000.json
    2026-04-15 19:25:09,525 | INFO | Skipping weekly_record_871_2007-08-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,525 | INFO | Processing file: weekly_record_872_2007-09-03T00-00-00.000.json
    2026-04-15 19:25:09,543 | INFO | Skipping weekly_record_872_2007-09-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,543 | INFO | Processing file: weekly_record_873_2007-09-10T00-00-00.000.json
    2026-04-15 19:25:09,567 | INFO | Skipping weekly_record_873_2007-09-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,567 | INFO | Processing file: weekly_record_874_2007-09-17T00-00-00.000.json
    2026-04-15 19:25:09,585 | INFO | Skipping weekly_record_874_2007-09-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,585 | INFO | Processing file: weekly_record_875_2007-09-24T00-00-00.000.json
    2026-04-15 19:25:09,602 | INFO | Skipping weekly_record_875_2007-09-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,602 | INFO | Processing file: weekly_record_876_2007-10-01T00-00-00.000.json
    2026-04-15 19:25:09,620 | INFO | Skipping weekly_record_876_2007-10-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,621 | INFO | Processing file: weekly_record_877_2007-10-08T00-00-00.000.json
    2026-04-15 19:25:09,645 | INFO | Skipping weekly_record_877_2007-10-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,645 | INFO | Processing file: weekly_record_878_2007-10-15T00-00-00.000.json
    2026-04-15 19:25:09,663 | INFO | Skipping weekly_record_878_2007-10-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,664 | INFO | Processing file: weekly_record_879_2007-10-22T00-00-00.000.json
    2026-04-15 19:25:09,686 | INFO | Skipping weekly_record_879_2007-10-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,700 | INFO | Processing file: weekly_record_87_1992-08-17T00-00-00.000.json
    2026-04-15 19:25:09,724 | INFO | Skipping weekly_record_87_1992-08-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,725 | INFO | Processing file: weekly_record_880_2007-10-29T00-00-00.000.json
    2026-04-15 19:25:09,751 | INFO | Skipping weekly_record_880_2007-10-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,752 | INFO | Processing file: weekly_record_881_2007-11-05T00-00-00.000.json
    2026-04-15 19:25:09,774 | INFO | Skipping weekly_record_881_2007-11-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,775 | INFO | Processing file: weekly_record_882_2007-11-12T00-00-00.000.json
    2026-04-15 19:25:09,797 | INFO | Skipping weekly_record_882_2007-11-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,798 | INFO | Processing file: weekly_record_883_2007-11-19T00-00-00.000.json
    2026-04-15 19:25:09,827 | INFO | Skipping weekly_record_883_2007-11-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,828 | INFO | Processing file: weekly_record_884_2007-11-26T00-00-00.000.json
    2026-04-15 19:25:09,852 | INFO | Skipping weekly_record_884_2007-11-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,853 | INFO | Processing file: weekly_record_885_2007-12-03T00-00-00.000.json
    2026-04-15 19:25:09,878 | INFO | Skipping weekly_record_885_2007-12-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,878 | INFO | Processing file: weekly_record_886_2007-12-10T00-00-00.000.json
    2026-04-15 19:25:09,896 | INFO | Skipping weekly_record_886_2007-12-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,897 | INFO | Processing file: weekly_record_887_2007-12-17T00-00-00.000.json
    2026-04-15 19:25:09,915 | INFO | Skipping weekly_record_887_2007-12-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,916 | INFO | Processing file: weekly_record_888_2007-12-24T00-00-00.000.json
    2026-04-15 19:25:09,942 | INFO | Skipping weekly_record_888_2007-12-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,943 | INFO | Processing file: weekly_record_889_2007-12-31T00-00-00.000.json
    2026-04-15 19:25:09,963 | INFO | Skipping weekly_record_889_2007-12-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,964 | INFO | Processing file: weekly_record_88_1992-08-24T00-00-00.000.json
    2026-04-15 19:25:09,983 | INFO | Skipping weekly_record_88_1992-08-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:09,984 | INFO | Processing file: weekly_record_890_2008-01-07T00-00-00.000.json
    2026-04-15 19:25:10,001 | INFO | Skipping weekly_record_890_2008-01-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,001 | INFO | Processing file: weekly_record_891_2008-01-14T00-00-00.000.json
    2026-04-15 19:25:10,031 | INFO | Skipping weekly_record_891_2008-01-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,032 | INFO | Processing file: weekly_record_892_2008-01-21T00-00-00.000.json
    2026-04-15 19:25:10,069 | INFO | Skipping weekly_record_892_2008-01-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,074 | INFO | Processing file: weekly_record_893_2008-01-28T00-00-00.000.json
    2026-04-15 19:25:10,119 | INFO | Skipping weekly_record_893_2008-01-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,119 | INFO | Processing file: weekly_record_894_2008-02-04T00-00-00.000.json
    2026-04-15 19:25:10,147 | INFO | Skipping weekly_record_894_2008-02-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,148 | INFO | Processing file: weekly_record_895_2008-02-11T00-00-00.000.json
    2026-04-15 19:25:10,165 | INFO | Skipping weekly_record_895_2008-02-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,165 | INFO | Processing file: weekly_record_896_2008-02-18T00-00-00.000.json
    2026-04-15 19:25:10,184 | INFO | Skipping weekly_record_896_2008-02-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,184 | INFO | Processing file: weekly_record_897_2008-02-25T00-00-00.000.json
    2026-04-15 19:25:10,207 | INFO | Skipping weekly_record_897_2008-02-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,207 | INFO | Processing file: weekly_record_898_2008-03-03T00-00-00.000.json
    2026-04-15 19:25:10,224 | INFO | Skipping weekly_record_898_2008-03-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,225 | INFO | Processing file: weekly_record_899_2008-03-10T00-00-00.000.json
    2026-04-15 19:25:10,245 | INFO | Skipping weekly_record_899_2008-03-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,245 | INFO | Processing file: weekly_record_89_1992-08-31T00-00-00.000.json
    2026-04-15 19:25:10,270 | INFO | Skipping weekly_record_89_1992-08-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,270 | INFO | Processing file: weekly_record_8_1991-02-11T00-00-00.000.json
    2026-04-15 19:25:10,296 | INFO | Skipping weekly_record_8_1991-02-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,296 | INFO | Processing file: weekly_record_900_2008-03-17T00-00-00.000.json
    2026-04-15 19:25:10,316 | INFO | Skipping weekly_record_900_2008-03-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,317 | INFO | Processing file: weekly_record_901_2008-03-24T00-00-00.000.json
    2026-04-15 19:25:10,344 | INFO | Skipping weekly_record_901_2008-03-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,345 | INFO | Processing file: weekly_record_902_2008-03-31T00-00-00.000.json
    2026-04-15 19:25:10,366 | INFO | Skipping weekly_record_902_2008-03-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,367 | INFO | Processing file: weekly_record_903_2008-04-07T00-00-00.000.json
    2026-04-15 19:25:10,394 | INFO | Skipping weekly_record_903_2008-04-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,394 | INFO | Processing file: weekly_record_904_2008-04-14T00-00-00.000.json
    2026-04-15 19:25:10,411 | INFO | Skipping weekly_record_904_2008-04-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,412 | INFO | Processing file: weekly_record_905_2008-04-21T00-00-00.000.json
    2026-04-15 19:25:10,438 | INFO | Skipping weekly_record_905_2008-04-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,439 | INFO | Processing file: weekly_record_906_2008-04-28T00-00-00.000.json
    2026-04-15 19:25:10,457 | INFO | Skipping weekly_record_906_2008-04-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,458 | INFO | Processing file: weekly_record_907_2008-05-05T00-00-00.000.json
    2026-04-15 19:25:10,483 | INFO | Skipping weekly_record_907_2008-05-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,483 | INFO | Processing file: weekly_record_908_2008-05-12T00-00-00.000.json
    2026-04-15 19:25:10,503 | INFO | Skipping weekly_record_908_2008-05-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,513 | INFO | Processing file: weekly_record_909_2008-05-19T00-00-00.000.json
    2026-04-15 19:25:10,536 | INFO | Skipping weekly_record_909_2008-05-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,538 | INFO | Processing file: weekly_record_90_1992-09-07T00-00-00.000.json
    2026-04-15 19:25:10,563 | INFO | Skipping weekly_record_90_1992-09-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,563 | INFO | Processing file: weekly_record_910_2008-05-26T00-00-00.000.json
    2026-04-15 19:25:10,589 | INFO | Skipping weekly_record_910_2008-05-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,590 | INFO | Processing file: weekly_record_911_2008-06-02T00-00-00.000.json
    2026-04-15 19:25:10,617 | INFO | Skipping weekly_record_911_2008-06-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,617 | INFO | Processing file: weekly_record_912_2008-06-09T00-00-00.000.json
    2026-04-15 19:25:10,645 | INFO | Skipping weekly_record_912_2008-06-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,645 | INFO | Processing file: weekly_record_913_2008-06-16T00-00-00.000.json
    2026-04-15 19:25:10,661 | INFO | Skipping weekly_record_913_2008-06-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,661 | INFO | Processing file: weekly_record_914_2008-06-23T00-00-00.000.json
    2026-04-15 19:25:10,688 | INFO | Skipping weekly_record_914_2008-06-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,688 | INFO | Processing file: weekly_record_915_2008-06-30T00-00-00.000.json
    2026-04-15 19:25:10,706 | INFO | Skipping weekly_record_915_2008-06-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,707 | INFO | Processing file: weekly_record_916_2008-07-07T00-00-00.000.json
    2026-04-15 19:25:10,725 | INFO | Skipping weekly_record_916_2008-07-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,726 | INFO | Processing file: weekly_record_917_2008-07-14T00-00-00.000.json
    2026-04-15 19:25:10,747 | INFO | Skipping weekly_record_917_2008-07-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,749 | INFO | Processing file: weekly_record_918_2008-07-21T00-00-00.000.json
    2026-04-15 19:25:10,775 | INFO | Skipping weekly_record_918_2008-07-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,775 | INFO | Processing file: weekly_record_919_2008-07-28T00-00-00.000.json
    2026-04-15 19:25:10,797 | INFO | Skipping weekly_record_919_2008-07-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,797 | INFO | Processing file: weekly_record_91_1992-09-14T00-00-00.000.json
    2026-04-15 19:25:10,824 | INFO | Skipping weekly_record_91_1992-09-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,825 | INFO | Processing file: weekly_record_920_2008-08-04T00-00-00.000.json
    2026-04-15 19:25:10,851 | INFO | Skipping weekly_record_920_2008-08-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,857 | INFO | Processing file: weekly_record_921_2008-08-11T00-00-00.000.json
    2026-04-15 19:25:10,920 | INFO | Skipping weekly_record_921_2008-08-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,920 | INFO | Processing file: weekly_record_922_2008-08-18T00-00-00.000.json
    2026-04-15 19:25:10,955 | INFO | Skipping weekly_record_922_2008-08-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,955 | INFO | Processing file: weekly_record_923_2008-08-25T00-00-00.000.json
    2026-04-15 19:25:10,977 | INFO | Skipping weekly_record_923_2008-08-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:10,977 | INFO | Processing file: weekly_record_924_2008-09-01T00-00-00.000.json
    2026-04-15 19:25:11,001 | INFO | Skipping weekly_record_924_2008-09-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,001 | INFO | Processing file: weekly_record_925_2008-09-08T00-00-00.000.json
    2026-04-15 19:25:11,026 | INFO | Skipping weekly_record_925_2008-09-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,027 | INFO | Processing file: weekly_record_926_2008-09-15T00-00-00.000.json
    2026-04-15 19:25:11,053 | INFO | Skipping weekly_record_926_2008-09-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,054 | INFO | Processing file: weekly_record_927_2008-09-22T00-00-00.000.json
    2026-04-15 19:25:11,074 | INFO | Skipping weekly_record_927_2008-09-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,076 | INFO | Processing file: weekly_record_928_2008-09-29T00-00-00.000.json
    2026-04-15 19:25:11,097 | INFO | Skipping weekly_record_928_2008-09-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,098 | INFO | Processing file: weekly_record_929_2008-10-06T00-00-00.000.json
    2026-04-15 19:25:11,123 | INFO | Skipping weekly_record_929_2008-10-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,125 | INFO | Processing file: weekly_record_92_1992-09-21T00-00-00.000.json
    2026-04-15 19:25:11,150 | INFO | Skipping weekly_record_92_1992-09-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,150 | INFO | Processing file: weekly_record_930_2008-10-13T00-00-00.000.json
    2026-04-15 19:25:11,173 | INFO | Skipping weekly_record_930_2008-10-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,174 | INFO | Processing file: weekly_record_931_2008-10-20T00-00-00.000.json
    2026-04-15 19:25:11,200 | INFO | Skipping weekly_record_931_2008-10-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,201 | INFO | Processing file: weekly_record_932_2008-10-27T00-00-00.000.json
    2026-04-15 19:25:11,216 | INFO | Skipping weekly_record_932_2008-10-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,217 | INFO | Processing file: weekly_record_933_2008-11-03T00-00-00.000.json
    2026-04-15 19:25:11,234 | INFO | Skipping weekly_record_933_2008-11-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,235 | INFO | Processing file: weekly_record_934_2008-11-10T00-00-00.000.json
    2026-04-15 19:25:11,284 | INFO | Skipping weekly_record_934_2008-11-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,285 | INFO | Processing file: weekly_record_935_2008-11-17T00-00-00.000.json
    2026-04-15 19:25:11,314 | INFO | Skipping weekly_record_935_2008-11-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,315 | INFO | Processing file: weekly_record_936_2008-11-24T00-00-00.000.json
    2026-04-15 19:25:11,338 | INFO | Skipping weekly_record_936_2008-11-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,339 | INFO | Processing file: weekly_record_937_2008-12-01T00-00-00.000.json
    2026-04-15 19:25:11,366 | INFO | Skipping weekly_record_937_2008-12-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,367 | INFO | Processing file: weekly_record_938_2008-12-08T00-00-00.000.json
    2026-04-15 19:25:11,385 | INFO | Skipping weekly_record_938_2008-12-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,385 | INFO | Processing file: weekly_record_939_2008-12-15T00-00-00.000.json
    2026-04-15 19:25:11,404 | INFO | Skipping weekly_record_939_2008-12-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,405 | INFO | Processing file: weekly_record_93_1992-09-28T00-00-00.000.json
    2026-04-15 19:25:11,420 | INFO | Skipping weekly_record_93_1992-09-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,421 | INFO | Processing file: weekly_record_940_2008-12-22T00-00-00.000.json
    2026-04-15 19:25:11,447 | INFO | Skipping weekly_record_940_2008-12-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,447 | INFO | Processing file: weekly_record_941_2008-12-29T00-00-00.000.json
    2026-04-15 19:25:11,465 | INFO | Skipping weekly_record_941_2008-12-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,466 | INFO | Processing file: weekly_record_942_2009-01-05T00-00-00.000.json
    2026-04-15 19:25:11,483 | INFO | Skipping weekly_record_942_2009-01-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,484 | INFO | Processing file: weekly_record_943_2009-01-12T00-00-00.000.json
    2026-04-15 19:25:11,512 | INFO | Skipping weekly_record_943_2009-01-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,512 | INFO | Processing file: weekly_record_944_2009-01-19T00-00-00.000.json
    2026-04-15 19:25:11,537 | INFO | Skipping weekly_record_944_2009-01-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,538 | INFO | Processing file: weekly_record_945_2009-01-26T00-00-00.000.json
    2026-04-15 19:25:11,559 | INFO | Skipping weekly_record_945_2009-01-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,560 | INFO | Processing file: weekly_record_946_2009-02-02T00-00-00.000.json
    2026-04-15 19:25:11,581 | INFO | Skipping weekly_record_946_2009-02-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,581 | INFO | Processing file: weekly_record_947_2009-02-09T00-00-00.000.json
    2026-04-15 19:25:11,633 | INFO | Skipping weekly_record_947_2009-02-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,634 | INFO | Processing file: weekly_record_948_2009-02-16T00-00-00.000.json
    2026-04-15 19:25:11,656 | INFO | Skipping weekly_record_948_2009-02-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,657 | INFO | Processing file: weekly_record_949_2009-02-23T00-00-00.000.json
    2026-04-15 19:25:11,678 | INFO | Skipping weekly_record_949_2009-02-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,679 | INFO | Processing file: weekly_record_94_1992-10-05T00-00-00.000.json
    2026-04-15 19:25:11,702 | INFO | Skipping weekly_record_94_1992-10-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,703 | INFO | Processing file: weekly_record_950_2009-03-02T00-00-00.000.json
    2026-04-15 19:25:11,729 | INFO | Skipping weekly_record_950_2009-03-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,729 | INFO | Processing file: weekly_record_951_2009-03-09T00-00-00.000.json
    2026-04-15 19:25:11,756 | INFO | Skipping weekly_record_951_2009-03-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,757 | INFO | Processing file: weekly_record_952_2009-03-16T00-00-00.000.json
    2026-04-15 19:25:11,776 | INFO | Skipping weekly_record_952_2009-03-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,776 | INFO | Processing file: weekly_record_953_2009-03-23T00-00-00.000.json
    2026-04-15 19:25:11,806 | INFO | Skipping weekly_record_953_2009-03-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,807 | INFO | Processing file: weekly_record_954_2009-03-30T00-00-00.000.json
    2026-04-15 19:25:11,834 | INFO | Skipping weekly_record_954_2009-03-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,835 | INFO | Processing file: weekly_record_955_2009-04-06T00-00-00.000.json
    2026-04-15 19:25:11,855 | INFO | Skipping weekly_record_955_2009-04-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,856 | INFO | Processing file: weekly_record_956_2009-04-13T00-00-00.000.json
    2026-04-15 19:25:11,877 | INFO | Skipping weekly_record_956_2009-04-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,877 | INFO | Processing file: weekly_record_957_2009-04-20T00-00-00.000.json
    2026-04-15 19:25:11,900 | INFO | Skipping weekly_record_957_2009-04-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,901 | INFO | Processing file: weekly_record_958_2009-04-27T00-00-00.000.json
    2026-04-15 19:25:11,917 | INFO | Skipping weekly_record_958_2009-04-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,918 | INFO | Processing file: weekly_record_959_2009-05-04T00-00-00.000.json
    2026-04-15 19:25:11,944 | INFO | Skipping weekly_record_959_2009-05-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,945 | INFO | Processing file: weekly_record_95_1992-10-12T00-00-00.000.json
    2026-04-15 19:25:11,962 | INFO | Skipping weekly_record_95_1992-10-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,963 | INFO | Processing file: weekly_record_960_2009-05-11T00-00-00.000.json
    2026-04-15 19:25:11,980 | INFO | Skipping weekly_record_960_2009-05-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:11,981 | INFO | Processing file: weekly_record_961_2009-05-18T00-00-00.000.json
    2026-04-15 19:25:12,001 | INFO | Skipping weekly_record_961_2009-05-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,012 | INFO | Processing file: weekly_record_962_2009-05-25T00-00-00.000.json
    2026-04-15 19:25:12,040 | INFO | Skipping weekly_record_962_2009-05-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,041 | INFO | Processing file: weekly_record_963_2009-06-01T00-00-00.000.json
    2026-04-15 19:25:12,065 | INFO | Skipping weekly_record_963_2009-06-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,065 | INFO | Processing file: weekly_record_964_2009-06-08T00-00-00.000.json
    2026-04-15 19:25:12,089 | INFO | Skipping weekly_record_964_2009-06-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,089 | INFO | Processing file: weekly_record_965_2009-06-15T00-00-00.000.json
    2026-04-15 19:25:12,114 | INFO | Skipping weekly_record_965_2009-06-15T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,114 | INFO | Processing file: weekly_record_966_2009-06-22T00-00-00.000.json
    2026-04-15 19:25:12,137 | INFO | Skipping weekly_record_966_2009-06-22T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,137 | INFO | Processing file: weekly_record_967_2009-06-29T00-00-00.000.json
    2026-04-15 19:25:12,159 | INFO | Skipping weekly_record_967_2009-06-29T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,159 | INFO | Processing file: weekly_record_968_2009-07-06T00-00-00.000.json
    2026-04-15 19:25:12,187 | INFO | Skipping weekly_record_968_2009-07-06T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,187 | INFO | Processing file: weekly_record_969_2009-07-13T00-00-00.000.json
    2026-04-15 19:25:12,207 | INFO | Skipping weekly_record_969_2009-07-13T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,207 | INFO | Processing file: weekly_record_96_1992-10-19T00-00-00.000.json
    2026-04-15 19:25:12,226 | INFO | Skipping weekly_record_96_1992-10-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,227 | INFO | Processing file: weekly_record_970_2009-07-20T00-00-00.000.json
    2026-04-15 19:25:12,244 | INFO | Skipping weekly_record_970_2009-07-20T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,244 | INFO | Processing file: weekly_record_971_2009-07-27T00-00-00.000.json
    2026-04-15 19:25:12,270 | INFO | Skipping weekly_record_971_2009-07-27T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,270 | INFO | Processing file: weekly_record_972_2009-08-03T00-00-00.000.json
    2026-04-15 19:25:12,298 | INFO | Skipping weekly_record_972_2009-08-03T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,298 | INFO | Processing file: weekly_record_973_2009-08-10T00-00-00.000.json
    2026-04-15 19:25:12,326 | INFO | Skipping weekly_record_973_2009-08-10T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,326 | INFO | Processing file: weekly_record_974_2009-08-17T00-00-00.000.json
    2026-04-15 19:25:12,350 | INFO | Skipping weekly_record_974_2009-08-17T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,350 | INFO | Processing file: weekly_record_975_2009-08-24T00-00-00.000.json
    2026-04-15 19:25:12,377 | INFO | Skipping weekly_record_975_2009-08-24T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,397 | INFO | Processing file: weekly_record_976_2009-08-31T00-00-00.000.json
    2026-04-15 19:25:12,417 | INFO | Skipping weekly_record_976_2009-08-31T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,418 | INFO | Processing file: weekly_record_977_2009-09-07T00-00-00.000.json
    2026-04-15 19:25:12,443 | INFO | Skipping weekly_record_977_2009-09-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,444 | INFO | Processing file: weekly_record_978_2009-09-14T00-00-00.000.json
    2026-04-15 19:25:12,471 | INFO | Skipping weekly_record_978_2009-09-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,471 | INFO | Processing file: weekly_record_979_2009-09-21T00-00-00.000.json
    2026-04-15 19:25:12,497 | INFO | Skipping weekly_record_979_2009-09-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,498 | INFO | Processing file: weekly_record_97_1992-10-26T00-00-00.000.json
    2026-04-15 19:25:12,526 | INFO | Skipping weekly_record_97_1992-10-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,526 | INFO | Processing file: weekly_record_980_2009-09-28T00-00-00.000.json
    2026-04-15 19:25:12,543 | INFO | Skipping weekly_record_980_2009-09-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,544 | INFO | Processing file: weekly_record_981_2009-10-05T00-00-00.000.json
    2026-04-15 19:25:12,560 | INFO | Skipping weekly_record_981_2009-10-05T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,561 | INFO | Processing file: weekly_record_982_2009-10-12T00-00-00.000.json
    2026-04-15 19:25:12,583 | INFO | Skipping weekly_record_982_2009-10-12T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,584 | INFO | Processing file: weekly_record_983_2009-10-19T00-00-00.000.json
    2026-04-15 19:25:12,607 | INFO | Skipping weekly_record_983_2009-10-19T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,607 | INFO | Processing file: weekly_record_984_2009-10-26T00-00-00.000.json
    2026-04-15 19:25:12,624 | INFO | Skipping weekly_record_984_2009-10-26T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,624 | INFO | Processing file: weekly_record_985_2009-11-02T00-00-00.000.json
    2026-04-15 19:25:12,645 | INFO | Skipping weekly_record_985_2009-11-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,645 | INFO | Processing file: weekly_record_986_2009-11-09T00-00-00.000.json
    2026-04-15 19:25:12,669 | INFO | Skipping weekly_record_986_2009-11-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,670 | INFO | Processing file: weekly_record_987_2009-11-16T00-00-00.000.json
    2026-04-15 19:25:12,695 | INFO | Skipping weekly_record_987_2009-11-16T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,696 | INFO | Processing file: weekly_record_988_2009-11-23T00-00-00.000.json
    2026-04-15 19:25:12,742 | INFO | Skipping weekly_record_988_2009-11-23T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,749 | INFO | Processing file: weekly_record_989_2009-11-30T00-00-00.000.json
    2026-04-15 19:25:12,778 | INFO | Skipping weekly_record_989_2009-11-30T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,778 | INFO | Processing file: weekly_record_98_1992-11-02T00-00-00.000.json
    2026-04-15 19:25:12,808 | INFO | Skipping weekly_record_98_1992-11-02T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,808 | INFO | Processing file: weekly_record_990_2009-12-07T00-00-00.000.json
    2026-04-15 19:25:12,838 | INFO | Skipping weekly_record_990_2009-12-07T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,839 | INFO | Processing file: weekly_record_991_2009-12-14T00-00-00.000.json
    2026-04-15 19:25:12,857 | INFO | Skipping weekly_record_991_2009-12-14T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,858 | INFO | Processing file: weekly_record_992_2009-12-21T00-00-00.000.json
    2026-04-15 19:25:12,886 | INFO | Skipping weekly_record_992_2009-12-21T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,887 | INFO | Processing file: weekly_record_993_2009-12-28T00-00-00.000.json
    2026-04-15 19:25:12,908 | INFO | Skipping weekly_record_993_2009-12-28T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,909 | INFO | Processing file: weekly_record_994_2010-01-04T00-00-00.000.json
    2026-04-15 19:25:12,934 | INFO | Skipping weekly_record_994_2010-01-04T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,935 | INFO | Processing file: weekly_record_995_2010-01-11T00-00-00.000.json
    2026-04-15 19:25:12,966 | INFO | Skipping weekly_record_995_2010-01-11T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,966 | INFO | Processing file: weekly_record_996_2010-01-18T00-00-00.000.json
    2026-04-15 19:25:12,998 | INFO | Skipping weekly_record_996_2010-01-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:12,999 | INFO | Processing file: weekly_record_997_2010-01-25T00-00-00.000.json
    2026-04-15 19:25:13,025 | INFO | Skipping weekly_record_997_2010-01-25T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:13,026 | INFO | Processing file: weekly_record_998_2010-02-01T00-00-00.000.json
    2026-04-15 19:25:13,043 | INFO | Skipping weekly_record_998_2010-02-01T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:13,044 | INFO | Processing file: weekly_record_999_2010-02-08T00-00-00.000.json
    2026-04-15 19:25:13,061 | INFO | Skipping weekly_record_999_2010-02-08T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:13,062 | INFO | Processing file: weekly_record_99_1992-11-09T00-00-00.000.json
    2026-04-15 19:25:13,077 | INFO | Skipping weekly_record_99_1992-11-09T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:13,078 | INFO | Processing file: weekly_record_9_1991-02-18T00-00-00.000.json
    2026-04-15 19:25:13,105 | INFO | Skipping weekly_record_9_1991-02-18T00-00-00.000.json because it is already uploaded.
    2026-04-15 19:25:13,106 | INFO | Upload complete | uploaded=0 | skipped=1839 | failed=0


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
    #"gas_price",
    "wti_price",
    "recession",
    "wti_pct_change",
    #"gas_lag1",
    #"gas_lag2",
    #"gas_lag3"
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
