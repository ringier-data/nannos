"""Helpers for user-supplied search terms in SQL LIKE/ILIKE predicates."""

# Backslash is both our escape character and a literal users can type, so it is
# escaped first — otherwise escaping % and _ would double-escape it.
_LIKE_METACHARACTERS = str.maketrans({"\\": r"\\", "%": r"\%", "_": r"\_"})


def like_contains(term: str) -> str:
    """Turn a search term into a `%…%` pattern with its metacharacters escaped.

    `%` and `_` are wildcards in LIKE/ILIKE, so a term containing either matches
    far more than the user asked for: searching for `50%` otherwise matches every
    row starting with `50`, and a single `_` matches everything. Pair this with
    ``ESCAPE '\\'`` on the predicate, which `like_clause` writes for you.

    This is about wildcard semantics, not injection — the term is always a bound
    parameter.
    """
    return f"%{term.translate(_LIKE_METACHARACTERS)}%"


def like_clause(*columns: str, param: str = "search") -> str:
    """An OR'd case-insensitive match over `columns`, with the escape declared.

        like_clause("u.first_name", "u.email")
        -> "(u.first_name ILIKE :search ESCAPE '\\' OR u.email ILIKE :search ESCAPE '\\')"
    """
    matches = " OR ".join(f"{column} ILIKE :{param} ESCAPE '\\'" for column in columns)
    return f"({matches})"
