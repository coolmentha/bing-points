param(
  [string]$Mode = "state",
  [long]$Hwnd = 0,
  [string]$RunId = "",
  [string]$ExpectedEmail = "",
  [string]$ResultPath = "$env:TEMP\bing_points_rewards_desktop.txt"
)
$ErrorActionPreference = "Stop"
if (-not $RunId) { $RunId = [guid]::NewGuid().ToString("N") }
$lines = @("RUN_ID=$RunId")
$uiaReady = $false
$inv = [System.Globalization.CultureInfo]::InvariantCulture
$script:winHwnd = 0
$script:mouseNativeReady = $false
$script:taskTitle = [string][char]0x684C + [char]0x9762 + [char]0x5956 + [char]0x52B1 + [char]0x5E94 + [char]0x7528 + [char]0x6BCF + [char]0x5468 + [char]0x4E13 + [char]0x5C5E + [char]0x4EFB + [char]0x52A1

function Add-Line([string]$Text) {
  $script:lines += $Text
}

function Get-SafeName($Element) {
  if ($null -eq $Element) { return "" }
  try { return [string]$Element.Current.Name } catch { return "" }
}

function Get-ControlType($Element) {
  if ($null -eq $Element) { return "" }
  try { return [string]$Element.Current.ControlType.ProgrammaticName } catch { return "" }
}

function Initialize-MouseNative {
  if ($script:mouseNativeReady) { return $true }
  try {
    if (-not ("BingPointsMouseInput" -as [type])) {
      Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class BingPointsMouseInput {
  [DllImport("user32.dll", SetLastError=true)]
  public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll", SetLastError=true)]
  public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")]
  public static extern IntPtr GetForegroundWindow();
}
"@
    }
    $script:mouseNativeReady = $true
    return $true
  } catch {
    return $false
  }
}

function Get-ElementRect($Element) {
  if ($null -eq $Element) { return $null }
  try {
    $rect = $Element.Current.BoundingRectangle
    if ($null -eq $rect) { return $null }
    $x = [double]$rect.X
    $y = [double]$rect.Y
    $width = [double]$rect.Width
    $height = [double]$rect.Height
    if ([double]::IsNaN($x) -or [double]::IsNaN($y) -or
        [double]::IsNaN($width) -or [double]::IsNaN($height) -or
        [double]::IsInfinity($x) -or [double]::IsInfinity($y) -or
        [double]::IsInfinity($width) -or [double]::IsInfinity($height)) {
      return $null
    }
    if ($width -lt 4 -or $height -lt 4) { return $null }
    return [pscustomobject]@{
      X = $x
      Y = $y
      Width = $width
      Height = $height
      CenterX = [int][Math]::Round($x + ($width / 2))
      CenterY = [int][Math]::Round($y + ($height / 2))
    }
  } catch {
    return $null
  }
}

function Format-ElementRect($Rect) {
  if ($null -eq $Rect) { return "" }
  return [string]::Format($inv, "{0},{1},{2},{3}",
    [int][Math]::Round($Rect.X), [int][Math]::Round($Rect.Y),
    [int][Math]::Round($Rect.Width), [int][Math]::Round($Rect.Height))
}

function Test-SameOrAncestorElement($HitElement, $TargetElement) {
  if ($null -eq $HitElement -or $null -eq $TargetElement) { return $false }
  try { $targetId = [string]::Join(",", $TargetElement.GetRuntimeId()) } catch { return $false }
  $current = $HitElement
  $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
  for ($i = 0; $i -lt 10 -and $null -ne $current; $i++) {
    try {
      if ([string]::Join(",", $current.GetRuntimeId()) -eq $targetId) { return $true }
    } catch {}
    try { $current = $walker.GetParent($current) } catch { return $false }
  }
  return $false
}

function Test-MouseTargetAtPoint($TargetElement, $Rect) {
  if ($null -eq $TargetElement -or $null -eq $Rect) { return $false }
  try {
    $point = New-Object System.Windows.Point($Rect.CenterX, $Rect.CenterY)
    $hit = [System.Windows.Automation.AutomationElement]::FromPoint($point)
    return Test-SameOrAncestorElement $hit $TargetElement
  } catch {
    return $false
  }
}

