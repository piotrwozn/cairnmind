#Requires -Version 7.2
<#
External observer for already-qualified DevBrain controllers. No process starts
without -Execute. A missing receipt is never a PASS. This Windows process must
outlive the Linux supervisor and observe at least 30 seconds after its exit.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ArtifactRoot,
    [Parameter(Mandatory)][string]$LinuxArtifactRoot,
    [Parameter(Mandatory)][string]$PythonExecutable,
    [Parameter(Mandatory)][string]$Controller,
    [Parameter(Mandatory)][string]$ControllerArgumentsFile,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_-]{1,80}$')][string]$RunId,
    [Parameter(Mandatory)][ValidateRange(1,86400)][int]$TimeoutSeconds,
    [ValidateRange(30,120)][int]$CleanupGraceSeconds = 30,
    [string]$WslCrashDirectory = (Join-Path $env:LOCALAPPDATA 'Temp/wsl-crashes'),
    [switch]$Execute
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$fatalPattern = 'segfault|segmentation fault|fatal signal|SIGSEGV|SIGABRT|invalid opcode|general protection fault|oom-kill|Out of memory|Kernel panic'
$coreArtifacts = @('gate.json','controller-report.json','supervisor-report.json')
$runtimeKeys = @('python_executable','python_binary_sha256','java_executable','java_binary_sha256','tmux_executable','tmux_binary_sha256')
$allowedEnv = @('PATH','LANG','LC_ALL','TZ','TMPDIR','UV_CACHE_DIR','UV_OFFLINE','UV_PYTHON_DOWNLOADS','JAVA_HOME','OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','DEVBRAIN_ARCADE_DIST','DEVBRAIN_TEST_ARCADE_HOME','DEVBRAIN_TEST_UPDATER_PATH','DEVBRAIN_JAVA_EXECUTABLE','DEVBRAIN_TMUX_EXECUTABLE')

