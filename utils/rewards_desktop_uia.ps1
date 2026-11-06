param(
  [string]$Mode = "state",
  [long]$Hwnd = 0,
  [string]$ResultPath = "$env:TEMP\bing_points_rewards_desktop.txt"
)
$ErrorActionPreference = "Stop"
$lines = @()
try {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  Add-Type -AssemblyName UIAutomationClient
  Add-Type -AssemblyName UIAutomationTypes
  if ($Hwnd -ne 0) {
    $win = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
  } else {
    # 兼容手动调试：正常入口由 Python 传入 HWND，避免子进程看不到 AppX 进程。
    $p = Get-Process microsoft-rewards-desktop -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $p) { $lines += 'AVAILABLE=0'; throw 'NO_PROCESS' }
    $win = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
  }
  if ($null -eq $win) { $lines += 'AVAILABLE=0'; throw 'NO_WINDOW' }
  $lines += 'AVAILABLE=1'
  $doc = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'RootWebArea')))
  $lines += "DOC=$($doc.Current.Name)"
  function Get-All { $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition) }
  function Get-TaskProgress {
    $doc = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'RootWebArea')))
    if ($null -eq $doc -or $doc.Current.Name -notmatch '桌面奖励应用每周专属任务') { return $null }
    foreach ($e in $doc.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
      if ($e.Current.Name -match '^([0-9]+)/([0-9]+) 个任务$') { return "$($Matches[1])/$($Matches[2])" }
    }
    return $null
  }
  function Add-Progress { $v = Get-TaskProgress; if ($v) { $script:lines += "TASK_PROGRESS=$v" } }
  Add-Progress
  if ($Mode -eq 'earn') {
    foreach ($e in (Get-All)) {
      if ($e.Current.AutomationId -and $e.Current.AutomationId.Contains('-tab-/earn')) {
        try { $e.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() } catch {}
        break
      }
    }
    Start-Sleep -Seconds 4
    $doc = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'RootWebArea')))
    if ($doc.Current.Name -notmatch '桌面奖励应用每周专属任务') {
      foreach ($e in (Get-All)) {
        if ($e.Current.ControlType -eq [System.Windows.Automation.ControlType]::Hyperlink -and $e.Current.Name -match '桌面奖励应用每周专属任务') {
          try { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch {}
          break
        }
      }
      Start-Sleep -Seconds 5
    }
  }
  if ($Mode -eq 'run') {
    # 先切到“积分赚取”并打开周任务，避免用首页/其他页面的进度误判。
    $doc = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'RootWebArea')))
    if ($null -eq $doc -or $doc.Current.Name -notmatch '桌面奖励应用每周专属任务') {
      foreach ($e in (Get-All)) {
        if ($e.Current.AutomationId -and $e.Current.AutomationId.Contains('-tab-/earn')) {
          try { $e.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() } catch {}
          break
        }
      }
      Start-Sleep -Seconds 4
      $doc = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'RootWebArea')))
      if ($doc.Current.Name -notmatch '桌面奖励应用每周专属任务') {
        foreach ($e in (Get-All)) {
          if ($e.Current.ControlType -eq [System.Windows.Automation.ControlType]::Hyperlink -and $e.Current.Name -match '桌面奖励应用每周专属任务') {
            try { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch {}
            break
          }
        }
        Start-Sleep -Seconds 5
      }
    }
    $before = Get-TaskProgress
    $lines += "TASK_BEFORE=$before"
    $task = $null
    $doc = $win.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'RootWebArea')))
    if ($null -eq $doc -or $doc.Current.Name -notmatch '桌面奖励应用每周专属任务') {
      $lines += 'TASK_ERROR=weekly_document_not_found'
      $lines += 'SUCCESS=0'
      $task = $null
    } else {
      foreach ($e in $doc.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($e.Current.ControlType -eq [System.Windows.Automation.ControlType]::Hyperlink -and $e.Current.IsEnabled -and $e.Current.Name -match '^10[\s\S]*') { $task=$e; break }
      }
    }
    if ($task -ne $null) {
      $lines += "TASK=$($task.Current.Name)"
      try {
        $task.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView()
        Start-Sleep -Milliseconds 700
        $task.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        $lines += 'TASK_INVOKED=1'
      } catch { $lines += "TASK_ERROR=$($_.Exception.Message)" }
      Start-Sleep -Seconds 8
      foreach ($e in (Get-All)) {
        if ($e.Current.AutomationId -eq 'back' -and $e.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button) {
          try { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); $lines += 'BACK=1' } catch {}
          break
        }
      }
      Start-Sleep -Seconds 8
      $after = Get-TaskProgress
      $lines += "TASK_AFTER=$after"
      if ($before -and $after) {
        $b=[int]($before.Split('/')[0]); $a=[int]($after.Split('/')[0]); if ($a -gt $b) { $lines += 'SUCCESS=1' } else { $lines += 'SUCCESS=0' }
      } else { $lines += 'SUCCESS=0' }
    } else { $lines += 'TASK=0'; $lines += 'SUCCESS=0' }
  }
} catch {
  $lines += "ERROR=$($_.Exception.Message)"
} finally {
  $lines | Set-Content -LiteralPath $ResultPath -Encoding UTF8
  $lines | ForEach-Object { Write-Output $_ }
}