function Invoke-MouseClickAt([int]$X, [int]$Y) {
  if (-not (Initialize-MouseNative)) { return $false }
  try {
    $null = [BingPointsMouseInput]::SetForegroundWindow([IntPtr]$script:winHwnd)
    Start-Sleep -Milliseconds 150
    $foreground = [BingPointsMouseInput]::GetForegroundWindow()
    if ($foreground.ToInt64() -ne $script:winHwnd) { return $false }
    if (-not [BingPointsMouseInput]::SetCursorPos($X, $Y)) { return $false }
    Start-Sleep -Milliseconds 150
    [BingPointsMouseInput]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 60
    [BingPointsMouseInput]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    return $true
  } catch {
    return $false
  }
}

function Get-Document {
  return $win.FindFirst(
    [System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
      "RootWebArea"
    ))
  )
}

function Get-DocumentWhenReady {
  $shell = $null
  try { $shell = New-Object -ComObject WScript.Shell } catch {}
  $windowProcesses = @(Get-Process -Name "microsoft-rewards-desktop" -ErrorAction SilentlyContinue)
  $target = $windowProcesses | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
  if ($null -ne $target -and $shell) {
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
      try { $null = $shell.AppActivate($target.Id) } catch {}
      Start-Sleep -Milliseconds 500
    }
  }
  return Get-Document
}function Get-All($Root) {
  if ($null -eq $Root) { return @() }
  return $Root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
  )
}

function Invoke-Element($Element) {
  if ($null -eq $Element) { return $false }
  try {
    $Element.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView()
    Start-Sleep -Milliseconds 200
  } catch {}
  try {
    $Element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    return $true
  } catch {}
  try {
    $Element.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
    return $true
  } catch {}
  try {
    $Element.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern).DoDefaultAction()
    return $true
  } catch {}
  return $false
}

function Invoke-TaskItemElement($Element) {
  $result = [pscustomobject]@{
    Invoked = $false
    Mode = "none"
    Fallback = "none"
    Rect = ""
  }
  if ($null -eq $Element) { return $result }

  $controlType = Get-ControlType $Element
  if ($controlType -ne "ControlType.Hyperlink") { return $result }
  try {
    if (-not [bool]$Element.Current.IsEnabled) { return $result }
  } catch {
    return $result
  }

  try {
    $Element.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView()
    Start-Sleep -Milliseconds 250
  } catch {}

  try {
    $Element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    $result.Invoked = $true
    $result.Mode = "invoke_pattern"
    return $result
  } catch {}

  $result.Fallback = "mouse"
  try {
    if ([bool]$Element.Current.IsOffscreen) { return $result }
    if (-not [bool]$Element.Current.IsEnabled) { return $result }
  } catch {
    return $result
  }
  $rect = Get-ElementRect $Element
  if ($null -eq $rect) { return $result }
  $result.Rect = Format-ElementRect $rect
  if (-not (Test-MouseTargetAtPoint $Element $rect)) { return $result }
  if (-not (Invoke-MouseClickAt $rect.CenterX $rect.CenterY)) { return $result }
  $result.Invoked = $true
  $result.Mode = "mouse"
  return $result
}

function Get-ActionableElement($Element) {
  $current = $Element
  $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
  for ($i = 0; $i -lt 6 -and $null -ne $current; $i++) {
    $controlType = $current.Current.ControlType.ProgrammaticName
    if ($controlType -eq "ControlType.Hyperlink" -or $controlType -eq "ControlType.Button") {
      return $current
    }
    try {
      $null = $current.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
      return $current
    } catch {}
    try {
      $null = $current.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
      return $current
    } catch {}
    $current = $walker.GetParent($current)
  }
  return $null
}

