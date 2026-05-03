from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Protocol

import tree_sitter_c as _tsc
import tree_sitter_cpp as _tscpp
import tree_sitter_go as _tsgo
import tree_sitter_java as _tsjava
import tree_sitter_javascript as _tsjs
import tree_sitter_typescript as _tsts
from tree_sitter import Language, Parser

from xauditor.graph.canonical import DiscoveredFile


@dataclass(frozen=True)
class _ParsedClassMember:
    member_id: str
    name: str
    class_id: str
    line_number: int


@dataclass(frozen=True)
class _ParsedFunction:
    function_id: str
    name: str
    qualified_name: str
    class_id: str | None
    class_name: str | None
    start_line: int
    end_line: int


@dataclass(frozen=True)
class _ParsedClass:
    class_id: str
    name: str
    start_line: int
    end_line: int
    methods: tuple[_ParsedFunction, ...]
    members: tuple[_ParsedClassMember, ...]


@dataclass(frozen=True)
class _ParsedCall:
    caller_function_id: str
    callee_name: str
    callee_qualified_name: str | None
    caller_class_id: str | None
    line_number: int
    evidence: str


@dataclass(frozen=True)
class _ParsedModuleSymbol:
    symbol_id: str
    name: str
    kind: str
    start_line: int
    end_line: int
    type_annotation: str = ""
    value_repr: str = ""
    is_placeholder: bool = False


@dataclass(frozen=True)
class _ParsedSymbolUse:
    function_id: str
    symbol_name: str
    line_number: int
    evidence: str


@dataclass(frozen=True)
class _ParsedFile:
    classes: tuple[_ParsedClass, ...]
    functions: tuple[_ParsedFunction, ...]
    calls: tuple[_ParsedCall, ...]
    module_symbols: tuple[_ParsedModuleSymbol, ...] = ()
    symbol_uses: tuple[_ParsedSymbolUse, ...] = ()


_LITERAL_PLACEHOLDER_THRESHOLD = 256


def _summarize_literal_value(value: object) -> tuple[str, bool]:
    if isinstance(value, str):
        if len(value) > _LITERAL_PLACEHOLDER_THRESHOLD:
            return f"<placeholder:string:length={len(value)}>", True
        return repr(value), False
    if isinstance(value, (bytes, bytearray)):
        if len(value) > _LITERAL_PLACEHOLDER_THRESHOLD:
            return f"<placeholder:bytes:length={len(value)}>", True
        return repr(bytes(value)), False
    rendered = repr(value)
    if len(rendered) > _LITERAL_PLACEHOLDER_THRESHOLD:
        return f"<placeholder:literal:length={len(rendered)}>", True
    return rendered, False


class LanguageParser(Protocol):
    def parse(self, file: DiscoveredFile, content: str) -> _ParsedFile: ...


_parser_logger: object | None = None


def set_parser_logger(logger: object | None) -> None:
    global _parser_logger
    _parser_logger = logger


