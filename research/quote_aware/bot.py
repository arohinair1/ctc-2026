"""Exchange adapter. Starts in dry-run mode. API key comes from CTC_API_KEY.

The supplied packet documents requests but not every response schema. Unknown
account/clock schemas stop execution instead of assuming flat positions.
"""
from __future__ import annotations
import argparse,asyncio,json,os,time,urllib.request,urllib.error
from collections import deque
from pathlib import Path
import joblib,numpy as np
import websockets
from core import *

HERE=Path(__file__).resolve().parent
DEFAULT_URL='https://cornelltradingcompetition.org'

def position_map(payload):
    raw=payload.get('positions') if isinstance(payload,dict) and 'positions' in payload else payload
    if isinstance(raw,list):
        result={}
        for p in raw:
            if not isinstance(p,dict) or 'symbol' not in p:raise ValueError('unrecognized position row')
            q=p.get('quantity',p.get('position',p.get('net_quantity')))
            if q is None:raise ValueError('position quantity missing')
            result[p['symbol']]=float(q)
    elif isinstance(raw,dict) and raw and all(k in SYMBOLS for k in raw):
        result={}
        for k,v in raw.items():
            if isinstance(v,dict):v=v.get('quantity',v.get('position'))
            if v is None:raise ValueError('position quantity missing')
            result[k]=float(v)
    else:raise ValueError('unknown positions schema; inspect REST response first')
    if any(not np.isfinite(v) or int(v)!=v for v in result.values()):raise ValueError('nonintegral/nonfinite position')
    return {s:int(result.get(s,0)) for s in SYMBOLS}

def open_order_list(payload):
    raw=payload.get('orders') if isinstance(payload,dict) and 'orders' in payload else payload
    if not isinstance(raw,list):raise ValueError('unknown open-orders schema')
    result=[]
    for o in raw:
        oid=o.get('order_id',o.get('id'));remaining=o.get('remaining_quantity',o.get('remaining'))
        if remaining is None and 'quantity' in o and ('filled_quantity' in o or 'filled' in o):
            remaining=float(o['quantity'])-float(o.get('filled_quantity',o.get('filled')))
        if oid is None or remaining is None or o.get('side') not in ('buy','sell') or o.get('symbol') not in SYMBOLS:
            raise ValueError('unknown order row; require id, symbol, side, remaining quantity')
        result.append({'id':str(oid),'symbol':o['symbol'],'side':o['side'],'quantity':float(remaining)})
    return result

