import unittest
from unittest.mock import patch

from weather99.domain import timestamp
from weather99.settlement import collect_settlement, metar_evidence, routine_temperature


class SettlementTests(unittest.TestCase):
    def test_precise_remark_precedes_body_and_excludes_specials(self):
        self.assertEqual(routine_temperature('METAR ZSPD 181000Z 00000KT 14/13 RMK T01390133'), 13.9)
        self.assertEqual(routine_temperature('ZSPD 181000Z 00000KT M02/M03 RMK T10171022'), -1.7)
        self.assertEqual(routine_temperature('METAR COR ZSPD 181010Z M11/M17 Q1010'), -11)
        self.assertIsNone(routine_temperature('SPECI ZSPD 181000Z 30/20 RMK T03000200'))
        self.assertIsNone(routine_temperature('METAR ZSSS 181000Z 40/20'))
        self.assertIsNone(routine_temperature('METAR ZSPD 181000Z Q1010'))
        self.assertEqual(routine_temperature('ZSPD 181000Z 14/13 RMK T099999999'), 14)

    def row(self, moment, temp='13/05', **extra):
        return {'icaoId': 'ZSPD', 'obsTime': timestamp(moment).timestamp(),
                'rawOb': f'METAR ZSPD 181000Z {temp}', **extra}

    def test_local_date_corrections_precision_rounding_and_cutoff(self):
        rows = [
            self.row('2026-11-17T15:59:00Z', '40/20'),
            self.row('2026-11-17T16:00:00Z', '17/13 RMK T01650130'),
            self.row('2026-11-18T10:00:00Z', '30/20', receiptTime='2026-11-18T10:01:00Z'),
            self.row('2026-11-18T10:00:00Z', '16/12', receiptTime='2026-11-18T10:02:00Z'),
            self.row('2026-11-18T11:00:00Z', rawOb='SPECI ZSPD 181100Z 50/20'),
            self.row('2026-11-18T12:00:00Z', '45/20', receiptTime='2026-11-19T10:00:00Z'),
            self.row('2026-11-18T16:00:00Z', '15/10'),
        ]
        result = metar_evidence(rows, '2026-11-18', '2026-11-18T17:00:00Z')
        self.assertEqual(result['observation_count'], 2)
        self.assertEqual(result['observed_max_c'], 16.5)
        self.assertEqual(result['rounded_max_c'], 17)
        self.assertEqual(result['status'], 'day_closed_evidence')
        self.assertFalse(result['final'])
        negative = metar_evidence([self.row('2026-11-18T10:00:00Z', 'M09/M10 RMK T10851100')],
                                 '2026-11-18', '2026-11-18T11:00:00Z')
        self.assertEqual(negative['rounded_max_c'], -8)

    def event(self):
        return {'resolutionSource': 'https://aviationweather.gov/api/data/metar?ids=ZSPD&format=raw&date=2026-11-18T19:00:00Z&hours=30',
                'description': 'hourly temperature remark SPECI half rounded up'}

    def test_collection_covers_midnight_and_retains_transport(self):
        response = {'url': 'https://aviationweather.gov/api/data/metar', 'data': [self.row('2026-11-18T10:00:00Z')]}
        with patch('weather99.settlement.fetch', return_value=response) as fetch:
            result, raw = collect_settlement(self.event(), '2026-11-18', as_of='2026-11-18T10:30:00Z')
        self.assertEqual(fetch.call_args.args[1]['hours'], 20)
        self.assertEqual(result['observed_max_c'], 13)
        self.assertEqual(raw, response)

    def test_future_day_is_pending_and_unsupported_rules_fail_closed(self):
        with patch('weather99.settlement.fetch') as fetch:
            pending, _ = collect_settlement(self.event(), '2026-11-19', as_of='2026-11-18T10:00:00Z')
            unsupported, _ = collect_settlement(self.event() | {'resolutionSource': 'https://evil.example/'}, '2026-11-18')
        fetch.assert_not_called()
        self.assertEqual(pending['status'], 'awaiting_target_day')
        self.assertFalse(unsupported['rules_verified'])

    def test_failure_and_empty_responses_do_not_fabricate_temperature(self):
        for response, expected in (({'error': 'HTTP 403'}, 'source_error'), ({'data': []}, 'no_observations')):
            with patch('weather99.settlement.fetch', return_value=response):
                result, _ = collect_settlement(self.event(), '2026-11-18', as_of='2026-11-18T10:00:00Z')
            self.assertEqual(result['status'], expected)
            self.assertIsNone(result.get('observed_max_c'))


if __name__ == '__main__':
    unittest.main()
