import unittest

from bounds import bound


class OriginalTests(unittest.TestCase):
    def test_value_below_limit(self):
        self.assertEqual(bound(2, 5), 2)