class API:
    def __init__(self,url,key,budget=120):
        self.url=url.rstrip('/');self.key=key;self.actions=deque();self.budget=budget
    def request(self,path,method='GET',body=None,action=False):
        if action:
            now=time.monotonic()
            while self.actions and self.actions[0]<=now-60:self.actions.popleft()
            if len(self.actions)>=self.budget:raise RuntimeError('local order-action budget exhausted')
            self.actions.append(now)
        headers={'X-API-Key':self.key,'User-Agent':'ctc-systematic-equities-research/1.0','Content-Type':'application/json'}
        req=urllib.request.Request(self.url+path,headers=headers,method=method,data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(req,timeout=8) as resp:
            data=resp.read();return json.loads(data) if data else {}

class Bot:
    def __init__(self,args):
        self.args=args;self.case=json.loads((HERE/'case.json').read_text());self.state=State(self.case)
        self.model=joblib.load(HERE/'model.joblib');self.cal=json.loads((HERE/'calibration.json').read_text())
        self.api=API(args.url,os.environ['CTC_API_KEY'],args.action_budget)
        self.quotes={};self.clock={};self.competition=None;self.stamp=None
        self.connected=False;self.fault=None;self.owned=set();self.last_plan=0
        self.season_journal=[]
        Path(args.log_dir).mkdir(parents=True,exist_ok=True)
        self.logfile=(Path(args.log_dir)/'events.jsonl').open('a',buffering=1)
    def log(self,kind,**data):
        row={'wall_time':time.time(),'kind':kind,**data};line=json.dumps(row,default=float)
        self.logfile.write(line+'\n');print(line,flush=True)
    async def call(self,path,**kwargs):return await asyncio.to_thread(self.api.request,path,**kwargs)
    def control(self):
        p=Path(self.args.control)
        return json.loads(p.read_text()) if p.exists() else {}
    async def cancel_owned(self,orders):
        for o in orders:
            if o['id'] in self.owned:
                await self.call('/api/v1/orders/'+o['id'],method='DELETE',action=True)
                self.owned.discard(o['id']);self.log('cancel',order_id=o['id'])
    async def feed(self):
        url=self.args.url.rstrip('/').replace('https://','wss://',1).replace('http://','ws://',1)+'/ws/v1/market-data'
        while True:
            try:
                async with websockets.connect(url,additional_headers={'X-API-Key':self.api.key},max_size=4*1024*1024,ping_interval=20) as ws:
                    await ws.send(json.dumps({'action':'subscribe','symbols':SYMBOLS,'channels':['news','quotes']}))
                    self.connected=True;self.log('connected')
                    async for frame in ws:
                        data=json.loads(frame)
                        if data.get('type')=='quote' and data.get('symbol') in SYMBOLS:
                            self.quotes[data['symbol']]={**data,'received':time.monotonic()}
                        elif data.get('type')=='news' and isinstance(data.get('message'),dict):
                            m=data['message'];self.logfile.write(json.dumps({'type':'news','message':m})+'\n')
                            self.state.ingest(m);self.stamp=m.get('time',self.stamp)
                            if m.get('type')=='announcement':self.log('announcement_halt',message=m)
            except (OSError,websockets.WebSocketException,ValueError) as exc:
                self.connected=False;self.quotes.clear();self.log('disconnect',error=str(exc))
                # Full observations may be lost. Report-only final valuations remain usable.
                self.state.continuity_ok=False
                await asyncio.sleep(2)
    async def run(self):
        remote=await self.call('/cases/hurricane.json')
        for k in ('companies','etfs','limits','timing'):
            if remote.get(k)!=self.case[k]:raise RuntimeError(f'case mismatch: {k}; inspect before using historical model')
        feed=asyncio.create_task(self.feed())
        try:
            while True:
                try:
                    clock=await self.call('/api/v1/competitions/time');self.clock=clock
                    cid=clock.get('competition_id',clock.get('competition',{}).get('id') if isinstance(clock.get('competition'),dict) else None)
                    if cid is None:raise ValueError('competition id missing: inspect /competitions/time schema')
                    if self.competition is None:self.competition=cid
                    elif cid!=self.competition:
                        self.competition=cid;self.state=State(self.case);self.quotes.clear();self.stamp=None;self.owned.clear();self.fault=None
                        self.log('season_reset',competition_id=cid)
                    pos=position_map(await self.call('/api/v1/positions'))
                    orders=open_order_list(await self.call('/api/v1/orders/open'))
                    if self.args.execute:await self.cancel_owned(orders)
                    controls=self.control()
                    if controls.get('paused',False) or self.fault or not self.connected or clock.get('state')!='running':
                        await asyncio.sleep(2);continue
                    unacked=[k for k in self.state.announcements if f'{cid}:{k}' not in controls.get('acknowledged_announcements',[])]
                    if unacked:
                        self.log('paused_for_announcement',ids=[f'{cid}:{k}' for k in unacked]);await asyncio.sleep(5);continue
                    stamp=clock.get('time',clock.get('market_time',clock.get('current_time',self.stamp)))
                    if not isinstance(stamp,str) or 'T' not in stamp:raise ValueError('market clock timestamp missing')
                    sec=seconds_from_open(stamp)
                    if not 0<=sec<780:await asyncio.sleep(2);continue
                    if self.state.round==1 and sec<60 and self.stamp is None:
                        # Require a snapshot or storm-formed event to establish season state.
                        await asyncio.sleep(2);continue
                    blocked=set(controls.get('blocked_symbols',[]))
                    fresh={s:q for s,q in self.quotes.items() if time.monotonic()-q['received']<=self.args.quote_max_age}
                    if self.state.announcements and not controls.get('allow_historical_model_after_announcement',False):
                        blocked|=set(SYMBOLS)-set(controls.get('fair_value_overrides',{}))
                    if self.state.loss()>1766.2:blocked.add('REIN')
                    if self.state.final_known():
                        fairs=self.state.exact()
                        if not self.state.continuity_ok:blocked|=set(SYMBOLS)-REPORT_ONLY
                    else:
                        if not self.state.continuity_ok:await asyncio.sleep(2);continue
                        x=self.state.features(sec)
                        if self.cal.get('quote_aware'):
                            if not all(s in fresh and fresh[s].get('bid') is not None and fresh[s].get('ask') is not None for s in SYMBOLS):
                                await asyncio.sleep(2);continue
                            mids=np.array([(float(fresh[s]['bid'])+float(fresh[s]['ask']))/2 for s in SYMBOLS])
                            if not np.isfinite(mids).all():raise ValueError('invalid quote midpoint')
                            x=np.r_[x,mids];anchor=mids[:10]
                        else:anchor=terminal_values(self.state.damage(),self.state.hours(),self.state.loss())
                        if len(x)!=self.cal['feature_count']:raise RuntimeError('feature schema mismatch')
                        fairs=add_etfs(bound_stock_values(self.model.predict(x[None,:])[0]+anchor))
                    for s,v in controls.get('fair_value_overrides',{}).items():
                        if s in SYMBOLS:fairs[SYMBOLS.index(s)]=float(v)
                    sigma=np.array(self.cal['sigma'][phase_bucket(self.state.round,sec)])
                    # Other resting orders reserve limits. Don't mix this bot with another
                    # trader in the same symbol without an agreed allocation.
                    blocked|={o['symbol'] for o in orders if o['id'] not in self.owned}
                    plans=decide(fairs,sigma,fresh,pos,self.case['limits'],self.args.max_fraction,risk_multiple=self.cal['risk_multiple'],blocked=blocked)
                    if time.monotonic()-self.last_plan>10:
                        self.last_plan=time.monotonic();self.log('valuation',round=self.state.round,seconds=sec,fairs=dict(zip(SYMBOLS,fairs)),positions=pos,blocked=sorted(blocked),etf_alerts=etf_opportunities(fresh,self.case,pos,orders))
                    # At most one order every cycle. No automatic retry after an uncertain POST.
                    if plans:
                        a=plans[0];order={'symbol':a.symbol,'side':a.side,'quantity':a.quantity,'price':a.price,'order_type':'limit'}
                        if not self.args.execute:self.log('would_order',order=order,edge=a.edge)
                        else:
                            # Fresh position after cancellations; avoid stale-account overshoot.
                            p2=position_map(await self.call('/api/v1/positions'))
                            delta=a.target-p2[a.symbol]
                            if delta and (delta>0)==(a.side=='buy') and time.monotonic()-fresh[a.symbol]['received']<=self.args.quote_max_age:
                                order['quantity']=abs(delta)
                                try:response=await self.call('/api/v1/orders',method='POST',body=order,action=True)
                                except Exception:
                                    self.fault='ambiguous POST; reconcile orders and restart before resuming';raise
                                oid=response.get('order_id',response.get('id')) if isinstance(response,dict) else None
                                if oid is not None:self.owned.add(str(oid))
                                else:
                                    # An unidentifiable possibly resting order is unsafe to repeat.
                                    self.fault='order response has no id; inspect account before restart'
                                self.log('order_response',order=order,response=response)
                except Exception as exc:
                    self.log('blocked',error=str(exc))
                    # Do not clear ambiguous-post/announcement latches automatically.
                await asyncio.sleep(self.args.interval)
        finally:
            feed.cancel()
            if self.args.execute:
                try:await self.cancel_owned(open_order_list(await self.call('/api/v1/orders/open')))
                except Exception as exc:self.log('cleanup_failed',error=str(exc))
            self.logfile.close()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',default=DEFAULT_URL)
    p.add_argument('--execute',action='store_true',help='Send orders to the simulation exchange; dry-run otherwise')
    p.add_argument('--max-fraction',type=float,default=.35);p.add_argument('--action-budget',type=int,default=120)
    p.add_argument('--interval',type=float,default=2.0);p.add_argument('--quote-max-age',type=float,default=5.)
    p.add_argument('--control',default=str(HERE/'control.json'));p.add_argument('--log-dir',default=str(HERE/'logs'))
    args=p.parse_args()
    if args.execute:p.error("This research adapter is read-only in this repository; use the reviewed ctc-bot execution stack.")
    if not os.environ.get('CTC_API_KEY'):p.error('set CTC_API_KEY in your local environment; do not paste it into chat')
    if not 0<args.max_fraction<=1:p.error('max-fraction must be in (0,1]')
    if not 1<=args.action_budget<=120:p.error('action budget must be 1-120; share remaining team budget explicitly')
    if args.interval<1:p.error('interval must be at least one second')
    try:asyncio.run(Bot(args).run())
    except KeyboardInterrupt:pass

if __name__=='__main__':main()
