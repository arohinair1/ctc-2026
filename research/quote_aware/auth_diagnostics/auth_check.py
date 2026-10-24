"""Read-only CTC authentication checks. Never places/cancels orders or creates keys.

Public checks: python auth_check.py --public-only
Key checks:   set CTC_API_KEY locally, then python auth_check.py
Requires websockets>=14 only for the optional WebSocket check.
"""
from __future__ import annotations
import argparse,asyncio,json,os,sys,urllib.error,urllib.parse,urllib.request
from datetime import datetime,timezone

USER_AGENT='ctc-auth-diagnostic/1.0'

class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        # Never forward the API key to another URL or origin implicitly.
        return None

def normalize_origin(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
        raise ValueError('--url must be an HTTPS origin without path, credentials, query or fragment')
    return f'{p.scheme}://{p.netloc}'

def redact(value,key):
    if isinstance(value,dict):
        return {k:(v if k=='key_sent' and isinstance(v,bool) else '[redacted]' if any(s in k.lower() for s in ('token','secret','key','cookie','authorization')) else redact(v,key)) for k,v in value.items()}
    if isinstance(value,list):return [redact(x,key) for x in value]
    if isinstance(value,str):return value.replace(key,'[redacted]') if key else value
    return value

def http_check(origin,path,key=''):
    headers={'User-Agent':USER_AGENT,'Accept':'application/json'}
    if key:headers['X-API-Key']=key
    req=urllib.request.Request(origin+path,headers=headers,method='GET')
    opener=urllib.request.build_opener(NoRedirects())
    try:
        response=opener.open(req,timeout=12)
    except urllib.error.HTTPError as exc:
        response=exc
    except Exception as exc:
        return {'path':path,'key_sent':bool(key),'transport_error':type(exc).__name__}
    with response:
        status=response.code;ctype=response.headers.get('Content-Type','')
        raw=response.read(16384).decode('utf-8',errors='replace')
    result={'path':path,'key_sent':bool(key),'status':status,'content_type':ctype}
    try:
        data=json.loads(raw)
        result['json_type']=type(data).__name__
        if isinstance(data,dict):result['top_level_fields']=list(data)[:25]
        if status>=400:result['error']=redact(data,key)
    except ValueError:
        result['json_type']=None
        if status>=400:result['error']='Non-JSON response; inspect locally without sharing headers or cookies.'
    if 300<=status<400:result['diagnosis']='Redirect refused; confirm the canonical host before sending a key.'
    elif status==401:result['diagnosis']='Expected without a key; with a key, this endpoint rejected authentication.'
    elif status==403:result['diagnosis']='Access denied; this alone does not establish a bad key or a server-wide outage.'
    elif status==404:result['diagnosis']='Path not found on this host.'
    elif status==429:result['diagnosis']='Rate limited; stop and retry after the server permits it.'
    elif status>=500:result['diagnosis']='Server error; retain this timestamp and endpoint for the organizer.'
    return result

async def ws_check(origin,key):
    try:
        import websockets
    except ImportError:
        return {'status':'skipped','reason':'Install websockets>=14 to test the feed.'}
    url=origin.replace('https://','wss://',1)+'/ws/v1/market-data'
    try:
        async with websockets.connect(url,additional_headers={'X-API-Key':key},user_agent_header=USER_AGENT,open_timeout=12) as ws:
            await ws.send(json.dumps({'action':'subscribe','symbols':[],'channels':['news']}))
            try:
                frame=await asyncio.wait_for(ws.recv(),timeout=12)
            except asyncio.TimeoutError:
                return {'status':'handshake_accepted','feed':'No frame within 12 seconds; a quiet/paused market can do this.'}
            data=json.loads(frame)
            return {'status':'handshake_accepted','first_frame_type':data.get('type'),'message_type':data.get('message',{}).get('type') if isinstance(data.get('message'),dict) else None}
    except Exception as exc:
        response=getattr(exc,'response',None)
        return {'status':'failed','exception':type(exc).__name__,'http_status':getattr(response,'status_code',None)}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url',default='https://cornelltradingcompetition.org');p.add_argument('--public-only',action='store_true');p.add_argument('--skip-ws',action='store_true');p.add_argument('--out',default='auth_result.json');args=p.parse_args()
    try:origin=normalize_origin(args.url)
    except ValueError as exc:p.error(str(exc))
    raw='' if args.public_only else os.environ.get('CTC_API_KEY','');key=raw.strip()
    if not args.public_only and not key:p.error('Set CTC_API_KEY locally, or use --public-only. Do not send the key in chat.')
    if key and ((key[0] in '\"\'' or key[-1] in '\"\'') or any(c.isspace() for c in key) or not key.isascii()):p.error('The key contains quotes, internal whitespace or non-ASCII characters; check the copied raw key locally.')
    report={'checked_at_utc':datetime.now(timezone.utc).isoformat(),'origin':origin,'public_only':args.public_only,'outer_whitespace_removed':raw!=key,'requests':[]}
    report['requests'].append(http_check(origin,'/cases/hurricane.json'))
    for path in ['/api/v1/competitions/time','/api/v1/positions']:
        report['requests'].append(http_check(origin,path,key))
    if key and not args.skip_ws:report['websocket']=asyncio.run(ws_check(origin,key))
    report=redact(report,key)
    with open(args.out,'w') as f:json.dump(report,f,indent=2)
    print(json.dumps(report,indent=2))
    if key and any(x.get('status')!=200 for x in report['requests'][1:]):sys.exit(2)

if __name__=='__main__':main()
