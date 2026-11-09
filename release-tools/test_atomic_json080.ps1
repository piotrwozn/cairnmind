#Requires -Version 7.2
<# Windows-only I/O fixture. Imports function ASTs, never executes a wrapper.
No WSL, Python, Java, model, system configuration or production data is used. #>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Root)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
$physicalRoot = 'C:\Users\piotr\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\DevBrain\qualification'
if (-not $rootPath.StartsWith($physicalRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $rootPath)) { throw 'Use a new private fixture directory under the verified physical qualification root.' }
$cursor = $rootPath
while ($cursor) {
    if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse fixture path refused.' }
    $cursor = [IO.Path]::GetDirectoryName($cursor)
}
$null = [IO.Directory]::CreateDirectory($rootPath)
$caseRoot = Join-Path $rootPath 'cases'
$null = [IO.Directory]::CreateDirectory($caseRoot)
$allowedRoot = $rootPath # Plain() remains restricted to this synthetic fixture.
$results = [Collections.Generic.List[object]]::new()
$errors = [Collections.Generic.List[string]]::new()
$sourceHashes = @{}
$retryLog = [IO.StringWriter]::new([Globalization.CultureInfo]::InvariantCulture)
$previousErrorWriter = [Console]::Error
$cleanupComplete = $false
function Check([string]$Name, [bool]$Condition, $Metadata=@{}) {
    $results.Add(@{name=$Name; passed=$Condition; metadata=$Metadata})
    if (-not $Condition) { throw ('Assertion failed: '+$Name) }
}
function CauseCode($Exception) {
    while ($Exception -and $Exception -isnot [IO.IOException] -and $Exception -isnot [UnauthorizedAccessException]) { $Exception=$Exception.InnerException }
    if ($Exception) { return ($Exception.HResult -band 65535) }
    return $null
}
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
namespace DevBrainAtomicFixture {
    public sealed class TimedReadLock : IDisposable {
        private FileStream stream;
        public readonly Task ReleaseTask;
        public TimedReadLock(string path, int milliseconds) {
            stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
            ReleaseTask = Task.Run(async () => { await Task.Delay(milliseconds); Dispose(); });
        }
        public TimedReadLock(FileStream existing, int milliseconds) {
            stream = existing;
            ReleaseTask = Task.Run(async () => { await Task.Delay(milliseconds); Dispose(); });
        }
        public void Dispose() { Interlocked.Exchange(ref stream, null)?.Dispose(); }
    }
}
'@
try {
    [Console]::SetError($retryLog)
    foreach ($variant in @(
        @{name='final'; file='run-supervised-080.ps1'; writer='Write-AtomicJson'; reader='Read-Json'; guard='Assert-PlainPath'},
        @{name='diagnostic'; file='run-postbios-diagnostic-080.ps1'; writer='Atomic'; reader='ReadJson'; guard='Plain'})) {
        $source = Join-Path $PSScriptRoot $variant.file
        $sourceHashes[$variant.file] = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
        $tokens=$null; $parseErrors=$null
        $ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$parseErrors)
        if ($parseErrors.Count) { throw 'Fixture refuses helpers with syntax errors.' }
        $definitions=@{}
        foreach ($name in @($variant.writer,$variant.reader,$variant.guard)) {
            $function = $ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$true)
            if ($function.Count -ne 1) { throw 'Expected exactly one reviewed helper function.' }
            $definitions[$name]=$function[0]
            # Only the three exact function definitions above are installed;
            # wrapper param blocks, loops and main statements are not evaluated.
            . ([scriptblock]::Create($function[0].Extent.Text))
        }
        $old=@{sequence=1; unix_time=1234.5; value='synthetic-old'}
        $new=@{sequence=2; unix_time=1234.5; value='synthetic-new'}
        $path=Join-Path $caseRoot ($variant.name+'.json')
        $candidate=Join-Path $caseRoot ($variant.name+'.candidate')
        $null = & $variant.writer $path $old
        [IO.File]::WriteAllText($candidate, ($new | ConvertTo-Json -Compress))
        $handle=[IO.FileStream]::new($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        $nativeCode=$null
        try { try { [IO.File]::Move($candidate,$path,$true) } catch { $nativeCode=CauseCode $_.Exception } }
        finally { $handle.Dispose(); if ([IO.File]::Exists($candidate)) { [IO.File]::Delete($candidate) } }
        Check ($variant.name+'_single_move_collision') ($nativeCode -in @(5,32,33)) @{observed_win32_code=$nativeCode; historical_error_code_inferred=$false}

        $lock=[DevBrainAtomicFixture.TimedReadLock]::new($path,250)
        $clock=[Diagnostics.Stopwatch]::StartNew()
        try { $publication=& $variant.writer $path $new } finally { $lock.Dispose(); $null=$lock.ReleaseTask.Wait(1000) }
        $elapsed=$clock.Elapsed.TotalMilliseconds
        $observed=& $variant.reader $path
        Check ($variant.name+'_short_collision_preserves_payload') ($observed.sequence -eq 2 -and $observed.unix_time -eq 1234.5 -and $elapsed -ge 150 -and $elapsed -lt 1500) @{elapsed_ms=$elapsed}
        Check ($variant.name+'_successful_retry_removes_temp') (@(Get-ChildItem -LiteralPath $caseRoot -Filter '*.tmp').Count -eq 0)

        $null=& $variant.writer $path $old
        $handle=[IO.FileStream]::new($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        $clock=[Diagnostics.Stopwatch]::StartNew(); $failed=$false; $lastPublished=1
        try {
            try { $null=& $variant.writer $path $new; $lastPublished=2 } catch { $failed=$true; $nativeCode=CauseCode $_.Exception }
        } finally { $handle.Dispose() }
        $elapsed=$clock.Elapsed.TotalMilliseconds
        $observed=& $variant.reader $path
        Check ($variant.name+'_permanent_collision_is_bounded_failure') ($failed -and $nativeCode -in @(5,32,33) -and $elapsed -ge 900 -and $elapsed -le 1500) @{elapsed_ms=$elapsed; observed_win32_code=$nativeCode}
        Check ($variant.name+'_failed_publish_keeps_previous_heartbeat') ($observed.sequence -eq 1 -and $observed.unix_time -eq 1234.5 -and $lastPublished -eq 1)
        Check ($variant.name+'_failed_retry_removes_temp') (@(Get-ChildItem -LiteralPath $caseRoot -Filter '*.tmp').Count -eq 0)

        # Execute the reader's actual FileStream constructor. Windows rename
        # replacement can still deny an open target even with delete sharing.
        # Test bounded retry for this actual reader, not immediate replacement.
        $open = $definitions[$variant.reader].Body.FindAll({param($node)
            $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Right.Extent.Text.Contains('[IO.FileStream]::new(')
        },$true)
        if ($open.Count -ne 1) { throw 'Reader has no unique snapshot FileStream constructor.' }
        $null=& $variant.writer $path $old
        $handle=& ([scriptblock]::Create($open[0].Right.Extent.Text))
        $lock=[DevBrainAtomicFixture.TimedReadLock]::new($handle,250)
        $clock=[Diagnostics.Stopwatch]::StartNew()
        try { $null=& $variant.writer $path $new } finally { $lock.Dispose(); $null=$lock.ReleaseTask.Wait(1000) }
        $elapsed=$clock.Elapsed.TotalMilliseconds
        $observed=& $variant.reader $path
        Check ($variant.name+'_actual_reader_short_collision_preserves_lease') ($observed.sequence -eq 2 -and $observed.unix_time -eq 1234.5 -and $elapsed -lt 1500) @{elapsed_ms=$elapsed; immediate_overwrite_guaranteed=$false}

        $null=& $variant.writer $path $old
        $handle=& ([scriptblock]::Create($open[0].Right.Extent.Text))
        $clock=[Diagnostics.Stopwatch]::StartNew(); $failed=$false; $lastPublished=1
        try { try { $null=& $variant.writer $path $new; $lastPublished=2 } catch { $failed=$true; $nativeCode=CauseCode $_.Exception } }
        finally { $handle.Dispose() }
        $elapsed=$clock.Elapsed.TotalMilliseconds
        $observed=& $variant.reader $path
        Check ($variant.name+'_actual_reader_permanent_collision_fails_closed') ($failed -and $nativeCode -in @(5,32,33) -and $elapsed -le 1500 -and $observed.sequence -eq 1 -and $observed.unix_time -eq 1234.5 -and $lastPublished -eq 1 -and @(Get-ChildItem -LiteralPath $caseRoot -Filter '*.tmp').Count -eq 0) @{elapsed_ms=$elapsed; observed_win32_code=$nativeCode}

        # Delete-sharing itself is checked by renaming the open source to an
        # UNUSED path. This is a test of reader flags, not the publication path.
        $handle=& ([scriptblock]::Create($open[0].Right.Extent.Text))
        $snapshotPath=Join-Path $caseRoot ($variant.name+'.reader-snapshot')
        try {
            [IO.File]::Move($path,$snapshotPath)
            $null=& $variant.writer $path $new
            $reader=[IO.StreamReader]::new($handle,[Text.Encoding]::UTF8,$true,4096,$true)
            try { $snapshot=$reader.ReadToEnd() | ConvertFrom-Json -AsHashtable } finally { $reader.Dispose() }
        } finally { $handle.Dispose(); if ([IO.File]::Exists($snapshotPath)) { [IO.File]::Delete($snapshotPath) } }
        $observed=& $variant.reader $path
        Check ($variant.name+'_reader_delete_share_and_complete_old_snapshot') ($snapshot.sequence -eq 1 -and $observed.sequence -eq 2)
    }
} catch { $errors.Add($_.Exception.Message) }
finally {
    [Console]::SetError($previousErrorWriter)
    [IO.File]::WriteAllText((Join-Path $rootPath 'retry-events.jsonl'),$retryLog.ToString(),[Text.UTF8Encoding]::new($false))
    $retryLog.Dispose()
    # Resolve and bound the exact private target before recursive removal.
    $resolvedCases=[IO.Path]::GetFullPath($caseRoot)
    if ($resolvedCases -cne (Join-Path $rootPath 'cases') -or -not $resolvedCases.StartsWith($rootPath+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Fixture cleanup target escaped its root.' }
    if (Test-Path -LiteralPath $resolvedCases) {
        if ((Get-Item -LiteralPath $resolvedCases).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Fixture cleanup refuses a replaced directory.' }
        Remove-Item -LiteralPath $resolvedCases -Recurse -Force
    }
    $cleanupComplete = -not (Test-Path -LiteralPath $resolvedCases)
}
$report=@{schema_version=1; scope='windows_atomic_io_regression'; passed=($errors.Count -eq 0 -and $cleanupComplete -and $results.Count -eq 18); cases=$results.ToArray(); errors=$errors.ToArray(); cleanup_complete=$cleanupComplete; source_sha256=$sourceHashes; runtime_gate_qualification=$false; linux_started=$false; test_data_root=$rootPath; powershell=[string]$PSVersionTable.PSVersion; finished_utc=[DateTime]::UtcNow.ToString('o'); filesystem_contract=@{replacement_of_open_target_may_be_denied=$true; retry_deadline_ms=1000; wall_clock_assertion_ceiling_ms=1500; heartbeat_payload_timestamp_refreshed=$false; parent_lease_seconds=15; reader_delete_share_is_not_immediate_overwrite_guarantee=$true; primary_source='https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fscc/1d2673a8-8fb9-4868-920a-775ccaa30cf8'}}
[IO.File]::WriteAllText((Join-Path $rootPath 'report.json'),($report | ConvertTo-Json -Depth 12)+"`n",[Text.UTF8Encoding]::new($false))
$report | ConvertTo-Json -Depth 5
if (-not $report.passed) { exit 1 }
