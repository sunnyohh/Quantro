import ast
from pathlib import Path
import unittest


class ArchitectureTests(unittest.TestCase):
    def test_domain_and_strategies_have_no_infrastructure_or_io_imports(self):
        root = Path(__file__).resolve().parents[2] / "src" / "quantro"
        forbidden = {"sqlalchemy", "psycopg", "fastapi", "requests", "httpx", "win32com",
                     "socket", "os", "pathlib", "random", "time", "subprocess"}
        for directory in (root / "domain", root / "strategies"):
            for path in directory.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports = [item.name for item in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        imports = [node.module or ""]
                    else:
                        continue
                    for module in imports:
                        self.assertNotIn(module.split(".")[0], forbidden, str(path))
                        self.assertFalse(module.startswith(("quantro.infrastructure", "quantro.api")), str(path))
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse(node.module == "datetime" and any(a.name == "datetime" for a in node.names)
                                         and directory.name == "strategies", str(path))
