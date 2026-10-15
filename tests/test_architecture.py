"""Architectural boundaries and real adapter error translation regressions."""
import ast
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "src/agent-framework-agent-with-local-tools-responses"
sys.path.insert(0, str(SOURCE))
from learning_coach.db.memory import LearningStore
from learning_coach.service.ports import StorageError
from learning_coach.adapters.visuals import render_visual
from learning_coach.db.artifacts import ArtifactStore


class ArchitectureTests(unittest.TestCase):
    def test_inner_modules_do_not_import_adapters_or_sdk(self):
        package = SOURCE / "learning_coach"
        allowed = {
            "domain": {"domain"},
            "service": {"service", "domain"},
            "api": {"api", "service", "domain"},
            "db": {"db", "service", "domain"},
            "adapters": {"adapters", "service", "domain"},
        }
        for layer, dependencies in allowed.items():
            for path in (package / layer).glob("*.py"):
                for node in ast.walk(ast.parse(path.read_text())):
                    imports = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                               else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                    for target in imports:
                        if target.startswith("learning_coach."):
                            self.assertIn(target.split('.')[1], dependencies, f"{path.name} imports {target}")
                        if layer in {"domain", "service"}:
                            self.assertNotIn(target.split('.')[0], {"sqlite3", "agent_framework", "azure", "gradio"})

    def test_package_root_contains_only_bootstrap(self):
        self.assertEqual({p.name for p in (SOURCE / "learning_coach").glob("*.py")},
                         {"__init__.py", "server.py"})

    def test_pipeline_import_does_not_load_sdk_or_database_adapter(self):
        script = '''
import sys
from learning_coach.service.pipeline import LearningPipeline
from learning_coach.service.research import ResearchService
assert not any(name == "agent_framework" or name.startswith("agent_framework.") for name in sys.modules)
assert "sqlite3" not in sys.modules
assert "learning_coach.db.memory" not in sys.modules
assert "learning_coach.adapters.visuals" not in sys.modules
'''
        env = dict(os.environ, PYTHONPATH=str(SOURCE), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sqlite_error_is_translated_at_memory_boundary(self):
        store = LearningStore(":memory:")
        store.close()
        with self.assertRaisesRegex(StorageError, "^storage_operation_failed$"):
            store.snapshot("s")

    def test_sqlite_error_is_translated_at_artifact_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ArtifactStore(folder, renderer=render_visual)
            with patch("learning_coach.db.artifacts.sqlite3.connect", side_effect=sqlite3.OperationalError("PRIVATE_PATH")):
                with self.assertRaisesRegex(StorageError, "^storage_operation_failed$"):
                    store.delete_session("s")

    def test_factory_wires_overrides_without_credentials(self):
        from learning_coach.server import create_pipeline
        from learning_coach.adapters.runner import BUDGET as old_budget, StageFailure as old_failure
        from learning_coach.service.execution import BUDGET, StageFailure
        self.assertIs(old_budget, BUDGET)
        self.assertIs(old_failure, StageFailure)
        client, provider = object(), object()
        async def image_generator(prompt):
            return b"image"
        with tempfile.TemporaryDirectory() as folder:
            with patch("learning_coach.server.create_agents", return_value=dict.fromkeys(
                    ("coordinator", "researcher", "verifier", "notes", "visualizer"), object())) as agents, \
                 patch("learning_coach.server.DefaultAzureCredential", side_effect=AssertionError("credentials")):
                pipeline = create_pipeline(client=client, data_dir=folder,
                                           image_generator=image_generator, search_provider=provider)
            self.assertIs(pipeline.image_generator, image_generator)
            self.assertIs(pipeline.research.provider, provider)
            self.assertIs(agents.call_args.args[0], client)
            pipeline.store.close()

    def test_both_entrypoints_use_shared_factory(self):
        root = SOURCE.parents[1]
        for path in (SOURCE / "main.py", root / "space/app.py"):
            tree = ast.parse(path.read_text())
            self.assertTrue(any(isinstance(n, ast.ImportFrom) and n.module == "learning_coach.server"
                                for n in ast.walk(tree)))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"LearningStore", "ArtifactStore", "StructuredRunner", "ResearchService"})
