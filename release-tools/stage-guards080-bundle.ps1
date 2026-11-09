#Requires -Version 7.2
[CmdletBinding()]
param([switch]$Execute,[string]$InputManifestSha,[string]$PreviousRunId,[string]$PreviousCleanupAdmissionSha)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$workspace='C:\Users\piotr\OneDrive\Dokumenty\ChatGPT\devbrain'
$physicalBase='C:\Users\piotr\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\DevBrain\qualification'
if(-not $Execute){@{execute=$false;scope='stage_current_guard_tools_only'}|ConvertTo-Json;return}
if($InputManifestSha -cnotmatch '^[0-9a-f]{64}$'){throw 'Explicit reviewed manifest SHA is required'}
# Reuse only the reviewed Windows handle/path checks, never execute its staging body.
$tokens=$null;$parseErrors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $workspace 'release-tools/stage-postbios-bundle-080.ps1'),[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Path helper parse failed'}
$addType=@($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'Add-Type'},$true))
if($addType.Count -ne 1){throw 'Unexpected canonical path helper'}
. ([scriptblock]::Create($addType[0].Extent.Text))
foreach($name in 'NoReparse','HashFile','SourceProof'){
 $function=@($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$false))
 if($function.Count -ne 1){throw 'Unexpected path function'}
 . ([scriptblock]::Create($function[0].Extent.Text))
}
if(-not [QualificationStagePath080]::FinalPath($workspace).Equals($workspace,[StringComparison]::OrdinalIgnoreCase)){throw 'Workspace canonical handle differs'}
$inventoryPath=Join-Path $workspace 'evidence/080-guards-input-files.json'
SourceProof $inventoryPath
if((HashFile $inventoryPath) -cne $InputManifestSha){throw 'Guard freeze inventory changed'}
$inventory=Get-Content -LiteralPath $inventoryPath -Raw|ConvertFrom-Json -AsHashtable
if($inventory.helper_files.Count -ne 28 -or $inventory.test_modules.Count -ne 8){throw 'Guard inventory incomplete'}
$qualified=Get-Content -LiteralPath (Join-Path $workspace 'evidence/080-postbios-active-context.json') -Raw|ConvertFrom-Json
$preflight=Get-Content -LiteralPath (Join-Path $workspace 'evidence/080-preflight-candidate-context.json') -Raw|ConvertFrom-Json
$gateSource=Join-Path $qualified.artifact_root 'postbios-runtime-gate.json'
NoReparse $gateSource
if((HashFile $gateSource) -cne 'a6bd365cea68986ed6795990272c766679bbd29fd9407e904a12c2956bcdf7fb'){throw 'Qualified gate changed'}
$gate=Get-Content -LiteralPath $gateSource -Raw|ConvertFrom-Json
if($gate.status -cne 'pass'){throw 'Runtime gate is not passed'}
$contextPath=Join-Path $workspace 'evidence/080-guards-active-context.json'
$previousContext=$null
if(Test-Path -LiteralPath $contextPath){
 $previousContext=Get-Content -LiteralPath $contextPath -Raw|ConvertFrom-Json -AsHashtable
 if(-not $PreviousRunId -or $previousContext.run_id -cne $PreviousRunId -or $previousContext.status -cnotin @('failed_interrupted','failed_bootstrap')){throw 'Explicit, cleaned failed context required before a new run'}
 if($previousContext.windows_root -cne (Join-Path $physicalBase $PreviousRunId)){throw 'Previous context escaped the known physical root'}
 if($previousContext.status -ceq 'failed_bootstrap'){
  if($PreviousCleanupAdmissionSha -cnotmatch '^[0-9a-f]{64}$'){throw 'Explicit reviewed bootstrap cleanup SHA is required'}
  $admissionPath=Join-Path $previousContext.windows_root 'artifacts/cleanup-admission.json'
  NoReparse $admissionPath
  if((HashFile $admissionPath) -cne $PreviousCleanupAdmissionSha){throw 'Bootstrap cleanup admission changed'}
  $previousAudit=Get-Content -LiteralPath $admissionPath -Raw|ConvertFrom-Json -AsHashtable
  if($previousAudit.run_id -cne $PreviousRunId -or $previousAudit.scope -cne 'failed_guard_cleanup_admission' -or $previousAudit.cleanup_complete -ne $true -or $previousAudit.safe_to_start_new_isolated_run -ne $true -or $previousAudit.guard_pass -ne $false -or $previousAudit.qualified -ne $false -or $previousAudit.resuming_failed_run_allowed -ne $false -or $previousAudit.original_receipt_modified -ne $false){throw 'Bootstrap cleanup is not admitted for a fresh run'}
  foreach($binding in @(@('receipt.json','receipt_sha256'),@('supervisor-exit.json','exit_proof_sha256'),@('supervisor.stderr.log','stderr_sha256'))){
   $boundPath=Join-Path $previousContext.windows_root ('artifacts/'+$binding[0]);NoReparse $boundPath
   if((HashFile $boundPath) -cne $previousAudit[$binding[1]]){throw 'Bootstrap cleanup evidence changed'}
  }
  $previousCleanupHash=$PreviousCleanupAdmissionSha
 }else{
  NoReparse $previousContext.cleanup_audit
  if(-not $previousContext.cleanup_audit.StartsWith($previousContext.windows_root+'\',[StringComparison]::OrdinalIgnoreCase) -or (HashFile $previousContext.cleanup_audit) -cne $previousContext.cleanup_audit_sha256){throw 'Previous cleanup audit binding failed'}
  $previousAudit=Get-Content -LiteralPath $previousContext.cleanup_audit -Raw|ConvertFrom-Json -AsHashtable
  if($previousAudit.cleanup_complete -ne $true -or $previousAudit.receipt_pass -ne $false){throw 'Previous failed run cleanup is not confirmed'}
  $previousCleanupHash=$previousContext.cleanup_audit_sha256
 }
} elseif($PreviousRunId){throw 'Expected previous context is missing'}
$runId='guards080-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$runRoot=Join-Path $physicalBase $runId
if(Test-Path -LiteralPath $runRoot){throw 'Fresh guard run required'}
NoReparse $runRoot
$toolsRoot=Join-Path $runRoot 'tools'
$artifactRoot=Join-Path $runRoot 'artifacts'
$candidateRoot=Join-Path $runRoot 'candidate'
foreach($directory in $toolsRoot,$artifactRoot,$candidateRoot){$null=[IO.Directory]::CreateDirectory($directory)}
$files=@{}
function CopyBound([string]$Source,[string]$Relative,[string]$Expected,[bool]$WorkspaceSource){
 if($Relative -match '(^|/)\.\.(/|$)|\\' -or $Relative.StartsWith('/')){throw 'Unsafe bundle relative path'}
 if($WorkspaceSource){SourceProof $Source}else{NoReparse $Source}
 if((HashFile $Source) -cne $Expected){throw ('Unexpected input SHA: '+$Relative)}
 $target=Join-Path $runRoot $Relative
 NoReparse $target
 $null=[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
 [IO.File]::Copy($Source,$target,$false)
 NoReparse $target
 if(-not [QualificationStagePath080]::FinalPath($target).Equals($target,[StringComparison]::OrdinalIgnoreCase)){throw 'Destination handle proof differs'}
 if((HashFile $target) -cne $Expected -or (HashFile $Source) -cne $Expected){throw ('Copy binding failed: '+$Relative)}
 $files[$Relative]=$Expected
}
foreach($name in @($inventory.helper_files.Keys|Sort-Object)){
 if([IO.Path]::GetFileName($name) -cne $name -or $name -match '__pycache__|\.pyc$'){throw 'Unexpected helper name'}
 CopyBound (Join-Path $workspace ('release-tools/'+$name)) ('tools/'+$name) $inventory.helper_files[$name] $true
}
CopyBound $inventoryPath 'guard-input-manifest.json' (HashFile $inventoryPath) $true
CopyBound $gateSource 'artifacts/gate.json' (HashFile $gateSource) $false
$candidateSource=Join-Path $preflight.windows_root 'releases/candidates/preflight/devbrain-update-0.8.0.py'
CopyBound $candidateSource 'candidate/devbrain-update-0.8.0.py' 'cca9f326ed49ff419a58344d5ca9c12c6ede68d475fb51cb8d8b2016a7594bcf' $false
$linuxRoot='/mnt/c/'+$runRoot.Substring(3).Replace('\','/')
$linuxArtifacts=$linuxRoot+'/artifacts'
$qualifiedCache='/home/piotr/.cache/devbrain-runtime-080/postbios-postbios4601-20261107T194513Z'
$spec=@{run_id=$runId;python_launcher=($qualifiedCache+'/venv/bin/python');argv=@('--execute','--phase','guards','--run-id',$runId,'--artifact-root',$linuxArtifacts,'--source-root',($qualifiedCache+'/project/devbrain'),'--private-root','/home/piotr/.cache/devbrain-regressions-080','--updater',($linuxRoot+'/candidate/devbrain-update-0.8.0.py'));env=@{};required_artifacts=@('gate.json','controller-report.json','supervisor-report.json','regression-source.json','regressions-results.json','guards-collection.json','guards.xml')}
$specPath=Join-Path $artifactRoot 'spec.json'
[IO.File]::WriteAllText($specPath,($spec|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
$files['artifacts/spec.json']=HashFile $specPath
$actual=@(Get-ChildItem -LiteralPath $runRoot -Recurse -Force -File)
if($actual.Count -ne 32 -or $files.Count -ne 32){throw 'Bundle inventory count mismatch'}
foreach($file in $actual){NoReparse $file.FullName;$relative=$file.FullName.Substring($runRoot.Length+1).Replace('\','/');if(-not $files.ContainsKey($relative) -or (HashFile $file.FullName) -cne $files[$relative]){throw 'Final bundle mismatch'}}
$freeze=@{scope='current_guard_toolbundle';run_id=$runId;frozen_utc=[DateTime]::UtcNow.ToString('o');helper_count=28;executed_test_modules=$inventory.test_modules;files=$files;source_and_destination_hashes_verified=$true;canonical_handle_proofs_passed=$true;destination_reparse_checks_passed=$true;qualified_gate_sha256=(HashFile $gateSource);candidate_is_preflight_only=$true;linux_workload_started=$false}
$freezePath=Join-Path $runRoot 'toolbundle-freeze.json'
[IO.File]::WriteAllText($freezePath,($freeze|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
$context=@{run_id=$runId;windows_root=$runRoot;linux_root=$linuxRoot;windows_tools=$toolsRoot;linux_tools=($linuxRoot+'/tools');artifact_root=$artifactRoot;linux_artifact_root=$linuxArtifacts;specification=$specPath;controller=($linuxRoot+'/tools/run-regressions-080.py');base_python=$gate.runtime.python_executable;python_launcher=$spec.python_launcher;source_root=($qualifiedCache+'/project/devbrain');private_root='/home/piotr/.cache/devbrain-regressions-080';freeze_manifest=$freezePath;freeze_sha256=(HashFile $freezePath);gate_sha256=(HashFile $gateSource);timeout_seconds=600;cleanup_grace_seconds=30;phase='guards';status='prepared_not_run'}
if($null -ne $previousContext){
 $backup=Join-Path $previousContext.windows_root 'failed-context-before-replacement.json'
 if(Test-Path -LiteralPath $backup){if((HashFile $backup) -cne (HashFile $contextPath)){throw 'Previous context backup differs'}}
 else{[IO.File]::Copy($contextPath,$backup,$false)}
 $context.previous_failed_run=$previousContext.run_id
 $context.previous_cleanup_audit_sha256=$previousCleanupHash
}
$atomicPath=Join-Path $qualified.bundle_root 'release-tools/run-postbios-diagnostic-080.ps1'
NoReparse $atomicPath
if((HashFile $atomicPath) -cne '47f20a48b95ef3f2f1e6f16651b3f5dd189e62070a8cddee883bee1d36b5281f'){throw 'Qualified atomic publisher changed'}
$tokens=$null;$parseErrors=$null
$atomicAst=[System.Management.Automation.Language.Parser]::ParseFile($atomicPath,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Qualified atomic publisher parse failed'}
$atomicFunction=@($atomicAst.FindAll({param($node)$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Atomic'},$false))
if($atomicFunction.Count -ne 1){throw 'Qualified atomic function differs'}
. ([scriptblock]::Create($atomicFunction[0].Extent.Text))
Atomic $contextPath $context
$context|ConvertTo-Json -Depth 5