class PythonParser:
    def parse(self, file: DiscoveredFile, content: str) -> _ParsedFile:
        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            if _parser_logger is not None:
                _parser_logger.warning(
                    f"PythonParser skipped {file.path}: {type(exc).__name__}: {exc.msg} "
                    f"(line {exc.lineno}, offset {exc.offset})"
                )
            return _ParsedFile(classes=(), functions=(), calls=())
        classes: list[_ParsedClass] = []
        functions: list[_ParsedFunction] = []
        calls: list[_ParsedCall] = []
        module_symbols: list[_ParsedModuleSymbol] = []
        function_nodes_by_id: dict[str, ast.AST] = {}

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = self._build_function(
                    node=node, file=file, class_id=None, class_name=None
                )
                functions.append(fn)
                function_nodes_by_id[fn.function_id] = node
                calls.extend(self._extract_calls(node, fn, content=content))
                continue
            if isinstance(node, ast.ClassDef):
                class_id = f"{file.path}:{node.name}:{node.lineno}"
                methods: list[_ParsedFunction] = []
                members: list[_ParsedClassMember] = []
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method = self._build_function(
                            node=child, file=file, class_id=class_id, class_name=node.name
                        )
                        methods.append(method)
                        function_nodes_by_id[method.function_id] = child
                        calls.extend(self._extract_calls(child, method, content=content))
                        continue
                    members.extend(self._build_class_members(child, file=file, class_id=class_id))
                end_line = getattr(node, "end_lineno", node.lineno)
                classes.append(
                    _ParsedClass(
                        class_id=class_id,
                        name=node.name,
                        start_line=node.lineno,
                        end_line=end_line,
                        methods=tuple(methods),
                        members=tuple(members),
                    )
                )
                functions.extend(methods)
                module_symbols.append(
                    _ParsedModuleSymbol(
                        symbol_id=f"{file.path}:sym:{node.name}:{node.lineno}",
                        name=node.name,
                        kind="data_structure",
                        start_line=node.lineno,
                        end_line=end_line,
                    )
                )
                continue
            module_symbols.extend(
                self._extract_module_assignments(node, file=file, content=content)
            )

        symbols_by_name: dict[str, _ParsedModuleSymbol] = {sym.name: sym for sym in module_symbols}
        symbol_uses: list[_ParsedSymbolUse] = []
        for function in functions:
            fn_node = function_nodes_by_id.get(function.function_id)
            if fn_node is None:
                continue
            parameter_names = self._collect_parameter_names(fn_node)
            local_names = self._collect_local_assignments(fn_node)
            reserved = parameter_names | local_names | {"self", "cls"}
            seen: set[tuple[str, int]] = set()
            for inner in ast.walk(fn_node):
                name: str | None = None
                line = getattr(inner, "lineno", function.start_line)
                if isinstance(inner, ast.Name):
                    name = inner.id
                elif isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
                    name = inner.value.id
                if not name or name in reserved:
                    continue
                if name not in symbols_by_name:
                    continue
                if name == function.name and symbols_by_name[name].kind != "data_structure":
                    continue
                key = (name, line)
                if key in seen:
                    continue
                seen.add(key)
                symbol_uses.append(
                    _ParsedSymbolUse(
                        function_id=function.function_id,
                        symbol_name=name,
                        line_number=line,
                        evidence=ast.get_source_segment(content, inner) or name,
                    )
                )

        return _ParsedFile(
            classes=tuple(classes),
            functions=tuple(functions),
            calls=tuple(calls),
            module_symbols=tuple(module_symbols),
            symbol_uses=tuple(symbol_uses),
        )

    def _extract_module_assignments(
        self, node: ast.AST, *, file: DiscoveredFile, content: str
    ) -> list[_ParsedModuleSymbol]:
        results: list[_ParsedModuleSymbol] = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            value_repr, is_placeholder = self._literal_repr(node.value)
            for target in targets:
                kind = "constant" if target.id.isupper() else "global"
                results.append(
                    _ParsedModuleSymbol(
                        symbol_id=f"{file.path}:sym:{target.id}:{node.lineno}",
                        name=target.id,
                        kind=kind,
                        start_line=node.lineno,
                        end_line=getattr(node, "end_lineno", node.lineno),
                        value_repr=value_repr,
                        is_placeholder=is_placeholder,
                    )
                )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            annotation = ast.get_source_segment(content, node.annotation) or ""
            if node.value is not None:
                value_repr, is_placeholder = self._literal_repr(node.value)
            else:
                value_repr, is_placeholder = "", False
            is_final = "Final" in annotation or node.target.id.isupper()
            kind = "constant" if is_final else "global"
            results.append(
                _ParsedModuleSymbol(
                    symbol_id=f"{file.path}:sym:{node.target.id}:{node.lineno}",
                    name=node.target.id,
                    kind=kind,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    type_annotation=annotation,
                    value_repr=value_repr,
                    is_placeholder=is_placeholder,
                )
            )
        return results

    def _literal_repr(self, node: ast.AST | None) -> tuple[str, bool]:
        if node is None:
            return "", False
        try:
            value = ast.literal_eval(node)
        except (ValueError, SyntaxError):
            return "<non-literal>", False
        return _summarize_literal_value(value)

    def _collect_parameter_names(self, node: ast.AST) -> set[str]:
        names: set[str] = set()
        args = getattr(node, "args", None)
        if args is None:
            return names
        for group in (args.args, args.kwonlyargs, args.posonlyargs):
            for arg in group:
                names.add(arg.arg)
        if args.vararg:
            names.add(args.vararg.arg)
        if args.kwarg:
            names.add(args.kwarg.arg)
        return names

    def _collect_local_assignments(self, node: ast.AST) -> set[str]:
        names: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                names.add(child.target.id)
            elif isinstance(child, (ast.For, ast.AsyncFor)) and isinstance(child.target, ast.Name):
                names.add(child.target.id)
        return names

    def _build_function(
        self,
        *,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        file: DiscoveredFile,
        class_id: str | None,
        class_name: str | None,
    ) -> _ParsedFunction:
        qualified_name = f"{class_name}.{node.name}" if class_name else node.name
        end_line = getattr(node, "end_lineno", node.lineno)
        return _ParsedFunction(
            function_id=f"{file.path}:{qualified_name}:{node.lineno}",
            name=node.name,
            qualified_name=qualified_name,
            class_id=class_id,
            class_name=class_name,
            start_line=node.lineno,
            end_line=end_line,
        )

    def _build_class_members(
        self, statement: ast.AST, *, file: DiscoveredFile, class_id: str
    ) -> list[_ParsedClassMember]:
        results: list[_ParsedClassMember] = []
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            return results
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            line_number = getattr(statement, "lineno", 1)
            results.append(
                _ParsedClassMember(
                    member_id=f"{file.path}:{class_id.split(':')[1]}:{target.id}:{line_number}",
                    name=target.id,
                    class_id=class_id,
                    line_number=line_number,
                )
            )
        return results

    def _extract_calls(
        self,
        function_node: ast.FunctionDef | ast.AsyncFunctionDef,
        parsed_fn: _ParsedFunction,
        *,
        content: str,
    ) -> list[_ParsedCall]:
        calls: list[_ParsedCall] = []
        for inner in ast.walk(function_node):
            if not isinstance(inner, ast.Call):
                continue
            callee_name, callee_qualified = self._resolve_callee(
                inner.func, class_name=parsed_fn.class_name
            )
            if not callee_name:
                continue
            calls.append(
                _ParsedCall(
                    caller_function_id=parsed_fn.function_id,
                    callee_name=callee_name,
                    callee_qualified_name=callee_qualified,
                    caller_class_id=parsed_fn.class_id,
                    line_number=inner.lineno,
                    evidence=ast.get_source_segment(content, inner) or callee_name,
                )
            )
        return calls

    def _resolve_callee(
        self, node: ast.AST, *, class_name: str | None
    ) -> tuple[str | None, str | None]:
        if isinstance(node, ast.Name):
            return node.id, None
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                owner = node.value.id
                if owner in {"self", "cls"} and class_name:
                    return node.attr, f"{class_name}.{node.attr}"
                if owner and owner[:1].isupper():
                    return node.attr, f"{owner}.{node.attr}"
            return None, None
        return None, None


