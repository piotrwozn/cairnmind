#Requires -Version 7.2
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Root,[switch]$Execute)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if(-not $Execute){throw 'Explicit execution is required'}
$physical='C:\Users\piotr\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\DevBrain\qualification'
$tools=Join-Path $physical 'guards080-20261107T204028Z\tools'
$wrapper=Join-Path $tools 'run-supervised-080.ps1'
if((Get-FileHash -LiteralPath $wrapper -Algorithm SHA256).Hash.ToLowerInvariant() -cne 'f806b8a45887892a491bf7e878b1c090eeb2c12c44e42369235fe07e80d79a2b'){throw 'Frozen wrapper changed'}
$tokens=$null;$parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($wrapper,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Frozen wrapper syntax error'}
foreach($name in @('Assert-PlainPath','Convert-LinuxPath','Read-Json','Write-AtomicJson','Get-Sha','Get-CrashMetadata','Start-StreamProcess','Close-StreamProcess')){
 $found=@($ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true))
 if($found.Count -ne 1){throw 'Unexpected wrapper helper'}
 . ([scriptblock]::Create($found[0].Extent.Text))
}
$root=Assert-PlainPath $Root
if(-not $root.StartsWith($physical+'\restart080-',[StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath (Join-Path $root 'receipt.json'))){throw 'Fresh physical restart artifact root required'}
if((Assert-PlainPath $PSCommandPath) -cne (Join-Path $root 'run-restart-preflight080.ps1')){throw 'Execute only the physical frozen observer copy'}
$inputPath=Join-Path $root 'input.json';$probePath=Join-Path $root 'restart-preflight080.py'
$inputData=Read-Json $inputPath
$manifest=Read-Json (Join-Path $root 'freeze.json')
foreach($name in $manifest.files.Keys){if((Get-Sha (Join-Path $root $name)) -cne $manifest.files[$name]){throw 'Frozen preflight input changed'}}
$powerHelper=Join-Path $tools 'qualification-power-080.ps1'
if((Get-Sha $powerHelper) -cne '0a46c1d6a2abca8a4be4dfc79daaff33332e91f95d4c627fe7c301523ba9e8a9'){throw 'Power helper changed'}
. $powerHelper
$crashRoot=Assert-PlainPath 'C:\Users\piotr\AppData\Local\Temp\wsl-crashes'
$errors=[Collections.Generic.List[string]]::new()
$watch=[Diagnostics.Stopwatch]::StartNew();$child=$null;$probe=$null;$sleepState=$null;$actualExit=$null;$childExitAt=$null;$finalObservationAt=$null;$linux=$null;$boot=$null;$probeNumber=0;$powerEnd=$null
$winBoot=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
$report=@{schema_version=1;scope='restart080_readonly_preflight';run_id=$inputData.run_id;passed=$false;historical_qualification_replaced=$false;errors=@();application_started=$false;windows_boot_start=$winBoot;native_failure=$false;cleanup_complete=$false;final_runtime_hashes_verified=$false}
function Check-Host {
 $current=Get-CrashMetadata
 if($current.Count -ne $inputData.crashes.Count){throw 'Global WSL dump inventory changed'}
 foreach($key in $inputData.crashes.Keys){if(-not $current.ContainsKey($key) -or $current[$key] -cne $inputData.crashes[$key]){throw 'Global WSL dump metadata changed'}}
 $power=Get-QualificationPowerState
 if(-not (Test-QualificationPowerStateEqual $inputData.gate.power_profile $power)){throw 'Power fingerprint differs from qualification'}
 [IO.File]::AppendAllText((Join-Path $root 'power-samples.jsonl'),(@{elapsed_seconds=$watch.Elapsed.TotalSeconds;state=$power}|ConvertTo-Json -Depth 6 -Compress)+"`n",[Text.UTF8Encoding]::new($false))
 $script:powerEnd=$power
 if((Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o') -cne $winBoot){throw 'Windows boot changed'}
}
try{
 $sleepState=Start-QualificationSleepInhibition
 Check-Host
 $report.power_start=$powerEnd
 $child=Start-StreamProcess @($inputData.python_launcher,'-I','-B',(Convert-LinuxPath $probePath),'--input',(Convert-LinuxPath $inputPath)) (Join-Path $root 'metadata.stdout.json') (Join-Path $root 'metadata.stderr.log') $true
 while(-not $child.process.HasExited){
  if($watch.Elapsed.TotalSeconds -gt 75){throw 'Metadata probe exceeded bounded startup deadline'}
  Check-Host;Start-Sleep -Milliseconds 1000
 }
 $actualExit=$child.process.ExitCode;$childExitAt=$watch.Elapsed.TotalSeconds
 Close-StreamProcess $child
 $null=Write-AtomicJson (Join-Path $root 'metadata-exit.json') @{actual_proxy_exit=$actualExit;actual_exit_verified=$true;identity=$child.windows_identity;elapsed_seconds=$childExitAt;stdio_complete=$true}
 $linux=Read-Json (Join-Path $root 'metadata.stdout.json')
 if($linux.run_id -cne $inputData.run_id -or $actualExit -ne 0 -or $linux.passed -ne $true){throw ('Metadata probe failed: '+($linux.errors -join '; '))}
 $boot=$linux.initial_boot_id
}catch{$errors.Add($_.Exception.Message)}
finally{
 try{
  # Even a nonzero metadata probe receives a late native-failure watch. Never
  # retry it or start an application. A timeout can never qualify cleanup.
  if($child -and -not $child.process.HasExited){
   try{$child.process.Kill();$null=$child.process.WaitForExit(5000)}catch{$errors.Add('Owned WSL proxy could not be stopped')}
   $errors.Add('Timed-out Linux probe cleanup is unverified; manual admission required')
  }
  if($child -and $child.process.HasExited){
   $actualExit=$child.process.ExitCode
   if($null -eq $childExitAt){$childExitAt=$watch.Elapsed.TotalSeconds}
   try{Close-StreamProcess $child}catch{$errors.Add('Metadata stream finalization failed')}
   do{
    try{
     Check-Host
     if($watch.Elapsed.TotalSeconds -gt 120){throw 'Overall preflight observation exceeded 120 seconds'}
     $probeNumber++
     $finalRuntimeCheck=($watch.Elapsed.TotalSeconds -ge $childExitAt+30)
     $probeArguments=@('/bin/sh','-c','set -eu; /usr/bin/cat /proc/sys/kernel/random/boot_id; /usr/bin/dmesg --raw')
     if($finalRuntimeCheck){$probeArguments=@('/bin/sh','-c','set -eu; /usr/bin/cat /proc/sys/kernel/random/boot_id; /usr/bin/dmesg --raw; /usr/bin/sha256sum "$@"','restart-preflight080',$inputData.gate.runtime.python_executable,$inputData.gate.runtime.java_executable,$inputData.gate.runtime.tmux_executable)}
     $probe=Start-StreamProcess $probeArguments (Join-Path $root ('late-'+$probeNumber+'.stdout.log')) (Join-Path $root ('late-'+$probeNumber+'.stderr.log')) $true
     if(-not $probe.process.WaitForExit(5000)){throw 'Late native metadata probe timed out'}
     Close-StreamProcess $probe
     if($probe.process.ExitCode -ne 0){throw 'Late native metadata probe returned nonzero'}
     $lines=[IO.File]::ReadAllLines($probe.path)
     if($lines.Length -lt 2 -or $lines[0] -notmatch '^[0-9a-f-]{36}$'){throw 'Missing boot/kernel evidence'}
     if($null -eq $boot){$boot=$lines[0]}
     if($boot -cne $lines[0]){throw 'WSL boot changed during preflight'}
     if(@($lines|Where-Object {$_ -match 'segfault|segmentation fault|fatal signal|SIGSEGV|SIGABRT|invalid opcode|general protection fault|oom-kill|Out of memory|Kernel panic'}).Count){$report.native_failure=$true;throw 'Native fatal present in current boot'}
     if($finalRuntimeCheck){
      foreach($runtimeName in @('python','java','tmux')){
       $expectedLine=$inputData.gate.runtime[$runtimeName+'_binary_sha256']+'  '+$inputData.gate.runtime[$runtimeName+'_executable']
       if(@($lines|Where-Object {$_ -ceq $expectedLine}).Count -ne 1){throw ('Final runtime hash changed: '+$runtimeName)}
      }
      $report.final_runtime_hashes_verified=$true
     }
     $finalObservationAt=$watch.Elapsed.TotalSeconds
    }catch{$errors.Add($_.Exception.Message);break}
    finally{
     if($probe){
      if(-not $probe.process.HasExited){try{$probe.process.Kill();$null=$probe.process.WaitForExit(1000)}catch{};$errors.Add('Late WSL probe cleanup unverified')}
      if($probe.process.HasExited){try{Close-StreamProcess $probe}catch{$errors.Add('Late stream finalization failed')};$probe.process.Dispose()}
      $probe=$null
     }
    }
    if($watch.Elapsed.TotalSeconds -lt $childExitAt+30){Start-Sleep -Milliseconds 1500}
   }while($null -eq $finalObservationAt -or $finalObservationAt -lt $childExitAt+30 -or -not $finalRuntimeCheck)
  }
  try{Check-Host}catch{$errors.Add($_.Exception.Message)}
  foreach($name in $manifest.files.Keys){if((Get-Sha (Join-Path $root $name)) -cne $manifest.files[$name]){$errors.Add('Frozen input changed: '+$name)}}
 }finally{
  try{Stop-QualificationSleepInhibition $sleepState}catch{$errors.Add('Sleep inhibition restoration failed')}
 }
 $report.errors=@($errors);$report.sleep_inhibition=$sleepState;$report.actual_proxy_exit=$actualExit
 $report.actual_exit_verified=($child -and $child.process.HasExited)
 $report.post_exit_observed_seconds=$(if($null -ne $childExitAt -and $null -ne $finalObservationAt){$finalObservationAt-$childExitAt}else{0})
 $report.wsl_boot_id=$boot;$report.windows_boot_end=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
 $report.power_end=$powerEnd;$report.elapsed_seconds=$watch.Elapsed.TotalSeconds;$report.late_probe_count=$probeNumber
 $report.cleanup_complete=($errors.Count -eq 0 -and $report.actual_exit_verified -and $report.post_exit_observed_seconds -ge 30)
 $report.passed=($report.cleanup_complete -and $actualExit -eq 0 -and $null -ne $linux -and $linux.passed -eq $true -and $sleepState.restored -and $report.final_runtime_hashes_verified)
 $report.input_sha256=Get-Sha $inputPath;$report.metadata_sha256=$(if(Test-Path -LiteralPath (Join-Path $root 'metadata.stdout.json')){Get-Sha (Join-Path $root 'metadata.stdout.json')}else{$null})
 $null=Write-AtomicJson (Join-Path $root 'receipt.json') $report
 if($child -and $child.process.HasExited){$child.process.Dispose()}
}
$report|Select-Object run_id,passed,actual_proxy_exit,cleanup_complete,post_exit_observed_seconds,elapsed_seconds,errors|ConvertTo-Json -Depth 4
if(-not $report.passed){exit 1}
