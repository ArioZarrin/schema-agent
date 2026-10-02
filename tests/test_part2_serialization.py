"""Exercise Part 2 JSON and real checkpoint round trips without API calls."""
import ast
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Literal, TypedDict

import duckdb
import numpy as np
import pandas as pd
import yaml
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from firmable.database import connect_database, close_database
from firmable.readers import prepare_csv_input, read_csv_python

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = json.loads((ROOT / 'notebooks/02_schema_inference.ipynb').read_text())


def load_definitions():
    namespace = dict(globals(), tool=lambda function: function, EXTRACT_ROWS=2)
    constants = {'CTX', 'ACTIVE_THREAD_ID', 'BLOCKED_SQL', 'UNSAFE_EXPRESSION'}
    for cell in NOTEBOOK['cells']:
        if cell['cell_type'] != 'code':
            continue
        tree = ast.parse(''.join(cell['source']))
        declarations = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                        or (isinstance(node, ast.Assign) and any(
                            isinstance(target, ast.Name) and target.id in constants for target in node.targets))]
        exec(compile(ast.Module(body=declarations, type_ignores=[]), '<part2 definitions>', 'exec'), namespace)
    return namespace


def assert_native(test, value):
    if isinstance(value, dict):
        for key, child in value.items():
            test.assertIs(type(key), str)
            assert_native(test, child)
    elif isinstance(value, list):
        for child in value:
            assert_native(test, child)
    else:
        test.assertIn(type(value), (type(None), str, int, float, bool))


