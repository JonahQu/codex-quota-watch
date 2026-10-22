import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from monitor import next_threshold,reset_reason
from forecast import build_forecast
class MonitorTests(unittest.TestCase):
    def test_thresholds_and_dedup(self):
        for remaining,alerts,want in [(11,{},None),(10,{},10),(5,{},5),(4,{'5':{}},None),(9,{'10':{}},None)]:
            self.assertEqual(next_threshold(remaining,alerts),want)
    def test_account_change_cannot_signal_reset(self):
        a={'accountFingerprint':'a','limitId':'codex','windowDurationMins':10080,'usedPercent':90,'resetsAt':100}
        b={**a,'usedPercent':1}
        self.assertIsNotNone(reset_reason(a,b,timestamp=50))
        self.assertIsNone(reset_reason(a,{**b,'accountFingerprint':'b'},timestamp=50))
    def test_trend_is_not_claimed_without_data(self):
        current={'checkedAt':'2026-01-01T00:00:00+00:00','limitId':'codex','windowDurationMins':10080,'resetsAt':100,'usedPercent':10,'remainingPercent':90}
        self.assertEqual(build_forecast([],current)['status'],'insufficient_consumption')
if __name__=='__main__':unittest.main()
