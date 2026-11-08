import sys, unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from crowd import level, levels_for, target  # noqa: E402


class CrowdTest(unittest.TestCase):
    def test_level(self):
        self.assertEqual("0", level(10))
        self.assertEqual("1", level(30))
        self.assertEqual("2", level(70))

    def test_levels_for_days_and_missing(self):
        rows = [{"tAtsNm": "경복궁", "baseYmd": "20261109", "cnctrRate": "80"},
                {"tAtsNm": "경복궁", "baseYmd": "20261111", "cnctrRate": "12.5"},
                {"tAtsNm": "경복궁", "baseYmd": "20261201", "cnctrRate": "50"}]
        got = levels_for(rows, datetime(2026, 11, 9))
        self.assertEqual("2-0----", got["경복궁"])

    def test_target_codes(self):
        self.assertEqual(("11", "11110"), target({"areaCd": "11", "signguCd": "110", "signguNm": "종로구"}))
        self.assertEqual(("29", "29170"), target({"areaCd": "12", "signguCd": "300", "signguNm": "북구"}))
        self.assertEqual(("46", "46110"), target({"areaCd": "12", "signguCd": "110", "signguNm": "목포시"}))
        self.assertEqual(("36", "36110"), target({"areaCd": "36110", "signguCd": "36110", "signguNm": "세종특별자치시"}))


if __name__ == "__main__":
    unittest.main()
