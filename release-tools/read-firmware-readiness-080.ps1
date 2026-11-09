#requires -Version 7.2
# Read-only elevated inspection. Never reads recovery keys or changes protection.
[CmdletBinding()]
param([Parameter(Mandatory)][string]$OutputPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$workspaceRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$reportRoot = Join-Path $workspaceRoot 'evidence\080-firmware-preparation'
$reportPath = [IO.Path]::GetFullPath($OutputPath)
if (-not $reportPath.StartsWith($reportRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
    (Test-Path -LiteralPath $reportPath)) { throw 'Use a new report in the firmware preparation directory.' }
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
$report = [ordered]@{
    schema_version = 1
    scope = 'read_only_firmware_readiness'
    time = (Get-Date).ToString('o')
    administrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    settings_changed = $false
    recovery_keys_read = $false
    volumes = @()
    errors = @()
}
try {
    if (-not $report.administrator) { throw 'Elevated administrator access is required.' }
    $report['board'] = Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer, Product
    $report['bios'] = Get-CimInstance Win32_BIOS | Select-Object SMBIOSBIOSVersion, ReleaseDate
    $volumes = @(Get-CimInstance -Namespace 'root/CIMV2/Security/MicrosoftVolumeEncryption' `
        -ClassName Win32_EncryptableVolume -Filter "DriveLetter = 'C:' OR DriveLetter = 'D:'")
    foreach ($volume in $volumes) {
        $protection = Invoke-CimMethod -InputObject $volume -MethodName GetProtectionStatus
        $conversion = Invoke-CimMethod -InputObject $volume -MethodName GetConversionStatus
        $lock = Invoke-CimMethod -InputObject $volume -MethodName GetLockStatus
        if ($protection.ReturnValue -ne 0 -or $conversion.ReturnValue -ne 0 -or $lock.ReturnValue -ne 0) {
            throw "Encryption status method failed for $($volume.DriveLetter)."
        }
        $report.volumes += [ordered]@{
            drive = $volume.DriveLetter
            protection_status = [int]$protection.ProtectionStatus
            conversion_status = [int]$conversion.ConversionStatus
            encryption_percentage = [int]$conversion.EncryptionPercentage
            lock_status = [int]$lock.LockStatus
        }
    }
    if (-not @($report.volumes | Where-Object drive -eq 'C:').Count) {
        throw 'No system-drive encryption status was returned.'
    }
    $report['success'] = $true
} catch {
    $report['success'] = $false
    $report.errors += $_.Exception.Message
} finally {
    $temporary = $reportPath + '.pending'
    $json = $report | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
    [IO.File]::Move($temporary, $reportPath)
}
if (-not $report.success) { exit 1 }
