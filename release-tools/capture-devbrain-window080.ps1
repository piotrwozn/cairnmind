#Requires -Version 7.2
<#
Prepared, NOT RUN. A specific user request to use this Win32 fallback is required
before -Execute. Without it, this file does not compile or invoke any UI API.
Capture only the exact existing DevBrain UX window. No input, activation, resize,
desktop capture, CopyFromScreen/BitBlt fallback, elevation, or settings changes.
An image is evidence awaiting visual review, never an automatic UX PASS.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateRange(1,[long]::MaxValue)][long]$WindowHandle,
    [Parameter(Mandatory)][ValidateSet('DevBrain UX 80x24','DevBrain UX 120x36','DevBrain UX 180x55','DevBrain Capture 80x24','DevBrain Capture 120x36','DevBrain Capture 180x55')][string]$Title,
    [Parameter(Mandatory)][ValidateRange(1,[int]::MaxValue)][int]$ExpectedProcessId,
    [Parameter(Mandatory)][ValidateRange(1,[long]::MaxValue)][long]$ExpectedProcessStartFileTime,
    [Parameter(Mandatory)][string]$OutputPath,
    [switch]$Execute,
    [Parameter(DontShow)][switch]$InternalWorker
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$qualificationRoot='C:\Users\piotr\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\DevBrain\qualification'
if(-not $Execute){
    [ordered]@{status='prepared_not_run';specific_user_permission_required=$true;
        hwnd=$WindowHandle;title=$Title;pid=$ExpectedProcessId;process_start_filetime=$ExpectedProcessStartFileTime;
        output=$OutputPath;api='PrintWindow(PW_RENDERFULLCONTENT)';
        target_only=$true;input_or_activation=$false;desktop_fallback=$false;automatic_ux_pass=$false}|ConvertTo-Json
    return
}
function PlainOutput([string]$Path){
    if(-not [IO.Path]::IsPathFullyQualified($Path)){throw 'output_absolute_required'}
    $full=[IO.Path]::GetFullPath($Path)
    if(-not $full.StartsWith($qualificationRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or
       $full.Substring(2).Contains(':') -or [IO.Path]::GetFileName($full) -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*\.png$'){
        throw 'output_outside_private_qualification_root'
    }
    $cursor=[IO.Path]::GetDirectoryName($full)
    if(-not [IO.Directory]::Exists($cursor)){throw 'existing_output_directory_required'}
    while($cursor){
        if((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'output_reparse_refused'}
        $cursor=[IO.Path]::GetDirectoryName($cursor)
    }
    if(Test-Path -LiteralPath $full){throw 'output_already_exists'}
    return $full
}
$output=PlainOutput $OutputPath
$reportPath=$output+'.capture.json'
if(Test-Path -LiteralPath $reportPath){throw 'capture_report_already_exists'}
$scriptHash=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()

if(-not $InternalWorker){
    # Retain the actual child Process handle. A timeout kills only this worker,
    # never Windows Terminal or another process with a recycled numeric PID.
    $psi=[Diagnostics.ProcessStartInfo]::new()
    $psi.FileName=(Get-Process -Id $PID).Path
    $psi.UseShellExecute=$false
    $psi.CreateNoWindow=$true
    $psi.WindowStyle=[Diagnostics.ProcessWindowStyle]::Hidden
    $psi.RedirectStandardOutput=$true
    $psi.RedirectStandardError=$true
    foreach($arg in @('-NoLogo','-NoProfile','-NonInteractive','-File',$PSCommandPath,
        '-WindowHandle',[string]$WindowHandle,'-Title',$Title,'-ExpectedProcessId',[string]$ExpectedProcessId,
        '-ExpectedProcessStartFileTime',[string]$ExpectedProcessStartFileTime,'-OutputPath',$output,'-Execute','-InternalWorker')){
        $psi.ArgumentList.Add($arg)
    }
    $child=[Diagnostics.Process]::Start($psi)
    try{
        $stdout=$child.StandardOutput.ReadToEndAsync()
        $stderr=$child.StandardError.ReadToEndAsync()
        if(-not $child.WaitForExit(20000)){
            $child.Kill() # retained process object; no process tree/global kill
            if(-not $child.WaitForExit(5000)){throw 'capture_worker_exit_unverified'}
            @{status='blocked';reason='printwindow_worker_timeout';automatic_ux_pass=$false}|ConvertTo-Json
            exit 2
        }
        $null=$stdout.GetAwaiter().GetResult()
        $null=$stderr.GetAwaiter().GetResult()
        if($child.ExitCode-ne0 -or -not (Test-Path -LiteralPath $reportPath)){
            @{status='blocked';reason='capture_worker_failed';worker_exit=$child.ExitCode;automatic_ux_pass=$false}|ConvertTo-Json
            exit 2
        }
        $report=Get-Content -LiteralPath $reportPath -Raw|ConvertFrom-Json -AsHashtable
        if($report.status-cne'captured_pending_visual_review' -or $report.script_sha256-cne$scriptHash -or
           (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()-cne$scriptHash -or
           (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()-cne$report.png_sha256){
            throw 'capture_evidence_binding_changed'
        }
        $report|ConvertTo-Json -Depth 8
        return
    }finally{$child.Dispose()}
}

# The only branch that compiles/invokes UI code is the explicitly executed,
# bounded child above. Merely parsing this script does not compile this string.
$nativeCode=@'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
public static class DevBrainWindowCapture080 {
 [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left,Top,Right,Bottom; }
 [StructLayout(LayoutKind.Sequential)] struct Point { public int X,Y; }
 [StructLayout(LayoutKind.Sequential)] struct BitmapInfo {
  public uint Size; public int Width,Height; public ushort Planes,BitCount;
  public uint Compression,SizeImage; public int XPels,YPels; public uint ClrUsed,ClrImportant;
  public uint Color;
 }
 public sealed class Frame {
  public byte[] Pixels; public int Width,Height,Pid,ThreadId; public long StartFileTime;
  public string Executable,WindowClass; public int DistinctClientColors; public double ClientRange;
 }
 [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
 [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
 [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowTextW(IntPtr hwnd,StringBuilder text,int count);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassNameW(IntPtr hwnd,StringBuilder text,int count);
 [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd,out Rect rect);
 [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hwnd,out Rect rect);
 [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr hwnd,ref Point point);
 [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr value);
 [DllImport("user32.dll",SetLastError=true)] static extern bool PrintWindow(IntPtr hwnd,IntPtr dc,uint flags);
 [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleDC(IntPtr dc);
 [DllImport("gdi32.dll")] static extern IntPtr CreateDIBSection(IntPtr dc,ref BitmapInfo info,uint usage,out IntPtr bits,IntPtr section,uint offset);
 [DllImport("gdi32.dll")] static extern IntPtr SelectObject(IntPtr dc,IntPtr item);
 [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr item);
 [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr dc);
 [DllImport("gdi32.dll")] static extern bool GdiFlush();
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafeFileHandle CreateFileW(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle,StringBuilder text,uint size,uint flags);
 public static string FinalPath(SafeFileHandle handle) {
  var text=new StringBuilder(4096); uint count=GetFinalPathNameByHandleW(handle,text,(uint)text.Capacity,0);
  if(count==0 || count>=text.Capacity) throw new InvalidOperationException("canonical_handle_failed");
  string path=text.ToString(); return path.StartsWith(@"\\?\")?path.Substring(4):path;
 }
 public static string FinalPath(string path) {
  using(var handle=CreateFileW(path,128,7,IntPtr.Zero,3,0x02000000,IntPtr.Zero)) {
   if(handle.IsInvalid) throw new InvalidOperationException("canonical_open_failed");
   return FinalPath(handle);
  }
 }
 static string Title(IntPtr hwnd) { var s=new StringBuilder(256); if(GetWindowTextW(hwnd,s,s.Capacity)==0) throw new InvalidOperationException("window_title_unavailable"); return s.ToString(); }
 static string Class(IntPtr hwnd) { var s=new StringBuilder(256); if(GetClassNameW(hwnd,s,s.Capacity)==0) throw new InvalidOperationException("window_class_unavailable"); return s.ToString(); }
 static void Check(IntPtr hwnd,string title,Process owner,long start,uint thread,string klass,string exe) {
  uint pid; uint current=GetWindowThreadProcessId(hwnd,out pid);
  if(!IsWindow(hwnd)||!IsWindowVisible(hwnd)||IsIconic(hwnd)||owner.HasExited||pid!=owner.Id||current!=thread||
     owner.StartTime.ToUniversalTime().ToFileTimeUtc()!=start||Title(hwnd)!=title||Class(hwnd)!=klass||
     !String.Equals(FinalPath(owner.MainModule.FileName),exe,StringComparison.OrdinalIgnoreCase))
   throw new InvalidOperationException("target_identity_changed_or_unavailable");
 }
 public static Frame Capture(long value,string title,string exe,int expectedPid,long expectedStart) {
  IntPtr hwnd=new IntPtr(value); uint pid; uint thread=GetWindowThreadProcessId(hwnd,out pid);
  if(thread==0 || pid==0 || pid!=expectedPid) throw new InvalidOperationException("target_window_missing_or_replaced");
  using(var owner=Process.GetProcessById(checked((int)pid))) {
   IntPtr retained=owner.Handle; // retain process identity, including start time
   long start=owner.StartTime.ToUniversalTime().ToFileTimeUtc(); string klass=Class(hwnd);
   if(start!=expectedStart) throw new InvalidOperationException("target_process_birth_changed");
   string canonical=FinalPath(owner.MainModule.FileName);
   if(!String.Equals(canonical,exe,StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("target_not_installed_windows_terminal");
   if(owner.SessionId!=Process.GetCurrentProcess().SessionId) throw new InvalidOperationException("target_in_different_session");
   Check(hwnd,title,owner,start,thread,klass,exe);
   IntPtr previousDpi=SetThreadDpiAwarenessContext(new IntPtr(-4)); // only this worker thread, restored below
   if(previousDpi==IntPtr.Zero) throw new InvalidOperationException("thread_pixel_geometry_unavailable");
   IntPtr dc=IntPtr.Zero,bitmap=IntPtr.Zero,old=IntPtr.Zero,bits=IntPtr.Zero;
   try {
    Rect rect,client; Point origin=new Point();
    if(!GetWindowRect(hwnd,out rect)||!GetClientRect(hwnd,out client)||!ClientToScreen(hwnd,ref origin)) throw new InvalidOperationException("target_geometry_unavailable");
    int width=checked(rect.Right-rect.Left),height=checked(rect.Bottom-rect.Top);
    if(width<160||height<120||width>8192||height>4096||(long)width*height>16000000) throw new InvalidOperationException("target_geometry_out_of_bounds");
    int bytes=checked(width*height*4); dc=CreateCompatibleDC(IntPtr.Zero);
    if(dc==IntPtr.Zero) throw new InvalidOperationException("memory_dc_failed");
    var info=new BitmapInfo{Size=40,Width=width,Height=-height,Planes=1,BitCount=32,Compression=0,SizeImage=(uint)bytes};
    bitmap=CreateDIBSection(dc,ref info,0,out bits,IntPtr.Zero,0);
    if(bitmap==IntPtr.Zero||bits==IntPtr.Zero) throw new InvalidOperationException("memory_bitmap_failed");
    old=SelectObject(dc,bitmap);
    if(old==IntPtr.Zero||old==new IntPtr(-1)) throw new InvalidOperationException("select_bitmap_failed");
    var pixels=new byte[bytes]; Marshal.Copy(pixels,0,bits,bytes); // known blank buffer, no screen reads
    Check(hwnd,title,owner,start,thread,klass,exe);
    if(!PrintWindow(hwnd,dc,2)) throw new InvalidOperationException("printwindow_failed"); // PW_RENDERFULLCONTENT
    if(!GdiFlush()) throw new InvalidOperationException("gdi_flush_failed");
    Check(hwnd,title,owner,start,thread,klass,exe);
    Rect after;
    if(!GetWindowRect(hwnd,out after)||after.Left!=rect.Left||after.Top!=rect.Top||after.Right!=rect.Right||after.Bottom!=rect.Bottom)
     throw new InvalidOperationException("target_geometry_changed");
    Marshal.Copy(bits,pixels,0,bytes);
    // Reject an unrendered/flat client area. This is only a blank-image check;
    // actual OS-window inspection is still required for any UX judgment.
    int left=Math.Max(0,origin.X-rect.Left),top=Math.Max(0,origin.Y-rect.Top);
    int right=Math.Min(width,left+client.Right),bottom=Math.Min(height,top+client.Bottom);
    left+=Math.Max(8,(right-left)/10); right-=Math.Max(8,(right-left)/10);
    top+=Math.Max(8,(bottom-top)/10); bottom-=Math.Max(8,(bottom-top)/10);
    if(right-left<80||bottom-top<40) throw new InvalidOperationException("client_geometry_unusable");
    var colors=new HashSet<int>(); int minimum=255,maximum=0;
    for(int y=top;y<bottom;y+=2) for(int x=left;x<right;x+=2) {
     int i=(y*width+x)*4; int v=(pixels[i]+pixels[i+1]+pixels[i+2])/3;
     minimum=Math.Min(minimum,v);maximum=Math.Max(maximum,v);
     if(colors.Count<256) colors.Add((pixels[i]>>3)|((pixels[i+1]>>3)<<5)|((pixels[i+2]>>3)<<10));
    }
    if(colors.Count<8||maximum-minimum<16) throw new InvalidOperationException("blank_or_unrendered_client_image");
    return new Frame{Pixels=pixels,Width=width,Height=height,Pid=(int)pid,ThreadId=(int)thread,
     StartFileTime=start,Executable=canonical,WindowClass=klass,DistinctClientColors=colors.Count,ClientRange=maximum-minimum};
   } finally {
    if(old!=IntPtr.Zero&&old!=new IntPtr(-1)&&dc!=IntPtr.Zero) SelectObject(dc,old);
    if(bitmap!=IntPtr.Zero) DeleteObject(bitmap);
    if(dc!=IntPtr.Zero) DeleteDC(dc);
    SetThreadDpiAwarenessContext(previousDpi);
   }
  }
 }
}
'@
try{
    $packages=@(Get-AppxPackage -Name Microsoft.WindowsTerminal | Where-Object{$_.PackageFamilyName -ceq 'Microsoft.WindowsTerminal_8wekyb3d8bbwe'})
    if($packages.Count-ne1){throw 'installed_windows_terminal_identity_ambiguous'}
    $package=$packages[0]
    $expectedExe=Join-Path $package.InstallLocation 'WindowsTerminal.exe'
    $cursor=$expectedExe
    while($cursor){
        if((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'terminal_executable_reparse_refused'}
        $cursor=[IO.Path]::GetDirectoryName($cursor)
    }
    Add-Type -TypeDefinition $nativeCode
    Add-Type -AssemblyName System.Drawing.Common
    if(-not [DevBrainWindowCapture080]::FinalPath($expectedExe).Equals($expectedExe,[StringComparison]::OrdinalIgnoreCase)){
        throw 'terminal_executable_canonical_mismatch'
    }
    $parent=[IO.Path]::GetDirectoryName($output)
    if(-not [DevBrainWindowCapture080]::FinalPath($parent).Equals($parent,[StringComparison]::OrdinalIgnoreCase)){
        throw 'output_directory_canonical_mismatch'
    }
    $frame=[DevBrainWindowCapture080]::Capture($WindowHandle,$Title,$expectedExe,$ExpectedProcessId,$ExpectedProcessStartFileTime)
    $bitmap=[Drawing.Bitmap]::new($frame.Width,$frame.Height,[Drawing.Imaging.PixelFormat]::Format32bppRgb)
    try{
        $rect=[Drawing.Rectangle]::new(0,0,$frame.Width,$frame.Height)
        $locked=$bitmap.LockBits($rect,[Drawing.Imaging.ImageLockMode]::WriteOnly,[Drawing.Imaging.PixelFormat]::Format32bppRgb)
        try{
            if($locked.Stride-ne$frame.Width*4){throw 'unexpected_png_bitmap_stride'}
            [Runtime.InteropServices.Marshal]::Copy($frame.Pixels,0,$locked.Scan0,$frame.Pixels.Length)
        }finally{$bitmap.UnlockBits($locked)}
        $null=PlainOutput $output
        $file=[IO.FileStream]::new($output,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        try{
            if(-not [DevBrainWindowCapture080]::FinalPath($file.SafeFileHandle).Equals($output,[StringComparison]::OrdinalIgnoreCase)){
                throw 'output_file_canonical_mismatch'
            }
            $bitmap.Save($file,[Drawing.Imaging.ImageFormat]::Png)
            $file.Flush($true)
        }finally{$file.Dispose()}
    }finally{$bitmap.Dispose();[Array]::Clear($frame.Pixels,0,$frame.Pixels.Length)}
    if((Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()-cne$scriptHash){throw 'capture_script_changed'}
    $report=[ordered]@{schema_version=1;status='captured_pending_visual_review';automatic_ux_pass=$false;
        hwnd=$WindowHandle;title=$Title;pid=$frame.Pid;thread_id=$frame.ThreadId;process_start_filetime=$frame.StartFileTime;
        executable=$frame.Executable;window_class=$frame.WindowClass;package_version=[string]$package.Version;
        width=$frame.Width;height=$frame.Height;nonblank_client_check=$true;api='PrintWindow';flags=2;
        identity_verified_before_and_after=$true;desktop_capture=$false;input_or_activation=$false;
        png=$output;png_sha256=(Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant();
        script_sha256=$scriptHash;captured_utc=[DateTime]::UtcNow.ToString('o')}
    $json=[Text.UTF8Encoding]::new($false).GetBytes(($report|ConvertTo-Json -Depth 8))
    $file=[IO.FileStream]::new($reportPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{
        if(-not [DevBrainWindowCapture080]::FinalPath($file.SafeFileHandle).Equals($reportPath,[StringComparison]::OrdinalIgnoreCase)){throw 'report_file_canonical_mismatch'}
        $file.Write($json,0,$json.Length);$file.Flush($true)
    }finally{$file.Dispose()}
    exit 0
}catch{
    # No exception text or terminal content is emitted. Failed/blank captures
    # remain BLOCKED; there is deliberately no alternate capture mechanism.
    @{status='blocked';reason='target_capture_or_validation_failed';error_type=$_.Exception.GetType().Name;automatic_ux_pass=$false}|ConvertTo-Json
    exit 2
}
