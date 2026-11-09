# Shared read-only Windows power observations. Dot-sourcing has no side effects.
# The only stateful calls are explicitly named sleep inhibition start/stop.
function Invoke-QualificationPowerCfg([string[]]$Arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = Join-Path $env:WINDIR 'System32/powercfg.exe'
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $info; $started = $false
    try {
        if (-not $process.Start()) { throw 'Could not start the power observation.' }
        $started = $true
        $stdout = $process.StandardOutput.ReadToEndAsync(); $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(2000)) { throw 'Power observation exceeded its two-second bound.' }
        if (-not $stdout.Wait(500) -or -not $stderr.Wait(500)) { throw 'Power observation streams did not finish.' }
        if ($process.ExitCode -ne 0 -or $stdout.Result.Length -gt 2MB -or $stderr.Result.Length -gt 4096) { throw 'Power observation failed or exceeded its output bound.' }
        return $stdout.Result
    } finally {
        # Exact own Process handle, never a PID, tree, plan mutation, or shell.
        if ($started -and -not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(1000) }
        $process.Dispose()
    }
}

function ConvertFrom-QualificationPowerQuery([string]$Text, [string]$ActiveGuid) {
    $balanced = '381b4222-f694-41f0-9685-ff5bb260df2e'
    if ($ActiveGuid -cne $balanced) { throw 'Qualification requires the Balanced power plan.' }
    $guidPattern = '[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}'
    $scheme = ''; $subgroup = ''; $setting = ''; $values = @(); $entries = @{}
    foreach ($line in $Text.Split("`n")) {
        $line = $line.TrimEnd("`r")
        # Structural indentation, GUIDs and hex values are independent of
        # localized labels/friendly names and the active-plan star.
        $match = [regex]::Match($line, '^([ ]*)[^\r\n]*?(' + $guidPattern + ')(?:\s|$)')
        if ($match.Success -and $match.Groups[1].Length -in @(0,2,4)) {
            $indent = $match.Groups[1].Length
            $guid = $match.Groups[2].Value.ToLowerInvariant()
            if ($setting) {
                if ($values.Count -ne 2) { throw 'A power setting lacks its exact AC/DC pair.' }
                $key = $subgroup + '/' + $setting
                if ($entries.ContainsKey($key)) { throw 'Duplicate power setting.' }
                $entries[$key] = $values -join ','; $setting = ''; $values = @()
            }
            if ($indent -eq 0) {
                if ($scheme -or $guid -cne $balanced) { throw 'Unexpected power scheme in query.' }
                $scheme = $guid
            } elseif ($indent -eq 2) {
                if (-not $scheme) { throw 'Power subgroup precedes scheme.' }
                $subgroup = $guid
            } else {
                if (-not $subgroup) { throw 'Power setting precedes subgroup.' }
                $setting = $guid
            }
        } elseif ($line -match '^    \S.*:\s*0x([0-9a-fA-F]{1,8})\s*$') {
            if (-not $setting -or $values.Count -ge 2) { throw 'Unexpected AC/DC power index.' }
            $values += [Convert]::ToUInt32($Matches[1],16).ToString('x8')
        }
    }
    if ($setting) {
        if ($values.Count -ne 2) { throw 'Final power setting lacks AC/DC values.' }
        $key = $subgroup + '/' + $setting
        if ($entries.ContainsKey($key)) { throw 'Duplicate power setting.' }
        $entries[$key] = $values -join ','
    }
    if ($scheme -cne $balanced -or -not $entries.Count) { throw 'Power query contains no complete settings.' }
    $canonical = 'scheme=' + $scheme + "`n"
    [string[]]$keys = @($entries.Keys)
    [Array]::Sort($keys, [StringComparer]::Ordinal)
    foreach ($key in $keys) { $canonical += $key + '=' + $entries[$key] + "`n" }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [Convert]::ToHexString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical))).ToLowerInvariant() } finally { $sha.Dispose() }
    return @{active_guid=$scheme; canonical_format='powercfg-qh-acdc-v1'; setting_count=$entries.Count; settings_sha256=$hash; canonical_settings=$canonical}
}

function Get-QualificationPowerState {
    $balanced = '381b4222-f694-41f0-9685-ff5bb260df2e'
    $pattern = '[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}'
    $before = [regex]::Matches((Invoke-QualificationPowerCfg @('/getactivescheme')), $pattern)
    if ($before.Count -ne 1 -or $before[0].Value.ToLowerInvariant() -cne $balanced) { throw 'Active power plan is not Balanced.' }
    $query = Invoke-QualificationPowerCfg @('/qh',$balanced)
    $after = [regex]::Matches((Invoke-QualificationPowerCfg @('/getactivescheme')), $pattern)
    if ($after.Count -ne 1 -or $after[0].Value.ToLowerInvariant() -cne $balanced) { throw 'Power plan changed during observation.' }
    return ConvertFrom-QualificationPowerQuery $query $balanced
}

function Test-QualificationPowerStateEqual($Before, $After) {
    if ($null -eq $Before -or $null -eq $After) { return $false }
    foreach ($key in @('active_guid','canonical_format','setting_count','settings_sha256','canonical_settings')) {
        if ($Before[$key] -cne $After[$key]) { return $false }
    }
    return $true
}

function Start-QualificationSleepInhibition {
    if (-not ('DevBrainQualification.NativePower' -as [type])) {
        Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
namespace DevBrainQualification {
    public static class NativePower {
        [DllImport("kernel32.dll", SetLastError=true)]
        public static extern uint SetThreadExecutionState(uint flags);
    }
}
'@
    }
    $previous = [DevBrainQualification.NativePower]::SetThreadExecutionState([uint32]2147483649)
    if ($previous -eq 0) { throw 'Could not inhibit system sleep for qualification.' }
    return @{acquired=$true; restored=$false; previous_flags=$previous; requested_flags=[uint32]2147483649; thread_id=[Environment]::CurrentManagedThreadId}
}

function Stop-QualificationSleepInhibition($State) {
    if ($null -eq $State -or -not $State.acquired -or $State.restored) { return }
    if ($State.thread_id -ne [Environment]::CurrentManagedThreadId) { throw 'Sleep inhibitor must be restored on its owning thread.' }
    $result = [DevBrainQualification.NativePower]::SetThreadExecutionState([uint32]$State.previous_flags)
    if ($result -eq 0) { throw 'Could not restore the previous sleep inhibitor flags.' }
    $State.restored = $true
}
