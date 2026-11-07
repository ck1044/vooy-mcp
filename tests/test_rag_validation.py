import unittest
from vooy_storage.rag import RAGService, validate_vectors


class RagValidationTests(unittest.TestCase):
    def test_bad_image_metadata_and_date_filters_are_rejected_before_db(self):
        service = RAGService(None)
        for fields in [('2025-05-01T10:00:00+09:00', 540, None, None),
                       ('2025-05-01T10:00:00', 900, None, None),
                       (None, None, 33.5, None), (None, None, 91, 120),
                       (None, None, float('nan'), 120)]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                service.record_image_metadata('job', 1, *fields)
        with self.assertRaises(ValueError):
            service.search_vector('00000000-0000-0000-0000-000000000001', [1], captured_from='2025-06-01', captured_to='2025-05-01')
        with self.assertRaises(ValueError):
            service.search_vector('00000000-0000-0000-0000-000000000001', [1], captured_from='20250501')

    def test_dimensions_finite_and_cosine_zero(self):
        space = {'dimensions': 3, 'metric': 'cosine', 'normalization': 'none', 'representation': 'single_vector'}
        self.assertEqual(validate_vectors([{'ordinal': 0, 'values': [1, 0, 0]}], space), [[1.0, 0.0, 0.0]])
        for values in ([1, 2], [1, float('nan'), 0], [0, 0, 0]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_vectors([{'ordinal': 0, 'values': values}], space)

    def test_unit_normalization_and_ordinals(self):
        space = {'dimensions': 2, 'metric': 'l2', 'normalization': 'unit', 'representation': 'multi_vector'}
        with self.assertRaises(ValueError):
            validate_vectors([{'ordinal': 0, 'values': [2, 0]}], space)
        with self.assertRaises(ValueError):
            validate_vectors([{'ordinal': 1, 'values': [1, 0]}], space)


if __name__ == '__main__':
    unittest.main()
