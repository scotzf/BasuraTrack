# share.ps1 - put Sukod on a public HTTPS address so someone else can test it.
#
# Why this exists: a phone cannot install a progressive web app (PWA) from
# 127.0.0.1, and a service worker only runs over HTTPS. ngrok gives us a real
# HTTPS address that forwards to the local server, satisfying both.
#
# Run it with:   .\share.ps1
# Stop it with:  Ctrl+C  (both processes are cleaned up on exit)

$ErrorActionPreference = "Stop"
$port = 8000

# --- 1. Start Django, bound to 0.0.0.0 so ngrok can reach it ---------------
# --noreload keeps it to a single process, so we can stop it cleanly later.
Write-Host "Starting Django on port $port..." -ForegroundColor Cyan
$django = Start-Process -PassThru -WindowStyle Hidden `
  -FilePath ".venv\Scripts\python.exe" `
  -ArgumentList "manage.py", "runserver", "0.0.0.0:$port", "--noreload"

# --- 2. Start the tunnel ---------------------------------------------------
Write-Host "Opening ngrok tunnel..." -ForegroundColor Cyan
$ngrok = Start-Process -PassThru -WindowStyle Hidden `
  -FilePath "ngrok" -ArgumentList "http", "$port"

# --- 3. Ask ngrok's local API what public address it was given -------------
# ngrok runs a small web interface on 127.0.0.1:4040. Querying it is more
# reliable than scraping the console, and it is the only way to learn the
# address when ngrok is running hidden.
$publicUrl = $null
foreach ($attempt in 1..20) {
    Start-Sleep -Milliseconds 700
    try {
        $api = Invoke-RestMethod "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 2
        $publicUrl = ($api.tunnels | Where-Object { $_.proto -eq "https" })[0].public_url
        if ($publicUrl) { break }
    } catch { }   # not up yet - keep waiting
}

if (-not $publicUrl) {
    Write-Host "ngrok did not report a tunnel. Is it authenticated?" -ForegroundColor Red
    Stop-Process -Id $django.Id, $ngrok.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host ""
Write-Host "  Send your friend this link:" -ForegroundColor Green
Write-Host "  $publicUrl" -ForegroundColor White
Write-Host ""
Write-Host "  Pairing code: SUKOD1"
Write-Host "  On the free plan they must click 'Visit Site' on ngrok's warning"
Write-Host "  page once. Nothing works until they do - not even the logger."
Write-Host ""
Write-Host "  Tunnel inspector (every request, replayable): http://127.0.0.1:4040"
Write-Host "  Ctrl+C to stop both." -ForegroundColor DarkGray

# --- 4. Hold until Ctrl+C, then stop both children -------------------------
try   { while ($true) { Start-Sleep -Seconds 1 } }
finally {
    Write-Host "`nStopping..." -ForegroundColor Cyan
    Stop-Process -Id $django.Id, $ngrok.Id -Force -ErrorAction SilentlyContinue
}
