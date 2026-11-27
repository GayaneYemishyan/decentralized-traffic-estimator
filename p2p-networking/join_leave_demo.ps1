# Join/leave demo for P2P-3. Start the cluster first: .\run_local_cluster.ps1
# Stops node2, restarts it, and checks topic_peers on node1 goes 2 -> 1 -> 2 and
# that messages flow again after the rejoin. Exits non-zero on failure.
Set-Location $PSScriptRoot
$Node1 = "http://127.0.0.1:8080"
$Node2 = "http://127.0.0.1:8081"
$Node3 = "http://127.0.0.1:8082"   

function Get-TopicPeers($base) {
    try { (Invoke-RestMethod "$base/health").topic_peers } catch { -1 }
}

function Wait-For($want, $timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    $got = -1
    while ((Get-Date) -lt $deadline) {
        $got = Get-TopicPeers $Node1
        if ($got -eq $want) { return }
        Start-Sleep 1
    }
    Write-Host "FAIL: node1 topic_peers did not reach $want (last: $got)"
    exit 1
}

$t0 = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

Wait-For 2 30   
Write-Host "before : node1 topic_peers=$(Get-TopicPeers $Node1)"

docker compose stop node2
if ($LASTEXITCODE -ne 0) { exit 1 }
Wait-For 1 30  
Write-Host "stopped: node1 topic_peers=$(Get-TopicPeers $Node1)"

docker compose start node2
if ($LASTEXITCODE -ne 0) { exit 1 }
Wait-For 2 60   
Write-Host "started: node1 topic_peers=$(Get-TopicPeers $Node1)"

# Messages must flow again after the rejoin (node2's bridge restarted, so its ring is empty).
# node3 was never stopped, so it checks that the survivors were unaffected.
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('{"demo":"rejoin"}'))
Invoke-RestMethod -Method Post -Uri "$Node1/publish" -ContentType "application/json" `
    -Body (@{ data = $b64 } | ConvertTo-Json -Compress) | Out-Null
foreach ($n in @($Node2, $Node3)) {   # CHANGED
    $resp = Invoke-RestMethod "$n/subscribe?cursor=1&timeout_ms=5000"
    if (@($resp.messages | Where-Object { $_.data -eq $b64 }).Count -ge 1) {
        Write-Host "message after rejoin: received on $n"
    } else {
        Write-Host "FAIL: $n did not receive the message after rejoining"
        exit 1
    }
}

Write-Host "--- node1 join/leave log lines"
docker compose logs --since $t0 node1 2>&1 | Select-String "peer (joined|left)"
Write-Host "PASS"