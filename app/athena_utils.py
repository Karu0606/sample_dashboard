"""Helpers for working with Amazon Athena query results."""


def collect_query_results(client, query_execution_id):
    """Return ``(columns, rows)`` for a completed Athena query.

    Amazon Athena's ``GetQueryResults`` API returns at most 1000 rows per
    page and exposes additional pages via a ``NextToken``. This helper follows
    those tokens so result sets larger than a single page are fully retrieved,
    instead of being silently truncated at the first page.

    Athena includes the column header as the first row of the **first page
    only**; subsequent pages contain data rows from the very first row. The
    header is therefore stripped from the first page exclusively.

    Args:
        client: A boto3 Athena client.
        query_execution_id: The execution id of a SUCCEEDED query.

    Returns:
        A tuple ``(columns, rows)`` where ``columns`` is a list of column
        labels and ``rows`` is a list of row value lists (strings).
    """
    columns = None
    rows = []
    next_token = None
    first_page = True

    while True:
        kwargs = {"QueryExecutionId": query_execution_id}
        if next_token:
            kwargs["NextToken"] = next_token
        result = client.get_query_results(**kwargs)

        if columns is None:
            columns = [
                col["Label"]
                for col in result["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
            ]

        page_rows = result["ResultSet"]["Rows"]
        if first_page:
            # The header row is only present on the first page.
            page_rows = page_rows[1:]
            first_page = False

        for row in page_rows:
            rows.append([field.get("VarCharValue", "") for field in row["Data"]])

        next_token = result.get("NextToken")
        if not next_token:
            break

    return columns, rows
