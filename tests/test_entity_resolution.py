"""Check identity conflicts, conservative matches, and enrichment across passes."""
import ast
import hashlib
import json
import re
import tempfile
import unittest
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
from rapidfuzz.fuzz import ratio
from rapidfuzz import process

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = json.loads((ROOT / 'notebooks/03_entity_resolution.ipynb').read_text())
NS = dict(globals())
CONSTANTS = {
    'ABN_WEIGHTS', 'ACN_WEIGHTS', 'FEATURE_COLUMNS', 'SHARED_WEBSITE_HOSTS',
    'LINK_COLUMNS', 'REVIEW_COLUMNS', 'CANDIDATE_COLUMNS',
}
for cell in NOTEBOOK['cells']:
    if cell['cell_type'] != 'code':
        continue
    tree = ast.parse(''.join(cell['source']))
    declarations = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    or (isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id in CONSTANTS for target in node.targets))]
    exec(compile(ast.Module(body=declarations, type_ignores=[]), '<part3 definitions>', 'exec'), NS)
NS.update(MIN_LINK_SCORE=0.97, MIN_SCORE_MARGIN=0.02, MAX_MATCH_PASSES=10,
          MAX_CANDIDATES=500, REVIEW_SAMPLE_SIZE=50, REVIEW_SEED=20261001)


def row(record_id, abn=None, acn=None, **fields):
    return {'gold_table': 'gold_fixture', 'observation_source_id': 'fixture',
            'observation_source_record_id': record_id,
            'entity_abn': abn, 'entity_acn': acn, **fields}


def run(rows):
    observations, duplicates = NS['prepare_observations'](pd.DataFrame(rows))
    return (*NS['resolve_observations'](observations), observations, duplicates)


