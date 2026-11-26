import base64, json, urllib.request, urllib.error

def post(port, raw: bytes):
    body = json.dumps({"data": base64.b64encode(raw).decode()}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/publish", body, method="POST")
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code

def get(port, path):
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}{path}"))

print("oversize:", post(8080, b'"' + b"a" * 70000 + b'"'))   # expect 400

for i in range(1100):
    post(8080, json.dumps({"i": i}).encode())
r = get(8081, "/subscribe?cursor=1&max=1&timeout_ms=1000")
print("dropped:", r["dropped"], "first cursor:", r["messages"][0]["cursor"])