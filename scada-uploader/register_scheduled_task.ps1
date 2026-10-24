# Registers "SCADA Portal Upload" for the current user.
# Requires Windows PowerShell 5.1 (Windows 10/11). No password is stored.
# Default: run only while this user is logged on. The PC must stay powered on.

param(
    [string]$InstallDir = "",
    [string]$TaskName = "SCADA Portal Upload",
    [int]$IntervalMinutes = 15,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

function ConvertTo-XmlText {
    param([string]$Text)
    if ($null -eq $Text) { return "" }
    return $Text.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace('"', "&quot;")
}

function Get-NextSlot {
    # :02, :17, :32, :47 so a file exported on the quarter hour can finish writing.
    $now = Get-Date
    $hourStart = $now.Date.AddHours($now.Hour)
    $offsets = @(2, 17, 32, 47)
    foreach ($offset in $offsets) {
        $candidate = $hourStart.AddMinutes($offset)
        if ($candidate -gt $now.AddSeconds(30)) {
            return $candidate
        }
    }
    return $hourStart.AddHours(1).AddMinutes(2)
}

if ($IntervalMinutes -lt 1) {
    Write-Host "IntervalMinutes must be at least 1."
    exit 1
}

if ($Unregister) {
    $existing = Get-ScheduledTask -TaskName $TaskName -TaskPath "\" -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "No task named '$TaskName' was found."
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath "\" -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

if (-not $InstallDir) {
    if ($PSScriptRoot) {
        $InstallDir = $PSScriptRoot
    } else {
        $InstallDir = (Get-Location).Path
    }
}

if (-not (Test-Path -LiteralPath $InstallDir)) {
    Write-Host "Install folder was not found: $InstallDir"
    exit 1
}
$InstallDir = (Resolve-Path -LiteralPath $InstallDir).Path
$exePath = Join-Path $InstallDir "scada-uploader.exe"
$configPath = Join-Path $InstallDir "config.json"

if (-not (Test-Path -LiteralPath $exePath)) {
    Write-Host "scada-uploader.exe was not found in:"
    Write-Host "  $InstallDir"
    Write-Host "Build it with build_windows.bat, copy dist\scada-uploader.exe and config.json into one folder, then pass that folder as -InstallDir."
    exit 1
}
if (-not (Test-Path -LiteralPath $configPath)) {
    Write-Host "config.json was not found next to the exe:"
    Write-Host "  $InstallDir"
    Write-Host "Copy config.json into that folder and set watch_folder to the SCADA CSV folder. This script does not read the config file."
    exit 1
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$start = Get-NextSlot
$startText = $start.ToString("yyyy-MM-ddTHH:mm:ss")
$intervalText = "PT{0}M" -f $IntervalMinutes
$exeXml = ConvertTo-XmlText $exePath
$dirXml = ConvertTo-XmlText $InstallDir
$configXml = ConvertTo-XmlText $configPath
$userXml = ConvertTo-XmlText $currentUser
$arguments = "--config &quot;$configXml&quot; --send"

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Read SCADA CSV files and POST pending readings. No status window. See scada_uploader.log.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>$intervalText</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>$startText</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$userXml</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$exeXml</Command>
      <Arguments>$arguments</Arguments>
      <WorkingDirectory>$dirXml</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

try {
    Register-ScheduledTask -TaskName $TaskName -TaskPath "\" -Xml $xml -Force | Out-Null
} catch {
    Write-Host "Could not register the task: $($_.Exception.Message)"
    exit 1
}

Write-Host "Registered '$TaskName'."
Write-Host "Folder: $InstallDir"
Write-Host "Command: scada-uploader.exe --config config.json --send"
Write-Host "First run: $startText, then every $IntervalMinutes minutes."
Write-Host "A second copy is not started if the previous run is still going."
Write-Host "Each run is limited to 10 minutes."
Write-Host "The task runs only while $currentUser is logged on."
Write-Host "It does not wake the PC. The PC must stay powered on."
Write-Host "watch_folder stays in config.json. Use a full path to the CSV folder."
Write-Host ""
Write-Host "There is no status window. After a run, open:"
Write-Host "  $InstallDir\scada_uploader.log"
Write-Host "  $InstallDir\scada_uploader.sqlite"
Write-Host ""
Write-Host "To also run when nobody is logged in:"
Write-Host "  1. Open Task Scheduler."
Write-Host "  2. Open Task Scheduler Library and find '$TaskName'."
Write-Host "  3. Right-click the task, choose Properties, then General."
Write-Host "  4. Select 'Run whether user is logged on or not'."
Write-Host "  5. Click OK and enter the Windows password when prompted."
Write-Host "Do not put that password in a script. Use a local disk path for the CSV folder."
Write-Host "A mapped drive letter is often missing when nobody is logged on."
Write-Host ""
Write-Host "The same change, which prompts for the password and does not store it here:"
Write-Host "  schtasks /Change /TN `"$TaskName`" /RU `"$currentUser`" /RP *"
Write-Host ""
Write-Host "Remove the task with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Unregister"
exit 0