_C_LANGUAGE = Language(_tsc.language())
_CPP_LANGUAGE = Language(_tscpp.language())


class _TreeSitterCLikeParser:
    def __init__(self, *, language: str, ts_language: Language) -> None:
        self._language = language
        self._parser = Parser(ts_language)

    def parse(self, file: DiscoveredFile, content: str) -> _ParsedFile:
        source_bytes = content.encode("utf-8", errors="replace")
        tree = self._parser.parse(source_bytes)
        classes: list[_ParsedClass] = []
        functions: list[_ParsedFunction] = []
        calls: list[_ParsedCall] = []
        self._walk(
            tree.root_node,
            source_bytes=source_bytes,
            file=file,
            scope_stack=[],
            classes=classes,
            functions=functions,
            calls=calls,
        )
        return _ParsedFile(
            classes=tuple(classes), functions=tuple(functions), calls=tuple(calls)
        )

    def _walk(
        self,
        node,
        *,
        source_bytes: bytes,
        file: DiscoveredFile,
        scope_stack: list[str],
        classes: list[_ParsedClass],
        functions: list[_ParsedFunction],
        calls: list[_ParsedCall],
    ) -> None:
        for child in node.named_children:
            kind = child.type
            if kind == "namespace_definition" and self._language == "cpp":
                name_node = child.child_by_field_name("name")
                name = self._text(name_node, source_bytes) if name_node else "(anonymous)"
                body = child.child_by_field_name("body")
                if body is not None:
                    self._walk(
                        body,
                        source_bytes=source_bytes,
                        file=file,
                        scope_stack=[*scope_stack, name],
                        classes=classes,
                        functions=functions,
                        calls=calls,
                    )
                continue
            if kind in {"class_specifier", "struct_specifier"} and self._language == "cpp":
                parsed_class = self._build_class(
                    child,
                    source_bytes=source_bytes,
                    file=file,
                    scope_stack=scope_stack,
                    functions=functions,
                    calls=calls,
                )
                if parsed_class is not None:
                    classes.append(parsed_class)
                continue
            if kind == "function_definition":
                parsed_fn = self._build_function(
                    child,
                    source_bytes=source_bytes,
                    file=file,
                    scope_stack=scope_stack,
                    enclosing_class_id=None,
                    enclosing_class_name=None,
                )
                if parsed_fn is not None:
                    functions.append(parsed_fn)
                    calls.extend(
                        self._extract_calls(child, parsed_fn, source_bytes=source_bytes)
                    )
                continue
            if kind == "linkage_specification":
                body = child.child_by_field_name("body") or child
                self._walk(
                    body,
                    source_bytes=source_bytes,
                    file=file,
                    scope_stack=scope_stack,
                    classes=classes,
                    functions=functions,
                    calls=calls,
                )
                continue

    def _build_class(
        self,
        node,
        *,
        source_bytes: bytes,
        file: DiscoveredFile,
        scope_stack: list[str],
        functions: list[_ParsedFunction],
        calls: list[_ParsedCall],
    ) -> _ParsedClass | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = self._text(name_node, source_bytes)
        if not name:
            return None
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        class_id = f"{file.path}:{name}:{start_line}"
        methods: list[_ParsedFunction] = []
        members: list[_ParsedClassMember] = []
        body = node.child_by_field_name("body")
        if body is not None:
            for member_node in body.named_children:
                if member_node.type == "function_definition":
                    method = self._build_function(
                        member_node,
                        source_bytes=source_bytes,
                        file=file,
                        scope_stack=[*scope_stack, name],
                        enclosing_class_id=class_id,
                        enclosing_class_name=name,
                    )
                    if method is not None:
                        methods.append(method)
                        functions.append(method)
                        calls.extend(
                            self._extract_calls(member_node, method, source_bytes=source_bytes)
                        )
                elif member_node.type == "field_declaration":
                    members.extend(
                        self._extract_field_members(
                            member_node,
                            source_bytes=source_bytes,
                            class_id=class_id,
                            class_name=name,
                            file=file,
                        )
                    )
        return _ParsedClass(
            class_id=class_id,
            name=name,
            start_line=start_line,
            end_line=end_line,
            methods=tuple(methods),
            members=tuple(members),
        )

    def _extract_field_members(
        self,
        node,
        *,
        source_bytes: bytes,
        class_id: str,
        class_name: str,
        file: DiscoveredFile,
    ) -> list[_ParsedClassMember]:
        results: list[_ParsedClassMember] = []
        for declarator in node.children_by_field_name("declarator"):
            name_node = self._extract_field_name(declarator)
            if name_node is None:
                continue
            field_name = self._text(name_node, source_bytes)
            if not field_name:
                continue
            line_number = node.start_point[0] + 1
            results.append(
                _ParsedClassMember(
                    member_id=f"{file.path}:{class_name}:{field_name}:{line_number}",
                    name=field_name,
                    class_id=class_id,
                    line_number=line_number,
                )
            )
        return results

    def _extract_field_name(self, declarator):
        cur = declarator
        while cur is not None:
            if cur.type == "field_identifier" or cur.type == "identifier":
                return cur
            if cur.type == "function_declarator":
                return None
            nested = cur.child_by_field_name("declarator")
            if nested is None or nested is cur:
                return None
            cur = nested
        return None

    def _build_function(
        self,
        node,
        *,
        source_bytes: bytes,
        file: DiscoveredFile,
        scope_stack: list[str],
        enclosing_class_id: str | None,
        enclosing_class_name: str | None,
    ) -> _ParsedFunction | None:
        declarator = node.child_by_field_name("declarator")
        name_info = self._resolve_function_name(declarator, source_bytes=source_bytes)
        if name_info is None:
            return None
        base_name, qualified_parts = name_info
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        class_id = enclosing_class_id
        class_name = enclosing_class_name
        if len(qualified_parts) > 1:
            qualified_name = "::".join(qualified_parts)
            class_name = qualified_parts[-2]
            class_id = f"{file.path}:{class_name}:?"
        elif scope_stack:
            qualified_name = "::".join([*scope_stack, base_name])
        else:
            qualified_name = base_name
        return _ParsedFunction(
            function_id=f"{file.path}:{qualified_name}:{start_line}",
            name=base_name,
            qualified_name=qualified_name,
            class_id=class_id,
            class_name=class_name,
            start_line=start_line,
            end_line=end_line,
        )

    def _resolve_function_name(
        self, declarator, *, source_bytes: bytes
    ) -> tuple[str, list[str]] | None:
        cur = declarator
        while cur is not None and cur.type in {
            "pointer_declarator",
            "reference_declarator",
            "parenthesized_declarator",
        }:
            cur = cur.child_by_field_name("declarator")
        if cur is None or cur.type != "function_declarator":
            return None
        name_node = cur.child_by_field_name("declarator")
        while name_node is not None and name_node.type in {
            "pointer_declarator",
            "reference_declarator",
            "parenthesized_declarator",
        }:
            name_node = name_node.child_by_field_name("declarator")
        if name_node is None:
            return None
        if name_node.type == "qualified_identifier":
            parts = self._flatten_qualified(name_node, source_bytes=source_bytes)
            if not parts:
                return None
            return parts[-1], parts
        if name_node.type in {"identifier", "field_identifier", "destructor_name", "operator_name"}:
            text = self._text(name_node, source_bytes)
            if not text:
                return None
            return text, [text]
        text = self._text(name_node, source_bytes)
        if not text:
            return None
        return text, [text]

    def _flatten_qualified(self, node, *, source_bytes: bytes) -> list[str]:
        parts: list[str] = []

        def visit(current) -> None:
            if current.type == "qualified_identifier":
                scope = current.child_by_field_name("scope")
                name = current.child_by_field_name("name")
                if scope is not None:
                    visit(scope)
                if name is not None:
                    visit(name)
                return
            text = self._text(current, source_bytes)
            if text:
                parts.append(text)

        visit(node)
        return parts

    def _extract_calls(
        self, function_node, parsed_fn: _ParsedFunction, *, source_bytes: bytes
    ) -> list[_ParsedCall]:
        body = function_node.child_by_field_name("body")
        if body is None:
            return []
        collected: list[_ParsedCall] = []
        self._collect_calls(body, parsed_fn, source_bytes=source_bytes, calls=collected)
        return collected

    def _collect_calls(
        self, node, parsed_fn: _ParsedFunction, *, source_bytes: bytes, calls: list[_ParsedCall]
    ) -> None:
        if node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node is not None:
                callee = self._callee_from_node(fn_node, source_bytes=source_bytes)
                if callee is not None:
                    callee_name, callee_qualified = callee
                    evidence = self._text(node, source_bytes) or callee_name
                    calls.append(
                        _ParsedCall(
                            caller_function_id=parsed_fn.function_id,
                            callee_name=callee_name,
                            callee_qualified_name=callee_qualified,
                            caller_class_id=parsed_fn.class_id,
                            line_number=node.start_point[0] + 1,
                            evidence=evidence,
                        )
                    )
        for child in node.named_children:
            self._collect_calls(child, parsed_fn, source_bytes=source_bytes, calls=calls)

    def _callee_from_node(
        self, node, *, source_bytes: bytes
    ) -> tuple[str, str | None] | None:
        kind = node.type
        if kind == "identifier":
            text = self._text(node, source_bytes)
            return (text, None) if text else None
        if kind == "qualified_identifier":
            parts = self._flatten_qualified(node, source_bytes=source_bytes)
            if not parts:
                return None
            return parts[-1], "::".join(parts)
        if kind == "field_expression":
            field_node = node.child_by_field_name("field")
            if field_node is None:
                return None
            field_name = self._text(field_node, source_bytes)
            if not field_name:
                return None
            argument = node.child_by_field_name("argument")
            if argument is not None:
                owner = self._text(argument, source_bytes)
                if owner:
                    return field_name, f"{owner}.{field_name}"
            return field_name, None
        if kind == "template_function":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                return self._callee_from_node(name_node, source_bytes=source_bytes)
        return None

    def _text(self, node, source_bytes: bytes) -> str:
        if node is None:
            return ""
        return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _node_text(node, source_bytes: bytes) -> str:
    if node is None:
        return ""
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


