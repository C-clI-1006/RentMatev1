"""Repositories (shared persistence layer).

SQLite via stdlib `sqlite3` (no ORM for the course prototype).
Each module's repository owns its own tables per db/schema.sql and only
exposes the data its module owns. Cross-module reads go through
contracts, not through foreign tables.
"""
