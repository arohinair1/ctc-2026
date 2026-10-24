"""Offline financial, causal-state and execution-adapter checks. No network calls."""
import copy,json,unittest
from pathlib import Path
import numpy as np
from core import *
from bot import position_map,open_order_list,API

CASE=json.loads((Path(__file__).parent/'case.json').read_text())

class FinancialTests(unittest.TestCase):
    def test_insurance_attachment_and_exhaustion(self):
        d=np.zeros(5);h=np.zeros(5)
        for loss,haven,rein in [(399,75,90),(400,75,90),(1000,39,90),(1300,21,90),(1500,21,83)]:
            v=terminal_values(d,h,loss)
            self.assertAlmostEqual(v[8],haven);self.assertAlmostEqual(v[9],rein)
    def test_insurance_expectation_is_not_payoff_at_mean(self):
        low=terminal_values(np.zeros(5),np.zeros(5),1200)[9]
        high=terminal_values(np.zeros(5),np.zeros(5),1400)[9]
        atmean=terminal_values(np.zeros(5),np.zeros(5),1300)[9]
        self.assertAlmostEqual((low+high)/2,88.25)
        self.assertNotEqual((low+high)/2,atmean)
    def test_ferry_closure_dominates_small_damage(self):
        a=terminal_values(np.zeros(5),np.zeros(5))
        d=np.zeros(5);d[4]=10;h=np.zeros(5);h[4]=1
        b=terminal_values(d,h)
        self.assertAlmostEqual(a[4]-b[4],7.7)
    def test_etf_identity(self):
        v=np.arange(1,11,dtype=float);full=add_etfs(v)
        self.assertEqual(full[10],v[0]+v[1]+2*v[2]+v[3]+2*v[4]+v[8])
        self.assertEqual(full[11],2*v[5]+2*v[6]+v[7])
    def test_market_clock(self):
        self.assertEqual(seconds_from_open('2026-08-04T15:40:00'),740)
        self.assertEqual(seconds_from_open('2026-08-04T10:00:00'),60)
    def test_etf_executable_spreads(self):
        q={s:{'bid':10.,'ask':11.} for s in SYMBOLS}
        q['REBLD']={'bid':54.,'ask':56.}
        self.assertFalse(any(x['etf']=='REBLD' for x in etf_opportunities(q,CASE)))
        q['REBLD']['bid']=57.
        opportunities=[x for x in etf_opportunities(q,CASE) if x['etf']=='REBLD']
        self.assertEqual(opportunities[0]['edge_per_share'],2.)
        self.assertFalse(opportunities[0]['depth_verified'])
    def test_conversion_counts_temporary_inventory(self):
        q={s:{'bid':10.,'ask':11.} for s in SYMBOLS};q['REBLD']={'bid':57.,'ask':58.}
        x=[x for x in etf_opportunities(q,CASE,{'RECON':-499}) if x['etf']=='REBLD'][0]
        self.assertEqual(x['conversion_first_limit_capacity'],0)

class StateTests(unittest.TestCase):
    def observation(self,age,seq):
        return dict(type='storm',round=1,seq=seq,age=age,x=470.,y=80.,vx=0,vy=0,radius=80.,intensity=1.)
    def test_deduplicate_and_integrate_market_hours(self):
        state=State(CASE);m=self.observation(0,0);state.ingest(m);state.ingest(self.observation(2,1))
        before=state.features(62).copy();state.ingest(self.observation(2,1))
        np.testing.assert_array_equal(before,state.features(62));self.assertAlmostEqual(state.hours()[0],2/120)
    def test_gap_disables_trajectory_trades(self):
        state=State(CASE);state.ingest(self.observation(0,0));state.ingest(self.observation(12,2))
        self.assertFalse(state.continuity_ok)
    def test_preliminary_replaced_not_added(self):
        state=State(CASE)
        for seq,stage,damage in [(0,'preliminary',20),(1,'final',10),(2,'preliminary',30)]:
            state.ingest(dict(type='damage_report',round=1,seq=seq,stage=stage,damage_pct={s:damage for s in PHYSICAL},industry_loss_musd=100))
        np.testing.assert_array_equal(state.damage(),np.ones(5)*10)
        self.assertEqual(state.loss(),100)
    def test_snapshot_retains_prior_rounds_and_flags_missing_path(self):
        state=State(CASE)
        reports=[dict(type='damage_report',round=r,stage='final',damage_pct={s:1 for s in PHYSICAL},industry_loss_musd=10) for r in (1,2,3)]
        state.ingest(dict(type='snapshot',round=3,phase='aftermath',last_seq=900,damage=reports,obs=self.observation(480,800)))
        self.assertTrue(state.final_known());self.assertFalse(state.continuity_ok);self.assertEqual(state.loss(),30)
    def test_calendar_and_storm_name_not_features(self):
        a=State(CASE);b=State(CASE)
        x=self.observation(0,0);y=copy.deepcopy(x);x.update(storm='foo',time='2026-08-04T10:00:00');y.update(storm='bar',time='1500-08-04T10:00:00')
        a.ingest(x);b.ingest(y);np.testing.assert_array_equal(a.features(60),b.features(60))
    def test_announcement_latched(self):
        s=State(CASE);s.ingest(dict(type='announcement',round=1,seq=1,headline='new rules',symbols=['PORT']))
        self.assertIn('1',s.announcements)

class ExecutionTests(unittest.TestCase):
    def test_position_adapters_and_fail_closed(self):
        self.assertEqual(position_map({'positions':[{'symbol':'PORT','quantity':-20}]})['PORT'],-20)
        self.assertEqual(position_map([])['PORT'],0)
        for data in [{},{'error':'unauthorized'},[{'symbol':'PORT'}]]:
            with self.assertRaises(ValueError):position_map(data)
    def test_remaining_orders_not_original_size(self):
        p=open_order_list([{'id':'1','symbol':'PORT','side':'buy','quantity':20,'filled_quantity':7}])
        self.assertEqual(p[0]['quantity'],13)
        with self.assertRaises(ValueError):open_order_list([{'id':'1','symbol':'PORT','side':'buy','quantity':20}])
    def test_limits_and_no_crossed_book(self):
        q={s:{'bid':1.,'ask':2.} for s in SYMBOLS}
        a=decide(np.ones(12)*100,np.ones(12),q,{},CASE['limits'])
        for action in a:self.assertLessEqual(action.quantity,int(CASE['limits'][action.symbol]*.35))
        q={s:{'bid':3.,'ask':2.} for s in SYMBOLS}
        self.assertEqual(decide(np.ones(12)*100,np.ones(12),q,{},CASE['limits']),[])
    def test_rate_limit_blocks_without_network(self):
        api=API('https://example.invalid','not-a-key',budget=1)
        import time
        api.actions.append(time.monotonic())
        with self.assertRaisesRegex(RuntimeError,'budget'):api.request('/orders',method='POST',action=True)

if __name__=='__main__':unittest.main(verbosity=2)
