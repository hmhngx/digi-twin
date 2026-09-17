# Cluster preflight (Windows PowerShell).
# Checks minikube status first and only intervenes if something is actually
# wrong, then waits for pods before saying it's safe to run
# scripts/port-forwards.ps1. See notes/KUBECONFIG_DRIFT_AUDIT.md for why
# this is needed: the minikube container's ports are unpinned, so Docker
# hands it a new random host port for the API server every time it
# restarts, which happens whenever Docker Desktop wasn't left running.
#
# IMPORTANT, found by actually testing this script rather than assuming:
# `minikube start` on an ALREADY-healthy cluster is NOT a safe no-op --
# tested 2026-09-16 against a cluster that had been running fine for 52
# minutes (confirmed via `docker ps`), and it still knocked every pod back
# to not-Ready for several minutes before things settled (nginx and grafana
# took the longest, same pattern as a genuine cold start). So this script
# checks `minikube status` first and only calls `minikube start` when
# something is actually reported Stopped/Misconfigured -- never
# unconditionally.

$ErrorActionPreference = "Stop"

Write-Host "Checking minikube status..."
$statusOutput = minikube status 2>&1 | Out-String
$statusExitCode = $LASTEXITCODE
Write-Host $statusOutput

if ($statusExitCode -eq 0 -and $statusOutput -notmatch "Stopped" -and $statusOutput -notmatch "Misconfigured") {
    Write-Host "Cluster already healthy (kubelet/apiserver Running, kubeconfig Configured) -- skipping minikube start to avoid an unnecessary disruption."
} else {
    Write-Host "Cluster needs recovery. Running minikube start..."
    minikube start
    if ($LASTEXITCODE -ne 0) {
        Write-Error "minikube start failed. Is Docker Desktop running? Investigate manually -- do not proceed to port-forwards."
        exit 1
    }
    Write-Host "Fixing kubeconfig context (this is the actual drift fix)..."
    minikube update-context
}

Write-Host ""
Write-Host "Waiting for all pods in namespace 'default' to be Ready..."
$maxWaitSeconds = 300
$pollIntervalSeconds = 10
$elapsed = 0

while ($true) {
    $podsJson = kubectl get pods -n default -o json 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ($elapsed s) kubectl not reachable yet -- $podsJson"
    } else {
        $pods = ($podsJson | Out-String | ConvertFrom-Json).items
        if ($pods.Count -eq 0) {
            Write-Host "  ($elapsed s) no pods found in namespace 'default' yet"
        } else {
            $notReady = @()
            foreach ($pod in $pods) {
                $statuses = $pod.status.containerStatuses
                $allReady = $statuses -and (@($statuses | Where-Object { -not $_.ready }).Count -eq 0)
                if (-not $allReady) { $notReady += $pod.metadata.name }
            }
            if ($notReady.Count -eq 0) {
                Write-Host "All $($pods.Count) pods Ready."
                break
            }
            Write-Host "  ($elapsed s) not ready yet ($($notReady.Count)/$($pods.Count)): $($notReady -join ', ')"
        }
    }

    if ($elapsed -ge $maxWaitSeconds) {
        Write-Warning "Timed out after $maxWaitSeconds s with pods still not Ready. This can happen on a genuinely cold start (Grafana plugin downloads, etc. -- see README.md 'Windows operator run cold start'). Check 'kubectl get pods -n default' by hand before running port-forwards.ps1."
        break
    }
    Start-Sleep -Seconds $pollIntervalSeconds
    $elapsed += $pollIntervalSeconds
}

Write-Host ""
Write-Host "Preflight complete. Safe to run .\scripts\port-forwards.ps1 now."
