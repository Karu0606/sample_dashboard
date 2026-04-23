#!/usr/bin/env python3
"""
Kiro Pilot Cost Report
Reads Kiro user activity report CSVs from S3 and calculates estimated costs
based on subscription tier pricing and overage credits.

Usage:
    python3 kiro_pilot_cost_report.py \
        --bucket YOUR-BUCKET \
        --prefix path/to/KiroLogs/user_report/us-east-1 \
        --region us-east-1

    # Optional: filter to last N days
    python3 kiro_pilot_cost_report.py \
        --bucket YOUR-BUCKET \
        --prefix path/to/KiroLogs/user_report/us-east-1 \
        --region us-east-1 \
        --days 30

    # Optional: output as CSV
    python3 kiro_pilot_cost_report.py \
        --bucket YOUR-BUCKET \
        --prefix path/to/KiroLogs/user_report/us-east-1 \
        --region us-east-1 \
        --output cost_report.csv
"""

import argparse
import csv
import io
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import boto3

# Kiro tier pricing (monthly)
TIER_PRICING = {
    "Pro": 20.00,
    "ProPlus": 40.00,
    "Power": 200.00,
}
OVERAGE_COST_PER_CREDIT = 0.04


def list_csv_files(s3_client, bucket, prefix):
    """List all CSV files under the given S3 prefix."""
    files = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".csv"):
                files.append(obj["Key"])
    return sorted(files)


def read_csv_from_s3(s3_client, bucket, key):
    """Read a CSV file from S3 and return rows as list of dicts."""
    response = s3_client.get_object(Bucket=bucket, Key=key)
    content = response["Body"].read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    return list(reader)


def safe_float(val, default=0.0):
    try:
        return float(val) if val and val.strip() not in ("", "None") else default
    except (ValueError, TypeError):
        return default


def parse_data(all_rows, days_filter=None):
    """Parse rows into structured data, optionally filtering by date."""
    cutoff = None
    if days_filter:
        cutoff = (datetime.utcnow() - timedelta(days=days_filter)).strftime("%Y-%m-%d")

    user_data = defaultdict(lambda: {
        "tier": "Pro",
        "credits_used": 0.0,
        "overage_credits": 0.0,
        "messages": 0,
        "conversations": 0,
        "days_active": set(),
        "months": defaultdict(lambda: {"credits": 0.0, "overage": 0.0}),
    })

    for row in all_rows:
        date = row.get("date", "").strip()
        if not date:
            continue
        if cutoff and date < cutoff:
            continue

        userid = row.get("userid", "").strip().strip("'\"")
        if not userid:
            continue

        tier = row.get("subscription_tier", "Pro").strip()
        credits = safe_float(row.get("credits_used"))
        overage = safe_float(row.get("overage_credits_used"))
        messages = int(safe_float(row.get("total_messages")))
        convos = int(safe_float(row.get("chat_conversations")))
        month = date[:7]  # YYYY-MM

        u = user_data[userid]
        u["tier"] = tier
        u["credits_used"] += credits
        u["overage_credits"] += overage
        u["messages"] += messages
        u["conversations"] += convos
        u["days_active"].add(date)
        u["months"][month]["credits"] += credits
        u["months"][month]["overage"] += overage

    return user_data


def calculate_costs(user_data):
    """Calculate estimated costs per user."""
    results = []
    for userid, data in user_data.items():
        tier = data["tier"]
        sub_cost = TIER_PRICING.get(tier, 20.0)
        # Count distinct months active to calculate subscription cost
        months_active = len(data["months"])
        total_sub = sub_cost * max(months_active, 1)
        overage_cost = data["overage_credits"] * OVERAGE_COST_PER_CREDIT
        total_cost = total_sub + overage_cost

        results.append({
            "userid": userid,
            "tier": tier,
            "credits_used": round(data["credits_used"], 1),
            "overage_credits": round(data["overage_credits"], 1),
            "messages": data["messages"],
            "conversations": data["conversations"],
            "days_active": len(data["days_active"]),
            "months_active": months_active,
            "subscription_cost": round(total_sub, 2),
            "overage_cost": round(overage_cost, 2),
            "total_cost": round(total_cost, 2),
            "monthly": dict(data["months"]),
        })

    results.sort(key=lambda x: x["total_cost"], reverse=True)
    return results


