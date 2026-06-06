"""SQL schema files shipped with the package.

Sub-packages hold one directory per backend (currently only ``postgres``;
SQLite schema lives inline in :mod:`knowledge.db` because APSW's load-as-
extension model already handles versioning via ``meta.schema_version``).
"""
