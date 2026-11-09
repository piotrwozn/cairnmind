#Requires -Version 7.2
[CmdletBinding()]
param([switch]$Execute,[string]$InputFreezeSha256)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
if(-not $Execute){Write-Output 'NOT_RUN: read-only comparison requires explicit serial handoff and -Execute.';return}
if($InputFreezeSha256 -cnotmatch '^[0-9a-f]{64}$'){throw 'Exact prepared input-freeze digest is required'}
$root=$PSScriptRoot
$tools='C:\Users\piotr\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\DevBrain\qualification\guards-task-state080-20261108T193425Z\tools'
$wrapper=Join-Path $tools 'run-supervised-080.ps1'
if((Get-FileHash -LiteralPath $wrapper -Algorithm SHA256).Hash.ToLowerInvariant() -cne '721109e2295099fcc4dea6e1a1bc55e773e577e2de673931c33a7e4bb7812c74'){throw 'Frozen observer functions changed'}
$tokens=$null;$parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($wrapper,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Frozen observer syntax changed'}
foreach($name in @('Assert-PlainPath','Convert-LinuxPath','Read-Json','Write-AtomicJson','Get-Sha','Get-CrashMetadata','Start-StreamProcess','Close-StreamProcess')){
 $found=@($ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true))
 if($found.Count -ne 1){throw 'Unexpected observer function inventory'}
 . ([scriptblock]::Create($found[0].Extent.Text))
}
$root=Assert-PlainPath $root
$physicalBase='C:\Users\piotr\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\DevBrain\qualification'
if((Split-Path $root -Parent) -cne $physicalBase -or (Split-Path $root -Leaf) -cnotmatch '^source-preflight-(baseline072|target080)-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$'){throw 'Only a fresh physical comparison envelope may execute'}
$art=Assert-PlainPath (Join-Path $root 'artifacts')
if(Test-Path -LiteralPath (Join-Path $art 'source-preflight-started.json')){throw 'Never repeat an attempted comparison in place'}
$freezePath=Assert-PlainPath (Join-Path $root 'input-freeze.json')
if((Get-Sha $freezePath) -cne $InputFreezeSha256){throw 'Prepared comparison input-freeze differs'}
$freeze=Read-Json $freezePath
# All plan/checker/launcher bytes are checked BEFORE trusting the launch plan.
foreach($rel in $freeze.files.Keys){
 if($rel -match '(^|/)\.\.(/|$)|\\|:' -or $rel.StartsWith('/')){throw 'Unsafe frozen input path'}
 $path=Assert-PlainPath (Join-Path $root $rel)
 if(-not $path.StartsWith($root+'\',[StringComparison]::Ordinal) -or (Get-Sha $path) -cne $freeze.files[$rel]){throw 'Frozen comparison input changed'}
}
$plan=Read-Json (Join-Path $root 'launch-plan.json')
$gate=Read-Json (Join-Path $root 'inputs/gate.json')
$powerHelper=Join-Path $tools 'qualification-power-080.ps1'
if((Get-Sha $powerHelper) -cne '0a46c1d6a2abca8a4be4dfc79daaff33332e91f95d4c627fe7c301523ba9e8a9'){throw 'Frozen power helper changed'}
. $powerHelper
$script:crashRoot=Assert-PlainPath 'C:\Users\piotr\AppData\Local\Temp\wsl-crashes'
$watch=[Diagnostics.Stopwatch]::StartNew();$errors=[Collections.Generic.List[string]]::new()
$child=$null;$sleepState=$null;$actualExit=$null;$exitAt=$null;$lastNativeAt=$null;$boot=$null;$probeNumber=0;$powerEnd=$null
$initialCrashes=Get-CrashMetadata
$winBoot=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
$report=@{schema_version=1;scope='devbrain_readonly_source_process_observation';run_id=$plan.run_id;status='running';passed=$false;qualified=$false;application_qualification=$false;production_modified=$false;cleanup_complete=$false;manual_pid_birth_cleanup_admission_required=$false;errors=@();output_sha256=@{};comparison_validated=$false;registered_controller=$false;mode=$plan.mode;point_in_time_only=$true;deployment_authorized=$false;app_imported=$false;services_or_models_started=$false;source_contents_or_actual_digests_published=$false}
function Verify-Inputs {
 if((Get-Sha $freezePath) -cne $InputFreezeSha256){throw 'Input freeze changed'}
 foreach($rel in $freeze.files.Keys){if((Get-Sha (Assert-PlainPath (Join-Path $root $rel))) -cne $freeze.files[$rel]){throw 'Frozen input changed'}}
 foreach($name in $plan.observer_helper_sha256.Keys){if((Get-Sha (Assert-PlainPath (Join-Path $tools $name))) -cne $plan.observer_helper_sha256[$name]){throw 'Qualified observer dependency changed'}}
 if($freeze.scope -cne 'devbrain_readonly_source_observer_inputs' -or $freeze.run_id -cne $plan.run_id -or $plan.windows_root -cne $root -or $plan.production_source -cne '/home/piotr/src/devbrain'){throw 'Comparison root/source binding differs'}
 if($plan.checker_sha256 -cne '47838bbff07f73639563c43d0f37c5c92e3dc8ff7ab0529132e4d4fad570b8b3' -or (Get-Sha (Join-Path $root 'check-production-source080.py')) -cne $plan.checker_sha256){throw 'Reviewed no-child read-only checker differs'}
 $modes=@{baseline072=@(182,64,'6b65e7315895b8c44b84bafc9146dfcdae732c3890f959d0e5e58cbf4fd9aa2c');target080=@(198,69,'4fb8b1203dfce52eb5bfd094b2649899316e09ada64cbd2dc14e1bb8de071508')}
 if(-not $modes.ContainsKey($plan.mode)){throw 'Unknown source comparison mode'}
 $mode=$modes[$plan.mode]
 if($plan.expected_file_count -ne $mode[0] -or $plan.expected_package_file_count -ne $mode[1] -or $plan.release_zip_sha256 -cne $mode[2]){throw 'Fixed public release inventory differs'}
 if((Get-Sha (Join-Path $root 'inputs/release.zip')) -cne $mode[2] -or (Get-Sha (Join-Path $root 'inputs/gate.json')) -cne 'a6bd365cea68986ed6795990272c766679bbd29fd9407e904a12c2956bcdf7fb'){throw 'Pinned ZIP/runtime gate differs'}
 if($gate.status -cne 'pass' -or $plan.python_launcher -cne $gate.runtime.python_executable){throw 'Direct qualified base Python required'}
 $expected=@($gate.runtime.python_executable,'-I','-B',(Convert-LinuxPath (Join-Path $root 'check-production-source080.py')),'--execute','--mode',$plan.mode,'--source-dir','/home/piotr/src/devbrain','--release-zip',(Convert-LinuxPath (Join-Path $root 'inputs/release.zip')),'--runtime-gate',(Convert-LinuxPath (Join-Path $root 'inputs/gate.json')),'--output',(Convert-LinuxPath (Join-Path $art 'source-comparison.json')))
 $bounded=@('/usr/bin/timeout','--signal=TERM','--kill-after=5s','60s')+$expected
 if($plan.checker_argv.Count -ne 15 -or $plan.bounded_linux_argv.Count -ne 19){throw 'Exact checker/timeout argv schema required'}
 for($i=0;$i -lt 15;$i++){if($plan.checker_argv[$i] -cne $expected[$i]){throw 'Checker argv differs'}}
 for($i=0;$i -lt 19;$i++){if($plan.bounded_linux_argv[$i] -cne $bounded[$i]){throw 'Bounded checker argv differs'}}
 if($plan.canonical_output -cne (Join-Path $art 'source-comparison.json') -or $plan.canonical_linux_output -cne $expected[-1]){throw 'Fresh canonical comparison output differs'}
}
function Observe-Host {
 $current=Get-CrashMetadata
 if($current.Count -ne $initialCrashes.Count){throw 'Global WSL dump inventory changed'}
 foreach($key in $initialCrashes.Keys){if(-not $current.ContainsKey($key) -or $current[$key] -cne $initialCrashes[$key]){throw 'Global WSL dump metadata changed'}}
 $power=Get-QualificationPowerState
 if(-not (Test-QualificationPowerStateEqual $gate.power_profile $power)){throw 'Power fingerprint changed'}
 if((Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o') -cne $winBoot){throw 'Windows boot changed'}
 [IO.File]::AppendAllText((Join-Path $art 'power-samples.jsonl'),(@{elapsed_seconds=$watch.Elapsed.TotalSeconds;state=$power}|ConvertTo-Json -Depth 6 -Compress)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))
 $script:powerEnd=$power
}
function Observe-Linux {
 $script:probeNumber++
 $probe=$null
 try{
  $args=@('/usr/bin/timeout','--signal=TERM','--kill-after=1s','6s','/bin/sh','-c','set -eu; /usr/bin/cat /proc/sys/kernel/random/boot_id; /usr/bin/dmesg --raw; /usr/bin/sha256sum "$@"; /usr/bin/readlink -f "$1"','source-preflight-watch',$gate.runtime.python_executable)
  $probe=Start-StreamProcess $args (Join-Path $art ('native-'+$probeNumber+'.stdout.log')) (Join-Path $art ('native-'+$probeNumber+'.stderr.log')) $true
  if(-not $probe.process.WaitForExit(8000)){throw 'Bounded native observation did not exit'}
  Close-StreamProcess $probe
  if($probe.process.ExitCode -ne 0){throw 'Native observation failed'}
  $lines=[IO.File]::ReadAllLines($probe.path)
  if($lines.Count -lt 2 -or $lines[0] -notmatch '^[0-9a-f-]{36}$'){throw 'Missing native boot/hash evidence'}
  if($null -eq $script:boot){$script:boot=$lines[0]}
  if($script:boot -cne $lines[0]){throw 'WSL boot changed during comparison observation'}
  if(@($lines|Where-Object {$_ -match 'segfault|segmentation fault|fatal signal|SIGSEGV|SIGABRT|invalid opcode|general protection fault|oom-kill|Out of memory|Kernel panic'}).Count){throw 'Native fatal found in current kernel boot'}
  $expected=$gate.runtime.python_binary_sha256+'  '+$gate.runtime.python_executable
  if(@($lines|Where-Object {$_ -ceq $expected}).Count -ne 1){throw 'Qualified Python binary hash changed'}
  if(@($lines|Where-Object {$_ -ceq $gate.runtime.python_executable}).Count -ne 1){throw 'Qualified base Python canonical identity changed'}

  $script:lastNativeAt=$watch.Elapsed.TotalSeconds
 }finally{
  if($probe){
   if(-not $probe.process.HasExited){try{$probe.process.Kill();$null=$probe.process.WaitForExit(1000)}finally{$report.manual_pid_birth_cleanup_admission_required=$true}}
   if($probe.process.HasExited){try{Close-StreamProcess $probe}finally{$probe.process.Dispose()}}
  }
 }
}
function Verify-Outputs {
 $path=Assert-PlainPath $plan.canonical_output
 $result=Read-Json $path
 $allowed=@('schema_version','scope','status','mode','production_modified','app_imported','services_or_models_started','source_contents_or_actual_digests_published','deployment_authorized','external_actual_exit_observation_required','point_in_time_only','expected_file_count','expected_package_file_count','expected_release_zip_sha256','comparison','two_pass_identity_stable','comparison_passed','elapsed_seconds')
 if(@(Compare-Object @($result.Keys|Sort-Object) @($allowed|Sort-Object)).Count){throw 'Unexpected output fields; do not publish private metadata'}
 foreach($name in @('production_modified','app_imported','services_or_models_started','source_contents_or_actual_digests_published','deployment_authorized','external_actual_exit_observation_required','point_in_time_only','two_pass_identity_stable','comparison_passed')){if($result[$name] -isnot [bool]){throw 'Boolean output field has wrong JSON type'}}
 if($result.schema_version -ne 1 -or $result.scope -cne 'devbrain_readonly_release_source_comparison' -or $result.status -cne 'matched_pending_external_observation' -or $result.mode -cne $plan.mode -or $result.comparison_passed -ne $true -or $result.two_pass_identity_stable -ne $true){throw 'Comparison did not match fixed inventory'}
 foreach($name in @('production_modified','app_imported','services_or_models_started','source_contents_or_actual_digests_published','deployment_authorized')){if($result[$name] -ne $false){throw 'Read-only/privacy boundary differs'}}
 if($result.point_in_time_only -ne $true -or $result.external_actual_exit_observation_required -ne $true -or $result.expected_file_count -ne $plan.expected_file_count -or $result.expected_package_file_count -ne $plan.expected_package_file_count -or $result.expected_release_zip_sha256 -cne $plan.release_zip_sha256){throw 'Output release boundary differs'}
 $counts=$result.comparison
 if(@(Compare-Object @($counts.Keys|Sort-Object) @('missing','modified','unexpected','ignored_subtrees','ignored_bytecode','entries','matched'|Sort-Object)).Count){throw 'Unexpected comparison fields'}
 foreach($name in $counts.Keys){if($counts[$name] -isnot [long] -and $counts[$name] -isnot [int]){throw 'Noninteger comparison count'};if($counts[$name] -lt 0 -or $counts[$name] -gt 20000){throw 'Comparison count outside bound'}}
 foreach($name in @('missing','modified','unexpected')){if($counts[$name] -ne 0){throw 'Production source has a mismatch; never overwrite'}}
 if($counts.matched -ne $plan.expected_file_count -or $counts.entries -lt $counts.matched -or [double]::IsNaN([double]$result.elapsed_seconds) -or [double]::IsInfinity([double]$result.elapsed_seconds) -or $result.elapsed_seconds -lt 0 -or $result.elapsed_seconds -gt 60){throw 'Incomplete/bounded comparison failed'}
 $report.comparison_validated=$true;$report.output_path=$path
 $report.output_sha256['source-comparison.json']=Get-Sha $path
 $report.comparison_counts=$counts;$report.expected_file_count=$plan.expected_file_count;$report.expected_package_file_count=$plan.expected_package_file_count
}

try{
 Verify-Inputs
 $claim=[IO.File]::Open((Join-Path $art 'source-preflight-started.json'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
 try{$bytes=[Text.UTF8Encoding]::new($false).GetBytes((@{run_id=$plan.run_id;input_freeze_sha256=$InputFreezeSha256;single_attempt=$true;production_modified=$false}|ConvertTo-Json -Compress));$claim.Write($bytes,0,$bytes.Length);$claim.Flush($true)}finally{$claim.Dispose()}
 if(Test-Path -LiteralPath $plan.canonical_output){throw 'Canonical comparison output must be fresh; never overwrite'}
 $sleepState=Start-QualificationSleepInhibition
 Observe-Host;Observe-Linux
 $report.initial_boot_id=$boot;$report.power_start=$powerEnd
 # Retain the timeout wrapper PID/birth before exec. A nonzero/timeout is never
 # admitted as clean: exact Linux process cleanup must be independently proven.
 $identity=Convert-LinuxPath (Join-Path $art 'timeout-process-identity.txt')
 $args=@('/bin/sh','-c','set -eu; umask 077; printf "%s\n" "$$" > "$1"; /usr/bin/cat "/proc/$$/stat" >> "$1"; shift; exec "$@"','standalone-source-comparison',$identity)+@($plan.bounded_linux_argv)
 $child=Start-StreamProcess $args (Join-Path $art 'comparison.stdout.log') (Join-Path $art 'comparison.stderr.log') $true
 $report.windows_child_identity=$child.windows_identity
 while(-not $child.process.HasExited){
  if($child.started.Elapsed.TotalSeconds -gt 72){throw 'Bounded checker proxy failed to exit'}
  Observe-Host;Observe-Linux;Start-Sleep -Milliseconds 250
 }
 $actualExit=$child.process.ExitCode;$exitAt=$watch.Elapsed.TotalSeconds
 Close-StreamProcess $child
 $null=Write-AtomicJson (Join-Path $art 'comparison-exit.json') @{actual_proxy_exit=$actualExit;actual_exit_verified=$true;windows_identity=$child.windows_identity;stdio_complete=$child.closed;elapsed_seconds=$exitAt}
 if($actualExit -ne 0){$report.manual_pid_birth_cleanup_admission_required=$true;throw 'Checker nonzero/timeout; preserve mismatch and require cleanup admission'}
}catch{$errors.Add($_.Exception.Message)}
finally{
 try{
  if($child -and -not $child.process.HasExited){
   $report.manual_pid_birth_cleanup_admission_required=$true
   try{$child.process.Kill();$null=$child.process.WaitForExit(1000)}catch{$errors.Add('Exact owned proxy stop failed')}
   $errors.Add('Checker cleanup not proven; no further workload is admitted')
  }
  if($child -and $child.process.HasExited){
   $actualExit=$child.process.ExitCode
   if($null -eq $exitAt){$exitAt=$watch.Elapsed.TotalSeconds}
   if($actualExit -ne 0){$report.manual_pid_birth_cleanup_admission_required=$true}
   try{Close-StreamProcess $child}catch{$errors.Add('Checker streams incomplete')}
   do{
    try{Observe-Host;Observe-Linux}catch{$errors.Add($_.Exception.Message)}
    if($watch.Elapsed.TotalSeconds -gt $exitAt+45){$errors.Add('Late observation could not complete its required bound');break}
    if($null -eq $lastNativeAt -or $lastNativeAt -lt $exitAt+30){Start-Sleep -Milliseconds 1500}
   }while($null -eq $lastNativeAt -or $lastNativeAt -lt $exitAt+30)
  }
  try{Observe-Host;Verify-Inputs;if($actualExit -eq 0){Verify-Outputs}}catch{$errors.Add($_.Exception.Message)}
 }finally{
  try{Stop-QualificationSleepInhibition $sleepState}catch{$errors.Add('Sleep inhibition restoration failed')}
 }
 $report.actual_proxy_exit=$actualExit;$report.actual_exit_verified=[bool]($child -and $child.process.HasExited)
 $report.stdio_complete=[bool]($child -and $child.closed);$report.final_boot_id=$boot;$report.power_end=$powerEnd
 $report.post_exit_observed_seconds=$(if($null -ne $exitAt -and $null -ne $lastNativeAt){$lastNativeAt-$exitAt}else{0})
 $report.sleep_inhibition=$sleepState;$report.errors=@($errors);$report.native_probe_count=$probeNumber;$report.elapsed_seconds=$watch.Elapsed.TotalSeconds
 $report.cleanup_complete=($errors.Count -eq 0 -and $report.actual_exit_verified -and $report.stdio_complete -and $actualExit -eq 0 -and $report.post_exit_observed_seconds -ge 30 -and -not $report.manual_pid_birth_cleanup_admission_required -and $sleepState.restored)
 $report.passed=($report.cleanup_complete -and $report.comparison_validated)
 $report.status=$(if($report.passed){'source_compared_observed'}else{'comparison_failed_or_observation_incomplete'})
 $report.input_freeze_sha256=Get-Sha $freezePath;$report.checker_sha256=$plan.checker_sha256;$report.launcher_sha256=Get-Sha $PSCommandPath;$report.release_zip_sha256=$plan.release_zip_sha256
 $null=Write-AtomicJson (Join-Path $art 'source-preflight-observation.json') $report
 if($child -and $child.process.HasExited){$child.process.Dispose()}
}
$report|Select-Object run_id,status,passed,qualified,actual_proxy_exit,cleanup_complete,post_exit_observed_seconds,manual_pid_birth_cleanup_admission_required,errors|ConvertTo-Json -Depth 5
if(-not $report.passed){exit 1}
