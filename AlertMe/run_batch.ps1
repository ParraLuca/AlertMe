# --- run_batch.ps1 ---
# Répertoire du projet
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# Fichier de log (défini tôt pour logger le git pull)
$Log    = Join-Path $Root "run_batch.log"

# --- SIMPLE GIT PULL AU DÉBUT ---
if (Test-Path (Join-Path $Root ".git")) {
  "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Git pull (début)..." | Tee-Object -FilePath $Log -Append | Out-Null
  git pull --ff-only 2>&1 | Tee-Object -FilePath $Log -Append
  "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Git pull (fin)." | Tee-Object -FilePath $Log -Append | Out-Null
} else {
  "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Pas de dépôt Git détecté (./.git introuvable). Skip pull." | Tee-Object -FilePath $Log -Append | Out-Null
}

# Python du venv
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Batch  = Join-Path $Root "batch_alertme.py"
$Config = Join-Path $Root "alerts.jsonl"
$Lock   = Join-Path $Root "run_batch.lock"

# (Optionnel) niveau de logs
$env:LOG_LEVEL = "INFO"

# Anti-chevauchement simple via lockfile
if (Test-Path $Lock) {
  $age = (Get-Item $Lock).LastWriteTime
  if ((Get-Date) - $age -lt (New-TimeSpan -Minutes 55)) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Déjà en cours (lock récent). Skip." | Tee-Object -FilePath $Log -Append | Out-Null
    exit 0
  } else {
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
  }
}
New-Item $Lock -ItemType File -Force | Out-Null

try {
  "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Démarrage batch..." | Tee-Object -FilePath $Log -Append | Out-Null
  & $Python $Batch --config $Config --default-pages 2 2>&1 | Tee-Object -FilePath $Log -Append
  "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Fin batch." | Tee-Object -FilePath $Log -Append | Out-Null
}
catch {
  "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Exception: $($_.Exception.Message)" | Tee-Object -FilePath $Log -Append | Out-Null
  exit 1
}
finally {
  Remove-Item $Lock -Force -ErrorAction SilentlyContinue
}
