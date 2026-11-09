#Requires -Version 7.2
<# Independent Windows observer for the first, not-yet-qualified runtime probes.
No WSL process starts without -Execute. This script never changes a power plan.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$BundleRoot,
    [Parameter(Mandatory)][string]$ArtifactRoot,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9_-]{1,64}$')][string]$RunId,
    [Parameter(Mandatory)][ValidateSet('prepare','perl','ssl','imports','unit','tmux','jvm1','jvm2','jvm3','attest')][string]$Phase,
    [ValidateRange(30,1800)][int]$TimeoutSeconds = 600,
    [switch]$Execute
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$phases = @('prepare','perl','ssl','imports','unit','tmux','jvm1','jvm2','jvm3','attest')
# Codex MSIX redirects the apparent LOCALAPPDATA/DevBrain path without a reparse
# point. The parent verified this physical path using GetFinalPathNameByHandle.
$allowedRoot = 'C:\Users\piotr\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\DevBrain\qualification'
$python = '/home/piotr/.cache/devbrain-runtime-080/runtimes/python/bin/python3.12'
$linuxRoot = '/home/piotr/.cache/devbrain-runtime-080/postbios-' + $RunId
$fatalPattern = 'segfault|segmentation fault|fatal signal|SIGSEGV|SIGABRT|invalid opcode|general protection fault|oom-kill|Out of memory|Kernel panic'
$runtimeHashes = @{
    '/home/piotr/.cache/devbrain-runtime-080/runtimes/python/bin/python3.12'='23bc148d2b48964b621c5561ff2b81c1d9c7b717967a7491efbf12f4304882b5'
    '/home/piotr/.cache/devbrain-runtime-080/runtimes/jdk-21.0.13+9/bin/java'='9cad3f2620d3ef2ed10b26f922af7e773d54fdaa05f615d6d6df014f3fec4155'
    '/home/piotr/.cache/devbrain-runtime-080/runtimes/tmux-3.8/bin/tmux'='b4338a07981a2d6e008578059050cf91e43e086929630358937a4df365766669'
}

