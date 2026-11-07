import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_oba_retrieval import evaluate, visual_questions


class EvaluationTests(unittest.TestCase):
    def test_visual_only_discards_synthetic_filters(self):
        common = {'reviewed': True, 'split': 'dev', 'expected_status': 'ok', 'query': '바닷가 사진',
                  'relevant_photo_ids': ['p1']}
        rows = [dict(common, id='visual', expected_filters={}),
                dict(common, id='synthetic', expected_filters={'location': '서울'})]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'questions.jsonl'
            path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows), encoding='utf-8')
            self.assertEqual([row['id'] for row in visual_questions(path)], ['visual'])

    def test_reports_scores_and_hits_without_inventing_negative_decisions(self):
        rows = [{'id':'positive', 'split':'dev', 'query':'scene', 'expected_status':'ok', 'relevant_photo_ids':['p1']},
                {'id':'negative', 'split':'test', 'query':'not present', 'expected_status':'no_match', 'relevant_photo_ids':[]}]
        calls = []
        def encode(text):
            calls.append(text)
            return [1, 0]
        def search(vector):
            return [{'asset_id':'asset-1', 'distance':0.25}]
        result = evaluate(rows, encode, search, {'asset-1':'p1'})
        self.assertFalse(result['threshold_applied'])
        self.assertEqual(result['splits']['dev']['hit_at_3'], 1.0)
        self.assertEqual(result['splits']['test']['negative_questions_returning_candidates_without_threshold'], 1)
        self.assertEqual(result['results'][1]['candidates'][0]['cosine_similarity'], 0.75)
        self.assertIsNone(result['results'][1]['hit_at_3'])
        self.assertEqual(calls, ['scene', 'not present'])
        self.assertNotIn('not present', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
