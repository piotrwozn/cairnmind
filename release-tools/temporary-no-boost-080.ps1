#requires -Version 7.2
<#
PREPARED ONLY. Inspect is read-only. Begin/Restore/Watchdog require -Execute.
Future consent must cover BOTH the temporary plan and one hidden scheduled task.
No BIOS, voltage, maximum-CPU, affinity, reboot or original-plan edits.
Watchdog rolls back at 150s, leaving margin to the 180s operating budget.
This is not a hard guarantee through Windows/Task Scheduler failure or suspension.
Power APIs have no compare-and-swap; see the narrow manual-change race in the report.
#>
[CmdletBinding()]
param(
    [ValidateSet('Inspect', 'Begin', 'Restore', 'Watchdog')][string]$Mode = 'Inspect',
    [string]$StatePath,
    [switch]$Execute
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$scriptPath = [IO.Path]::GetFullPath($PSCommandPath)
$powercfg = Join-Path $env:SystemRoot 'System32\powercfg.exe'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$subgroup = [guid]'54533251-82be-4824-96c1-47b60b740d00'
$boostSetting = [guid]'be337238-0d82-4146-a960-4f3749d470c7'

function Assert-LocalPath([string]$Path) {
    if (-not [IO.Path]::IsPathFullyQualified($Path)) { throw 'Artifact path must be absolute.' }
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Artifact path escapes the workspace.'
    }
    $cursor = $full
    while ($cursor.Length -gt $workspace.Length) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Reparse points are not allowed for power diagnostic artifacts.'
            }
        }
        $cursor = Split-Path -Parent $cursor
    }
    return $full
}

function Invoke-PowerCfg([string[]]$Arguments) {
    $psi = [Diagnostics.ProcessStartInfo]::new($powercfg)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    foreach ($value in $Arguments) { [void]$psi.ArgumentList.Add($value) }
    $process = [Diagnostics.Process]::Start($psi)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    try {
        $timeout = 5000
        if (Get-Variable -Name rollbackDeadlineTick -Scope Script -ErrorAction SilentlyContinue) {
            $timeout = [int][Math]::Min(5000, $script:rollbackDeadlineTick - [Environment]::TickCount64)
            if ($timeout -le 0) { $process.Kill(); throw 'Rollback operating deadline reached.' }
        }
        if (-not $process.WaitForExit($timeout)) { $process.Kill(); throw 'powercfg timeout' }
        if ($process.ExitCode -ne 0) { throw "powercfg failed ($($process.ExitCode)): $($stderr.Result)" }
        return $stdout.Result
    } finally { $process.Dispose() }
}

function Get-ActivePlan {
    $text = Invoke-PowerCfg @('/getactivescheme')
    $match = [regex]::Match($text, '[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
    if (-not $match.Success) { throw 'Cannot identify the active power plan.' }
    return ([guid]$match.Value).ToString()
}

if (-not ('DevBrainPowerRead080' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class DevBrainPowerRead080 {
 [DllImport("powrprof.dll")] public static extern uint PowerReadACValueIndex(IntPtr root, ref Guid plan, ref Guid subgroup, ref Guid setting, out uint value);
 [DllImport("powrprof.dll")] public static extern uint PowerReadDCValueIndex(IntPtr root, ref Guid plan, ref Guid subgroup, ref Guid setting, out uint value);
}
'@
}
function Get-Boost([string]$Plan) {
    $guid = [guid]$Plan
    [uint32]$ac = 0; [uint32]$dc = 0
    if ([DevBrainPowerRead080]::PowerReadACValueIndex([IntPtr]::Zero, [ref]$guid, [ref]$subgroup, [ref]$boostSetting, [ref]$ac) -ne 0 -or
        [DevBrainPowerRead080]::PowerReadDCValueIndex([IntPtr]::Zero, [ref]$guid, [ref]$subgroup, [ref]$boostSetting, [ref]$dc) -ne 0) {
        throw 'Cannot read both AC/DC PERFBOOSTMODE values.'
    }
    return @{ ac = $ac; dc = $dc }
}

function Write-Atomic([string]$Path, $Value) {
    $Path = Assert-LocalPath $Path
    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, $null) }
    else { [IO.File]::Move($temporary, $Path) }
}

