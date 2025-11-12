# --- run_batch.ps1 ---
# Répertoire du projet
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# Python du venv
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Batch  = Join-Path $Root "batch_alertme.py"
$Config = Join-Path $Root "alerts.jsonl"
$Log    = Join-Path $Root "run_batch.log"
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
  # --- NOUVEAU: sync Git avant toute exécution ---
  $isGitRepo = Test-Path (Join-Path $Root ".git")
  if ($isGitRepo) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Git sync: fetch + pull..." | Tee-Object -FilePath $Log -Append | Out-Null
    & git -C $Root fetch --all 2>&1 | Tee-Object -FilePath $Log -Append
    if ($LASTEXITCODE -ne 0) {
      "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ERREUR: git fetch a échoué (code $LASTEXITCODE). Arrêt." | Tee-Object -FilePath $Log -Append | Out-Null
      throw "git fetch failed"
    }
    & git -C $Root pull --rebase --autostash 2>&1 | Tee-Object -FilePath $Log -Append
    if ($LASTEXITCODE -ne 0) {
      "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ERREUR: git pull a échoué (code $LASTEXITCODE). Arrêt." | Tee-Object -FilePath $Log -Append | Out-Null
      throw "git pull failed"
    }
  } else {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Pas de dépôt Git détecté (./.git introuvable). Skip pull." | Tee-Object -FilePath $Log -Append | Out-Null
  }
  # --- fin ajout Git ---

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