function Assert-PlainPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -notmatch '^[Cc]:\\' -or $Path.Contains([char]0)) { throw 'Only absolute C: paths can be mapped to /mnt/c.' }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $entry = Get-Item -LiteralPath $cursor -Force
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Reparse-point paths are not accepted: $cursor" }
        }
        $next = [IO.Path]::GetDirectoryName($cursor)
        if ($next -eq $cursor) { break }
        $cursor = $next
    }
    return 'C:' + $full.Substring(2).TrimEnd('\')
}
function Convert-LinuxPath([string]$Path) { return '/mnt/c/' + $Path.Substring(3).Replace('\','/') }
function Get-ArtifactPath([string]$Relative) {
    if (-not $Relative -or $Relative.Contains('\') -or $Relative.Contains(':') -or $Relative.Contains([char]0) -or $Relative.StartsWith('/') -or @($Relative.Split('/') | Where-Object { $_ -in @('','.','..') }).Count) {
        throw 'Artifact names must be normalized relative POSIX paths.'
    }
    $path = Assert-PlainPath (Join-Path $script:root $Relative)
    if (-not $path.StartsWith($script:root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Artifact leaves its root.' }
    return $path
}
function Read-Json([string]$Path) {
    $null = Assert-PlainPath $Path
    # A snapshot reader must allow an atomic publisher to replace its path.
    $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {
        if ($stream.Length -gt 8MB) { throw 'Oversized JSON artifact.' }
        $reader = [IO.StreamReader]::new($stream, [Text.UTF8Encoding]::new($false,$true), $true, 4096, $true)
        try { $raw = $reader.ReadToEnd() } finally { $reader.Dispose() }
        if ([Text.Encoding]::UTF8.GetByteCount($raw) -gt 8MB) { throw 'JSON artifact grew beyond its bound.' }
        return ($raw | ConvertFrom-Json -AsHashtable)
    } finally { $stream.Dispose() }
}
function Write-AtomicJson([string]$Path, $Value) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 40) + "`n")
    try {
        $stream = [IO.FileStream]::new($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes); $stream.Flush($true) } finally { $stream.Dispose() }
        # A cross-OS reader can briefly deny delete-sharing on NTFS. Reuse the
        # same closed temp/payload: retrying never refreshes the heartbeat stamp.
        $watch = [Diagnostics.Stopwatch]::StartNew(); $retries = 0
        while ($true) {
            try {
                [IO.File]::Move($temporary, $Path, $true)
                return @{published_utc=[DateTime]::UtcNow.ToString('o'); replacement_elapsed_ms=$watch.Elapsed.TotalMilliseconds; retries=$retries}
            } catch {
                $failure = $_.Exception
                while ($failure -and $failure -isnot [IO.IOException] -and $failure -isnot [UnauthorizedAccessException]) { $failure = $failure.InnerException }
                if (-not $failure) { throw }
                $code = $failure.HResult -band 65535
                $elapsed = $watch.Elapsed.TotalMilliseconds
                if ($code -notin @(5,32,33) -or $elapsed -ge 1000) { throw }
                $retries++
                [Console]::Error.WriteLine((@{event='atomic_replace_retry'; artifact=[IO.Path]::GetFileName($Path); win32_code=$code; elapsed_ms=$elapsed; attempt=$retries} | ConvertTo-Json -Compress))
                Start-Sleep -Milliseconds ([int][Math]::Max(1,[Math]::Min(25,1000-$elapsed)))
                if ($watch.Elapsed.TotalMilliseconds -ge 1000) { throw }
            }
        }
    } finally {
        # Only the unique temporary file created by this call, never its target.
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}
function Get-Sha([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Get-CrashMetadata {
    if (-not (Test-Path -LiteralPath $script:crashRoot -PathType Container)) { throw 'WSL crash metadata directory is unavailable.' }
    $result = @{}
    foreach ($item in Get-ChildItem -LiteralPath $script:crashRoot -File -Force -ErrorAction Stop) {
        if ($item.Name.StartsWith('wsl-crash-')) { $result[$item.Name] = "$($item.Length):$($item.LastWriteTimeUtc.Ticks)" }
    }
    return $result
}
function Start-StreamProcess([string[]]$LinuxArguments, [string]$StdoutPath, [string]$StderrPath, [bool]$Fresh) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = Join-Path $env:WINDIR 'System32/wsl.exe'
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true; $info.RedirectStandardInput = $true
    foreach ($part in @('--distribution','Ubuntu','--user','piotr','--exec') + $LinuxArguments) { $info.ArgumentList.Add($part) }
    $mode = if ($Fresh) { [IO.FileMode]::CreateNew } else { [IO.FileMode]::Create }
    $out = [IO.FileStream]::new($StdoutPath, $mode, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $err = $null; $process = $null
    try {
        $err = [IO.FileStream]::new($StderrPath, $mode, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        $process = [Diagnostics.Process]::new(); $process.StartInfo = $info
        if (-not $process.Start()) { throw 'Could not start the owned WSL proxy.' }
        $process.StandardInput.Close()
        $outTask = $process.StandardOutput.BaseStream.CopyToAsync($out)
        $errTask = $process.StandardError.BaseStream.CopyToAsync($err)
        return @{ process=$process; windows_identity=@{pid=$process.Id; started_utc=$process.StartTime.ToUniversalTime().ToString('o')}; stdout=$out; stderr=$err; outTask=$outTask; errTask=$errTask; path=$StdoutPath; errorPath=$StderrPath; started=[Diagnostics.Stopwatch]::StartNew(); closed=$false }
    } catch {
        # Only this exact Windows Process handle; never a PID or process tree.
        if ($process -and -not $process.HasExited) { $process.Kill() }
        $out.Dispose(); if ($err) { $err.Dispose() }; throw
    }
}
function Close-StreamProcess($Item) {
    if ($Item.closed) { return }
    if (-not $Item.process.HasExited) { throw 'Cannot close a live process as completed.' }
    if (-not $Item.outTask.Wait(1000) -or -not $Item.errTask.Wait(1000)) { throw 'Log stream did not complete after process exit.' }
    $Item.stdout.Flush(); $Item.stderr.Flush(); $Item.stdout.Dispose(); $Item.stderr.Dispose(); $Item.closed = $true
}
function Test-FailedBootstrapGrace([hashtable]$State, [int]$GraceSeconds) {
    # This ends an already FAILED setup observation; it never establishes scope
    # ownership or qualifies a controller that was not observed running.
    return ($State.actual_exit -eq $true -and $null -ne $State.returncode -and $State.returncode -ne 0 -and
        $State.scope_seen -eq $false -and $State.initially_absent -eq $true -and
        $State.load_state -ceq 'not-found' -and $State.population -ceq '0' -and
        -not $State.group -and -not $State.identity -and $null -ne $State.absent_since -and
        $State.last_scope -ge $State.absent_since+$GraceSeconds -and
        $State.last_probe -ge $State.absent_since+$GraceSeconds)
}
function Fail-Run([string]$Message) {
    if (-not $script:errors.Contains($Message)) { $script:errors.Add($Message) }
    if ($script:supervisor -and -not (Test-Path -LiteralPath (Join-Path $script:root 'cancel.json'))) {
        $null = Write-AtomicJson (Join-Path $script:root 'cancel.json') @{run_id=$RunId; nonce=$script:nonce; reason=$Message}
    }
}
function Observe-Power {
    $state = Get-QualificationPowerState
    if ($null -eq $script:powerStart) { $script:powerStart = $state }
    if (-not (Test-QualificationPowerStateEqual $script:powerStart $state)) {
        $script:powerChanged = $true
        Fail-Run 'Power plan or AC/DC settings changed during qualification.'
    }
    $script:powerEnd = $state
    $sample = @{elapsed_seconds=$script:clock.Elapsed.TotalSeconds; state=$state}
    [IO.File]::AppendAllText((Join-Path $script:root 'power-samples.jsonl'), ($sample | ConvertTo-Json -Depth 8 -Compress) + "`n", [Text.UTF8Encoding]::new($false))
    $script:powerSamples++
}
function Assert-Gate($Gate) {
    if ($Gate['schema_version'] -ne 1 -or $Gate['scope'] -cne 'bounded_runtime_qualification' -or $Gate['status'] -cne 'pass') { throw 'Existing runtime gate is missing or blocked. No controller will start.' }
    $checks = $Gate['checks']
    if ($checks -isnot [Collections.IDictionary]) { throw 'Gate checks are missing.' }
    foreach ($name in @('ssl_candidate','unit','native_evaluation','c2_repetitions','native_children','engines_stopped')) { if (-not $checks.Contains($name)) { throw "Missing gate check: $name" } }
    foreach ($check in $checks.Values) { if ($check -isnot [Collections.IDictionary] -or $check['pass'] -isnot [bool] -or -not $check['pass']) { throw 'Every gate check must explicitly pass.' } }
    foreach ($key in $runtimeKeys) {
        $value = $Gate['runtime'][$key]
        if ($value -isnot [string] -or ($key.EndsWith('_sha256') -and $value -cnotmatch '^[0-9a-f]{64}$') -or ($key.EndsWith('_executable') -and (-not $value.StartsWith('/') -or $value.Contains([char]0)))) { throw "Invalid runtime identity: $key" }
    }
    if ($PythonExecutable -cne $Gate['runtime']['python_executable']) { throw 'PythonExecutable differs from the passed gate.' }
}

$root = Assert-PlainPath $ArtifactRoot
if (-not (Test-Path -LiteralPath $root -PathType Container) -or (Convert-LinuxPath $root) -cne $LinuxArtifactRoot) { throw 'Artifact root must exist and have the exact C: to /mnt/c mapping.' }
$argumentsPath = Assert-PlainPath $ControllerArgumentsFile
if (-not $argumentsPath.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Controller arguments must be inside ArtifactRoot.' }
$supervisorPath = Assert-PlainPath (Join-Path $PSScriptRoot 'supervise-080.py')
$launcherPath = Assert-PlainPath $PSCommandPath
$powerHelperPath = Assert-PlainPath (Join-Path $PSScriptRoot 'qualification-power-080.ps1')
$crashRoot = Assert-PlainPath $WslCrashDirectory
if (-not $Controller.StartsWith('/') -or $Controller.Contains([char]0)) { throw 'Controller must be an absolute Linux file.' }
$spec = Read-Json $argumentsPath
if ((@($spec.Keys | Sort-Object) -join ',') -cnotin @('argv,env,required_artifacts,run_id','argv,env,python_launcher,required_artifacts,run_id') -or $spec['run_id'] -cne $RunId) { throw 'Controller specification keys or run identity do not match.' }
if ($spec.Contains('python_launcher') -and ($spec['python_launcher'] -isnot [string] -or -not $spec['python_launcher'].StartsWith('/') -or $spec['python_launcher'].Contains([char]0))) { throw 'python_launcher must be an absolute Linux path.' }
if ($spec['argv'] -isnot [array] -or $spec['env'] -isnot [Collections.IDictionary] -or $spec['required_artifacts'] -isnot [array]) { throw 'Controller argv/env/artifacts types are invalid.' }
foreach ($arg in $spec['argv']) { if ($arg -isnot [string] -or $arg.Contains([char]0)) { throw 'argv must contain only strings without NUL.' } }
foreach ($key in $spec['env'].Keys) { if ($key -cnotin $allowedEnv -or $spec['env'][$key] -isnot [string] -or $spec['env'][$key].Contains([char]0)) { throw 'Environment entry is outside the allowlist.' } }
$required = @($spec['required_artifacts'])
foreach ($relative in $required) { if ($relative -isnot [string] -or $relative -eq 'receipt.json') { throw 'Invalid required artifact.' }; $null = Get-ArtifactPath $relative }
if (@($required | Select-Object -Unique).Count -ne $required.Count) { throw 'Duplicate required artifacts.' }
foreach ($relative in $coreArtifacts) { if ($relative -cnotin $required) { throw "Missing core artifact declaration: $relative" } }
$gatePath = Get-ArtifactPath 'gate.json'
$gate = Read-Json $gatePath
Assert-Gate $gate
if (-not $Execute) {
    @{ execute=$false; message='Plan only: no files written, probes or processes started.'; run_id=$RunId; artifact_root=$root; linux_artifact_root=$LinuxArtifactRoot; controller=$Controller } | ConvertTo-Json
    return
}
foreach ($name in @('receipt.json','supervisor-report.json','controller-report.json','heartbeat.json','mapping-proof.json','cancel.json','supervisor.stdout.log','supervisor.stderr.log','controller.stdout.log','controller.stderr.log','probe-boot.stdout.log','probe-boot.stderr.log','probe-kernel.stdout.log','probe-kernel.stderr.log','probe-scope.stdout.log','probe-scope.stderr.log','scope-stop.stdout.log','scope-stop.stderr.log','power-samples.jsonl')) {
    if (Test-Path -LiteralPath (Get-ArtifactPath $name)) { throw "Refusing to overwrite prior run output: $name" }
}
$startHashes = @{ launcher=(Get-Sha $launcherPath); supervisor=(Get-Sha $supervisorPath); arguments=(Get-Sha $argumentsPath); gate=(Get-Sha $gatePath) }
$helperPaths = @{}
$controllerName = $Controller.Split('/')[-1]
$controllerDependencies = @{
    'test_devbrain_update080_smoke.py'=@('validate-native-account080.py','test_devbrain_update071_smoke.py')
    'validate-native-account080.py'=@('validate-native-account080.py')
    'validate-copilot-account080.py'=@('validate-native-account080.py','validate-copilot-account-e2e.py')
    'replay-failed-queue-080.py'=@('run-full-suite-080.py')
    'retry-production-queue-080.py'=@('validate-native-account080.py')
    'backup-production080.py'=@('retry-production-queue-080.py','validate-native-account080.py')
    'deploy-production080.py'=@('retry-production-queue-080.py','validate-native-account080.py')
    'run-interactive-ux080.py'=@('validate-native-account080.py','open-interactive-ux080.ps1')
    'run-regressions-080.py'=@('validate-native-account080.py','validate-local-recall.py')
    'run-baseline-ux072.py'=@('run-interactive-ux080.py','validate-native-account080.py','open-interactive-ux080.ps1','build-devbrain-update-080.py','devbrain-update080-template.py')
}
$controllerArtifacts = @{
    'validate-native-account080.py'=@('native-results.json'); 'validate-copilot-account080.py'=@('copilot-results.json')
    'run-interactive-ux080.py'=@('interactive-ready.json','interactive-results.json')
    'run-regressions-080.py'=@('regression-source.json','regressions-results.json')
    'run-baseline-ux072.py'=@('interactive-ready.json','interactive-results.json')
    'retry-production-queue-080.py'=@('production-admission.json','production-retry-results.json')
    'backup-production080.py'=@('production-backup-admission.json','production-backup-results.json')
    'deploy-production080.py'=@('production-deployment-admission.json','production-deployment-results.json')
}
if ($controllerName -cnotin @('run-full-suite-080.py','soak-workload-080.py','test_devbrain_update080_smoke.py','test_supervision080_fixture.py','validate-native-account080.py','validate-copilot-account080.py','replay-failed-queue-080.py','retry-production-queue-080.py','backup-production080.py','deploy-production080.py','run-interactive-ux080.py','run-regressions-080.py','run-baseline-ux072.py')) { throw 'Unsupported controller.' }
if ($controllerName -ceq 'run-regressions-080.py') {
    $positions=@(for($index=0; $index -lt $spec['argv'].Count; $index++){if($spec['argv'][$index] -ceq '--phase'){$index}})
    if($positions.Count -ne 1 -or $positions[0]+1 -ge $spec['argv'].Count -or @($spec['argv'] | Where-Object {$_.StartsWith('--phase=')}).Count) { throw 'Regression controller needs one explicit --phase value.' }
    $phaseArtifacts=@{
        guards=@('guards-collection.json','guards.xml')
        ux=@('ux-collection.json','ux.xml')
        recall=@('recall-results.json','recall/checkpoint.json','recall/cases.jsonl','recall/resources.jsonl')
    }
    $regressionPhase=$spec['argv'][$positions[0]+1]
    if($regressionPhase -cnotin @('guards','ux','recall')){throw 'Unknown regression phase.'}
    $controllerArtifacts[$controllerName]+=$phaseArtifacts[$regressionPhase]
}
foreach($relative in @($controllerArtifacts[$controllerName])) {
    if($relative -and $relative -cnotin $required){throw 'Missing controller-specific result artifact declaration.'}
}
foreach ($name in (@('supervise-080.py','attest-full-suite-080.py','validate-stability-080.py','run-supervised-080.ps1','qualification-power-080.ps1',$controllerName) + @($controllerDependencies[$controllerName]))) {
    if (-not $name) { continue }
    $helperPaths[$name] = Assert-PlainPath (Join-Path $PSScriptRoot $name)
}
if ($Controller -cne (Convert-LinuxPath $helperPaths[$Controller.Split('/')[-1]])) { throw 'Use the exact reviewed controller beside the launcher.' }
$helperStartHashes = @{}; foreach ($name in $helperPaths.Keys) { $helperStartHashes[$name] = Get-Sha $helperPaths[$name] }
$nonce = [Guid]::NewGuid().ToString('N')
$scopeUnit = 'devbrain-q-' + $nonce + '.scope'
$errors = [Collections.Generic.List[string]]::new()
$newDumps = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$newKernel = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$kernelBaseline = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$supervisor = $null; $probes = @(); $probeGeneration = 0; $initialBoot = ''; $finalBoot = ''
$crashBaseline = @{}; $supervisorCode = $null; $proxyCode = $null; $actualExit = $false; $forcedProxy = $false
$sequence = 0; $nextBeat = 0.0; $nextProbe = 0.0; $phase = 'baseline'; $exitAt = $null; $lastProbeCompleted = -1.0
$clock = [Diagnostics.Stopwatch]::StartNew(); $deadline = $TimeoutSeconds + 2 * $CleanupGraceSeconds + 90
$powerStart = $null; $powerEnd = $null; $powerChanged = $false; $powerSamples = 0; $nextPower = 30.0; $sleepState = $null
$scopeState = ''; $scopePopulation = ''; $scopeGroup = ''; $scopeIdentity = ''; $scopeSeen = $false; $scopeAbsentBefore = $false
$scopeEmptyAt = $null; $lastScopeCompleted = -1.0; $scopeStop = $null; $scopeForced = $false
$bootstrapAbsentAt = $null; $bootstrapObservationComplete = $false
$scopeRuntimeSeconds = $TimeoutSeconds + $CleanupGraceSeconds + 60
$lastPublishedHeartbeat = $null
. $powerHelperPath
$mappingProofPath = Join-Path $root 'mapping-proof.json'
$null = Write-AtomicJson $mappingProofPath @{ windows_root=$root; linux_root=$LinuxArtifactRoot; run_id=$RunId; nonce=$nonce; scope_unit=$scopeUnit }
$mappingProofSha = Get-Sha $mappingProofPath
$scopeProbeScript = @'
set -eu
state=$(systemctl --user show "$1" --property=LoadState --value 2>/dev/null || true)
group=$(systemctl --user show "$1" --property=ControlGroup --value 2>/dev/null || true)
population=unknown
identity=
if [ "$state" = not-found ]; then
  [ -z "$group" ] || exit 42
  if [ -n "$5" ] && [ -e "/sys/fs/cgroup$5" ]; then group="$5"; else population=0; fi
fi
if [ -n "$group" ]; then
  case "$group" in /user.slice/*/"$1") ;; *) exit 42;; esac
  if [ -r "/sys/fs/cgroup$group/cgroup.events" ]; then
    identity=$(stat -c '%d:%i' "/sys/fs/cgroup$group")
    population=$(awk '$1=="populated" {print $2}' "/sys/fs/cgroup$group/cgroup.events")
  elif [ ! -e "/sys/fs/cgroup$group" ] && [ "$(systemctl --user show "$1" --property=LoadState --value 2>/dev/null || true)" = not-found ]; then
    state=not-found; group=; population=0
  else exit 43; fi
fi
printf 'LOAD=%s\nGROUP=%s\nPOPULATED=%s\nIDENTITY=%s\n' "$state" "$group" "$population" "$identity"
sha256sum "$2" "$3" "$4"
'@
$scopeStopScript = @'
set -eu
state=$(systemctl --user show "$1" --property=LoadState --value 2>/dev/null || true)
if [ "$state" = not-found ]; then exit 0; fi
group=$(systemctl --user show "$1" --property=ControlGroup --value 2>/dev/null || true)
case "$group" in /user.slice/*/"$1") ;; *) exit 42;; esac
[ "$group" = "$2" ] || exit 43
[ -r "/sys/fs/cgroup$group/cgroup.events" ] || exit 44
[ "$(stat -c '%d:%i' "/sys/fs/cgroup$group")" = "$3" ] || exit 45
exec systemctl --user stop --no-block "$1"
'@
try {
    $sleepState = Start-QualificationSleepInhibition
    Observe-Power
    if (-not (Test-QualificationPowerStateEqual $gate['power_profile'] $powerStart)) { throw 'Power configuration differs from the qualified runtime gate.' }
    $crashBaseline = Get-CrashMetadata
    while ($true) {
        $elapsed = $clock.Elapsed.TotalSeconds
        if ($elapsed -ge $nextBeat) {
            $sequence++
            $publication = Write-AtomicJson (Join-Path $root 'heartbeat.json') @{run_id=$RunId; nonce=$nonce; sequence=$sequence; unix_time=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0}
            $lastPublishedHeartbeat = @{sequence=$sequence; published_utc=$publication.published_utc; elapsed_seconds=$clock.Elapsed.TotalSeconds; retries=$publication.retries}
            $nextBeat = $clock.Elapsed.TotalSeconds + 2
        }
        if ($elapsed -ge $nextPower) {
            try { Observe-Power } catch { Fail-Run ('Power observation failed: ' + $_.Exception.Message) }
            $nextPower = $clock.Elapsed.TotalSeconds + 30
        }
        try {
            $current = Get-CrashMetadata
            foreach ($name in $current.Keys) { if (-not $crashBaseline.ContainsKey($name) -or $crashBaseline[$name] -cne $current[$name]) { $null = $newDumps.Add($name) } }
            if ($newDumps.Count) { Fail-Run 'A new WSL crash dump appeared.' }
        } catch { Fail-Run ('Crash metadata observation failed: ' + $_.Exception.Message) }
        if (-not $probes.Count -and $elapsed -ge $nextProbe) {
            $probeGeneration++
            $probes = @()
            $probes += @{kind='boot'; item=(Start-StreamProcess @('/usr/bin/cat','/proc/sys/kernel/random/boot_id') (Join-Path $root 'probe-boot.stdout.log') (Join-Path $root 'probe-boot.stderr.log') ($probeGeneration -eq 1))}
            $probes += @{kind='kernel'; item=(Start-StreamProcess @('/usr/bin/dmesg','--raw') (Join-Path $root 'probe-kernel.stdout.log') (Join-Path $root 'probe-kernel.stderr.log') ($probeGeneration -eq 1))}
            $probes += @{kind='scope'; item=(Start-StreamProcess @('/bin/sh','-c',$scopeProbeScript,'--',$scopeUnit,(Convert-LinuxPath $supervisorPath),$Controller,(Convert-LinuxPath $mappingProofPath),$scopeGroup) (Join-Path $root 'probe-scope.stdout.log') (Join-Path $root 'probe-scope.stderr.log') ($probeGeneration -eq 1))}
        }
        foreach ($probe in $probes) {
            $item = $probe.item
            if (-not $item.closed -and ($item.started.Elapsed.TotalSeconds -ge 7 -or $item.stdout.Length -gt 8MB -or $item.stderr.Length -gt 1MB)) {
                if (-not $item.process.HasExited) { $item.process.Kill(); $null = $item.process.WaitForExit(1000) }
                Fail-Run ('Bounded ' + $probe.kind + ' probe timed out or exceeded its output bound.')
            }
        }
        if ($probes.Count -and @($probes | Where-Object { -not $_.item.process.HasExited }).Count -eq 0) {
            foreach ($probe in $probes) {
                $item = $probe.item
                try {
                    Close-StreamProcess $item
                    if ($item.process.ExitCode -ne 0) { throw 'Probe returned nonzero.' }
                    $value = [IO.File]::ReadAllText($item.path)
                    if ($value.Length -gt 8MB) { throw 'Probe result exceeded its bound.' }
                    if ($probe.kind -eq 'boot') {
                        $finalBoot = $value.Trim()
                        if ($finalBoot -cnotmatch '^[a-f0-9-]{36}$') { throw 'Invalid boot identity.' }
                        if (-not $initialBoot) { $initialBoot = $finalBoot } elseif ($finalBoot -cne $initialBoot) { Fail-Run 'Linux boot identity changed.' }
                    } elseif ($probe.kind -eq 'scope') {
                        $scopeState = [regex]::Match($value,'(?m)^LOAD=([^\r\n]*)').Groups[1].Value
                        $group = [regex]::Match($value,'(?m)^GROUP=([^\r\n]*)').Groups[1].Value
                        $scopePopulation = [regex]::Match($value,'(?m)^POPULATED=([^\r\n]*)').Groups[1].Value
                        $cgIdentity = [regex]::Match($value,'(?m)^IDENTITY=([^\r\n]*)').Groups[1].Value
                        if ($scopeState -notin @('loaded','not-found') -or $scopePopulation -notin @('0','1')) { throw 'Owned scope state is unavailable.' }
                        if ($phase -eq 'baseline') {
                            if ($scopeState -cne 'not-found' -or $scopePopulation -cne '0') { throw 'Refusing to adopt an existing scope.' }
                            $scopeAbsentBefore = $true
                        } elseif ($group -or $cgIdentity) {
                            if ($group -cnotmatch ('^/user\.slice/.+/' + [regex]::Escape($scopeUnit) + '$')) { throw 'Scope control group differs from its UUID identity.' }
                            if ($cgIdentity) {
                                if ($cgIdentity -cnotmatch '^[0-9]+:[0-9]+$' -or ($scopeSeen -and ($scopeGroup -cne $group -or $scopeIdentity -cne $cgIdentity))) { throw 'Scope cgroup identity changed.' }
                                $scopeGroup = $group; $scopeIdentity = $cgIdentity; $scopeSeen = $true
                            }
                        }
                        foreach ($binding in @(@{path=(Convert-LinuxPath $supervisorPath); sha=$startHashes.supervisor},@{path=$Controller; sha=$helperStartHashes[$Controller.Split('/')[-1]]},@{path=(Convert-LinuxPath $mappingProofPath); sha=$mappingProofSha})) {
                            $hash = [regex]::Match($value,'(?m)^([a-f0-9]{64})  '+[regex]::Escape($binding.path)+'\r?$')
                            if (-not $hash.Success -or $hash.Groups[1].Value -cne $binding.sha) { throw 'Windows/Linux helper or scope mapping proof differs.' }
                        }
                        $lastScopeCompleted = $clock.Elapsed.TotalSeconds
                    } else {
                        foreach ($line in $value.Split("`n")) {
                            if ($line -match $fatalPattern) {
                                if ($phase -eq 'baseline') { $null = $kernelBaseline.Add($line) } elseif (-not $kernelBaseline.Contains($line)) { $null = $newKernel.Add($line) }
                            }
                        }
                        if ($newKernel.Count) { Fail-Run 'A new kernel fatal appeared.' }
                    }
                } catch { Fail-Run ('Missing ' + $probe.kind + ' observation: ' + $_.Exception.Message) }
                $item.process.Dispose()
            }
            $probes = @(); $lastProbeCompleted = $clock.Elapsed.TotalSeconds; $nextProbe = $lastProbeCompleted + 3
            if ($phase -eq 'baseline') {
                if ($errors.Count -or -not $initialBoot) { break }
                if (-not $scopeAbsentBefore) { Fail-Run 'Fresh scope ownership was not established.'; break }
                $linuxArgs = @('/usr/bin/systemd-run','--user','--scope','--quiet',('--unit='+$scopeUnit),'--property=MemoryMax=8589934592','--property=MemorySwapMax=0',('--property=RuntimeMaxSec='+$scopeRuntimeSeconds+'s'),'--property=TimeoutStopSec=5s','--',$PythonExecutable,'-I','-B',(Convert-LinuxPath $supervisorPath),'--execute','--artifact-root',$LinuxArtifactRoot,'--controller',$Controller,'--arguments-file',(Convert-LinuxPath $argumentsPath),'--run-id',$RunId,'--windows-nonce',$nonce,'--scope-unit',$scopeUnit,'--crash-dir',(Convert-LinuxPath $crashRoot),'--timeout-seconds',[string]$TimeoutSeconds,'--cleanup-grace',[string]$CleanupGraceSeconds)
                $supervisor = Start-StreamProcess $linuxArgs (Join-Path $root 'supervisor.stdout.log') (Join-Path $root 'supervisor.stderr.log') $true
                $phase = 'running'
            }
        }
        if ($phase -eq 'running' -and $supervisor.process.HasExited) {
            $proxyCode = $supervisor.process.ExitCode; $supervisorCode = $proxyCode; $actualExit = $true
            $exitAt = $clock.Elapsed.TotalSeconds; $phase = 'grace'
            # Preserve small stderr output immediately, while the independent
            # post-exit observation continues. Retain the Process handle.
            try { Close-StreamProcess $supervisor } catch { Fail-Run ('Supervisor exit log finalization failed: ' + $_.Exception.Message) }
            $null = Write-AtomicJson (Join-Path $root 'supervisor-exit.json') @{
                schema_version=1; run_id=$RunId; scope_unit=$scopeUnit
                windows_proxy_identity=$supervisor.windows_identity
                windows_proxy_returncode=$proxyCode; actual_exit_verified=$actualExit
                elapsed_seconds=$exitAt; observed_utc=[DateTime]::UtcNow.ToString('o')
                stdio_complete=$supervisor.closed; qualification_passed=$false
            }
            if ($supervisorCode -ne 0) { Fail-Run 'Linux supervisor returned nonzero or crashed.' }
        }
        if ($elapsed -ge $deadline -and $phase -eq 'running') {
            Fail-Run 'Supervisor exceeded the outer deadline; Linux cleanup is not verified.'
            $forcedProxy = $true; $supervisor.process.Kill(); $null = $supervisor.process.WaitForExit(1000)
            $exitAt = $clock.Elapsed.TotalSeconds; $phase = 'grace'
        }
        if ($null -ne $exitAt -and $lastScopeCompleted -ge $exitAt -and $scopePopulation -eq '0' -and $scopeSeen) {
            if ($null -eq $scopeEmptyAt) { $scopeEmptyAt = $lastScopeCompleted }
        } elseif ($null -ne $exitAt -and $clock.Elapsed.TotalSeconds-$exitAt -gt 5 -and $scopePopulation -ne '0') { Fail-Run 'Owned descendants survived supervisor exit.' }
        if ($null -ne $scopeEmptyAt -and $scopePopulation -ne '0') { Fail-Run 'The owned scope became populated after cleanup.' }
        if ($actualExit -and -not $scopeSeen -and $scopeAbsentBefore -and $lastScopeCompleted -ge $exitAt -and
            $scopeState -ceq 'not-found' -and $scopePopulation -ceq '0' -and -not $scopeGroup -and -not $scopeIdentity) {
            if ($null -eq $bootstrapAbsentAt) { $bootstrapAbsentAt = $lastScopeCompleted }
        } else { $bootstrapAbsentAt = $null }
        if ($errors.Count -and $supervisor -and $scopeSeen -and -not $scopeStop) {
            $scopeForced = $true
            $scopeStop = Start-StreamProcess @('/bin/sh','-c',$scopeStopScript,'--',$scopeUnit,$scopeGroup,$scopeIdentity) (Join-Path $root 'scope-stop.stdout.log') (Join-Path $root 'scope-stop.stderr.log') $true
        }
        if ($scopeStop -and -not $scopeStop.closed) {
            if (-not $scopeStop.process.HasExited -and $scopeStop.started.Elapsed.TotalSeconds -ge 5) { $scopeStop.process.Kill(); $null = $scopeStop.process.WaitForExit(1000); Fail-Run 'Bounded owned-scope stop did not finish.' }
            if ($scopeStop.process.HasExited) { Close-StreamProcess $scopeStop; if ($scopeStop.process.ExitCode -ne 0) { Fail-Run 'Owned-scope stop was refused or failed.' } }
        }
        if ($phase -eq 'grace' -and $null -ne $scopeEmptyAt -and -not $probes.Count -and $lastProbeCompleted -ge $scopeEmptyAt+$CleanupGraceSeconds -and $lastScopeCompleted -ge $scopeEmptyAt+$CleanupGraceSeconds) { break }
        if ($phase -eq 'grace' -and -not $probes.Count -and (Test-FailedBootstrapGrace @{
            actual_exit=$actualExit; returncode=$supervisorCode; scope_seen=$scopeSeen; initially_absent=$scopeAbsentBefore
            load_state=$scopeState; population=$scopePopulation; group=$scopeGroup; identity=$scopeIdentity
            absent_since=$bootstrapAbsentAt; last_scope=$lastScopeCompleted; last_probe=$lastProbeCompleted
        } $CleanupGraceSeconds)) { $bootstrapObservationComplete = $true; break }
        if ($clock.Elapsed.TotalSeconds -ge $deadline + $CleanupGraceSeconds + 20) { Fail-Run 'Final observation could not finish within its bound.'; break }
        Start-Sleep -Milliseconds 200
    }
} catch { Fail-Run ('External observer failure: ' + $_.Exception.Message) }
finally {
    try {
    if ($supervisor -and $scopeSeen -and ($errors.Count -or $null -eq $scopeEmptyAt) -and -not $scopeStop) {
        $scopeForced = $true
        try {
            $scopeStop = Start-StreamProcess @('/bin/sh','-c',$scopeStopScript,'--',$scopeUnit,$scopeGroup,$scopeIdentity) (Join-Path $root 'scope-stop.stdout.log') (Join-Path $root 'scope-stop.stderr.log') $true
            if (-not $scopeStop.process.WaitForExit(5000)) { $scopeStop.process.Kill(); $null = $scopeStop.process.WaitForExit(1000) }
        } catch { Fail-Run 'Emergency owned-scope cleanup could not be verified.' }
    }
    if ($scopeStop) {
        if (-not $scopeStop.process.HasExited) { $scopeStop.process.Kill(); $null = $scopeStop.process.WaitForExit(1000) }
        try { Close-StreamProcess $scopeStop } catch { Fail-Run 'Scope-stop log finalization failed.' }
        $scopeStop.process.Dispose()
    }
    foreach ($probe in $probes) {
        if (-not $probe.item.process.HasExited) { $probe.item.process.Kill(); $null = $probe.item.process.WaitForExit(1000); Fail-Run 'An observer probe required forced cleanup.' }
        try { Close-StreamProcess $probe.item } catch { Fail-Run 'Observer probe log finalization failed.' }
        $probe.item.process.Dispose()
    }
    if ($supervisor) {
        if (-not $supervisor.process.HasExited) {
            Fail-Run 'Windows observer exited before the Linux supervisor; explicit tagged cleanup is required.'
            $forcedProxy = $true
            # Stop heartbeat; Linux's independent 15-second lease must expire.
            # Killing the WSL proxy is not evidence of the Linux process exiting.
            $supervisor.process.Kill(); $null = $supervisor.process.WaitForExit(1000)
        }
        try { Close-StreamProcess $supervisor } catch { Fail-Run 'Supervisor log finalization failed.' }
        if ($supervisor.process.HasExited) { $proxyCode = $supervisor.process.ExitCode }
        $supervisor.process.Dispose()
    }
    } finally {
        try { Observe-Power } catch { Fail-Run ('Final power observation failed: ' + $_.Exception.Message) }
        try { Stop-QualificationSleepInhibition -State $sleepState } catch { Fail-Run ('Sleep inhibitor restoration failed: ' + $_.Exception.Message) }
    }
}
$receipt = @{
    schema_version=1; scope='devbrain_external_process_watch'; run_id=$RunId; passed=$false
    child_returncode=$null; supervisor_returncode=$supervisorCode; windows_proxy_returncode=$proxyCode
    supervisor_exit_verified=$actualExit; controller_exit_verified=$false; cleanup_complete=$false
    forced_cleanup=($forcedProxy -or $scopeForced); native_failure=($newDumps.Count -gt 0 -or $newKernel.Count -gt 0 -or ($initialBoot -and $initialBoot -cne $finalBoot))
    cleanup_grace_seconds=$(if ($null -ne $exitAt) { [Math]::Max(0,$clock.Elapsed.TotalSeconds-$exitAt) } else { 0 })
    initial_boot_id=$initialBoot; final_boot_id=$finalBoot; new_wsl_dumps=@($newDumps | Sort-Object); new_kernel_fatals=@($newKernel | Sort-Object)
    artifact_sha256=@{}; runtime=@{}; input_sha256_start=$startHashes; input_sha256_end=@{}
    helper_sha256_start=$helperStartHashes; helper_sha256_end=@{}
    power_profile_start=$powerStart; power_profile_end=$powerEnd
    power_monitor=@{interval_seconds=30; maximum_gap_seconds=45; sample_count=$powerSamples; changed=$powerChanged; samples_artifact='power-samples.jsonl'; supervisor_exit_elapsed_seconds=$exitAt; scope_empty_elapsed_seconds=$scopeEmptyAt}
    sleep_inhibition=$sleepState
    last_published_heartbeat=$lastPublishedHeartbeat
    scope_unit=$scopeUnit; control_group=$scopeGroup; scope_cgroup_identity=$scopeIdentity
    scope_initially_absent=$scopeAbsentBefore; scope_membership_verified=$scopeSeen
    scope_empty_verified=($null -ne $scopeEmptyAt -and $scopePopulation -eq '0')
    failed_bootstrap_observation_complete=$bootstrapObservationComplete
    failed_bootstrap_grace_seconds=$(if ($null -ne $bootstrapAbsentAt) { $lastScopeCompleted-$bootstrapAbsentAt } else { 0 })
    scope_cleanup_grace_seconds=$(if ($null -ne $scopeEmptyAt) { $lastScopeCompleted-$scopeEmptyAt } else { 0 })
    scope_runtime_max_seconds_requested=$scopeRuntimeSeconds; scope_memory_max_bytes_requested=8589934592; scope_memory_swap_max_bytes_requested=0
    python_launcher=$null; python_launcher_base_sha256=$null
    controller=$Controller; controller_sha256_start=$null; controller_sha256_end=$null
    gate_sha256=$startHashes.gate; explicit_tagged_cleanup_may_be_required=$true
}
try {
    $endHashes = @{ launcher=(Get-Sha $launcherPath); supervisor=(Get-Sha $supervisorPath); arguments=(Get-Sha $argumentsPath); gate=(Get-Sha $gatePath) }
    $receipt.input_sha256_end = $endHashes
    foreach ($key in $startHashes.Keys) { if ($startHashes[$key] -cne $endHashes[$key]) { throw "Helper or input changed during qualification: $key" } }
    foreach ($name in $helperPaths.Keys) { $receipt.helper_sha256_end[$name] = Get-Sha $helperPaths[$name]; if ($receipt.helper_sha256_end[$name] -cne $helperStartHashes[$name]) { throw 'A qualification helper changed.' } }
    foreach ($relative in $required) { $receipt.artifact_sha256[$relative] = Get-Sha (Get-ArtifactPath $relative) }
    $receipt.artifact_sha256['power-samples.jsonl'] = Get-Sha (Get-ArtifactPath 'power-samples.jsonl')
    $report = Read-Json (Get-ArtifactPath 'supervisor-report.json')
    if ($report['schema_version'] -ne 1 -or $report['scope'] -cne 'devbrain_linux_process_supervision' -or $report['run_id'] -cne $RunId) { throw 'Supervisor report identity mismatch.' }
    foreach ($key in @('passed','controller_exit_verified','cleanup_complete')) { if ($report[$key] -isnot [bool] -or -not $report[$key]) { throw "Supervisor did not verify $key." } }
    foreach ($key in @('forced_cleanup','native_failure')) { if ($report[$key] -isnot [bool] -or $report[$key]) { throw "Supervisor reported $key or omitted its verdict." } }
    foreach ($key in @('errors','new_wsl_dumps','new_kernel_fatals','remaining_tagged_processes','remaining_scope_processes')) { if ($report[$key] -isnot [array] -or $report[$key].Count) { throw "Supervisor reported unresolved $key." } }
    if ($report['scope_unit'] -cne $scopeUnit -or $report['control_group'] -cne $scopeGroup -or $report['scope_cgroup_identity'] -cne $scopeIdentity -or $report['scope_membership_verified'] -ne $true -or $report['scope_descendants_stopped'] -ne $true) { throw 'Supervisor ownership differs from the externally observed UUID scope.' }
    if ($report['child_returncode'] -ne 0 -or $report['cleanup_grace_seconds'] -lt 30 -or $report['initial_boot_id'] -cne $initialBoot -or $report['final_boot_id'] -cne $finalBoot -or $report['controller'] -cne $Controller) { throw 'Supervisor process or observation evidence mismatch.' }
    if ($report['gate_sha256'] -cne $startHashes.gate -or $report['arguments_sha256'] -cne $startHashes.arguments -or $report['supervisor_sha256'] -cne $startHashes.supervisor -or $report['controller_report_sha256'] -cne $receipt.artifact_sha256['controller-report.json']) { throw 'Supervisor evidence digests do not match.' }
    foreach ($key in $runtimeKeys) { if ($report['runtime'][$key] -cne $gate['runtime'][$key]) { throw 'Supervisor runtime differs from the gate.' }; $receipt.runtime[$key] = $report['runtime'][$key] }
    foreach ($name in $helperPaths.Keys) {
        if ($name -cne 'run-supervised-080.ps1' -and ($report['helper_sha256_start'][$name] -cne $helperStartHashes[$name] -or $report['helper_sha256_end'][$name] -cne $helperStartHashes[$name])) { throw 'Supervisor helper bindings differ from the external observer.' }
    }
    $expectedLauncher = if ($spec.Contains('python_launcher')) { $spec['python_launcher'] } else { $PythonExecutable }
    if ($report['python_launcher'] -cne $expectedLauncher -or $report['python_launcher_base_sha256'] -cne $gate['runtime']['python_binary_sha256']) { throw 'Controller launcher binding differs from the qualified Python.' }
    $receipt.python_launcher = $report['python_launcher']; $receipt.python_launcher_base_sha256 = $report['python_launcher_base_sha256']
    $receipt.controller_sha256_start = $report['controller_sha256']
    $receipt.controller_sha256_end = $report['controller_sha256_end']
    if ($receipt.controller_sha256_start -cnotmatch '^[0-9a-f]{64}$' -or $receipt.controller_sha256_start -cne $receipt.controller_sha256_end) { throw 'Controller changed or was not hashed after cleanup.' }
    $receipt.child_returncode = $report['child_returncode']; $receipt.controller_exit_verified = $report['controller_exit_verified']; $receipt.cleanup_complete = $report['cleanup_complete']
    $receipt.explicit_tagged_cleanup_may_be_required = $false
} catch { $errors.Add('Final evidence verification: ' + $_.Exception.Message) }
if (-not $actualExit -or $forcedProxy -or $supervisorCode -ne 0) { $errors.Add('Actual successful supervisor exit was not verified.') }
if (-not $initialBoot -or $initialBoot -cne $finalBoot -or $receipt.cleanup_grace_seconds -lt 30 -or $null -eq $exitAt -or $lastProbeCompleted -lt $exitAt + 30) { $errors.Add('The independent final observation was incomplete.') }
if (-not $scopeAbsentBefore -or -not $scopeSeen -or -not $receipt.scope_empty_verified -or $receipt.scope_cleanup_grace_seconds -lt 30 -or $scopeForced) { $errors.Add('Owned scope emptiness and post-cleanup grace were not verified.') }
if ($null -eq $sleepState -or -not $sleepState.acquired -or -not $sleepState.restored -or $powerChanged -or $powerSamples -lt 2 -or -not (Test-QualificationPowerStateEqual $powerStart $powerEnd)) { $errors.Add('Stable Balanced power and restored sleep inhibition were not verified.') }
try {
    $previousSampleTime = $null; $sampleCount = 0
    foreach ($line in [IO.File]::ReadLines((Join-Path $root 'power-samples.jsonl'))) {
        $sample = $line | ConvertFrom-Json -AsHashtable
        $stamp = $sample.elapsed_seconds
        if ($stamp -isnot [ValueType] -or $stamp -is [bool] -or -not [double]::IsFinite([double]$stamp) -or $stamp -lt 0 -or
            ($null -ne $previousSampleTime -and ($stamp -lt $previousSampleTime -or $stamp-$previousSampleTime -gt 45)) -or
            -not (Test-QualificationPowerStateEqual $powerStart $sample.state)) { throw 'Power sample changed or exceeded the maximum gap.' }
        $previousSampleTime = $stamp; $sampleCount++
    }
    if ($sampleCount -ne $powerSamples -or $sampleCount -lt 2 -or $null -eq $exitAt -or $null -eq $scopeEmptyAt -or $previousSampleTime -lt $scopeEmptyAt+30) { throw 'Power samples do not cover the complete post-cleanup grace.' }
} catch { $errors.Add('Power observation verification: ' + $_.Exception.Message) }
$receipt.errors = @($errors)
$receipt.passed = $errors.Count -eq 0 -and $receipt.cleanup_complete -and $receipt.controller_exit_verified -and -not $receipt.native_failure -and -not $receipt.forced_cleanup
$null = Write-AtomicJson (Join-Path $root 'receipt.json') $receipt
$receipt | ConvertTo-Json -Depth 8
if (-not $receipt.passed) { exit 1 }
