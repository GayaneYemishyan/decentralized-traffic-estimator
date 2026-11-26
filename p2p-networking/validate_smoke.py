import base64, json, urllib.request as u, urllib.error as e

def send(body, method="POST"):
    r = u.Request("http://127.0.0.1:8080/publish", body, method=method)
    try:
        return u.urlopen(r).status
    except e.HTTPError as x:
        return x.code

b64 = lambda b: base64.b64encode(b).decode()
print("bad base64      :", send(json.dumps({"data": "!!!"}).encode()), "expect 400")
print("not json        :", send(json.dumps({"data": b64(b"not json")}).encode()), "expect 400")
print("empty data      :", send(json.dumps({"data": ""}).encode()), "expect 400")
print("bad body        :", send(b"garbage"), "expect 400")
print("GET             :", send(None, "GET"), "expect 405")
print("valid           :", send(json.dumps({"data": b64(b'{"ok":1}')}).encode()), "expect 200")