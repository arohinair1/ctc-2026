"""Read-only API inspection; never sends an order or conversion."""
import argparse,json,os
from bot import API,DEFAULT_URL

def main():
    p=argparse.ArgumentParser();p.add_argument('--url',default=DEFAULT_URL);args=p.parse_args()
    key=os.environ.get('CTC_API_KEY')
    if not key:p.error('set CTC_API_KEY locally')
    api=API(args.url,key)
    for path in ['/api/v1/competitions/time','/api/v1/positions','/api/v1/orders/open','/api/v1/orderbook/PORT']:
        print(path);print(json.dumps(api.request(path),indent=2))

if __name__=='__main__':main()
