"""Record a verified publisher title without changing a paper's displayed title."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def set_original_title(db_path: Path, *, source: str, source_id: str, title: str) -> int:
    title = title.strip()
    if not title:
        raise ValueError("An original title is required")
    # mode=rw refuses to create a database if the operator supplies the wrong path.
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT paper_id, metadata_json FROM paper_sources WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        if row is None:
            raise ValueError("No article matches that source and source ID")
        metadata = json.loads(row[1] or "{}")
        metadata["original_title"] = title
        connection.execute(
            "UPDATE paper_sources SET metadata_json=? WHERE source=? AND source_id=?",
            (json.dumps(metadata, ensure_ascii=False), source, source_id),
        )
        return int(row[0])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    paper_id = set_original_title(args.db, source=args.source, source_id=args.source_id, title=args.title)
    print(f"Recorded original title for paper {paper_id}")
