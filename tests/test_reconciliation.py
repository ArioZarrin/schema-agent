"""Verify Part 4 scoring, conflict abstention and provenance without running the pipeline."""
import ast
import hashlib
import json
import math
import re
import tempfile
import unittest
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
import duckdb
import yaml
from firmable.database import connect_database, close_database

ROOT = Path(__file__).resolve().parents[1]
ontology = yaml.safe_load((ROOT / 'resources/firmable_ontology.yaml').read_text())
NS = dict(globals())
CONSTANTS = {
    'FIELD_MAP', 'PROFILE_FIELDS', 'DECISION_COLUMNS', 'CLAIM_COLUMNS',
    'SCORE_COLUMNS', 'CONFLICT_COLUMNS', 'VALUE_COLUMNS',
}
notebook = json.loads((ROOT / 'notebooks/04_entity_profiles.ipynb').read_text())
for cell in notebook['cells']:
    if cell['cell_type'] != 'code':
        continue
    tree = ast.parse(''.join(cell['source']))
    declarations = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    or (isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id in CONSTANTS for target in node.targets))]
    exec(compile(ast.Module(body=declarations, type_ignores=[]), '<part4 definitions>', 'exec'), NS)


def observation(record, value, reliability=1.0, mapping=1.0):
    return {
        'gold_table': 'gold_fixture', 'observation_source_id': 'source',
        'observation_source_record_id': record, 'entity_legal_name': value,
        'confidence_source_reliability': reliability,
        'field_confidence': json.dumps({'entity.legal_name': mapping}),
        'derivation_level': json.dumps({'entity.legal_name': 'L0'}),
        'observation_observed_at': '2026-10-01', 'observation_licence': 'CC-BY',
    }


def run(rows, links=None, pool=None):
    pool = pd.DataFrame({'entity_id': ['entity'], 'abn': [None], 'acn': [None]}) if pool is None else pool
    if links is None:
        links = pd.DataFrame([
            {'link_id': r['observation_source_record_id'], 'source_id': 'source',
             'source_record_id': r['observation_source_record_id'], 'entity_id': 'entity',
             'link_confidence': r.get('test_link_confidence', 1.0),
             'match_method': 'fixture', 'match_evidence': '{}'} for r in rows
        ]).drop_duplicates('link_id')
    gold = pd.DataFrame(rows).drop(columns='test_link_confidence', errors='ignore')
    linked = NS['attach_links'](gold, pool, links)
    claims = NS['build_claims'](linked)
    return NS['reconcile_fields'](pool, claims)