function Read-Ownership([string]$Path) {
    $Path = Assert-LocalPath $Path
    if ([IO.Path]::GetFileName($Path) -ne 'state.json') { throw 'Expected owned state.json.' }
    $directory = Split-Path -Parent $Path
    $name = [IO.Path]::GetFileName($directory)
    if ($name -notmatch '^power-ab-080-([0-9a-f]{32})$') { throw 'Unexpected artifact directory.' }
    $owner = $Matches[1]
    $receiptPath = Assert-LocalPath (Join-Path $directory 'ownership.json')
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json -AsHashtable
    $state = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -AsHashtable
    if ($receipt.owner -ne $owner -or $receipt.sid -ne $sid -or $receipt.script -ne $scriptPath -or
        $state.owner -ne $owner -or $state.original -ne $receipt.original -or $state.clone -ne $receipt.clone -or
        $receipt.clone -eq $receipt.original -or $receipt.task -ne "DevBrainPower080-$owner") { throw 'Ownership metadata mismatch.' }
    [void][guid]::Parse($receipt.clone); [void][guid]::Parse($receipt.original)
    return @{ receipt = $receipt; state = $state; directory = $directory; path = $Path }
}

function With-Lock($Owned, [scriptblock]$Action) {
    $mutex = [Threading.Mutex]::new($false, 'Local\DevBrainPower080-' + $Owned.receipt.owner)
    $held = $false
    try {
        try { $held = $mutex.WaitOne(5000) } catch [Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { throw 'Power diagnostic lock timeout.' }
        & $Action
    } finally { if ($held) { $mutex.ReleaseMutex() }; $mutex.Dispose() }
}

function Get-TaskRoot {
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    return @{ service = $service; folder = $service.GetFolder('\') }
}

function Remove-OwnedTask($Receipt) {
    $scheduler = Get-TaskRoot
    try { $task = $scheduler.folder.GetTask($Receipt.task) } catch { return }
    if ($task.Definition.RegistrationInfo.Description -ne "DevBrainPower080 owner=$($Receipt.owner)") {
        throw 'Scheduled-task ownership mismatch.'
    }
    $scheduler.folder.DeleteTask($Receipt.task, 0)
}

function Restore-Owned($Owned) {
    With-Lock $Owned {
        $fresh = Read-Ownership $Owned.path
        $receipt = $fresh.receipt; $state = $fresh.state
        $active = Get-ActivePlan
        $state['restore_observed_active'] = $active
        if ($active -eq $receipt.clone) {
            # No Windows API offers atomic compare-and-swap here. This is the
            # final observed-state check; do not switch plans during the trial.
            [void](Invoke-PowerCfg @('/setactive', $receipt.original))
            if ((Get-ActivePlan) -ne $receipt.original) { throw 'Original GUID restore readback failed.' }
            $state['status'] = 'restored_original'
        } elseif ($active -eq $receipt.original) { $state['status'] = 'original_already_active' }
        else { $state['status'] = 'user_selected_other_plan_preserved' }
        $plans = Invoke-PowerCfg @('/list')
        $line = $plans -split "`r?`n" | Where-Object { $_ -match [regex]::Escape($receipt.clone) }
        if ($line) {
            if (-not ($line -match [regex]::Escape($receipt.name))) { throw 'Clone name/ownership mismatch; refusing deletion.' }
            if ((Get-ActivePlan) -eq $receipt.clone) { throw 'Refusing to delete an active clone.' }
            [void](Invoke-PowerCfg @('/delete', $receipt.clone))
        }
        $boost = Get-Boost $receipt.original
        $state['original_values_preserved'] = $boost.ac -eq $receipt.ac -and $boost.dc -eq $receipt.dc
        $state['final_active'] = Get-ActivePlan
        $state['ended_utc'] = [DateTime]::UtcNow.ToString('o')
        Write-Atomic $Owned.path $state
        Remove-OwnedTask $receipt
        return $state
    }
}

if ($Mode -eq 'Inspect') {
    $active = Get-ActivePlan; $values = Get-Boost $active
    @{ mode = 'Inspect'; active = $active; ac = $values.ac; dc = $values.dc;
       planned_changes = 'Duplicate active plan; set only clone AC/DC PERFBOOSTMODE=0; register hidden one-time watchdog before activation; restore original GUID and delete inactive owned clone/task.';
       experiment_limit_seconds = 150; restore_operating_budget_seconds = 180;
       applied = $false; actual_guest_frequency_verified = $false } | ConvertTo-Json
    exit 0
}
if (-not $Execute) { throw "$Mode requires explicit -Execute after user authorization." }
[void](Assert-LocalPath $scriptPath)

if ($Mode -eq 'Restore') {
    Restore-Owned (Read-Ownership $StatePath) | ConvertTo-Json -Depth 12
    exit 0
}
if ($Mode -eq 'Watchdog') {
    $owned = Read-Ownership $StatePath
    $receipt = $owned.receipt
    try {
        while ([Environment]::TickCount64 -lt $receipt.deadline_tick -and [DateTime]::UtcNow -lt [DateTime]$receipt.deadline_utc) {
            $owned = Read-Ownership $StatePath
            if ($owned.state.status -in @('restored_original', 'original_already_active', 'user_selected_other_plan_preserved')) { exit 0 }
            Write-Atomic (Join-Path $owned.directory 'heartbeat.json') @{ owner = $receipt.owner; pid = $PID; tick = [Environment]::TickCount64 }
            if ($owned.state.status -eq 'active' -and (Get-ActivePlan) -ne $receipt.clone) { break }
            Start-Sleep -Milliseconds 500
        }
    } finally {
        $script:rollbackDeadlineTick = $receipt.deadline_tick + 30000
        $restored = $false
        do {
            try { Restore-Owned (Read-Ownership $StatePath) | Out-Null; $restored = $true; break }
            catch {
                Write-Atomic (Join-Path $owned.directory 'restore-error.json') @{ owner = $receipt.owner; error = $_.Exception.Message; time_utc = [DateTime]::UtcNow.ToString('o') }
                Start-Sleep -Milliseconds 250
            }
        } while ([Environment]::TickCount64 -lt $script:rollbackDeadlineTick)
        if (-not $restored) { throw 'Restore could not be verified within the operating deadline; inspect restore-error.json.' }
    }
    exit 0
}

if ($StatePath) { throw 'Begin generates its own new artifact directory; StatePath is only for Restore/Watchdog.' }
$owner = [guid]::NewGuid().ToString('N')
$clone = [guid]::NewGuid().ToString()
$directory = Assert-LocalPath (Join-Path $workspace "evidence\power-ab-080-$owner")
$StatePath = Join-Path $directory 'state.json'
$active = Get-ActivePlan; $values = Get-Boost $active
$receipt = @{ owner = $owner; sid = $sid; script = $scriptPath; script_sha256 = (Get-FileHash -LiteralPath $scriptPath -Algorithm SHA256).Hash;
              original = $active; ac = $values.ac; dc = $values.dc; clone = $clone;
              name = "DevBrain temporary boost diagnostic $owner"; task = "DevBrainPower080-$owner";
              deadline_tick = [Environment]::TickCount64 + 150000; deadline_utc = [DateTime]::UtcNow.AddSeconds(150).ToString('o') }
[void][IO.Directory]::CreateDirectory($directory)
Write-Atomic (Join-Path $directory 'ownership.json') $receipt
Write-Atomic $StatePath @{ owner = $owner; original = $active; clone = $clone; status = 'preparing' }
$owned = Read-Ownership $StatePath
try {
    [void](Invoke-PowerCfg @('/duplicatescheme', $active, $clone))
    [void](Invoke-PowerCfg @('/changename', $clone, $receipt.name))
    [void](Invoke-PowerCfg @('/setacvalueindex', $clone, $subgroup.ToString(), $boostSetting.ToString(), '0'))
    [void](Invoke-PowerCfg @('/setdcvalueindex', $clone, $subgroup.ToString(), $boostSetting.ToString(), '0'))
    $cloneValues = Get-Boost $clone
    if ($cloneValues.ac -ne 0 -or $cloneValues.dc -ne 0) { throw 'Clone AC/DC readback failed.' }
    $scheduler = Get-TaskRoot
    $definition = $scheduler.service.NewTask(0)
    $definition.RegistrationInfo.Description = "DevBrainPower080 owner=$owner"
    $definition.Principal.UserId = $sid
    $definition.Principal.LogonType = 3 # TASK_LOGON_INTERACTIVE_TOKEN; no stored credentials.
    $definition.Principal.RunLevel = 0
    $definition.Settings.Hidden = $true
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.ExecutionTimeLimit = 'PT4M'
    $definition.Settings.MultipleInstances = 2
    $action = $definition.Actions.Create(0)
    $action.Path = (Get-Process -Id $PID).Path
    # Task Scheduler ExecAction has a command-line string, not ArgumentList.
    # Validated Windows paths cannot contain quotes; all other arguments are fixed.
    if ($scriptPath.Contains('"') -or $StatePath.Contains('"')) { throw 'Invalid quoted path.' }
    $action.Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $scriptPath + '" -Mode Watchdog -Execute -StatePath "' + $StatePath + '"'
    $action.WorkingDirectory = $workspace
    $task = $scheduler.folder.RegisterTaskDefinition($receipt.task, $definition, 2, $sid, $null, 3, $null)
    [void]$task.Run($null)
    $readyUntil = [Environment]::TickCount64 + 10000
    do {
        Start-Sleep -Milliseconds 100
        $heartbeatPath = Join-Path $directory 'heartbeat.json'
        if (Test-Path -LiteralPath $heartbeatPath) {
            $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw | ConvertFrom-Json -AsHashtable
            if ($heartbeat.owner -eq $owner -and [Environment]::TickCount64 - $heartbeat.tick -lt 1500 -and $task.State -eq 4) { break }
        }
    } while ([Environment]::TickCount64 -lt $readyUntil)
    if (-not (Test-Path -LiteralPath $heartbeatPath) -or $task.State -ne 4) { throw 'Live independent watchdog not confirmed.' }
    With-Lock $owned {
        $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw | ConvertFrom-Json -AsHashtable
        if ($heartbeat.owner -ne $owner -or [Environment]::TickCount64 - $heartbeat.tick -gt 1500 -or
            $task.State -ne 4 -or [Environment]::TickCount64 -gt $receipt.deadline_tick - 120000) { throw 'Watchdog stale or insufficient trial time remains.' }
        if ((Get-ActivePlan) -ne $active) { throw 'User changed the active plan; activation refused.' }
        [void](Invoke-PowerCfg @('/setactive', $clone))
        if ((Get-ActivePlan) -ne $clone) { throw 'Clone activation readback failed.' }
        Write-Atomic $StatePath @{ owner = $owner; original = $active; clone = $clone; status = 'active'; activated_utc = [DateTime]::UtcNow.ToString('o') }
    }
    @{ state_path = $StatePath; original = $active; clone = $clone; deadline_utc = $receipt.deadline_utc; status = 'active' } | ConvertTo-Json
} catch {
    $failure = $_
    try { Restore-Owned $owned | Out-Null } catch { Write-Warning "Restore failed; independent watchdog remains armed: $($_.Exception.Message)" }
    throw $failure
}
