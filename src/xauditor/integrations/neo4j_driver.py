from __future__ import annotations

from typing import Any, Mapping, Protocol

from neo4j import GraphDatabase
from neo4j import Driver as BoltDriver

from xauditor.config import Neo4jConfig
from xauditor.errors import GraphdbError


class Neo4jDriverProtocol(Protocol):
    def verify_connectivity(self) -> None: ...

    def run_batch(self, queries: list[str], *, timeout: int | None = None) -> None: ...

    def run_write(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> None: ...

    def read_rows(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


class Neo4jDriver:
    """Thin wrapper around the neo4j Python SDK driver."""

    def __init__(self, config: Neo4jConfig, *, host: str = "localhost") -> None:
        self.config = config
        self._host = host
        self._driver: BoltDriver | None = None

    @property
    def host(self) -> str:
        # Public read of ``_host`` so the subprocess pool's
        # ``Neo4jSourceDescriptor`` capture in
        # ``Neo4jAuditGraphSource.pool_descriptor`` doesn't reach into
        # private attribute names.
        return self._host

    @property
    def uri(self) -> str:
        return f"bolt://{self._host}:{self.config.bolt_port}"

    def _driver_handle(self) -> BoltDriver:
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                self.uri,
                auth=(self.config.username, self.config.password),
            )
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def verify_connectivity(self) -> None:
        try:
            self._driver_handle().verify_connectivity()
        except Exception as exc:
            raise GraphdbError(f"Failed to verify Neo4j connectivity: {exc}") from exc

    def run_batch(self, queries: list[str], *, timeout: int | None = None) -> None:
        del timeout
        if not queries:
            return
        try:
            with self._driver_handle().session(database=self.config.database) as session:
                for query in queries:
                    session.run(query).consume()
        except GraphdbError:
            raise
        except Exception as exc:
            raise GraphdbError(f"Failed to execute Cypher batch: {exc}") from exc

    def read_rows(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        del timeout
        try:
            with self._driver_handle().session(database=self.config.database) as session:
                def work(tx) -> list[dict[str, Any]]:
                    result = tx.run(query, dict(parameters or {}))
                    return [record.data() for record in result]

                return session.execute_read(work)
        except Exception as exc:
            raise GraphdbError(f"Failed to execute Cypher read: {exc}") from exc

    def run_write(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> None:
        del timeout
        try:
            with self._driver_handle().session(database=self.config.database) as session:
                def work(tx) -> None:
                    tx.run(query, dict(parameters or {})).consume()

                session.execute_write(work)
        except Exception as exc:
            raise GraphdbError(f"Failed to execute Cypher write: {exc}") from exc


class InMemoryNeo4jDriver:
    """No-op driver used by the in-memory testing stack."""

    def __init__(self, config: Neo4jConfig | None = None) -> None:
        self.config = config
        self.running = True

    def verify_connectivity(self) -> None:
        if not self.running:
            raise GraphdbError("In-memory Neo4j driver is not available.")

    def run_batch(self, queries: list[str], *, timeout: int | None = None) -> None:
        if not self.running:
            raise GraphdbError("In-memory Neo4j driver is not available.")

    def run_write(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> None:
        if not self.running:
            raise GraphdbError("In-memory Neo4j driver is not available.")

    def read_rows(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self.running:
            raise GraphdbError("In-memory Neo4j driver is not available.")
        return []

    def close(self) -> None:
        self.running = False


__all__ = [
    "InMemoryNeo4jDriver",
    "Neo4jDriver",
    "Neo4jDriverProtocol",
]