class _GoParser:
    def __init__(self, ts_language: Language) -> None:
        self._parser = Parser(ts_language)

    def parse(self, file: DiscoveredFile, content: str) -> _ParsedFile:
        source_bytes = content.encode("utf-8", errors="replace")
        tree = self._parser.parse(source_bytes)
        functions: list[_ParsedFunction] = []
        calls: list[_ParsedCall] = []
        for child in tree.root_node.named_children:
            if child.type == "function_declaration":
                fn = self._build_function(
                    child, source_bytes, file, receiver_type=None
                )
                if fn is not None:
                    functions.append(fn)
                    calls.extend(self._extract_calls(child, fn, source_bytes))
            elif child.type == "method_declaration":
                receiver_type = self._extract_receiver_type(child, source_bytes)
                fn = self._build_function(
                    child, source_bytes, file, receiver_type=receiver_type
                )
                if fn is not None:
                    functions.append(fn)
                    calls.extend(self._extract_calls(child, fn, source_bytes))
        return _ParsedFile(classes=(), functions=tuple(functions), calls=tuple(calls))

    def _extract_receiver_type(self, node, source_bytes: bytes) -> str | None:
        receiver = node.child_by_field_name("receiver")
        if receiver is None:
            return None
        for param in receiver.named_children:
            if param.type != "parameter_declaration":
                continue
            type_node = param.child_by_field_name("type")
            if type_node is None:
                continue
            cur = type_node
            while cur is not None and cur.type == "pointer_type":
                cur = cur.named_child(0) if cur.named_child_count else None
            if cur is not None and cur.type == "type_identifier":
                return _node_text(cur, source_bytes)
        return None

    def _build_function(
        self,
        node,
        source_bytes: bytes,
        file: DiscoveredFile,
        *,
        receiver_type: str | None,
    ) -> _ParsedFunction | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        base_name = _node_text(name_node, source_bytes)
        if not base_name:
            return None
        qualified = f"{receiver_type}.{base_name}" if receiver_type else base_name
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        return _ParsedFunction(
            function_id=f"{file.path}:{qualified}:{start_line}",
            name=base_name,
            qualified_name=qualified,
            class_id=None,
            class_name=receiver_type,
            start_line=start_line,
            end_line=end_line,
        )

    def _extract_calls(self, node, parsed_fn: _ParsedFunction, source_bytes: bytes) -> list[_ParsedCall]:
        body = node.child_by_field_name("body")
        if body is None:
            return []
        collected: list[_ParsedCall] = []
        self._collect(body, parsed_fn, source_bytes, collected)
        return collected

    def _collect(self, node, parsed_fn: _ParsedFunction, source_bytes: bytes, out: list[_ParsedCall]) -> None:
        if node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node is not None:
                callee = self._callee(fn_node, source_bytes)
                if callee is not None:
                    name, qualified = callee
                    out.append(
                        _ParsedCall(
                            caller_function_id=parsed_fn.function_id,
                            callee_name=name,
                            callee_qualified_name=qualified,
                            caller_class_id=parsed_fn.class_id,
                            line_number=node.start_point[0] + 1,
                            evidence=_node_text(node, source_bytes) or name,
                        )
                    )
        for child in node.named_children:
            self._collect(child, parsed_fn, source_bytes, out)

    def _callee(self, node, source_bytes: bytes) -> tuple[str, str | None] | None:
        if node.type == "identifier":
            text = _node_text(node, source_bytes)
            return (text, None) if text else None
        if node.type == "selector_expression":
            field = node.child_by_field_name("field")
            operand = node.child_by_field_name("operand")
            if field is None:
                return None
            name = _node_text(field, source_bytes)
            if not name:
                return None
            if operand is not None:
                owner = _node_text(operand, source_bytes)
                if owner:
                    return name, f"{owner}.{name}"
            return name, None
        return None


