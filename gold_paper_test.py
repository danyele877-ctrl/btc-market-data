import unittest
from gold_paper import advance

class PaperTest(unittest.TestCase):
    def item(self):
        return {"direction":"LONG","plan":{"entry_limit":100,"stop":95,"tp1":105,"tp2":110,"tp3":115},"step":60,"cursor":60,"expires_at":600,"holding_seconds":3600,"state":"PENDING"}
    def test_targets(self):
        x=self.item()
        advance(x,[[60,101,106,99,102,1],[120,102,106,100,105,1],[180,105,111,104,110,1]],240)
        self.assertEqual(x["state"],"TOUCHED")
        self.assertEqual(x["targets_touched"],{"tp1":180,"tp2":240})
        advance(x,[[240,110,116,109,115,1]],300)
        self.assertEqual(x["state"],"FINAL_TARGET_TOUCHED")
    def test_ambiguous(self):
        x=self.item()
        advance(x,[[60,101,116,94,105,1]],120)
        self.assertEqual(x["state"],"AMBIGUOUS")
        self.assertFalse(x.get("targets_touched"))
    def test_gap(self):
        x=self.item()
        advance(x,[[120,101,103,99,102,1]],180)
        self.assertEqual(x["state"],"DATA_GAP")

if __name__=="__main__": unittest.main()
