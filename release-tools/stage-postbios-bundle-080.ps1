#Requires -Version 7.2
[CmdletBinding()]
param([switch]$Execute)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$workspace='C:\Users\piotr\OneDrive\Dokumenty\ChatGPT\devbrain'
$physicalBase='C:\Users\piotr\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\DevBrain\qualification'
if(-not $Execute){@{execute=$false; workspace=$workspace; destination_base=$physicalBase}|ConvertTo-Json; return}
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
public static class QualificationStagePath080 {
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder text, uint length, uint flags);
 public static string FinalPath(string path) {
  using(var handle=CreateFileW(path,128,7,IntPtr.Zero,3,0x02000000,IntPtr.Zero)) {
   if(handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
   var text=new StringBuilder(4096);
   uint length=GetFinalPathNameByHandleW(handle,text,(uint)text.Capacity,0);
   if(length==0) throw new Win32Exception(Marshal.GetLastWin32Error());
   if(length>=text.Capacity) throw new InvalidOperationException("Canonical path too long");
   string result=text.ToString();
   return result.StartsWith(@"\\?\") ? result.Substring(4) : result;
  }
 }
}
'@
function NoReparse([string]$Path,[string]$StopAt='') {
    if(-not [IO.Path]::IsPathFullyQualified($Path)){throw 'Absolute path required'}
    $cursor=[IO.Path]::GetFullPath($Path)
    while($cursor){
        if((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint)){throw ('Reparse path: '+$cursor)}
        if($StopAt -and $cursor.Equals($StopAt,[StringComparison]::OrdinalIgnoreCase)){return}
        $cursor=[IO.Path]::GetDirectoryName($cursor)
    }
    if($StopAt){throw 'Source did not stay within its verified workspace'}
}
function HashFile([string]$Path){return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()}
function SourceProof([string]$Path) {
    $full=[IO.Path]::GetFullPath($Path)
    if(-not $full.StartsWith($workspace+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Source escaped allowlisted workspace'}
    NoReparse $full $workspace
    if(-not [QualificationStagePath080]::FinalPath($full).Equals($full,[StringComparison]::OrdinalIgnoreCase)){throw 'Source canonical handle path differs'}
}
if(-not [QualificationStagePath080]::FinalPath($workspace).Equals($workspace,[StringComparison]::OrdinalIgnoreCase)){throw 'Workspace handle proof differs from allowlist'}
$contextPath=Join-Path $workspace 'evidence/080-postbios-active-context.json'
$oldContext=Get-Content -LiteralPath $contextPath -Raw|ConvertFrom-Json -AsHashtable -DateKind String
$oldManifest=Get-Content -LiteralPath (Join-Path $oldContext.artifact_root 'bundle-manifest.json') -Raw|ConvertFrom-Json -AsHashtable
if($oldContext.run_id -cne 'postbios4601-20261107T191300Z'){throw 'This staging recipe requires the reviewed failed baseline'}
$fixturePath=Join-Path $physicalBase 'atomic-io-20261107T194048Z-7253e925/report.json'
NoReparse $fixturePath
$fixture=Get-Content -LiteralPath $fixturePath -Raw|ConvertFrom-Json -AsHashtable
if($fixture.passed -ne $true -or $fixture.cleanup_complete -ne $true -or $fixture.cases.Count -ne 18){throw 'Windows publication fixture is incomplete'}
$runId='postbios4601-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$runRoot=Join-Path $physicalBase $runId
if(Test-Path -LiteralPath $runRoot){throw 'Fresh run root required'}
NoReparse $runRoot
$bundleRoot=Join-Path $runRoot 'bundle'
$artifactRoot=Join-Path $runRoot 'diagnostic-evidence'
$null=[IO.Directory]::CreateDirectory($bundleRoot)
$null=[IO.Directory]::CreateDirectory($artifactRoot)
Push-Location $workspace
try {
    $appFiles=@(rg --files --hidden devbrain -g '!__pycache__' -g '!*.pyc'|ForEach-Object{$_.Replace('\','/')})
    if($LASTEXITCODE -ne 0 -or $appFiles.Count -ne 195){throw 'Current app file inventory is not 195'}
} finally {Pop-Location}
$known=@($oldManifest.files.Keys|Where-Object{$_ -like 'devbrain/*'})
if(@(Compare-Object ($known|Sort-Object) ($appFiles|Sort-Object)).Count){throw 'App inventory changed unexpectedly'}
$approvedChanged=@{
 'devbrain/docs/README.md'='61853aec1f1e5e2447ee53f659e25ef3bfdaa410e99b15e2b1472f5ed61929af'
 'devbrain/frontend/package.json'='5f9ed6484c5f6e90bb86932a240d33fe60ed4572345d761cfbb0373f0ca5898a'
 'devbrain/frontend/package-lock.json'='a5f21cc9b2921530128071466176cfb25877a6e337a4546eec06d576bc49f9d4'
 'release-tools/run-postbios-diagnostic-080.ps1'='47f20a48b95ef3f2f1e6f16651b3f5dd189e62070a8cddee883bee1d36b5281f'
}
$frozen=@{}
foreach($relative in @($oldManifest.files.Keys|Sort-Object)){
    if($relative -match '(^|/)\.\.(/|$)|\\|__pycache__|\.pyc$'){throw 'Unexpected manifest path'}
    $prior=Join-Path $oldContext.bundle_root $relative
    NoReparse $prior
    if((HashFile $prior) -cne $oldManifest.files[$relative]){throw ('Prior frozen bundle changed: '+$relative)}
    $current=($relative -like 'devbrain/*' -or $relative -eq 'release-tools/run-postbios-diagnostic-080.ps1')
    $source=if($current){Join-Path $workspace $relative}else{$prior}
    if($current){SourceProof $source}else{NoReparse $source}
    $expected=if($approvedChanged.ContainsKey($relative)){$approvedChanged[$relative]}else{$oldManifest.files[$relative]}
    if((HashFile $source) -cne $expected){throw ('Unexpected source SHA: '+$relative)}
    $destination=Join-Path $bundleRoot $relative
    if(-not ([IO.Path]::GetFullPath($destination)).StartsWith($bundleRoot+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Destination escaped bundle'}
    NoReparse $destination
    $null=[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
    [IO.File]::Copy($source,$destination,$false)
    NoReparse $destination
    if(-not [QualificationStagePath080]::FinalPath($destination).Equals($destination,[StringComparison]::OrdinalIgnoreCase)){throw 'Destination handle proof differs'}
    if((HashFile $destination) -cne $expected -or (HashFile $source) -cne $expected){throw ('Copy verification failed: '+$relative)}
    $frozen[$relative]=$expected
}
$actual=@(Get-ChildItem -LiteralPath $bundleRoot -Recurse -Force -File)
if($actual.Count -ne 206 -or $frozen.Count -ne 206){throw 'Frozen bundle count mismatch'}
foreach($file in $actual){NoReparse $file.FullName; $relative=$file.FullName.Substring($bundleRoot.Length+1).Replace('\','/'); if(-not $frozen.ContainsKey($relative) -or (HashFile $file.FullName) -cne $frozen[$relative]){throw 'Final file verification failed'}}
$tokens=$null;$parseErrors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $workspace 'release-tools/run-postbios-diagnostic-080.ps1'),[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Observer parse failed'}
$functionAst=@($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Atomic'},$false))
if($functionAst.Count -ne 1){throw 'Atomic function identity failed'}
. ([scriptblock]::Create($functionAst[0].Extent.Text))
$freeze=@{run_id=$runId; created_utc=[DateTime]::UtcNow.ToString('o'); previous_failed_run=$oldContext.run_id; app_files=195; helper_files=9; total_files=206; files=$frozen; source_changes=$approvedChanged; canonical_workspace=$workspace; source_file_handle_proofs_passed=$true; destination_file_handle_proofs_passed=$true; destination_reparse_checks_passed=$true; source_and_destination_hashes_verified=$true; atomic_fixture_report=$fixturePath; atomic_fixture_sha256=(HashFile $fixturePath)}
Atomic (Join-Path $runRoot 'bundle-freeze.json') $freeze
$newContext=@{}
foreach($key in $oldContext.Keys){$newContext[$key]=$oldContext[$key]}
$newContext.run_id=$runId
$newContext.windows_root=$runRoot
$newContext.linux_root='/mnt/c/'+$runRoot.Substring(3).Replace('\','/')
$newContext.bundle_root=$bundleRoot
$newContext.linux_bundle_root='/mnt/c/'+$bundleRoot.Substring(3).Replace('\','/')
$newContext.artifact_root=$artifactRoot
$newContext.previous_failed_run=$oldContext.run_id
$newContext.new_observer_fix='bounded immutable atomic publication retry; reader share-delete'
$newContext.source_changes=@('frontend package versions 0.8.0; dependency graph unchanged','docs/README: prepared account controllers and explicit not_run status')
$newContext.freeze_manifest=Join-Path $runRoot 'bundle-freeze.json'
$newContext.freeze_manifest_sha256=HashFile $newContext.freeze_manifest
Atomic $contextPath $newContext
@{run_id=$runId; app_files=195; helper_files=9; total_files=206; bundle=$bundleRoot; artifacts=$artifactRoot; freeze_sha256=$newContext.freeze_manifest_sha256; observer_sha256=$frozen['release-tools/run-postbios-diagnostic-080.ps1']}|ConvertTo-Json -Depth 4