class Part2SerializationTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_definitions()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        for name in ['PROFILE_DIR', 'REVIEW_DIR', 'CONFIG_DIR', 'REJECTED_DIR', 'STATE_DIR', 'METRICS_DIR', 'GOLD_COPY_DIR']:
            self.ns[name] = root / name.lower()
            self.ns[name].mkdir()
        self.ns['duckdb_lock'] = threading.Lock()
        self.ns['DB_PATH'] = root / 'fixture.duckdb'
        self.connection = duckdb.connect(str(self.ns['DB_PATH']))
        self.addCleanup(self.connection.close)
        self.ns['con'] = self.connection
        self.connection.execute('''
            CREATE TABLE fixture (
                __firmable_row_id BIGINT, record_id VARCHAR, name VARCHAR,
                registered DATE, amount DOUBLE, enabled BOOLEAN
            );
            INSERT INTO fixture VALUES
                (1, 'a', 'Alpha', DATE '2026-10-01', 12.5, TRUE),
                (2, 'b', 'Beta', NULL, NULL, NULL);
            CREATE VIEW source_data AS SELECT * FROM fixture;
            CREATE VIEW validation_sample AS SELECT * FROM fixture;
        ''')
        self.ns['CTX'].update({
            'dataset': {'slug': 'fixture', 'dataset_id': 'dataset', 'title': 'Fixture'},
            'metadata': {'license_title': 'CC-BY'},
            'resources': [{'id': 'resource', 'name': 'Fixture CSV', 'format': 'CSV'}],
            'resource_id': 'resource', 'source_table': 'fixture', 'source_rows': np.int64(2),
            'validation_rows': np.int64(2), 'source_columns': ['record_id', 'name', 'registered', 'amount', 'enabled'],
            'reader': self.ns['ReaderConfig'](format='csv'),
        })
        ontology = yaml.safe_load((ROOT / 'resources/firmable_ontology.yaml').read_text())
        self.ns['CANONICAL_FIELDS'] = [f'{section}.{field}' for section in ['entity', 'address', 'observation'] for field in ontology[section]]
        self.ns['FIELD_SPECS'] = {f'{section}.{field}': spec for section in ['entity', 'address', 'observation'] for field, spec in ontology[section].items()}
        self.ns['EXTRACTOR_VERSION'] = 'serialization-test'
        self.ns['VALIDATION_ROWS'] = 1000
        self.ns['HOLDOUT_SEED'] = 20260929
        self.ns['validate_config'] = lambda config: {'valid': True, 'errors': [], 'warnings': [], 'validation_rows': np.int64(2)}
        self.candidate = self.ns['MappingConfig'].model_validate({
            'source': {
                'dataset_id': 'dataset', 'dataset_title': 'Fixture', 'resource_id': 'resource',
                'resource_name': 'Fixture CSV', 'resource_url': 'https://example.invalid/fixture.csv',
                'resource_format': 'CSV', 'licence': 'CC-BY',
            },
            'reader': {'format': 'csv'}, 'source_reliability': .95,
            'source_reliability_reason': 'Test fixture',
            'mappings': [
                {'source_fields': [source], 'target_field': target,
                 'transformation': {'type': 'direct', 'expression': None, 'value': None},
                 'confidence': .9, 'derivation_level': 'L0', 'reason': 'Test fixture'}
                for source, target in [('name', 'entity.legal_name'), ('registered', 'entity.date_registered'), ('record_id', 'observation.source_record_id')]
            ],
            'runtime_fields': {}, 'unmapped_fields': [],
        })

    def assert_json_safe(self, value):
        assert_native(self, value)
        return json.loads(json.dumps(value, allow_nan=False))

    def test_nested_values_keep_numbers_booleans_nulls_and_iso_timestamps(self):
        payload = {
            np.int64(7): (np.int64(9), np.float32(.5), np.bool_(True)),
            'missing': [None, pd.NA, pd.NaT, np.nan, np.datetime64('NaT', 'ns')],
            'timestamp': pd.Timestamp('2026-10-02T10:00:00+11:00'),
        }
        result = self.ns['json_safe'](payload)
        self.assertEqual(result['7'], [9, .5, True])
        self.assertEqual(result['missing'], [None] * 5)
        self.assertEqual(result['timestamp'], '2026-10-02T10:00:00+11:00')
        self.assert_json_safe(result)

    def test_arrays_numpy_datetimes_and_timedeltas(self):
        result = self.ns['json_safe']({
            'array': np.array([[1., np.nan], [2., np.inf]]),
            'date': np.datetime64('2026-10-02'),
            'datetime': np.datetime64('2026-10-02T10:20:30'),
            'duration': pd.Timedelta(days=1, seconds=2),
            'native_date': date(2026, 10, 2),
            'negative_infinity': float('-inf'),
        })
        self.assertEqual(result['array'], [[1., None], [2., None]])
        self.assertEqual(result['date'], '2026-10-02')
        self.assertEqual(result['datetime'], '2026-10-02T10:20:30')
        self.assertEqual(result['duration'], 'P1DT0H0M2S')
        self.assertEqual(result['native_date'], '2026-10-02')
        self.assertIsNone(result['negative_infinity'])
        self.assert_json_safe(result)

    def test_find_dataset_normalizes_pandas_shortlist_metadata_in_context(self):
        self.ns['shortlist'] = pd.DataFrame({
            'dataset_id': ['dataset'], 'title': ['Fixture'],
            'publishing_organisation': [np.nan], 'confidence': [np.float64(.8)],
        })
        found = json.loads(self.ns['find_dataset']('Fixture'))
        self.assertIsNone(found['publisher'])
        self.assertEqual(found['part1_confidence'], .8)
        self.assert_json_safe(self.ns['CTX']['dataset'])

    def test_profile_file_and_context_keep_native_samples(self):
        result = json.loads(self.ns['profile_source']())
        sample = result['sample_rows']
        self.assertEqual(sample[0]['registered'], '2026-10-01T00:00:00')
        self.assertIsNone(sample[1]['registered'])
        self.assertIsNone(sample[1]['amount'])
        self.assertIs(sample[0]['enabled'], True)
        self.assertEqual(sample[0]['amount'], 12.5)
        self.assert_json_safe(self.ns['CTX']['profile'])
        stored = json.loads((self.ns['PROFILE_DIR'] / 'fixture.profile.json').read_text())
        self.assertEqual(stored, result)

    def test_query_response_uses_same_helper_including_duckdb_lists(self):
        response = self.ns['query_source']('SELECT registered, amount, enabled, [record_id] AS tags FROM source_data')
        result = json.loads(response)
        self.assertEqual(result[0]['tags'], ['a'])
        self.assertEqual(result[0]['registered'], '2026-10-01T00:00:00')
        self.assertIsNone(result[1]['registered'])
        self.assertIsNone(result[1]['enabled'])
        self.assert_json_safe(result)

    def test_review_payload_keeps_both_samples_json_safe(self):
        review = self.ns['make_review'](
            self.candidate, {'valid': np.bool_(True), 'rows': np.int64(2)},
            {'tokens': np.int64(4), 'cost': np.nan},
        )
        self.assertEqual(review['source_sample'][0]['registered'], '2026-10-01T00:00:00')
        self.assertEqual(review['sample_results'][0]['entity_date_registered'], '2026-10-01T00:00:00')
        self.assertEqual(len(review['sample_results']), 1)
        self.assertIsNone(review['sample_results'][0]['entity_website'])
        self.assertIn('entity_website', review['gold_columns_without_sample'])
        self.assertIsNone(review['metrics']['cost'])
        self.assert_json_safe(review)

    def test_extraction_sample_is_safe_without_changing_gold_date_types(self):
        result = self.ns['extract_gold'](self.candidate, limit=2)
        self.assertEqual(result['rows'], 2)
        self.assertEqual(result['sample'][0]['entity_date_registered'], '2026-10-01T00:00:00')
        self.assertIsNone(result['sample'][1]['entity_date_registered'])
        self.assert_json_safe(result)
        date_type = self.connection.execute('DESCRIBE gold_fixture').df().set_index('column_name').loc['entity_date_registered', 'column_type']
        self.assertEqual(date_type, 'DATE')

    def test_state_file_and_yaml_use_native_values(self):
        self.ns['save_state']('human_review', {'sample': {'n': np.int64(3), 'date': pd.NaT, 'flag': np.bool_(False)}})
        result = json.loads(self.ns['state_path']().read_text())
        self.assertEqual(result['sample'], {'n': 3, 'date': None, 'flag': False})
        path = Path(self.directory.name) / 'sample.yaml'
        self.ns['save_yaml'](path, {'n': np.int64(3), 'date': pd.NaT})
        self.assertEqual(yaml.safe_load(path.read_text()), {'n': 3, 'date': None})

    def exercise_checkpoint(self, saver):
        graph = StateGraph(self.ns['WorkflowState'])
        graph.add_node('review', self.ns['review_node'])
        graph.add_node('approve', self.ns['approve_node'])
        graph.add_edge(START, 'review')
        graph.add_conditional_edges('review', self.ns['route_review'], {'approve': 'approve'})
        graph.add_edge('approve', END)
        workflow = graph.compile(checkpointer=saver)
        self.ns['workflow'] = workflow
        seen = []
        def decide(payload):
            self.assert_json_safe(payload)
            self.assertIsNone(self.ns['con'])
            # A separate process/connection can use the database while review is open.
            with duckdb.connect(str(self.ns['DB_PATH'])) as observer:
                self.assertEqual(observer.execute('SELECT count(*) FROM fixture').fetchone()[0], 2)
            seen.append(payload)
            return {'action': np.str_('approve'), 'at': pd.Timestamp('2026-10-02'), 'checked': np.bool_(True)}
        self.ns['_ask_workflow_decision'] = decide
        config = {'configurable': {'thread_id': 'serialization-fixture'}}
        with redirect_stdout(StringIO()):
            result = self.ns['_run_workflow']({
                'candidate': self.candidate.model_dump(mode='json'),
                'validation': {'valid': np.bool_(True)},
                'metrics': {'tokens': np.int64(5), 'cost': np.nan},
            }, config)
        self.assertEqual(len(seen), 1)
        self.assertEqual(result['final']['status'], 'approved')
        self.assert_json_safe(result)
        for snapshot in workflow.get_state_history(config):
            self.assert_json_safe(snapshot.values)
            for task in snapshot.tasks:
                for pending in task.interrupts:
                    self.assert_json_safe(pending.value)
        self.assertEqual(workflow.get_state(config).values['final']['rows'], 2)
        metrics = json.loads(self.ns['metrics_path']().read_text())
        self.assertEqual(metrics['tokens'], 5)
        self.assertIsNone(metrics['cost'])
        return config

    def test_memory_checkpoint_review_resume_and_approve(self):
        self.exercise_checkpoint(InMemorySaver())

    def test_sqlite_checkpoint_review_resume_approve_and_reopen(self):
        database = str(Path(self.directory.name) / 'checkpoints.sqlite')
        with SqliteSaver.from_conn_string(database) as saver:
            config = self.exercise_checkpoint(saver)
        with SqliteSaver.from_conn_string(database) as saver:
            saved = saver.get_tuple(config)
            final = saved.checkpoint['channel_values']['final']
            self.assert_json_safe(final)
            self.assertIsNone(final['sample'][1]['entity_date_registered'])
            self.assertEqual(final['status'], 'approved')

    def test_all_json_writes_are_strict_and_use_the_shared_helper(self):
        helpers = 0
        writes = 0
        for cell in NOTEBOOK['cells']:
            if cell['cell_type'] != 'code':
                continue
            tree = ast.parse(''.join(cell['source']))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == 'json_safe':
                    helpers += 1
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'json' and node.func.attr == 'dumps':
                    writes += 1
                    self.assertIsInstance(node.args[0], ast.Call)
                    self.assertEqual(node.args[0].func.id, 'json_safe')
                    self.assertNotIn('default', [keyword.arg for keyword in node.keywords])
                    strict = next(keyword for keyword in node.keywords if keyword.arg == 'allow_nan')
                    self.assertIs(strict.value.value, False)
        self.assertEqual(helpers, 1)
        self.assertGreater(writes, 20)

    def test_actual_cbs_csv_loads_with_windows_encoding_and_preserves_raw_file(self):
        source = ROOT / 'outputs/part2/raw/annual_report_cbs/2017-18-consumer-and-business-services-building-work-contractors-act_data.csv'
        if not source.exists():
            self.skipTest('Local CBS raw file is unavailable')
        original = source.read_bytes()
        self.connection.execute('DROP VIEW source_data; DROP VIEW validation_sample')
        self.ns['CTX']['source_table'] = None
        result = self.ns['_load_source_file'](str(source), {'header': True, 'skip': 2})
        self.assertEqual(result['reader']['encoding'], 'cp1252')
        self.assertEqual(result['rows'], 6)
        self.assertEqual(source.read_bytes(), original)
        stored = self.connection.execute('SELECT * FROM src_fixture').fetchall()
        self.assertEqual(stored[0][1], '4943')
        self.assertEqual(len(stored), 6)

    def test_python_parser_fallback_retains_ragged_rows(self):
        source = Path(self.directory.name) / 'ragged.csv'
        source.write_text('name,value\nAlpha,1,extra\nBeta\n', encoding='utf-8')
        frame, info = read_csv_python(source, delimiter=',')
        self.assertEqual(len(frame), 2)
        self.assertEqual(frame.iloc[0].tolist(), ['Alpha', '1', 'extra'])
        self.assertEqual(frame.iloc[1].tolist(), ['Beta', '', ''])
        self.assertEqual(info['extra_columns'], 1)

    def test_python_fallback_is_used_after_duckdb_parser_rejects_input(self):
        source = Path(self.directory.name) / 'fallback.csv'
        source.write_text('name,value\nAlpha,1,extra\nBeta\n', encoding='utf-8')
        connection = self.connection
        class ParserFailure:
            def execute(self, sql, *arguments):
                if 'read_csv_auto' in sql:
                    raise duckdb.InvalidInputException('CSV parser rejected the file')
                return connection.execute(sql, *arguments)
            def __getattr__(self, name):
                return getattr(connection, name)
        self.connection.execute('DROP VIEW source_data; DROP VIEW validation_sample')
        self.ns['con'] = ParserFailure()
        result = self.ns['_load_source_file'](str(source), {'delimiter': ','})
        self.assertEqual(result['rows'], 2)
        self.assertEqual(result['reader_recovery']['parser'], 'python_csv')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM src_fixture').fetchone()[0], 2)

    def test_utf16_preparation_preserves_unicode_and_original_bytes(self):
        source = Path(self.directory.name) / 'utf16.csv'
        original = 'name,value\r\nMāori,1\r\n'.encode('utf-16')
        source.write_bytes(original)
        result = prepare_csv_input(source, Path(self.directory.name) / 'cache')
        self.assertEqual(result['encoding'], 'utf-16')
        self.assertEqual(Path(result['file_path']).read_text(encoding='utf-8'), 'name,value\nMāori,1\n')
        self.assertEqual(source.read_bytes(), original)

    def test_workflow_failure_returns_status_and_closes_connection(self):
        class FailedWorkflow:
            def invoke(self, *args, **kwargs):
                raise duckdb.IOException('connection unavailable')
        self.ns['workflow'] = FailedWorkflow()
        with redirect_stdout(StringIO()):
            result = self.ns['_run_workflow']({}, {})
        self.assertEqual(result['final']['status'], 'failed')
        self.assertIsNone(self.ns['con'])

    def add_website_mapping(self):
        self.connection.execute('ALTER TABLE fixture ADD COLUMN website VARCHAR')
        self.ns['CTX']['source_columns'].append('website')
        mapping = self.ns['FieldMapping'].model_validate({
            'source_fields': ['website'], 'target_field': 'entity.website',
            'transformation': {'type': 'direct', 'expression': None, 'value': None},
            'confidence': .9, 'derivation_level': 'L0', 'reason': 'Fixture',
        })
        return self.candidate.model_copy(update={'mappings': [*self.candidate.mappings, mapping]})

    def test_gold_preview_finds_complete_record_beyond_first_eight_rows(self):
        candidate = self.add_website_mapping()
        self.connection.execute("INSERT INTO fixture SELECT 100+i, 'middle-'||i, 'Middle', NULL, NULL, NULL, NULL FROM range(20) t(i)")
        self.connection.execute("INSERT INTO fixture VALUES (999, 'last', 'Complete', DATE '2026-10-02', NULL, NULL, 'https://example.com')")
        review = self.ns['make_review'](candidate, {'valid': True}, {})
        self.assertEqual(len(review['sample_results']), 1)
        self.assertEqual(review['sample_results'][0]['observation_source_record_id'], 'last')
        self.assertEqual(review['sample_results'][0]['entity_website'], 'https://example.com')
        self.assertEqual(review['source_sample'][0]['record_id'], 'last')
        expected = [field.replace('.', '_') for field in self.ns['CANONICAL_FIELDS']]
        self.assertTrue(set(expected).issubset(review['gold_columns']))

    def test_gold_preview_adds_complementary_records_and_displays_all_columns(self):
        candidate = self.add_website_mapping()
        self.connection.execute("INSERT INTO fixture VALUES (999, 'last', 'Website only', NULL, NULL, NULL, 'https://example.com')")
        review = self.ns['make_review'](candidate, {'valid': True}, {})
        self.assertEqual(len(review['sample_results']), 2)
        self.assertTrue(any(row['entity_date_registered'] for row in review['sample_results']))
        self.assertTrue(any(row['entity_website'] for row in review['sample_results']))
        shown = []
        def display(frame):
            shown.append(frame)
            self.assertIsNone(pd.get_option('display.max_columns'))
        self.ns['display'] = display
        with redirect_stdout(StringIO()):
            self.ns['display_review'](review)
        self.assertEqual(shown[-1].columns.tolist(), review['gold_columns'])
        self.assertEqual(len(shown[-1]), 2)


if __name__ == '__main__':
    unittest.main()
