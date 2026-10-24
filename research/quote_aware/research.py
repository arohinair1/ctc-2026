"""Reproducible season-disjoint fit and executable-cost proxy evaluation.

python research.py --data /path/to/hurricane-season-research
Train 1-400, calibration/selection 401-500, evaluation 501-600.
No order books were supplied: all PnL here is explicitly a proxy, not realized.
"""
from __future__ import annotations
import argparse,csv,json,time
from pathlib import Path
import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesRegressor
from core import *

HERE=Path(__file__).resolve().parent

def build_dataset(root):
    case=json.loads((root/'case.json').read_text())
    seasons=json.loads((root/'history/manifest.json').read_text())['seasons']
    X=[];Y=[];anchors=[];prices=[];exact=[];ids=[];rounds=[];times=[];truth=[]
    adviser=[];terminal_errors=[]
    for season in seasons:
        folder=root/'history'/season['dir'];state=State(case)
        feed=[json.loads(l) for l in (folder/'feed.jsonl').read_text().splitlines()]
        settlement=json.loads((folder/'settlement.json').read_text())
        target=np.array([settlement[s] for s in STOCKS])
        full_target=np.array([settlement[s] for s in SYMBOLS])
        ptr=0
        with (folder/'market.csv').open() as f:
            market=list(csv.DictReader(f))
        for row in market:
            sec=seconds_from_open(row['time'])
            # every ten real seconds, including both preliminary and final reports.
            if round(sec)%10:continue
            r=int(row['round']);state.begin_round(r)
            while ptr<len(feed) and feed[ptr]['time']<=row['time']:
                state.ingest(feed[ptr]);ptr+=1
            if r==1 and sec<60:state.start_seen=state.continuity_ok=True
            anchor=terminal_values(state.damage(),state.hours(),state.loss())
            X.append(state.features(sec));Y.append(target-anchor);anchors.append(anchor)
            prices.append([float(row[s]) for s in SYMBOLS]);exact.append(state.exact())
            truth.append(full_target);ids.append(season['season']);rounds.append(r);times.append(sec)
        terminal_errors.append(state.exact()-full_target)
        for st in season['storms']:
            for m in feed:
                if m['round']==st['round'] and m['type']=='advisory':
                    true='erratic' if st['turned_erratic_at'] is not None and m['age']>=st['turned_erratic_at'] else st['pattern']
                    adviser.append([season['season'],m['source'],m['age'],m['outlook'],true])
        if season['season']%100==0:print('loaded',season['season'],flush=True)
    data={k:np.asarray(v) for k,v in dict(X=X,Y=Y,anchors=anchors,prices=prices,exact=exact,ids=ids,rounds=rounds,times=times,truth=truth,terminal_errors=terminal_errors).items()}
    np.savez_compressed(HERE/'dataset.npz',**data)
    (HERE/'advisory_stats_raw.json').write_text(json.dumps(adviser))
    return data

def predictive_fairs(model,data,mask):
    pred=add_etfs(model.predict(data['X'][mask])+data['anchors'][mask])
    final=(data['rounds'][mask]==3)&(data['times'][mask]>=740)
    pred[final]=data['exact'][mask][final]
    return pred

def calibration(data,pred,mask):
    buckets=np.array([phase_bucket(r,t) for r,t in zip(data['rounds'][mask],data['times'][mask])])
    result={}
    for b in sorted(set(buckets)):
        err=pred[buckets==b]-data['truth'][mask][buckets==b]
        result[b]=np.maximum(.05,np.sqrt(np.mean(err**2,axis=0))).tolist()
    return result

