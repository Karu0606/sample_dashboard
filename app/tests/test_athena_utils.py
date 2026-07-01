"""Tests for athena_utils.collect_query_results.

These run without AWS credentials by mocking the Athena client. They lock in
the pagination behaviour (following NextToken) and the rule that the column
header appears only on the first page.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from athena_utils import collect_query_results


def _cell(value):
    return {"VarCharValue": value}


def _row(values):
    return {"Data": [_cell(v) for v in values]}


def _page(rows, columns=None, next_token=None):
    result_set = {"Rows": rows}
    if columns is not None:
        result_set["ResultSetMetadata"] = {
            "ColumnInfo": [{"Label": c} for c in columns]
        }
    page = {"ResultSet": result_set}
    if next_token is not None:
        page["NextToken"] = next_token
    return page


class FakeAthenaClient:
    """Returns pre-canned pages in order; records the NextTokens it was given."""

    def __init__(self, pages):
        self._pages = pages
        self._idx = 0
        self.seen_tokens = []

    def get_query_results(self, **kwargs):
        self.seen_tokens.append(kwargs.get("NextToken"))
        page = self._pages[self._idx]
        self._idx += 1
        return page


def test_single_page_strips_header_only():
    header = _row(["userid", "messages"])
    data = [_row(["u1", "5"]), _row(["u2", "9"])]
    client = FakeAthenaClient([_page([header] + data, columns=["userid", "messages"])])

    columns, rows = collect_query_results(client, "qid")

    assert columns == ["userid", "messages"]
    assert rows == [["u1", "5"], ["u2", "9"]]


def test_follows_next_token_across_pages():
    header = _row(["userid"])
    page1 = _page(
        [header, _row(["u1"]), _row(["u2"])],
        columns=["userid"],
        next_token="tok1",
    )
    # Subsequent pages have NO header row - first row is real data.
    page2 = _page([_row(["u3"]), _row(["u4"])], next_token="tok2")
    page3 = _page([_row(["u5"])])  # no token -> last page

    client = FakeAthenaClient([page1, page2, page3])

    columns, rows = collect_query_results(client, "qid")

    assert columns == ["userid"]
    # All five users, in order, with no header leaking in and no page-2/3 data dropped.
    assert [r[0] for r in rows] == ["u1", "u2", "u3", "u4", "u5"]
    # First call has no token, then it followed tok1 and tok2.
    assert client.seen_tokens == [None, "tok1", "tok2"]


def test_large_result_set_not_truncated():
    """Simulate >1000 rows spread across pages of 1000 (Athena's max page)."""
    header = _row(["n"])
    page1_rows = [header] + [_row([str(i)]) for i in range(999)]  # 999 data rows
    page1 = _page(page1_rows, columns=["n"], next_token="more")
    page2 = _page([_row([str(i)]) for i in range(999, 1500)])  # 501 data rows

    client = FakeAthenaClient([page1, page2])

    columns, rows = collect_query_results(client, "qid")

    assert len(rows) == 1500
    assert rows[0] == ["0"]
    assert rows[-1] == ["1499"]
