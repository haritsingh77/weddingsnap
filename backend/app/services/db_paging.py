"""
Paged reads for Supabase/PostgREST.

PostgREST caps a single select at 1000 rows and returns the truncated set with
no error and no indication that anything is missing. That silence has caused
four separate bugs in this project:

  matching        a guest saw 1000 of 3,688 group photos
  People tab      counting 23,813 guest_photos rows counted the first 1000, so
                  70 of 70 guests displayed "0 photos"
  cluster photos  a person with more than 1000 photos showed only the first 1000
  downloads       a guest's zip silently stopped at 1000 files

Every one looked like working code. Use these helpers instead of .execute() on
anything whose size grows with the gallery.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

PAGE = 1000


def fetch_all(query_factory: Callable[[int, int], Any]) -> list[dict]:
    """Read every row, a page at a time.

    query_factory(start, end) must return a query with .range(start, end)
    already applied, e.g.

        fetch_all(lambda a, b:
            supabase.table("faces").select("id").range(a, b))
    """
    rows: list[dict] = []
    offset = 0
    while True:
        page = (query_factory(offset, offset + PAGE - 1).execute()).data or []
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += PAGE


def chunked(items: Iterable, size: int = 200) -> Iterable[list]:
    """Split a list for .in_() filters.

    Separate from the row cap: .in_() goes into the URL, so a few thousand ids
    exceed the maximum URL length and the request fails outright rather than
    truncating. 200 keeps it comfortably short.
    """
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
