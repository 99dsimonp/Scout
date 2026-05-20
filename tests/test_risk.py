import unittest

from scout.risk import extract_risk


class RiskTests(unittest.TestCase):
    def test_extract_risk_from_schema_json(self):
        self.assertEqual(extract_risk('{"risk":"high"}'), "high")

    def test_extract_risk_from_provider_result_envelope(self):
        self.assertEqual(
            extract_risk('{"type":"result","result":"{\\"risk\\":\\"low\\"}"}'),
            "low",
        )

    def test_extract_risk_falls_back_to_medium_for_invalid_output(self):
        self.assertEqual(extract_risk("not a risk"), "medium")


if __name__ == "__main__":
    unittest.main()
