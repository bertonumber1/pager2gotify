import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pager2gotify as p

class Pager2GotifyTests(unittest.TestCase):
    def setUp(self):
        p.seen.clear()

    def test_send_valid_pocsag_512_suffix_450(self):
        line = 'POCSAG512: Address: 825450 Function: 1 Alpha: FIRE CALL AT SAMPLE ROAD'
        result = p.process_line(line)
        self.assertIsNotNone(result)
        self.assertEqual(result['action'], 'send')
        self.assertEqual(result['capcode'], '825450')

    def test_drop_wrong_suffix(self):
        line = 'POCSAG512: Address: 825451 Function: 1 Alpha: FIRE CALL AT SAMPLE ROAD'
        result = p.process_line(line)
        self.assertEqual(result['action'], 'drop')

    def test_drop_wrong_baud(self):
        line = 'POCSAG1200: Address: 825450 Function: 1 Alpha: FIRE CALL AT SAMPLE ROAD'
        result = p.process_line(line)
        self.assertEqual(result['action'], 'drop')

    def test_drop_test_message(self):
        line = 'POCSAG512: Address: 825450 Function: 1 Alpha: TEST MESSAGE'
        result = p.process_line(line)
        self.assertEqual(result['action'], 'drop')

    def test_drop_flex(self):
        line = 'FLEX: Address 123456 test'
        result = p.process_line(line)
        self.assertIsNone(result)

if __name__ == '__main__':
    unittest.main()
