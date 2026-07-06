import json, urllib.request, time, sys
API="https://ca-api-fsi-demo.orangecoast-891e69ba.eastus2.azurecontainerapps.io"
sc=sys.argv[1]
body=json.dumps({"scenario":sc}).encode()
req=urllib.request.Request(API+"/api/run",data=body,headers={"Content-Type":"application/json"})
arts=[];agents=[];errs=[];t0=time.time()
with urllib.request.urlopen(req,timeout=1200) as r:
    for raw in r:
        line=raw.decode("utf-8").strip()
        if not line.startswith("data:"): continue
        ev=json.loads(line[5:].strip());t=ev.get("type")
        if t=="agent_start": agents.append(ev["agent"])
        elif t=="artifact": arts.append(ev.get("filename"))
        elif t=="error": errs.append(ev.get("message"))
print(sc,"| time",int(time.time()-t0),"s | AGENTS",agents,"| ARTIFACTS",arts,"| ERRORS",errs)
