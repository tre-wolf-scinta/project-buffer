<#
.SYNOPSIS
  Copies configuration and secrets from Bitwarden into the Render environment
  group used by the web service and the worker.

.DESCRIPTION
  Run this yourself, in your own terminal. It never prints a secret value.

  What it does:
    1. Unlocks your Bitwarden vault. Bitwarden asks for your master password
       itself; this script never sees it.
    2. Finds a Render credential (a "Render API Key" item in Bitwarden, or the
       token saved by "render login", or a key you have copied to the clipboard).
    3. Collects each setting:
         - AI provider key: read from Bitwarden.
         - Twilio auth token: read from Bitwarden, or taken from the clipboard
           and then saved to Bitwarden.
         - Message encryption key: read from Bitwarden, or generated and saved
           to Bitwarden so you have a permanent copy.
         - Names and phone numbers: read from deploy.local.json, or asked once.
    4. Writes them to the Render environment group and starts a deploy.
    5. Clears the clipboard and locks the vault.

  Windows PowerShell 5.1 compatible. Prompts are plain text for screen readers.

.PARAMETER EnvGroup
  Name of the Render environment group. Default: buffer-config.

.PARAMETER AiKeySearch
  Text used to find the AI provider key in Bitwarden. Default: anthropic.

.PARAMETER NoDeploy
  Set the values but do not start a deploy.
#>
[CmdletBinding()]
param(
    [string]$EnvGroup = "buffer-config",
    [string]$AiKeySearch = "anthropic",
    [switch]$NoDeploy
)

$ErrorActionPreference = "Stop"
$RenderApi = "https://api.render.com/v1"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LocalConfigPath = Join-Path $RepoRoot "deploy.local.json"

function Say([string]$Text) { Write-Host $Text }

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "STOPPED: $Text"
    exit 1
}

# --- Bitwarden ---------------------------------------------------------------

function Open-Vault {
    if (-not (Get-Command bw -ErrorAction SilentlyContinue)) {
        Fail "The Bitwarden command-line tool (bw) is not on the PATH. Close and reopen the terminal, then run this again."
    }
    $status = (bw status | ConvertFrom-Json).status
    if ($status -eq "unauthenticated") {
        Say "Bitwarden is not signed in on this computer. Bitwarden will now ask for your email, master password and two-step code."
        bw login
        if ($LASTEXITCODE -ne 0) { Fail "Bitwarden sign-in did not complete." }
        $status = (bw status | ConvertFrom-Json).status
    }
    if ($status -ne "unlocked") {
        Say "Bitwarden will now ask for your master password to unlock the vault."
        $session = bw unlock --raw
        if ($LASTEXITCODE -ne 0 -or -not $session) { Fail "The vault was not unlocked." }
        $env:BW_SESSION = $session
    }
    bw sync | Out-Null
    Say "Bitwarden vault is unlocked."
}

function Get-VaultSecret([string]$Search) {
    # Returns the secret from the single item matching $Search, or $null if none.
    # Windows PowerShell 5.1 passes a JSON array down the pipeline as one object,
    # so it is assigned first and then enumerated.
    $parsed = bw list items --search $Search | ConvertFrom-Json
    $items = @($parsed | ForEach-Object { $_ })
    if ($items.Count -eq 0) { return $null }
    $item = $items[0]
    if ($items.Count -gt 1) {
        Say "More than one Bitwarden item matches '$Search':"
        for ($i = 0; $i -lt $items.Count; $i++) { Say ("  {0}. {1}" -f ($i + 1), $items[$i].name) }
        $choice = Read-Host "Type the number of the one to use"
        $index = 0
        if (-not [int]::TryParse($choice, [ref]$index) -or $index -lt 1 -or $index -gt $items.Count) {
            Fail "That was not one of the listed numbers."
        }
        $item = $items[$index - 1]
    }
    if ($item.login -and $item.login.password) { return $item.login.password }
    if ($item.fields) {
        $hidden = @($item.fields | Where-Object { $_.value })
        if ($hidden.Count -gt 0) { return $hidden[0].value }
    }
    if ($item.notes) { return $item.notes.Trim() }
    Fail "The Bitwarden item '$($item.name)' has no password, custom field or note to read."
}

function Save-VaultSecret([string]$Name, [string]$Secret, [string]$Note) {
    $template = bw get template item | ConvertFrom-Json
    $template.type = 1
    $template.name = $Name
    $template.notes = $Note
    $template.login = [pscustomobject]@{ username = ""; password = $Secret; uris = @(); totp = $null }
    $encoded = $template | ConvertTo-Json -Depth 10 -Compress | bw encode
    $encoded | bw create item | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Could not save '$Name' to Bitwarden." }
    Say "Saved to Bitwarden as '$Name'."
}

