"""Shared query models for the console's list endpoints."""

from enum import Enum


class OwnershipFilter(str, Enum):
    """Which side of the console's owned / shared-with-me split to list.

    The console shows these as tabs. Splitting a page in the browser would give
    each tab an arbitrary fraction of its real contents, so the choice is a
    server-side filter applied before the page is cut.
    """

    OWNED = "owned"
    SHARED = "shared"
