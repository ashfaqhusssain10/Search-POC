"""Quick inspection script — prints 5 rows from each platter DynamoDB table.

Usage:
    python -m scripts.inspect_dynamo
"""

import json
import os

import boto3

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")

TABLES = [
    "DefaultPlattersTable",
    "DefaultPlattersCategoriesTable",
    "DefaultPlatterItemsTable",
]


def scan_sample(table_name: str, limit: int = 5) -> list[dict]:
    resource = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = resource.Table(table_name)
    response = table.scan(Limit=limit)
    return response.get("Items", [])


def main() -> None:
    for table_name in TABLES:
        print(f"\n{'='*60}")
        print(f"TABLE: {table_name}")
        print('='*60)
        try:
            rows = scan_sample(table_name)
            for i, row in enumerate(rows, 1):
                print(f"\n--- Row {i} ---")
                print(json.dumps(row, indent=2, default=str))
        except Exception as exc:
            print(f"ERROR: {exc}")


if __name__ == "__main__":
    main()
