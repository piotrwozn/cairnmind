#Requires -Version 7.2
<# Counterfactual supervisor fixture only. Never an application qualification.
The Linux read-only pidfd observer starts BEFORE the tested Windows parent and
keeps WSL foreground. This script outlives both, including a final 30 s watch. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ToolsDirectory,
    [Parameter(Mandatory)][string]$OracleScript,
    [Parameter(Mandatory)][string]$ArtifactRoot,
    [Parameter(Mandatory)][string]$GuardReceipt,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$GuardReceiptSha256,
    [Parameter(Mandatory)][string]$GateFile,
    [Parameter(Mandatory)][string]$PythonLauncher,
    [Parameter(Mandatory)][string]$WslCrashDirectory,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_-]{1,80}$')][string]$RunId,
    [switch]$Execute
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if(-not $Execute){throw 'Explicit -Execute required after the final guard receipt PASS and workload handoff.'}
$wrapper=Join-Path $ToolsDirectory 'run-supervised-080.ps1'
$tokens=$null;$parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($wrapper,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Frozen wrapper has syntax errors'}
foreach($name in @('Assert-PlainPath','Convert-LinuxPath','Read-Json','Write-AtomicJson','Get-Sha','Get-CrashMetadata','Start-StreamProcess','Close-StreamProcess')){
    $nodes=@($ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true))
    if($nodes.Count -ne 1){throw 'Expected exactly one helper function'}
    . ([scriptblock]::Create($nodes[0].Extent.Text))
}
$root=Assert-PlainPath $ArtifactRoot
$physical='C:\Users\piotr\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\DevBrain\qualification'
if(-not $root.StartsWith($physical+'\',[StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $root)){throw 'Use a fresh root under the verified physical qualification directory'}
$ToolsDirectory=Assert-PlainPath $ToolsDirectory
$OracleScript=Assert-PlainPath $OracleScript
foreach($path in @($ToolsDirectory,$OracleScript,(Assert-PlainPath $PSCommandPath))){
    if(-not $path.StartsWith($physical+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'All fixture helpers must be copied outside OneDrive into the physical qualification root'}
}
$crashRoot=Assert-PlainPath $WslCrashDirectory
if((Get-Sha (Assert-PlainPath $GuardReceipt)) -cne $GuardReceiptSha256){throw 'Guard receipt differs from the externally admitted digest'}
$guard=Read-Json (Assert-PlainPath $GuardReceipt)
foreach($key in @('passed','cleanup_complete','controller_exit_verified','supervisor_exit_verified','scope_empty_verified')){
    if($guard[$key] -ne $true){throw ('Guard receipt did not verify '+$key)}
}
if($guard.scope -cne 'devbrain_external_process_watch' -or $guard.forced_cleanup -or $guard.native_failure -or
    $guard.cleanup_grace_seconds -lt 30 -or $guard.scope_cleanup_grace_seconds -lt 30 -or $guard.errors.Count){throw 'Guard qualification is incomplete'}
$gate=Read-Json (Assert-PlainPath $GateFile)
if($gate.status -cne 'pass' -or (Get-Sha $GateFile) -cne $guard.gate_sha256){throw 'Gate differs from the admitted guard run'}
$guardSourcesPath=Assert-PlainPath (Join-Path (Split-Path $GuardReceipt) 'regression-source.json')
if((Get-Sha $guardSourcesPath) -cne $guard.artifact_sha256['regression-source.json']){throw 'Guard source manifest does not match its receipt'}
$guardSources=Read-Json $guardSourcesPath
if($guardSources.run_id -cne $guard.run_id -or $guardSources.phase -cne 'guards'){throw 'A guard-phase source manifest is required'}
foreach($name in $guardSources.guard_sources.helper_sha256.Keys){
    if($name -cne [IO.Path]::GetFileName($name) -or (Get-Sha (Assert-PlainPath (Join-Path $ToolsDirectory $name))) -cne $guardSources.guard_sources.helper_sha256[$name]){throw 'Frozen helper bundle differs from the passed guard source manifest'}
}
$hashes=@{}
foreach($path in @($wrapper,(Join-Path $ToolsDirectory 'supervise-080.py'),(Join-Path $ToolsDirectory 'qualification-power-080.ps1'),$OracleScript,$PSCommandPath,$GateFile,$GuardReceipt,$guardSourcesPath)){$hashes[$path]=Get-Sha $path}
foreach($name in $guardSources.guard_sources.helper_sha256.Keys){$path=Join-Path $ToolsDirectory $name;$hashes[$path]=Get-Sha $path}
$null=[IO.Directory]::CreateDirectory($root)
Copy-Item -LiteralPath $GateFile -Destination (Join-Path $root 'gate.json')
if((Get-Sha (Join-Path $root 'gate.json')) -cne $guard.gate_sha256){throw 'Fixture gate copy differs from admission'}
$hashes[(Join-Path $root 'gate.json')]=Get-Sha (Join-Path $root 'gate.json')
$linuxRoot=Convert-LinuxPath $root
$supervisor=Join-Path $ToolsDirectory 'supervise-080.py'
$controller=Join-Path $ToolsDirectory 'test_supervision080_fixture.py'
$specPath=Join-Path $root 'spec.json'
$null=Write-AtomicJson $specPath @{run_id=$RunId;python_launcher=$PythonLauncher;argv=@('--execute','--run-id',$RunId,'--artifact-root',$linuxRoot,'--mode','parent-loss');env=@{};required_artifacts=@('gate.json','controller-report.json','supervisor-report.json')}
$hashes[$specPath]=Get-Sha $specPath
. (Join-Path $ToolsDirectory 'qualification-power-080.ps1')
$clock=[Diagnostics.Stopwatch]::StartNew()
$observer=$null;$parent=$null;$sleepState=$null;$powerStart=$null;$powerEnd=$null;$crashes=@{}
$errors=[Collections.Generic.List[string]]::new();$newDumps=[Collections.Generic.HashSet[string]]::new()
$parentKilled=$false;$parentExit=$false;$parentStreamsComplete=$false;$observerExit=$false;$observerExitAt=$null;$powerSamples=0;$nextPower=0.0
$nextKernel=0.0;$kernelProbeCount=0;$lastKernelObservedAt=$null;$ready=$null;$pidfds=$null
$report=@{schema_version=1;scope='devbrain_parent_loss_fixture_oracle';run_id=$RunId;passed=$false;application_qualification=$false;errors=@()}
$oraclePowerCfgImplementation=(Get-Command Invoke-QualificationPowerCfg -CommandType Function).ScriptBlock
function Invoke-QualificationPowerCfg([string[]]$Arguments) {
    # Instrument the unmodified frozen function; its retained Process handle,
    # per-command timeout and finally cleanup remain the single implementation.
    $commandStart=$clock.Elapsed.TotalSeconds;$failure=$null
    try { return & $oraclePowerCfgImplementation -Arguments $Arguments }
    catch { $failure=$_.Exception.Message;throw }
    finally {
        $ended=$clock.Elapsed.TotalSeconds
        [IO.File]::AppendAllText((Join-Path $root 'oracle-power-commands.jsonl'),(@{arguments=$Arguments;started_elapsed_seconds=$commandStart;finished_elapsed_seconds=$ended;duration_seconds=$ended-$commandStart;passed=($null -eq $failure);error=$failure}|ConvertTo-Json -Compress)+"`n",[Text.UTF8Encoding]::new($false))
    }
}
function Get-OraclePowerState([string]$Stage) {
    $attemptStart=$clock.Elapsed.TotalSeconds;$failure=$null
    try { return Get-QualificationPowerState }
    catch { $failure=$_.Exception.Message;throw }
    finally {
        $ended=$clock.Elapsed.TotalSeconds
        [IO.File]::AppendAllText((Join-Path $root 'oracle-power-attempts.jsonl'),(@{stage=$Stage;started_elapsed_seconds=$attemptStart;finished_elapsed_seconds=$ended;duration_seconds=$ended-$attemptStart;passed=($null -eq $failure);error=$failure}|ConvertTo-Json -Compress)+"`n",[Text.UTF8Encoding]::new($false))
    }
}
function Observe-Windows {
    $current=Get-CrashMetadata
    foreach($name in $current.Keys){if(-not $crashes.ContainsKey($name) -or $crashes[$name] -cne $current[$name]){$null=$newDumps.Add($name)}}
    if($newDumps.Count){throw 'A new WSL crash appeared during the fixture'}
    if($script:observerExit -and $null -ne $script:ready -and $clock.Elapsed.TotalSeconds -ge $script:nextKernel){
        $script:kernelProbeCount++
        $probe=Start-StreamProcess @('/bin/sh','-c','set -eu; /usr/bin/cat /proc/sys/kernel/random/boot_id; /usr/bin/dmesg --raw') (Join-Path $root ('oracle-final-kernel-'+$script:kernelProbeCount+'.stdout.log')) (Join-Path $root ('oracle-final-kernel-'+$script:kernelProbeCount+'.stderr.log')) $true
        try{
            if(-not $probe.process.WaitForExit(7000)){$probe.process.Kill();$null=$probe.process.WaitForExit(1000);throw 'Final kernel probe timed out'}
            Close-StreamProcess $probe
            if($probe.process.ExitCode -ne 0){throw 'Final kernel probe returned nonzero'}
            $lines=[IO.File]::ReadAllLines($probe.path)
            if($lines.Length -lt 1 -or $lines[0] -cne $script:ready.initial_boot_id){throw 'Boot changed after the Linux oracle exit'}
            $fatals=@($lines | Where-Object {$_ -match 'segfault|segmentation fault|fatal signal|SIGSEGV|SIGABRT|invalid opcode|general protection fault|oom-kill|Out of memory|Kernel panic'} | Sort-Object -Unique)
            if(($fatals -join "`n") -cne (@($script:ready.initial_kernel_fatals | Sort-Object -Unique) -join "`n")){throw 'Kernel fatal history changed after the Linux oracle exit'}
            $script:lastKernelObservedAt=$clock.Elapsed.TotalSeconds
        }finally{
            if($probe.process.HasExited){try{Close-StreamProcess $probe}finally{$probe.process.Dispose()}}
        }
        $script:nextKernel=$clock.Elapsed.TotalSeconds+2
    }
    if($clock.Elapsed.TotalSeconds -ge $script:nextPower){
        $state=Get-OraclePowerState $(if($script:observerExit){'post_oracle_exit'}else{'linux_oracle_live'})
        if(-not (Test-QualificationPowerStateEqual $powerStart $state)){throw 'Balanced power fingerprint changed'}
        $script:powerEnd=$state;$script:powerSamples++;$script:nextPower=$clock.Elapsed.TotalSeconds+10
        [IO.File]::AppendAllText((Join-Path $root 'oracle-power.jsonl'),(@{elapsed_seconds=$clock.Elapsed.TotalSeconds;state=$state}|ConvertTo-Json -Depth 8 -Compress)+"`n",[Text.UTF8Encoding]::new($false))
    }
}
function Start-WindowsParent {
    $info=[Diagnostics.ProcessStartInfo]::new()
    $info.FileName=Join-Path $PSHOME 'pwsh.exe';$info.UseShellExecute=$false;$info.CreateNoWindow=$true
    $info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true;$info.RedirectStandardInput=$true
    foreach($part in @('-NoLogo','-NoProfile','-File',$wrapper,'-ArtifactRoot',$root,'-LinuxArtifactRoot',$linuxRoot,
        '-PythonExecutable',$gate.runtime.python_executable,'-Controller',(Convert-LinuxPath $controller),'-ControllerArgumentsFile',$specPath,
        '-RunId',$RunId,'-TimeoutSeconds','120','-CleanupGraceSeconds','30','-WslCrashDirectory',$crashRoot,'-Execute')){$info.ArgumentList.Add($part)}
    $process=[Diagnostics.Process]::new();$process.StartInfo=$info
    $out=[IO.FileStream]::new((Join-Path $root 'parent.stdout.log'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    $err=[IO.FileStream]::new((Join-Path $root 'parent.stderr.log'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try{
        if(-not $process.Start()){throw 'Could not create the own Windows parent'}
        $process.StandardInput.Close()
        return @{process=$process;windows_identity=@{pid=$process.Id;started_utc=$process.StartTime.ToUniversalTime().ToString('o')};stdout=$out;stderr=$err;outTask=$process.StandardOutput.BaseStream.CopyToAsync($out);errTask=$process.StandardError.BaseStream.CopyToAsync($err);closed=$false}
    }catch{if($process -and -not $process.HasExited){$process.Kill();$null=$process.WaitForExit(5000)};$out.Dispose();$err.Dispose();throw}
}
function Complete-ParentStreams($Parent, $Observer) {
    # A killed parent's descendants may still hold inherited pipe handles.
    # Its retained process exit is recorded immediately; EOF is independently
    # required only once the external Linux observer has actually exited.
    if(-not $Observer -or -not $Observer.process.HasExited){throw 'Cannot finalize parent streams before oracle actual exit'}
    Close-StreamProcess $Parent
}
function Invoke-ParentLossFinalization(
    [scriptblock]$Cleanup, [scriptblock]$Restore, [scriptblock]$Dispose,
    [scriptblock]$Publish, [Collections.Generic.List[string]]$Failures
) {
    # Release the inhibitor even when cleanup raises from Kill/Wait/Flush.
    # Hashing/report publication is deliberately AFTER release, and disposal
    # failures are known before the sole final verdict is published.
    try {
        try { & $Cleanup } catch { $Failures.Add('Cleanup: '+$_.Exception.Message) }
    } finally {
        try {
            try { & $Restore } catch { $Failures.Add('Sleep restoration: '+$_.Exception.Message) }
        } finally {
            try { & $Dispose } catch { $Failures.Add('Handle disposal: '+$_.Exception.Message) }
            & $Publish
        }
    }
}
try{
    $sleepState=Start-QualificationSleepInhibition
    $powerStart=Get-OraclePowerState 'initial'
    if(-not (Test-QualificationPowerStateEqual $powerStart $gate.power_profile)){throw 'Power differs from gate'}
    $crashes=Get-CrashMetadata
    Observe-Windows
    $observer=Start-StreamProcess @($gate.runtime.python_executable,'-I','-B',(Convert-LinuxPath $OracleScript),'--execute','--artifact-root',$linuxRoot,'--supervisor',(Convert-LinuxPath $supervisor),'--crash-dir',(Convert-LinuxPath $crashRoot),'--run-id',$RunId) (Join-Path $root 'oracle-linux.stdout.log') (Join-Path $root 'oracle-linux.stderr.log') $true
    while(-not (Test-Path -LiteralPath (Join-Path $root 'oracle-linux-ready.json'))){
        Observe-Windows
        if($observer.process.HasExited -or $clock.Elapsed.TotalSeconds -gt 15){throw 'Linux foreground oracle did not become ready'}
        Start-Sleep -Milliseconds 200
    }
    $ready=Read-Json (Join-Path $root 'oracle-linux-ready.json')
    if($ready.run_id -cne $RunId -or $ready.foreground_before_parent -ne $true){throw 'Wrong observer readiness'}
    $parent=Start-WindowsParent
    while(-not (Test-Path -LiteralPath (Join-Path $root 'parent-loss-ready.json'))){
        Observe-Windows
        if($observer.process.HasExited -or $parent.process.HasExited -or $clock.Elapsed.TotalSeconds -gt 60){throw 'Live three-pidfd fixture readiness failed'}
        Start-Sleep -Milliseconds 200
    }
    $pidfds=Read-Json (Join-Path $root 'parent-loss-ready.json')
    if($pidfds.run_id -cne $RunId -or $pidfds.pidfds_open -ne $true -or $pidfds.environment_cleared -ne $true -or $parent.process.HasExited){throw 'Parent-loss admission changed'}
    # Retained Process object only: no numeric PID lookup, tree kill or global kill.
    $parent.process.Kill();$parentKilled=$true
    if(-not $parent.process.WaitForExit(5000)){throw 'Own Windows parent did not exit'}
    $parentExit=$true
    $null=Write-AtomicJson (Join-Path $root 'parent-loss-trigger.json') @{run_id=$RunId;scope_unit=$pidfds.scope_unit;windows_parent_identity=$parent.windows_identity;windows_parent_exit_verified=$true;returncode=$parent.process.ExitCode;retained_handle=$true;observed_utc=[DateTime]::UtcNow.ToString('o')}
    while(-not $observer.process.HasExited){
        Observe-Windows
        if($clock.Elapsed.TotalSeconds -gt 210){throw 'Read-only Linux observer exceeded its own deadline'}
        Start-Sleep -Milliseconds 250
    }
    $observerExit=$true;$observerExitAt=$clock.Elapsed.TotalSeconds;Close-StreamProcess $observer
    if($observer.process.ExitCode -ne 0){throw 'Pidfd oracle returned nonzero'}
    $linux=Read-Json (Join-Path $root 'parent-loss-linux.json')
    if($linux.run_id -cne $RunId -or $linux.passed -ne $true -or $linux.all_retained_pidfds_exited -ne $true -or $linux.scope_empty_verified -ne $true -or $linux.post_cleanup_seconds -lt 30 -or $linux.initial_boot_id -cne $ready.initial_boot_id -or $linux.final_boot_id -cne $ready.initial_boot_id){throw 'Independent pidfd/scope/boot proof incomplete'}
    while($clock.Elapsed.TotalSeconds -lt $observerExitAt+30){Observe-Windows;Start-Sleep -Milliseconds 250}
    $report.linux_oracle_sha256=Get-Sha (Join-Path $root 'parent-loss-linux.json')
    $report.scope_unit=$linux.scope_unit;$report.initial_boot_id=$linux.initial_boot_id;$report.final_boot_id=$linux.final_boot_id
}catch{$errors.Add($_.Exception.Message)}
finally{
    Invoke-ParentLossFinalization -Failures $errors -Cleanup {
    # On any setup failure, stop only our Windows parent. Its independent
    # heartbeat lease and scope RuntimeMax remain responsible for Linux cleanup.
    try {
        if($parent -and -not $parent.process.HasExited){$parent.process.Kill();$script:parentKilled=$true;$script:parentExit=$parent.process.WaitForExit(5000)}
    } catch { $errors.Add('Parent stop failed: '+$_.Exception.Message) }
    if($observer){
        while(-not $observer.process.HasExited -and $clock.Elapsed.TotalSeconds -lt 220){try{Observe-Windows}catch{$errors.Add($_.Exception.Message)};Start-Sleep -Milliseconds 500}
        if($observer.process.HasExited){
            $script:observerExit=$true
            if($null -eq $observerExitAt){$script:observerExitAt=$clock.Elapsed.TotalSeconds}
            try{Close-StreamProcess $observer}catch{$errors.Add('Oracle stream finalization failed')}
            while($clock.Elapsed.TotalSeconds -lt $observerExitAt+30){try{Observe-Windows}catch{$errors.Add($_.Exception.Message)};Start-Sleep -Milliseconds 500}
        }else{$errors.Add('Oracle actual exit unverified; manual cleanup required')}
    }
    if($parent -and $parent.process.HasExited -and $observer -and $observer.process.HasExited){
        try{Complete-ParentStreams $parent $observer;$script:parentStreamsComplete=$parent.closed}
        catch{$errors.Add('Parent stream finalization failed: '+$_.Exception.Message)}
    }
    try{$script:nextPower=0;$script:nextKernel=0;Observe-Windows}catch{$errors.Add($_.Exception.Message)}
    } -Restore {
        Stop-QualificationSleepInhibition $sleepState
    } -Dispose {
        foreach($item in @($parent,$observer)) {
            try { if($item -and $item.process.HasExited){$item.process.Dispose()} }
            catch { $errors.Add('Process handle disposal failed: '+$_.Exception.Message) }
        }
    } -Publish {
    foreach($path in $hashes.Keys){
        try{if((Get-Sha $path) -cne $hashes[$path]){$errors.Add('Fixture input changed')}}
        catch{$errors.Add('Fixture input hash could not be verified: '+$_.Exception.Message)}
    }
    $report.errors=@($errors);$report.new_wsl_dumps=@($newDumps);$report.sleep_inhibition=$sleepState
    $report.windows_parent_exit_verified=$parentExit;$report.parent_loss_triggered=$parentKilled;$report.oracle_actual_exit_verified=$observerExit
    $report.parent_streams_complete=$parentStreamsComplete
    $report.external_post_oracle_exit_seconds=$(if($null -ne $observerExitAt){$clock.Elapsed.TotalSeconds-$observerExitAt}else{0})
    $report.power_profile_start=$powerStart;$report.power_profile_end=$powerEnd;$report.hashes=$hashes
    $report.final_kernel_probe_count=$kernelProbeCount
    $report.last_kernel_observed_elapsed_seconds=$lastKernelObservedAt
    $report.child_environment_cleared=($null -ne $pidfds -and $pidfds.environment_cleared -eq $true)
    $report.child_nondumpable_tested=$false
    $report.passed=($errors.Count -eq 0 -and $parentKilled -and $parentExit -and $parentStreamsComplete -and $observerExit -and $null -ne $sleepState -and $sleepState.restored -and $kernelProbeCount -gt 0 -and $null -ne $lastKernelObservedAt -and $lastKernelObservedAt -ge $observerExitAt+30 -and $report.external_post_oracle_exit_seconds -ge 30)
    $null=Write-AtomicJson (Join-Path $root 'fixture-oracle.json') $report
    }
}
$report|ConvertTo-Json -Depth 8
if(-not $report.passed){exit 1}
