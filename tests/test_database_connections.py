"""Check lock recovery, busy-owner handling and graceful notebook pauses."""
import ast
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import duckdb
import pandas as pd

from firmable import database

ROOT = Path(__file__).resolve().parents[1]


class DatabaseConnectionTests(unittest.TestCase):
    def test_external_writer_lock_returns_status_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.duckdb'
            connection = database.connect_database(path)
            connection.execute('CREATE TABLE saved AS SELECT 42 AS value')
            database.close_database(connection)
            writer = subprocess.Popen([
                sys.executable, '-c',
                'import duckdb,sys; c=duckdb.connect(sys.argv[1]); print("ready",flush=True); sys.stdin.readline(); c.close()',
                str(path),
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(writer.stdout.readline().strip(), 'ready')
                messages = []
                self.assertIsNone(database.connect_database(path, messages.append))
                self.assertTrue(any('still in use' in text for text in messages))
            finally:
                writer.communicate(input='\n', timeout=5)
            connection = database.connect_database(path)
            try:
                self.assertEqual(connection.execute('SELECT value FROM saved').fetchone(), (42,))
            finally:
                database.close_database(connection)

    def test_lock_recovery_retries_once(self):
        error = duckdb.IOException('Conflicting lock held by PID 999999')
        connection = MagicMock()
        with patch.object(database.duckdb, 'connect', side_effect=[error, connection]) as connect:
            with patch.object(database, '_release_idle_notebook', return_value=True) as release:
                self.assertIs(database.connect_database('/tmp/fixture.duckdb'), connection)
                self.assertEqual(connect.call_count, 2)
                release.assert_called_once()

    def test_non_lock_failure_is_reported_without_recovery_or_traceback(self):
        messages = []
        with patch.object(database.duckdb, 'connect', side_effect=duckdb.IOException('not a valid database')):
            with patch.object(database, '_release_idle_notebook') as release:
                self.assertIsNone(database.connect_database('/tmp/fixture.duckdb', messages.append))
                release.assert_not_called()
        self.assertIn('not a valid database', messages[0])

    def test_close_is_safe_after_failure(self):
        connection = MagicMock()
        connection.close.side_effect = duckdb.IOException('already closed')
        database.close_database(connection)
        database.close_database(None)

    def kernel_fixture(self, directory):
        connection_file = Path(directory) / 'kernel-fixture.json'
        connection_file.write_text('{}')
        process = MagicMock(stdout=f'python -m ipykernel_launcher --f={connection_file}')
        client = MagicMock()
        client.execute.return_value = 'request'
        return process, client

    def test_busy_kernel_is_never_sent_close_code(self):
        with tempfile.TemporaryDirectory() as directory:
            process, client = self.kernel_fixture(directory)
            client.wait_for_ready.side_effect = RuntimeError('kernel is busy')
            with patch.object(database.subprocess, 'run', return_value=process):
                with patch('jupyter_client.BlockingKernelClient', return_value=client):
                    self.assertFalse(database._release_idle_notebook(Path(directory) / 'fixture.duckdb', 'PID 999999', lambda text: None))
            client.execute.assert_not_called()
            client.stop_channels.assert_called_once()

    def test_pending_review_keeps_connection_and_target_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            process, client = self.kernel_fixture(directory)
            client.get_iopub_msg.side_effect = [
                {'parent_header': {'msg_id': 'request'}, 'header': {'msg_type': 'stream'}, 'content': {'text': 'FIRMABLE_WORKFLOW_PENDING'}},
                {'parent_header': {'msg_id': 'request'}, 'header': {'msg_type': 'status'}, 'content': {'execution_state': 'idle'}},
            ]
            messages = []
            path = Path(directory) / 'fixture.duckdb'
            with patch.object(database.subprocess, 'run', return_value=process):
                with patch('jupyter_client.BlockingKernelClient', return_value=client):
                    self.assertFalse(database._release_idle_notebook(path, 'PID 999999', messages.append))
            code = client.execute.call_args.args[0]
            self.assertIn(str(path.resolve()), code)
            self.assertIn('PRAGMA database_list', code)
            self.assertIn('task.interrupts', code)
            self.assertIn('pending workflow', messages[0])
            self.assertNotIn('shutdown', code)
            self.assertNotIn('kill(', code)

    def test_verified_idle_kernel_release_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            process, client = self.kernel_fixture(directory)
            client.get_iopub_msg.side_effect = [
                {'parent_header': {'msg_id': 'request'}, 'header': {'msg_type': 'stream'}, 'content': {'text': 'FIRMABLE_CONNECTION_RELEASED'}},
                {'parent_header': {'msg_id': 'request'}, 'header': {'msg_type': 'status'}, 'content': {'execution_state': 'idle'}},
            ]
            messages = []
            with patch.object(database.subprocess, 'run', return_value=process):
                with patch('jupyter_client.BlockingKernelClient', return_value=client):
                    self.assertTrue(database._release_idle_notebook(Path(directory) / 'fixture.duckdb', 'PID 999999', messages.append))
            self.assertIn('released an idle notebook connection', messages[0])

    def test_parts3_and4_pause_without_missing_variable_errors_or_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ['03_entity_resolution', '04_entity_profiles']:
                namespace = {'__name__': '__main__'}
                notebook = json.loads((ROOT / f'notebooks/{name}.ipynb').read_text())
                logs = StringIO()
                initialized = False
                with redirect_stdout(logs):
                    for cell in notebook['cells']:
                        if cell['cell_type'] != 'code':
                            continue
                        source = ''.join(cell['source'])
                        exec(source, namespace)
                        if not initialized:
                            initialized = True
                            namespace['DB_PATH'] = Path(directory) / 'missing.duckdb'
                            namespace['GOLD_COPY_DIR'] = Path(directory) / 'no_gold'
                            namespace['PART3_DIR'] = Path(directory) / 'part3'
                            namespace['PART4_DIR'] = Path(directory) / 'part4'
                            namespace['connect_database'] = lambda *args, **kwargs: None
                self.assertIn('paused', logs.getvalue().lower())
                self.assertFalse(namespace.get('DATABASE_READY', namespace.get('PART4_READY')))
                self.assertFalse((Path(directory) / 'part3/entities_pool.parquet').exists())
                self.assertFalse((Path(directory) / 'part4/firmable_entities.parquet').exists())

    def test_part4_file_outputs_survive_busy_database(self):
        with tempfile.TemporaryDirectory() as directory:
            notebook = json.loads((ROOT / 'notebooks/04_entity_profiles.ipynb').read_text())
            namespace = dict(globals(), PART4_DIR=Path(directory), DB_PATH=Path(directory) / 'busy.duckdb',
                             connect_database=lambda *args: None, close_database=database.close_database)
            for cell in notebook['cells']:
                if cell['cell_type'] != 'code':
                    continue
                tree = ast.parse(''.join(cell['source']))
                for node in tree.body:
                    if isinstance(node, ast.FunctionDef) and node.name == 'save_outputs':
                        exec(compile(ast.Module(body=[node], type_ignores=[]), '<save outputs>', 'exec'), namespace)
            metrics = {}
            with redirect_stdout(StringIO()):
                saved = namespace['save_outputs']({'firmable_entities': pd.DataFrame({'entity_id': ['fixture']})}, metrics)
            self.assertFalse(saved)
            self.assertFalse(metrics['database_written'])
            self.assertTrue((Path(directory) / 'firmable_entities.parquet').exists())
            self.assertTrue((Path(directory) / 'firmable_entities.csv').exists())


if __name__ == '__main__':
    unittest.main()