def simulate(data,mask,pred,cal,case,cost=.20,fraction=.35,risk=.35,lag=10,late_only=False):
    ids=data['ids'][mask];rs=data['rounds'][mask];ts=data['times'][mask];prices=data['prices'][mask];truth=data['truth'][mask]
    outcomes=[];rows=[]
    for season in sorted(set(ids)):
        idx=np.flatnonzero(ids==season);position={s:0 for s in SYMBOLS};cash=0.;orders=0;turnover=0.;skips=0;by_symbol=np.zeros(12);pending=[];action_times=deque();max_abs=np.zeros(12)
        for k in idx:
            absolute=(int(rs[k])-1)*780+ts[k]
            # Previously decided limits meet the next sampled consensus plus cost.
            for action,ready in list(pending):
                if absolute<ready:continue
                pending.remove((action,ready));j=SYMBOLS.index(action.symbol)
                execution=prices[k,j]+(cost if action.side=='buy' else -cost)
                if (action.side=='buy' and execution<=action.price+1e-9) or (action.side=='sell' and execution>=action.price-1e-9):
                    signed=action.quantity*(1 if action.side=='buy' else -1)
                    position[action.symbol]+=signed;cash-=signed*execution;by_symbol[j]-=signed*execution;turnover+=abs(signed)
                # Unfilled limits are canceled next decision; reserve a cancel action.
                else:orders+=1;action_times.append(absolute)
            if late_only and not (rs[k]==3 and ts[k]>=740):continue
            sigma=cal[phase_bucket(int(rs[k]),ts[k])]
            quotes={s:{'bid':prices[k,j]-cost,'ask':prices[k,j]+cost} for j,s in enumerate(SYMBOLS)}
            decisions=decide(pred[k],sigma,quotes,position,case['limits'],max_fraction=fraction,risk_multiple=risk)
            pending_symbols={a.symbol for a,_ in pending}
            # A live loop can place at most five orders in ten seconds at its
            # two-second cadence. Do not simulate all twelve at once.
            for a in decisions[:5]:
                if a.symbol in pending_symbols:continue
                while action_times and action_times[0]<=absolute-60:action_times.popleft()
                if len(action_times)>=120:skips+=1;continue
                orders+=1;action_times.append(absolute)
                if lag:
                    pending.append((a,absolute+lag));pending_symbols.add(a.symbol)
                else:
                    j=SYMBOLS.index(a.symbol);signed=a.quantity*(1 if a.side=='buy' else -1)
                    position[a.symbol]+=signed;cash-=signed*a.price;by_symbol[j]-=signed*a.price;turnover+=abs(signed)
            max_abs=np.maximum(max_abs,np.abs([position[s] for s in SYMBOLS]))
        p=np.array([position[s] for s in SYMBOLS]);by_symbol+=p*truth[idx[-1]]
        pnl=cash+p@truth[idx[-1]];outcomes.append(pnl)
        rows.append({'season':int(season),'pnl':float(pnl),'orders_including_cancel':orders,'share_turnover':turnover,'rate_skips':skips,**{f'pnl_{s}':float(by_symbol[j]) for j,s in enumerate(SYMBOLS)}})
        if np.any(max_abs>np.array([case['limits'][s] for s in SYMBOLS])*fraction+1):raise AssertionError('position cap breached')
    p=np.array(outcomes)
    return {'mean':float(p.mean()),'median':float(np.median(p)),'p05':float(np.percentile(p,5)),'worst':float(p.min()),'best':float(p.max()),'win_rate':float(np.mean(p>0)),'n':len(p),'cost_per_share_per_side':cost,'fill_delay_real_seconds':lag,'max_position_fraction':fraction,'late_only':late_only},rows

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',type=Path,required=True);parser.add_argument('--rebuild',action='store_true');args=parser.parse_args()
    case=json.loads((args.data/'case.json').read_text())
    path=HERE/'dataset.npz'
    data=dict(np.load(path)) if path.exists() and not args.rebuild else build_dataset(args.data)
    train=data['ids']<=400;val=(data['ids']>400)&(data['ids']<=500);test=data['ids']>500
    print('fitting',train.sum(),'rows;',data['X'].shape[1],'features',flush=True)
    model=ExtraTreesRegressor(n_estimators=80,max_depth=17,min_samples_leaf=35,max_features=.8,n_jobs=2,random_state=20261024)
    model.fit(data['X'][train],data['Y'][train])
    joblib.dump(model,HERE/'model.joblib',compress=3)
    vp=predictive_fairs(model,data,val);cal=calibration(data,vp,val)
    # Select only on validation seasons; the test set is not in this loop.
    selections=[]
    for risk in [.25,.5,1.0]:
        result,_=simulate(data,val,vp,cal,case,cost=.20,risk=risk)
        selections.append({'risk_multiple':risk,**result})
    risk=max(selections,key=lambda r:r['mean']+.25*r['p05'])['risk_multiple']
    tp=predictive_fairs(model,data,test)
    report={'split':{'training':'1-400','validation':'401-500','evaluation':'501-600'},'seed':20261024,'symbols':SYMBOLS,'selected_risk_multiple':risk,'validation_selection':selections,'limitations':['House consensus is not an executable price.','No order books, depth, fills or own market impact supplied.','Costs are scenarios, not calibrated exchange spreads.','Historical seasons contain no announcements.','Model not connected to testnet; API response adapters require dry-run verification.']}
    report['terminal_formula_mae_evaluation']=dict(zip(SYMBOLS,np.mean(abs(data['terminal_errors'][500:]),axis=0).tolist()))
    report['valuation_mae']={}
    for name,mask,pred in [('validation',val,vp),('evaluation',test,tp)]:
        for r in (1,2,3):
            a=(data['rounds'][mask]==r)
            report['valuation_mae'][f'{name}_round_{r}']={'model':dict(zip(SYMBOLS,np.mean(abs(pred[a]-data['truth'][mask][a]),axis=0).tolist())),'house':dict(zip(SYMBOLS,np.mean(abs(data['prices'][mask][a]-data['truth'][mask][a]),axis=0).tolist()))}
    report['evaluation_proxy_pnl']=[]
    for cost,lag,late in [(.05,10,False),(.20,10,False),(.50,10,False),(1.,10,False),(.20,0,False),(.20,10,True)]:
        stats,rows=simulate(data,test,tp,cal,case,cost=cost,risk=risk,lag=lag,late_only=late)
        report['evaluation_proxy_pnl'].append(stats)
        if cost==.2 and lag==10 and not late:
            with (HERE/'evaluation_seasons.csv').open('w') as f:
                writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    adv=json.loads((HERE/'advisory_stats_raw.json').read_text())
    report['source_accuracy_training']={}
    for source in ['official','wire']:
        a=[m for m in adv if m[0]<=400 and m[1]==source]
        confusion={true:{pred:0 for pred in OUTLOOKS} for true in OUTLOOKS}
        for _,_,_,pred,true in a:confusion[true][pred]+=1
        report['source_accuracy_training'][source]={'n':len(a),'accuracy':sum(m[3]==m[4] for m in a)/len(a),'confusion_true_rows':confusion}
    artifact={'sigma':cal,'risk_multiple':risk,'training_seasons':400,'feature_count':data['X'].shape[1],'max_position_fraction':.35,'model_format':'scikit-learn 1.8.0 joblib; load only this trusted artifact'}
    (HERE/'calibration.json').write_text(json.dumps(artifact,indent=2));(HERE/'research_results.json').write_text(json.dumps(report,indent=2))
    np.savez_compressed(HERE/'evaluation_predictions.npz',ids=data['ids'][test],rounds=data['rounds'][test],seconds=data['times'][test],prediction=tp,truth=data['truth'][test],house=data['prices'][test])
    print(json.dumps({'terminal_mae':report['terminal_formula_mae_evaluation'],'selection':selections,'proxy':report['evaluation_proxy_pnl']},indent=2),flush=True)

if __name__=='__main__':main()
