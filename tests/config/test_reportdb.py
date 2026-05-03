from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.config import ConfigError, RemoteConnectionConfig, load_config


def _write_project_yaml(repo_root: Path, body: str) -> None:
    (repo_root / "xauditor.yml").write_text(
        textwrap.dedent(body).strip() + "\n", encoding="utf-8"
    )


class ReportDBDefaultsTests(unittest.TestCase):
    def test_defaults_when_no_reportdb_block_is_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            reportdb = config.reportdb
            self.assertEqual(reportdb.image, "postgres:16-alpine")
            self.assertEqual(reportdb.container_name, "xauditor-reportdb")
            self.assertEqual(reportdb.volume_name, "xauditor-reportdb-data")
            self.assertEqual(reportdb.username, "xauditor")
            self.assertEqual(reportdb.password, "xauditor-password")
            self.assertEqual(reportdb.port, 5432)
            self.assertEqual(reportdb.database, "xauditor_reportdb")
            self.assertEqual(reportdb.ready_timeout_seconds, 60)
            self.assertIsNone(reportdb.remote)

    def test_local_yaml_overrides_selected_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                reportdb:
                  image: postgres:16-alpine
                  port: 55432
                  database: custom_db
                  ready_timeout_seconds: 90
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.reportdb.port, 55432)
            self.assertEqual(config.reportdb.database, "custom_db")
            self.assertEqual(config.reportdb.ready_timeout_seconds, 90)
            self.assertIsNone(config.reportdb.remote)

    def test_invalid_port_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                reportdb:
                  port: not-a-number
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("reportdb.port", str(ctx.exception))

    def test_unknown_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                reportdb:
                  bogus_field: nope
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("reportdb", str(ctx.exception))


class ReportDBRemoteTests(unittest.TestCase):
    def test_valid_remote_url_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                reportdb:
                  remote:
                    url: postgresql+asyncpg://user:pass@host:5432/xauditor
                    pool_size: 10
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            remote = config.reportdb.remote
            self.assertIsInstance(remote, RemoteConnectionConfig)
            assert remote is not None  # for type narrowing
            self.assertEqual(
                remote.url, "postgresql+asyncpg://user:pass@host:5432/xauditor"
            )
            self.assertEqual(remote.pool_size, 10)
            self.assertIsNone(remote.ssl_ca)

    def test_remote_url_must_use_a_supported_scheme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                reportdb:
                  remote:
                    url: sqlite:///tmp/data.db
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("reportdb.remote.url", str(ctx.exception))

    def test_remote_url_without_scheme_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                reportdb:
                  remote:
                    url: just-a-hostname
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("reportdb.remote.url", str(ctx.exception))

    def test_remote_block_without_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                reportdb:
                  remote:
                    pool_size: 4
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("reportdb.remote.url", str(ctx.exception))

    def test_remote_with_both_local_and_remote_fields_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                reportdb:
                  image: postgres:16-alpine
                  port: 5432
                  remote:
                    url: postgresql://user:pass@host:5432/db
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIsNotNone(config.reportdb.remote)
            assert config.reportdb.remote is not None
            self.assertEqual(
                config.reportdb.remote.url, "postgresql://user:pass@host:5432/db"
            )
            self.assertEqual(config.reportdb.port, 5432)


class ReportDBEnvOverrideTests(unittest.TestCase):
    def test_env_variables_override_yaml_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                reportdb:
                  image: postgres:16-alpine
                  port: 5432
                  database: yaml_db
                """,
            )
            config = load_config(
                repo_root=repo_root,
                env={
                    "HOME": tmp,
                    "XAUDITOR_REPORTDB_IMAGE": "postgres:17-alpine",
                    "XAUDITOR_REPORTDB_PASSWORD": "from-env",
                    "XAUDITOR_REPORTDB_PORT": "6543",
                    "XAUDITOR_REPORTDB_DATABASE": "env_db",
                },
            )
            self.assertEqual(config.reportdb.image, "postgres:17-alpine")
            self.assertEqual(config.reportdb.password, "from-env")
            self.assertEqual(config.reportdb.port, 6543)
            self.assertEqual(config.reportdb.database, "env_db")

    def test_env_remote_url_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(
                repo_root=repo_root,
                env={
                    "HOME": tmp,
                    "XAUDITOR_REPORTDB_REMOTE_URL": "postgresql://u:p@example.com:5432/db",
                },
            )
            self.assertIsNotNone(config.reportdb.remote)
            assert config.reportdb.remote is not None
            self.assertEqual(
                config.reportdb.remote.url,
                "postgresql://u:p@example.com:5432/db",
            )


class GraphDbRemoteTests(unittest.TestCase):
    def test_graph_db_remote_url_from_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                graph:
                  db:
                    remote:
                      url: neo4j+s://graph.example.com:7687
                """,
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIsNotNone(config.graphdb.remote)
            assert config.graphdb.remote is not None
            self.assertEqual(
                config.graphdb.remote.url, "neo4j+s://graph.example.com:7687"
            )

    def test_graph_db_remote_url_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = load_config(
                repo_root=repo_root,
                env={
                    "HOME": tmp,
                    "XAUDITOR_GRAPHDB_REMOTE_URL": "bolt://graph.example.com:7687",
                },
            )
            self.assertIsNotNone(config.graphdb.remote)
            assert config.graphdb.remote is not None
            self.assertEqual(
                config.graphdb.remote.url, "bolt://graph.example.com:7687"
            )

    def test_graph_db_rejects_invalid_remote_scheme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                """
                graph:
                  db:
                    remote:
                      url: http://graph.example.com
                """,
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("graph.db.remote.url", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
