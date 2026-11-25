import unittest

from materials import list_feeds, read_feed

FIELDS = {
    'objectives': ('scope', 'month_start', 'month_end', 'error_units', 'fallback_ok', 'latency_ms', 'holds_until_min'),
    'intake': ('minute', 'request', 'services', 'client_receipt'),
    'facts': ('at', 'request', 'service', 'source', 'ref', 'revision', 'outcome', 'latency_ms', 'fallback'),
    'changes': ('at', 'action', 'kind', 'period_start', 'scopes', 'units'),
}


class MaterialTest(unittest.TestCase):
    def test_feed_fields(self):
        self.assertEqual(list_feeds(), sorted(FIELDS))
        for name, fields in FIELDS.items():
            with self.subTest(feed=name):
                records = list(read_feed(name))
                self.assertTrue(records)
                self.assertTrue(all(set(row) == set(fields) for row in records))
