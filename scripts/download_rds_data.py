import csv
import os
import subprocess

# Configuration
DB_HOST = "catalogstack-databaserdseb6bf969-lnaf4kh9utsi.cgifqtggnaml.ap-south-1.rds.amazonaws.com"
DB_USER = "admin"
DB_PASSWORD = ".qdlj5iLMC3CQnj1H1-QB0c7VSNw81"
DB_NAME = "catalog"
DB_PORT = 3306
OUTPUT_DIR = "./rds_dump"
S3_BUCKET = "search-item-item-poc"  # Your existing S3 bucket

def run_mysql(query):
    """Run a mysql query and return tab-separated rows."""
    cmd = [
        "mysql",
        "-h", DB_HOST,
        "-u", DB_USER,
        f"-p{DB_PASSWORD}",
        "--batch",       # Tab-separated output, includes column headers
        DB_NAME,
        "-e", query
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"MySQL error: {result.stderr}")
        return []
    lines = result.stdout.strip().split("\n")
    return [line.split("\t") for line in lines if line]

def export_table_to_csv(table):
    """Export a single table to a CSV file."""
    print(f"Exporting table: {table}...")
    rows = run_mysql(f"SELECT * FROM `{table}`")
    if not rows:
        print(f"  Table {table} is empty or failed.")
        return

    csv_path = os.path.join(OUTPUT_DIR, f"{table}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)  # First row is the header (column names)

    print(f"  Saved {len(rows) - 1} rows to {csv_path}")
    return csv_path

def upload_to_s3(file_path):
    """Upload a file to S3 so it can be downloaded on your Mac."""
    filename = os.path.basename(file_path)
    s3_key = f"rds_export/{filename}"
    cmd = ["aws", "s3", "cp", file_path, f"s3://{S3_BUCKET}/{s3_key}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  Uploaded to s3://{S3_BUCKET}/{s3_key}")
        print(f"  Download on your Mac with:")
        print(f"    aws s3 cp s3://{S3_BUCKET}/{s3_key} ~/Downloads/{filename}")
    else:
        print(f"  S3 upload failed: {result.stderr}")

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Get list of tables
    tables_rows = run_mysql("SHOW TABLES")
    tables = [row[0] for row in tables_rows[1:]]  # Skip header row
    print(f"Found {len(tables)} tables: {', '.join(tables)}")

    # Export each table to CSV and upload to S3
    for table in tables:
        csv_path = export_table_to_csv(table)
        if csv_path:
            upload_to_s3(csv_path)

    print("\nAll done! Run the aws s3 cp commands above on your Mac to download the CSVs.")

if __name__ == "__main__":
    main()
