from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from xauditor.audit.context import build_path_context
from xauditor.config import load_config
from xauditor.graph.builder import LangChainGraphBuilder
from xauditor.graph.canonical import DiscoveredFile
from xauditor.graph.parsers import get_parser
from xauditor.graph.scope import resolve_repository_scope


def _make_file(path: str, content: str) -> DiscoveredFile:
    del content
    return DiscoveredFile(path=path, module_name="m", language="python")


def _parse_python(path: str, content: str):
    return get_parser("python").parse(_make_file(path, content), content)


class PythonModuleSymbolExtractionTest(unittest.TestCase):
    def test_extracts_globals_constants_and_data_structures(self) -> None:
        source = textwrap.dedent(
            """
            API_KEY = "abc123"
            counter = 0

            class Settings:
                retries: int = 3

            def handler(request):
                counter_local = counter
                settings = Settings()
                return API_KEY, counter_local, settings.retries
            """
        ).strip() + "\n"
        parsed = _parse_python("app.py", source)
        by_name = {sym.name: sym for sym in parsed.module_symbols}
        self.assertEqual(by_name["API_KEY"].kind, "constant")
        self.assertEqual(by_name["API_KEY"].value_repr, "'abc123'")
        self.assertEqual(by_name["counter"].kind, "global")
        self.assertEqual(by_name["Settings"].kind, "data_structure")

        use_names = {use.symbol_name for use in parsed.symbol_uses}
        self.assertIn("API_KEY", use_names)
        self.assertIn("counter", use_names)
        self.assertIn("Settings", use_names)

    def test_large_string_constant_is_replaced_with_placeholder(self) -> None:
        big_literal = "x" * 300
        source = f'BIG_BLOB = "{big_literal}"\n'
        parsed = _parse_python("c.py", source)
        big_sym = next(sym for sym in parsed.module_symbols if sym.name == "BIG_BLOB")
        self.assertTrue(big_sym.is_placeholder)
        self.assertIn("placeholder:string:length=300", big_sym.value_repr)

    def test_large_bytes_constant_is_replaced_with_placeholder(self) -> None:
        source = "PAYLOAD = b'" + ("a" * 400) + "'\n"
        parsed = _parse_python("c.py", source)
        sym = next(s for s in parsed.module_symbols if s.name == "PAYLOAD")
        self.assertTrue(sym.is_placeholder)
        self.assertIn("placeholder:bytes:length=400", sym.value_repr)


class AuditPathContextIncludesSymbolsTest(unittest.TestCase):
    def test_path_context_includes_referenced_globals_constants_and_types(self) -> None:
        source = textwrap.dedent(
            """
            API_KEY = "abc"
            request_count = 0

            class Session:
                pass

            def handler():
                global request_count
                request_count += 1
                session = Session()
                return API_KEY, session
            """
        ).strip() + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "app.py").write_text(source, encoding="utf-8")
            config = load_config(
                repo_root=repo_root,
                env={
                    "XAUDITOR_LLM_BASE_URL": "mock://offline",
                    "XAUDITOR_LLM_API_KEY": "secret",
                    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
                    "XAUDITOR_GRAPH_BUILD_ENABLE_LLM_ENRICHMENT": "false",
                },
                require_llm=True,
            )
            scope = resolve_repository_scope(repo_root, ())
            from xauditor.integrations.neo4j import InMemoryNeo4jAdapter
            from xauditor.integrations.neo4j_repository import InMemoryNeo4jGraphRepository
            adapter = InMemoryNeo4jAdapter()
            repo = InMemoryNeo4jGraphRepository(adapter)
            fingerprint = LangChainGraphBuilder.from_config(config, repository=repo).build(
                repo_root=repo_root, scope=scope
            )
            build = repo._require_build(fingerprint)
            symbol_kinds = {sym.name: sym.kind for sym in build.module_symbols}
            self.assertEqual(symbol_kinds.get("API_KEY"), "constant")
            self.assertEqual(symbol_kinds.get("request_count"), "global")
            self.assertEqual(symbol_kinds.get("Session"), "data_structure")
            handler = next(fn for fn in build.functions if fn.name == "handler")
            used_symbol_ids = {
                use.symbol_id for use in build.function_symbol_uses if use.function_id == handler.function_id
            }
            self.assertTrue(used_symbol_ids, "handler should reference module symbols")

            context = build_path_context(
                module_symbols=build.module_symbols,
                function_symbol_uses=build.function_symbol_uses,
                repo_root=repo_root,
                path_functions=[handler],
            )
            referenced_names = {item["name"] for item in context["referenced_symbols"]}
            self.assertIn("API_KEY", referenced_names)
            self.assertIn("request_count", referenced_names)
            self.assertIn("Session", referenced_names)


if __name__ == "__main__":
    unittest.main()
