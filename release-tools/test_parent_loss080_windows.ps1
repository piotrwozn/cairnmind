#Requires -Version 7.2
<# Pure exception-injection guards. No WSL, child process, or power API call. #>
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$path=Join-Path $PSScriptRoot 'run-parent-loss080.ps1'
$tokens=$null;$parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'Oracle syntax error'}
$functions=@($ast.FindAll({param($node)$node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Invoke-ParentLossFinalization'},$true))
if($functions.Count -ne 1){throw 'Expected one finalization function'}
. ([scriptblock]::Create($functions[0].Extent.Text))
$checks=[Collections.Generic.List[object]]::new()
foreach($failure in @('none','kill','wait','flush','restore','dispose','hash','publish','multiple')){
    $state=@{order=[Collections.Generic.List[string]]::new();restore_attempts=0;restored=$false;published=$false;publish_attempted=$false}
    $errors=[Collections.Generic.List[string]]::new();$escaped=$false
    try{
        Invoke-ParentLossFinalization -Failures $errors -Cleanup {
            $state.order.Add('cleanup')
            if($failure -in @('kill','wait','flush','multiple')){throw ('synthetic '+$failure)}
        } -Restore {
            $state.order.Add('restore');$state.restore_attempts++
            if($failure -in @('restore','multiple')){throw 'synthetic restore'}
            $state.restored=$true
        } -Dispose {
            $state.order.Add('dispose')
            if($failure -in @('dispose','multiple')){throw 'synthetic dispose'}
        } -Publish {
            $state.order.Add('publish');$state.publish_attempted=$true
            if($failure -in @('hash','publish')){throw ('synthetic '+$failure)}
            $state.published=($errors.Count -eq 0 -and $state.restored)
        }
    }catch{$escaped=$true}
    $passed=($state.restore_attempts -eq 1 -and ($state.order -join ',') -ceq 'cleanup,restore,dispose,publish' -and
        $state.publish_attempted -and $state.published -eq ($failure -eq 'none') -and
        $escaped -eq ($failure -in @('hash','publish')) -and
        $state.restored -eq ($failure -notin @('restore','multiple')))
    $checks.Add(@{case=$failure;passed=$passed;restore_attempts=$state.restore_attempts;errors=@($errors)})
    if(-not $passed){throw ('Finalization regression: '+$failure)}
}
$streamFunctions=@($ast.FindAll({param($node)$node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Complete-ParentStreams'},$true))
if($streamFunctions.Count -ne 1){throw 'Expected one parent stream finalization function'}
. ([scriptblock]::Create($streamFunctions[0].Extent.Text))
function Close-StreamProcess($Item){$Item.close_calls++;$Item.closed=$true}
foreach($exited in @($false,$true)){
    $fakeParent=@{close_calls=0;closed=$false};$fakeObserver=@{process=@{HasExited=$exited}}
    $rejected=$false
    try{Complete-ParentStreams $fakeParent $fakeObserver}catch{$rejected=$true}
    $passed=($rejected -eq (-not $exited) -and $fakeParent.closed -eq $exited -and $fakeParent.close_calls -eq [int]$exited)
    $checks.Add(@{case=('parent_streams_oracle_exited_'+$exited);passed=$passed;close_calls=$fakeParent.close_calls})
    if(-not $passed){throw 'Parent stream ordering regression'}
}
$powerFunctions=@($ast.FindAll({param($node)$node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Invoke-QualificationPowerCfg'},$true))
if($powerFunctions.Count -ne 1){throw 'Expected one power observation decorator'}
. ([scriptblock]::Create($powerFunctions[0].Extent.Text))
$privateTestBase=[IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$root=Join-Path $privateTestBase ('devbrain-parentloss-guard-'+[Guid]::NewGuid().ToString('N'))
$null=[IO.Directory]::CreateDirectory($root)
$clock=[Diagnostics.Stopwatch]::StartNew()
$oraclePowerCfgImplementation={param([string[]]$Arguments) if($Arguments[0] -ceq '/fail'){throw 'synthetic power timeout'};return 'unchanged payload'}
try{
    foreach($failPower in @($false,$true)){
        $argsForPower=$(if($failPower){@('/fail')}else{@('/qh','synthetic-balanced-guid')})
        $rejected=$false;$result=$null
        try{$result=Invoke-QualificationPowerCfg -Arguments $argsForPower}catch{$rejected=$true}
        $attempt=([IO.File]::ReadAllLines((Join-Path $root 'oracle-power-commands.jsonl'))[-1]|ConvertFrom-Json)
        $passed=($rejected -eq $failPower -and $attempt.passed -eq (-not $failPower) -and
            ($attempt.arguments -join ',') -ceq ($argsForPower -join ',') -and $attempt.duration_seconds -ge 0 -and
            ($failPower -or $result -ceq 'unchanged payload'))
        $checks.Add(@{case=('power_command_failure_'+$failPower);passed=$passed;real_power_api_called=$false})
        if(-not $passed){throw 'Power decorator changed output, exceptions or arguments'}
    }
}finally{
    if(-not ([IO.Path]::GetFullPath($root)).StartsWith($privateTestBase,[StringComparison]::OrdinalIgnoreCase)){throw 'Guard cleanup escaped temporary directory'}
    $log=Join-Path $root 'oracle-power-commands.jsonl'
    if(Test-Path -LiteralPath $log){Remove-Item -LiteralPath $log}
    Remove-Item -LiteralPath $root
}
@{scope='devbrain_parent_loss_windows_guards';passed=$true;checks=@($checks);source_sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant();linux_started=$false;process_signalling_tested=$false;real_power_api_called=$false}|ConvertTo-Json -Depth 6
