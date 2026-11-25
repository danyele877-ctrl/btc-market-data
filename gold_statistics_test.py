import unittest
from gold_dashboard import summarize
from gold_intermarket import assess
from gold_ws_model import assess as ws_assess

class StatisticsTest(unittest.TestCase):
    def test_outcomes(self):
        now=1800000000
        def row(t,value=None):
            x={"observed_at":t,"v1":{"candidate":"LONG"}}
            if value is not None:
                start=(int(t//60)+1)*60
                x["outcomes"]={"early_v1":{"5":{"net_fee_only":value,"entry_ts":start,"exit_ts":start+300}}}
            return x
        s=summarize([row(now-1200,.01),row(now-1140,-.02),row(now-600),row(now-60)],now)
        m=s["models"]["EARLY V1"]["outcomes"]["LONG_5m"]
        self.assertEqual((m["settled"],m["pending"],m["missing"],m["nonoverlapping_n"]),(2,1,1,1))
        self.assertEqual(m["positive_rate_fee_only"],.5)
        self.assertEqual(s["models"]["INTERMARKET"]["counts"]["NOT_EVALUATED"],4)
    def test_intermarket(self):
        now=1800000000
        rows=[[str((now-(31-i)*60)*1000),"100","105","95",str(100+i*.01),"1" if i<28 else "3","0","0","1"] for i in range(31)]
        macro={"dxy":{"validated":True,"closed_observations":[[now-7200,101],[now-3600,100]]},"gold_futures_proxy":{"validated":True,"closed_observations":[[now-7200,100],[now-3600,101]]}}
        self.assertEqual(assess(rows,macro,now)["candidate"],"LONG")
        macro["dxy"]["validated"]=False
        self.assertEqual(assess(rows,macro,now)["candidate"],"NONE")
    def test_ws_missing(self):
        self.assertEqual(ws_assess({},None,1800000000)["candidate"],"NONE")

if __name__=="__main__": unittest.main()