function Read-ClipboardSecret([string]$What) {
    Say ""
    Say "Copy the $What to the clipboard now."
    Read-Host "When it is on the clipboard, press Enter" | Out-Null
    $value = Get-Clipboard -Raw
    Set-Clipboard -Value " "
    if (-not $value -or -not $value.Trim()) { Fail "The clipboard was empty." }
    return $value.Trim()
}

function New-EncryptionKey {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    return [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_")
}

# --- Render ------------------------------------------------------------------

function Invoke-Render([string]$Method, [string]$Path, $Body = $null) {
    $headers = @{ Authorization = "Bearer $script:RenderToken"; Accept = "application/json" }
    $params = @{ Method = $Method; Uri = "$RenderApi$Path"; Headers = $headers }
    if ($null -ne $Body) {
        $params.Body = ($Body | ConvertTo-Json -Compress)
        $params.ContentType = "application/json"
    }
    try {
        return Invoke-RestMethod @params
    } catch {
        $code = "no response"
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        # Only the status code is reported. Response bodies are not echoed.
        throw "Render API $Method $Path failed with status $code."
    }
}

function Test-RenderToken([string]$Token) {
    if (-not $Token) { return $false }
    $script:RenderToken = $Token
    try { Invoke-Render "GET" "/owners?limit=1" | Out-Null; return $true } catch { return $false }
}

function Get-RenderToken {
    $fromVault = Get-VaultSecret "Render API Key"
    if (Test-RenderToken $fromVault) { Say "Using the Render API key stored in Bitwarden."; return }

    $cliConfig = Join-Path $HOME ".render\cli.yaml"
    if (Test-Path $cliConfig) {
        $match = [regex]::Match((Get-Content $cliConfig -Raw), "rnd_[A-Za-z0-9]+")
        if ($match.Success -and (Test-RenderToken $match.Value)) {
            Say "Using the sign-in saved by the Render command-line tool."
            return
        }
    }

    Say ""
    Say "No working Render credential was found."
    Say "In the Render dashboard, open Account Settings, then API Keys, create a key, and copy it."
    $pasted = Read-ClipboardSecret "Render API key"
    if (-not $pasted.StartsWith("rnd_")) {
        Fail "What was on the clipboard is not a Render API key (it should start with rnd_). Nothing was sent or saved."
    }
    if (-not (Test-RenderToken $pasted)) { Fail "Render did not accept that key." }
    Save-VaultSecret "Render API Key" $pasted "Created for Project Buffer deployment."
}

function Get-Unwrapped($Rows, [string]$Property) {
    # Render list endpoints wrap each row, for example { cursor, service: {...} }.
    return @($Rows | ForEach-Object { if ($_.$Property) { $_.$Property } else { $_ } })
}

# --- Local, non-secret settings ----------------------------------------------

function Get-LocalConfig {
    if (Test-Path $LocalConfigPath) { return Get-Content $LocalConfigPath -Raw | ConvertFrom-Json }
    return [pscustomobject]@{}
}

function Get-Setting($Config, [string]$Key, [string]$Question, [string]$Default = "") {
    $existing = $Config.$Key
    if ($existing) { return [string]$existing }
    $prompt = $Question
    if ($Default) { $prompt = "$Question (press Enter for $Default)" }
    $answer = (Read-Host $prompt).Trim()
    if (-not $answer) { $answer = $Default }
    if (-not $answer) { Fail "A value is needed for $Key." }
    $Config | Add-Member -NotePropertyName $Key -NotePropertyValue $answer -Force
    return $answer
}

# --- Main --------------------------------------------------------------------

Say "Project Buffer: send settings from Bitwarden to Render."
Say "No secret value will be shown on screen."
Say ""

Open-Vault
Get-RenderToken

$groups = Get-Unwrapped (Invoke-Render "GET" "/env-groups?limit=100") "envGroup"
$group = @($groups | Where-Object { $_.name -eq $EnvGroup })
if ($group.Count -ne 1) {
    Fail "Render has no environment group named '$EnvGroup'. Create the Blueprint first."
}
$groupId = $group[0].id
Say "Found the Render environment group '$EnvGroup'."

$services = Get-Unwrapped (Invoke-Render "GET" "/services?limit=100") "service"
$web = @($services | Where-Object { $_.name -eq "buffer-web" })
$worker = @($services | Where-Object { $_.name -eq "buffer-worker" })

$config = Get-LocalConfig
$values = [ordered]@{}

$defaultUrl = ""
if ($web.Count -eq 1 -and $web[0].serviceDetails -and $web[0].serviceDetails.url) {
    $defaultUrl = $web[0].serviceDetails.url.TrimEnd("/")
}
$values.APPLICATION_BASE_URL = Get-Setting $config "APPLICATION_BASE_URL" "Public address of the web service, starting with https" $defaultUrl
$values.TWILIO_ACCOUNT_SID = Get-Setting $config "TWILIO_ACCOUNT_SID" "Twilio Account SID, starting with AC"
$values.TWILIO_PHONE_NUMBER = Get-Setting $config "TWILIO_PHONE_NUMBER" "The Twilio number, like +13255550100"
$values.OWNER_PHONE_NUMBER = Get-Setting $config "OWNER_PHONE_NUMBER" "Your own mobile number, like +13255550100"
$values.COPARENT_PHONE_NUMBER = Get-Setting $config "COPARENT_PHONE_NUMBER" "Your co-parent's mobile number, like +13255550100"
$values.OWNER_DISPLAY_NAME = Get-Setting $config "OWNER_DISPLAY_NAME" "Your first name, as summaries should refer to you"
$values.COPARENT_DISPLAY_NAME = Get-Setting $config "COPARENT_DISPLAY_NAME" "Your co-parent's first name"
$values.CHILDREN_NAMES = Get-Setting $config "CHILDREN_NAMES" "Children's first names, separated by commas"
$values.SMS_BRAND_NAME = Get-Setting $config "SMS_BRAND_NAME" "Name registered with Twilio as the brand"
$values.OWNER_TIMEZONE = Get-Setting $config "OWNER_TIMEZONE" "Time zone" "America/Chicago"
$values.LLM_PROVIDER = Get-Setting $config "LLM_PROVIDER" "AI provider, anthropic or openai" "anthropic"

$config | ConvertTo-Json | Set-Content -Path $LocalConfigPath -Encoding utf8
Say "Saved the non-secret answers to deploy.local.json so you are not asked again."

$aiKey = Get-VaultSecret $AiKeySearch
if (-not $aiKey) { Fail "No Bitwarden item matches '$AiKeySearch'. Run again with -AiKeySearch and part of the item's name." }
# Guard against picking up an account password from a sign-in item with a similar name.
$expectedPrefix = "sk-ant-"
if ($values.LLM_PROVIDER -eq "openai") { $expectedPrefix = "sk-" }
if (-not $aiKey.StartsWith($expectedPrefix)) {
    Fail "The Bitwarden item found for '$AiKeySearch' does not look like an API key (it should start with $expectedPrefix). Run again with -AiKeySearch and the exact name of the item that holds the key."
}
if ($values.LLM_PROVIDER -eq "openai") { $values.OPENAI_API_KEY = $aiKey } else { $values.ANTHROPIC_API_KEY = $aiKey }
Say "Read the AI provider key from Bitwarden."

$twilioToken = Get-VaultSecret "Twilio Auth Token"
if (-not $twilioToken) {
    Say ""
    Say "The Twilio auth token is not in Bitwarden yet."
    Say "On the Twilio console home page there is a Copy button right after the Auth Token field."
    $twilioToken = Read-ClipboardSecret "Twilio auth token"
    if ($twilioToken -notmatch "^[0-9a-fA-F]{32}$") {
        Fail "What was on the clipboard is not a Twilio auth token (32 letters and digits). Nothing was sent or saved."
    }
    Save-VaultSecret "Twilio Auth Token" $twilioToken "Project Buffer. Account auth token; used to verify Twilio webhooks."
}
$values.TWILIO_AUTH_TOKEN = $twilioToken

$encryptionKey = Get-VaultSecret "Buffer Message Encryption Key"
if (-not $encryptionKey) {
    $encryptionKey = New-EncryptionKey
    Save-VaultSecret "Buffer Message Encryption Key" $encryptionKey "Project Buffer RAW_MESSAGE_ENCRYPTION_KEY. If this is lost, stored original messages can never be read. Do not delete."
    Say "Generated a new message encryption key."
} else {
    Say "Read the existing message encryption key from Bitwarden."
}
$values.RAW_MESSAGE_ENCRYPTION_KEY = $encryptionKey

Say ""
foreach ($key in $values.Keys) {
    $escaped = [uri]::EscapeDataString($key)
    Invoke-Render "PUT" "/env-groups/$groupId/env-vars/$escaped" @{ value = [string]$values[$key] } | Out-Null
    Say "Set $key"
}

if (-not $NoDeploy) {
    foreach ($service in @($web + $worker)) {
        Invoke-Render "POST" "/services/$($service.id)/deploys" @{ clearCache = "do_not_clear" } | Out-Null
        Say "Started a deploy of $($service.name)."
    }
}

$aiKey = $null; $twilioToken = $null; $encryptionKey = $null; $values = $null; $script:RenderToken = $null
Set-Clipboard -Value " "
bw lock | Out-Null
$env:BW_SESSION = $null

Say ""
Say "Done. $EnvGroup is configured, the clipboard is cleared and the vault is locked."