class _JavaParser:
    _CLASS_TYPES = frozenset(
        {"class_declaration", "interface_declaration", "record_declaration", "enum_declaration"}
    )
    _METHOD_TYPES = frozenset({"method_declaration", "constructor_declaration"})

    def __init__(self, ts_language: Language) -> None:
        self._parser = Parser(ts_language)

    def parse(self, file: DiscoveredFile, content: str) -> _ParsedFile:
        source_bytes = content.encode("utf-8", errors="replace")
        tree = self._parser.parse(source_bytes)
        classes: list[_ParsedClass] = []
        functions: list[_ParsedFunction] = []
        calls: list[_ParsedCall] = []
        self._walk_top(tree.root_node, source_bytes, file, [], classes, functions, calls)
        return _ParsedFile(
            classes=tuple(classes), functions=tuple(functions), calls=tuple(calls)
        )

    def _walk_top(
        self,
        node,
        source_bytes: bytes,
        file: DiscoveredFile,
        scope_stack: list[str],
        classes: list[_ParsedClass],
        functions: list[_ParsedFunction],
        calls: list[_ParsedCall],
    ) -> None:
        for child in node.named_children:
            if child.type in self._CLASS_TYPES:
                parsed_cls = self._build_class(
                    child, source_bytes, file, scope_stack, classes, functions, calls
                )
                if parsed_cls is not None:
                    classes.append(parsed_cls)

    def _build_class(
        self,
        node,
        source_bytes: bytes,
        file: DiscoveredFile,
        scope_stack: list[str],
        classes: list[_ParsedClass],
        functions: list[_ParsedFunction],
        calls: list[_ParsedCall],
    ) -> _ParsedClass | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = _node_text(name_node, source_bytes)
        if not name:
            return None
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        class_id = f"{file.path}:{name}:{start_line}"
        nested_scope = [*scope_stack, name]
        methods: list[_ParsedFunction] = []
        members: list[_ParsedClassMember] = []
        body = node.child_by_field_name("body")
        if body is not None:
            for member_node in body.named_children:
                if member_node.type in self._METHOD_TYPES:
                    method = self._build_method(
                        member_node, source_bytes, file, class_id, name, nested_scope
                    )
                    if method is not None:
                        methods.append(method)
                        functions.append(method)
                        calls.extend(self._extract_calls(member_node, method, source_bytes))
                elif member_node.type == "field_declaration":
                    members.extend(
                        self._extract_fields(member_node, source_bytes, class_id, name, file)
                    )
                elif member_node.type in self._CLASS_TYPES:
                    nested = self._build_class(
                        member_node, source_bytes, file, nested_scope, classes, functions, calls
                    )
                    if nested is not None:
                        classes.append(nested)
        return _ParsedClass(
            class_id=class_id,
            name=name,
            start_line=start_line,
            end_line=end_line,
            methods=tuple(methods),
            members=tuple(members),
        )

    def _build_method(
        self,
        node,
        source_bytes: bytes,
        file: DiscoveredFile,
        class_id: str,
        class_name: str,
        scope: list[str],
    ) -> _ParsedFunction | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = _node_text(name_node, source_bytes)
        if not name:
            return None
        qualified = ".".join([*scope, name])
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        return _ParsedFunction(
            function_id=f"{file.path}:{qualified}:{start_line}",
            name=name,
            qualified_name=qualified,
            class_id=class_id,
            class_name=class_name,
            start_line=start_line,
            end_line=end_line,
        )

    def _extract_fields(
        self,
        node,
        source_bytes: bytes,
        class_id: str,
        class_name: str,
        file: DiscoveredFile,
    ) -> list[_ParsedClassMember]:
        results: list[_ParsedClassMember] = []
        line_number = node.start_point[0] + 1
        for declarator in node.children_by_field_name("declarator"):
            name_node = declarator.child_by_field_name("name")
            if name_node is None:
                continue
            field_name = _node_text(name_node, source_bytes)
            if not field_name:
                continue
            results.append(
                _ParsedClassMember(
                    member_id=f"{file.path}:{class_name}:{field_name}:{line_number}",
                    name=field_name,
                    class_id=class_id,
                    line_number=line_number,
                )
            )
        return results

    def _extract_calls(self, node, parsed_fn: _ParsedFunction, source_bytes: bytes) -> list[_ParsedCall]:
        body = node.child_by_field_name("body")
        if body is None:
            return []
        collected: list[_ParsedCall] = []
        self._collect(body, parsed_fn, source_bytes, collected)
        return collected

    def _collect(self, node, parsed_fn: _ParsedFunction, source_bytes: bytes, out: list[_ParsedCall]) -> None:
        if node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _node_text(name_node, source_bytes)
                if name:
                    obj = node.child_by_field_name("object")
                    qualified: str | None = None
                    if obj is not None:
                        owner = _node_text(obj, source_bytes)
                        if owner:
                            qualified = f"{owner}.{name}"
                    out.append(
                        _ParsedCall(
                            caller_function_id=parsed_fn.function_id,
                            callee_name=name,
                            callee_qualified_name=qualified,
                            caller_class_id=parsed_fn.class_id,
                            line_number=node.start_point[0] + 1,
                            evidence=_node_text(node, source_bytes) or name,
                        )
                    )
        for child in node.named_children:
            self._collect(child, parsed_fn, source_bytes, out)