function Get-TaskProgress($Element, [bool]$AllowDocument) {
  if ($null -eq $Element) { return $null }
  $current = $Element
  $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
  for ($i = 0; $i -lt 8 -and $null -ne $current; $i++) {
    $isDocument = $current.Current.AutomationId -eq "RootWebArea"
    if (-not $isDocument -or $AllowDocument) {
      foreach ($e in (Get-All $current)) {
        $name = Get-SafeName $e
        if ($name -match "^([0-9]+)/([0-9]+) 个任务$") {
          return "$($Matches[1])/$($Matches[2])"
        }
      }
    }
    if ($isDocument) { break }
    $current = $walker.GetParent($current)
  }
  return $null
}

function Get-TaskProgressFromWindow {
  try {
    $documentName = ""
    $doc = Get-Document
    if ($null -ne $doc) { $documentName = Get-SafeName $doc }
    foreach ($e in (Get-All $win)) {
      $name = Get-SafeName $e
      if ($name -match "^([0-9]+)/([0-9]+) 个任务$") {
        $controlType = Get-ControlType $e
        if ($documentName.Contains($taskTitle) -or $controlType -eq "ControlType.Text") {
          return "$($Matches[1])/$($Matches[2])"
        }
      }
    }
  } catch {
    return $null
  }
  return $null
}

