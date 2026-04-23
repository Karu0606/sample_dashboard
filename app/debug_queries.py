#!/usr/bin/env python3
"""Debug script to test Athena queries outside of Streamlit."""

import boto3
import time
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / '.env')

AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')
ATHENA_DATABASE = os.getenv('ATHENA_DATABASE', '')
ATHENA_OUTPUT_BUCKET = os.getenv('ATHENA_OUTPUT_BUCKET', '')
CUR_DATABASE = os.getenv('CUR_DATABASE', '')
CUR_ENABLED = os.getenv('CUR_ENABLED', 'false').lower() == 'true'

athena = boto3.client('athena', region_name=AWS_REGION)
glue = boto3.client('glue', region_name=AWS_REGION)


def run_query(database, query):
    print(f"\n{'='*60}")
    print(f"DB: {database}")
    print(f"Query: {query[:200]}...")
    print('='*60)
    try:
        resp = athena.start_query_execution(
            QueryString=query,
            QueryExecutionContext={'Database': database},
            ResultConfiguration={'OutputLocation': ATHENA_OUTPUT_BUCKET}
        )
        qid = resp['QueryExecutionId']
        while True:
            result = athena.get_query_execution(QueryExecutionId=qid)
            status = result['QueryExecution']['Status']['State']
            if status in ['SUCCEEDED', 'FAILED', 'CANCELLED']:
                break
            time.sleep(1)
        if status != 'SUCCEEDED':
            error = result['QueryExecution']['Status'].get('StateChangeReason', 'Unknown')
            print(f"❌ FAILED: {error}")
            return
        results = athena.get_query_results(QueryExecutionId=qid)
        columns = [c['Label'] for c in results['ResultSet']['ResultSetMetadata']['ColumnInfo']]
        print(f"Columns: {columns}")
        rows = results['ResultSet']['Rows'][1:]  # skip header
        print(f"Rows returned: {len(rows)}")
        for row in rows[:10]:
            vals = [f.get('VarCharValue', '') for f in row['Data']]
            print(f"  {dict(zip(columns, vals))}")
    except Exception as e:
        print(f"❌ ERROR: {e}")


def main():
    print("=== KIRO DASHBOARD DEBUG ===")
    print(f"Region: {AWS_REGION}")
    print(f"Athena DB: {ATHENA_DATABASE}")
    print(f"CUR DB: {CUR_DATABASE}")
    print(f"CUR Enabled: {CUR_ENABLED}")
    print(f"Output: {ATHENA_OUTPUT_BUCKET}")

    # 1. Check Glue tables for user report
    print("\n--- Glue Tables (user report) ---")
    try:
        tables = glue.get_tables(DatabaseName=ATHENA_DATABASE, MaxResults=10)
        for t in tables.get('TableList', []):
            print(f"  Table: {t['Name']} | Location: {t.get('StorageDescriptor', {}).get('Location', 'N/A')}")
            table_name = t['Name']
    except Exception as e:
        print(f"  ❌ {e}")
        table_name = None

    # 2. Query user report data
    if table_name:
        run_query(ATHENA_DATABASE, f"SELECT * FROM \"{table_name}\" LIMIT 5")
        run_query(ATHENA_DATABASE, f"""
            SELECT COUNT(*) as row_count,
                   COUNT(DISTINCT userid) as users,
                   MIN(date) as min_date,
                   MAX(date) as max_date
            FROM \"{table_name}\"
        """)

    # 3. Check CUR tables
    if CUR_ENABLED and CUR_DATABASE:
        print("\n--- Glue Tables (CUR) ---")
        try:
            cur_tables = glue.get_tables(DatabaseName=CUR_DATABASE, MaxResults=10)
            for t in cur_tables.get('TableList', []):
                print(f"  Table: {t['Name']} | Location: {t.get('StorageDescriptor', {}).get('Location', 'N/A')}")
        except Exception as e:
            print(f"  ❌ {e}")

        # Find the data table
        cur_table = None
        for t in cur_tables.get('TableList', []):
            if t['Name'] not in ('cost_and_usage_data_status',):
                cur_table = t['Name']
                break

        if cur_table:
            # Check all product codes
            run_query(CUR_DATABASE, f"""
                SELECT DISTINCT line_item_product_code
                FROM \"{cur_table}\"
                ORDER BY 1
                LIMIT 50
            """)

            # Check for AmazonQ specifically
            run_query(CUR_DATABASE, f"""
                SELECT line_item_product_code, line_item_operation,
                       COUNT(*) as cnt,
                       SUM(line_item_unblended_cost) as total_cost
                FROM \"{cur_table}\"
                WHERE line_item_product_code = 'AmazonQ'
                GROUP BY line_item_product_code, line_item_operation
                ORDER BY total_cost DESC
                LIMIT 20
            """)

            # Broader search
            run_query(CUR_DATABASE, f"""
                SELECT line_item_product_code, line_item_operation,
                       SUM(line_item_unblended_cost) as total_cost
                FROM \"{cur_table}\"
                WHERE LOWER(line_item_product_code) LIKE '%q%'
                   OR LOWER(line_item_product_code) LIKE '%kiro%'
                   OR LOWER(line_item_product_code) LIKE '%whisperer%'
                GROUP BY line_item_product_code, line_item_operation
                LIMIT 20
            """)

    print("\n=== DEBUG COMPLETE ===")


if __name__ == '__main__':
    main()
