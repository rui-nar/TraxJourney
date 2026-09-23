# ── .env loader for the local dev scripts ─────────────────────────────────────
# Single source of truth for local secrets/config: dev.ps1 and dev-server.ps1
# dot-source this file and call `Import-DotEnv` to populate $env:* from .env.
# deploy.ps1 calls `Read-DotEnv` on deploy.env instead, so its settings stay in
# a table and never leak into, or get filled in from, the calling shell.
#
# Contains NO secrets — safe to commit. The real .env is gitignored.
#
# Parsing rules:
#   - blank lines and lines starting with `#` are ignored
#   - `KEY=VALUE`, split on the FIRST `=` only (values may contain `=`/`:`/`/`)
#   - surrounding single or double quotes are stripped; whitespace is trimmed
#   - EMPTY values are skipped, so a blank `DATABASE_URL=` never clobbers a
#     server default with an empty string
#   - .env wins: a non-empty value overwrites any pre-existing $env: var

# Returns the file's non-empty KEY=VALUE pairs as an ordered hashtable.
function Read-DotEnv {
    param([Parameter(Mandatory)][string]$Path)

    $values = [ordered]@{}
    foreach ($line in Get-Content -Path $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }

        $eq = $trimmed.IndexOf('=')
        if ($eq -lt 1) { continue } # no key, or `=` at start — skip

        $key = $trimmed.Substring(0, $eq).Trim()
        $val = $trimmed.Substring($eq + 1).Trim()

        # Strip one layer of matching surrounding quotes.
        if ($val.Length -ge 2 -and
            (($val.StartsWith('"') -and $val.EndsWith('"')) -or
             ($val.StartsWith("'") -and $val.EndsWith("'")))) {
            $val = $val.Substring(1, $val.Length - 2)
        }

        if ($val -eq '') { continue } # don't override defaults with empties

        $values[$key] = $val
    }

    return $values
}

function Import-DotEnv {
    param([string]$Path = (Join-Path $PSScriptRoot '.env'))

    if (-not (Test-Path $Path)) {
        Write-Error ".env not found at '$Path'. Copy .env.example to .env and fill it in."
        return $false
    }

    $values = Read-DotEnv -Path $Path
    foreach ($key in $values.Keys) {
        Set-Item -Path "Env:$key" -Value $values[$key]
    }

    return $true
}
