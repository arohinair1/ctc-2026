"""Run the causal valuation engine on ONE supplied historical season offline."""
import argparse,csv,json
from pathlib import Path
import joblib,numpy as np
from core import *

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',type=Path,required=True);p.add_argument('--season',type=int,default=501);p.add_argument('--output',type=Path,default=Path('replay_values.csv'));args=p.parse_args()
    here=Path(__file__).resolve().parent;state=State(json.loads((here/'case.json').read_text()));model=joblib.load(here/'model.joblib');cal=json.loads((here/'calibration.json').read_text())
    folder=args.data/'history'/f'season-{args.season:03d}'
    feed=[json.loads(l) for l in (folder/'feed.jsonl').read_text().splitlines()];ptr=0
    with (folder/'market.csv').open() as inp,args.output.open('w') as out:
        writer=csv.writer(out);writer.writerow(['time','round','phase',*[f'fair_{s}' for s in SYMBOLS],*[f'house_{s}' for s in SYMBOLS]])
        for row in csv.DictReader(inp):
            sec=seconds_from_open(row['time'])
            if round(sec)%10:continue
            state.begin_round(int(row['round']))
            while ptr<len(feed) and feed[ptr]['time']<=row['time']:state.ingest(feed[ptr]);ptr+=1
            x=state.features(sec);anchor=terminal_values(state.damage(),state.hours(),state.loss())
            if cal.get('quote_aware'):
                mids=np.array([float(row[s]) for s in SYMBOLS]);x=np.r_[x,mids];anchor=mids[:10]
            fair=state.exact() if state.final_known() else add_etfs(bound_stock_values(model.predict(x[None,:])[0]+anchor))
            writer.writerow([row['time'],row['round'],phase_bucket(state.round,sec),*np.round(fair,4),*[row[s] for s in SYMBOLS]])
    print(args.output)

if __name__=='__main__':main()