class EntityResolutionTests(unittest.TestCase):
    def test_official_examples_and_no_padding(self):
        self.assertTrue(NS['valid_abn']('51824753556'))
        self.assertTrue(NS['valid_acn']('004085616'))
        self.assertFalse(NS['valid_acn']('824753556'))
        self.assertEqual(NS['derive_abn']('004085616'), '53004085616')
        self.assertEqual(NS['normalize_identifier']('51 824-753-556'), '51824753556')
        for bad in ['82475355', '000000000', '004085616.0', 'x004085616']:
            self.assertFalse(NS['valid_acn'](bad))
        self.assertFalse(NS['valid_abn']('00000000000'))

    def test_abn_and_acn_only_resolve_to_one_entity(self):
        pool, links, review, *_ = run([
            row('a', abn='53004085616'), row('b', acn='004085616')])
        self.assertEqual(pool.to_dict('records'), [
            {'entity_id': '53004085616', 'abn': '53004085616', 'acn': '004085616'}])
        self.assertEqual(len(links), 2)
        self.assertTrue(review.empty)

    def test_derived_value_does_not_claim_registered_abn(self):
        pool, links, *_ = run([row('a', acn='004085616')])
        self.assertEqual(pool.iloc[0]['entity_id'], '53004085616')
        self.assertIsNone(pool.iloc[0]['abn'])
        self.assertEqual(links.iloc[0]['match_method'], 'acn_derived_abn')

    def test_conflicting_or_invalid_stated_ids_never_link(self):
        pool, links, review, *_ = run([
            row('a', abn='51824753556', acn='000000019'),
            row('b', abn='not_an_abn', acn='004085616')])
        self.assertTrue(pool.empty)
        self.assertTrue(links.empty)
        self.assertEqual(set(review['reason']), {'abn_acn_conflict', 'invalid_stated_abn'})

    def test_abn_does_not_automatically_establish_acn(self):
        pool, *_ = run([row('a', abn='51824753556')])
        self.assertIsNone(pool.iloc[0]['acn'])

    def test_acn_checksum_ambiguity_abstains(self):
        ambiguous = None
        for number in range(1, 5000):
            body = f'{number:08d}'
            check = (10 - sum(int(x)*w for x,w in zip(body, NS['ACN_WEIGHTS'])) % 10) % 10
            acn = body + str(check)
            candidates = NS['abn_candidates_from_acn'](acn)
            if len(candidates) == 2:
                ambiguous = (acn, candidates)
                break
        self.assertIsNotNone(ambiguous)
        acn, candidates = ambiguous
        self.assertIsNone(NS['derive_abn'](acn))
        _, links, review, *_ = run([row('a', acn=acn)])
        self.assertTrue(links.empty)
        self.assertEqual(review.iloc[0]['reason'], 'ambiguous_acn_abn_prefix')
        _, links, review, *_ = run([
            row('a', abn=candidates[0], acn=acn), row('b', abn=candidates[1], acn=acn)])
        self.assertTrue(links.empty)
        self.assertEqual(set(review['reason']), {'acn_claimed_by_multiple_abns'})

    def test_name_alone_is_insufficient(self):
        _, links, review, *_ = run([
            row('a', abn='51824753556', entity_legal_name='Example Pty Ltd'),
            row('b', entity_legal_name='Example Proprietary Limited')])
        self.assertEqual(len(links), 1)
        self.assertEqual(review.iloc[0]['reason'], 'insufficient_evidence')

    def test_ambiguous_name_address_match_is_not_selected(self):
        fields = dict(entity_legal_name='Example Pty Ltd', address_full='1 High Street Melbourne')
        _, links, review, *_ = run([
            row('a', abn='51824753556', **fields), row('b', abn='89000000019', **fields),
            row('c', **fields)])
        self.assertEqual(len(links), 2)
        self.assertEqual(review.iloc[0]['reason'], 'ambiguous_candidates')

    def test_website_or_state_conflict_blocks_name_match(self):
        common = dict(entity_legal_name='Example Pty Ltd', address_full='1 High Street Melbourne')
        _, links, review, *_ = run([
            row('a', abn='51824753556', entity_website='example.com.au', address_state='VIC', **common),
            row('b', entity_website='other.com.au', address_state='NSW', **common)])
        self.assertEqual(len(links), 1)
        self.assertEqual(review.iloc[0]['reason'], 'conflicting_candidate')

    def test_iterative_enrichment_reaches_second_indirect_record(self):
        common = dict(entity_legal_name='Example Pty Ltd', address_full='1 High Street Melbourne')
        _, links, review, passes, *_ = run([
            row('a', abn='51824753556', **common),
            row('b', entity_website='example.com.au', entity_trading_name='Example Brand', **common),
            row('c', entity_website='www.example.com.au/contact', entity_trading_name='Example Brand')])
        self.assertEqual(len(links), 3)
        self.assertTrue(review.empty)
        self.assertEqual(passes['new_links'].tolist(), [1, 1, 1])
        by_source = links.set_index('source_record_id')
        self.assertLessEqual(by_source.loc['c', 'link_confidence'], by_source.loc['b', 'link_confidence'])

    def test_conflicting_source_duplicates_are_quarantined(self):
        _, links, review, _, _, duplicates = run([
            row('a', abn='51824753556'), row('a', abn='89000000019')])
        self.assertEqual(duplicates, 1)
        self.assertTrue(links.empty)
        self.assertIn('conflicting_duplicate_source_record', review.iloc[0]['reason'])

    def test_missing_source_keys_do_not_collapse_unrelated_rows(self):
        _, _, review, _, _, duplicates = run([
            row(None, entity_legal_name='One'), row(None, entity_legal_name='Two')])
        self.assertEqual(duplicates, 0)
        self.assertEqual(len(review), 2)

    def test_weak_name_candidate_is_visible_but_not_linked(self):
        rows = [row('a', abn='51824753556', entity_legal_name='Example Pty Ltd'),
                row('b', entity_legal_name='Example Pty Ltd')]
        _, links, _, _, observations, _ = run(rows)
        report = NS['possible_match_report'](observations, links.set_index('link_id').to_dict('index'))
        self.assertEqual(len(links), 1)
        self.assertEqual(len(report), 1)
        self.assertEqual(report.iloc[0]['candidate_entity_id'], '51824753556')
        self.assertLess(report.iloc[0]['proposal_score'], NS['MIN_LINK_SCORE'])
        self.assertEqual(report.iloc[0]['review_status'], 'possible match — needs review')

    def test_conflicting_identifier_does_not_become_name_proposal(self):
        rows = [row('a', abn='51824753556', entity_legal_name='Example Pty Ltd'),
                row('b', abn='invalid', entity_legal_name='Example Pty Ltd')]
        _, links, _, _, observations, _ = run(rows)
        report = NS['possible_match_report'](observations, links.set_index('link_id').to_dict('index'))
        self.assertTrue(report.empty)

    def test_rerun_link_ids_and_decisions_are_stable(self):
        rows = [row('a', abn='53004085616'), row('b', acn='004085616')]
        first = run(rows)[1].drop(columns='created_at').sort_values('link_id').reset_index(drop=True)
        second = run(list(reversed(rows)))[1].drop(columns='created_at').sort_values('link_id').reset_index(drop=True)
        pd.testing.assert_frame_equal(first, second)

    def test_review_labels_survive_only_unchanged_evidence(self):
        rows = [row('a', abn='51824753556')]
        _, links, _, _, observations, _ = run(rows)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'review.csv'
            sample = NS['make_review_sample'](links, observations, path)
            sample['label_correct'] = 'true'
            sample.to_csv(path, index=False)
            kept = NS['make_review_sample'](links, observations, path)
            self.assertEqual(kept.iloc[0]['label_correct'], 'true')
            links.loc[:, 'entity_id'] = '89000000019'
            changed = NS['make_review_sample'](links, observations, path)
            self.assertEqual(changed.iloc[0]['label_correct'], '')


if __name__ == '__main__':
    unittest.main()