def print_report(results, all_rows, days_filter):
    """Print a formatted cost report to stdout."""
    if not results:
        print("No data found.")
        return

    # Gather all dates
    dates = set()
    for row in all_rows:
        d = row.get("date", "").strip()
        if d:
            dates.add(d)
    min_date = min(dates) if dates else "N/A"
    max_date = max(dates) if dates else "N/A"

    total_users = len(results)
    total_credits = sum(r["credits_used"] for r in results)
    total_overage = sum(r["overage_credits"] for r in results)
    total_sub_cost = sum(r["subscription_cost"] for r in results)
    total_overage_cost = sum(r["overage_credits"] * OVERAGE_COST_PER_CREDIT for r in results)
    total_cost = sum(r["total_cost"] for r in results)

    w = 65
    print()
    print("═" * w)
    print("KIRO PILOT COST REPORT".center(w))
    print("═" * w)

    print()
    print("SUMMARY")
    period = f"{min_date} → {max_date}"
    if days_filter:
        period += f"  (last {days_filter} days)"
    print(f"  Report period:         {period}")
    print(f"  Total users:           {total_users}")
    print(f"  Total credits used:    {total_credits:,.1f}")
    print(f"  Total overage credits: {total_overage:,.1f}")

    print()
    print("ESTIMATED COSTS")
    print(f"  Subscription fees:     ${total_sub_cost:,.2f}")
    print(f"  Overage charges:       ${total_overage_cost:,.2f}")
    print(f"  Total estimated:       ${total_cost:,.2f}")

    print()
    print("PER-USER BREAKDOWN")
    hdr = f"  {'User':<30} {'Tier':<8} {'Credits':>9} {'Overage':>9} {'Est. Cost':>10}"
    print(hdr)
    print("  " + "─" * (w - 2))
    for r in results:
        uid = r["userid"][:30]
        print(f"  {uid:<30} {r['tier']:<8} {r['credits_used']:>9,.1f} {r['overage_credits']:>9,.1f} ${r['total_cost']:>9,.2f}")

    # Monthly trend
    monthly = defaultdict(lambda: {"users": set(), "credits": 0.0, "overage": 0.0})
    for r in results:
        for month, vals in r["monthly"].items():
            monthly[month]["users"].add(r["userid"])
            monthly[month]["credits"] += vals["credits"]
            monthly[month]["overage"] += vals["overage"]

    if monthly:
        print()
        print("MONTHLY TREND")
        hdr = f"  {'Month':<10} {'Users':>6} {'Credits':>10} {'Overage':>10} {'Est. Cost':>11}"
        print(hdr)
        print("  " + "─" * (w - 2))
        for month in sorted(monthly.keys()):
            m = monthly[month]
            n_users = len(m["users"])
            # Estimate: each user pays their tier price for that month
            month_sub = 0
            for r in results:
                if month in r["monthly"]:
                    month_sub += TIER_PRICING.get(r["tier"], 20.0)
            month_overage = m["overage"] * OVERAGE_COST_PER_CREDIT
            month_total = month_sub + month_overage
            print(f"  {month:<10} {n_users:>6} {m['credits']:>10,.1f} {m['overage']:>10,.1f} ${month_total:>10,.2f}")

    print()
    print("═" * w)
    print()


def write_csv_output(results, output_path):
    """Write per-user cost data to a CSV file."""
    fieldnames = [
        "userid", "tier", "credits_used", "overage_credits",
        "messages", "conversations", "days_active", "months_active",
        "subscription_cost", "overage_cost", "total_cost",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            row = {k: r[k] for k in fieldnames}
            writer.writerow(row)
    print(f"CSV written to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Kiro Pilot Cost Report")
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--prefix", required=True, help="S3 prefix to user_report folder")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("--days", type=int, default=None, help="Filter to last N days")
    parser.add_argument("--output", default=None, help="Output CSV file path")
    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=args.region)

    print(f"Scanning s3://{args.bucket}/{args.prefix}/ ...")
    csv_files = list_csv_files(s3, args.bucket, args.prefix)

    if not csv_files:
        print("No CSV files found. Check your bucket/prefix and ensure user reports are enabled.")
        sys.exit(1)

    print(f"Found {len(csv_files)} CSV file(s). Reading...")

    all_rows = []
    for key in csv_files:
        try:
            rows = read_csv_from_s3(s3, args.bucket, key)
            all_rows.extend(rows)
        except Exception as e:
            print(f"  Warning: could not read {key}: {e}", file=sys.stderr)

    print(f"Loaded {len(all_rows)} row(s) of user activity data.")

    user_data = parse_data(all_rows, days_filter=args.days)
    results = calculate_costs(user_data)

    print_report(results, all_rows, args.days)

    if args.output:
        write_csv_output(results, args.output)


if __name__ == "__main__":
    main()
