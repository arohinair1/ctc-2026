"""Quote-aware challenger. Selection uses validation seasons only.

The first independently forecast model is retained as a documented rejected
benchmark. This script adds observable consensus prices, then predicts only
their valuation error. A separate true holdout is not created by this change.
"""
import json,sys
from pathlib import Path
import joblib,numpy as np
from sklearn.ensemble import ExtraTreesRegressor
from research import calibration,simulate
from core import *
H=Path(__file__).parent
d=dict(np.load(H/'dataset.npz'));case=json.loads((H/'case.json').read_text())
train=d['ids']<=400;val=(d['ids']>400)&(d['ids']<=500);test=d['ids']>500
# No calendar or season identifier enters the model; groups only define splits.
X=np.c_[d['X'],d['prices']]
y=d['truth'][:,:10]-d['prices'][:,:10]
model=ExtraTreesRegressor(n_estimators=100,max_depth=18,min_samples_leaf=70,max_features=.85,n_jobs=2,random_state=20261025)
print('fitting quote-aware model',X[train].shape,flush=True)
if '--reuse-model' in sys.argv:model=joblib.load(H/'model.joblib')
else:model.fit(X[train],y[train])
vp=add_etfs(bound_stock_values(model.predict(X[val])+d['prices'][val,:10]));final=(d['rounds'][val]==3)&(d['times'][val]>=740);vp[final]=d['exact'][val][final]
cal=calibration(d,vp,val)
selections=[]
for risk in [.1,.25,.5,1.]:
 stats,_=simulate(d,val,vp,cal,case,cost=.2,risk=risk)
 selections.append({'risk_multiple':risk,**stats})
print('VALIDATION',json.dumps(selections),flush=True)
risk=max(selections,key=lambda r:r['mean']+.25*r['p05'])['risk_multiple']
tp=add_etfs(bound_stock_values(model.predict(X[test])+d['prices'][test,:10]));final=(d['rounds'][test]==3)&(d['times'][test]>=740);tp[final]=d['exact'][test][final]
results=json.loads((H/'research_results.json').read_text())
if 'rejected_independent_forecast' not in results:results['rejected_independent_forecast']= {'validation_selection':results['validation_selection'],'evaluation_proxy_pnl':results['evaluation_proxy_pnl'],'valuation_mae':results['valuation_mae']}
results['selected_model']='quote-aware ExtraTrees valuation-error predictor';results['validation_selection']=selections;results['selected_risk_multiple']=risk
results['valuation_mae']={}
for name,mask,pred in [('validation',val,vp),('evaluation',test,tp)]:
 for r in (1,2,3):
  a=d['rounds'][mask]==r
  results['valuation_mae'][f'{name}_round_{r}']={'model':dict(zip(SYMBOLS,np.mean(abs(pred[a]-d['truth'][mask][a]),axis=0).tolist())),'house':dict(zip(SYMBOLS,np.mean(abs(d['prices'][mask][a]-d['truth'][mask][a]),axis=0).tolist()))}
results['evaluation_proxy_pnl']=[]
for cost,lag,late in [(.05,10,False),(.20,10,False),(.50,10,False),(1.,10,False),(.20,0,False),(.20,10,True)]:
 stats,rows=simulate(d,test,tp,cal,case,cost=cost,risk=risk,lag=lag,late_only=late);results['evaluation_proxy_pnl'].append(stats)
 if cost==.2 and lag==10 and not late:
  import csv
  with (H/'evaluation_seasons.csv').open('w') as f:
   writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
joblib.dump(model,H/'model.joblib',compress=3)
(H/'calibration.json').write_text(json.dumps({'sigma':cal,'risk_multiple':risk,'training_seasons':400,'feature_count':X.shape[1],'quote_aware':True,'max_position_fraction':.35,'model_format':'scikit-learn 1.8.0 joblib'},indent=2))
(H/'research_results.json').write_text(json.dumps(results,indent=2))
np.savez_compressed(H/'evaluation_predictions.npz',ids=d['ids'][test],rounds=d['rounds'][test],seconds=d['times'][test],prediction=tp,truth=d['truth'][test],house=d['prices'][test])
print(json.dumps({'selected_risk':risk,'proxy':results['evaluation_proxy_pnl']},indent=2),flush=True)
