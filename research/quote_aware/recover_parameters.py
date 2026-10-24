"""Recover observed-regime stock valuation coefficients; no network access."""
import argparse,json
from pathlib import Path
import numpy as np
from scipy.optimize import least_squares
from core import *

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',type=Path,required=True);args=p.parse_args()
    case=json.loads((args.data/'case.json').read_text());seasons=json.loads((args.data/'history/manifest.json').read_text())['seasons']
    D=[];H=[];L=[];Y=[]
    for season in seasons:
        folder=args.data/'history'/season['dir'];s=State(case)
        for line in (folder/'feed.jsonl').read_text().splitlines():s.ingest(json.loads(line))
        D.append(s.damage());H.append(s.hours());L.append(s.loss())
        settle=json.loads((folder/'settlement.json').read_text());Y.append([settle[k] for k in STOCKS])
    d,h,l,y=map(np.asarray,(D,H,L,Y));out={}
    for j,sym in enumerate(PHYSICAL):
        X=np.c_[np.ones(600),d[:,j],h[:,j]];c=np.linalg.lstsq(X[:400],y[:400,j],rcond=None)[0]
        out[sym]={'intercept_damage_outage_coefficients':c.tolist(),'validation_mae':float(np.mean(abs((X@c-y[:,j])[400:500])))}
    c=np.linalg.lstsq(d[:400],l[:400],rcond=None)[0];out['loss_coefficients_per_damage_point']=c.tolist()
    for sym,j,guess in [('HAVEN',8,[75,.06,400,900]),('REIN',9,[90,.035,1300,1000])]:
        def f(p):return p[0]-p[1]*np.clip(l-p[2],0,p[3])
        fit=least_squares(lambda p:f(p)[:400]-y[:400,j],guess,bounds=([0,0,0,0],[200,1,5000,5000]))
        out[sym]={'base_slope_attachment_width':fit.x.tolist(),'validation_mae':float(np.mean(abs((f(fit.x)-y[:,j])[400:500]))),'note':'REIN upper width is not identified; do not interpret the fitted width.' if sym=='REIN' else ''}
    for sym,j in [('RECON',5),('MAT',6),('GEN',7)]:
        X=np.c_[d,h[:,3]]
        def f(p):return p[0]+p[1]*(-np.expm1(-X@p[2:]))
        fit=least_squares(lambda p:f(p)[:400]-y[:400,j],np.r_[30,30,np.full(6,.005)],bounds=(np.zeros(8),np.r_[100,200,np.ones(6)]),max_nfev=3000)
        out[sym]={'base_capacity_damage5_gridhours_coefficients':fit.x.tolist(),'validation_mae':float(np.mean(abs((f(fit.x)-y[:,j])[400:500])))}
    Path('parameter_fit.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))

if __name__=='__main__':main()
