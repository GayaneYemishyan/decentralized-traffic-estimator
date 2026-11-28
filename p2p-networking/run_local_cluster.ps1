$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force keys | Out-Null
$id = go run . -key keys/node1.key -print-id
if ($LASTEXITCODE -ne 0) { exit 1 }
foreach ($n in "node2", "node3") {
    go run . -key "keys/$n.key" -print-id | Out-Null
    if ($LASTEXITCODE -ne 0) { exit 1 }
}
"NODE1_ID=$id" | Out-File -Encoding ascii .env
docker compose up --build