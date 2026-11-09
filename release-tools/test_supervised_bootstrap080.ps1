#Requires -Version 7.2
<# Pure Windows guards. Imports two function ASTs; never starts WSL or a child.
   The fake HasExited property tests the refusal branch, not real process exit. #>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$source=Join-Path $PSScriptRoot 'run-supervised-080.ps1'
$tokens=$null; $parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Wrapper syntax errors'}
foreach($name in @('Test-FailedBootstrapGrace','Close-StreamProcess')){
    $nodes=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name},$true))
    if($nodes.Count -ne 1){throw 'Expected exactly one frozen function'}
    . ([scriptblock]::Create($nodes[0].Extent.Text))
}
$checks=[Collections.Generic.List[object]]::new()
function Check($Name,$Condition){
    $checks.Add(@{name=$Name;passed=[bool]$Condition})
    if(-not $Condition){throw ('Guard failed: '+$Name)}
}
$state=@{actual_exit=$true;returncode=1;scope_seen=$false;initially_absent=$true;load_state='not-found';population='0';group='';identity='';absent_since=4.0;last_scope=34.0;last_probe=34.0}
Check 'failed_bootstrap_finishes_only_after_full_absence_grace' (Test-FailedBootstrapGrace $state 30)
foreach($change in @(
    @{key='actual_exit';value=$false},@{key='returncode';value=$null},@{key='returncode';value=0},
    @{key='scope_seen';value=$true},@{key='initially_absent';value=$false},@{key='load_state';value='loaded'},
    @{key='population';value='1'},@{key='population';value='unknown'},@{key='group';value='/user.slice/foreign.scope'},
    @{key='identity';value='1:2'},@{key='absent_since';value=$null},@{key='last_scope';value=33.99},@{key='last_probe';value=33.99}
)){
    $bad=$state.Clone();$bad[$change.key]=$change.value
    Check ('refuse_'+$change.key+'_'+[string]$change.value) (-not (Test-FailedBootstrapGrace $bad 30))
}
$out=[IO.MemoryStream]::new();$err=[IO.MemoryStream]::new()
$item=@{closed=$false;process=[pscustomobject]@{HasExited=$false};stdout=$out;stderr=$err;outTask=[Threading.Tasks.Task]::CompletedTask;errTask=[Threading.Tasks.Task]::CompletedTask}
try{
    $refused=$false;try{Close-StreamProcess $item}catch{$refused=$true}
    Check 'live_process_cannot_close_logs_as_complete' ($refused -and -not $item.closed -and $out.CanWrite -and $err.CanWrite)
    $item.process.HasExited=$true
    $bytes=[Text.Encoding]::UTF8.GetBytes('small diagnostic stderr')
    $err.Write($bytes)
    Close-StreamProcess $item
    Check 'completed_logs_are_flushed_closed_and_retained' ($item.closed -and -not $out.CanWrite -and -not $err.CanWrite -and [Text.Encoding]::UTF8.GetString($err.ToArray()) -ceq 'small diagnostic stderr')
    Close-StreamProcess $item
    Check 'repeated_final_close_is_idempotent' $item.closed
}finally{$out.Dispose();$err.Dispose()}
@{scope='devbrain_pure_windows_bootstrap_guards';passed=$true;source_sha256=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant();checks=@($checks);linux_processes_started=$false;actual_process_exit_tested=$false}|ConvertTo-Json -Depth 5
