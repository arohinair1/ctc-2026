"""Causal state, inferred terminal values and bounded trade decisions.

Only public messages already received are used. No season ID, date, storm name,
manifest pattern or future market price is a model feature.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import json
from pathlib import Path
import numpy as np

PHYSICAL = ['PORT', 'ISLE', 'AQUA', 'GRID', 'FERRY']
STOCKS = PHYSICAL + ['RECON', 'MAT', 'GEN', 'HAVEN', 'REIN']
SYMBOLS = STOCKS + ['ARCH', 'REBLD']
OUTLOOKS = ['direct', 'chain', 'recurve', 'stall', 'eyewall', 'erratic']
ISLANDS = ['cascadia_prime', 'coral_key', 'azure_isle', 'palm_atoll']
REPORT_ONLY = {'ISLE', 'AQUA', 'RECON', 'MAT', 'HAVEN', 'REIN'}

def bound_stock_values(values):
    """Observed-regime economic bounds; invalid after unmodeled announcements."""
    return np.clip(values,[0,22,15,0,0,40,28,32,21,0],[100,60,35,50,25,64,42,50,75,90])

def seconds_from_open(timestamp: str) -> float:
    h, m, s = map(float, timestamp.split('T')[1][:8].split(':'))
    return ((h * 3600 + m * 60 + s) - (9 * 3600 + 30 * 60)) / 30

def add_etfs(values):
    v = np.asarray(values)
    arch = v[..., 0] + v[..., 1] + 2*v[..., 2] + v[..., 3] + 2*v[..., 4] + v[..., 8]
    reb = 2*v[..., 5] + 2*v[..., 6] + v[..., 7]
    return np.concatenate([v, np.stack([arch, reb], axis=-1)], axis=-1)

def terminal_values(damage_pct, outage_hours, season_loss=None):
    """Empirical simulator formulas; valid in the historical economic regime.

    Damage is cumulative percentage POINTS, outage is weighted market hours.
    REIN upper exhaustion is not identified by the observed loss range.
    """
    d = np.asarray(damage_pct, dtype=float)
    h = np.asarray(outage_hours, dtype=float)
    if season_loss is None:
        season_loss = d @ np.array([9, 6, 2.5, 7, 1.2])
    loss = np.asarray(season_loss)
    return np.stack([
        100 - .55*d[..., 0] - 2.4*h[..., 0],
        60 - .38*d[..., 1],
        35 - .20*d[..., 2],
        50 - .24*d[..., 3] - 6*h[..., 3],
        25 - .05*d[..., 4] - 7.2*h[..., 4],
        40 + 24*(-np.expm1(-(.009*d[..., 0]+.007*d[..., 3]+.0012*d[..., 4]))),
        28 + 14*(-np.expm1(-(d @ np.array([.0075,.005,1/480,.007*5/6,.001])))),
        32 + 18*(-np.expm1(-h[..., 3])),
        75 - .06*np.clip(loss-400, 0, 900),
        90 - .035*np.maximum(loss-1300, 0),
    ], axis=-1)


class State:
    def __init__(self, case):
        self.case = case
        sites = [s for k in PHYSICAL for s in case['companies'][k]['sites']]
        self.xy = np.array([[s['x'], s['y']] for s in sites])
        self.weights = np.zeros((len(sites), 5))
        k = 0
        for j, sym in enumerate(PHYSICAL):
            for site in case['companies'][sym]['sites']:
                self.weights[k, j] = site['weight']; k += 1
        self.round = 1
        self.last_seq = -1
        self.reports = {}
        self.total_integrals = np.zeros((4, len(sites)))
        self.round_integrals = np.zeros_like(self.total_integrals)
        self.max_wind = np.zeros(len(sites))
        self.observations = deque(maxlen=80)
        self.obs = None
        self.official = None
        self.wire = None
        self.official_counts = np.zeros(6)
        self.wire_counts = np.zeros(6)
        self.announcements = {}
        self.continuity_ok = False
        self.start_seen = False
        self.last_age = None
        self.last_wind = None
        self.snapshot_phase = None

    def begin_round(self, number):
        if number == self.round:
            return
        self.round = number
        self.round_integrals[:] = 0
        self.max_wind[:] = 0
        self.observations.clear()
        self.obs = self.official = self.wire = None
        self.official_counts[:] = 0
        self.wire_counts[:] = 0
        self.last_age = self.last_wind = None

    def _report(self, m):
        r = int(m['round'])
        old = self.reports.get(r)
        if old is None or old.get('stage') != 'final' or m.get('stage') == 'final':
            self.reports[r] = m

    def ingest(self, m):
        kind = m.get('type')
        if kind == 'snapshot':
            self.begin_round(int(m.get('round', self.round)))
            self.snapshot_phase = m.get('phase')
            for report in m.get('damage', []):
                if 'round' in report:
                    self._report(report)
            for item in m.get('headlines', []):
                if item.get('type') == 'announcement':
                    self.announcements[str(item.get('seq', item.get('time')))] = item
            if m.get('advisory'):
                self.official = m['advisory']
            if not self.start_seen and self.round == 1 and m.get('phase') == 'pre_storm':
                self.start_seen = self.continuity_ok = True
            # A sparse snapshot does not reconstruct integrated historical outages.
            if m.get('obs') and self.obs is None:
                self.obs = {'type':'storm', 'round':self.round, **m['obs']}
                self.continuity_ok = False
            self.last_seq = max(self.last_seq, int(m.get('last_seq', -1)))
            return True
        seq = m.get('seq')
        if seq is not None and seq <= self.last_seq:
            return False
        if seq is not None:
            self.last_seq = seq
        self.begin_round(int(m.get('round', self.round)))
        if kind == 'storm_formed' and self.round == 1:
            self.start_seen = self.continuity_ok = True
        elif kind == 'storm':
            age = float(m['age'])
            if self.round == 1 and age == 0:
                self.start_seen = self.continuity_ok = True
            wind = float(m['intensity'])*np.exp(-np.sum((self.xy-[m['x'],m['y']])**2,axis=1)/float(m['radius'])**2)
            cur = np.stack([wind, wind**2, wind**3, (wind>.35).astype(float)])
            if self.last_age is not None:
                delta = age-self.last_age
                if delta <= 0:
                    return False
                if delta > 4.01:
                    self.continuity_ok = False
                # Trapezoidal integration; hours use real seconds / 120.
                inc = .5*(self.last_wind+cur)*delta
                self.round_integrals += inc
                self.total_integrals += inc
            elif age > 0:
                self.continuity_ok = False
            self.last_age, self.last_wind = age, cur
            self.max_wind = np.maximum(self.max_wind, wind)
            self.obs = m
            self.observations.append(m)
        elif kind == 'damage_report':
            self._report(m)
        elif kind == 'advisory':
            outlook = m.get('outlook')
            if m.get('source') == 'official':
                self.official = m
                if outlook in OUTLOOKS:self.official_counts[OUTLOOKS.index(outlook)] += 1
            else:
                self.wire = m
                if outlook in OUTLOOKS:self.wire_counts[OUTLOOKS.index(outlook)] += 1
        elif kind == 'announcement':
            self.announcements[str(seq)] = m
        return True

    def damage(self):
        return sum((np.array([r['damage_pct'][s] for s in PHYSICAL]) for r in self.reports.values()), np.zeros(5))

    def hours(self):
        return self.total_integrals[3] @ self.weights / 120

    def loss(self):
        return sum(r['industry_loss_musd'] for r in self.reports.values())

    def final_known(self):
        return all(self.reports.get(r,{}).get('stage')=='final' for r in (1,2,3))

    def exact(self):
        return add_etfs(terminal_values(self.damage(), self.hours(), self.loss()))

    def features(self, second):
        values=[]
        def add(x):values.extend(np.asarray(x,dtype=float).ravel().tolist())
        add([self.round,second,max(0,min(480,second-60)),self.obs is not None])
        add(self.damage());add([self.loss()])
        for r in (1,2,3):
            rep=self.reports.get(r,{})
            add([bool(rep),rep.get('stage')=='final'])
        add(self.total_integrals);add(self.round_integrals);add(self.max_wind)
        obs=self.obs or {}
        add([obs.get(k,0) for k in ['x','y','vx','vy','radius','intensity']])
        for lag in (30,90):
            older=[m for m in self.observations if m['age']<=obs.get('age',0)-lag]
            m=older[-1] if older else obs
            add([m.get(k,0) for k in ['x','y','vx','vy']])
        for adv in (self.official,self.wire):
            adv=adv or {}
            add([bool(adv),max(0,second-60-adv.get('age',0))])
            add([adv.get('outlook')==key for key in OUTLOOKS])
        add(self.official_counts);add(self.wire_counts)
        adv=self.official or {}
        add([adv.get('hurricane_conditions',{}).get(k,0) for k in ISLANDS])
        cone=adv.get('cone',[])
        for horizon in (30,90,180):
            target=obs.get('age',max(0,second-60))+horizon
            p=min(cone,key=lambda p:abs(p['age']-target)) if cone else {}
            add([p.get(k,0) for k in ['x','y','spread']])
        # Cheap current-condition anchor. Unreported ongoing damage is learned
        # from observed wind integrals by the residual model.
        add(terminal_values(self.damage(), self.hours(), self.loss()))
        return np.array(values,dtype=np.float32)


def phase_bucket(round_number, second):
    phase = 'pre' if second < 60 else 'early' if second < 240 else 'late' if second < 540 else 'aftermath' if second < 740 else 'final'
    return f'{round_number}:{phase}'

@dataclass
class Decision:
    symbol: str
    side: str
    quantity: int
    price: float
    fair: float
    target: int
    edge: float

def decide(fairs, sigma, quotes, positions, limits, max_fraction=.35,
           edge_floor=.20, risk_multiple=.35, blocked=(), previous_targets=None):
    """Return ranked bounded orders. A caller must reconcile open orders first.

    Sigma is held-out prediction error, not just dispersion between trees.
    Keep the existing position inside the no-trade band (hysteresis).
    """
    actions=[]
    for j,s in enumerate(SYMBOLS):
        if s in blocked or s not in quotes:continue
        q=quotes[s];bid,ask=q.get('bid'),q.get('ask')
        if bid is None or ask is None:continue
        bid,ask=float(bid),float(ask)
        if not np.isfinite([bid,ask,fairs[j],sigma[j]]).all() or bid<=0 or ask<bid:continue
        pos=int(positions.get(s,0));cap=int(limits[s]*max_fraction)
        threshold=max(edge_floor,risk_multiple*float(sigma[j]))
        buy_edge=float(fairs[j])-ask;sell_edge=bid-float(fairs[j])
        if buy_edge>threshold:
            target=max(1,int(cap*min(1,(buy_edge-threshold)/max(threshold,.25))))
        elif sell_edge>threshold:
            target=-max(1,int(cap*min(1,(sell_edge-threshold)/max(threshold,.25))))
        elif bid-edge_floor<=fairs[j]<=ask+edge_floor:
            target=0
        else:
            continue
        delta=target-pos
        if not delta:continue
        side='buy' if delta>0 else 'sell';price=ask if delta>0 else bid
        edge=buy_edge if delta>0 else sell_edge
        # An exit can have slightly negative edge to avoid churn around zero.
        if edge < -edge_floor:continue
        actions.append(Decision(s,side,abs(delta),round(price,6),float(fairs[j]),target,edge))
    return sorted(actions,key=lambda a:a.quantity*max(a.edge,.01),reverse=True)

def etf_opportunities(quotes, case, positions=None, open_orders=None):
    """Executable-side top-of-book arithmetic; NOT a fill guarantee.

    This returns alerts. It deliberately does not execute a non-atomic basket.
    Create first changes inventory by +ETF/-basket; redeem first is opposite.
    Limits include existing same-side resting orders in the conversion state.
    """
    positions=positions or {};open_orders=open_orders or []
    out=[]
    for etf, info in case['etfs'].items():
        basket=info['basket']
        if not all(s in quotes and quotes[s].get('bid') is not None and quotes[s].get('ask') is not None for s in [etf,*basket]):continue
        for direction in ('create','redeem'):
            edge=(quotes[etf]['bid']-sum(n*quotes[s]['ask'] for s,n in basket.items())) if direction=='create' else (sum(n*quotes[s]['bid'] for s,n in basket.items())-quotes[etf]['ask'])
            if edge<=0:continue
            changes={etf:1,**{s:-n for s,n in basket.items()}}
            if direction=='redeem':changes={s:-n for s,n in changes.items()}
            capacity=[]
            for s,delta in changes.items():
                same=sum(o['quantity'] for o in open_orders if o['symbol']==s and o['side']==('buy' if delta>0 else 'sell'))
                room=case['limits'][s]-(positions.get(s,0) if delta>0 else -positions.get(s,0))-same
                capacity.append(max(0,int(room/abs(delta))))
            out.append({'etf':etf,'direction':direction,'edge_per_share':round(edge,6),'conversion_first_limit_capacity':min(capacity),'depth_verified':False})
    return out
