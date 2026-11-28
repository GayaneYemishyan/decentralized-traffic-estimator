import base64, json, urllib.request as u, urllib.error as e

def send(body, method="POST"):
    r = u.Request("http://127.0.0.1:8080/publish", body, method=method)
    try:
        return u.urlopen(r).status
    except e.HTTPError as x:
        return x.code

b64 = lambda b: base64.b64encode(b).decode()
fails = 0

def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{name:16}: {got} {'ok' if ok else f'FAIL (want {want})'}")

check("bad base64", send(json.dumps({"data": "!!!"}).encode()), 400)
check("not json", send(json.dumps({"data": b64(b"not json")}).encode()), 400)
check("empty data", send(json.dumps({"data": ""}).encode()), 400)
check("bad body", send(b"garbage"), 400)
check("GET", send(None, "GET"), 405)
check("valid", send(json.dumps({"data": b64(b'{"ok":1}')}).encode()), 200)
raise SystemExit(1 if fails else 0)