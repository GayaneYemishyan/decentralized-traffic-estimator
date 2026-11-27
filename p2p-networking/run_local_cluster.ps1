New-Item -ItemType Directory -Force keys | Out-Null
$id = go run . -key keys/node1.key -print-id
go run . -key keys/node2.key -print-id | Out-Null
go run . -key keys/node3.key -print-id | Out-Null
"NODE1_ID=$id" | Out-File -Encoding ascii .env
docker compose up --build