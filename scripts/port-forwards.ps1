# OpenTwins local port-forwards (Windows PowerShell).
# Run from an elevated-enough shell with kubectl context set to minikube.
# Keep this window open; each forward runs in the background of this session.

$ErrorActionPreference = "Stop"

$forwards = @(
    @{ svc = "svc/opentwins-ditto-extended-api"; local = 8080; remote = 8080 },
    @{ svc = "svc/opentwins-ditto-nginx";        local = 8081; remote = 8080 },
    @{ svc = "svc/opentwins-grafana";           local = 3000; remote = 80 },
    @{ svc = "svc/opentwins-influxdb2";         local = 8086; remote = 80 },
    @{ svc = "svc/opentwins-mosquitto";         local = 1883; remote = 1883 }
)

foreach ($f in $forwards) {
    Write-Host "Starting port-forward $($f.svc) localhost:$($f.local) -> $($f.remote)"
    Start-Process -NoNewWindow -FilePath "kubectl" -ArgumentList @(
        "port-forward", $f.svc, "$($f.local):$($f.remote)"
    )
}

Write-Host ""
Write-Host "Port-forwards started in background processes."
Write-Host "  Extended API : http://localhost:8080"
Write-Host "  Ditto HTTP   : http://localhost:8081"
Write-Host "  Grafana      : http://localhost:3000"
Write-Host "  InfluxDB     : http://localhost:8086"
Write-Host "  Mosquitto    : localhost:1883"
Write-Host "Stop them via Task Manager or: Get-Process kubectl | Stop-Process"
