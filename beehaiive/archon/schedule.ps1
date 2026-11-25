# Manage the Windows scheduled tasks that fire Archon trigger bindings.
# BeeHAIve runs this file with a fixed argument list; it never builds a command string.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('List', 'Install', 'Remove', 'Pause', 'Resume')]
    [string] $Action,
    [string] $Name = '',
    [string] $Exe = '',
    [string] $Config = '',
    [string] $WorkingDirectory = '',
    [int] $IntervalSeconds = 0
)

$ErrorActionPreference = 'Stop'
$Prefix = 'Archon-'

function ConvertTo-IsoTime($value) {
    if ($null -eq $value -or $value.Year -lt 2000) { return $null }
    return $value.ToUniversalTime().ToString('o')
}

function Assert-TaskName {
    if (-not $Name.StartsWith($Prefix)) { throw "A task name must start with $Prefix" }
}

switch ($Action) {
    'List' {
        $rows = @()
        foreach ($task in @(Get-ScheduledTask -TaskName "$Prefix*" -ErrorAction SilentlyContinue)) {
            $info = Get-ScheduledTaskInfo -InputObject $task
            $rows += [pscustomobject]@{
                name       = $task.TaskName
                state      = [string] $task.State
                lastRun    = ConvertTo-IsoTime $info.LastRunTime
                nextRun    = ConvertTo-IsoTime $info.NextRunTime
                lastResult = $info.LastTaskResult
            }
        }
        ConvertTo-Json -InputObject @($rows) -Depth 3 -Compress
    }
    'Install' {
        Assert-TaskName
        if ($IntervalSeconds -lt 60) { throw 'IntervalSeconds must be at least 60' }
        $minutes = [int][Math]::Ceiling($IntervalSeconds / 60)
        # conhost --headless runs the console program without opening a window
        # every time the task fires.
        $argument = '--headless "{0}" trigger fire --config "{1}"' -f $Exe, $Config
        $taskAction = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument $argument -WorkingDirectory $WorkingDirectory
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $minutes)
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
        # Registering anew enables the task, so a reinstall while triggers are
        # paused, after an edit to the interval or folder, disables it again.
        $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        $paused = $null -ne $existing -and [string] $existing.State -eq 'Disabled'
        Register-ScheduledTask -TaskName $Name -Action $taskAction -Trigger $trigger -Settings $settings -Description 'Fires an Archon trigger binding. Created by BeeHAIve.' -Force | Out-Null
        if ($paused) { Disable-ScheduledTask -TaskName $Name | Out-Null }
        'installed'
    }
    'Remove' {
        Assert-TaskName
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        'removed'
    }
    # Pause and Resume answer with how many tasks they changed. Disabling a task
    # only stops its later starts: a fire hands its run to a separate archon
    # process and ends within seconds, so a run already going keeps going.
    'Pause' {
        $count = 0
        foreach ($task in @(Get-ScheduledTask -TaskName "$Prefix*" -ErrorAction SilentlyContinue)) {
            if ([string] $task.State -ne 'Disabled') {
                Disable-ScheduledTask -InputObject $task | Out-Null
                $count++
            }
        }
        "$count"
    }
    'Resume' {
        # ASSUMPTION: every disabled Archon- task was disabled by Pause. BeeHAIve
        # never disables a single task, so only a hand edit in Task Scheduler
        # leaves one off, and Resume turns that one back on as well.
        $count = 0
        foreach ($task in @(Get-ScheduledTask -TaskName "$Prefix*" -ErrorAction SilentlyContinue)) {
            if ([string] $task.State -eq 'Disabled') {
                Enable-ScheduledTask -InputObject $task | Out-Null
                $count++
            }
        }
        "$count"
    }
}