function Wait-TaskProgressFromWindow([string]$Previous, [int]$TimeoutMilliseconds = 12000) {
  $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
  $last = Get-TaskProgressFromWindow
  while ([DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $current = Get-TaskProgressFromWindow
    if ($current) { $last = $current }
    if ($Previous -and $current) {
      try {
        $beforeCount = [int]($Previous.Split("/")[0])
        $currentCount = [int]($current.Split("/")[0])
        if ($currentCount -gt $beforeCount) { return $current }
      } catch {}
    }
  }
  return $last
}

function Find-EarnTab {
  foreach ($e in (Get-All $win)) {
    $automationId = ""
    $name = Get-SafeName $e
    try { $automationId = [string]$e.Current.AutomationId } catch {}
    if ($automationId.Contains("-tab-/earn")) { return $e }
    $controlType = $e.Current.ControlType.ProgrammaticName
    if (($controlType -eq "ControlType.TabItem" -or $controlType -eq "ControlType.Button" -or
         $controlType -eq "ControlType.Hyperlink") -and $name -match "^(积分赚取|Earn)$") {
      return $e
    }
  }
  return $null
}

function Find-TaskCandidate($Document) {
  $documentName = Get-SafeName $Document
  if ($documentName -match $taskTitle) {
    return [pscustomobject]@{
      Element = $Document
      Actionable = $null
      IsTaskPage = $true
    }
  }

  $titleElement = $null
  foreach ($e in (Get-All $Document)) {
    $name = Get-SafeName $e
    if ($name -match $taskTitle) {
      $titleElement = $e
      break
    }
  }
  if ($null -eq $titleElement) { return $null }

  $actionable = Get-ActionableElement $titleElement
  $enabled = $false
  if ($null -ne $actionable) {
    try { $enabled = [bool]$actionable.Current.IsEnabled } catch {}
  }
  if (-not $enabled) {
    try { $enabled = [bool]$titleElement.Current.IsEnabled } catch {}
  }
  $current = $titleElement
  $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
  for ($i = 0; $i -lt 5 -and $null -ne $current; $i++) {
    $name = Get-SafeName $current
    if ($name -match "未启用|不可用|not enabled|not available") {
      $enabled = $false
      break
    }
    $current = $walker.GetParent($current)
  }

  return [pscustomobject]@{
    Element = $titleElement
    Actionable = if ($enabled) { $actionable } else { $null }
    IsTaskPage = $false
  }
}

function Find-VisibleEmail($Root) {
  if ($null -eq $Root) { return "" }
  $emailPattern = "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
  $preferred = @()
  $fallback = @()
  try {
    foreach ($e in (Get-All $Root)) {
      $name = Get-SafeName $e
      if (-not $name) { continue }
      $match = [regex]::Match($name, $emailPattern)
      if (-not $match.Success) { continue }
      $metadata = $name.ToLowerInvariant()
      try { $metadata += " " + ([string]$e.Current.AutomationId).ToLowerInvariant() } catch {}
      try { $metadata += " " + ([string]$e.Current.ClassName).ToLowerInvariant() } catch {}
      if ($metadata -match "account|profile|user|email|邮箱|账号|帐户") {
        $preferred += $match.Value
      } else {
        $fallback += $match.Value
      }
    }
  } catch {}
  foreach ($candidates in @($preferred, $fallback)) {
    $unique = @($candidates | ForEach-Object { $_.ToLowerInvariant() } | Sort-Object -Unique)
    if ($unique.Count -eq 1) { return $unique[0] }
    if ($unique.Count -gt 1) { return "" }
  }
  return ""
}

function Find-TaskItem($Document) {
  foreach ($e in (Get-All $Document)) {
    $name = Get-SafeName $e
    if ((Get-ControlType $e) -ne "ControlType.Hyperlink") { continue }
    $isScoreTask = $name -match "^\s*[0-9]+\s*(积分|分|points?)\s*,\s*\S+"
    if (-not $isScoreTask) { continue }
    try {
      if (-not [bool]$e.Current.IsEnabled) { continue }
    } catch { continue }
    return $e
  }
  return $null
}

try {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  Add-Type -AssemblyName UIAutomationClient
  Add-Type -AssemblyName UIAutomationTypes
  if ($Hwnd -ne 0) {
    $win = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
    $script:winHwnd = $Hwnd
  } else {
    $p = Get-Process microsoft-rewards-desktop -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $p) { Add-Line "AVAILABLE=0"; throw "NO_PROCESS" }
    $win = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
    $script:winHwnd = [long]$p.MainWindowHandle
  }
  if ($null -eq $win) { Add-Line "AVAILABLE=0"; throw "NO_WINDOW" }
  Add-Line "AVAILABLE=1"

  $doc = Get-DocumentWhenReady
  if ($null -eq $doc) {
    Add-Line "UIA_READY=0"
    Add-Line "DOC="
    Add-Line "TASK_STATE=uia_unavailable"
    throw "RootWebArea not found"
  }
  $uiaReady = $true
  Add-Line "UIA_READY=1"
  Add-Line "DOC=$(Get-SafeName $doc)"

  $desktopIdentity = Find-VisibleEmail $win
  Add-Line "DESKTOP_IDENTITY=$desktopIdentity"
  if ($ExpectedEmail) {
    if (-not $desktopIdentity) {
      Add-Line "IDENTITY_STATE=identity_unverified"
      Add-Line "SUCCESS=0"
      return
    }
    $sameIdentity = [string]::Equals(
      $desktopIdentity.Trim(),
      $ExpectedEmail.Trim(),
      [System.StringComparison]::OrdinalIgnoreCase
    )
    if (-not $sameIdentity) {
      Add-Line "IDENTITY_STATE=identity_mismatch"
      Add-Line "SUCCESS=0"
      return
    }
    Add-Line "IDENTITY_STATE=verified"
  } else {
    Add-Line "IDENTITY_STATE=not_checked"
  }

  $startDocumentName = ""
  $earnTab = Find-EarnTab
  if ($null -ne $earnTab) {
    $null = Invoke-Element $earnTab
    Start-Sleep -Seconds 3
    $doc = Get-Document
    if ($null -eq $doc) { throw "RootWebArea disappeared after switching to earn" }
    Add-Line "EARN_DOC=$(Get-SafeName $doc)"
  }

  $candidate = $null
  for ($attempt = 0; $attempt -lt 6 -and $null -eq $candidate; $attempt++) {
    $doc = Get-Document
    if ($null -eq $doc) { throw "RootWebArea disappeared while locating task" }
    $candidate = Find-TaskCandidate $doc
  }

  if ($null -eq $candidate) {
    Add-Line "TASK_STATE=no_task"
    Add-Line "SUCCESS=0"
    return
  }

  Add-Line "TASK_STATE=task"
  Add-Line "TASK=$(Get-SafeName $candidate.Element)"
  if ($null -eq $candidate.Actionable -and -not $candidate.IsTaskPage) {
    Add-Line "TASK_STATE=no_task"
    Add-Line "SUCCESS=0"
    return
  }

  $progress = Get-TaskProgress $candidate.Element $true
  if ($progress) { Add-Line "TASK_PROGRESS=$progress" }

  if ($Mode -eq "state") {
    Add-Line "SUCCESS=0"
    return
  }
  if ($Mode -ne "run") { throw "Unsupported mode: $Mode" }

  if (-not $candidate.IsTaskPage) {
    Add-Line "TASK_ELEMENT_TYPE=$(Get-ControlType $candidate.Actionable)"
    $candidateRect = Get-ElementRect $candidate.Actionable
    Add-Line "TASK_RECT=$(Format-ElementRect $candidateRect)"
    if (-not (Invoke-Element $candidate.Actionable)) {
      Add-Line "TASK_ERROR=weekly_task_not_invokable"
      Add-Line "SUCCESS=0"
      return
    }
    Start-Sleep -Seconds 8
  }

  $taskDocument = $null
  for ($attempt = 0; $attempt -lt 6 -and $null -eq $taskDocument; $attempt++) {
    $doc = Get-Document
    if ($null -eq $doc) { throw "RootWebArea disappeared after opening task" }
    if ((Get-SafeName $doc).Contains($taskTitle)) { $taskDocument = $doc; break }
    Start-Sleep -Seconds 1
  }
  if ($null -eq $taskDocument) {
    Add-Line "TASK_ERROR=weekly_document_not_found"
    Add-Line "SUCCESS=0"
    return
  }

  $before = Get-TaskProgressFromWindow
  if (-not $before) { $before = Get-TaskProgress $taskDocument $true }
  Add-Line "TASK_BEFORE=$before"
  $taskItem = Find-TaskItem $taskDocument
  if ($null -eq $taskItem) {
    Add-Line "TASK=0"
    Add-Line "TASK_STATE=task_item_missing"
    Add-Line "TASK_ERROR=weekly_item_missing"
    Add-Line "SUCCESS=0"
    return
  }

  Add-Line "TASK=$(Get-SafeName $taskItem)"
  Add-Line "TASK_ELEMENT_TYPE=$(Get-ControlType $taskItem)"
  $taskRect = Get-ElementRect $taskItem
  Add-Line "TASK_RECT=$(Format-ElementRect $taskRect)"
  $invokeResult = Invoke-TaskItemElement $taskItem
  Add-Line "TASK_INVOKE_MODE=$($invokeResult.Mode)"
  if ($invokeResult.Fallback -ne "none") {
    Add-Line "INVOKE_FALLBACK=$($invokeResult.Fallback)"
  }
  if (-not $invokeResult.Invoked) {
    Add-Line "TASK_ERROR=weekly_item_not_invokable"
    Add-Line "SUCCESS=0"
    return
  }
  Add-Line "TASK_INVOKED=1"
  Start-Sleep -Seconds 2

  foreach ($e in (Get-All $win)) {
    $automationId = ""
    $name = Get-SafeName $e
    try { $automationId = [string]$e.Current.AutomationId } catch {}
    if (($automationId -eq "back" -and $e.Current.ControlType.ProgrammaticName -eq "ControlType.Button") -or
        $name -match "^(返回|Back)$") {
      if (Invoke-Element $e) { Add-Line "BACK=1" }
      break
    }
  }
  $after = Wait-TaskProgressFromWindow $before 12000
  Add-Line "TASK_AFTER=$after"
  if ($before -and $after) {
    $beforeCount = [int]($before.Split("/")[0])
    $afterCount = [int]($after.Split("/")[0])
    if ($afterCount -gt $beforeCount) { Add-Line "SUCCESS=1" } else { Add-Line "SUCCESS=0" }
  } else {
    Add-Line "SUCCESS=0"
  }
} catch {
  if (-not $uiaReady) {
    Add-Line "UIA_READY=0"
    Add-Line "TASK_STATE=uia_unavailable"
  } elseif ($script:lines -notcontains "TASK_STATE=no_task") {
    Add-Line "TASK_STATE=error"
  }
  Add-Line "ERROR=$($_.Exception.Message)"
} finally {
  $lines | Set-Content -LiteralPath $ResultPath -Encoding UTF8
  $lines | ForEach-Object { Write-Output $_ }
}