function Plain([string]$Path) {
    if (-not [IO.Path]::IsPathFullyQualified($Path)) { throw 'An absolute path is required.' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not $full.StartsWith($allowedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Path must be under the verified physical Codex LOCALAPPDATA qualification root.' }
    $cursor = $full
    while ($cursor) {
        if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse paths are not accepted.' }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
    return $full
}
function Linux([string]$Path) {
    if (-not $Path.StartsWith('C:\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Only C: mapping is supported.' }
    return '/mnt/c/' + $Path.Substring(3).Replace('\','/')
}
function Sha([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function ReadJson([string]$Path) {
    $null = Plain $Path
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer -or $item.Length -gt 8MB) { throw 'Missing or oversized JSON.' }
    # Readers permit replacement of the old, fully published file.
    $jsonStream = [IO.FileStream]::new($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    $jsonReader = $null
    try {
        if ($jsonStream.Length -gt 8MB) { throw 'Oversized JSON.' }
        $jsonReader = [IO.StreamReader]::new($jsonStream,[Text.Encoding]::UTF8,$true,4096,$true)
        return $jsonReader.ReadToEnd() | ConvertFrom-Json -AsHashtable
    } finally {
        if ($null -ne $jsonReader) { $jsonReader.Dispose() }
        $jsonStream.Dispose()
    }
}
function Atomic([string]$Path, $Value) {
    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $atomicFailure = $null
    $publishClock = $null
    try {
        $stream = [IO.FileStream]::new($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try { $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 40) + "`n"); $stream.Write($bytes); $stream.Flush($true) } finally { $stream.Dispose() }
        # Retry only publication of these exact, already flushed bytes. A failed
        # heartbeat never acquires a newer timestamp, sequence or lease deadline.
        $publishClock = [Diagnostics.Stopwatch]::StartNew()
        $publishAttempt = 0
        while ($true) {
            $publishAttempt++
            try { [IO.File]::Move($temporary,$Path,$true); break }
            catch {
                $cause = $_.Exception
                while ($null -ne $cause.InnerException) { $cause = $cause.InnerException }
                $nativeCode = $cause.HResult -band 0xFFFF
                $remaining = 1000 - $publishClock.Elapsed.TotalMilliseconds
                if (($cause -isnot [IO.IOException] -and $cause -isnot [UnauthorizedAccessException]) -or $nativeCode -notin @(5,32,33) -or $remaining -le 0) { throw }
                [Console]::Error.WriteLine((@{event='atomic_replace_retry'; artifact=[IO.Path]::GetFileName($Path); win32_code=$nativeCode; elapsed_ms=$publishClock.Elapsed.TotalMilliseconds; attempt=$publishAttempt} | ConvertTo-Json -Compress))
                Start-Sleep -Milliseconds ([int][Math]::Min(50,[Math]::Ceiling($remaining)))
                if ($publishClock.Elapsed.TotalMilliseconds -ge 1000) { throw }
            }
        }
    } catch {
        $atomicFailure = $_.Exception
        $atomicFailure.Data['atomic_path'] = $Path
        $atomicFailure.Data['atomic_temporary_path'] = $temporary
        $atomicFailure.Data['publish_elapsed_ms'] = if ($null -ne $publishClock) { $publishClock.Elapsed.TotalMilliseconds } else { 0 }
        throw
    } finally {
        # Only this invocation's UUID temporary is eligible for cleanup.
        try { [IO.File]::Delete($temporary) }
        catch {
            if ($null -ne $atomicFailure) { $atomicFailure.Data['temporary_cleanup_error'] = $_.Exception.Message }
            else { throw }
        }
    }
}
function BundleManifest {
    $files = @{}
    foreach ($item in Get-ChildItem -LiteralPath $bundle -Recurse -Force) {
        $null = Plain $item.FullName
        if (-not $item.PSIsContainer) { $files[$item.FullName.Substring($bundle.Length + 1).Replace('\','/')] = Sha $item.FullName }
    }
    return $files
}
function SameManifest($Left,$Right) {
    if ($Left.Count -ne $Right.Count) { return $false }
    foreach ($key in $Left.Keys) { if (-not $Right.Contains($key) -or $Left[$key] -cne $Right[$key]) { return $false } }
    return $true
}
function Crashes {
    $directory = Join-Path $env:LOCALAPPDATA 'Temp/wsl-crashes'
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) { throw 'Crash metadata directory unavailable.' }
    $result = @{}
    foreach ($file in Get-ChildItem -LiteralPath $directory -File -Force) {
        if ($file.Name.StartsWith('wsl-crash-')) { $result[$file.Name] = "$($file.Length):$($file.LastWriteTimeUtc.Ticks)" }
    }
    return $result
}
function StartWsl([string[]]$Arguments,[string]$Name) {
    $psi = [Diagnostics.ProcessStartInfo]::new((Join-Path $env:WINDIR 'System32/wsl.exe'))
    $psi.UseShellExecute=$false; $psi.CreateNoWindow=$true; $psi.WindowStyle='Hidden'
    $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$true; $psi.RedirectStandardInput=$true
    foreach ($part in @('-d','Ubuntu','-u','piotr','--exec') + $Arguments) { $psi.ArgumentList.Add($part) }
    $out = [IO.FileStream]::new((Join-Path $directory ($Name+'.out.log')),[IO.FileMode]::Create,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    $err = [IO.FileStream]::new((Join-Path $directory ($Name+'.err.log')),[IO.FileMode]::Create,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    $process = [Diagnostics.Process]::Start($psi); $process.StandardInput.Close()
    return @{ process=$process; stdout=$out; stderr=$err; output=$process.StandardOutput.BaseStream.CopyToAsync($out); error=$process.StandardError.BaseStream.CopyToAsync($err); name=$Name; clock=[Diagnostics.Stopwatch]::StartNew(); closed=$false }
}
function CloseProcess($Item) {
    if ($Item.closed) { return }
    if (-not $Item.process.HasExited -or -not $Item.output.Wait(1000) -or -not $Item.error.Wait(1000)) { throw 'Actual process/log exit is unverified.' }
    $Item.stdout.Flush(); $Item.stderr.Flush(); $Item.stdout.Dispose(); $Item.stderr.Dispose(); $Item.closed=$true
}
function Fail([string]$Message) {
    if (-not $errors.Contains($Message)) { $errors.Add($Message) }
    if (-not (Test-Path -LiteralPath (Join-Path $directory 'cancel.json'))) { Atomic (Join-Path $directory 'cancel.json') @{run_id=$RunId; nonce=$nonce; reason=$Message} }
}

$bundle = Plain $BundleRoot
$root = Plain $ArtifactRoot
if ($root -eq $bundle -or $root.StartsWith($bundle+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Artifacts must be outside the immutable bundle.' }
$controller = Join-Path $bundle 'release-tools/postbios-diagnostic-080.py'
$powerHelper = Join-Path $bundle 'release-tools/qualification-power-080.ps1'
if (-not $Execute) {
    @{execute=$false; phase=$Phase; bundle=$bundle; artifacts=$root; linux_root=$linuxRoot; timeout_seconds=$TimeoutSeconds; post_exit_watch_seconds=30; changes_power=$false} | ConvertTo-Json
    return
}
if ([IO.Path]::GetFullPath($PSCommandPath) -cne (Join-Path $bundle 'release-tools/run-postbios-diagnostic-080.ps1')) { throw 'Run the frozen launcher from its bundle.' }
. $powerHelper
$power = Get-QualificationPowerState
$manifest = BundleManifest
$phaseIndex = [Array]::IndexOf($phases,$Phase)
$priorBoot = $null
if ($Phase -eq 'prepare') {
    if ((Test-Path -LiteralPath $root) -and @(Get-ChildItem -LiteralPath $root -Force).Count) { throw 'Preparation requires an empty artifact root.' }
    $null = [IO.Directory]::CreateDirectory($root)
    Atomic (Join-Path $root 'bundle-manifest.json') @{files=$manifest; run_id=$RunId}
    Atomic (Join-Path $root 'power-baseline.json') $power
    Atomic (Join-Path $root 'global-crash-baseline.json') (Crashes)
} else {
    $saved = ReadJson (Join-Path $root 'bundle-manifest.json')
    if ($saved.run_id -cne $RunId -or -not (SameManifest $saved.files $manifest)) { throw 'Bundle or run identity changed.' }
    $initialPower = ReadJson (Join-Path $root 'power-baseline.json')
    if (-not (Test-QualificationPowerStateEqual $power $initialPower)) { throw 'Power configuration differs from preparation.' }
    for ($index=0; $index -lt $phaseIndex; $index++) {
        $previous = ReadJson (Join-Path $root ($phases[$index]+'/receipt.json'))
        if ($previous.passed -ne $true -or $previous.run_id -cne $RunId -or $previous.bundle_manifest_sha256 -cne (Sha (Join-Path $root 'bundle-manifest.json')) -or $previous.global_crash_baseline_sha256 -cne (Sha (Join-Path $root 'global-crash-baseline.json')) -or $previous.global_kernel_baseline_sha256 -cne (Sha (Join-Path $root 'global-kernel-baseline.json'))) { throw 'A prior phase is missing, failed or differently bound.' }
        if ($priorBoot -and $priorBoot -cne $previous.final_boot_id) { throw 'Prior boot identities differ.' }
        $priorBoot = $previous.final_boot_id
    }
}
$directory = Join-Path $root $Phase
if (Test-Path -LiteralPath $directory) { throw 'Phase already attempted; no automatic retry.' }
$null = [IO.Directory]::CreateDirectory($directory)
$nonce = [guid]::NewGuid().ToString('N')
$unit = 'devbrain-pb-'+$nonce+'.scope'
$memory = if ($Phase -eq 'perl') { '2147483648' } else { '4294967296' }
$errors = [Collections.Generic.List[string]]::new()
$newDumps = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$newKernel = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$kernelBaseline = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$globalKernelPath = Join-Path $root 'global-kernel-baseline.json'
$globalKernelSha = $null
$kernelBaselineReady = $false
if ($Phase -ne 'prepare') {
    $savedKernel = ReadJson $globalKernelPath
    if ($savedKernel.boot_id -cne $priorBoot) { throw 'Global kernel baseline has a different boot identity.' }
    foreach ($line in $savedKernel.fatal_lines) { $null=$kernelBaseline.Add([string]$line) }
    $globalKernelSha = Sha $globalKernelPath
    $kernelBaselineReady = $true
}
$globalCrashPath = Join-Path $root 'global-crash-baseline.json'
$baselineCrashes = ReadJson $globalCrashPath
$globalCrashSha = Sha $globalCrashPath
if (-not (SameManifest $baselineCrashes (Crashes))) { throw 'Global crash metadata changed since preparation; no phase starts.' }
$clock = [Diagnostics.Stopwatch]::StartNew()
$main=$null; $probe=$null; $cleanup=$null; $kill=$null; $sequence=0; $nextBeat=0.0; $nextProbe=0.0; $nextPower=0.0
$initialBoot=''; $finalBoot=''; $proxyExit=$null; $exitAt=$null; $emptyAt=$null; $lastProbe=-1.0
$scopeState=''; $populated=''; $actualExit=$false; $forced=$false; $started=$false
$receipt = @{schema_version=1; scope='postbios_external_diagnostic_watch'; run_id=$RunId; phase=$Phase; passed=$false; unit=$unit; errors=@(); bundle_manifest_sha256=(Sha (Join-Path $root 'bundle-manifest.json')); global_crash_baseline_sha256=$globalCrashSha; global_kernel_baseline_sha256=$globalKernelSha; power=$power; power_profile_start=$power; started_utc=[DateTime]::UtcNow.ToString('o')}
$samples = [Collections.Generic.List[object]]::new()
$lastPower = $power
Atomic (Join-Path $directory 'receipt.json') $receipt
$mappingProof = Join-Path $directory 'mapping-proof.json'
Atomic $mappingProof @{run_id=$RunId; phase=$Phase; nonce=$nonce; windows_artifact_root=$root; linux_artifact_root=(Linux $root)}
$mappingProofSha = Sha $mappingProof
$controllerSha = Sha $controller
$probeScript = @'
set -eu
printf 'BOOT='
cat /proc/sys/kernel/random/boot_id
state=$(systemctl --user show "$1" --property=LoadState --value 2>/dev/null || true)
printf 'LOAD=%s\n' "$state"
group=$(systemctl --user show "$1" --property=ControlGroup --value 2>/dev/null || true)
printf 'GROUP=%s\n' "$group"
if [ "$state" = not-found ]; then
  printf 'POPULATED=0\n'
elif [ -n "$group" ]; then
  case "$group" in /user.slice/*/"$1") ;; *) exit 42;; esac
  if [ -f "/sys/fs/cgroup$group/cgroup.events" ]; then
    awk '$1=="populated" {print "POPULATED=" $2}' "/sys/fs/cgroup$group/cgroup.events"
  else printf 'POPULATED=0\n'; fi
else printf 'POPULATED=unknown\n'; fi
sha256sum /home/piotr/.cache/devbrain-runtime-080/runtimes/python/bin/python3.12 /home/piotr/.cache/devbrain-runtime-080/runtimes/jdk-21.0.13+9/bin/java /home/piotr/.cache/devbrain-runtime-080/runtimes/tmux-3.8/bin/tmux
sha256sum "$2" "$3"
printf 'KERNEL_BEGIN\n'
dmesg --raw
'@
$sleepToken = $null
try {
    $sleepToken = Start-QualificationSleepInhibition
    while ($true) {
        $elapsed = $clock.Elapsed.TotalSeconds
        if ($elapsed -ge $nextBeat) { $sequence++; Atomic (Join-Path $directory 'heartbeat.json') @{run_id=$RunId; nonce=$nonce; sequence=$sequence; unix_time=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0}; $nextBeat=$elapsed+2 }
        $current = Crashes
        foreach ($name in $current.Keys) { if (-not $baselineCrashes.Contains($name) -or $baselineCrashes[$name] -cne $current[$name]) { $null=$newDumps.Add($name) } }
        foreach ($name in $baselineCrashes.Keys) { if (-not $current.Contains($name)) { Fail 'A baseline native crash artifact disappeared.' } }
        if ($newDumps.Count) { Fail 'New or changed native crash artifact.' }
        if ($elapsed -ge $nextPower) {
            $nowPower = Get-QualificationPowerState
            if (-not (Test-QualificationPowerStateEqual $power $nowPower)) { Fail 'Balanced plan or its AC/DC configuration changed.' }
            $lastPower=$nowPower
            $sample=@{elapsed_seconds=$clock.Elapsed.TotalSeconds; state=$nowPower}
            $samples.Add($sample)
            [IO.File]::AppendAllText((Join-Path $directory 'power-samples.jsonl'),(($sample | ConvertTo-Json -Depth 8 -Compress)+"`n"),[Text.UTF8Encoding]::new($false))
            $nextPower=$elapsed+2
        }
        if (-not $probe -and $elapsed -ge $nextProbe) { $probe=StartWsl @('/bin/sh','-c',$probeScript,'--',$unit,(Linux $controller),(Linux $mappingProof)) 'probe' }
        if ($probe -and ($probe.clock.Elapsed.TotalSeconds -gt 7 -or $probe.stdout.Length -gt 8MB -or $probe.stderr.Length -gt 1MB)) {
            if (-not $probe.process.HasExited) { $probe.process.Kill(); $null=$probe.process.WaitForExit(1000) }
            Fail 'Kernel/scope probe exceeded its time or output bound.'
        }
        if ($probe -and $probe.process.HasExited) {
            CloseProcess $probe
            if ($probe.process.ExitCode -ne 0) { Fail 'Kernel/scope probe failed.' }
            $text=[IO.File]::ReadAllText((Join-Path $directory 'probe.out.log'))
            $bootMatch=[regex]::Match($text,'(?m)^BOOT=([a-f0-9-]{36})\r?$')
            if (-not $bootMatch.Success) { Fail 'Missing boot identity.' } else {
                $finalBoot=$bootMatch.Groups[1].Value
                if (-not $initialBoot) { $initialBoot=$finalBoot; if ($priorBoot -and $initialBoot -cne $priorBoot) { Fail 'Boot changed between diagnostic phases.' } }
                elseif ($finalBoot -cne $initialBoot) { Fail 'WSL boot changed.' }
            }
            $scopeState=[regex]::Match($text,'(?m)^LOAD=([^\r\n]*)').Groups[1].Value
            $populated=[regex]::Match($text,'(?m)^POPULATED=([^\r\n]*)').Groups[1].Value
            if ($scopeState -notin @('loaded','not-found') -or $populated -notin @('0','1')) { Fail 'Scope state could not be verified.' }
            foreach ($path in $runtimeHashes.Keys) {
                $hashMatch=[regex]::Match($text,'(?m)^([a-f0-9]{64})  '+[regex]::Escape($path)+'\r?$')
                if (-not $hashMatch.Success -or $hashMatch.Groups[1].Value -cne $runtimeHashes[$path]) { Fail 'A runtime binary differs from its pre-BIOS pinned SHA.' }
            }
            foreach ($binding in @(@{path=(Linux $controller); sha=$controllerSha},@{path=(Linux $mappingProof); sha=$mappingProofSha})) {
                $mappedHash=[regex]::Match($text,'(?m)^([a-f0-9]{64})  '+[regex]::Escape($binding.path)+'\r?$')
                if (-not $mappedHash.Success -or $mappedHash.Groups[1].Value -cne $binding.sha) { Fail 'Windows/Linux file mapping or controller SHA does not match.' }
            }
            $kernelLines = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
            $kernelText = [regex]::Match($text,'(?s)(?:^|\n)KERNEL_BEGIN\r?\n(.*)$')
            if (-not $kernelText.Success) { Fail 'Kernel log observation is missing.' }
            foreach ($line in $kernelText.Groups[1].Value.Split("`n")) {
                if ($line -match $fatalPattern) { $null=$kernelLines.Add($line.TrimEnd("`r")) }
            }
            if (-not $kernelBaselineReady -and $Phase -eq 'prepare' -and $errors.Count -eq 0) {
                foreach ($line in $kernelLines) { $null=$kernelBaseline.Add($line) }
                Atomic $globalKernelPath @{boot_id=$initialBoot; fatal_lines=@($kernelBaseline | Sort-Object)}
                $globalKernelSha=Sha $globalKernelPath; $kernelBaselineReady=$true
                $receipt.global_kernel_baseline_sha256=$globalKernelSha
                Atomic (Join-Path $directory 'receipt.json') $receipt
            }
            if (-not $kernelBaselineReady) { Fail 'Global kernel baseline is unavailable.' }
            foreach ($line in $kernelLines) { if (-not $kernelBaseline.Contains($line)) { $null=$newKernel.Add($line) } }
            foreach ($line in $kernelBaseline) {
                if (-not $kernelLines.Contains($line)) { Fail 'Baseline kernel fatal history disappeared.' }
            }
            if ($newKernel.Count) { Fail 'New kernel native fatal.' }
            $lastProbe=$clock.Elapsed.TotalSeconds; $probe.process.Dispose(); $probe=$null; $nextProbe=$lastProbe+2
            if (-not $started) {
                if ($errors.Count -or $scopeState -ne 'not-found') { Fail 'Baseline scope identity is not clean.'; break }
                $argv=@('/usr/bin/systemd-run','--user','--scope','--quiet',('--unit='+$unit),('--property=MemoryMax='+$memory),'--property=MemorySwapMax=0',('--property=RuntimeMaxSec='+($TimeoutSeconds+45)+'s'),'--property=TimeoutStopSec=5s','--', $python,'-I','-B',(Linux $controller),'--bundle-root',(Linux $bundle),'--artifact-root',(Linux $root),'--linux-root',$linuxRoot,'--phase',$Phase,'--run-id',$RunId,'--scope-unit',$unit,'--nonce',$nonce,'--timeout-seconds',[string]$TimeoutSeconds)
                $main=StartWsl $argv 'controller'; $started=$true
            }
        }
        if ($main -and $main.process.HasExited -and $null -eq $exitAt) {
            CloseProcess $main; $proxyExit=$main.process.ExitCode; $actualExit=$true; $exitAt=$clock.Elapsed.TotalSeconds
            if ($proxyExit -ne 0) { Fail 'Controller or its Python finalization exited nonzero.' }
        }
        if ($elapsed -gt $TimeoutSeconds+15 -and $null -eq $exitAt) { Fail 'Outer phase deadline exceeded.' }
        # The scope contains all descendants, including daemons that erase their environment.
        if ($errors.Count -and $started -and -not $cleanup) {
            $forced=$true; $cleanup=StartWsl @('/usr/bin/systemctl','--user','stop','--no-block',$unit) 'cleanup'
        }
        if ($cleanup -and $cleanup.process.HasExited -and -not $cleanup.closed) { CloseProcess $cleanup }
        if ($cleanup -and $cleanup.clock.Elapsed.TotalSeconds -ge 5 -and $populated -ne '0' -and -not $kill) {
            $kill=StartWsl @('/usr/bin/systemctl','--user','kill','--kill-whom=all','--signal=SIGKILL',$unit) 'cleanup-kill'
        }
        if ($kill -and $kill.process.HasExited -and -not $kill.closed) { CloseProcess $kill }
        if ($null -ne $exitAt -and $lastProbe -ge $exitAt -and $populated -eq '0') {
            if ($null -eq $emptyAt) { $emptyAt=$lastProbe }
        } elseif ($null -ne $exitAt -and $clock.Elapsed.TotalSeconds-$exitAt -gt 5 -and $populated -ne '0') { Fail 'Descendants survived controller exit.' }
        if ($null -ne $emptyAt -and $populated -ne '0') { Fail 'Owned descendants reappeared after cleanup.' }
        if ($null -ne $emptyAt -and $lastProbe -ge $emptyAt+30 -and -not $probe -and $samples.Count -gt 0 -and $samples[$samples.Count-1].elapsed_seconds -ge $emptyAt+30) { break }
        if ($elapsed -gt $TimeoutSeconds+90) { Fail 'Bounded cleanup/observation could not finish.'; break }
        Start-Sleep -Milliseconds 200
    }
} catch {
    $observerFailure = $_
    $chain = [Collections.Generic.List[object]]::new()
    $exception = $observerFailure.Exception
    while ($null -ne $exception) {
        $data = @{}
        foreach ($key in $exception.Data.Keys) { $data[[string]$key] = [string]$exception.Data[$key] }
        $chain.Add(@{type=$exception.GetType().FullName; message=$exception.Message; hresult=$exception.HResult; native_code=($exception.HResult -band 0xFFFF); data=$data})
        $exception=$exception.InnerException
    }
    $receipt.observer_error=@{exception_chain=@($chain); script_stack_trace=$observerFailure.ScriptStackTrace}
    Fail ('Observer error: '+$observerFailure.Exception.Message)
}
finally {
    try {
    if ($started -and ($errors.Count -or $null -eq $emptyAt)) {
        # A failing Windows observer must still request termination of its exact UUID scope.
        # This is cleanup only, never a retry of a workload or another user's process.
        $forced=$true
        try {
            $emergency=StartWsl @('/usr/bin/systemctl','--user','kill','--kill-whom=all','--signal=SIGKILL',$unit) 'emergency-cleanup'
            if (-not $emergency.process.WaitForExit(5000)) { $emergency.process.Kill(); $null=$emergency.process.WaitForExit(1000); Fail 'Emergency scope cleanup did not complete.' }
            CloseProcess $emergency; $emergency.process.Dispose()
        } catch { Fail 'Emergency scope termination could not be verified.' }
    }
    foreach ($item in @($probe,$cleanup,$kill,$main)) {
        if ($item) {
            if (-not $item.process.HasExited) { $item.process.Kill(); $null=$item.process.WaitForExit(1000); $forced=$true; Fail 'A Windows proxy needed forced cleanup; Linux cleanup remains unqualified.' }
            try { CloseProcess $item } catch { Fail 'Process log finalization failed.' }
            $item.process.Dispose()
        }
    }
    } finally {
    if ($null -ne $sleepToken) { try { Stop-QualificationSleepInhibition -State $sleepToken } catch { Fail 'System-required execution state restoration failed.' } }
    }
}
try {
    if ((Sha $globalCrashPath) -cne $globalCrashSha -or -not (SameManifest $baselineCrashes (Crashes))) { throw 'Global crash baseline or its observed metadata changed.' }
    if (-not $kernelBaselineReady -or (Sha $globalKernelPath) -cne $globalKernelSha) { throw 'Global kernel baseline changed or was not established.' }
    if (-not (SameManifest $manifest (BundleManifest))) { throw 'Frozen bundle changed during phase.' }
    $report=ReadJson (Join-Path $directory 'controller-report.json')
    $work=ReadJson (Join-Path $directory 'workload-report.json')
    if ($report.status -cne 'controller_passed_pending_external_watch' -or $report.worker_returncode -ne 0 -or $report.errors.Count -or $work.status -cne 'workload_passed_pending_external_watch' -or -not (SameManifest $report.runtime $report.runtime_end) -or -not (SameManifest $work.runtime $work.runtime_end)) { throw 'Controller/workload did not pass or runtime identities changed.' }
    if ($report.run_id -cne $RunId -or $report.phase -cne $Phase -or $report.initial_boot_id -cne $initialBoot -or $report.final_boot_id -cne $finalBoot) { throw 'Controller identity/boot mismatch.' }
    if ($report.workload_sha256 -cne (Sha (Join-Path $directory 'workload-report.json'))) { throw 'Workload report hash mismatch.' }
    $receipt.runtime=$report.runtime
    $receipt.controller_report_sha256=Sha (Join-Path $directory 'controller-report.json')
    $receipt.workload_report_sha256=Sha (Join-Path $directory 'workload-report.json')
} catch { Fail ('Final evidence: '+$_.Exception.Message) }
$receipt.errors=@($errors); $receipt.initial_boot_id=$initialBoot; $receipt.final_boot_id=$finalBoot
$receipt.proxy_exit_code=$proxyExit; $receipt.controller_exit_verified=$actualExit
$receipt.cleanup_complete=($actualExit -and $null -ne $emptyAt -and $populated -eq '0')
$receipt.cleanup_grace_seconds=if ($null -ne $emptyAt) { $lastProbe-$emptyAt } else { 0 }
$receipt.cleanup_confirmed_elapsed_seconds=$emptyAt
$receipt.runtime_verified_after_cleanup=($null -ne $emptyAt -and $lastProbe -ge $emptyAt+30 -and $errors.Count -eq 0)
$receipt.forced_cleanup=$forced; $receipt.new_wsl_dumps=@($newDumps | Sort-Object); $receipt.new_kernel_fatals=@($newKernel | Sort-Object)
$receipt.finished_utc=[DateTime]::UtcNow.ToString('o')
$receipt.power_profile_end=$lastPower
$receipt.power_samples_sha256=if (Test-Path -LiteralPath (Join-Path $directory 'power-samples.jsonl')) { Sha (Join-Path $directory 'power-samples.jsonl') } else { $null }
$receipt.sleep_inhibition=$sleepToken
$receipt.power_monitor=@{interval_seconds=2; sample_count=$samples.Count; controller_exit_elapsed_seconds=$exitAt; samples_artifact='power-samples.jsonl'}
$maxPowerGap=0.0; $previousPowerTime=0.0
foreach ($sample in $samples) { $maxPowerGap=[Math]::Max($maxPowerGap,$sample.elapsed_seconds-$previousPowerTime); $previousPowerTime=$sample.elapsed_seconds }
$receipt.power_monitor.maximum_observed_gap_seconds=$maxPowerGap
if (-not $samples.Count -or $maxPowerGap -gt 15 -or $null -eq $emptyAt -or $previousPowerTime -lt $emptyAt+30) { Fail 'Power sampling did not cover cleanup and the full grace period.' }
$receipt.errors=@($errors)
$receipt.passed=($errors.Count -eq 0 -and $actualExit -and $proxyExit -eq 0 -and $receipt.cleanup_complete -and $receipt.cleanup_grace_seconds -ge 30 -and -not $forced)
Atomic (Join-Path $directory 'receipt.json') $receipt
if ($Phase -eq 'attest' -and $receipt.passed) {
    $candidate=ReadJson (Join-Path $root 'gate-candidate.json')
    if ($work.gate_candidate_sha256 -cne (Sha (Join-Path $root 'gate-candidate.json')) -or $candidate.status -cne 'pending_external_attestation') { throw 'Gate candidate binding failed.' }
    $candidate.status='pass'; $candidate.attestation_receipt_sha256=Sha (Join-Path $directory 'receipt.json')
    $candidate.published_utc=[DateTime]::UtcNow.ToString('o')
    Atomic (Join-Path $root 'postbios-runtime-gate.json') $candidate
}
@{phase=$Phase; passed=$receipt.passed; errors=$receipt.errors; controller_exit_verified=$receipt.controller_exit_verified;
  cleanup_complete=$receipt.cleanup_complete; cleanup_grace_seconds=$receipt.cleanup_grace_seconds;
  new_wsl_dumps=$receipt.new_wsl_dumps; new_kernel_fatals=$receipt.new_kernel_fatals;
  receipt_path=(Join-Path $directory 'receipt.json')} | ConvertTo-Json -Depth 5
if (-not $receipt.passed) { exit 1 }
