"""Inventory state separate from the legacy seed catalog; no media filesystem IO."""
from contextlib import AbstractContextManager
from typing import Protocol

from app.models.inventory import InventoryState
from app.repositories.database import Database


class InventoryRepository(Protocol):
    def transaction(self) -> AbstractContextManager[object]: ...
    def load(self) -> InventoryState: ...
    def save(self, state: InventoryState) -> None: ...


class SQLiteInventoryRepository:
    # One versioned document suffices for fixture reconciliation. Normalize/index
    # file/revision tables before scaling to a real library; no schema-v1 rewrite.
    key = "reconciliation_inventory_v1"

    def __init__(self, database: Database):
        self.database = database

    def transaction(self) -> AbstractContextManager[object]:
        return self.database.transaction()

    def load(self) -> InventoryState:
        with self.database.transaction() as connection:
            row = connection.execute("SELECT payload FROM metadata WHERE id=?", (self.key,)).fetchone()
            return InventoryState.model_validate_json(row[0]) if row else InventoryState()

    def save(self, state: InventoryState) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO metadata VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (self.key, state.model_dump_json()),
            )