class ReconciliationTests(unittest.TestCase):
    def test_single_claim_uses_all_three_components_and_preserves_provenance(self):
        row = observation('a', 'Alpha Ltd', .8, .7)
        row['test_link_confidence'] = .9
        outputs = run([row])
        profile = outputs['firmable_entities'].iloc[0]
        self.assertEqual(profile['legal_name'], 'ALPHA LTD')
        self.assertAlmostEqual(profile['legal_name_confidence'], .504)
        claim = outputs['entity_field_claims'].iloc[0]
        self.assertEqual(claim['raw_value'], 'Alpha Ltd')
        self.assertEqual(claim['source_record_id'], 'a')
        self.assertEqual(claim['observed_at'], '2026-10-01')
        self.assertEqual(claim['licence'], 'CC-BY')
        self.assertEqual(claim['derivation_level'], 'L0')
        self.assertEqual(claim['field_confidence'], .7)
        self.assertEqual(claim['link_confidence'], .9)

    def test_agreement_adds_scores_without_forcing_one(self):
        result = run([observation('a', 'Alpha Ltd.', .2), observation('b', ' ALPHA  LTD ', .3)])
        score = result['entity_field_scores'].iloc[0]
        self.assertEqual(score['status'], 'agreement')
        self.assertAlmostEqual(score['confidence'], .5)
        self.assertEqual(score['claim_count'], 2)
        self.assertTrue(result['entity_field_conflicts'].empty)

    def test_agreement_caps_profile_confidence_but_preserves_support(self):
        result = run([observation(str(i), 'Alpha', .9) for i in range(3)])
        score = result['entity_field_scores'].iloc[0]
        self.assertEqual(score['confidence'], 1.0)
        self.assertAlmostEqual(score['support_score'], 2.7)

    def test_conflict_shares_use_uncapped_support_and_profile_abstains(self):
        result = run([observation('a', 'Alpha'), observation('b', 'ALPHA'), observation('c', 'Beta')])
        profile = result['firmable_entities'].iloc[0]
        self.assertIsNone(profile['legal_name'])
        self.assertIsNone(profile['legal_name_confidence'])
        self.assertTrue(profile['has_conflict'])
        self.assertTrue(result['entity_field_scores'].empty)
        values = result['entity_conflict_values'].set_index('value')
        self.assertEqual(values.loc['ALPHA', 'support_score'], 2.0)
        self.assertAlmostEqual(values.loc['ALPHA', 'relative_score'], 2 / 3)
        self.assertAlmostEqual(values.loc['BETA', 'relative_score'], 1 / 3)
        self.assertEqual(len(result['entity_field_claims']), 3)
        self.assertEqual(result['entity_field_claims']['conflict_id'].nunique(), 1)
        conflict = result['entity_field_conflicts'].iloc[0]
        for column in NS['DECISION_COLUMNS']:
            self.assertIsNone(conflict[column])

    def test_three_alternatives_and_zero_support(self):
        result = run([observation(str(i), str(i), score) for i, score in enumerate([.2, .3, .5])])
        values = result['entity_conflict_values']
        self.assertEqual(values['alternative_id'].tolist(), ['k1', 'k2', 'k3'])
        self.assertAlmostEqual(values['relative_score'].sum(), 1.0)
        zero = run([observation('a', 'Alpha', 0), observation('b', 'Beta', 0)])
        self.assertTrue(zero['entity_conflict_values']['relative_score'].isna().all())
        self.assertEqual(zero['entity_field_conflicts'].iloc[0]['total_support'], 0)

    def test_duplicate_identical_gold_row_counts_once_but_differing_duplicate_stops(self):
        row = observation('a', 'Alpha', .3)
        result = run([row, row.copy()])
        self.assertEqual(len(result['entity_field_claims']), 1)
        self.assertAlmostEqual(result['firmable_entities'].iloc[0]['legal_name_confidence'], .3)
        with self.assertRaisesRegex(ValueError, 'Differing gold observations'):
            run([row, observation('a', 'Beta', .3)])

    def test_unlinked_observations_cannot_create_conflict(self):
        rows = [observation('a', 'Alpha'), observation('b', 'Beta')]
        links = pd.DataFrame([{
            'link_id': 'a', 'source_id': 'source', 'source_record_id': 'a', 'entity_id': 'entity',
            'link_confidence': 1.0, 'match_method': 'fixture', 'match_evidence': '{}',
        }])
        result = run(rows, links=links)
        self.assertEqual(len(result['entity_field_claims']), 1)
        self.assertTrue(result['entity_field_conflicts'].empty)

    def test_invalid_confidence_names_component_and_source(self):
        for reliability, mapping, link, component in [
            (None, 1, 1, 'R'), (1, None, 1, 'F'), (1, 1, 1.1, 'L'),
            (1, float('nan'), 1, 'F'), (True, 1, 1, 'R'),
        ]:
            row = observation('a', 'Alpha', reliability, mapping)
            row['test_link_confidence'] = link
            with self.assertRaisesRegex(ValueError, f'invalid {component}.*gold_fixture.*record a'):
                run([row])

    def test_identifiers_and_address_units_remain_distinct(self):
        normalize = NS['normalize_value']
        self.assertEqual(normalize('entity.acn', '004 085-616'), '004085616')
        self.assertEqual(normalize('address.postcode', '0800'), '0800')
        self.assertNotEqual(normalize('address.full', '1/23 King St'), normalize('address.full', '123 King St'))
        self.assertNotEqual(normalize('entity.legal_name', 'ALPHA PTY LTD'), normalize('entity.legal_name', 'ALPHA'))
        self.assertEqual(normalize('entity.website', 'HTTPS://WWW.Example.com/'), 'example.com')
        self.assertNotEqual(normalize('entity.website', 'example.com/A'), normalize('entity.website', 'example.com/a'))

    def test_identity_value_is_not_fabricated_abn_and_empty_entities_survive(self):
        pool = pd.DataFrame({'entity_id': ['53004085616', 'empty'], 'abn': [None, None], 'acn': ['004085616', None]})
        links = pd.DataFrame([{
            'link_id': 'a', 'source_id': 'source', 'source_record_id': 'a', 'entity_id': '53004085616',
            'link_confidence': 1., 'match_method': 'acn_derived_abn', 'match_evidence': '{}',
        }])
        result = run([observation('a', 'Alpha')], links=links, pool=pool)
        profiles = result['firmable_entities'].set_index('entity_id')
        self.assertIsNone(profiles.loc['53004085616', 'abn'])
        self.assertEqual(profiles.loc['empty', 'resolved_field_count'], 0)
        self.assertEqual(len(profiles), 2)

    def test_missing_or_dangling_links_fail_before_outputs(self):
        links = pd.DataFrame([{
            'link_id': 'missing', 'source_id': 'source', 'source_record_id': 'missing', 'entity_id': 'entity',
            'link_confidence': 1., 'match_method': 'fixture', 'match_evidence': '{}',
        }])
        with self.assertRaisesRegex(ValueError, 'no current gold observation'):
            run([observation('a', 'Alpha')], links=links)
        links['entity_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'outside entities_pool'):
            run([observation('a', 'Alpha')], links=links)

    def test_rerun_has_stable_claim_ids_conflict_ids_and_scores(self):
        rows = [observation('a', 'Alpha', .9), observation('b', 'Beta', .6)]
        first, second = run(rows), run(list(reversed(rows)))
        for table in first:
            columns = first[table].columns.tolist()
            left = first[table].sort_values(columns).reset_index(drop=True)
            right = second[table].sort_values(columns).reset_index(drop=True)
            pd.testing.assert_frame_equal(left, right)

    def test_empty_outputs_have_nullable_types_and_unknown_is_preserved(self):
        result = run([observation('a', None)])
        for frame in result.values():
            NS['output_types'](frame)
        self.assertEqual(result['firmable_entities'].iloc[0]['resolved_field_count'], 0)
        self.assertEqual(NS['normalize_value']('entity.status', 'unknown'), 'unknown')

    def test_conflicts_and_empty_reports_persist_with_null_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            NS['DB_PATH'] = Path(directory) / 'fixture.duckdb'
            NS['PART4_DIR'] = Path(directory)
            for names in [('Alpha', 'Beta'), ('Alpha', 'Alpha')]:
                outputs = {
                    table: NS['output_types'](frame)
                    for table, frame in run([
                        observation('a', names[0], .8), observation('b', names[1], .4)
                    ]).items()
                }
                NS['save_outputs'](outputs, {'fixture': True})
                with duckdb.connect(str(NS['DB_PATH'])) as connection:
                    decisions = connection.sql('SELECT ai_decision, data_analyser_decision, final_verdict FROM entity_field_conflicts').fetchall()
                    self.assertEqual(decisions, [(None, None, None)] if names[0] != names[1] else [])
                    for table, frame in outputs.items():
                        self.assertEqual(connection.sql(f'SELECT count(*) FROM {table}').fetchone()[0], len(frame))
                        self.assertEqual(len(pd.read_parquet(Path(directory) / f'{table}.parquet')), len(frame))


if __name__ == '__main__':
    unittest.main()
