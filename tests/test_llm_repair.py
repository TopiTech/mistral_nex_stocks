import unittest
import json
from unittest.mock import patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import app

class LLMRepairFlowTest(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    @patch('app.call_mistral_chat')
    @patch('app.repair_analysis_json_with_llm')
    def test_analyze_v2_repair_path(self, mock_repair, mock_call):
        # Simulate Mistral returning non-JSON content
        mock_call.return_value = {"choices": [{"message": {"content": 'this is not json'}}]}

        # Simulate LLM repair returning a full analysis dict
        repaired = {
            'recommendation': '買い',
            'sentiment': '強気',
            'target_price_3m': 100.0,
            'upside_3m': '+10%',
            'confidence': '中',
            'analysis_summary': 'Repaired summary',
            'key_catalysts': ['c1'],
            'risk_factors': ['r1'],
            'technical_analysis': 'ta',
            'fundamental_analysis': 'fa',
            'latest_news_impact': 'impact',
        }
        mock_repair.return_value = (repaired, 'repaired content')

        response = self.client.post(
            '/api/analyze-v2',
            json={
                'symbol': 'AAPL',
                'market': 'us',
                'history': [{'date': '2026-05-20', 'close': 150.0}],
                'news': 'some news',
                'indices_summary': 'up'
            },
            headers={
                'Origin': 'http://localhost:5000',
                'Authorization': 'Bearer dummy-key'
            }
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data.get('analysis_summary'), 'Repaired summary')
        self.assertIn('version', data)

if __name__ == '__main__':
    unittest.main()