class _JsLikeParser:
    _CLASS_TYPES = frozenset({"class_declaration", "abstract_class_declaration"})
    _CONTAINER_TYPES = frozenset(
        {
            "export_statement",
            "export_default_declaration",
            "lexical_declaration",
            "variable_declaration",
        }
    )

    def __init__(self, ts_language: Language) -> None:
        self._parser = Parser(ts_language)

    def parse(self, file: DiscoveredFile, content: str) -> _ParsedFile:
        source_bytes = content.encode("utf-8", errors="replace")
        tree = self._parser.parse(source_bytes)
        classes: list[_ParsedClass] = []
        functions: list[_ParsedFunction] = []
        calls: list[_ParsedCall] = []
        self._walk(
            tree.root_node, source_bytes, file, classes, functions, calls
        )
        return _ParsedFile(
            classes=tuple(classes), functions=tuple(functions), calls=tuple(calls)
        )

    def _walk(
        self,
        node,
        source_bytes: bytes,
        file: DiscoveredFile,
        classes: list[_ParsedClass],
        functions: list[_ParsedFunction],
        calls: list[_ParsedCall],
    ) -> None:
        for child in node.named_children:
            t = child.type
            if t in self._CLASS_TYPES:
                parsed_cls = self._build_class(
                    child, source_bytes, file, functions, calls
                )
                if parsed_cls is not None:
                    classes.append(parsed_cls)
                continue
            if t == "function_declaration":
                fn = self._build_function(child, source_bytes, file)
                if fn is not None:
                    functions.append(fn)
                    calls.extend(self._extract_calls(child, fn, source_bytes))
                continue
            if t in self._CONTAINER_TYPES:
                self._walk(child, source_bytes, file, classes, functions, calls)

    def _build_class(
        self,
        node,
        source_bytes: bytes,
        file: DiscoveredFile,
        functions: list[_ParsedFunction],
        calls: list[_ParsedCall],
    ) -> _ParsedClass | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = _node_text(name_node, source_bytes)
        if not name:
            return None
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        class_id = f"{file.path}:{name}:{start_line}"
        methods: list[_ParsedFunction] = []
        body = node.child_by_field_name("body")
        if body is not None:
            for member in body.named_children:
                if member.type == "method_definition":
                    method = self._build_method(
                        member, source_bytes, file, class_id, name
                    )
                    if method is not None:
                        methods.append(method)
                        functions.append(method)
                        calls.extend(self._extract_calls(member, method, source_bytes))
        return _ParsedClass(
            class_id=class_id,
            name=name,
            start_line=start_line,
            end_line=end_line,
            methods=tuple(methods),
            members=(),
        )

    def _build_method(
        self,
        node,
        source_bytes: bytes,
        file: DiscoveredFile,
        class_id: str,
        class_name: str,
    ) -> _ParsedFunction | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = _node_text(name_node, source_bytes)
        if not name:
            return None
        qualified = f"{class_name}.{name}"
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        return _ParsedFunction(
            function_id=f"{file.path}:{qualified}:{start_line}",
            name=name,
            qualified_name=qualified,
            class_id=class_id,
            class_name=class_name,
            start_line=start_line,
            end_line=end_line,
        )

    def _build_function(
        self, node, source_bytes: bytes, file: DiscoveredFile
    ) -> _ParsedFunction | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = _node_text(name_node, source_bytes)
        if not name:
            return None
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        return _ParsedFunction(
            function_id=f"{file.path}:{name}:{start_line}",
            name=name,
            qualified_name=name,
            class_id=None,
            class_name=None,
            start_line=start_line,
            end_line=end_line,
        )

    def _extract_calls(self, node, parsed_fn: _ParsedFunction, source_bytes: bytes) -> list[_ParsedCall]:
        body = node.child_by_field_name("body")
        if body is None:
            return []
        collected: list[_ParsedCall] = []
        self._collect(body, parsed_fn, source_bytes, collected)
        return collected

    def _collect(self, node, parsed_fn: _ParsedFunction, source_bytes: bytes, out: list[_ParsedCall]) -> None:
        if node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node is not None:
                callee = self._callee(fn_node, source_bytes)
                if callee is not None:
                    name, qualified = callee
                    out.append(
                        _ParsedCall(
                            caller_function_id=parsed_fn.function_id,
                            callee_name=name,
                            callee_qualified_name=qualified,
                            caller_class_id=parsed_fn.class_id,
                            line_number=node.start_point[0] + 1,
                            evidence=_node_text(node, source_bytes) or name,
                        )
                    )
        for child in node.named_children:
            self._collect(child, parsed_fn, source_bytes, out)

    def _callee(self, node, source_bytes: bytes) -> tuple[str, str | None] | None:
        if node.type == "identifier":
            text = _node_text(node, source_bytes)
            return (text, None) if text else None
        if node.type == "member_expression":
            prop = node.child_by_field_name("property")
            obj = node.child_by_field_name("object")
            if prop is None:
                return None
            name = _node_text(prop, source_bytes)
            if not name:
                return None
            if obj is not None:
                owner = _node_text(obj, source_bytes)
                if owner:
                    return name, f"{owner}.{name}"
            return name, None
        return None


_GO_LANGUAGE = Language(_tsgo.language())
_JAVA_LANGUAGE = Language(_tsjava.language())
_JS_LANGUAGE = Language(_tsjs.language())
_TS_LANGUAGE = Language(_tsts.language_tsx())


_PARSER_REGISTRY: dict[str, LanguageParser] = {
    "python": PythonParser(),
    "c": _TreeSitterCLikeParser(language="c", ts_language=_C_LANGUAGE),
    "cpp": _TreeSitterCLikeParser(language="cpp", ts_language=_CPP_LANGUAGE),
    "go": _GoParser(_GO_LANGUAGE),
    "java": _JavaParser(_JAVA_LANGUAGE),
    "javascript": _JsLikeParser(_JS_LANGUAGE),
    "typescript": _JsLikeParser(_TS_LANGUAGE),
}


def get_parser(language: str) -> LanguageParser | None:
    return _PARSER_REGISTRY.get(language)


__all__ = [
    "LanguageParser",
    "PythonParser",
    "_ParsedCall",
    "_ParsedClass",
    "_ParsedClassMember",
    "_ParsedFile",
    "_ParsedFunction",
    "get_parser",
]
