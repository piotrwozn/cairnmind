#Requires -Version 7.2
# Pure synthetic guards. No native API, powercfg, WSL, runtime or plan change.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'qualification-power-080.ps1')
$script:cases = 0
function Check([bool]$Condition) { if (-not $Condition) { throw 'Synthetic power assertion failed.' }; $script:cases++ }
function Reject([scriptblock]$Action) {
    $rejected = $false
    try { $null = & $Action } catch { $rejected = $true }
    Check $rejected
}
$balanced = '381b4222-f694-41f0-9685-ff5bb260df2e'
$fixture = @'
Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e (Balanced) *
  Subgroup GUID: fea3413e-7e05-4911-9a71-700331f1c294 (Subgroup)
    Power Setting GUID: 0e796bdb-100d-47d6-a2d5-f7d2daa51f51 (Setting)
      Possible Setting Index: 000
      Possible Setting Friendly Name: No
      Minimum Possible Setting: 0x00000000
      Maximum Possible Setting: 0xffffffff
    Current AC Power Setting Index: 0x00000001
    Current DC Power Setting Index: 0x00000000
'@
$first = ConvertFrom-QualificationPowerQuery $fixture $balanced
Check ($first.setting_count -eq 1 -and $first.canonical_settings.EndsWith("=00000001,00000000`n"))
$localized = $fixture.Replace('Power Scheme','Plan zasilania').Replace('Subgroup','Podgrupa').Replace('Power Setting','Ustawienie').Replace('Balanced','Zrównoważony').Replace(' *','')
Check (Test-QualificationPowerStateEqual $first (ConvertFrom-QualificationPowerQuery $localized $balanced))
Check (-not (Test-QualificationPowerStateEqual $first (ConvertFrom-QualificationPowerQuery $fixture.Replace('0x00000001','0x00000002') $balanced)))
Reject { ConvertFrom-QualificationPowerQuery ($fixture -replace '(?m)^    Current DC.*$', '') $balanced }
Reject { ConvertFrom-QualificationPowerQuery ($fixture + "`n" + $fixture) $balanced }
Reject { ConvertFrom-QualificationPowerQuery $fixture 'a1841308-3541-4fab-bc81-f71556f20b4a' }
# Replace only the read-only transport in this script scope; exercise a plan race.
$script:responses = [Collections.Generic.Queue[string]]::new()
$script:responses.Enqueue('Power Scheme GUID: ' + $balanced)
$script:responses.Enqueue($fixture)
$script:responses.Enqueue('Power Scheme GUID: a1841308-3541-4fab-bc81-f71556f20b4a')
function Invoke-QualificationPowerCfg([string[]]$Arguments) { return $script:responses.Dequeue() }
Reject { Get-QualificationPowerState }
@{scope='synthetic_power_guards'; passed=$true; cases=$script:cases; native_calls=$false} | ConvertTo-Json
