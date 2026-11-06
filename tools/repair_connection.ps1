$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$bypass=@($env:NO_PROXY -split ',' | Where-Object { $_ }) + @('127.0.0.1','localhost','::1')
$env:NO_PROXY=($bypass | Select-Object -Unique) -join ','
$resultDir = Join-Path $appRoot 'validation/native-connection'
New-Item -ItemType Directory -Path $resultDir -Force | Out-Null
$result = [ordered]@{ time=(Get-Date).ToString('o'); cisco_running=$false; token_valid=$false; bridge_up=$false; connected=$false; error=$null }
try {
    if (-not (Get-Process -Name PacketTracer -ErrorAction SilentlyContinue)) {
        throw 'Cisco закрыт. Откройте его самостоятельно. Автозапуск отключён.'
    }
    $result.cisco_running = $true
    $tokenDir = Join-Path $env:LOCALAPPDATA 'packet-tracer-mcp'
    New-Item -ItemType Directory -Path $tokenDir -Force | Out-Null
    $tokenPath = Join-Path $tokenDir 'bridge_token'
    $bridgeExe = Join-Path $appRoot 'bin/Direct/PacketTracerBridge/PacketTracerBridge.exe'
    if (-not (Test-Path -LiteralPath $bridgeExe)) { $bridgeExe=Join-Path $appRoot 'bin/PacketTracerBridge/PacketTracerBridge.exe' }
    if (-not (Test-Path -LiteralPath $bridgeExe)) { throw 'Не найден PacketTracerBridge.exe рядом с приложением.' }
    $ping = $null
    try { $ping=Invoke-RestMethod 'http://127.0.0.1:54321/ping' -TimeoutSec 2 } catch { }
    if (-not $ping) {
        # Starts only the local helper. PacketTracer.exe is never launched here.
        Start-Process -FilePath $bridgeExe -WorkingDirectory $resultDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $resultDir 'server-out.log') -RedirectStandardError (Join-Path $resultDir 'server-error.log') | Out-Null
    }
    # Only the upstream server creates the token. Never manufacture or replace it here.
    for ($tokenWait=0; $tokenWait -lt 20 -and -not (Test-Path -LiteralPath $tokenPath); $tokenWait++) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Path -LiteralPath $tokenPath)) { throw 'Сервер не создал токен. Проверьте server-error.log.' }
    $token = [IO.File]::ReadAllText($tokenPath).Trim().TrimStart([char]0xFEFF)
    if ($token -notmatch '^[A-Za-z0-9_-]{32,}$') { throw 'Файл токена повреждён. Он сохранён без изменений.' }
    $result.token_valid = $true
    Write-Host 'Файл подключения доступен. Содержимое скрыто.'
    $headers = @{ 'X-PT-Token'=$token }
    Write-Host 'Оставьте Cisco открытым. В модуле выберите Tools → Reload token → Run.'
    Write-Host 'Проверяю связь в течение 90 секунд...'
    for ($attempt=0; $attempt -lt 45; $attempt++) {
        try {
            $status=Invoke-RestMethod 'http://127.0.0.1:54321/status' -Headers $headers -TimeoutSec 2
            $result.bridge_up=$true
            if ($status.connected -eq $true) { $result.connected=$true; break }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if ($result.connected) {
        Write-Host 'ПОДКЛЮЧЕНО. Теперь можно начать выполнение в приложении.' -ForegroundColor Green
    } else {
        Write-Host 'Подключение пока не установлено. Диагностика сохранена в validation/native-connection.' -ForegroundColor Yellow
    }
} catch {
    $result.error=$_.Exception.Message
    Write-Host $result.error -ForegroundColor Red
} finally {
    $result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $resultDir 'result.json') -Encoding UTF8
    Remove-Variable token -ErrorAction SilentlyContinue
}
[void](Read-Host 'Нажмите Enter, чтобы закрыть проверку')
